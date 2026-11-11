"""One-time Search experiment: actual workflow, synthetic history and mock HTTP."""
import asyncio
from contextlib import redirect_stdout
from decimal import Decimal
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
from diagnostics import fallback_pilot as pilot
from diagnostics.cycle_a import diagnostic_fingerprint, validate_a1_receipt
from diagnostics.trace import DiagnosticGateway, Trace
from benchmark.evaluate import runtime_fingerprint
from llm_gateway import DEFAULT_MODEL, LLMError


def proof():
    return {'mode':'offline_suite','live':False,'passed':True,'suite_version':1,
            'runtime_sha256':runtime_fingerprint(),'diagnostic_sha256':diagnostic_fingerprint()}


class SearchPilotTests(unittest.TestCase):
    def setUp(self):
        from diagnostics.trace import fallback_controls
        self.enterContext(fallback_controls())
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root/'search-state'
        self.enterContext(patch.object(pilot,'SEARCH_STATE',self.state))
        self.enterContext(patch.dict(os.environ,{'BUDGET':'900'}))
        # Even fake-LIVE tests must fail closed if a mock is accidentally removed.
        self.enterContext(patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')))
        self.enterContext(patch.object(socket,'create_connection',side_effect=AssertionError('network forbidden')))

    def test_history_and_full_reservation_are_exact(self):
        p=pilot.search_preflight(proof())
        self.assertEqual(p['budget_before']['spent'],'155.9')
        self.assertEqual(p['budget_before']['available'],'744.1')
        self.assertEqual((p['estimated_input'],p['reservation']),(1823,'694.3'))
        self.assertEqual((p['max_tokens'],p['http_timeout'],p['max_new_requests']),(512,20,1))
        self.assertFalse(p['A2_permitted']); self.assertFalse(self.state.exists())

    def test_supported_task_uses_real_workflow_and_persists_budget_and_answer(self):
        report=pilot.run_search(proof(),workspace=self.root/'tasks')
        row=report['rows'][0]
        self.assertEqual((row['task_id'],row['actual'],row['grade']),('search_d15','Porto','correct'))
        self.assertEqual((row['transport_attempts'],row['independent_plan'],row['execution']),(1,'equivalent','success'))
        self.assertTrue(row['outer_json_valid']);self.assertTrue(row['plan_json_valid']);self.assertTrue(row['plan_schema_valid'])
        self.assertEqual(report['budget']['spent'],'185.9')
        self.assertEqual(report['budget']['reserved'],'0.0')
        self.assertEqual(report['historical_attempts'],3);self.assertEqual(report['total_attempts_upper_bound'],4)
        self.assertFalse(report['A2_permitted']);self.assertFalse(report['retry_permitted'])
        with self.assertRaises(ValueError):validate_a1_receipt(report,pilot.a1_resume.BASE_URL)
        answer=self.root/'tasks/search_d15/answer.txt'
        self.assertEqual(answer.read_text(),'Porto\n');self.assertEqual(answer.stat().st_mode&0o777,0o644)
        self.assertEqual(self.state.stat().st_mode&0o777,0o700)
        for path in self.state.iterdir():self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(pilot.recover_search()['result'],report)

    def test_other_tasks_models_or_stale_offline_receipt_never_claim(self):
        for options in ({'task_id':'math_d15'},{'task_id':'search_h15'},{'model':'qwen3.8-27b'}):
            with self.subTest(options=options),self.assertRaises(ValueError):pilot.run_search(proof(),**options)
        stale=proof();stale['runtime_sha256']={}
        with self.assertRaises(ValueError):pilot.run_search(stale)
        self.assertFalse(self.state.exists())

    def test_new_local_support_blocks_paid_pilot_without_claiming_slot(self):
        import harness
        from search_solver import solve_search
        with patch.dict(harness.SOLVERS,{'search':solve_search}),self.assertRaises(ValueError):
            pilot.run_search(proof())
        self.assertFalse(self.state.exists())

    def test_insufficient_reserve_or_excessive_context_blocks_before_intent(self):
        manager=pilot.search_history_manager();manager.reserve('other','other',0,100);manager.settle('other',0,100)
        with patch.object(pilot,'search_history_manager',return_value=manager),self.assertRaises(ValueError):
            pilot.run_search(proof())
        with patch.object(pilot.LLMGateway,'estimate_input',return_value=2001),self.assertRaises(ValueError):
            pilot.run_search(proof())
        with patch.object(pilot.LLMGateway,'estimate_input',return_value=1824),self.assertRaises(ValueError):
            pilot.run_search(proof())  # Fits 900, but exceeds the approved 694.3 reserve.
        self.assertFalse(self.state.exists())

    def test_changed_prompt_is_rejected_before_transport_or_reservation(self):
        original=pilot.run_tasks_diagnostic
        def changed(**kwargs):
            kwargs['search_forecast']={**kwargs['search_forecast'],'prompt_sha256':'0'*64}
            return original(**kwargs)
        with patch.object(pilot,'run_tasks_diagnostic',side_effect=changed):
            report=pilot.run_search(proof())
        self.assertEqual(report['rows'][0]['transport_attempts'],0)
        self.assertEqual(report['budget']['spent'],'155.9')
        self.assertEqual(report['budget']['reserved'],'0.0')
        with self.assertRaises(ValueError):pilot.run_search(proof())

    def test_offline_search_never_constructs_network_transport_or_reads_keys(self):
        get=os.environ.get
        def guarded(name,*args):
            if name in ('LLM_API_KEY','LLM_BASE_URL'):raise AssertionError('credential read')
            return get(name,*args)
        with patch.object(os.environ,'get',side_effect=guarded),patch('httpx.AsyncHTTPTransport',side_effect=AssertionError('network transport')):
            report=pilot.run_search(proof())
        self.assertEqual(report['rows'][0]['grade'],'correct')
        self.assertFalse(report['live'])

    def test_corrupt_state_never_enables_another_attempt(self):
        self.state.mkdir(mode=0o700)
        for intent,result in (('{}',None),('{',None),('{}','['),('[]','{}'),('{}','[]'),('{}','{}')):
            with self.subTest(intent=intent,result=result):
                (self.state/pilot.SEARCH_INTENT).write_text(intent)
                if result is not None:(self.state/pilot.SEARCH_RESULT).write_text(result)
                with patch.object(pilot,'run_tasks_diagnostic',side_effect=AssertionError('repeat')):
                    with self.assertRaises(ValueError):pilot.run_search(proof())
                    if result is None:self.assertEqual(pilot.recover_search()['state'],'uncertain')
                    else:
                        with self.assertRaises(ValueError):pilot.recover_search()

    def test_competing_processes_claim_only_one_mock_workflow(self):
        script='''import socket,sys
from pathlib import Path
from diagnostics import fallback_pilot as p
from diagnostics.cycle_a import diagnostic_fingerprint
from benchmark.evaluate import runtime_fingerprint
def forbidden(*args,**kwargs):raise AssertionError('network forbidden')
socket.socket.connect=forbidden
p.SEARCH_STATE=Path(sys.argv[1])
proof={'mode':'offline_suite','live':False,'passed':True,'suite_version':1,'runtime_sha256':runtime_fingerprint(),'diagnostic_sha256':diagnostic_fingerprint()}
from diagnostics.trace import fallback_controls
try:
    with fallback_controls():r=p.run_search(proof)
except (ValueError,FileExistsError):print('blocked')
else:
    assert r['rows'][0]['transport_attempts']==1
    print('winner')
'''
        env={k:v for k,v in os.environ.items() if k not in ('LLM_BASE_URL','LLM_API_KEY')}
        command=[sys.executable,'-B','-c',script,str(self.state)]
        processes=[subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        answers=[]
        for process in processes:
            out,err=process.communicate(timeout=40)
            self.assertEqual(process.returncode,0,err.decode());answers.append(out.decode().strip())
        self.assertEqual(sorted(answers),['blocked','winner'])

    def test_real_time_input_limit_is_checked_before_transport(self):
        trace=Trace(['search_d15']);manager=pilot.search_history_manager()
        gateway=DiagnosticGateway('http://synthetic.invalid/v1','fake',manager=manager,trace=trace,
            max_tokens=512,max_calls=1,input_limit=1,transport_factory=lambda:self.fail('transport constructed'))
        with self.assertRaises(LLMError):asyncio.run(gateway.complete('context',{},task_id='search_d15'))
        self.assertEqual(manager.snapshot()['spent'],Decimal('155.9'))
        self.assertEqual(gateway.attempts,0)

    def test_repeat_with_new_output_cannot_call_runner(self):
        pilot.run_search(proof())
        with patch.object(pilot,'run_tasks_diagnostic',side_effect=AssertionError('repeat')):
            with self.assertRaises(ValueError):pilot.run_search(proof(),workspace=self.root/'different')

    def test_timeout_uses_20_seconds_retains_unknown_cost_and_never_retries(self):
        calls=[];settings=[]
        async def timeout(request):
            calls.append(request);raise httpx.ReadTimeout('synthetic timeout')
        original=DiagnosticGateway
        def gateway(*args,**kwargs):
            settings.append(kwargs);return original(*args,**kwargs)
        config={'base_url':pilot.a1_resume.BASE_URL,'api_key':'SYNTHETIC_SEARCH_SECRET'}
        with patch('httpx.AsyncHTTPTransport',side_effect=lambda **kw:httpx.MockTransport(timeout)),patch('diagnostics.cycle_a.DiagnosticGateway',side_effect=gateway):
            report=pilot.run_search(proof(),config=config)
        self.assertEqual(len(calls),1)
        self.assertEqual((settings[0]['timeout'],settings[0]['max_tokens'],settings[0]['max_calls']),(20,512,1))
        self.assertTrue(settings[0]['strict_response'])
        self.assertTrue(report['budget']['frozen']);self.assertEqual(report['budget']['spent'],'155.9')
        self.assertEqual(report['budget']['reserved'],'694.3');self.assertIsNone(report['rows'][0]['confirmed_cost'])
        self.assertEqual(pilot.recover_search()['state'],'blocked')
        with self.assertRaises(ValueError):pilot.run_search(proof())

    def test_missing_usage_and_http_failure_do_not_become_free_or_retryable(self):
        for scenario in ('missing_usage','http_503'):
            with patch.object(pilot,'SEARCH_STATE',self.root/scenario):
                report=pilot.run_search(proof(),scenario=scenario)
                self.assertEqual(len(report['mock_requests']),1)
                self.assertTrue(report['budget']['frozen']);self.assertIsNone(report['rows'][0]['confirmed_cost'])
                with self.assertRaises(ValueError):pilot.run_search(proof())

    def test_interruption_survives_new_process_and_blocks_repeat(self):
        with patch.object(pilot,'run_tasks_diagnostic',side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):pilot.run_search(proof())
        self.assertTrue((self.state/pilot.SEARCH_INTENT).is_file())
        self.assertFalse((self.state/pilot.SEARCH_RESULT).exists())
        script="""import json,sys
from pathlib import Path
from diagnostics import fallback_pilot as p
p.SEARCH_STATE=Path(sys.argv[1])
print(json.dumps(p.recover_search()))
try: p.search_unclaimed()
except ValueError: print('REPEAT_BLOCKED')
else: raise AssertionError('repeat allowed')
"""
        result=subprocess.run([sys.executable,'-B','-c',script,str(self.state)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('"state": "uncertain"',result.stdout);self.assertIn('REPEAT_BLOCKED',result.stdout)

    def test_failed_intent_fsync_prevents_runner_and_consumes_existing_name(self):
        original=pilot.a1_resume.os.fsync;count=0
        def fail(fd):
            nonlocal count
            count+=1
            if count==2:raise OSError('synthetic fsync error')
            return original(fd)
        with patch.object(pilot.a1_resume.os,'fsync',side_effect=fail),patch.object(pilot,'run_tasks_diagnostic',side_effect=AssertionError('HTTP')):
            with self.assertRaises(OSError):pilot.run_search(proof())
        with self.assertRaises(ValueError):pilot.run_search(proof())

    def test_live_cli_shape_uses_only_mock_and_recovery_never_reads_credentials(self):
        key='SYNTHETIC_SEARCH_CANARY_87'
        offline=self.root/'offline.json';offline.write_text(json.dumps(proof()))
        output=self.root/'output'
        from diagnostics.cycle_a import reviewed_plans
        plan=reviewed_plans()['search_d15'];calls=[]
        async def reply(request):
            calls.append(request)
            return httpx.Response(200,json={'model':DEFAULT_MODEL,'usage':{'prompt_tokens':20,'completion_tokens':30},
                'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(plan)}}]})
        env={'BUDGET':'900','LLM_BASE_URL':pilot.a1_resume.BASE_URL,'LLM_API_KEY':key}
        args=['--search','--live','--offline-report',str(offline),'--output',str(output)]
        with patch.dict(os.environ,env),patch('httpx.AsyncHTTPTransport',side_effect=lambda **kw:httpx.MockTransport(reply)),redirect_stdout(io.StringIO()):
            self.assertEqual(pilot.main(args),0)
            self.assertEqual(pilot.main([*args[:-1],str(self.root/'new-output')]),2)
        self.assertEqual(len(calls),1)
        self.assertEqual(output.stat().st_mode&0o777,0o700)
        for name in ('report.json','report.md'):
            path=output/name;self.assertEqual(path.stat().st_mode&0o777,0o600)
        text=''.join(p.read_text() for p in self.state.iterdir())+(output/'report.json').read_text()
        for value in (key,'Authorization','Bearer','PRIVATE_SOURCE_CANARY'):self.assertNotIn(value,text)
        get=os.environ.get
        def guarded(name,*args):
            if name in ('LLM_API_KEY','LLM_BASE_URL'):raise AssertionError('credential read')
            return get(name,*args)
        with patch.object(os.environ,'get',side_effect=guarded),patch.object(pilot,'run_tasks_diagnostic',side_effect=AssertionError('HTTP')),redirect_stdout(io.StringIO()):
            self.assertEqual(pilot.main(['--search','--recover']),0)

    def test_bad_live_budget_endpoint_or_model_never_claims(self):
        config={'base_url':pilot.a1_resume.BASE_URL,'api_key':'synthetic'}
        for env,conf in (({'BUDGET':'899'},config),({'BUDGET':'900'},{**config,'base_url':'https://other.invalid/v1'}),
                         ({'BUDGET':'900'},{**config,'model':'other'})):
            with patch.dict(os.environ,env),self.assertRaises(ValueError):pilot.run_search(proof(),config=conf)
        self.assertFalse(self.state.exists())

    def test_failed_configuration_is_identified_without_claim_or_network(self):
        offline=self.root/'offline.json';offline.write_text(json.dumps(proof()))
        output=self.root/'output';args=['--search','--live','--offline-report',str(offline),'--output',str(output)]
        capture=io.StringIO()
        with patch.dict(os.environ,{'BUDGET':'900','LLM_BASE_URL':'','LLM_API_KEY':''}),redirect_stdout(capture):
            self.assertEqual(pilot.main(args),2)
        self.assertEqual(json.loads(capture.getvalue())['stage'],'live_configuration')
        self.assertFalse(self.state.exists())
        self.assertEqual(json.loads((output/'report.json').read_text())['mode'],'incomplete')
        capture=io.StringIO()
        with redirect_stdout(capture):self.assertEqual(pilot.main(args),2)
        self.assertEqual(json.loads(capture.getvalue())['stage'],'output_directory')
        self.assertFalse(self.state.exists())

    def test_search_json_schema_and_meaning_failures_are_distinguished(self):
        from diagnostics import cycle_a
        plan=cycle_a.reviewed_plans()[pilot.SEARCH_TASK]
        factory=cycle_a.mock_factory
        cases=(('json',None,'truncated_json',False,None,'response_format'),
               ('schema',{**plan,'extra':'ignored?'},'valid',True,False,'response_format'),
               ('relations',{**plan,'relations':['employer','mentor']},'valid',True,True,'model_meaning'),
               ('answer',{**plan,'answer':'Lima'},'valid',True,True,'model_meaning'))
        for label,candidate,scenario,json_ok,schema_ok,category in cases:
            def changed(trace,scenario,plans,requests):
                return factory(trace,scenario,{pilot.SEARCH_TASK:candidate},requests)
            with self.subTest(label=label),patch.object(pilot,'SEARCH_STATE',self.root/label),patch.object(cycle_a,'mock_factory',side_effect=changed):
                report=pilot.run_search(proof(),scenario=scenario,workspace=self.root/(label+'-tasks'))
                row=report['rows'][0]
                self.assertTrue(row['outer_json_valid']);self.assertEqual(row['plan_json_valid'],json_ok)
                self.assertEqual(row['plan_schema_valid'],schema_ok);self.assertEqual(row['category'],category)
                self.assertEqual(row['grade'],'skipped');self.assertEqual(Decimal(row['confirmed_cost']),Decimal('30.0'))
                self.assertEqual(row['transport_attempts'],1)
                self.assertFalse((self.root/(label+'-tasks')/pilot.SEARCH_TASK/'answer.txt').exists())
                with self.assertRaises(ValueError):pilot.run_search(proof())


if __name__=='__main__':unittest.main()
