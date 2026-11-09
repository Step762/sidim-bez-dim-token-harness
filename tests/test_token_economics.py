"""Replay invariants, independent arithmetic, no network and no oracle ranking."""
import copy
from fractions import Fraction
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from diagnostics.token_economics import (replay,synthetic_cohort,compare,POLICIES,
    write_jsonl,read_jsonl,historical_summaries,failure_class,forecast_error,compare_fixed_pairs)


def row(key='a',**changes):
    value=synthetic_cohort(count=1)[0]
    value.update(task_id=key,task_type='math',reward=4,blocker='',local_validated=False,
        local_correct=False,local_seconds=0.01,eligible=True,llm_validated=True,llm_correct=True,
        llm_seconds=0.1,estimated_input=100,max_tokens=100,input_tokens=50,output_tokens=10,
        estimated_cost='60',estimated_success='3/4',usage_known=True,reasoning_tokens=None,cost='15.0')
    value.update(changes);return value


class TokenEconomicsTests(unittest.TestCase):
    def test_production_replay_settles_and_releases_unused_reserve(self):
        r=replay([row('a'),row('b')],budget=125)
        self.assertEqual((r['correct'],r['calls'],r['spent'],r['reserved']),(2,2,'30.0','0.0'))
        self.assertFalse(r['frozen'])

    def test_full_reserve_blocks_call_even_if_hypothetical_actual_cost_fits(self):
        r=replay([row()],budget=100)
        self.assertEqual((r['calls'],r['correct'],r['spent']),(0,0,'0.0'))
        self.assertEqual(r['stop_reasons'],{'budget_blocked':1})

    def test_insufficient_initial_reserve_does_not_stop_free_local_answer(self):
        r=replay([row('paid'),row('free',local_validated=True,local_correct=True,eligible=False)],budget=0)
        self.assertEqual((r['correct'],r['calls']),(1,0))

    def test_unknown_usage_freezes_remaining_calls_and_retains_reserve(self):
        r=replay([row('a',usage_known=False),row('b')],budget=1000)
        self.assertEqual((r['calls'],r['correct'],r['spent'],r['reserved']),(1,0,'0.0','110.0'))
        self.assertTrue(r['frozen']);self.assertFalse(r['cost_known'])

    def test_accounted_output_is_not_charged_twice_for_reasoning(self):
        r=replay([row(output_tokens=10,reasoning_tokens=4)],budget=1000)
        self.assertEqual(r['spent'],'15.0')

    def test_known_overrun_is_visible_and_freezes_without_erasing_known_cost(self):
        r=replay([row('a',output_tokens=101),row('b')],budget=1000)
        self.assertEqual((r['calls'],r['spent'],r['reserved']),(1,'106.0','0.0'))
        self.assertTrue(r['frozen']);self.assertTrue(r['cost_known'])

    def test_deadline_during_paid_attempt_retains_uncertain_claim(self):
        r=replay([row(llm_seconds=2)],deadline=1)
        self.assertEqual((r['correct'],r['calls'],r['reserved']),(0,1,'110.0'))
        self.assertTrue(r['frozen']);self.assertEqual(r['simulated_seconds'],1)

    def test_exact_deadline_does_not_publish_and_prior_answers_survive(self):
        r=replay([row('a',local_validated=True,local_correct=True,local_seconds=0.5),
                  row('b',local_validated=True,local_correct=True,local_seconds=0.5)],deadline=1)
        self.assertEqual(r['correct'],1)
        self.assertEqual(r['stop_reasons'],{'solved':1,'timed_out':1})

    def test_local_operation_timeout_does_not_block_later_local_control(self):
        r=replay([row('a',local_seconds=2),row('b',local_validated=True,local_correct=True)],
                 policy='local_first',operation_timeout=1)
        self.assertEqual((r['correct'],r['calls']),(1,0))

    def test_wrong_answers_and_failed_proposals_do_not_earn_reward(self):
        r=replay([row('a',llm_correct=False),row('b',llm_validated=False)],budget=1000)
        self.assertEqual((r['correct'],r['wrong'],r['reward'],r['spent']),(0,1,0,'30.0'))

    def test_zero_reward_tasks_not_dropped_at_ample_budget(self):
        for policy in POLICIES:
            self.assertEqual(replay([row(reward=0)],policy)['correct'],1)

    def test_actual_outcomes_cannot_choose_the_first_task(self):
        original=[row('a',reward=2),row('b',reward=4,estimated_cost='80')]
        changed=copy.deepcopy(original)
        changed[0].update(input_tokens=1,output_tokens=1,llm_correct=False,llm_seconds=1)
        changed[1].update(input_tokens=90,output_tokens=90,llm_correct=True,llm_seconds=2)
        for policy in POLICIES:
            a=replay(original,policy)['order'];b=replay(changed,policy)['order']
            self.assertEqual(next(x for x in a if x[1]=='fallback'),next(x for x in b if x[1]=='fallback'))

    def test_reward_and_cost_and_expected_policies_have_distinct_documented_scores(self):
        rows=[row('a',reward=5,estimated_cost='10',estimated_success='1/10'),
              row('b',reward=9,estimated_cost='20',estimated_success='9/10')]
        for policy,key in (('reward_first','b'),('cost_first','a'),('expected_reward','b')):
            first=next(v[0] for v in replay(rows,policy)['order'] if v[1]=='fallback')
            self.assertEqual(first,key)

    def test_no_network_no_mutation_deterministic_and_fair_matrix(self):
        rows=synthetic_cohort(count=12);before=copy.deepcopy(rows)
        with patch('socket.socket.connect',side_effect=AssertionError('network')):
            self.assertEqual(compare(rows),compare(rows))
        self.assertEqual(rows,before)
        self.assertEqual(len(compare(rows)),20)

    def test_profiles_preserve_rewards_complexity_and_independent_task_outcomes(self):
        cheap=synthetic_cohort();long=synthetic_cohort('long_output');time=synthetic_cohort('time_pressure')
        fields=('task_id','reward','complexity','eligible','local_correct','llm_correct')
        for other in (long,time):
            self.assertEqual([[r[k] for k in fields] for r in cheap],[[r[k] for k in fields] for r in other])

    def test_coding_never_executes_or_gets_a_false_done(self):
        for blocker in ('unsafe',''):
            r=replay([row(task_type='coding',blocker=blocker,local_validated=True,llm_validated=True)])
            self.assertEqual((r['correct'],r['calls'],r['wrong']),(0,0,0))

    def test_historical_probe_summaries_cannot_be_used_as_quality_tape(self):
        values=historical_summaries()
        self.assertEqual(sum(Fraction(r['cost']) for r in values),Fraction('155.9'))
        self.assertIsNone(values[0]['correct']);self.assertIsNone(values[0]['seconds'])
        with self.assertRaises((KeyError,ValueError)):replay(values)

    def test_reject_invalid_models_duplicates_predictions_and_times(self):
        for rows in ([row(),row()], [row(model='invented')], [row(max_tokens=0)],
                     [row(estimated_input=0)], [row(estimated_success='2')],
                     [row(estimated_cost='0')], [row(reward=-1)], [row(local_seconds=float('nan'))],
                     [row(scope='holdout')], [row(task_id='../escape')]):
            with self.subTest(rows=rows),self.assertRaises(ValueError):replay(rows)
        for deadline in (0,3541,float('nan')):
            with self.assertRaises(ValueError):replay([row()],deadline=deadline)

    def test_jsonl_roundtrip_and_no_silent_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'traces.jsonl';rows=synthetic_cohort(count=4)
            write_jsonl(path,rows);self.assertEqual(read_jsonl(path),rows)
            with self.assertRaises(FileExistsError):write_jsonl(path,rows)

    def test_trace_writer_refuses_payload_keys_before_creating_output(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'must-not-exist'
            with self.assertRaises(ValueError):write_jsonl(p,[row(api_key='synthetic canary')])
            self.assertFalse(p.exists())

    def test_pairs_are_graded_independently_and_empty_denominator_is_unknown(self):
        r=replay([row('a'),row('b',llm_validated=False)],pairs=[['a','b']])
        self.assertEqual(r['qualification'],{'pairs':1,'2/2':0,'1/2':1,'0/2':0,'pass_fraction':0})
        self.assertIsNone(replay([row()])['qualification']['pass_fraction'])

    def test_frozen_pair_evaluation_resets_budget_and_does_not_tune_predictions(self):
        rows=[row('a',scope='holdout'),row('b',scope='holdout')]
        before=copy.deepcopy(rows)
        results=compare_fixed_pairs(rows,[{'tasks':['a','b']}])
        self.assertTrue(all(r['qualification']['2/2']==1 for r in results))
        self.assertEqual(rows,before)
        with self.assertRaises(ValueError):replay(rows)
        with self.assertRaises(ValueError):replay([row()],pairs=[['a','missing']])

    def test_forecast_quality_is_evaluated_without_learning_from_outcomes(self):
        rows=[row()];before=copy.deepcopy(rows);r=forecast_error(rows)
        self.assertEqual(r['brier'],0.0625)
        self.assertEqual((r['mean_actual_cost_on_tape'],r['cost_mae']),(15,45))
        self.assertEqual(rows,before)
        r=forecast_error([row(usage_known=False)])
        self.assertEqual(r['known_cost_rows'],0);self.assertIsNone(r['cost_mae'])

    def test_jsonl_special_files_and_oversize_are_rejected(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'fifo';os.mkfifo(p)
            with self.assertRaises(ValueError):read_jsonl(p)
            p=Path(d)/'oversize'
            with p.open('wb') as stream:stream.truncate(10*1024*1024+1)
            with self.assertRaises(ValueError):read_jsonl(p)

if __name__=='__main__':unittest.main()
