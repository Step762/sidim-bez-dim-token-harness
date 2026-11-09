"""Opt-in development observers. No production module imports this module.

Hooks call the original workflow, preparation, validation, executor and ledger.
Wire observation delegates the same byte stream, without retries or extra reads.
Only allowlisted metadata is serialised here; bodies/plans stay in memory.
cycle_a separately offers opt-in private /tmp capture for synthetic A1 text.
"""

from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from collections import Counter
import asyncio
from fractions import Fraction
import json
from pathlib import Path
import stat
from unittest.mock import patch

import httpx
import harness
import llm_fallback
import planner
import data_solver
from budget_manager import BudgetError
from llm_gateway import LLMError, LLMGateway, MAX_RESPONSE_BYTES, MODELS
from openjiuwen.core.workflow import Workflow
from plan_semantics import UnsupportedMeaning


_SCOPE = ContextVar("development_trace_scope", default=(None,"none"))
_FIELDS = frozenset({"phase","http_status","bytes_received","body_complete","json_valid",
    "choices_count","content_type","finish_reason","tool_calls","reasoning_content_present",
    "prompt_tokens","completion_tokens","total_tokens","reasoning_tokens","usage_present",
    "model_matches","estimated_input","max_tokens","reserved","spent","frozen","state",
    "independent","operation","method","fault_injected","error_code","mode","size",
    "plan_kind","steps_count","unknown_fields","requested_model","confirmed","sent"})
_FIELDS = _FIELDS | {"accounted_output_tokens","reasoning_conflict","model_reported","assistant_role"}
_FIELDS = _FIELDS | {"reason_code","plan_schema_valid","operations_supported"}


def math_plan_shape(plan):
    """Read-only checks of the documented production math plan fields."""
    operations = {"add","sub","mul","div","floordiv","increase_percent","decrease_percent","percent"}
    if not isinstance(plan,dict) or not isinstance(plan.get("steps"),list): return False,None
    steps = plan["steps"]
    supported = all(isinstance(s,dict) and isinstance(s.get("op"),str) and s["op"] in operations for s in steps)
    try:
        if set(plan)!={"initial","steps"} or not 1<=len(steps)<=12: return False,supported
        if set(plan["initial"])!={"value","quote"}: return False,supported
        for item in [plan["initial"],*steps]:
            if (not isinstance(item,dict) or set(item) not in ({"value","quote"},{"op","value","quote"})
                or not isinstance(item["quote"],str) or not item["quote"]): return False,supported
            llm_fallback._scalar(item["value"])
        return supported,supported
    except (ValueError,TypeError,KeyError): return False,supported


def exception_code(error):
    # Never copy exception messages: provider errors can contain credentials/data.
    if isinstance(error,asyncio.CancelledError):
        return "cancelled_or_timeout"
    if isinstance(error,(TimeoutError,httpx.TimeoutException)):
        return "timeout"
    if isinstance(error,httpx.HTTPError):
        return "transport"
    if isinstance(error,BudgetError):
        return "budget_denied"
    if isinstance(error,OSError):
        return "filesystem"
    if isinstance(error,(ValueError,TypeError,KeyError)):
        return "invalid_or_unsupported"
    return "other"


def _number(value):
    if type(value) not in (str,int) or len(str(value))>80:
        raise ValueError("not an assessed number")
    return Fraction(value)


def _affine(step):
    value = _number(step["value"])
    op = step["op"]
    if op=="add": return Fraction(1),value
    if op=="sub": return Fraction(1),-value
    if op=="mul": return value,Fraction(0)
    if op=="div" and value: return 1/value,Fraction(0)
    if op=="increase_percent": return 1+value/100,Fraction(0)
    if op=="decrease_percent": return 1-value/100,Fraction(0)
    raise ValueError("operation not independently assessed")


