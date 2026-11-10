"""Cycle A: offline-first diagnostics and explicitly gated future live probes.

Default mode only prints a plan. Offline mode never reads real credentials.
Live mode requires explicit flags, a passing offline report, new output files,
and (for A2) a successful live A1 receipt. No automatic retries or phase chaining.
"""

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager, redirect_stdout, nullcontext
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
from unittest.mock import patch

import httpx
import harness
from benchmark.evaluate import DATASET, compare, runtime_fingerprint
from budget_manager import BudgetManager
from llm_gateway import DEFAULT_MODEL, LLMError, strict_json
from diagnostics.selection import (A1_PROBES, A1_BUDGET, A2_BUDGET, A2_MAX_TOKENS,
                                   A2_MAX_CALLS, experiment_plan, load_selection)
from diagnostics.trace import DiagnosticGateway, Trace, classify, _SCOPE, fallback_controls


SUITE_VERSION = 1
SCENARIOS = ("valid","wrong_plan","equivalent_rejected","execution_error","unroutable",
             "http_400","http_401","http_429","http_500","http_503","timeout",
             "missing_usage","partial_usage","exhausted","zero_budget","truncated_json",
             "outer_json_invalid","partial_body","finish_length","content_array",
             "save_error","format_error")


def diagnostic_fingerprint():
    root=Path(__file__).parent
    return {name:hashlib.sha256((root/name).read_bytes()).hexdigest()
            for name in ("selection.py","trace.py","cycle_a.py","a1_resume.py","fallback_pilot.py")}


def require_live_budget(cap):
    # Never expand an inherited BUDGET=0/smaller user limit for a live run.
    # A larger inherited budget is tightened to the reviewed phase cap.
    if BudgetManager.from_env().limit<cap:
        raise ValueError("inherited BUDGET is below the reviewed experiment cap; no live request permitted")


def reviewed_plans():
    fixtures = json.loads((DATASET/"mocks.json").read_text())
    return {item["task_id"]:item["plan"] for item in fixtures.values() if item["task_id"] in load_selection()}


def budget_summary(manager):
    value = manager.snapshot()
    return {key:str(value[key]) if isinstance(value[key],Decimal) else value[key]
            for key in ("limit","spent","reserved","available","frozen","uncertain_calls")}


class BrokenBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"choices":['
        raise httpx.ReadError("controlled partial response")


def mock_factory(trace, scenario, plans, requests):
    async def respond(request):
        key,_ = _SCOPE.get()
        body = json.loads(request.content)
        payload = json.loads(body["messages"][-1]["content"])
        # Only harmless audit metadata; never copy the request or its headers.
        forbidden = {"expected","check","reference","oracle","hidden_examples"}
        def has_reference(value):
            return (isinstance(value,dict) and (bool(set(value)&forbidden) or any(has_reference(v) for v in value.values()))) or (isinstance(value,list) and any(has_reference(v) for v in value))
        requests.append({"task_id":key,"model":body["model"],"max_tokens":body["max_tokens"],
                         "message_roles":[m["role"] for m in body["messages"]],
                         "reference_fields_present":has_reference(payload)})
        trace.nodes[key]["sent"] = True  # Mock receiver confirms receipt.
        trace.emit("request","mock_received",sent=True,confirmed=True)
        if scenario.startswith("http_"):
            return httpx.Response(int(scenario[5:]),json={"error":{"message":"controlled provider error"}})
        if scenario=="timeout":
            await asyncio.sleep(0.2)
        if scenario=="partial_body":
            return httpx.Response(200,stream=BrokenBody())
        if scenario=="outer_json_invalid":
            return httpx.Response(200,content=b'{"choices":')
        if key=="a1_ack": plan = {"ok":True}
        elif key=="a1_math": plan = {"value":"5"}
        elif key=="a1_limit": plan = {"values":[1,2]}
        else: plan = json.loads(json.dumps(plans.get(key,{"status":"unsolvable"})))
        if scenario=="wrong_plan" and "steps" in plan:
            plan["steps"][0]["op"] = "mul"
        if scenario=="equivalent_rejected" and "steps" in plan:
            plan["steps"][0].update(op="add",value="-8")
        if scenario=="unsupported_plan" and "steps" in plan:
            plan["steps"][0]["op"] = "pow"
        content = json.dumps(plan)
        if scenario=="truncated_json": content = '{"initial":'
        limited = scenario=="finish_length" or key=="a1_limit"
        usage = {"prompt_tokens":100,"completion_tokens":body["max_tokens"] if limited else 20}
        usage["total_tokens"] = usage["prompt_tokens"]+usage["completion_tokens"]
        response = {"id":"cycle-a-mock","model":body["model"],"object":"chat.completion",
                    "choices":[{"index":0,"message":{"role":"assistant","content":content},
                                "finish_reason":"length" if limited else "stop"}],
                    "usage":usage,"unknown_provider_field":{"ignored":True}}
        if scenario=="missing_usage": response.pop("usage")
        if scenario=="partial_usage": response["usage"]={"prompt_tokens":100}
        if scenario=="content_array": response["choices"][0]["message"]["content"]=[{"type":"text","text":content}]
        return httpx.Response(200,json=response)
    return lambda:httpx.MockTransport(respond)


def run_tasks_diagnostic(*, scenario="valid", task_ids=None, trace_enabled=True,
                         live_config=None, pilot_manager=None, pilot_workspace=None, search_manager=None,
                         search_forecast=None):
    if search_manager is not None:
        if (pilot_manager is not None or task_ids != ["search_d15"] or search_manager.limit != 900
            or search_manager.snapshot()["spent"] != Decimal("155.9") or search_manager.snapshot()["frozen"]):
            raise ValueError("search pilot only permits search_d15 with reviewed history and limit 900")
        if (not isinstance(search_forecast,dict)
            or type(search_forecast.get("estimated_input")) is not int
            or not 0 < search_forecast["estimated_input"] <= 1823
            or not isinstance(search_forecast.get("prompt_sha256"),str)):
            raise ValueError("search pilot requires its bounded preflight forecast")
        pilot_manager = search_manager
    if search_manager is None and pilot_manager is not None and (task_ids!=["math_d15"] or pilot_manager.limit!=900):
        raise ValueError("pilot only permits math_d15 with a shared limit of 900")
    if pilot_workspace is not None and pilot_manager is None:
        raise ValueError("persistent diagnostic workspace is pilot-only")
    if live_config is not None: require_live_budget(pilot_manager.limit if pilot_manager is not None else A2_BUDGET)
    selected = load_selection()
    if task_ids is None:
        task_ids = list(selected) if scenario=="valid" else (
            ["math_d08","search_d14","data_analysis_d12"] if scenario=="unroutable" else ["math_d01","math_d15"])
    if not task_ids or len(set(task_ids))!=len(task_ids) or any(key not in selected for key in task_ids):
        raise ValueError("only the fixed development selection is permitted")
    if live_config is not None and scenario!="valid":
        raise ValueError("fault injection is forbidden for live runs")
    limit = pilot_manager.limit if pilot_manager is not None else 0 if scenario=="zero_budget" else 1 if scenario=="exhausted" else A2_BUDGET
    manager = pilot_manager if pilot_manager is not None else BudgetManager(limit)
    plans = reviewed_plans()
    key = live_config["api_key"] if live_config else "cycle-a-synthetic-key"
    trace = Trace(task_ids,reviewed=plans,instructions={task_id:selected[task_id]["task"]["instruction"] for task_id in task_ids},
                  enabled=trace_enabled,secret=key)
    requests = []
    factory = (lambda:httpx.AsyncHTTPTransport(retries=0)) if live_config else mock_factory(trace,scenario,plans,requests)
    gateway = DiagnosticGateway(live_config["base_url"] if live_config else "http://cycle-a.invalid/v1",key,
        model=DEFAULT_MODEL,manager=manager,max_tokens=A2_MAX_TOKENS,timeout=20 if live_config else 0.03 if scenario=="timeout" else 2,
        trace=trace,transport_factory=factory,max_calls=1 if pilot_manager is not None else A2_MAX_CALLS,
        strict_response=pilot_manager is not None,
        input_limit=search_forecast["estimated_input"] if search_manager is not None else None,
        messages_sha256=search_forecast["prompt_sha256"] if search_manager is not None else None)
    started = time.perf_counter()
    with (nullcontext(pilot_workspace) if pilot_workspace is not None else tempfile.TemporaryDirectory(prefix="cycle-a-tasks-")) as temporary:
        tasks = Path(temporary)
        if pilot_workspace is not None: tasks.mkdir(mode=0o700)
        for task_id in task_ids:
            shutil.copytree(DATASET/"tasks"/task_id,tasks/task_id)
            with trace.scope(task_id,"local"): trace.emit("task","loaded")
        planner_report = {}
        fault = scenario if scenario in {"execution_error","save_error","format_error"} else None
        with patch.dict(os.environ,{"BUDGET":str(limit)}),redirect_stdout(io.StringIO()),trace.observe(manager,fault=fault,fault_task="math_d15"):
            counts = asyncio.run(harness.run_tasks(tasks,llm=gateway,manager=manager,budget=limit,report=planner_report))
        outcomes = {Path(path).name:value for path,value in planner_report["outcomes"].items()}
        costs = {Path(path).name:value for path,value in manager.snapshot()["tasks"].items()}
        rows = []
        for task_id in task_ids:
            reference = selected[task_id]["reference"]
            node = trace.nodes[task_id]
            node["state"] = outcomes[task_id]["outcome"]
            answer = tasks/task_id/"answer.txt"
            actual = answer.read_text().strip() if answer.exists() else None
            grade = compare(actual,reference)
            category,detail = classify(node,grade=grade,answerable=reference["answerable"],
                                       candidate_correct=compare(trace._candidate.get(task_id),reference)=="correct")
            with trace.scope(task_id,"evaluation"):
                trace.emit("llm_eligibility","final",state=node["state"])
                trace.emit("independent_evaluation",grade)
            cost = costs.get(task_id,{})
            rows.append({"task_id":task_id,"task_type":reference["task_type"],"reward":reference["reward"],
                         "grade":grade,"actual":actual,"expected":reference["answer"],
                         "category":category,"detail":detail,"model":DEFAULT_MODEL,
                         "request_sent":node["sent"],"transport_attempts":node["transport_attempts"],
                         "independent_plan":node["independent"],"validation":node.get("validation","not_reached"),
                         "execution":node.get("execution","not_reached"),"planner_outcome":node["state"],
                         "usage_state":node.get("usage_state","not_called"),
                         "http_status":node.get("http_status"),"outer_json_valid":node.get("json_valid"),
                         "plan_json_valid":None if "plan_format" not in node else node["plan_format"]=="parsed",
                         "plan_schema_valid":node.get("plan_schema_valid"),
                         "operations_supported":node.get("operations_supported"),
                         "validation_reason":node.get("validation_reason"),
                         "response_contract_rejected":node.get("response_contract_rejected",False),
                         "finish_reason":node.get("finish_reason"),
                         "prompt_tokens":node.get("prompt_tokens"),"completion_tokens":node.get("completion_tokens"),
                         "confirmed_cost":str(Decimal(cost.get("spent_tenths",0))/10),
                         "reserved":str(Decimal(cost.get("reserved_tenths",0))/10),
                         "answer_mode":stat.S_IMODE(answer.stat().st_mode) if answer.exists() else None})
    return {"mode":"search_pilot" if search_manager is not None else "fallback_pilot" if pilot_manager is not None else "A2" if live_config else "offline_scenario","scenario":scenario,"live":live_config is not None,
            "runtime_sha256":runtime_fingerprint(),"model":DEFAULT_MODEL,"budget":budget_summary(manager),
            "max_requests":1 if pilot_manager is not None else A2_MAX_CALLS,"counts":counts,"seconds":time.perf_counter()-started,
            "fault_injected":fault is not None,"rows":rows,"events":trace.events,
            "tracing_enabled":trace_enabled,"mock_requests":requests,
            "categories":dict(Counter(row["category"] for row in rows)),
            "endpoint_digest":hashlib.sha256(live_config["base_url"].encode()).hexdigest() if live_config else None}


@contextmanager
def synthetic_response_files(enabled):
    """Only fixed artificial A1 replies, never prompts or arbitrary task data.

    Open private files in /tmp before any request. Keep descriptors open so a
    later symlink replacement cannot redirect a write. Do not capture headers,
    errors, the API envelope or reasoning_content.
    """
    if not enabled:
        yield None
        return
    from contextlib import ExitStack
    from safe_files import open_directory
    directory = Path(tempfile.mkdtemp(prefix="harness-a1-replies-",dir="/tmp"))
    with ExitStack() as stack:
        parent = stack.enter_context(open_directory(directory))
        handles = {}
        for spec in A1_PROBES:
            name = spec["id"]+".json"
            fd = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
            handles[spec["id"]] = stack.enter_context(os.fdopen(fd,"w",encoding="utf-8"))
        def write(probe_id,content,secret):
            if probe_id not in handles:
                raise ValueError("only fixed synthetic A1 probes can be captured")
            redacted = bool(secret and secret in content) or bool(re.search(r"\b(?:authorization|bearer)\b",content,re.IGNORECASE))
            # If the known key is echoed, omit the entire response, not merely
            # a prefix that could retain fragments around the truncation point.
            safe = "[response omitted: credential detected]" if redacted else content
            encoded = safe.encode("utf-8")
            clipped = encoded[:2048].decode("utf-8",errors="ignore")
            value = {"probe_id":probe_id,"synthetic_only":True,"content":clipped,
                     "original_characters":len(content),"truncated":len(encoded)>2048,
                     "credential_redacted":redacted}
            handle = handles[probe_id]
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(value,ensure_ascii=False)+"\n")
            handle.flush()
            os.fsync(handle.fileno())
            return {"path":str(directory/(probe_id+".json")),"truncated":value["truncated"],
                    "credential_redacted":redacted,"captured_utf8_bytes":len(clipped.encode("utf-8"))}
        yield write


