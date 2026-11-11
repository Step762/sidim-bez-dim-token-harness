"""Semantic compiler contracts, adversarial manual replay and real workflow integration."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
from fractions import Fraction
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
from diagnostics.compiler_replay import cases,response,run_replay
from llm_gateway import DEFAULT_MODEL,LLMGateway,LLMError
from math_ir import Quantity
from semantic_compiler import Contract,CompilerRefusal,OfflineRoute


def envelope(content,*,usage=True):
    value={'model':DEFAULT_MODEL,'choices':[{'message':{'role':'assistant','content':content},'finish_reason':'stop'}]}
    if usage:value['usage']={'prompt_tokens':100,'completion_tokens':100,'total_tokens':200}
    return value


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def contract(self,case,variant='A'):
        path=self.root/variant/case.id;path.mkdir(parents=True)
        if case.documents:
            kb=path/'workspace/kb';kb.mkdir(parents=True)
            for name,text in case.documents:(kb/name).write_text(text)
        return Contract(case.kind,case.instruction,path,variant=variant)

    def case(self,name):return next(c for c in cases() if c.id==name)

    def test_all_manual_replay_expectations_are_met_for_both_hypotheses(self):
        result=run_replay()
        self.assertEqual(result['real_requests'],0)
        self.assertEqual(len(result['rows']),66)
        for row in result['rows']:
            with self.subTest(id=row['id'],variant=row['variant']):
                self.assertEqual(row['outcome']['status']=='accepted',row['should_accept'])
                if row['should_accept']:self.assertEqual(row['outcome']['answer'],row['oracle'])
        self.assertEqual(result['variants']['A']['accepted'],10)
        self.assertEqual(result['variants']['B']['accepted'],9)
        for summary in result['variants'].values():
            self.assertEqual(summary['false_acceptances'],0)
            self.assertEqual(summary['accepted_answer_precision'],1)
            self.assertEqual(summary['executor_correct'],summary['executed'])

    def test_schema_valid_wrong_math_is_not_semantically_valid_or_executed(self):
        for name in ('wrong_math_op','lost_number','hallucinated_number','wrong_order','unjustified_round','wrong_unit'):
            case=self.case(name);out=self.contract(case).assess_content(response(case,'A'))
            with self.subTest(case=name):
                self.assertTrue(out.schema_valid);self.assertFalse(out.semantic_valid)
                self.assertFalse(out.executed);self.assertIsNone(out.answer)

    def test_identity_relation_and_hallucinated_entity_are_not_admitted(self):
        for name in ('wrong_identity','wrong_relation','hallucinated_entity'):
            case=self.case(name);out=self.contract(case).assess_content(response(case,'A'))
            with self.subTest(case=name):
                self.assertTrue(out.schema_valid);self.assertFalse(out.semantic_valid);self.assertFalse(out.executed)

    def test_conflicts_ambiguity_and_unverified_language_stop_in_preflight(self):
        for name in ('conflicting_facts','ambiguous_inverse','same_name_fragments','ambiguous_math','unverified_language','prompt_injection_instruction'):
            with self.subTest(name=name),self.assertRaises(CompilerRefusal) as caught:self.contract(self.case(name))
            self.assertEqual(caught.exception.outcome.status,'semantic_unverified')

    def test_json_schema_and_semantic_checks_have_separate_statuses(self):
        for name,status in [('malformed_json','json_rejected'),('duplicate_key','json_rejected'),
                            ('unknown_operator','schema_rejected'),('boolean_version','schema_rejected'),
                            ('deep_ir','schema_rejected'),('large_ir','schema_rejected'),
                            ('prompt_injection_plan','schema_rejected'),('model_abstains','model_abstained')]:
            case=self.case(name)
            with self.subTest(name=name):self.assertEqual(self.contract(case).assess_content(response(case,'A')).status,status)

    def test_evidence_is_checked_without_replacing_semantic_verification(self):
        case=self.case('bad_evidence')
        out=self.contract(case,'B').assess_content(response(case,'B'))
        self.assertTrue(out.schema_valid);self.assertTrue(out.semantic_valid);self.assertFalse(out.evidence_valid)
        self.assertFalse(out.executed)
        bad=self.case('hallucinated_number')
        out=self.contract(bad,'B').assess_content(response(bad,'B'))
        self.assertFalse(out.semantic_valid)  # Valid whole-instruction citation cannot prove a fake number.

    def test_missing_duplicate_outside_and_stale_evidence_are_rejected(self):
        case=self.case('math_mean');contract=self.contract(case,'B');body=json.loads(response(case,'B'))
        for refs in [[],body['evidence']*2,
                     [{'source':'../../secret','sha256':'0'*64,'span':[0,1]}],
                     [{**body['evidence'][0],'span':[1,2]}]]:
            with self.subTest(refs=refs):
                out=contract.assess_content(json.dumps({**body,'evidence':refs}))
                self.assertNotEqual(out.status,'accepted');self.assertFalse(out.executed)

    def test_search_independent_verifier_rejects_source_mutation(self):
        case=self.case('search_path');contract=self.contract(case)
        path=contract.task_dir/'workspace/kb/three.md'
        path.write_text(path.read_text().replace('1962','1971'))
        out=contract.assess_content(response(case,'A'))
        self.assertEqual(out.status,'verification_rejected');self.assertFalse(out.verification_valid)
        self.assertTrue(out.executed);self.assertIsNone(out.answer)

    def test_result_verifier_does_not_accept_corrupted_executor_result(self):
        case=self.case('math_mean');contract=self.contract(case)
        with patch('semantic_compiler.math_execute',return_value=Quantity(Fraction(999),'one')):
            out=contract.assess_content(response(case,'A'))
        self.assertEqual(out.status,'verification_rejected')
        self.assertEqual(out.executor_answer,'999');self.assertIsNone(out.answer)

    def test_extra_fields_numbers_nonfinite_and_huge_literal_fail_before_execution(self):
        case=self.case('math_mean');contract=self.contract(case);body=json.loads(response(case,'A'))
        mutants=[{**body,'answer':'4'},{**body,'version':1.0},
                 {**body,'ir':{'expr':{'value':2,'unit':'one'},'unit':'one'}},
                 {**body,'ir':{'expr':{'value':'1e999999','unit':'one'},'unit':'one'}},
                 {**body,'ir':{'expr':{'value':'1/0','unit':'one'},'unit':'one'}},
                 {**body,'ir':{'expr':{'op':'import','args':[]},'unit':'one'}}]
        for body in mutants:
            out=contract.assess_content(json.dumps(body));self.assertFalse(out.schema_valid);self.assertFalse(out.executed)
        self.assertEqual(contract.assess_content('{"value":NaN}').status,'json_rejected')
        self.assertEqual(contract.assess_content('x'*24001).status,'schema_rejected')

    def test_prompt_payload_never_contains_reference_answer_or_expected_ir(self):
        case=self.case('math_mean');contract=self.contract(case)
        self.assertNotIn('expected',contract.payload);self.assertNotIn('answer',contract.payload)
        self.assertNotIn('ir',contract.payload);self.assertNotIn('reference_value',contract.payload)

    def test_search_domain_is_not_a_way_to_read_arbitrary_paths(self):
        case=self.case('search_path');contract=self.contract(case,'B');body=json.loads(response(case,'B'))
        body['evidence'][0]['source']='kb:/etc/passwd'
        self.assertEqual(contract.assess_content(json.dumps(body)).status,'evidence_rejected')

    def test_irrelevant_spelling_and_field_conflicts_do_not_poison_context(self):
        original=next(c for c in cases() if c.id=='search_path')
        case=replace(original,documents=(*original.documents,
            ('noise.md','# Remote\nBorn: 1900\n# REMOTE\nBorn: 1900\n# Far\nBorn: 1901\nBorn: 1910\n')))
        out=self.contract(case).assess_content(response(case,'A'))
        self.assertEqual(out.status,'accepted');self.assertEqual(out.answer,'1962')

    def test_invalid_unicode_and_unknown_domain_fail_preflight(self):
        for kind,text in [('math','\ud800'),('coding','Compute mean(2, 6)')]:
            with self.subTest(kind=kind),self.assertRaises(CompilerRefusal):Contract(kind,text)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.environment=patch.dict(os.environ,{'BUDGET':'10000'},clear=True);self.environment.start();self.addCleanup(self.environment.stop)

    def task(self,case):
        path=self.root/case.id;path.mkdir()
        (path/'task.json').write_text(json.dumps({'task_type':case.kind,'instruction':case.instruction,'reward':2}))
        if case.documents:
            kb=path/'workspace/kb';kb.mkdir(parents=True)
            for name,text in case.documents:(kb/name).write_text(text)
        return path

    def gateway(self,case_map,variant='A',*,usage=True):
        calls=[];manager=BudgetManager(10000)
        async def respond(request):
            payload=json.loads(json.loads(request.content)['messages'][1]['content'])
            case=case_map[payload['instruction']];calls.append(case.id)
            return httpx.Response(200,json=envelope(response(case,variant),usage=usage))
        return LLMGateway('http://compiler-mock.invalid/v1','synthetic-compiler-key',manager=manager,
                          max_tokens=1024,transport=httpx.MockTransport(respond)),calls

    def run_tasks(self,route,gateway):
        report={}
        with route.installed(),patch('socket.socket.connect',side_effect=AssertionError('external network forbidden')),redirect_stdout(io.StringIO()):
            counts=asyncio.run(harness.run_tasks(self.root,llm=gateway,manager=gateway.manager,budget=gateway.manager.limit,report=report))
        return counts,report

    def test_both_variants_use_actual_workflow_ledger_and_atomic_answer(self):
        for variant in ('A','B'):
            case=next(c for c in cases() if c.id=='math_mean')
            path=self.task(replace(case,id='mean_'+variant));gateway,calls=self.gateway({case.instruction:case},variant)
            counts,report=self.run_tasks(OfflineRoute(variant),gateway)
            self.assertGreaterEqual(counts['solved'],1);self.assertEqual(calls,['math_mean'])
            self.assertEqual((path/'answer.txt').read_text(),'4\n');self.assertEqual((path/'answer.txt').stat().st_mode&0o777,0o644)
            self.assertEqual(str(gateway.manager.snapshot()['spent']),'110.0')
            self.assertEqual(gateway.manager.snapshot()['reserved'],0)

    def test_local_exact_success_never_calls_compiler(self):
        path=self.root/'exact';path.mkdir();(path/'task.json').write_text(json.dumps({'task_type':'math','instruction':'Compute 7 + 3','reward':1}))
        gateway,calls=self.gateway({})
        counts,_=self.run_tasks(OfflineRoute(),gateway)
        self.assertEqual(counts['solved'],1);self.assertEqual(calls,[]);self.assertEqual((path/'answer.txt').read_text(),'10\n')

    def test_semantic_refusal_keeps_usage_and_does_not_repeat(self):
        case=next(c for c in cases() if c.id=='wrong_math_op');path=self.task(case)
        gateway,calls=self.gateway({case.instruction:case});route=OfflineRoute()
        counts,_=self.run_tasks(route,gateway)
        self.assertEqual(counts['skipped'],1);self.assertEqual(calls,[case.id]);self.assertFalse((path/'answer.txt').exists())
        self.assertEqual(str(gateway.manager.snapshot()['spent']),'110.0')
        with self.assertRaises(CompilerRefusal) as caught:
            asyncio.run(route.fallback(gateway,case.kind,case.instruction,path,task_id=str(path.absolute())))
        self.assertEqual(caught.exception.outcome.status,'attempt_already_consumed');self.assertEqual(len(calls),1)

    def test_unknown_usage_freezes_all_later_compiler_attempts(self):
        selected=[next(c for c in cases() if c.id==name) for name in ('math_mean','math_nested')]
        for case in selected:self.task(case)
        exact=self.root/'exact';exact.mkdir()
        (exact/'task.json').write_text(json.dumps({'task_type':'math','instruction':'Compute 7 + 3','reward':1}))
        gateway,calls=self.gateway({c.instruction:c for c in selected},usage=False)
        counts,_=self.run_tasks(OfflineRoute(),gateway)
        self.assertEqual(len(calls),1);self.assertEqual(counts['solved'],1)
        self.assertEqual((exact/'answer.txt').read_text(),'10\n')
        self.assertTrue(gateway.manager.snapshot()['frozen']);self.assertGreater(gateway.manager.snapshot()['reserved'],0)

    def test_live_transport_is_unconditionally_disabled_before_budget_or_http(self):
        case=next(c for c in cases() if c.id=='math_mean');manager=BudgetManager(10000)
        gateway=LLMGateway('https://official-proxy.invalid/v1','synthetic-not-a-real-key',manager=manager)
        with patch.object(gateway,'complete',side_effect=AssertionError('never call')):
            with self.assertRaises(CompilerRefusal) as caught:asyncio.run(OfflineRoute().fallback(gateway,'math',case.instruction))
        self.assertEqual(caught.exception.outcome.status,'live_disabled');self.assertEqual(manager.snapshot()['requests'],())

    def test_preflight_refusal_has_no_attempt_or_transport(self):
        case=next(c for c in cases() if c.id=='ambiguous_math');self.task(case)
        gateway,calls=self.gateway({case.instruction:case});route=OfflineRoute()
        counts,_=self.run_tasks(route,gateway)
        self.assertEqual(counts['skipped'],1);self.assertEqual(calls,[]);self.assertEqual(route.attempted,set())

    def test_insufficient_budget_stops_before_transport(self):
        case=next(c for c in cases() if c.id=='math_mean');self.task(case)
        gateway,calls=self.gateway({case.instruction:case})
        gateway.manager=BudgetManager(1)
        with patch.dict(os.environ,{'BUDGET':'1'}):counts,_=self.run_tasks(OfflineRoute(),gateway)
        self.assertEqual(calls,[]);self.assertEqual(counts['solved'],0)

    def test_search_path_uses_real_workflow_and_task_isolation(self):
        case=next(c for c in cases() if c.id=='search_path');path=self.task(case)
        other=replace(case,id='second',documents=tuple((n,t.replace('1962','1973')) for n,t in case.documents))
        other_path=self.task(other)
        # Same question, distinct task-local context. The second replay deliberately
        # returns the same plan: result must be recomputed from its own sources.
        gateway,calls=self.gateway({case.instruction:case})
        counts,_=self.run_tasks(OfflineRoute(),gateway)
        self.assertEqual(counts['solved'],2);self.assertEqual(len(calls),2)
        self.assertEqual((path/'answer.txt').read_text(),'1962\n');self.assertEqual((other_path/'answer.txt').read_text(),'1973\n')

    def test_cancellation_retains_reserve_and_consumes_attempt_without_retry(self):
        case=next(c for c in cases() if c.id=='math_mean');route=OfflineRoute();manager=BudgetManager(10000)
        calls=[]
        async def run():
            entered=asyncio.Event()
            async def transport(request):
                calls.append(1);entered.set();await asyncio.sleep(10)
                raise AssertionError('cancelled mock must not finish')
            gateway=LLMGateway('http://compiler-mock.invalid/v1','synthetic-cancel-key',manager=manager,
                               max_tokens=1024,transport=httpx.MockTransport(transport))
            pending=asyncio.create_task(route.fallback(gateway,'math',case.instruction,task_id='one'))
            await asyncio.wait_for(entered.wait(),2)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):await pending
            with self.assertRaises(CompilerRefusal) as caught:
                await route.fallback(gateway,'math',case.instruction,task_id='one')
            self.assertEqual(caught.exception.outcome.status,'attempt_already_consumed')
        asyncio.run(run())
        self.assertEqual(calls,[1]);self.assertTrue(manager.snapshot()['frozen'])
        self.assertGreater(manager.snapshot()['reserved'],0)

    def test_context_manager_restores_production_hooks(self):
        import planner
        original_prepare=planner.prepare;original_fallback=harness.solve_fallback
        with OfflineRoute().installed():
            self.assertIsNot(planner.prepare,original_prepare)
            self.assertIsNot(harness.solve_fallback,original_fallback)
        self.assertIs(planner.prepare,original_prepare);self.assertIs(harness.solve_fallback,original_fallback)


    def test_prompt_uses_one_consistent_versioned_abstention_envelope(self):
        case=next(c for c in cases() if c.id=='math_mean')
        for variant in ('A','B'):
            system=Contract('math',case.instruction,variant=variant).system
            self.assertNotIn('return {"status":"unsolvable"}',system)
            self.assertIn('{"version":1,"kind":"math","status":"unsolvable"}',system)
            self.assertIn('untrusted task data',system)


if __name__=='__main__':unittest.main()