def assess_plan(plan, reviewed, instruction=None):
    """Small independent semantic oracle for the three selected reviewed plans.

Does not infer semantic correctness from final numeric equality. Math compares
each ordered affine map, including sub(8) == add(-8). This is an experiment
oracle, NOT a replacement validator or a proof for arbitrary instructions.
"""
    if plan=={"status":"unsolvable"}:
        return "abstained"
    if reviewed is None:
        return "not_assessed"
    if not isinstance(plan,dict):
        return "format_invalid"
    def grounded(quote, expected):
        if not isinstance(quote,str) or not quote:
            return False
        return quote==expected if instruction is None else quote in instruction and expected in quote
    try:
        if "initial" in reviewed:
            if set(plan)!={"initial","steps"} or set(plan["initial"])!={"value","quote"}:
                return "format_invalid"
            if type(plan["initial"]["value"]) is not str:
                return "format_invalid"
            if _number(plan["initial"]["value"])!=_number(reviewed["initial"]["value"]):
                return "incorrect"
            if not isinstance(plan["steps"],list) or len(plan["steps"])!=len(reviewed["steps"]):
                return "not_assessed"
            actual_maps,expected_maps = [],[]
            for actual,expected in zip(plan["steps"],reviewed["steps"]):
                if set(actual)!={"op","value","quote"}:
                    return "format_invalid"
                if type(actual["value"]) is not str:
                    return "format_invalid"
                if actual["op"] not in ("add","sub","mul","div","floordiv","increase_percent","decrease_percent","percent"):
                    return "format_invalid"
                actual_maps.append(_affine(actual))
                expected_maps.append(_affine(expected))
                if not grounded(actual["quote"],expected["quote"]):
                    return "not_assessed"
            if not grounded(plan["initial"]["quote"],reviewed["initial"]["quote"]):
                return "not_assessed"
            if actual_maps!=expected_maps:
                def apply(maps):
                    value=_number(reviewed["initial"]["value"])
                    for factor,offset in maps:value=factor*value+offset
                    return value
                # A different result proves an incorrect calculation. The same
                # result alone does NOT prove faithful semantics: do not blame
                # the model for an unassessed algebraic rewrite either.
                return "incorrect" if apply(actual_maps)!=apply(expected_maps) else "not_assessed"
            return "equivalent"
        if "relations" in reviewed:
            if set(plan)!=set(reviewed): return "format_invalid"
            if (not all(isinstance(plan[key],str) for key in ("entity","field","answer"))
                or not isinstance(plan["relations"],list)
                or any(relation not in ("mentor","employer") for relation in plan["relations"])):
                return "format_invalid"
            same = (isinstance(plan["entity"],str) and plan["entity"].casefold()==reviewed["entity"].casefold()
                    and all(plan[key]==reviewed[key] for key in ("relations","field","answer")))
            return "equivalent" if same else "incorrect"
        if "filters" in reviewed:
            if set(plan)!=set(reviewed): return "format_invalid"
            if plan["operation"] not in ("sum","count","min","max","avg"):
                return "format_invalid"
            if (not isinstance(plan["file"],str) or plan["column"] is not None and not isinstance(plan["column"],str)
                or not isinstance(plan["filters"],list) or len(plan["filters"])>8):
                return "format_invalid"
            for item in plan["filters"]:
                if (not isinstance(item,dict) or set(item)!={"column","op","type","value","quote"}
                    or not all(isinstance(item[key],str) for key in item)
                    or item["type"] not in ("string","number")
                    or item["op"] not in ("=","!=","<","<=",">",">=")):
                    return "format_invalid"
            def filters(items):
                return Counter((item["column"],item["op"],item["type"],
                                _number(item["value"]) if item["type"]=="number" else item["value"])
                               for item in items)
            same = (all(plan[key]==reviewed[key] for key in ("file","operation","column"))
                    and filters(plan["filters"])==filters(reviewed["filters"]))
            if same:
                for item in plan["filters"]:
                    cited=[other for other in reviewed["filters"] if filters([item])==filters([other])]
                    if not any(grounded(item.get("quote"),other["quote"]) for other in cited):
                        return "not_assessed"
            return "equivalent" if same else "incorrect"
    except (ValueError,TypeError,KeyError,ZeroDivisionError):
        return "not_assessed"
    return "not_assessed"