def assess_a1_reply(spec,node,content,*,rejected,confirmed_cost):
    """Diagnose each layer; None means a downstream check was not possible."""
    json_valid = schema_valid = value_correct = None
    if isinstance(content,str):
        try:
            parsed = strict_json(content)
            json_valid = True
        except LLMError:
            json_valid = False
        if json_valid:
            if spec["id"]=="a1_ack":
                schema_valid = isinstance(parsed,dict) and set(parsed)=={"ok"} and type(parsed["ok"]) is bool
                if schema_valid: value_correct = parsed["ok"] is True
            elif spec["id"]=="a1_math":
                schema_valid = isinstance(parsed,dict) and set(parsed)=={"value"} and type(parsed["value"]) is str
                if schema_valid: value_correct = parsed["value"]=="5"
            else:
                schema_valid = (isinstance(parsed,dict) and set(parsed)=={"values"}
                                and isinstance(parsed["values"],list)
                                and all(type(n) is int for n in parsed["values"]))
                if schema_valid: value_correct = parsed["values"]==list(range(1,81))
    limit_probe = spec["id"]=="a1_limit"
    api_ok = (node.get("http_status")==200 and node.get("body_complete") is True
              and node.get("json_valid") is True and node.get("choices_count")==1
              and node.get("assistant_role") is True and node.get("content_type")=="string"
              and not node.get("tool_calls"))
    within_output = (type(node.get("completion_tokens")) is int
                     and node["completion_tokens"]<=spec["max_tokens"])
    within_input = (type(node.get("prompt_tokens")) is int
                    and node["prompt_tokens"]<=node.get("estimated_input",-1))
    expected_finish = "length" if limit_probe else "stop"
    finish_ok = node.get("finish_reason")==expected_finish
    # JSON/schema/value checks are observations, not pass requirements for the
    # intentionally truncated third probe. Its goal is to observe length.
    reply_ok = finish_ok and rejected if limit_probe else json_valid is True and schema_valid is True and value_correct is True
    checks = {"protocol_reply":bool(reply_ok),"http_api_compatible":bool(api_ok),
              "usage_confirmed":node.get("usage_state")=="confirmed",
              "model_tag_consistent_when_present":not node.get("model_reported") or node.get("model_matches") is True,
              "within_requested_output":within_output,"within_estimated_input":within_input,
              "noreason_observed":not node.get("reasoning_content_present") and node.get("reasoning_tokens",0)==0,
              "expected_finish_reason":finish_ok}
    # A benign reply failure may be examined further only with an explicit
    # diagnostic opt-in. All API/accounting/generation failures stay fatal.
    fatal = (any(not value for name,value in checks.items() if name!="protocol_reply")
             or (rejected and not (limit_probe and finish_ok)))
    layers = {
        "http_api":{"compatible":bool(api_ok),"http_status":node.get("http_status"),
                    "outer_json_valid":node.get("json_valid"),"body_complete":node.get("body_complete"),
                    "gateway_accepted":not rejected},
        "json":{"valid":json_valid},"schema":{"valid":schema_valid},"value":{"correct":value_correct},
        "usage":{"confirmed":checks["usage_confirmed"],"prompt_tokens":node.get("prompt_tokens"),
                 "completion_tokens":node.get("completion_tokens"),"confirmed_cost":confirmed_cost},
        "generation":{"max_tokens":spec["max_tokens"],"within_requested_output":within_output,
                      "expected_finish_reason":expected_finish,"finish_reason":node.get("finish_reason"),
                      "expected_finish_observed":finish_ok,"intentional_truncation":limit_probe},
    }
    return checks,layers,fatal


