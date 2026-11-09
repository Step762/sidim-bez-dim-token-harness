"""One-shot resume safety. All transports are mocks; never real credentials."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal
import io
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from diagnostics import a1_resume as resume
from diagnostics.cycle_a import diagnostic_fingerprint, validate_a1_receipt
from benchmark.evaluate import runtime_fingerprint
from llm_gateway import DEFAULT_MODEL
from synthetic_history import patch_history


FIXTURE = Path(__file__).parent/"fixtures/a1_ack_synthetic.json"


def offline_proof():
    return {"mode":"offline_suite","live":False,"passed":True,"suite_version":1,
            "runtime_sha256":runtime_fingerprint(),"diagnostic_sha256":diagnostic_fingerprint()}


class A1ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a1-resume-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root/"state"
        self.source = self.root/"original.json"
        self.source.write_bytes(FIXTURE.read_bytes())
        self.before = self.source.read_bytes()
        self.addCleanup(patch.stopall)
        patch.object(resume,"STATE_ROOT",self.state).start()
        self.enterContext(patch_history())
        patch.dict(os.environ,{"BUDGET":"600"}).start()
        self.config = {"base_url":resume.BASE_URL,"api_key":"RESUME_SYNTHETIC_SECRET_8f2c"}
        self.calls = []

    def factory(self,*,content='{"value":"5"}',failure=None):
        async def respond(request):
            # A complete fsynced intent is visible BEFORE the mock receives I/O.
            intent=json.loads((self.state/resume.INTENT_NAME).read_text())
            self.assertEqual(intent["request_status"],"may_have_been_sent")
            self.assertEqual(intent["budget_before"]["spent"],"19.7")
            body=json.loads(request.content)
            self.calls.append(body)
            if isinstance(failure,int):return httpx.Response(failure,json={"error":{"message":"synthetic error"}})
            if failure=="timeout":raise httpx.ReadTimeout("synthetic timeout")
            raw={"model":DEFAULT_MODEL,"usage":{"prompt_tokens":40,"completion_tokens":7},
                 "choices":[{"finish_reason":"stop","message":{"role":"assistant","content":content}}]}
            if failure=="missing_usage":raw.pop("usage")
            if failure=="partial_usage":raw["usage"]={"prompt_tokens":40}
            if failure=="overrun":raw["usage"]["prompt_tokens"]=5000
            if failure=="reasoning":raw["usage"]["reasoning_tokens"]=3
            return httpx.Response(200,json=raw)
        return lambda:httpx.MockTransport(respond)

    def run_probe(self,**kwargs):
        factory=kwargs.pop("_transport_factory",None) or self.factory()
        return resume.run_resume(self.source,offline_proof(),self.config,capture_response=True,
                                 _transport_factory=factory,**kwargs)

    def test_restores_history_and_sends_only_math_once_with_cumulative_budget(self):
        self.assertEqual(resume.restore_history().snapshot()["spent"],Decimal('19.7'))
        report=self.run_probe()
        self.assertEqual(len(self.calls),1)
        body=self.calls[0]
        self.assertEqual((body['model'],body['max_tokens']),(DEFAULT_MODEL,64))
        self.assertEqual(json.loads(body['messages'][1]['content']),{'question':'What is 2 + 3?'})
        self.assertEqual(report['new_transport_attempts'],1)
        self.assertEqual(report['series_attempts_upper_bound'],2)
        self.assertEqual(report['budget']['limit'],600)
        self.assertEqual(report['budget']['spent'],'30.7')
        self.assertEqual(report['budget']['available'],'569.3')
        self.assertEqual(report['new_probe']['confirmed_cost'],'11.0')
        self.assertEqual(self.source.read_bytes(),self.before)
        self.assertFalse(report['historical_contract_observed'])
        self.assertFalse(report['contract_observed'])
        self.assertFalse(report['a1_limit_permitted'])
        with self.assertRaises(ValueError):validate_a1_receipt(report,resume.BASE_URL)

    def test_second_process_entry_or_source_alias_cannot_repeat_math(self):
        self.run_probe()
        alias=self.root/'renamed-original.json'
        alias.write_bytes(self.before)
        with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        with self.assertRaises(resume.ResumeBlocked):
            resume.run_resume(alias,offline_proof(),self.config,capture_response=True,_transport_factory=self.factory())
        self.assertEqual(len(self.calls),1)
        self.assertEqual(resume.state_status()[0],'completed')

    def test_ack_and_limit_are_forbidden_before_state_or_transport(self):
        for probe in ('a1_ack','a1_limit','arbitrary'):
            with self.assertRaises(resume.ResumeBlocked):self.run_probe(probe=probe)
        self.assertEqual(self.calls,[])
        self.assertFalse(self.state.exists())

    def test_invalid_receipt_flags_hashes_and_unknown_history_are_rejected(self):
        for field,value in (('live',False),('mode','offline_suite'),('model','other'),
                            ('request_attempts',2),('endpoint_digest','bad'),('runtime_sha256',{}),
                            ('diagnostic_sha256',{}),('budget',{'spent':'0'}),('probes',[])):
            with self.subTest(field=field):
                receipt=json.loads(self.before)
                receipt[field]=value
                self.source.write_text(json.dumps(receipt))
                with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        self.source.write_bytes(self.before)
        for bad in ('runtime','diagnostics','parameters'):
            with self.subTest(current=bad):
                if bad=='runtime': target='runtime_fingerprint';value={}
                elif bad=='diagnostics':target='LEGACY_DIAGNOSTICS';value={}
                else:target='parameters_fingerprint';value=lambda:'bad'
                with patch.object(resume,target,return_value=value) if bad=='runtime' else patch.object(resume,target,value):
                    with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        self.assertEqual(self.calls,[])

    def test_stale_offline_or_changed_endpoint_budget_capture_denies_transport(self):
        stale={**offline_proof(),'diagnostic_sha256':{}}
        with self.assertRaises(ValueError):resume.preflight(self.source,stale)
        self.config['base_url']='https://unapproved.invalid/v1'
        with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        self.config['base_url']=resume.BASE_URL
        for budget in ('0','599','601'):
            with patch.dict(os.environ,{'BUDGET':budget}):
                with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        with self.assertRaises(resume.ResumeBlocked):
            resume.run_resume(self.source,offline_proof(),self.config,capture_response=False,_transport_factory=self.factory())
        self.assertEqual(self.calls,[])
        self.assertFalse(self.state.exists())

    def test_synthetic_semantic_tampering_fails_even_with_matching_file_hash(self):
        original = json.loads(self.before)
        for field,value in (('live',False),('mode','offline_suite'),('model','other'),
                            ('request_attempts',2),('endpoint_digest','bad'),
                            ('runtime_sha256',{}),('diagnostic_sha256',{}),
                            ('budget',{'spent':'0'}),('probes',[])):
            receipt = {**original,field:value}
            self.source.write_text(json.dumps(receipt))
            sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
            with self.subTest(field=field),patch.object(resume,'SOURCE_SHA256',sha):
                with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        receipt = json.loads(self.before)
        receipt['probes'][0]['prompt_tokens'] += 1
        self.source.write_text(json.dumps(receipt))
        with patch.object(resume,'SOURCE_SHA256',hashlib.sha256(self.source.read_bytes()).hexdigest()):
            with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        self.assertEqual(self.calls,[])
        self.assertFalse(self.state.exists())

    def test_unknown_usage_and_api_errors_stop_and_block_every_retry(self):
        for failure in ('missing_usage','partial_usage','reasoning','timeout',401,429,503,'overrun'):
            with self.subTest(failure=failure),patch.object(resume,'STATE_ROOT',self.root/('state-'+str(failure))):
                self.state=resume.STATE_ROOT
                report=self.run_probe(_transport_factory=self.factory(failure=failure))
                self.assertTrue(report['safety_stop'])
                self.assertTrue(report['budget']['frozen'])
                self.assertFalse(report['A2_permitted'])
                count=len(self.calls)
                with self.assertRaises(resume.ResumeBlocked):self.run_probe()
                self.assertEqual(len(self.calls),count)
                recovered=resume.recover(self.source)
                self.assertTrue(recovered['budget']['frozen'])
                if failure!='overrun':
                    self.assertEqual(recovered['budget']['spent'],'19.7')
                    self.assertEqual(recovered['budget']['reserved'],'188.1')

    def test_json_schema_value_are_separate_without_any_retry(self):
        for index,(content,expected) in enumerate((('not JSON',(False,None,None)),
                ('{"value":5}',(True,False,None)),('{"value":"4"}',(True,True,False)))):
            with self.subTest(content=content),patch.object(resume,'STATE_ROOT',self.root/('reply-'+str(index))):
                self.state=resume.STATE_ROOT
                report=self.run_probe(_transport_factory=self.factory(content=content))
                layers=report['new_probe']['diagnostics']
                self.assertTrue(layers['http_api']['compatible'])
                self.assertEqual(tuple(layers[k][field] for k,field in (('json','valid'),('schema','valid'),('value','correct'))),expected)
                self.assertFalse(report['new_probe']['contract_observed'])
                with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        self.assertEqual(len(self.calls),3)

    def test_empty_or_torn_intent_blocks_retry_and_recovery_reserves_unknown_cost(self):
        self.state.mkdir(mode=0o700)
        (self.state/resume.INTENT_NAME).write_text('')
        with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        recovered=resume.recover(self.source)
        self.assertEqual(recovered['state'],'uncertain')
        self.assertEqual(recovered['series_attempts_upper_bound'],2)
        self.assertEqual(recovered['budget']['spent'],'19.7')
        self.assertEqual(recovered['budget']['reserved'],'188.1')
        self.assertTrue(recovered['budget']['frozen'])
        self.assertFalse(recovered['retry_permitted'])
        self.assertEqual(self.calls,[])

    def test_crash_after_successful_wire_before_result_is_never_retried(self):
        original=resume.write_once
        def crash(parent,name,value):
            if name==resume.RESULT_NAME:
                (self.state/name).write_text('{')
                raise KeyboardInterrupt('controlled crash before result commit')
            return original(parent,name,value)
        with patch.object(resume,'write_once',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.run_probe()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(resume.recover(self.source)['state'],'uncertain')
        with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        self.assertEqual(len(self.calls),1)

    def test_intent_fsync_failure_prevents_wire_and_consumes_attempt(self):
        original=resume.os.fsync
        def fail_file(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):raise OSError('controlled fsync failure')
            return original(fd)
        # Capture files are opened but not written before the intent is synced.
        with patch.object(resume.os,'fsync',side_effect=fail_file):
            with self.assertRaises(OSError):self.run_probe()
        self.assertEqual(self.calls,[])
        self.assertTrue((self.state/resume.INTENT_NAME).exists())
        with self.assertRaises(resume.ResumeBlocked):self.run_probe()

    def test_intent_directory_sync_failure_also_prevents_wire(self):
        original=resume.os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and (self.state/resume.INTENT_NAME).exists():
                raise OSError('controlled directory fsync failure')
            return original(fd)
        with patch.object(resume.os,'fsync',side_effect=fail_directory):
            with self.assertRaises(OSError):self.run_probe()
        self.assertEqual(self.calls,[])
        with self.assertRaises(resume.ResumeBlocked):self.run_probe()

    def test_durable_result_recovers_without_report_output_or_new_request(self):
        result=self.run_probe()
        # Simulate losing the external output after the durable private result.
        with patch('httpx.AsyncHTTPTransport',side_effect=AssertionError('no HTTP during recovery')):
            self.assertEqual(resume.recover(self.source),result)
        self.assertEqual(len(self.calls),1)
        for name in (resume.INTENT_NAME,resume.RESULT_NAME):
            self.assertEqual((self.state/name).stat().st_mode&0o777,0o600)
        self.assertEqual(self.state.stat().st_mode&0o777,0o700)

    def test_two_concurrent_claims_have_exactly_one_winner(self):
        barrier=threading.Barrier(2)
        def claim(_):
            with resume.state_directory(create=True) as parent:
                barrier.wait(timeout=5)
                try:
                    resume.write_once(parent,resume.INTENT_NAME,{'probe_id':'a1_math'})
                    return True
                except FileExistsError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(claim,range(2))),[False,True])
        self.assertEqual(self.calls,[])
        self.assertEqual(resume.state_status()[0],'uncertain')

    def test_process_exit_inside_transport_leaves_an_irrevocable_claim(self):
        script='''
import json,os,sys
from pathlib import Path
import httpx
from diagnostics import a1_resume as a
from diagnostics.cycle_a import diagnostic_fingerprint
from benchmark.evaluate import runtime_fingerprint
sys.path.insert(0,str(Path('tests').resolve()))
from synthetic_history import patch_history
synthetic_context=patch_history()
a.STATE_ROOT=Path(sys.argv[1])
async def respond(request):
    intent=json.loads((a.STATE_ROOT/a.INTENT_NAME).read_text())
    if intent['probe_id']!='a1_math':os._exit(94)
    os._exit(93)
proof={'mode':'offline_suite','live':False,'passed':True,'suite_version':1,'runtime_sha256':runtime_fingerprint(),'diagnostic_sha256':diagnostic_fingerprint()}
a.run_resume(Path(sys.argv[2]),proof,{'base_url':a.BASE_URL,'api_key':'fake-child-key'},capture_response=True,_transport_factory=lambda:httpx.MockTransport(respond))
'''
        env={k:v for k,v in os.environ.items() if k not in {'LLM_API_KEY','LLM_BASE_URL'}}
        result=subprocess.run([sys.executable,'-B','-c',script,str(self.state),str(self.source)],env=env,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        self.assertEqual(result.returncode,93,result.stderr.decode())
        self.assertEqual(resume.recover(self.source)['state'],'uncertain')
        with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        self.assertEqual(self.calls,[])

    def test_state_links_special_files_and_insecure_permissions_fail_closed(self):
        outside=self.root/'outside'
        outside.mkdir(mode=0o700)
        self.state.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):self.run_probe()
        self.assertEqual(list(outside.iterdir()),[])
        self.state.unlink()  # Only this test's own temporary link.
        self.state.mkdir(mode=0o755)
        with self.assertRaises(resume.ResumeBlocked):self.run_probe()
        self.state.chmod(0o700)
        (self.state/resume.INTENT_NAME).write_text('{}')
        os.mkfifo(self.state/resume.RESULT_NAME)
        self.assertEqual(resume.recover(self.source)['state'],'uncertain')
        self.assertEqual(self.calls,[])

    def test_key_and_authorization_never_appear_in_journal_output_or_capture(self):
        key=self.config['api_key']
        report=self.run_probe(_transport_factory=self.factory(content=key))
        capture=Path(report['new_probe']['response_capture']['path'])
        serialised=json.dumps(report)+''.join(p.read_text() for p in self.state.iterdir())+capture.read_text()
        self.assertNotIn(key,serialised)
        self.assertNotIn('Authorization',serialised)
        self.assertNotIn('Bearer',serialised)
        self.assertTrue(report['new_probe']['response_capture']['credential_redacted'])
        self.assertEqual(capture.stat().st_mode&0o777,0o600)

    def test_offline_cli_check_and_recovery_never_read_real_credentials(self):
        proof=self.root/'offline.json'
        proof.write_text(json.dumps(offline_proof()))
        original=os.environ.get
        def no_keys(key,*args):
            if key in {'LLM_API_KEY','LLM_BASE_URL'}:raise AssertionError('credential read')
            return original(key,*args)
        with patch.object(os.environ,'get',side_effect=no_keys),patch('httpx.AsyncHTTPTransport',side_effect=AssertionError('HTTP')),redirect_stdout(io.StringIO()):
            self.assertEqual(resume.main(['--resume-from',str(self.source),'--offline-report',str(proof)]),0)
            self.assertEqual(resume.main(['--resume-from',str(self.source),'--recover']),0)
        self.assertFalse(self.state.exists())

    def test_cli_existing_claim_blocks_before_credentials_even_with_new_output(self):
        self.run_probe()
        proof=self.root/'offline.json'
        proof.write_text(json.dumps(offline_proof()))
        original=os.environ.get
        def no_keys(key,*args):
            if key in {'LLM_API_KEY','LLM_BASE_URL'}:raise AssertionError('credential read after claim')
            return original(key,*args)
        with patch.object(os.environ,'get',side_effect=no_keys),redirect_stdout(io.StringIO()):
            self.assertEqual(resume.main(['--resume-from',str(self.source),'--offline-report',str(proof),'--live',
                 '--capture-a1-response','--output',str(self.root/'different-output')]),2)
        self.assertFalse((self.root/'different-output.json').exists())
        self.assertEqual(len(self.calls),1)

    def test_actual_cli_shape_uses_one_mock_request_and_new_output_cannot_bypass(self):
        proof=self.root/'offline.json'
        proof.write_text(json.dumps(offline_proof()))
        output=self.root/'math-only'
        args=['--resume-from',str(self.source),'--offline-report',str(proof),'--live',
              '--probe','a1_math','--capture-a1-response','--output',str(output)]
        with patch.dict(os.environ,{'LLM_BASE_URL':resume.BASE_URL,'LLM_API_KEY':self.config['api_key']}),patch(
            'httpx.AsyncHTTPTransport',return_value=self.factory()()
        ) as transport,redirect_stdout(io.StringIO()):
            self.assertEqual(resume.main(args),0)
            self.assertEqual(transport.call_count,1)
            self.assertEqual(resume.main(args[:-1]+[str(self.root/'second-output')]),2)
            self.assertEqual(transport.call_count,1)
        report=json.loads(output.with_suffix('.json').read_text())
        self.assertEqual(report['budget']['spent'],'30.7')
        self.assertEqual(report['new_transport_attempts'],1)
        self.assertTrue(report['new_probe']['diagnostics']['value']['correct'])
        self.assertEqual(self.source.read_bytes(),self.before)
        self.assertEqual(len(self.calls),1)
        self.assertNotIn(self.config['api_key'],output.with_suffix('.json').read_text())


if __name__=='__main__':unittest.main()
