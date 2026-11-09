"""Frozen history and real workflow, with mock HTTP only."""

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from benchmark.evaluate import runtime_fingerprint
from diagnostics import a1_resume, fallback_pilot as pilot
from diagnostics.cycle_a import diagnostic_fingerprint, validate_a1_receipt
from llm_gateway import LLMGateway
from synthetic_history import patch_history


FIXTURES = Path(__file__).parent/"fixtures"


def proof():
    return {"mode":"offline_suite","live":False,"passed":True,"suite_version":1,
            "runtime_sha256":runtime_fingerprint(),"diagnostic_sha256":diagnostic_fingerprint()}


class FallbackPilotTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix="fallback-pilot-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.history=self.root/'a1-state'
        self.history.mkdir(mode=0o700)
        self.enterContext(patch_history())
        self.original=self.root/'a1.json'
        self.original.write_bytes((FIXTURES/'a1_ack_synthetic.json').read_bytes())
        self.math=self.root/'a1-math.json'
        self.math.write_bytes((FIXTURES/'a1_math_synthetic.json').read_bytes())
        (self.history/a1_resume.RESULT_NAME).write_bytes(self.math.read_bytes())
        (self.history/a1_resume.INTENT_NAME).write_bytes((FIXTURES/'a1_math_intent_synthetic.json').read_bytes())
        self.state=self.root/'pilot-state'
        self.addCleanup(patch.stopall)
        patch.object(a1_resume,'STATE_ROOT',self.history).start()
        patch.object(pilot,'STATE_ROOT',self.state).start()
        patch.dict(os.environ,{'BUDGET':'900'}).start()
        self.history_before={p:p.read_bytes() for p in [self.original,self.math,*self.history.iterdir()]}

    def run_pilot(self,**kwargs):
        return pilot.run_pilot(self.original,self.math,proof(),**kwargs)

    def test_correct_mock_runs_production_workflow_and_persists_answer(self):
        before=runtime_fingerprint()
        report=self.run_pilot(workspace=self.root/'tasks')
        row=report['rows'][0]
        self.assertEqual((row['actual'],row['grade'],row['answer_mode']),('76','correct',0o644))
        self.assertEqual((self.root/'tasks/math_d15/answer.txt').read_text().strip(),'76')
        task=json.loads((self.root/'tasks/math_d15/task.json').read_text())
        self.assertNotIn('expected',task)
        self.assertNotIn('check',task)
        self.assertEqual(report['budget']['limit'],900)
        self.assertEqual(report['budget']['spent'],'61.6')
        self.assertEqual(report['max_requests'],1)
        self.assertEqual(row['transport_attempts'],1)
        self.assertEqual(row['confirmed_cost'],'30')
        self.assertEqual(report['total_attempts_upper_bound'],3)
        self.assertEqual(report['mock_requests'][0]['max_tokens'],512)
        self.assertFalse(report['mock_requests'][0]['reference_fields_present'])
        self.assertTrue(row['outer_json_valid'])
        self.assertTrue(row['plan_json_valid'])
        self.assertTrue(row['plan_schema_valid'])
        self.assertEqual(row['independent_plan'],'equivalent')
        self.assertEqual(row['execution'],'success')
        self.assertFalse(report['A2_permitted'])
        self.assertFalse(report['A1_contract_observed'])
        self.assertEqual(runtime_fingerprint(),before)
        with self.assertRaises(ValueError):validate_a1_receipt(report,a1_resume.BASE_URL)
        for p,value in self.history_before.items():self.assertEqual(p.read_bytes(),value)

    def test_bad_json_equivalent_rejection_and_unsupported_plan_are_distinct(self):
        for scenario in ('truncated_json','equivalent_rejected','unsupported_plan'):
            with self.subTest(scenario=scenario),patch.object(pilot,'STATE_ROOT',self.root/scenario):
                report=self.run_pilot(scenario=scenario)
                row=report['rows'][0]
                self.assertIsNone(row['actual'])
                self.assertEqual(row['execution'],'not_reached')
                self.assertEqual(row['transport_attempts'],1)
                if scenario=='truncated_json':
                    self.assertFalse(row['plan_json_valid'])
                    self.assertIsNone(row['plan_schema_valid'])
                elif scenario=='equivalent_rejected':
                    self.assertTrue(row['plan_schema_valid'])
                    self.assertTrue(row['operations_supported'])
                    self.assertEqual(row['independent_plan'],'equivalent')
                    self.assertEqual(row['category'],'validator_rejection')
                    self.assertEqual(row['validation_reason'],'operation_order_or_evidence')
                else:
                    self.assertFalse(row['plan_schema_valid'])
                    self.assertFalse(row['operations_supported'])
                    self.assertEqual(row['category'],'response_format')

    def test_unassessed_quote_does_not_invent_model_or_validator_blame(self):
        from diagnostics.cycle_a import reviewed_plans
        plan=copy.deepcopy(reviewed_plans()['math_d15'])
        plan['steps'][0]['quote']='invented evidence'
        with patch('diagnostics.cycle_a.reviewed_plans',return_value={'math_d15':plan}):
            # Review oracle still checks grounding against the real instruction.
            row=self.run_pilot()['rows'][0]
        self.assertEqual(row['independent_plan'],'not_assessed')
        self.assertEqual(row['category'],'validation_unassessed')

    def test_http_timeout_and_missing_usage_preserve_history_and_block_repeats(self):
        for scenario in ('http_429','timeout','missing_usage'):
            with self.subTest(scenario=scenario),patch.object(pilot,'STATE_ROOT',self.root/scenario):
                report=self.run_pilot(scenario=scenario)
                self.assertEqual(len(report['mock_requests']),1)
                self.assertEqual(report['budget']['spent'],'31.6')
                self.assertEqual(report['budget']['reserved'],'700.0')
                self.assertTrue(report['budget']['frozen'])
                self.assertIsNone(report['rows'][0]['confirmed_cost'])
                with self.assertRaises(a1_resume.ResumeBlocked):self.run_pilot()

    def test_insufficient_reserve_stops_before_claim_or_transport(self):
        with patch.object(LLMGateway,'estimate_input',return_value=10000):
            with self.assertRaises(a1_resume.ResumeBlocked):self.run_pilot()
        self.assertFalse(self.state.exists())

    def test_repeat_source_alias_and_new_output_never_release_claim(self):
        self.run_pilot()
        alias=self.root/'alias.json';alias.write_bytes(self.math.read_bytes())
        with self.assertRaises(a1_resume.ResumeBlocked):
            pilot.run_pilot(self.original,alias,proof())
        file=self.root/'offline.json';file.write_text(json.dumps(proof()))
        original=os.environ.get
        def guarded(key,*args):
            if key in ('LLM_BASE_URL','LLM_API_KEY'):raise AssertionError('no credentials after claim')
            return original(key,*args)
        with patch.object(os.environ,'get',side_effect=guarded),redirect_stdout(io.StringIO()):
            self.assertEqual(pilot.main(['--a1-report',str(self.original),'--a1-resume-report',str(self.math),
                '--offline-report',str(file),'--live','--output',str(self.root/'another')]),2)
        self.assertFalse((self.root/'another').exists())

    def test_interruption_after_intent_is_irrevocable(self):
        with patch.object(pilot,'run_tasks_diagnostic',side_effect=KeyboardInterrupt('controlled interruption')):
            with self.assertRaises(KeyboardInterrupt):self.run_pilot()
        self.assertTrue((self.state/pilot.INTENT_NAME).exists())
        self.assertFalse((self.state/pilot.RESULT_NAME).exists())
        with self.assertRaises(a1_resume.ResumeBlocked):self.run_pilot()

    def test_competing_processes_use_only_one_mock_workflow(self):
        script='''
import sys
from pathlib import Path
from diagnostics import a1_resume as a, fallback_pilot as p
from diagnostics.cycle_a import diagnostic_fingerprint
from benchmark.evaluate import runtime_fingerprint
sys.path.insert(0,str(Path('tests').resolve()))
from synthetic_history import patch_history
synthetic_context=patch_history()
a.STATE_ROOT=Path(sys.argv[1]);p.STATE_ROOT=Path(sys.argv[2])
proof={'mode':'offline_suite','live':False,'passed':True,'suite_version':1,'runtime_sha256':runtime_fingerprint(),'diagnostic_sha256':diagnostic_fingerprint()}
try:
    r=p.run_pilot(Path(sys.argv[3]),Path(sys.argv[4]),proof)
except (a.ResumeBlocked,FileExistsError):print('blocked')
else:
    assert r['rows'][0]['transport_attempts']==1
    print('winner')
'''
        env={k:v for k,v in os.environ.items() if k not in ('LLM_BASE_URL','LLM_API_KEY')}
        commands=[sys.executable,'-B','-c',script,str(self.history),str(self.state),str(self.original),str(self.math)]
        processes=[subprocess.Popen(commands,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        answers=[]
        for process in processes:
            out,err=process.communicate(timeout=40)
            self.assertEqual(process.returncode,0,err.decode())
            answers.append(out.decode().strip())
        self.assertEqual(sorted(answers),['blocked','winner'])
        self.assertEqual(set(p.name for p in self.state.iterdir()),{pilot.INTENT_NAME,pilot.RESULT_NAME})

    def test_missing_key_wrong_endpoint_and_wrong_budget_never_claim(self):
        for config in ({'base_url':a1_resume.BASE_URL,'api_key':''},
                       {'base_url':'https://wrong.invalid/v1','api_key':'synthetic-key'}):
            with self.assertRaises(a1_resume.ResumeBlocked):self.run_pilot(config=config)
        with patch.dict(os.environ,{'BUDGET':'899'}):
            with self.assertRaises(a1_resume.ResumeBlocked):
                self.run_pilot(config={'base_url':a1_resume.BASE_URL,'api_key':'synthetic-key'})
        self.assertFalse(self.state.exists())

    def test_history_unknown_usage_hash_or_stale_offline_denies(self):
        saved=self.math.read_bytes()
        r=json.loads(saved);r['budget']['spent']='0';self.math.write_text(json.dumps(r))
        with self.assertRaises(a1_resume.ResumeBlocked):self.run_pilot()
        self.math.write_bytes(saved)
        (self.history/a1_resume.RESULT_NAME).write_text('{}')
        with self.assertRaises(a1_resume.ResumeBlocked):self.run_pilot()
        (self.history/a1_resume.RESULT_NAME).write_bytes(saved)
        bad={**proof(),'diagnostic_sha256':{}}
        with self.assertRaises(ValueError):pilot.preflight(self.original,self.math,bad)
        self.assertFalse(self.state.exists())

    def test_no_key_headers_prompts_or_raw_response_are_saved(self):
        key='cycle-a-synthetic-key'
        async def respond(request):
            return httpx.Response(200,json={'usage':{'prompt_tokens':100,'completion_tokens':20},
                'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':key}}]})
        with patch('diagnostics.cycle_a.mock_factory',return_value=lambda:httpx.MockTransport(respond)):
            report=self.run_pilot()
        serialised=json.dumps(report)+''.join(p.read_text() for p in self.state.iterdir())
        for forbidden in (key,'Authorization','Bearer','Start with 27 coins'):
            self.assertNotIn(forbidden,serialised)

    def test_synthetic_history_semantics_fail_after_both_copies_and_hash_match(self):
        original = self.math.read_bytes()
        variants = [('live',False),('model','wrong'),('parameters_sha256','bad'),
                    ('new_transport_attempts',2),('series_attempts_upper_bound',3),
                    ('budget',{'spent':'0'}),('safety_stop',True)]
        variants += [('new_probe',{'usage_state':'unknown','contract_observed':True})]
        for field,value in variants:
            record = json.loads(original)
            record[field] = value
            data = json.dumps(record).encode()
            self.math.write_bytes(data)
            (self.history/a1_resume.RESULT_NAME).write_bytes(data)
            with self.subTest(field=field),patch.object(pilot,'MATH_RESULT_SHA',hashlib.sha256(data).hexdigest()):
                with self.assertRaises(a1_resume.ResumeBlocked):self.run_pilot()
        self.assertFalse(self.state.exists())

    def test_offline_cli_has_no_key_reads_or_real_transport(self):
        file=self.root/'offline.json';file.write_text(json.dumps(proof()))
        original=os.environ.get
        def guarded(key,*args):
            if key in ('LLM_BASE_URL','LLM_API_KEY'):raise AssertionError('credential read')
            return original(key,*args)
        with patch.object(os.environ,'get',side_effect=guarded),patch('httpx.AsyncHTTPTransport',side_effect=AssertionError('HTTP')),redirect_stdout(io.StringIO()):
            self.assertEqual(pilot.main(['--a1-report',str(self.original),'--a1-resume-report',str(self.math),'--offline-report',str(file)]),0)
        self.assertFalse(self.state.exists())

    def test_model_or_reasoning_mismatch_stops_before_plan_validation(self):
        from diagnostics.cycle_a import reviewed_plans
        for kind in ('model','reasoning'):
            async def respond(request):
                body=json.loads(request.content)
                message={'role':'assistant','content':json.dumps(reviewed_plans()['math_d15'])}
                if kind=='reasoning':message['reasoning_content']='controlled unexpected reasoning'
                return httpx.Response(200,json={'model':'unexpected-model' if kind=='model' else body['model'],
                    'usage':{'prompt_tokens':100,'completion_tokens':20},
                    'choices':[{'finish_reason':'stop','message':message}]})
            with self.subTest(kind=kind),patch.object(pilot,'STATE_ROOT',self.root/kind),patch(
                'diagnostics.cycle_a.mock_factory',return_value=lambda:httpx.MockTransport(respond)):
                report=self.run_pilot()
                row=report['rows'][0]
                self.assertTrue(report['safety_stop'])
                self.assertTrue(row['response_contract_rejected'])
                self.assertIsNone(row['plan_json_valid'])
                self.assertEqual(row['execution'],'not_reached')
                self.assertIsNone(row['actual'])

    def test_live_cli_shape_uses_mock_transport_and_private_output(self):
        from diagnostics.cycle_a import reviewed_plans
        from llm_fallback import prepare
        from diagnostics.selection import load_selection
        calls=[]
        async def respond(request):
            body=json.loads(request.content);calls.append(body)
            prepared=prepare('math',load_selection()['math_d15']['task']['instruction'])
            self.assertEqual(body['messages'],LLMGateway._messages(prepared.system,prepared.payload))
            self.assertEqual(body['max_tokens'],512)
            return httpx.Response(200,json={'model':body['model'],
                'usage':{'prompt_tokens':100,'completion_tokens':20},
                'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(reviewed_plans()['math_d15'])}}]})
        file=self.root/'offline.json';file.write_text(json.dumps(proof()))
        out=self.root/'private-output'
        key='PILOT_SYNTHETIC_KEY_90ed'
        with patch.dict(os.environ,{'LLM_BASE_URL':a1_resume.BASE_URL,'LLM_API_KEY':key}),patch(
            'httpx.AsyncHTTPTransport',return_value=httpx.MockTransport(respond)
        ) as transport,redirect_stdout(io.StringIO()):
            self.assertEqual(pilot.main(['--a1-report',str(self.original),'--a1-resume-report',str(self.math),
                 '--offline-report',str(file),'--live','--output',str(out)]),0)
        self.assertEqual(len(calls),1)
        self.assertEqual(transport.call_count,1)
        self.assertEqual(out.stat().st_mode&0o777,0o700)
        self.assertEqual((out/'report.json').stat().st_mode&0o777,0o600)
        self.assertEqual((out/'tasks/math_d15/answer.txt').read_text().strip(),'76')
        self.assertNotIn(key,(out/'report.json').read_text())


if __name__=='__main__':unittest.main()