async def perform_a1_probe(gateway,manager,trace,spec,capture=None):
    """One fixed synthetic probe through the real gateway and shared ledger."""
    gateway.max_tokens = spec["max_tokens"]
    content = None
    rejected = False
    started = time.perf_counter()
    spent_before = manager.snapshot()["spent"]
    try:
        completion = await gateway.complete(spec["system"],spec["payload"],task_id=spec["id"])
        content = completion.content
    except LLMError:
        rejected = True
    seconds = time.perf_counter()-started
    node = trace.nodes[spec["id"]]
    # Also diagnose intentionally clipped synthetic text rejected by production.
    raw = trace._wire.get(spec["id"],{})
    choices = raw.get("choices")
    if content is None and isinstance(choices,list) and len(choices)==1 and isinstance(choices[0],dict):
        message = choices[0].get("message")
        if isinstance(message,dict) and isinstance(message.get("content"),str):
            content = message["content"]
    cost = str(manager.snapshot()["spent"]-spent_before) if node.get("usage_state")=="confirmed" else None
    checks,layers,fatal = assess_a1_reply(spec,node,content,rejected=rejected,confirmed_cost=cost)
    result = {"id":spec["id"],"contract_observed":all(checks.values()),"request_sent":node["sent"],
              "finish_reason":node.get("finish_reason"),"max_tokens":spec["max_tokens"],
              "prompt_tokens":node.get("prompt_tokens"),"completion_tokens":node.get("completion_tokens"),
              "usage_state":node.get("usage_state","unknown"),"response_content_type":node.get("content_type"),
              "checks":checks,"diagnostics":layers,"seconds":seconds,"confirmed_cost":cost}
    if capture is not None and isinstance(content,str):
        result["response_capture"] = capture(spec["id"],content,trace._secret)
    return result,fatal


