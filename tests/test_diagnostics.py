"""Development diagnostics use mocks/loopback only; never NeuralDeep credentials."""

import asyncio
import copy
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import os
import shutil

import httpx
import harness
import llm_fallback
from budget_manager import BudgetManager
from diagnostics.cycle_a import (main, offline_suite, reviewed_plans, run_a1, run_tasks_diagnostic,
                                validate_a1_receipt, validate_offline_receipt, diagnostic_fingerprint,
                                synthetic_response_files)
from diagnostics.selection import CHOICES, experiment_plan, load_selection
from diagnostics.trace import DiagnosticGateway, Trace, assess_plan
from llm_gateway import AccountingPolicy, DEFAULT_MODEL, LLMError
from openjiuwen.core.workflow import Workflow
from test_llm import local_server


def target(report):
    return next(row for row in report["rows"] if row["task_id"]=="math_d15")


def artificial_a1_factory(ack, calls, *, failure=None):
    async def respond(request):
        from diagnostics.trace import _SCOPE
        probe_id,_ = _SCOPE.get()
        body = json.loads(request.content)
        calls.append((probe_id,body["model"],body["max_tokens"]))
        if failure=="timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if isinstance(failure,int):
            return httpx.Response(failure,json={"error":{"message":"synthetic provider error"}})
        limited = probe_id=="a1_limit"
        response = {"model":DEFAULT_MODEL,
                    "usage":{"prompt_tokens":35,"completion_tokens":32 if limited else 11},
                    "choices":[{"finish_reason":"length" if limited else "stop",
                                "message":{"role":"assistant","content":ack if probe_id=="a1_ack" else '{"values":[' if limited else '{"value":"5"}'}}]}
        if failure=="missing_usage":response.pop("usage")
        if failure=="partial_usage":response["usage"]={"prompt_tokens":35}
        if failure=="input_overrun":response["usage"]["prompt_tokens"]=5000
        if failure=="output_overrun":response["usage"]["completion_tokens"]=33
        if failure=="reasoning":response["choices"][0]["message"]["reasoning_content"]="synthetic unexpected reasoning"
        return httpx.Response(200,json=response)
    return lambda:httpx.MockTransport(respond)


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        from diagnostics.trace import fallback_controls
        self.enterContext(fallback_controls())

    def test_selection_is_development_only_and_covers_routing_and_evidence(self):
        selected=load_selection()
        self.assertEqual(len(selected),12)
        self.assertEqual({r["task"]["task_type"] for r in selected.values()},{"math","search","data_analysis"})
        self.assertTrue(all(r["reference"]["split"]=="development" for r in selected.values()))
        self.assertTrue(any(r["task"]["reward"]==0 for r in selected.values()))
        self.assertTrue(any(not r["reference"]["answerable"] for r in selected.values()))
        plan=experiment_plan()
        self.assertEqual(sum(r["natural_llm_candidate"] for r in plan["selection"]),2)
        self.assertEqual(plan["A1"]["planned_reservation"],"493.1")
        self.assertEqual(plan["A2"]["planned_reservation"],"1394.3")

    def test_preflight_never_reads_credentials_or_constructs_http_transport(self):
        original=os.environ.get
        def guarded(key,*args):
            if key in {"LLM_API_KEY","LLM_BASE_URL"}: raise AssertionError("credential read")
            return original(key,*args)
        with patch.object(os.environ,"get",side_effect=guarded),patch("httpx.AsyncHTTPTransport",side_effect=AssertionError("HTTP")):
            self.assertEqual(experiment_plan()["A1"]["max_requests"],3)

    def test_valid_mock_uses_real_workflow_ledger_and_immediate_publication(self):
        report=run_tasks_diagnostic()
        self.assertEqual(report["counts"],{"solved":10,"skipped":2,"failed":0})
        self.assertEqual(report["budget"]["spent"],"60.0")
        self.assertEqual(len(report["mock_requests"]),2)
        self.assertTrue(all(not request["reference_fields_present"] for request in report["mock_requests"]))
        self.assertTrue(all(row["answer_mode"]==0o644 for row in report["rows"] if row["grade"]=="correct"))
        self.assertEqual(next(r for r in report["rows"] if r["task_id"]=="search_d15")["actual"],"Porto")
        grouped=next(r for r in report["rows"] if r["task_id"]=="data_analysis_d12")
        self.assertEqual((grouped["grade"],grouped["request_sent"]),("correct",False))
        self.assertEqual(json.loads(grouped["actual"]),{"east":57,"west":23})
        total=next(r for r in report["rows"] if r["task_id"]=="data_analysis_d14")
        self.assertEqual((total["actual"],total["request_sent"]),("35",False))
        rounded=next(r for r in report["rows"] if r["task_id"]=="math_d09")
        self.assertEqual((rounded["actual"],rounded["grade"],rounded["request_sent"]),("7.24","correct",False))
        powered=next(r for r in report['rows'] if r['task_id']=='math_d08')
        self.assertEqual((powered['actual'],powered['grade'],powered['request_sent']),('29','correct',False))

    def test_events_order_reservation_wire_validation_execution_and_answer(self):
        report=run_tasks_diagnostic(task_ids=["math_d15"])
        events=[event for event in report["events"] if event["task_id"]=="math_d15"]
        def index(stage,status):
            return next(i for i,event in enumerate(events) if event["stage"]==stage and event["status"]==status)
        order=[index("local_attempt","started"),index("llm_eligibility","context_ready"),
               index("budget_reservation","reserved"),index("request","transport_started"),
               index("model_response","headers_received"),index("parsed_plan","parsed"),
               index("semantic_validation","accepted"),index("execution","started"),index("answer.txt","saved")]
        self.assertEqual(order,sorted(order))

    def test_wrong_meaning_is_not_reported_as_executor_failure(self):
        report=run_tasks_diagnostic(scenario="wrong_plan")
        row=target(report)
        self.assertEqual((row["category"],row["independent_plan"]),("model_meaning","incorrect"))
        self.assertEqual(row["validation"],"rejected")
        self.assertEqual(row["execution"],"not_reached")
        self.assertIsNone(row["actual"])

    def test_equivalent_plan_rejection_has_independent_algebraic_proof(self):
        reviewed=reviewed_plans()["math_d15"]
        equivalent=copy.deepcopy(reviewed)
        equivalent["steps"][0].update(op="add",value="-8")
        self.assertEqual(assess_plan(equivalent,reviewed),"equivalent")
        report=run_tasks_diagnostic(scenario="equivalent_rejected")
        self.assertEqual(target(report)["category"],"validator_rejection")
        self.assertEqual(target(report)["independent_plan"],"equivalent")
        case=load_selection()["math_d15"]
        with self.assertRaises(llm_fallback.InvalidPlan):
            llm_fallback.prepare("math",case["task"]["instruction"]).validate(equivalent)

    def test_invalid_plan_schema_or_unknown_evidence_is_not_called_equivalent(self):
        reviewed=reviewed_plans()["math_d15"]
        bad=copy.deepcopy(reviewed)
        bad["steps"][0]["value"]=8
        self.assertEqual(assess_plan(bad,reviewed),"format_invalid")
        bad=copy.deepcopy(reviewed)
        bad["steps"][0]["quote"]="invented evidence"
        self.assertEqual(assess_plan(bad,reviewed),"not_assessed")
        self.assertEqual(assess_plan({"arbitrary":True},None),"not_assessed")
        self.assertEqual(assess_plan(76,reviewed),"format_invalid")
        bad=copy.deepcopy(reviewed)
        bad["steps"][0]["quote"]="fabricated prefix: "+reviewed["steps"][0]["quote"]
        instruction=load_selection()["math_d15"]["task"]["instruction"]
        self.assertEqual(assess_plan(bad,reviewed,instruction),"not_assessed")
        data=reviewed_plans()["data_analysis_d14"]
        forged=copy.deepcopy(data)
        forged["filters"][0]["quote"]="invented "+data["filters"][0]["quote"]
        self.assertEqual(assess_plan(forged,data,load_selection()["data_analysis_d14"]["task"]["instruction"]),"not_assessed")
        extra=copy.deepcopy(data)
        extra["filters"][0]["extra_field"]="not in the requested schema"
        self.assertEqual(assess_plan(extra,data),"format_invalid")
        search=reviewed_plans()["search_d15"]
        malformed=copy.deepcopy(search)
        malformed["relations"]="mentor,employer"
        self.assertEqual(assess_plan(malformed,search),"format_invalid")
        rewrite=copy.deepcopy(reviewed)
        rewrite["steps"]=[{"op":"mul","value":"4","quote":"Multiply by 4"},
                          {"op":"sub","value":"32","quote":"Subtract 8 coins"}]
        self.assertEqual(assess_plan(rewrite,reviewed,instruction),"not_assessed")

    def test_supported_plan_with_injected_executor_fault_is_an_agent_failure(self):
        report=run_tasks_diagnostic(scenario="execution_error")
        row=target(report)
        self.assertEqual((row["category"],row["validation"],row["execution"]),("execution","accepted","error"))
        self.assertEqual(row["independent_plan"],"equivalent")
        self.assertTrue(report["fault_injected"])
        self.assertEqual(report["budget"]["spent"],"30.0")

    def test_unroutable_instructions_and_new_local_group_never_reach_model(self):
        report=run_tasks_diagnostic(scenario="unroutable")
        self.assertEqual(report["mock_requests"],[])
        grouped=next(row for row in report["rows"] if row["task_id"]=="data_analysis_d12")
        self.assertEqual((grouped["category"],grouped["grade"]),("success","correct"))
        powered=next(row for row in report['rows'] if row['task_id']=='math_d08')
        self.assertEqual((powered['category'],powered['grade'],powered['actual']),('success','correct','29'))
        self.assertEqual({row["category"] for row in report["rows"] if row not in (grouped,powered)},{"routing"})
        self.assertTrue(all(row["request_sent"] is False for row in report["rows"]))

    def test_all_http_error_codes_preserve_local_results_and_unknown_cost(self):
        for code in (400,401,429,500,503):
            with self.subTest(code=code):
                report=run_tasks_diagnostic(scenario=f"http_{code}")
                row=target(report)
                self.assertEqual((row["category"],row["detail"]),("api_or_budget",f"http_{code}"))
                self.assertEqual(row["usage_state"],"unknown")
                self.assertTrue(report["budget"]["frozen"])
                self.assertEqual(len(report["mock_requests"]),1)
                self.assertEqual(report["counts"]["solved"],1)

    def test_failure_freezes_shared_state_for_all_later_llm_candidates(self):
        report=run_tasks_diagnostic(scenario="http_503",task_ids=list(CHOICES))
        self.assertEqual(len(report["mock_requests"]),1)
        self.assertEqual(report["counts"]["solved"],8)
        self.assertEqual(report["budget"]["uncertain_calls"],1)
        rounded=next(r for r in report["rows"] if r["task_id"]=="math_d09")
        self.assertEqual((rounded["actual"],rounded["request_sent"]),("7.24",False))
        powered=next(r for r in report['rows'] if r['task_id']=='math_d08')
        self.assertEqual((powered['actual'],powered['request_sent']),('29',False))

    def test_timeout_is_distinguished_and_never_retried(self):
        report=run_tasks_diagnostic(scenario="timeout")
        self.assertEqual(target(report)["detail"],"timeout")
        self.assertEqual(len(report["mock_requests"]),1)
        self.assertGreater(float(report["budget"]["reserved"]),0)

    def test_missing_and_partial_usage_are_not_treated_as_free(self):
        for scenario,detail in (("missing_usage","usage_missing"),("partial_usage","usage_incomplete")):
            report=run_tasks_diagnostic(scenario=scenario)
            self.assertEqual(target(report)["detail"],detail)
            self.assertEqual(target(report)["usage_state"],"unknown")
            self.assertGreater(float(report["budget"]["reserved"]),0)
            self.assertEqual(report["budget"]["available"],"0.0")

    def test_zero_and_insufficient_budget_send_nothing(self):
        for scenario in ("zero_budget","exhausted"):
            report=run_tasks_diagnostic(scenario=scenario)
            self.assertEqual(report["mock_requests"],[])
            self.assertEqual(report["budget"]["spent"],"0.0")
            self.assertEqual(report["budget"]["reserved"],"0.0")
            self.assertEqual(target(report)["detail"],"budget_blocked")

    def test_truncated_inner_json_has_known_cost_outer_json_unknown_cost(self):
        inner=run_tasks_diagnostic(scenario="truncated_json")
        outer=run_tasks_diagnostic(scenario="outer_json_invalid")
        self.assertEqual(target(inner)["detail"],"plan_json_invalid")
        self.assertEqual(target(inner)["usage_state"],"confirmed")
        self.assertEqual(target(outer)["detail"],"envelope_json_invalid")
        self.assertEqual(target(outer)["usage_state"],"unknown")

    def test_partial_body_is_a_transport_failure_after_headers(self):
        report=run_tasks_diagnostic(scenario="partial_body")
        self.assertEqual(target(report)["detail"],"partial_response")
        self.assertIs(target(report)["request_sent"],True)
        self.assertEqual(target(report)["usage_state"],"unknown")
        self.assertTrue(any(e["stage"]=="model_response" and e.get("body_complete") is False and e.get("bytes_received",0)>0 for e in report["events"]))

    def test_length_and_content_arrays_are_contract_failures_not_wrong_answers(self):
        for scenario,detail in (("finish_length","generation_truncated"),("content_array","unsupported_content_type")):
            report=run_tasks_diagnostic(scenario=scenario)
            self.assertEqual(target(report)["detail"],detail)
            self.assertEqual(target(report)["usage_state"],"confirmed")
            self.assertIsNone(target(report)["actual"])

    def test_save_and_format_faults_are_separate_from_model_meaning(self):
        for scenario,detail in (("save_error","save_failed"),("format_error","format_or_written_value")):
            report=run_tasks_diagnostic(scenario=scenario)
            self.assertEqual((target(report)["category"],target(report)["detail"]),("answer_publication",detail))
            self.assertEqual(target(report)["independent_plan"],"equivalent")
            self.assertEqual(report["counts"]["solved"],1 if scenario=="save_error" else 2)

    def test_disable_trace_preserves_outcomes_and_budget(self):
        enabled=run_tasks_diagnostic(task_ids=["math_d01","math_d15"])
        disabled=run_tasks_diagnostic(task_ids=["math_d01","math_d15"],trace_enabled=False)
        self.assertEqual(disabled["events"],[])
        self.assertEqual(enabled["counts"],disabled["counts"])
        self.assertEqual(enabled["budget"],disabled["budget"])
        self.assertEqual(enabled["rows"],disabled["rows"])

    def test_hooks_are_restored_after_exception(self):
        original=(Workflow.invoke,llm_fallback.prepare,harness.save_answer)
        trace=Trace(["math_d15"])
        with self.assertRaises(RuntimeError),trace.observe(BudgetManager(100)):
            raise RuntimeError("controlled observer interruption")
        self.assertEqual((Workflow.invoke,llm_fallback.prepare,harness.save_answer),original)

    def test_secret_headers_bodies_and_private_sources_are_not_serialised(self):
        key="SYNTHETIC_SECRET_CANARY_0ba951"
        trace=Trace(["probe"],secret=key)
        manager=BudgetManager(1000)
        async def respond(request):
            return httpx.Response(200,json={"model":DEFAULT_MODEL,"usage":{"prompt_tokens":10,"completion_tokens":5},
                "choices":[{"finish_reason":"stop","message":{"role":"assistant","content":key}}],
                "unknown_field":key})
        gateway=DiagnosticGateway("http://local.invalid/v1",key,manager=manager,trace=trace,max_calls=1,
                                  max_tokens=64,transport_factory=lambda:httpx.MockTransport(respond))
        with patch.dict(os.environ,{"BUDGET":"1000"}),trace.observe(manager):
            with self.assertRaises(LLMError):
                asyncio.run(gateway.complete("PRIVATE_SOURCE_CANARY",{},task_id="probe"))
        serialised=json.dumps({"events":trace.events,"nodes":trace.nodes})
        for forbidden in (key,"Authorization","Bearer","PRIVATE_SOURCE_CANARY","unknown_field"):
            self.assertNotIn(forbidden,serialised)
        self.assertEqual(trace.nodes["probe"]["transport_attempts"],1)
        self.assertEqual(trace.nodes["probe"]["usage_state"],"confirmed")

    def test_request_cap_stops_before_a_second_transport(self):
        trace=Trace(["probe"])
        manager=BudgetManager(1000)
        calls=[]
        async def respond(request):
            calls.append(1)
            return httpx.Response(200,json={"model":DEFAULT_MODEL,"usage":{"prompt_tokens":10,"completion_tokens":5},
                "choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"{}"}}]})
        gateway=DiagnosticGateway("http://local.invalid/v1","synthetic-key",manager=manager,trace=trace,max_calls=1,
                                  max_tokens=64,transport_factory=lambda:httpx.MockTransport(respond))
        with patch.dict(os.environ,{"BUDGET":"1000"}),trace.observe(manager):
            asyncio.run(gateway.complete("JSON",{},task_id="probe"))
            with self.assertRaises(LLMError): asyncio.run(gateway.complete("JSON",{},task_id="probe"))
        self.assertEqual(len(calls),1)
        self.assertEqual(manager.snapshot()["spent"],6)

    def test_real_loopback_http_stream_preserves_gateway_semantics(self):
        plan=reviewed_plans()["math_d15"]
        with local_server(json.dumps(plan)) as server:
            trace=Trace(["probe"],secret=server["key"])
            manager=BudgetManager(1000)
            gateway=DiagnosticGateway(server["base"],server["key"],manager=manager,trace=trace,max_calls=1,
                                      max_tokens=512,transport_factory=lambda:httpx.AsyncHTTPTransport(retries=0))
            with patch.dict(os.environ,{"BUDGET":"1000"}),trace.observe(manager):
                result=asyncio.run(gateway.complete("JSON",{},task_id="probe"))
            self.assertEqual(json.loads(result.content),plan)
            self.assertEqual(len(server["requests"]),1)
            self.assertEqual(manager.snapshot()["spent"],30)
            self.assertTrue(trace.nodes["probe"]["body_complete"])
            self.assertTrue(trace.nodes["probe"]["sent"])
            self.assertNotIn(server["key"],json.dumps(trace.events))

    def test_reasoning_contracts_are_explicit_and_never_double_counted(self):
        for policy,total,expected in ((AccountingPolicy("included",True),120,30),
                                      (AccountingPolicy("separate",True),127,37)):
            trace=Trace(["probe"])
            manager=BudgetManager(1000)
            async def respond(request):
                return httpx.Response(200,json={"model":"qwen3.8-27b",
                    "usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":total,
                             "completion_tokens_details":{"reasoning_tokens":7}},
                    "choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"{}"}}]})
            gateway=DiagnosticGateway("http://local.invalid/v1","synthetic-key",model="qwen3.8-27b",
                accounting=policy,manager=manager,trace=trace,max_calls=1,max_tokens=64,
                transport_factory=lambda:httpx.MockTransport(respond))
            with patch.dict(os.environ,{"BUDGET":"1000"}),trace.observe(manager):
                asyncio.run(gateway.complete("JSON",{},task_id="probe"))
            self.assertEqual(manager.snapshot()["spent"],expected)
            self.assertEqual(trace.nodes["probe"]["completion_tokens"],20)
            self.assertEqual(trace.nodes["probe"]["reasoning_tokens"],7)

    def test_unverified_reasoning_model_stops_before_wire(self):
        trace=Trace(["probe"])
        manager=BudgetManager(1000)
        async def reject(request): raise AssertionError("reasoning contract was not confirmed")
        gateway=DiagnosticGateway("http://local.invalid/v1","synthetic-key",model="qwen3.8-27b",
            manager=manager,trace=trace,max_calls=1,max_tokens=64,transport_factory=lambda:httpx.MockTransport(reject))
        with patch.dict(os.environ,{"BUDGET":"1000"}),trace.observe(manager),self.assertRaises(LLMError):
            asyncio.run(gateway.complete("JSON",{},task_id="probe"))
        self.assertEqual(trace.nodes["probe"]["transport_attempts"],0)
        self.assertEqual(manager.snapshot()["reserved"],0)

    def test_observed_input_overrun_is_charged_and_blocks_further_calls(self):
        trace=Trace(["probe"])
        manager=BudgetManager(1000)
        calls=[]
        async def respond(request):
            calls.append(1)
            return httpx.Response(200,json={"model":DEFAULT_MODEL,"usage":{"prompt_tokens":5000,"completion_tokens":10},
                "choices":[{"finish_reason":"stop","message":{"role":"assistant","content":"{}"}}]})
        gateway=DiagnosticGateway("http://local.invalid/v1","synthetic-key",manager=manager,trace=trace,
            max_calls=2,max_tokens=64,transport_factory=lambda:httpx.MockTransport(respond))
        with patch.dict(os.environ,{"BUDGET":"1000"}),trace.observe(manager):
            asyncio.run(gateway.complete("JSON",{},task_id="probe"))
            with self.assertRaises(LLMError): asyncio.run(gateway.complete("JSON",{},task_id="probe"))
        self.assertEqual(len(calls),1)
        self.assertEqual(manager.snapshot()["spent"],510)
        self.assertTrue(manager.snapshot()["frozen"])

    def test_a1_mock_checks_format_usage_and_intentional_generation_limit(self):
        report=run_a1()
        self.assertFalse(report["live"])
        self.assertTrue(report["contract_observed"])
        self.assertEqual(report["request_attempts"],3)
        self.assertEqual(report["probes"][-1]["finish_reason"],"length")
        self.assertTrue(all(all(row["checks"].values()) for row in report["probes"]))

    def test_a1_distinguishes_reply_json_schema_and_value(self):
        for reply,expected in (("not JSON",(False,None,None)),
                               ('{"ok":"true"}',(True,False,None)),
                               ('{"ok":1}',(True,False,None)),
                               ('{"ok":true,"extra":1}',(True,False,None)),
                               ('{"ok":false}',(True,True,False)),
                               ('{"ok":true,"ok":false}',(False,None,None))):
            with self.subTest(reply=reply):
                calls=[]
                with patch("diagnostics.cycle_a.mock_factory",return_value=artificial_a1_factory(reply,calls)):
                    report=run_a1()
                probe=report["probes"][0]
                layers=probe["diagnostics"]
                self.assertEqual((layers["json"]["valid"],layers["schema"]["valid"],layers["value"]["correct"]),expected)
                self.assertTrue(layers["http_api"]["compatible"])
                self.assertTrue(layers["http_api"]["gateway_accepted"])
                self.assertEqual(layers["usage"]["confirmed_cost"],"14.5")
                self.assertGreaterEqual(probe["seconds"],0)
                self.assertFalse(probe["checks"]["protocol_reply"])
                self.assertEqual(report["stop_reason"],"protocol_reply_failure")
                self.assertEqual(len(calls),1)

    def test_a1_explicit_continuation_uses_three_distinct_probes_without_retry(self):
        calls=[]
        with patch("diagnostics.cycle_a.mock_factory",return_value=artificial_a1_factory('not JSON',calls)):
            report=run_a1(continue_on_protocol_error=True)
        self.assertEqual(calls,[("a1_ack",DEFAULT_MODEL,32),("a1_math",DEFAULT_MODEL,64),("a1_limit",DEFAULT_MODEL,32)])
        self.assertEqual(report["request_attempts"],3)
        self.assertEqual(report["budget"]["spent"],"64.5")
        self.assertFalse(report["contract_observed"])
        self.assertEqual(report["stop_reason"],"completed")
        self.assertTrue(report["probes"][1]["contract_observed"])
        limited=report["probes"][2]
        self.assertTrue(limited["contract_observed"])
        self.assertFalse(limited["diagnostics"]["json"]["valid"])
        self.assertIsNone(limited["diagnostics"]["schema"]["valid"])
        self.assertTrue(limited["diagnostics"]["generation"]["intentional_truncation"])
        self.assertFalse(limited["diagnostics"]["http_api"]["gateway_accepted"])

    def test_a1_continuation_never_overrides_api_usage_or_cost_stop(self):
        for failure in (400,401,429,500,503,"timeout","missing_usage","partial_usage","input_overrun","output_overrun","reasoning"):
            with self.subTest(failure=failure):
                calls=[]
                with patch("diagnostics.cycle_a.mock_factory",return_value=artificial_a1_factory('{"ok":true}',calls,failure=failure)):
                    report=run_a1(continue_on_protocol_error=True)
                self.assertEqual(len(calls),1)
                self.assertEqual(report["request_attempts"],1)
                self.assertFalse(report["contract_observed"])
                self.assertEqual(report["stop_reason"],"safety_or_api_failure")

    def test_a1_capture_is_private_bounded_synthetic_text_only(self):
        calls=[]
        with patch("diagnostics.cycle_a.mock_factory",return_value=artificial_a1_factory('Ж'*3000,calls)):
            report=run_a1(capture_response=True)
        probe=report["probes"][0]
        path=Path(probe["response_capture"]["path"])
        self.addCleanup(shutil.rmtree,path.parent)
        self.assertEqual(path.parent.parent,Path('/tmp'))
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(path.parent.stat().st_mode&0o777,0o700)
        value=json.loads(path.read_text())
        self.assertEqual(len(value["content"].encode('utf-8')),2048)
        self.assertTrue(value["truncated"])
        self.assertTrue(value["synthetic_only"])
        self.assertNotIn('Ж',json.dumps(report,ensure_ascii=False))
        self.assertEqual(len(calls),1)
        self.assertNotIn('system',value)
        self.assertNotIn('usage',value)

    def test_a1_capture_omits_echoed_key_or_authorization_text(self):
        for reply in ('cycle-a-synthetic-key','Authorization: Bearer SYNTHETIC_TOKEN'):
            calls=[]
            with patch("diagnostics.cycle_a.mock_factory",return_value=artificial_a1_factory(reply,calls)):
                report=run_a1(capture_response=True)
            capture=report["probes"][0]["response_capture"]
            path=Path(capture["path"])
            self.addCleanup(shutil.rmtree,path.parent)
            self.assertTrue(capture["credential_redacted"])
            self.assertNotIn(reply,path.read_text())
            self.assertNotIn(reply,json.dumps(report))
            self.assertEqual(len(calls),1)

    def test_a1_capture_is_opt_in_and_has_no_arbitrary_task_writer(self):
        calls=[]
        with patch("diagnostics.cycle_a.mock_factory",return_value=artificial_a1_factory('REPLY_CANARY',calls)):
            report=run_a1()
        self.assertFalse(report["response_capture_enabled"])
        self.assertNotIn('response_capture',report['probes'][0])
        self.assertNotIn('REPLY_CANARY',json.dumps(report))
        with synthetic_response_files(True) as capture:
            saved=capture('a1_ack','{}','synthetic-key')
            self.addCleanup(shutil.rmtree,Path(saved['path']).parent)
            with self.assertRaises(ValueError):capture('search_d15','PRIVATE_DOCUMENT','synthetic-key')

    def test_a1_capture_failure_happens_before_any_transport(self):
        calls=[]
        with patch("diagnostics.cycle_a.mock_factory",return_value=artificial_a1_factory('{}',calls)),patch(
            'diagnostics.cycle_a.tempfile.mkdtemp',side_effect=PermissionError('controlled unwritable capture')):
            with self.assertRaises(PermissionError):run_a1(capture_response=True)
        self.assertEqual(calls,[])

    def test_cli_a1_only_diagnostic_flags_are_rejected_for_other_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            for flag in ('--capture-a1-response','--continue-on-protocol-error'):
                with self.assertRaises(SystemExit),patch('sys.stderr',io.StringIO()):
                    main(['--mode','plan','--output',str(Path(temporary)/'bad'),flag])
            self.assertEqual(list(Path(temporary).iterdir()),[])

    def test_protocol_accepts_optional_model_total_but_stops_on_explicit_reasoning(self):
        def factory_for(reasoning=False):
            async def respond(request):
                from diagnostics.trace import _SCOPE
                key,_=_SCOPE.get()
                limited=key=="a1_limit"
                content=json.dumps({"ok":True} if key=="a1_ack" else {"value":"5"})
                message={"role":"assistant","content":content}
                if reasoning:message["reasoning_content"]="controlled unexpected reasoning"
                return httpx.Response(200,json={"usage":{"prompt_tokens":10,"completion_tokens":32 if limited else 5},
                    "choices":[{"finish_reason":"length" if limited else "stop","message":message}]})
            return lambda:httpx.MockTransport(respond)
        with patch("diagnostics.cycle_a.mock_factory",return_value=factory_for()):
            self.assertTrue(run_a1()["contract_observed"])
        with patch("diagnostics.cycle_a.mock_factory",return_value=factory_for(True)):
            report=run_a1()
            self.assertFalse(report["contract_observed"])
            self.assertEqual(report["request_attempts"],1)

    def test_cli_failed_a1_is_nonzero_and_never_overwrites_an_existing_report(self):
        receipt={"mode":"offline_suite","live":False,"passed":True,"suite_version":1,
                 "runtime_sha256":experiment_plan()["runtime_sha256"],"diagnostic_sha256":diagnostic_fingerprint()}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            proof=root/"offline.json"
            proof.write_text(json.dumps(receipt))
            with patch.dict(os.environ,{"BUDGET":"600","LLM_BASE_URL":"https://unused.invalid/v1","LLM_API_KEY":"synthetic-key"}),patch(
                "diagnostics.cycle_a.run_a1",return_value={"mode":"A1","live":True,"contract_observed":False,"budget":{}}
            ),patch("httpx.AsyncHTTPTransport",side_effect=AssertionError("no real request")),redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--mode","A1","--live","--offline-report",str(proof),"--output",str(root/"failed")]),1)
            before=(root/"failed.json").read_bytes()
            with self.assertRaises(FileExistsError):main(["--mode","plan","--output",str(root/"failed")])
            self.assertEqual((root/"failed.json").read_bytes(),before)
            original=os.environ.get
            def no_credentials(key,*args):
                if key in {"LLM_API_KEY","LLM_BASE_URL"}:raise AssertionError("zero budget read a key")
                return original(key,*args)
            with patch.dict(os.environ,{"BUDGET":"0"}),patch.object(os.environ,"get",side_effect=no_credentials):
                with self.assertRaises(ValueError):
                    main(["--mode","A1","--live","--offline-report",str(proof),"--output",str(root/"zero")])

    def test_offline_receipt_and_a1_live_gate_reject_mock_or_stale_evidence(self):
        offline={"mode":"offline_suite","live":False,"passed":True,"suite_version":1,"runtime_sha256":experiment_plan()["runtime_sha256"],
                 "diagnostic_sha256":diagnostic_fingerprint()}
        validate_offline_receipt(offline)
        for field,value in (("passed",False),("live",True),("runtime_sha256",{})):
            bad={**offline,field:value}
            with self.assertRaises(ValueError): validate_offline_receipt(bad)
        a1=run_a1()
        with self.assertRaises(ValueError): validate_a1_receipt(a1,"https://unused.invalid/v1")
        simulated={**a1,"live":True,"endpoint_digest":hashlib.sha256(b"https://unused.invalid/v1").hexdigest()}
        validate_a1_receipt(simulated,"https://unused.invalid/v1")
        for field,value in (("probes",[]),("request_attempts",2),("model","another-model")):
            with self.assertRaises(ValueError): validate_a1_receipt({**simulated,field:value},"https://unused.invalid/v1")

    def test_cli_requires_explicit_live_and_receipts_before_transport(self):
        with tempfile.TemporaryDirectory() as temporary,patch("httpx.AsyncHTTPTransport",side_effect=AssertionError("live transport")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--mode","plan","--output",str(Path(temporary)/"plan")]),0)
            with self.assertRaises(SystemExit),redirect_stdout(io.StringIO()),patch("sys.stderr",io.StringIO()):
                main(["--mode","A1","--output",str(Path(temporary)/"a1")])
            with self.assertRaises(SystemExit),redirect_stdout(io.StringIO()),patch("sys.stderr",io.StringIO()):
                main(["--mode","A1","--live","--output",str(Path(temporary)/"missing-receipt")])

    def test_unwritable_output_fails_before_any_credential_read(self):
        original=os.environ.get
        def guarded(key,*args):
            if key in {"LLM_API_KEY","LLM_BASE_URL"}: raise AssertionError("credential read")
            return original(key,*args)
        with tempfile.TemporaryDirectory() as temporary,patch.object(os.environ,"get",side_effect=guarded),patch("diagnostics.cycle_a.os.open",side_effect=PermissionError("controlled read-only output")):
            with self.assertRaises(PermissionError): main(["--mode","A1","--live","--output",str(Path(temporary)/"readonly")])


if __name__=="__main__":
    unittest.main()
