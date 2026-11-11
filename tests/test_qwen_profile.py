"""Synthetic approvals and mock-only calls; real Proxy/history remain untouched."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from diagnostics import qwen_profile as p
from diagnostics import fallback_pilot


class QwenProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qwen-profile-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.enterContext(patch.object(p, 'STATE_ROOT', self.state))
        self.enterContext(patch.dict(os.environ, {'BUDGET': '200', 'LLM_BASE_URL': 'https://proxy.synthetic.invalid/v1',
                                                'LLM_API_KEY': 'SYNTHETIC_KEY_CANARY_934'}, clear=True))
        self.enterContext(patch.object(socket.socket, 'connect', side_effect=AssertionError('no HTTP')))
        self.enterContext(patch.object(socket, 'getaddrinfo', side_effect=AssertionError('no DNS')))
        self.approval = self.root / 'approval.json'
        self.data = {'proxy_confirmed': True, 'model_confirmed': True, 'output_cap_confirmed': True,
                     'history_complete': True, 'history': [], 'usage_contract': 'noreason_completion',
                     'contract_sha256': p.digest(p.contract()), 'quota_scope_sha256': p.digest('synthetic quota'),
                     'endpoint_sha256': hashlib.sha256(b'https://proxy.synthetic.invalid/v1/chat/completions').hexdigest()}
        self.save()
        self.calls = []

    def save(self):
        self.approval.write_text(json.dumps(self.data))
        self.approval.chmod(0o600)

    def response(self, request):
        body = json.loads(request.content)
        self.calls.append(body)
        self.assertEqual((body['model'], body['max_tokens'], body['stream']), (p.MODEL, 32, False))
        self.assertEqual(request.extensions['timeout']['read'], 20)
        return httpx.Response(200, json={'model': p.MODEL, 'usage': {'prompt_tokens': 100, 'completion_tokens': 6, 'total_tokens': 106},
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"value":"4"}'}}]})

    def run_mock(self, handler=None):
        return p.run_mock(self.approval, model=p.MODEL, transport=httpx.MockTransport(handler or self.response))

    def test_first_mock_claim_and_safe_usage_are_durable(self):
        plan = p.preflight(self.approval, model=p.MODEL)
        self.assertEqual((plan['estimated_input'], plan['reservation']), (1217, '153.7'))
        self.assertFalse(self.state.exists())
        report = self.run_mock()
        self.assertEqual((report['status'], report['new_cost'], report['spent']), ('completed', '16.0', '16.0'))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(report['json_valid'] and report['schema_valid'] and report['value_correct'])
        self.assertFalse(report['retry_permitted'] or report['live_enabled'])
        from diagnostics.cycle_a import validate_a1_receipt
        with self.assertRaises(ValueError):
            validate_a1_receipt(report, os.environ['LLM_BASE_URL'])
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(f.stat().st_mode & 0o777 == 0o600 for f in self.state.iterdir()))
        self.assertEqual(p.recover()['spent'], '16.0')

    def test_repeat_denied_even_when_model_or_approval_or_output_changes(self):
        self.run_mock()
        self.data['quota_scope_sha256'] = p.digest('another quota')
        self.save()
        for model in (p.MODEL, 'other', None):
            with self.assertRaises(ValueError):
                p.run_mock(self.approval, model=model, transport=httpx.MockTransport(self.response))
        self.assertEqual(len(self.calls), 1)

    def test_missing_budget_url_key_model_and_wrong_parameters_never_claim(self):
        for key in ('BUDGET', 'LLM_BASE_URL', 'LLM_API_KEY'):
            with self.subTest(key=key), patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != key}, clear=True):
                with self.assertRaises(ValueError): self.run_mock()
                self.assertFalse(self.state.exists())
        for model in (None, 'qwen3.8-27b-noreason', 'qwen3.6-35b-a3b'):
            with self.assertRaises(ValueError):
                p.preflight(self.approval, model=model)
        for budget in ('0', '199', '201', 'bad', ''):
            with patch.dict(os.environ, {'BUDGET': budget}), self.assertRaises(ValueError): self.run_mock()
        self.assertFalse(self.state.exists())

    def test_unconfirmed_proxy_model_cap_or_history_is_not_a_default(self):
        for key in ('proxy_confirmed', 'model_confirmed', 'output_cap_confirmed', 'history_complete'):
            for value in (False, 'true', 1, None):
                with self.subTest(key=key, value=value):
                    self.data[key] = value; self.save()
                    with self.assertRaises(ValueError): self.run_mock()
                    self.assertFalse(self.state.exists())
            self.data[key] = True
        self.data.pop('history'); self.save()
        with self.assertRaises(ValueError): self.run_mock()
        self.assertEqual(self.calls, [])

    def test_stale_fingerprints_changed_endpoint_and_extra_fields_fail(self):
        original = dict(self.data)
        for key, value in (('contract_sha256', '0' * 64), ('endpoint_sha256', '0' * 64),
                           ('usage_contract', 'reasoning_included'), ('max_tokens', 64)):
            self.data = {**original, key: value}; self.save()
            with self.subTest(key=key), self.assertRaises(ValueError): self.run_mock()
        self.assertFalse(self.state.exists())

    def test_history_is_charged_and_insufficient_safe_reserve_denies(self):
        self.data['history'] = [{'input_tokens': 20, 'accounted_output_tokens': 8}]; self.save()
        self.assertEqual(p.preflight(self.approval, model=p.MODEL)['spent_before'], '10.0')
        self.assertEqual(self.run_mock()['spent'], '26.0')
        self.assertEqual(p.recover()['spent'], '26.0')
        self.state = self.root / 'exhausted'
        with patch.object(p, 'STATE_ROOT', self.state):
            # Exact public historical total would not fit a new cumulative cap of 200.
            self.data['history'] = [{'input_tokens': 35, 'accounted_output_tokens': 11},
                                    {'input_tokens': 53, 'accounted_output_tokens': 7},
                                    {'input_tokens': 181, 'accounted_output_tokens': 111}]; self.save()
            with self.assertRaises(ValueError): self.run_mock()
            self.assertFalse(self.state.exists())

    def test_input_reserve_limit_is_checked_before_claim(self):
        for estimate in (1501, 2000):
            with patch.object(p.LLMGateway, 'estimate_input', return_value=estimate), self.assertRaises(ValueError):
                self.run_mock()
        self.assertFalse(self.state.exists())

    def test_crash_before_claim_is_not_a_possible_send(self):
        with patch.object(p, 'write_once', side_effect=RuntimeError('pre-claim crash')):
            with self.assertRaises(RuntimeError): self.run_mock()
        self.assertEqual(self.calls, [])
        self.assertEqual(p.recover()['state'], 'unclaimed')
        self.assertEqual(self.run_mock()['status'], 'completed')

    def test_crash_after_claim_before_send_consumes_attempt(self):
        with patch.object(p.LLMGateway, 'complete', side_effect=RuntimeError('post-claim crash')):
            with self.assertRaises(RuntimeError): self.run_mock()
        self.assertTrue((self.state / p.INTENT).is_file())
        self.assertFalse((self.state / p.RESULT).exists())
        self.assertEqual(p.recover()['state'], 'uncertain')
        with self.assertRaises(ValueError): self.run_mock()
        self.assertEqual(self.calls, [])

    def test_timeout_and_error_after_possible_send_hold_reserve_and_never_repeat(self):
        for failure in ('timeout', 401, 403, 429, 500):
            with self.subTest(failure=failure), patch.object(p, 'STATE_ROOT', self.root / str(failure)):
                calls = []
                def handler(request):
                    calls.append(1)
                    if failure == 'timeout': raise httpx.ReadTimeout('synthetic')
                    return httpx.Response(failure)
                result = self.run_mock(handler)
                self.assertEqual((result['status'], result['reserved'], result['new_cost']), ('uncertain', '153.7', None))
                self.assertTrue(result['frozen'])
                with self.assertRaises(ValueError): self.run_mock(handler)
                self.assertEqual(len(calls), 1)
                self.assertEqual(p.recover()['available_for_new_requests'], '0.0')

    def test_missing_unknown_reasoning_usage_and_overrun_stop(self):
        for usage in (None, {'prompt_tokens': 100, 'completion_tokens': 6, 'reasoning_tokens': 3},
                      {'prompt_tokens': 100, 'completion_tokens': 33}):
            with self.subTest(usage=usage), patch.object(p, 'STATE_ROOT', self.root / str(len(str(usage)))):
                def handler(request):
                    body = self.response(request)
                    raw = json.loads(body.content); raw['usage'] = usage
                    return httpx.Response(200, json=raw)
                result = self.run_mock(handler)
                self.assertTrue(result['frozen'])
                if usage and usage['completion_tokens'] == 33:
                    self.assertEqual(result['new_cost'], '43.0')
                else:self.assertIsNone(result['new_cost'])
                with self.assertRaises(ValueError): self.run_mock(handler)

    def test_inner_json_schema_value_have_separate_results_and_no_retry(self):
        for i, text in enumerate(('{bad', '{"value":4}', '{"value":"5"}')):
            with patch.object(p, 'STATE_ROOT', self.root / f'format{i}'):
                def handler(request):
                    body = self.response(request); raw = json.loads(body.content)
                    raw['choices'][0]['message']['content'] = text
                    return httpx.Response(200, json=raw)
                result = self.run_mock(handler)
                self.assertEqual(tuple(result[k] for k in ('json_valid', 'schema_valid', 'value_correct')),
                                 [(False, False, False), (True, False, False), (True, True, False)][i])
                self.assertEqual((result['status'], result['new_cost']), ('rejected', '16.0'))
                with self.assertRaises(ValueError): self.run_mock(handler)

    def test_fsync_failures_consume_name_without_mock_send(self):
        for index in (2, 3):
            with patch.object(p, 'STATE_ROOT', self.root / f'fsync{index}'):
                original = os.fsync; count = 0
                def sync(fd):
                    nonlocal count
                    count += 1
                    if count == index: raise OSError('synthetic fsync')
                    return original(fd)
                with patch.object(os, 'fsync', side_effect=sync), self.assertRaises(OSError): self.run_mock()
                self.assertEqual(self.calls, [])
                with self.assertRaises(ValueError): self.run_mock()

    def test_corrupt_or_special_state_never_authorizes_resend_or_leaks(self):
        self.state.mkdir(mode=0o700)
        for text in ('', '{', '[]', '{"api_key":"PRIVATE_STATE_CANARY"}'):
            (self.state / p.INTENT).write_text(text); (self.state / p.INTENT).chmod(0o600)
            (self.state / p.RESULT).write_text(text); (self.state / p.RESULT).chmod(0o600)
            with self.assertRaises(ValueError): self.run_mock()
            self.assertNotIn('PRIVATE_STATE_CANARY', json.dumps(p.recover()))

    def test_no_keys_headers_prompts_or_raw_answers_are_persisted(self):
        self.run_mock()
        text = ''.join(path.read_text() for path in self.state.iterdir())
        for canary in ('SYNTHETIC_KEY_CANARY_934', 'Authorization', 'Bearer', p.SYSTEM, p.PAYLOAD['instruction'], '{"value":"4"}'):
            self.assertNotIn(canary, text)

    def test_changed_prompt_requires_new_confirmation_before_claim(self):
        with patch.dict(p.PAYLOAD, {'instruction': 'Compute (8 - 3)'}), self.assertRaises(ValueError):
            self.run_mock()
        self.assertFalse(self.state.exists())

    def test_private_parent_and_state_symlink_require_no_send(self):
        self.approval.chmod(0o644)
        with self.assertRaises(ValueError): self.run_mock()
        self.approval.chmod(0o600)
        parent = self.root / 'public-parent'; parent.mkdir(mode=0o755)
        with patch.object(p, 'STATE_ROOT', parent / 'state'), self.assertRaises(ValueError):
            self.run_mock()
        self.state.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): self.run_mock()
        self.assertEqual(self.calls, [])

    def test_live_transport_and_cli_are_sealed_without_claim(self):
        for transport in (None, object()):
            with self.assertRaises(ValueError): p.run_mock(self.approval, model=p.MODEL, transport=transport)
        with redirect_stdout(io.StringIO()), patch('httpx.AsyncHTTPTransport', side_effect=AssertionError('real transport')):
            self.assertEqual(fallback_pilot.main(['--qwen-direct', '--live']), 2)
            self.assertEqual(fallback_pilot.main(['--qwen-direct', '--check', '--approval', str(self.approval), '--model', p.MODEL]), 0)
        self.assertFalse(self.state.exists())

    def child(self, state, mode):
        script = '''import json,os,socket,sys
from pathlib import Path
import httpx
from diagnostics import qwen_profile as p
p.STATE_ROOT=Path(sys.argv[1])
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('no HTTP'))
def handler(request):
    with open(sys.argv[3], 'ab', buffering=0) as f:f.write(b'x')
    if sys.argv[4]=='crash':os._exit(41)
    return httpx.Response(200,json={'usage':{'prompt_tokens':100,'completion_tokens':6},'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"value":"4"}'}}]})
try:
    if sys.argv[4]=='recover':
        print(json.dumps(p.recover()))
    p.run_mock(Path(sys.argv[2]),model=p.MODEL,transport=httpx.MockTransport(handler))
except ValueError:print('BLOCKED')
'''
        return subprocess.Popen([sys.executable, '-B', '-c', script, str(state), str(self.approval), str(self.root / 'sends'), mode],
                                env=dict(os.environ), cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_new_process_detects_completed_attempt_without_second_mock_send(self):
        first = self.child(self.state, 'run'); out, err = first.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, err.decode())
        second = self.child(self.state, 'recover'); out, err = second.communicate(timeout=30)
        self.assertEqual(second.returncode, 0, err.decode())
        self.assertIn(b'consumed', out); self.assertIn(b'BLOCKED', out)
        self.assertEqual((self.root / 'sends').read_bytes(), b'x')

    def test_two_competing_processes_have_one_mock_sender(self):
        children = [self.child(self.state, 'run') for _ in range(2)]
        for child in children:
            out, err = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, err.decode())
        self.assertEqual((self.root / 'sends').read_bytes(), b'x')

    def test_process_death_after_possible_send_is_not_retried(self):
        child = self.child(self.state, 'crash'); out, err = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 41, err.decode())
        self.assertEqual(p.recover()['state'], 'uncertain')
        child = self.child(self.state, 'recover'); out, err = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, err.decode())
        self.assertIn(b'BLOCKED', out)
        self.assertEqual((self.root / 'sends').read_bytes(), b'x')


if __name__ == '__main__': unittest.main()