def run_a1(*,live_config=None,trace_enabled=True,capture_response=False,continue_on_protocol_error=False):
    if live_config is not None: require_live_budget(A1_BUDGET)
    manager = BudgetManager(A1_BUDGET)
    ids = [item["id"] for item in A1_PROBES]
    secret = live_config["api_key"] if live_config else "cycle-a-synthetic-key"
    trace = Trace(ids,enabled=trace_enabled,secret=secret)
    requests = []
    factory = (lambda:httpx.AsyncHTTPTransport(retries=0)) if live_config else mock_factory(trace,"valid",{},requests)
    gateway = DiagnosticGateway(live_config["base_url"] if live_config else "http://cycle-a.invalid/v1",secret,
        model=DEFAULT_MODEL,manager=manager,max_tokens=32,timeout=20,trace=trace,transport_factory=factory,max_calls=3)
    results = []
    stop_reason = "completed"
    async def probes(capture):
        nonlocal stop_reason
        for spec in A1_PROBES:
            if manager.snapshot()["frozen"]:
                stop_reason = "budget_frozen"
                break
            result,fatal = await perform_a1_probe(gateway,manager,trace,spec,capture)
            results.append(result)
            if fatal or manager.snapshot()["frozen"]:
                stop_reason = "safety_or_api_failure"
                break
            if not result["contract_observed"] and not continue_on_protocol_error:
                stop_reason = "protocol_reply_failure"
                break
    with synthetic_response_files(capture_response) as capture,patch.dict(os.environ,{"BUDGET":str(A1_BUDGET)}),trace.observe(manager),redirect_stdout(io.StringIO()):
        asyncio.run(probes(capture))
    return {"mode":"A1","live":live_config is not None,"model":DEFAULT_MODEL,
            "runtime_sha256":runtime_fingerprint(),"diagnostic_sha256":diagnostic_fingerprint(),
            "contract_observed":len(results)==3 and all(r["contract_observed"] for r in results)
                and not manager.snapshot()["frozen"],
            "probes":results,"budget":budget_summary(manager),"max_requests":3,"events":trace.events,
            "request_attempts":sum(node["transport_attempts"] for node in trace.nodes.values()),
            "mock_requests":requests,"endpoint_digest":hashlib.sha256(live_config["base_url"].encode()).hexdigest() if live_config else None,
            "stop_reason":stop_reason,"continue_on_protocol_error":continue_on_protocol_error,
            "response_capture_enabled":capture_response,
            "evidence_scope":"Three observations, not a proven upper bound on future remote tokens or account charges."}


