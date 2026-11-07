"""Run the real workflow with timing observers and no reference-answer input."""

import argparse
import asyncio
from dataclasses import asdict, is_dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlparse


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(type(value).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--budget", type=int, default=0)
    parser.add_argument("--strategy", choices=("priority", "original", "reward"), default="priority")
    parser.add_argument("--endpoint")
    parser.add_argument("--llm-timeout", type=float, default=2.0)
    args = parser.parse_args()
    if args.budget < 0:
        parser.error("budget must be nonnegative")
    if args.endpoint:
        url = urlparse(args.endpoint)
        if url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password:
            parser.error("benchmark worker accepts only the local mock proxy")
    os.environ["BUDGET"] = str(args.budget)
    sys.path.insert(0, str(args.runtime_dir))
    from budget_manager import BudgetManager
    import harness
    from llm_gateway import LLMGateway
    from openjiuwen.core.workflow import Workflow

    manager = BudgetManager(args.budget)
    gateway = LLMGateway(args.endpoint, "benchmark-local-only", manager=manager,
                         max_tokens=256, timeout=args.llm_timeout) if args.endpoint else None
    observations = {}
    original_invoke = Workflow.invoke

    async def observed_invoke(self, inputs, *positional, **keywords):
        started = time.perf_counter()
        result = None
        try:
            result = await original_invoke(self, inputs, *positional, **keywords)
            return result
        finally:
            task_id = Path(inputs["task_dir"]).name
            output = result.result.get("output", {}) if result is not None else {}
            observations.setdefault(task_id, []).append({
                "phase": inputs.get("route"), "seconds": time.perf_counter()-started,
                "status": output.get("status", "error"), "reason": output.get("reason", "workflow interrupted"),
            })

    # Measurement only: the original method is called with identical arguments.
    # There is no solver substitution, task execution bypass or trusted-code flag.
    Workflow.invoke = observed_invoke
    report = {}
    started = time.perf_counter()
    try:
        counts = asyncio.run(harness.run_tasks(args.tasks_dir, manager=manager, llm=gateway,
                                               budget=args.budget, strategy=args.strategy, report=report))
    finally:
        Workflow.invoke = original_invoke
    result = {"counts":counts,"agent_seconds":time.perf_counter()-started,
              "observations":observations,"planner":report,"trusted_code":False}
    args.result.write_text(json.dumps(result,default=json_value,sort_keys=True,indent=2)+"\n")


if __name__ == "__main__":
    main()