class Trace:
    def __init__(self, task_ids, *, reviewed=None, instructions=None, enabled=True, secret=""):
        self.task_ids = set(task_ids)
        self.reviewed = reviewed or {}
        self._instructions = instructions or {}
        self.enabled = enabled
        self._secret = secret
        self.events = []
        self.nodes = {key:{"sent":False,"transport_attempts":0,"independent":"not_assessed"} for key in task_ids}
        self._wire = {}  # Raw response objects never leave memory.
        self._candidate = {}
        self._active = False

    @contextmanager
    def scope(self, task_id, phase):
        task_id = task_id if task_id in self.task_ids else None
        token = _SCOPE.set((task_id,phase))
        try:
            yield
        finally:
            _SCOPE.reset(token)

    def emit(self, stage, status, **values):
        task_id,phase = _SCOPE.get()
        if task_id is None:
            return
        fields = {name:value for name,value in values.items() if name in _FIELDS}
        if any(type(v) not in (str,int,float,bool,type(None)) for v in fields.values()):
            raise TypeError("trace metadata must be scalar")
        event = {"task_id":task_id,"stage":stage,"status":status,"phase":phase,**fields}
        # Defence in depth. No arbitrary body/header/exception field is accepted.
        if self._secret:
            event = {k:v.replace(self._secret,"[redacted]") if isinstance(v,str) else v for k,v in event.items()}
        if self.enabled:
            self.events.append(event)

    def note_wire(self, data, *, complete, received, requested_model):
        task_id,_ = _SCOPE.get()
        if task_id is None: return
        node = self.nodes[task_id]
        meta = {"bytes_received":received,"body_complete":complete,"json_valid":False}
        node.update(meta)
        if complete and received<=MAX_RESPONSE_BYTES:
            try:
                raw = json.loads(data)
                if isinstance(raw,dict):
                    self._wire[task_id] = raw
                    meta["json_valid"] = True
                    meta["model_reported"] = "model" in raw
                    meta["model_matches"] = raw.get("model")==requested_model
                    usage = raw.get("usage")
                    meta["usage_present"] = isinstance(usage,dict)
                    if isinstance(usage,dict):
                        for key in ("prompt_tokens","completion_tokens","total_tokens"):
                            if type(usage.get(key)) is int and usage[key]>=0:
                                meta[key] = usage[key]
                        details = usage.get("completion_tokens_details")
                        reasoning = details.get("reasoning_tokens") if isinstance(details,dict) else usage.get("reasoning_tokens")
                        if type(reasoning) is int and reasoning>=0: meta["reasoning_tokens"] = reasoning
                        nested = details.get("reasoning_tokens") if isinstance(details,dict) else None
                        top = usage.get("reasoning_tokens")
                        meta["reasoning_conflict"] = type(nested) is int and type(top) is int and nested!=top
                    choices = raw.get("choices")
                    meta["choices_count"] = len(choices) if isinstance(choices,list) else None
                    if isinstance(choices,list) and len(choices)==1 and isinstance(choices[0],dict):
                        choice = choices[0]
                        reason = choice.get("finish_reason")
                        meta["finish_reason"] = reason if isinstance(reason,str) and reason in {"stop","length","tool_calls","content_filter"} else "other"
                        message = choice.get("message")
                        if isinstance(message,dict):
                            meta["assistant_role"] = message.get("role")=="assistant"
                            content = message.get("content")
                            meta["content_type"] = "string" if isinstance(content,str) else "array" if isinstance(content,list) else "null" if content is None else "other"
                            meta["tool_calls"] = bool(message.get("tool_calls"))
                            meta["reasoning_content_present"] = bool(message.get("reasoning_content"))
            except (ValueError,UnicodeError,RecursionError):
                pass
        node.update(meta)
        self.emit("model_response","body_observed",**meta)

    @contextmanager
    def observe(self, manager, *, fault=None, fault_task=None):
        if self._active:
            raise RuntimeError("diagnostic hooks cannot be nested")
        self._active = True
        original_invoke = Workflow.invoke
        original_prepare = llm_fallback.prepare
        original_json = llm_fallback.strict_json
        original_save = harness.save_answer
        original_finish = planner.Planner.finish
        def injected(kind):
            return fault==kind and (fault_task is None or _SCOPE.get()[0]==fault_task)

        async def invoke(workflow,inputs,*args,**kwargs):
            key = Path(inputs["task_dir"]).name
            phase = inputs.get("route") or "local"
            with self.scope(key,phase):
                if phase=="local": self.emit("local_attempt","started")
                result = await original_invoke(workflow,inputs,*args,**kwargs)
                output = result.result.get("output",{})
                if output.get("status")=="solved":
                    self._candidate[key] = output.get("answer")
                if phase=="local":
                    self.nodes[key]["local_status"] = output.get("status")
                    self.emit("local_attempt",output.get("status","error"))
                    if output.get("status")=="solved":
                        self.nodes[key]["execution"] = "success"
                        self.emit("execution","success",operation="local_solver")
                return result

        def prepare(task_type,instruction,task_dir=None):
            key = Path(task_dir).name if task_dir is not None else _SCOPE.get()[0]
            with self.scope(key,"eligibility"):
                try:
                    prepared = original_prepare(task_type,instruction,task_dir)
                except Exception as error:
                    self.nodes[key]["preparation"] = "blocked"
                    self.emit("llm_eligibility","context_rejected",error_code=exception_code(error))
                    raise
                self.nodes[key]["preparation"] = "ready"
                self.emit("llm_eligibility","context_ready")
            validate = prepared.validate
            def observed_validate(plan):
                self.emit("semantic_validation","started",independent=self.nodes[key]["independent"])
                try:
                    answer = validate(plan)
                except Exception as error:
                    if not self.nodes[key].get("execution_started"):
                        self.nodes[key]["validation"] = "rejected"
                        reasons = {"unexpected plan structure":"plan_structure",
                                   "invalid numeric operand":"numeric_operand",
                                   "initial quantity differs from instruction":"initial_quantity_or_evidence",
                                   "arithmetic plan changes the step count":"step_count",
                                   "arithmetic operation, operand order or evidence differs":"operation_order_or_evidence"}
                        reason = reasons.get(str(error),"unclassified_validation_failure")
                        self.nodes[key]["validation_reason"] = reason
                        self.emit("semantic_validation","rejected",error_code=exception_code(error),
                                  reason_code=reason,independent=self.nodes[key]["independent"])
                    raise
                self.nodes[key]["validation"] = "accepted"
                if task_type=="search":
                    # Search's validator resolves facts inline; there is no
                    # separate executor call. Do not invent that boundary.
                    self.nodes[key]["execution"] = "success"
                    self.emit("semantic_validation","accepted",operation="search_inline")
                    self.emit("execution","success",operation="combined_with_search_validation")
                return answer
            prepared.validate = observed_validate
            return prepared

        def parse(content):
            key,_ = _SCOPE.get()
            try:
                plan = original_json(content)
            except LLMError:
                self.nodes[key]["plan_format"] = "invalid"
                self.emit("parsed_plan","rejected",error_code="json_invalid")
                raise
            verdict = assess_plan(plan,self.reviewed.get(key),self._instructions.get(key))
            self.nodes[key].update(plan_format="parsed",independent=verdict)
            if self.reviewed.get(key,{}).get("initial") is not None:
                shape,supported = math_plan_shape(plan)
                self.nodes[key].update(plan_schema_valid=shape,operations_supported=supported)
            known = {"initial","steps","entity","relations","field","answer","file","operation","column","filters","status"}
            self.emit("parsed_plan","parsed",independent=verdict,
                      unknown_fields=len(set(plan)-known) if isinstance(plan,dict) else 0,
                      steps_count=len(plan["steps"]) if isinstance(plan,dict) and isinstance(plan.get("steps"),list) else None)
            return plan

        def executor(original,operation):
            def run(*args,**kwargs):
                key,_ = _SCOPE.get()
                if key is None: return original(*args,**kwargs)
                self.nodes[key].update(validation="accepted",execution_started=True)
                self.emit("semantic_validation","accepted")
                self.emit("execution","started",operation=operation,fault_injected=injected("execution_error"))
                try:
                    if injected("execution_error"): raise UnsupportedMeaning("controlled executor failure")
                    answer = original(*args,**kwargs)
                except Exception as error:
                    self.nodes[key]["execution"] = "error"
                    self.emit("execution","error",error_code=exception_code(error),fault_injected=injected("execution_error"))
                    raise
                self.nodes[key]["execution"] = "success"
                self.emit("execution","success",operation=operation)
                return answer
            return run

        def save(directory,answer):
            key = Path(directory).name
            with self.scope(key,"publication"):
                try:
                    if injected("save_error"): raise OSError("controlled publication failure")
                    original_save(directory,answer+" extra" if injected("format_error") else answer)
                except Exception as error:
                    self.nodes[key]["publication"] = "error"
                    self.emit("answer.txt","error",error_code=exception_code(error),fault_injected=injected("save_error"))
                    raise
                self.nodes[key]["publication"] = "saved"
                self.emit("answer.txt","saved",mode=stat.S_IMODE((directory/"answer.txt").stat().st_mode),
                          size=(directory/"answer.txt").stat().st_size,fault_injected=injected("format_error"))

        def finish(instance,decision,status,reason=""):
            original_finish(instance,decision,status,reason)
            key = decision.entry.task.directory.name
            self.nodes[key]["state"] = decision.entry.state
            if decision.phase=="local":
                with self.scope(key,"eligibility"):
                    self.emit("llm_eligibility",decision.entry.state)

        reserve,settle,uncertain = manager.reserve,manager.settle,manager.mark_uncertain
        def observed_reserve(*args,**kwargs):
            try:
                record = reserve(*args,**kwargs)
            except BudgetError:
                self.emit("budget_reservation","denied",error_code="budget_denied")
                raise
            self.emit("budget_reservation","reserved",estimated_input=record.estimated_input,
                      max_tokens=record.max_output,reserved=str(Fraction(record.reserved_tenths,10)))
            key,_ = _SCOPE.get()
            if key is not None: self.nodes[key]["estimated_input"] = record.estimated_input
            return record
        def observed_settle(*args,**kwargs):
            record = settle(*args,**kwargs)
            key,_ = _SCOPE.get()
            if key is not None: self.nodes[key]["usage_state"] = "confirmed"
            self.emit("usage","confirmed",prompt_tokens=record.input_tokens,accounted_output_tokens=record.output_tokens,
                      spent=str(Fraction(record.spent_tenths,10)),frozen=manager.snapshot()["frozen"])
            return record
        def observed_uncertain(*args,**kwargs):
            record = uncertain(*args,**kwargs)
            key,_ = _SCOPE.get()
            if key is not None and record.state=="uncertain": self.nodes[key]["usage_state"] = "unknown"
            if record.state=="uncertain": self.emit("usage","unknown",reserved=str(Fraction(record.reserved_tenths,10)),frozen=True)
            return record
        try:
            with ExitStack() as stack:
                for target,name,value in (
                    (Workflow,"invoke",invoke),(llm_fallback,"prepare",prepare),(planner,"prepare",prepare),
                    (llm_fallback,"strict_json",parse),(harness,"save_answer",save),(planner.Planner,"finish",finish),
                    (llm_fallback,"calculate_math",executor(llm_fallback.calculate_math,"math")),
                    (data_solver,"solve_data",executor(data_solver.solve_data,"csv")),
                    (manager,"reserve",observed_reserve),(manager,"settle",observed_settle),
                    (manager,"mark_uncertain",observed_uncertain)):
                    stack.enter_context(patch.object(target,name,value))
                yield
        finally:
            self._active = False