def offline_suite(*,trace_enabled=True):
    with fallback_controls():
        report = _offline_suite(trace_enabled=trace_enabled)
    report['forced_local_refusal'] = True
    return report


def _offline_suite(*,trace_enabled=True):
    experiments = [run_tasks_diagnostic(scenario=scenario,trace_enabled=trace_enabled) for scenario in SCENARIOS]
    expected = {"valid":("math_d15","success"),"wrong_plan":("math_d15","model_meaning"),
                "equivalent_rejected":("math_d15","validator_rejection"),"execution_error":("math_d15","execution"),
                "unroutable":("math_d08","routing"),"truncated_json":("math_d15","response_format"),
                "outer_json_invalid":("math_d15","response_format"),"finish_length":("math_d15","response_format"),
                "content_array":("math_d15","response_format"),"save_error":("math_d15","answer_publication"),
                "format_error":("math_d15","answer_publication")}
    failures = []
    for experiment in experiments:
        task_id,wanted = expected.get(experiment["scenario"],("math_d15","api_or_budget"))
        row = next(r for r in experiment["rows"] if r["task_id"]==task_id)
        if row["category"]!=wanted: failures.append({"scenario":experiment["scenario"],"wanted":wanted,"actual":row["category"]})
        if any(request["reference_fields_present"] for request in experiment["mock_requests"]):
            failures.append({"scenario":experiment["scenario"],"error":"reference_input_leak"})
        control = next((r for r in experiment["rows"] if r["task_id"]=="math_d01"),None)
        if control is not None and control["grade"]!="correct": failures.append({"scenario":experiment["scenario"],"error":"local_control_lost"})
    protocol = run_a1(trace_enabled=trace_enabled)
    if not protocol["contract_observed"]: failures.append({"scenario":"A1_mock","error":"protocol_probe_failed"})
    return {"mode":"offline_suite","suite_version":SUITE_VERSION,"live":False,"real_api_requests":0,
            "runtime_sha256":runtime_fingerprint(),"diagnostic_sha256":diagnostic_fingerprint(),"passed":not failures,"failures":failures,
            "experiments":experiments,"A1_mock":protocol,"plan":experiment_plan()}


