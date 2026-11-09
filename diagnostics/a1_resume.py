"""One-shot continuation of the reviewed LIVE A1: a1_math only.

Default --check is offline. A durable send-intent is consumed before a possible
HTTP request. --recover only reads evidence; it never releases or retries it.
"""

import argparse
import asyncio
from contextlib import contextmanager, redirect_stdout
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import stat

import httpx

from benchmark.evaluate import runtime_fingerprint
from budget_manager import BudgetManager
from diagnostics.cycle_a import (budget_summary, diagnostic_fingerprint, output_files,
    perform_a1_probe, read_receipt, synthetic_response_files, validate_offline_receipt)
from diagnostics.selection import A1_PROBES, A1_BUDGET
from diagnostics.trace import DiagnosticGateway, Trace
from llm_gateway import DEFAULT_MODEL, strict_json
from safe_files import open_directory, open_text, read_text, TASK_JSON_BYTES


# Reviewed immutable evidence, not a switch for accepting arbitrary stale hashes.
# The historical diagnostic version is checked against this exact receipt;
# current code must additionally have its own passing offline receipt.
SOURCE_SHA256 = "d119deb1bde48bc351c97bdfb916477f32730513a24a2e90c6c92dcf2bd7b50b"
ACK_USAGE = (35, 11)
LEGACY_DIAGNOSTICS = {
    "cycle_a.py":"283490d7c50beb79a761d22f7d7fdd68f8f04fe5a1990199d88a4220eff16351",
    "selection.py":"4a215126eec1bf072a73fa42f0dcb77bc93a364f9ccab3cfacf0117ed0b37d0c",
    "trace.py":"42c5b064b2d9304b903d08380b133f975e1b003ba8d100586427229fc31e7fb0",
}
PARAMETERS_SHA256 = "18057129922e6c0c033003ae4dd86f65811987bd9822fe6f70daa9f4e4029473"
BASE_URL = "https://api.neuraldeep.ru/v1"
STATE_ROOT = Path(__file__).resolve().parent / ".a1-resume-state"
INTENT_NAME = SOURCE_SHA256 + ".a1_math.intent.json"
RESULT_NAME = SOURCE_SHA256 + ".a1_math.result.json"


class ResumeBlocked(ValueError):
    """Safe fixed diagnostic; never includes provider messages or credentials."""


def parameters_fingerprint():
    value = {"model":DEFAULT_MODEL,"budget":A1_BUDGET,"probes":A1_PROBES}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def validate_source(path):
    path = Path(path).absolute()
    with open_text(path.parent,path.name,maximum=TASK_JSON_BYTES,newline="") as handle:
        text = handle.read()
    if hashlib.sha256(text.encode("utf-8")).hexdigest()!=SOURCE_SHA256:
        raise ResumeBlocked("historical receipt hash mismatch")
    receipt = strict_json(text)
    if (receipt.get("mode")!="A1" or receipt.get("live") is not True
        or receipt.get("model")!="qwen3.6-35b-a3b-noreason"
        or receipt.get("request_attempts")!=1 or receipt.get("contract_observed") is not False):
        raise ResumeBlocked("receipt is not the reviewed LIVE A1 history")
    if receipt.get("endpoint_digest")!=hashlib.sha256(BASE_URL.encode()).hexdigest():
        raise ResumeBlocked("historical endpoint hash mismatch")
    if receipt.get("runtime_sha256")!=runtime_fingerprint():
        raise ResumeBlocked("runtime hash mismatch")
    if receipt.get("diagnostic_sha256")!=LEGACY_DIAGNOSTICS:
        raise ResumeBlocked("historical diagnostic hash mismatch")
    if (diagnostic_fingerprint()["selection.py"]!=LEGACY_DIAGNOSTICS["selection.py"]
        or parameters_fingerprint()!=PARAMETERS_SHA256):
        raise ResumeBlocked("reviewed synthetic parameters changed")
    probes = receipt.get("probes",[])
    if (len(probes)!=1 or probes[0].get("id")!="a1_ack"
        or probes[0].get("request_sent") is not True or probes[0].get("usage_state")!="confirmed"
        or (probes[0].get("prompt_tokens"),probes[0].get("completion_tokens"))!=ACK_USAGE
        or probes[0].get("max_tokens")!=32 or probes[0].get("finish_reason")!="stop"):
        raise ResumeBlocked("historical attempt or usage is uncertain")
    manager = restore_history()
    if receipt.get("budget")!=budget_summary(manager):
        raise ResumeBlocked("historical budget mismatch")
    return receipt


