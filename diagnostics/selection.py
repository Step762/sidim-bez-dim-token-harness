"""Development-only selection and preflight; never creates an HTTP client."""

from decimal import Decimal
import json
from pathlib import Path

import harness  # Configure SDK logging before gateway construction.
from benchmark.evaluate import DATASET, load_dataset, runtime_fingerprint
from budget_manager import BudgetManager
from llm_fallback import prepare
from llm_gateway import DEFAULT_MODEL, LLMGateway


CHOICES = {
    "math_d01": "Дешёвый локальный контроль; LLM не должен вызываться.",
    "math_d08": "Нулевая награда, степень словами: неподдерживаемая операция.",
    "math_d09": "Явное round-half-up; модель потенциально понимает, грамматика нет.",
    "math_d15": "Локальный отказ, проверяемая арифметика; также эквивалентный план add(-8).",
    "search_d01": "Прямой факт из собственной Markdown-базы.",
    "search_d05": "Цепочка Mentor → Employer → Headquarters в разных документах.",
    "search_d14": "Непротиворечивые части профиля, отклонённые индексом как конфликт.",
    "search_d15": "Непривычная формулировка и цепочка связей, доступные fallback.",
    "data_analysis_d06": "CSV с несколькими фильтрами и включёнными границами.",
    "data_analysis_d11": "Повреждённое число: разумный отказ без запроса модели.",
    "data_analysis_d12": "Группировка: необходимой операции нет в текущей схеме.",
    "data_analysis_d14": "Сложная словесная фильтрация CSV, доступная fallback.",
}

A1_PROBES = (
    {"id":"a1_ack", "system":"Reply with one JSON object containing only ok set to true.",
     "payload":{"probe":"ack"}, "max_tokens":32},
    {"id":"a1_math", "system":"Return one JSON object with a string field value containing the sum requested by the user. No other fields or text.",
     "payload":{"question":"What is 2 + 3?"}, "max_tokens":64},
    {"id":"a1_limit", "system":"Return one JSON object with an array field values containing every integer from 1 through 80 in order. No other text.",
     "payload":{"probe":"bounded_output"}, "max_tokens":32},
)

A1_BUDGET = 600
A2_BUDGET = 2500
A2_MAX_TOKENS = 512
A2_MAX_CALLS = 3


def load_selection():
    _, references = load_dataset(DATASET)
    baseline = json.loads((DATASET.parent/"results/baseline/A-local.json").read_text())
    previous = {row["task_id"]:row for row in baseline["rows"]}
    selected = {}
    for task_id,reason in CHOICES.items():
        ref = references[task_id]
        if ref["split"]!="development" or ref["task_type"]=="coding":
            raise ValueError("diagnostic selection must be development and exclude coding")
        task = json.loads((DATASET/"tasks"/task_id/"task.json").read_text())
        selected[task_id] = {"task":task,"reference":ref,"reason":reason,
                             "local_status":previous[task_id]["status"],
                             "local_outcome":previous[task_id]["outcome"],
                             "local_answer":previous[task_id]["actual"]}
    return selected


def experiment_plan():
    # Synthetic endpoint/key are never used for complete(). No environment key
    # is read during preflight. estimate_input is a local UTF-8 heuristic.
    gateway = LLMGateway("http://127.0.0.1:1/v1", "preflight-not-a-credential",
                         manager=BudgetManager(100000), max_tokens=A2_MAX_TOKENS)
    selection = load_selection()
    rows = []
    for task_id,case in selection.items():
        task = case["task"]
        row = {"task_id":task_id,"task_type":task["task_type"],"reward":task["reward"],
               "reason":case["reason"],"expected":case["reference"]["answer"],
               "local_status":case["local_status"],"local_answer":case["local_answer"],
               "local_outcome":case["local_outcome"],"context_supported":False,
               "natural_llm_candidate":False,"estimated_input":None,"reservation":None}
        try:
            prepared = prepare(task["task_type"],task["instruction"],DATASET/"tasks"/task_id)
            estimated = gateway.estimate_input(prepared.system,prepared.payload)
            row.update(context_supported=True,estimated_input=estimated,
                       reservation=str(Decimal(estimated)/10+A2_MAX_TOKENS))
            row["natural_llm_candidate"] = case["local_status"]!="correct" and case["local_outcome"]=="budget_blocked"
        except (ValueError,OSError,UnicodeError):
            pass
        rows.append(row)
    a1 = []
    for probe in A1_PROBES:
        estimate = gateway.estimate_input(probe["system"],probe["payload"])
        a1.append({"id":probe["id"],"estimated_input":estimate,"max_tokens":probe["max_tokens"],
                   "reservation":str(Decimal(estimate)/10+probe["max_tokens"])})
    a1_sum = sum(Decimal(row["reservation"]) for row in a1)
    a2_sum = sum(Decimal(row["reservation"]) for row in rows if row["natural_llm_candidate"])
    if a1_sum>A1_BUDGET or a2_sum>A2_BUDGET or sum(row["natural_llm_candidate"] for row in rows)>A2_MAX_CALLS:
        raise ValueError("review the experiment caps before sending any requests")
    return {"model":DEFAULT_MODEL,"endpoint_assumption":"https://api.neuraldeep.ru/v1/chat/completions",
            "configuration":"LLM_BASE_URL and LLM_API_KEY only; not read by offline preflight",
            "runtime_sha256":runtime_fingerprint(),"selection":rows,
            "A1":{"budget":A1_BUDGET,"max_requests":3,"probes":a1,"planned_reservation":str(a1_sum)},
            "A2":{"budget":A2_BUDGET,"max_requests":A2_MAX_CALLS,"tasks":len(rows),
                  "max_tokens":A2_MAX_TOKENS,"planned_reservation":str(a2_sum),"requires_successful_live_A1":True},
            "limits_note":"Reservations use a heuristic, not a proven tokenizer bound. max_tokens enforcement and remote quota remain unverified. No absolute remote cost guarantee."}
