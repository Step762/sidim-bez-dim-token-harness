"""Explicitly approved one-task pilot, independent of the blocked A2 gate.

Default is offline preflight. The only live task is math_d15, one attempt, 512
output tokens, cumulative budget 900 including the two reviewed A1 requests.
"""

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path

from benchmark.evaluate import DATASET, runtime_fingerprint
from budget_manager import BudgetManager, cost_tenths, units
from diagnostics import a1_resume
from diagnostics.cycle_a import (budget_summary, diagnostic_fingerprint, output_files,
    read_receipt, run_tasks_diagnostic, validate_offline_receipt)
from diagnostics.selection import load_selection
from llm_fallback import prepare
from llm_gateway import DEFAULT_MODEL, LLMGateway, strict_json
from safe_files import read_text, open_directory, TASK_JSON_BYTES


TASK_ID = "math_d15"
LIMIT = 900
MAX_TOKENS = 512
STATE_ROOT = Path(__file__).resolve().parent/".fallback-pilot-state"
INTENT_NAME = "math_d15.intent.json"
RESULT_NAME = "math_d15.result.json"
MATH_RESULT_SHA = "5b0b429ac83c607665de08cd10418ff5bf4e24e8637996c5661f797d73df9065"
MATH_USAGE = (53, 7)
MATH_INTENT_SHA = "17a8a6ff80fe7e7699f257cf9b03cc22483b5b68aee5091a918a839bebdba454"
HISTORY_DIAGNOSTICS = {
    "a1_resume.py":"7c6024c68ead4f08b265efa869978af5affbff86a863898566e5225ce0b0674b",
    "cycle_a.py":"27881a07d987fd0bfcc42e774444bedd4eaac34cb39bd6a91c7f3e61678ee561",
    "selection.py":"4a215126eec1bf072a73fa42f0dcb77bc93a364f9ccab3cfacf0117ed0b37d0c",
    "trace.py":"3ec61482774dc321073262ddc90b1def8ee2dd16dab16511a1093ba9344997d8",
}


def read_record(path,expected_sha=None):
    path = Path(path).absolute()
    text = read_text(path.parent,path.name,maximum=TASK_JSON_BYTES)
    if expected_sha and hashlib.sha256(text.encode()).hexdigest()!=expected_sha:
        raise a1_resume.ResumeBlocked("reviewed A1 history hash mismatch")
    return strict_json(text)


def history_manager(limit=LIMIT):
    manager = BudgetManager(limit)
    for task,estimated,cap,inputs,outputs in (("a1_ack",1170,32,*a1_resume.ACK_USAGE),("a1_math",1241,64,*MATH_USAGE)):
        request = "historical:"+task
        manager.reserve(request,task,estimated,cap)
        manager.settle(request,inputs,outputs)
    return manager


def validate_history(original,math_report):
    original = a1_resume.validate_source(original)
    # Reuse the private A1 journal, but NEVER write or release it.
    with a1_resume.state_directory():
        intent = read_record(a1_resume.STATE_ROOT/a1_resume.INTENT_NAME,MATH_INTENT_SHA)
        record = read_record(a1_resume.STATE_ROOT/a1_resume.RESULT_NAME,MATH_RESULT_SHA)
    if read_record(math_report)!=record:
        raise a1_resume.ResumeBlocked("provided resume report differs from the reviewed A1 journal")
    probe = record["new_probe"]
    if (record.get("mode")!="A1_resume" or record.get("live") is not True
        or record.get("model")!=DEFAULT_MODEL or record.get("source_sha256")!=a1_resume.SOURCE_SHA256
        or record.get("endpoint_digest")!=original["endpoint_digest"]
        or record.get("runtime_sha256")!=runtime_fingerprint()
        or record.get("diagnostic_sha256")!=HISTORY_DIAGNOSTICS
        or intent.get("diagnostic_sha256")!=HISTORY_DIAGNOSTICS
        or record.get("parameters_sha256")!=a1_resume.parameters_fingerprint()
        or record.get("new_transport_attempts")!=1 or record.get("series_attempts_upper_bound")!=2
        or record.get("safety_stop") is not False or probe.get("contract_observed") is not True
        or probe.get("usage_state")!="confirmed" or not all(probe.get("checks",{}).values())
        or (probe.get("prompt_tokens"),probe.get("completion_tokens"))!=MATH_USAGE
        or probe.get("confirmed_cost")!=str(units(cost_tenths(*MATH_USAGE)))
        or record.get("budget")!=budget_summary(history_manager(600))):
        raise a1_resume.ResumeBlocked("A1 history, parameters or confirmed expense is inconsistent")
    return record


def ensure_unclaimed():
    if os.path.lexists(STATE_ROOT):
        with a1_resume.state_directory(root=STATE_ROOT) as parent:
            if os.listdir(parent):
                raise a1_resume.ResumeBlocked("pilot already claimed; never retry or clear the journal")