def restore_history():
    manager = BudgetManager(600)
    manager.reserve("historical:a1_ack","a1_ack",1170,32)
    manager.settle("historical:a1_ack",*ACK_USAGE)
    return manager


@contextmanager
def state_directory(*,create=False,root=None):
    root = STATE_ROOT if root is None else Path(root)
    if create:
        # Fixed project-local persistent state, independent of output/source path.
        # Every component is opened without following links.
        with open_directory(root.parent) as parent:
            try:
                os.mkdir(root.name,0o700,dir_fd=parent)
            except FileExistsError:
                pass
            os.fsync(parent)
    with open_directory(root) as fd:
        info = os.fstat(fd)
        if info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise ResumeBlocked("state directory must be private and owned by this user")
        yield fd


def write_once(parent,name,value):
    """A crash/failed fsync consumes the name conservatively; never remove it."""
    fd = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=parent)
    with os.fdopen(fd,"w",encoding="utf-8") as handle:
        json.dump(value,handle,ensure_ascii=False,sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.fsync(parent)  # Persist the directory entry BEFORE possible network I/O.


def state_status():
    if not os.path.lexists(STATE_ROOT):
        return "ready",None
    with state_directory() as parent:
        names = set(os.listdir(parent))
        if not names:
            return "ready",None
        if INTENT_NAME not in names or names-{INTENT_NAME,RESULT_NAME}:
            raise ResumeBlocked("unrecognised or orphaned state; manual investigation required")
        # Even an empty/torn intent means that a request MAY have been sent.
        if RESULT_NAME not in names:
            return "uncertain",None
        try:
            report = strict_json(read_text(STATE_ROOT,RESULT_NAME,maximum=TASK_JSON_BYTES))
            if (report.get("mode")!="A1_resume" or report.get("source_sha256")!=SOURCE_SHA256
                or report.get("probe_id")!="a1_math" or report.get("series_attempts_upper_bound")!=2):
                return "uncertain",None
            if report.get("budget",{}).get("frozen") or report.get("new_probe",{}).get("usage_state")!="confirmed":
                return "blocked",report
            return "completed",report
        except (ValueError,OSError,UnicodeError,AttributeError):
            return "uncertain",None


def preflight(source,offline_receipt,*,probe="a1_math"):
    if probe!="a1_math":
        raise ResumeBlocked("only a1_math is permitted; ack and limit are forbidden")
    history = validate_source(source)
    validate_offline_receipt(offline_receipt)
    status,_ = state_status()
    if status!="ready":
        raise ResumeBlocked("a1_math already claimed; use offline --recover, never retry")
    return history


def run_resume(source,offline_receipt,config,*,capture_response,probe="a1_math",_transport_factory=None):
    history = preflight(source,offline_receipt,probe=probe)
    if BudgetManager.from_env().limit!=600:
        raise ResumeBlocked("A1 series requires explicit BUDGET=600")
    if config.get("base_url")!=BASE_URL:
        raise ResumeBlocked("configured endpoint does not match historical A1")
    if not capture_response:
        raise ResumeBlocked("synthetic response capture is required for this continuation")
    manager = restore_history()
    spec = next(p for p in A1_PROBES if p["id"]=="a1_math")
    historical_cost = budget_summary(manager)["spent"]
    trace = Trace(["a1_math"],secret=config["api_key"])
    gateway = DiagnosticGateway(config["base_url"],config["api_key"],model=DEFAULT_MODEL,
        manager=manager,max_tokens=64,timeout=20,trace=trace,max_calls=1,
        transport_factory=_transport_factory or (lambda:httpx.AsyncHTTPTransport(retries=0)))
    estimate = gateway.estimate_input(spec["system"],spec["payload"])
    reservation = Decimal(estimate)/10+64
    if manager.available<reservation:
        raise ResumeBlocked("historical budget leaves insufficient reserve")
    intent = {"mode":"A1_resume_intent","version":1,"source_sha256":SOURCE_SHA256,
              "probe_id":"a1_math","model":DEFAULT_MODEL,"max_tokens":64,
              "endpoint_digest":history["endpoint_digest"],"runtime_sha256":runtime_fingerprint(),
              "diagnostic_sha256":diagnostic_fingerprint(),"parameters_sha256":parameters_fingerprint(),
              "historical_confirmed_cost":historical_cost,"budget_before":budget_summary(manager),
              "estimated_input":estimate,"pending_reserve":str(reservation),
              "series_attempts_upper_bound":2,"request_status":"may_have_been_sent"}
    # Capture descriptors and durable intent must exist before complete().
    with synthetic_response_files(True) as capture,state_directory(create=True) as parent:
        write_once(parent,INTENT_NAME,intent)
        with trace.observe(manager),redirect_stdout(io.StringIO()):
            result,fatal = asyncio.run(perform_a1_probe(gateway,manager,trace,spec,capture))
        report = {"mode":"A1_resume","live":_transport_factory is None,
                  "source_sha256":SOURCE_SHA256,"probe_id":"a1_math","model":DEFAULT_MODEL,
                  "endpoint_digest":history["endpoint_digest"],"runtime_sha256":runtime_fingerprint(),
                  "diagnostic_sha256":diagnostic_fingerprint(),"parameters_sha256":parameters_fingerprint(),
                  "historical_attempts":1,"new_transport_attempts":trace.nodes["a1_math"]["transport_attempts"],
                  "series_attempts_upper_bound":2,"max_new_requests":1,
                  "historical_confirmed_cost":historical_cost,"historical_contract_observed":False,
                  "contract_observed":False,"new_probe":result,"budget":budget_summary(manager),
                  "safety_stop":fatal or manager.snapshot()["frozen"],"events":trace.events,
                  "retry_permitted":False,"A2_permitted":False,"a1_limit_permitted":False}
        write_once(parent,RESULT_NAME,report)
    return report


def recover(source):
    """Read-only recovery. Uncertain attempts remain reserved and frozen."""
    validate_source(source)
    status,report = state_status()
    if report is not None:
        return report
    manager = restore_history()
    if status=="uncertain":
        # No live replay, including after a crash before transport actually ran.
        spec = next(p for p in A1_PROBES if p["id"]=="a1_math")
        from llm_gateway import LLMGateway
        estimate = len(json.dumps(LLMGateway._messages(spec["system"],spec["payload"]),ensure_ascii=False,allow_nan=False).encode())+1024
        manager.reserve("uncertain:a1_math","a1_math",estimate,64)
        manager.mark_uncertain("uncertain:a1_math")
    return {"mode":"A1_resume_recovery","live":False,"state":status,"source_sha256":SOURCE_SHA256,
            "probe_id":"a1_math","request_status":"unknown" if status=="uncertain" else "not_attempted",
            "series_attempts_upper_bound":2 if status=="uncertain" else 1,
            "budget":budget_summary(manager),"retry_permitted":False,"A2_permitted":False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--live",action="store_true")
    action.add_argument("--recover",action="store_true")
    parser.add_argument("--resume-from",type=Path,required=True)
    parser.add_argument("--offline-report",type=Path)
    parser.add_argument("--probe",choices=("a1_math",),default="a1_math")
    parser.add_argument("--capture-a1-response",action="store_true")
    parser.add_argument("--output",type=Path)
    args = parser.parse_args(argv)
    try:
        if args.recover:
            report = recover(args.resume_from)
        else:
            if args.offline_report is None:
                raise ResumeBlocked("a current passing offline receipt is required")
            offline = read_receipt(args.offline_report)
            preflight(args.resume_from,offline,probe=args.probe)
            if not args.live:
                report = {"mode":"A1_resume_check","live":False,"ready":True,"probe_id":"a1_math",
                          "historical_attempts":1,"max_new_requests":1,"series_attempts_upper_bound":2,
                          "budget":budget_summary(restore_history()),"parameters_sha256":parameters_fingerprint()}
            else:
                if args.output is None or not args.capture_a1_response:
                    raise ResumeBlocked("live resume requires separate output and synthetic response capture")
                if BudgetManager.from_env().limit!=600:
                    raise ResumeBlocked("A1 series requires explicit BUDGET=600")
                with output_files(args.output,live=True) as write:
                    # Actual credential reads occur ONLY in this explicit branch.
                    config = {"base_url":os.environ.get("LLM_BASE_URL",""),"api_key":os.environ.get("LLM_API_KEY","")}
                    if not config["api_key"].strip():
                        raise ResumeBlocked("LLM_API_KEY is missing")
                    report = run_resume(args.resume_from,offline,config,capture_response=True)
                    write(report)
                print(json.dumps({"mode":report["mode"],"output":str(args.output),"new_transport_attempts":report["new_transport_attempts"]}))
                return 0 if report["new_probe"]["contract_observed"] and not report["safety_stop"] else 1
        if args.output is not None:
            with output_files(args.output) as write: write(report)
        print(json.dumps(report,ensure_ascii=False,sort_keys=True))
        return 0
    except ResumeBlocked as error:
        print(json.dumps({"ready":False,"error":str(error)}))
    except (ValueError,OSError,UnicodeError):
        print(json.dumps({"ready":False,"error":"input, state, hash or filesystem validation failed; no automatic retry"}))
    return 2


if __name__=="__main__":
    raise SystemExit(main())