def validate_offline_receipt(receipt):
    if (receipt.get("mode")!="offline_suite" or receipt.get("live") is not False or receipt.get("passed") is not True
        or receipt.get("suite_version")!=SUITE_VERSION or receipt.get("runtime_sha256")!=runtime_fingerprint()):
        raise ValueError("a passing offline receipt for this runtime is required")
    if receipt.get("diagnostic_sha256")!=diagnostic_fingerprint():
        raise ValueError("offline receipt belongs to a different diagnostic version")


def validate_a1_receipt(receipt,base_url):
    if (receipt.get("mode")!="A1" or receipt.get("live") is not True or receipt.get("contract_observed") is not True
        or receipt.get("model")!=DEFAULT_MODEL or receipt.get("runtime_sha256")!=runtime_fingerprint()
        or receipt.get("diagnostic_sha256")!=diagnostic_fingerprint()
        or receipt.get("endpoint_digest")!=hashlib.sha256(base_url.encode()).hexdigest()
        or receipt.get("request_attempts")!=3 or receipt.get("budget",{}).get("frozen") is not False
        or receipt.get("budget",{}).get("uncertain_calls")!=0
        or len(receipt.get("probes",[]))!=3
        or {probe.get("id") for probe in receipt.get("probes",[])}!={probe["id"] for probe in A1_PROBES}
        or not all(probe.get("contract_observed") is True and probe.get("request_sent") is True for probe in receipt.get("probes",[]))):
        raise ValueError("A2 requires a successful LIVE A1 receipt for this runtime, endpoint and model")


