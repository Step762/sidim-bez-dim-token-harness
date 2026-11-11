"""Offline routing sensitivity and real gateway budget stress, without LIVE."""
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import harness
from budget_manager import BudgetManager
from diagnostics.reward_routing import POLICIES, compare, replay
from diagnostics.token_economics import replay as old_replay
from diagnostics.trace import fallback_controls
from llm_gateway import LLMGateway, LLMError
from test_llm import MATH, MATH_PLAN, envelope
from test_token_economics import row


def paid(result):
    return [key for key, phase in result['order'] if phase == 'fallback']


class RoutingTests(unittest.TestCase):
    def test_a_is_existing_production_replay_and_f_zero_band_matches_it(self):
        rows = [row('a'), row('b', reward=8), row('free', local_validated=True, local_correct=True)]
        original = old_replay(rows)
        for policy in ('A_q5', 'F_adaptive'):
            result = replay(rows, policy, band=0)
            for key in ('order', 'correct', 'wrong', 'spent', 'reserved', 'reward', 'calls'):
                self.assertEqual(result[key], original[key])

    def test_local_work_precedes_paid_in_every_arm_even_at_zero_budget(self):
        rows = [row('paid', reward=1000), row('free', local_validated=True, local_correct=True)]
        for policy in POLICIES:
            result = replay(rows, policy, budget=0)
            self.assertEqual((result['correct'], result['calls']), (1, 0))

    def test_b_prioritizes_native_local_probes_by_complexity(self):
        rows = [row('a', native_form=False, complexity=1), row('b', native_form=True, complexity=3),
                row('c', native_form=True, complexity=1)]
        self.assertEqual([key for key, phase in replay(rows, 'B_local')['order'] if phase == 'local'], ['c', 'b', 'a'])

    def test_c_uses_reward_over_cost_and_e_keeps_fixed_cheap_stage(self):
        rows = [row('rich', reward=1000, estimated_cost='100'), row('cheap', reward=1, estimated_cost='10'),
                row('medium', reward=2, estimated_cost='20')]
        self.assertEqual(paid(replay(rows, 'C_reward_cost'))[0], 'rich')
        self.assertEqual(paid(replay(rows, 'E_two_stage')), ['cheap', 'medium', 'rich'])

    def test_d_zero_lower_probability_does_not_silently_discard_task(self):
        result = replay([row()], 'D_conservative', band=1)
        self.assertEqual((result['calls'], result['correct']), (1, 1))

    def test_full_reserve_not_expected_cost_controls_every_policy(self):
        for policy in POLICIES:
            result = replay([row(estimated_cost='1')], policy, budget=109)
            self.assertEqual((result['calls'], result['spent']), (0, '0.0'))

    def test_unknown_usage_or_timeout_freezes_without_retries(self):
        for policy in POLICIES:
            for fault in ({'usage_known': False}, {'llm_seconds': 31}):
                rows = [row('a', **fault), row('b', **fault)]
                result = replay(rows, policy, budget=1000)
                self.assertEqual(result['calls'], 1)
                self.assertTrue(result['frozen'])
                self.assertEqual(result['reserved'], '110.0')

    def test_actual_outcomes_cannot_rank_first_and_judge_correctness_cannot_adapt(self):
        rows = [row(str(i), reward=i+1) for i in range(7)]
        changed = deepcopy(rows)
        for r in changed:
            r.update(llm_correct=False, input_tokens=5, output_tokens=1)
        for policy in POLICIES:
            self.assertEqual(paid(replay(rows, policy))[0], paid(replay(changed, policy))[0])
        wrong = deepcopy(rows)
        for r in wrong:
            r['llm_correct'] = False  # External grading is not fed back into scheduling.
        self.assertEqual(replay(rows, 'F_adaptive')['order'], replay(wrong, 'F_adaptive')['order'])

    def test_f_uses_only_confirmed_history_to_redistribute_remaining_budget(self):
        rows = [row(f'a{i}', reward=100) for i in range(3)] + [row('math', reward=4), row('search', reward=8, task_type='search')]
        cheap = deepcopy(rows)
        for r in cheap[:3]:
            r.update(input_tokens=5, output_tokens=1)
        expensive = deepcopy(rows)
        for r in expensive[:3]:
            r.update(input_tokens=100, output_tokens=100)
        a = paid(replay(cheap, 'F_adaptive')); b = paid(replay(expensive, 'F_adaptive'))
        self.assertEqual(a[:3], b[:3])
        self.assertEqual((a[3], b[3]), ('math', 'search'))

    def test_no_network_mutation_or_unapproved_source_and_repeat_is_deterministic(self):
        rows = [row('a'), row('b')]; before = deepcopy(rows)
        with patch('socket.socket.connect', side_effect=AssertionError('no network')):
            self.assertEqual(compare(rows, budgets=(1000,), bands=('0',)), compare(rows, budgets=(1000,), bands=('0',)))
        self.assertEqual(rows, before)
        for bad in (row(scope='holdout'), row(scope='unknown'), row(trace_source='unapproved'),
                    row(trace_source='real'), row(api_key='synthetic-canary')):
            with self.assertRaises(ValueError):
                replay([bad])
        for band in ('-1', '2'):
            with self.assertRaises(ValueError):
                replay(rows, band=band)


class BudgetStressTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('no real HTTP')))

    def run_fault(self, fault='ok', *, budget=5000, spent=0):
        manager = BudgetManager(budget)
        if spent:
            manager.reserve('history', 'previous', 0, spent)
            manager.settle('history', 0, spent)
        calls = []
        async def respond(request):
            calls.append(1)
            if fault == 'timeout':
                raise httpx.ReadTimeout('synthetic timeout')
            if fault == 'proxy_error':
                return httpx.Response(503, json={'error': {'message': 'synthetic-canary'}})
            value = envelope(json.dumps(MATH_PLAN))
            if fault == 'partial_usage':
                value['usage'] = {'prompt_tokens': 100}
            elif fault in ('output_overrun', 'input_underestimate'):
                value['usage'] = {'prompt_tokens': 9999 if fault == 'input_underestimate' else 100,
                                  'completion_tokens': 65 if fault == 'output_overrun' else 20}
            return httpx.Response(200, json=value)
        gateway = LLMGateway('http://budget-mock.invalid/v1', 'synthetic-canary', manager=manager,
                             max_tokens=64, transport=httpx.MockTransport(respond))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for key, text in (('paid1', MATH), ('paid2', MATH), ('free', 'Compute 7 + 2')):
                task = root/key; task.mkdir()
                (task/'task.json').write_text(json.dumps({'task_type': 'math', 'instruction': text, 'reward': 1}))
            report = {}; output = io.StringIO()
            with fallback_controls(), redirect_stdout(output):
                counts = asyncio.run(harness.run_tasks(root, manager=manager, llm=gateway, budget=budget, report=report))
            self.assertEqual((root/'free/answer.txt').read_text(), '9\n')
            self.assertEqual((root/'free/answer.txt').stat().st_mode & 0o777, 0o644)
            self.assertNotIn('synthetic-canary', output.getvalue() + str(report))
        return manager.snapshot(), calls, counts

    def test_zero_low_almost_exhausted_and_expensive_request_never_send(self):
        for budget, spent in ((0, 0), (1, 0), (5000, 4999)):
            snapshot, calls, counts = self.run_fault(budget=budget, spent=spent)
            self.assertEqual(calls, [])
            self.assertEqual(counts['solved'], 1)
            self.assertEqual(snapshot['spent'], Decimal(spent))

    def test_valid_usage_is_settled_exactly_and_shared_across_tasks(self):
        snapshot, calls, counts = self.run_fault()
        self.assertEqual((len(calls), counts['solved']), (2, 3))
        self.assertEqual((snapshot['spent'], snapshot['reserved']), (60, 0))
        self.assertFalse(snapshot['frozen'])

    def test_timeout_proxy_error_and_partial_usage_block_next_http(self):
        for fault in ('timeout', 'proxy_error', 'partial_usage'):
            snapshot, calls, counts = self.run_fault(fault)
            self.assertEqual(len(calls), 1)
            self.assertEqual(counts['solved'], 1)
            self.assertTrue(snapshot['frozen']); self.assertGreater(snapshot['reserved'], 0)

    def test_known_output_overrun_or_input_underestimate_is_visible_and_stops_next(self):
        for fault, cost in (('output_overrun', Decimal('75')), ('input_underestimate', Decimal('1019.9'))):
            snapshot, calls, _ = self.run_fault(fault)
            self.assertEqual(len(calls), 1)
            self.assertTrue(snapshot['frozen'])
            self.assertEqual(snapshot['spent'], cost)
            self.assertEqual(snapshot['reserved'], 0)


if __name__ == '__main__':
    unittest.main()