class TapStream(httpx.AsyncByteStream):
    def __init__(self, original, trace, model):
        self.original,self.trace,self.model = original,trace,model
        self.data = bytearray()
        self.received,self.complete,self.noted = 0,False,False

    async def __aiter__(self):
        try:
            async for chunk in self.original:
                self.received += len(chunk)
                self.data.extend(chunk[:max(0,MAX_RESPONSE_BYTES-len(self.data))])
                yield chunk
            self.complete = True
        except BaseException as error:
            key,_ = _SCOPE.get()
            if key is not None: self.trace.nodes[key]["transport_error"] = exception_code(error)
            self.trace.emit("model_response","stream_error",error_code=exception_code(error))
            raise
        finally:
            self.note()

    def note(self):
        if not self.noted:
            self.noted = True
            self.trace.note_wire(bytes(self.data),complete=self.complete,received=self.received,requested_model=self.model)

    async def aclose(self):
        try:
            await self.original.aclose()
        finally:
            self.note()


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, original, trace, model):
        self.original,self.trace,self.model = original,trace,model

    async def handle_async_request(self, request):
        key,_ = _SCOPE.get()
        node = self.trace.nodes[key]
        node.update(sent=None,transport_attempts=node["transport_attempts"]+1)
        self.trace.emit("request","transport_started",requested_model=self.model,method="POST",sent=None)
        try:
            response = await self.original.handle_async_request(request)
        except BaseException as error:
            node["transport_error"] = exception_code(error)
            self.trace.emit("model_response","transport_error",error_code=node["transport_error"],sent=None)
            raise
        node.update(sent=True,http_status=response.status_code)
        self.trace.emit("model_response","headers_received",http_status=response.status_code,sent=True)
        return httpx.Response(response.status_code,headers=response.headers,
                              stream=TapStream(response.stream,self.trace,self.model),extensions=response.extensions,
                              request=request)

    async def aclose(self):
        await self.original.aclose()