def markdown(report):
    lines = ["# Cycle A: диагностика", "", f"Режим: {report['mode']}; live={report.get('live',False)}.", "",
             "Mock-результаты не подтверждают NeuralDeep. Реальные вызовы возможны только с отдельным разрешением.", ""]
    experiments = report.get("experiments",[report] if "rows" in report else [])
    if report.get("mode")=="plan":
        plan = report["plan"]
        lines += ["## План", "", f"Модель: {plan['model']}. Предполагаемый endpoint: {plan['endpoint_assumption']}.",
                  f"A1: до 3 запросов, BUDGET={plan['A1']['budget']}, сумма оценочных резервов {plan['A1']['planned_reservation']}.",
                  f"A2: {plan['A2']['tasks']} задач, до {plan['A2']['max_requests']} запросов, BUDGET={plan['A2']['budget']}, оценочные резервы {plan['A2']['planned_reservation']}.",
                  "Это планируемый расход, не доказанный верхний предел удалённого сервера.", "",
                  "| ID | Тип | Reward | Эталон оценщика | Локальный статус | Возможен LLM | Резерв при вызове | Причина выбора |",
                  "| --- | --- | ---: | --- | --- | --- | ---: | --- |"]
        for row in plan["selection"]:
            lines.append(f"| {row['task_id']} | {row['task_type']} | {row['reward']} | {row['expected']} | {row['local_status']} | {row['natural_llm_candidate']} | {row['reservation']} | {row['reason']} |")
    for experiment in experiments:
        lines += [f"## {experiment['scenario']}", "", f"Расход: `{json.dumps(experiment['budget'])}`.", "",
                  "| ID | Оценка | Категория | Причина | Запрос отправлен | План | Исполнение |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for row in experiment["rows"]:
            lines.append(f"| {row['task_id']} | {row['grade']} | {row['category']} | {row['detail']} | {row['request_sent']} | {row['independent_plan']} | {row['execution']} |")
    if report.get("mode")=="offline_suite": lines += ["",f"Проверки классификации: passed={report['passed']}; failures={len(report['failures'])}."]
    if report.get("mode")=="A1":
        lines += ["",f"Наблюдаемый контракт: {report['contract_observed']}. Budget: `{json.dumps(report['budget'])}`."]
    return "\n".join(lines)+"\n"


@contextmanager
def output_files(base, *, live=False):
    """Reserve writable output BEFORE credential reads or possible network I/O."""
    base = Path(base)
    base.parent.mkdir(parents=True,exist_ok=True)
    handles = []
    try:
        for suffix in (".json",".md"):
            fd = os.open(base.with_suffix(suffix),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            handles.append(os.fdopen(fd,"w",encoding="utf-8"))
        def write(report):
            for handle,text in zip(handles,(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n",markdown(report))):
                handle.seek(0)
                handle.truncate()
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        write({"mode":"incomplete","live":live,"request_status":"unknown_if_interrupted",
               "note":"Incomplete receipt; requests may have been sent. Never sufficient to authorise A2 or retry automatically."})
        yield write
    finally:
        for handle in handles: handle.close()


def read_receipt(path):
    from safe_files import read_text, TASK_JSON_BYTES
    path = Path(path).absolute()
    if path.suffix!=".json": raise ValueError("receipt must be JSON")
    return json.loads(read_text(path.parent,path.name,maximum=TASK_JSON_BYTES))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode",choices=("plan","offline","A1","A2"),default="plan")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--live",action="store_true",help="requires separately granted user permission; never inferred from environment")
    parser.add_argument("--offline-report",type=Path)
    parser.add_argument("--a1-report",type=Path)
    parser.add_argument("--no-trace",action="store_true")
    parser.add_argument("--capture-a1-response",action="store_true",help="A1 only: private bounded synthetic replies in /tmp; never prompts or headers")
    parser.add_argument("--continue-on-protocol-error",action="store_true",help="A1 only: continue after reply JSON/schema/value failures; API/budget failures still stop; requires approval for every planned request")
    args = parser.parse_args(argv)
    if args.live != (args.mode in {"A1","A2"}):
        parser.error("A1/A2 require explicit --live; plan/offline cannot use --live")
    if (args.capture_a1_response or args.continue_on_protocol_error) and args.mode!="A1":
        parser.error("synthetic response capture and continuation policy are A1-only")
    with output_files(args.output,live=args.live) as write:
        if not args.live:
            report = offline_suite(trace_enabled=not args.no_trace) if args.mode=="offline" else {"mode":"plan","live":False,"plan":experiment_plan()}
        else:
            if args.offline_report is None: parser.error("a passing --offline-report is required")
            validate_offline_receipt(read_receipt(args.offline_report))
            if args.mode=="A2" and args.a1_report is None: parser.error("--a1-report from a successful live A1 is required")
            require_live_budget(A1_BUDGET if args.mode=="A1" else A2_BUDGET)
            # The ONLY real credential reads in this utility, behind --live and
            # validated receipts. These branches are not executed in preparation.
            config = {"base_url":os.environ.get("LLM_BASE_URL",""),"api_key":os.environ.get("LLM_API_KEY","")}
            if not all(config.values()): parser.error("LLM_BASE_URL and LLM_API_KEY are required")
            if args.mode=="A2": validate_a1_receipt(read_receipt(args.a1_report),config["base_url"])
            report = run_a1(live_config=config,trace_enabled=not args.no_trace,
                            capture_response=args.capture_a1_response,
                            continue_on_protocol_error=args.continue_on_protocol_error) if args.mode=="A1" else run_tasks_diagnostic(live_config=config,trace_enabled=not args.no_trace)
        write(report)
    print(json.dumps({"mode":report["mode"],"live":report.get("live",False),"passed":report.get("passed"),
                      "contract_observed":report.get("contract_observed"),"output":str(args.output)},ensure_ascii=False))
    return 0 if report.get("passed",report.get("contract_observed",True)) else 1


if __name__=="__main__":
    raise SystemExit(main())