def preflight(original,math_report,offline):
    validate_history(original,math_report)
    validate_offline_receipt(offline)
    ensure_unclaimed()
    case = load_selection()[TASK_ID]
    if case["reference"]["split"]!="development" or case["task"]["task_type"]!="math" or case["local_status"]!="skipped":
        raise a1_resume.ResumeBlocked("only the frozen development math_d15 fallback is permitted")
    prepared = prepare("math",case["task"]["instruction"],DATASET/"tasks"/TASK_ID)
    manager = history_manager()
    gateway = LLMGateway(a1_resume.BASE_URL,"pilot-preflight-not-a-key",manager=manager,max_tokens=MAX_TOKENS)
    estimate = gateway.estimate_input(prepared.system,prepared.payload)
    reserve = Decimal(estimate)/10+MAX_TOKENS
    if reserve>manager.available:
        raise a1_resume.ResumeBlocked("pilot reserve does not fit the cumulative budget")
    prompt_digest = hashlib.sha256(json.dumps(gateway._messages(prepared.system,prepared.payload),ensure_ascii=False).encode()).hexdigest()
    return {"mode":"fallback_pilot_check","ready":True,"live":False,"task_id":TASK_ID,
            "model":DEFAULT_MODEL,"max_new_requests":1,"max_tokens":MAX_TOKENS,
            "budget_before":budget_summary(manager),"estimated_input":estimate,"reservation":str(reserve),
            "history_plus_reserve":str(manager.snapshot()["spent"]+reserve),"prompt_sha256":prompt_digest,
            "A1_contract_observed":False,"A2_permitted":False}


def run_pilot(original,math_report,offline,*,config=None,scenario="valid",workspace=None):
    plan = preflight(original,math_report,offline)
    if config is not None:
        if scenario!="valid": raise a1_resume.ResumeBlocked("live fault injection is forbidden")
        if BudgetManager.from_env().limit!=LIMIT: raise a1_resume.ResumeBlocked("pilot requires explicit BUDGET=900")
        if config.get("base_url")!=a1_resume.BASE_URL: raise a1_resume.ResumeBlocked("configured endpoint differs from A1")
        key = config.get("api_key","")
        if not key.strip() or any(c in key for c in "\r\n"):
            raise a1_resume.ResumeBlocked("LLM_API_KEY is missing or invalid")
    manager = history_manager()
    intent = {**plan,"mode":"fallback_pilot_intent","live":config is not None,
              "source_sha256":a1_resume.SOURCE_SHA256,"math_resume_sha256":MATH_RESULT_SHA,
              "runtime_sha256":runtime_fingerprint(),"diagnostic_sha256":diagnostic_fingerprint(),
              "request_status":"may_have_been_sent","total_attempts_upper_bound":3}
    with a1_resume.state_directory(root=STATE_ROOT,create=True) as parent:
        a1_resume.write_once(parent,INTENT_NAME,intent)
        report = run_tasks_diagnostic(scenario=scenario,task_ids=[TASK_ID],live_config=config,
                                     pilot_manager=manager,pilot_workspace=workspace)
        if report["rows"][0]["usage_state"]!="confirmed":
            report["rows"][0]["confirmed_cost"] = None
        report.update(historical_confirmed_cost=budget_summary(history_manager())["spent"],historical_attempts=2,total_attempts_upper_bound=3,
                      A1_contract_observed=False,A2_permitted=False,retry_permitted=False,
                      safety_stop=manager.snapshot()["frozen"] or report["rows"][0]["response_contract_rejected"],
                      forecast=plan,source_sha256=a1_resume.SOURCE_SHA256,math_resume_sha256=MATH_RESULT_SHA)
        a1_resume.write_once(parent,RESULT_NAME,report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a1-report",type=Path,required=True)
    parser.add_argument("--a1-resume-report",type=Path,required=True)
    parser.add_argument("--offline-report",type=Path,required=True)
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--output",type=Path,help="new private directory; required for live")
    args = parser.parse_args(argv)
    try:
        offline = read_receipt(args.offline_report)
        plan = preflight(args.a1_report,args.a1_resume_report,offline)
        if not args.live:
            print(json.dumps(plan,ensure_ascii=False,sort_keys=True))
            return 0
        if args.output is None: raise a1_resume.ResumeBlocked("live pilot requires a new private output directory")
        if BudgetManager.from_env().limit!=LIMIT: raise a1_resume.ResumeBlocked("pilot requires explicit BUDGET=900")
        with open_directory(args.output.absolute().parent) as parent:
            os.mkdir(args.output.name,0o700,dir_fd=parent)
        with output_files(args.output/"report",live=True) as write:
            # The only actual credential reads: explicit live and passing checks.
            config = {"base_url":os.environ.get("LLM_BASE_URL",""),"api_key":os.environ.get("LLM_API_KEY","")}
            report = run_pilot(args.a1_report,args.a1_resume_report,offline,config=config,workspace=args.output/"tasks")
            write(report)
        print(json.dumps({"mode":"fallback_pilot","grade":report["rows"][0]["grade"],"output":str(args.output)}))
        return 0 if report["rows"][0]["grade"]=="correct" and not report["safety_stop"] else 1
    except a1_resume.ResumeBlocked as error:
        print(json.dumps({"ready":False,"error":str(error)}))
    except (ValueError,OSError,UnicodeError):
        print(json.dumps({"ready":False,"error":"history, hash, budget or filesystem check failed; no automatic retry"}))
    return 2


if __name__=="__main__":raise SystemExit(main())