class DiagnosticGateway(LLMGateway):
    """Same complete() and manager; adds observers and an explicit request cap."""
    def __init__(self,*args,trace,transport_factory,max_calls,strict_response=False,**kwargs):
        super().__init__(*args,**kwargs)
        self.trace,self.factory,self.max_calls = trace,transport_factory,max_calls
        self.strict_response = strict_response
        self.attempts = 0

    async def complete(self,system,payload,*,task_id="direct"):
        key = Path(task_id).name
        phase = "probe" if key.startswith("a1_") else "fallback"
        with self.trace.scope(key,phase):
            if self.attempts>=self.max_calls:
                self.trace.nodes[key]["request_cap"] = True
                self.trace.emit("budget_reservation","request_cap",sent=False)
                raise LLMError("diagnostic request cap reached")
            self.attempts += 1
            self._transport = RecordingTransport(self.factory(),self.trace,self.model)
            try:
                result = await super().complete(system,payload,task_id=task_id)
                node = self.trace.nodes[key]
                if self.strict_response and ((node.get("model_reported") and not node.get("model_matches")) or node.get("reasoning_content_present")):
                    node["response_contract_rejected"] = True
                    raise LLMError("diagnostic model or reasoning metadata mismatch")
                return result
            except LLMError:
                if self.trace.nodes[key].get("transport_error")=="cancelled_or_timeout":
                    self.trace.nodes[key]["transport_error"] = "timeout"
                self.trace.nodes[key]["gateway_error"] = True
                self.trace.emit("model_response","gateway_rejected")
                raise


def classify(node, *, grade, answerable, candidate_correct=False):
    if node.get("response_contract_rejected"): return "api_or_budget","model_or_reasoning_contract"
    if node.get("publication")=="error": return "answer_publication","save_failed"
    if grade=="wrong":
        if candidate_correct: return "answer_publication","format_or_written_value"
        if node.get("independent")=="equivalent": return "execution","incorrect_result"
        return "answer_incorrect","not_attributed_to_model_without_evidence"
    if grade=="correct": return "success","local" if not node["transport_attempts"] else "llm"
    if not answerable: return "appropriate_skip","no_reliable_answer"
    status = node.get("http_status")
    if status is not None and status!=200: return "api_or_budget",f"http_{status}"
    if node.get("transport_error"):
        return "api_or_budget","partial_response" if node.get("bytes_received",0)>0 else node["transport_error"]
    if node.get("gateway_error"):
        if node.get("finish_reason")=="length": return "response_format","generation_truncated"
        if node.get("json_valid") and not node.get("usage_present"): return "api_or_budget","usage_missing"
        if node.get("body_complete") and not node.get("json_valid"): return "response_format","envelope_json_invalid"
        if node.get("reasoning_conflict"): return "api_or_budget","reasoning_statistics_conflict"
        if node.get("usage_state")=="unknown" and node.get("reasoning_tokens",0)>0:
            return "api_or_budget","reasoning_contract_unverified"
        if node.get("usage_present") and ("prompt_tokens" not in node or "completion_tokens" not in node):
            return "api_or_budget","usage_incomplete"
        if node.get("json_valid") and node.get("content_type") not in (None,"string"):
            return "response_format","unsupported_content_type"
        if node.get("sent"): return "api_or_budget","protocol_or_usage_rejected"
        return "api_or_budget","budget_or_generation_contract"
    if node.get("execution")=="error": return "execution","executor_failed"
    if node.get("plan_format")=="invalid": return "response_format","plan_json_invalid"
    independent = node.get("independent")
    if node.get("validation")=="rejected":
        if independent=="equivalent": return "validator_rejection","equivalent_plan_rejected"
        if independent=="incorrect": return "model_meaning","incorrect_plan"
        if independent=="format_invalid": return "response_format","plan_schema"
        return "validation_unassessed","cannot_assign_blame"
    if independent=="abstained": return "model_meaning","model_abstained"
    if node.get("state")=="budget_blocked" or node.get("request_cap"):
        return "api_or_budget","budget_blocked"
    return "routing","context_or_local_evidence_gate"
