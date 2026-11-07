"""Task loading, openJiuwen execution and immediate answer persistence."""

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile

from openjiuwen.core.common.logging.log_config import configure_log_config

# The competition mounts /app read-only. Configure logging before workflow imports.
configure_log_config({
    "level": "WARNING",
    "output": ["console"],
    "interface_output": ["console"],
    "performance_output": ["console"],
    "log_path": tempfile.gettempdir(),
    # Native SDK error logs include full messages. Budget denials must not leak them.
    "loggers": {"llm": {"level": "CRITICAL"}},
})

from openjiuwen.core.workflow import (
    End,
    Start,
    Workflow,
    WorkflowComponent,
    WorkflowExecutionState,
    create_workflow_session,
)

from math_solver import UnsupportedMath, solve_math
from search_solver import UnsupportedSearch, solve_search
from data_solver import UnsupportedData, solve_data
from coding_solver import UnsupportedCoding, solve_coding
from llm_gateway import LLMError
from llm_gateway import install_openjiuwen_budget_guard
from budget_manager import BudgetManager
from llm_fallback import InvalidPlan, solve_fallback
from planner import Planner, local_outcome
import csv


@dataclass(frozen=True)
class Task:
    directory: Path
    task_id: str
    task_type: str
    instruction: str
    reward: int | float


def load_task(path: Path) -> Task:
    """Select only task inputs; reference answers are never part of a Task."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("task.json must contain an object")
    for field in ("task_type", "instruction"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"{field} must be a nonempty string")
    reward = data.get("reward")
    if (
        isinstance(reward, bool)
        or not isinstance(reward, (int, float))
        or not math.isfinite(reward)
        or reward < 0
    ):
        raise ValueError("reward must be a finite nonnegative number")
    task_id = data.get("task_id", path.parent.name)
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id must be a nonempty string")
    return Task(path.parent, task_id, data["task_type"], data["instruction"], reward)


# A later stage can add another local solver or a model-backed route here.
SOLVERS = {
    "math": solve_math, "search": solve_search,
    "data_analysis": solve_data, "coding": solve_coding,
}


class SolveTask(WorkflowComponent):
    async def invoke(self, inputs, session, context):
        if inputs.get("route") == "fallback":
            return {"status": "skipped", "answer": "", "reason": inputs.get("local_reason") or "unsupported local formulation"}
        solver = SOLVERS.get(inputs["task_type"])
        if solver is None:
            return {"status": "skipped", "answer": "", "reason": "unsupported task type"}
        try:
            if inputs["task_type"] == "coding" and inputs.get("trusted_code") is not True:
                raise UnsupportedCoding("coding execution requires --trusted-code for trusted copies")
            if inputs["task_type"] in {"search", "data_analysis", "coding"}:
                if not inputs.get("task_dir"):
                    return {"status": "skipped", "answer": "", "reason": "missing task directory"}
                if inputs["task_type"] == "coding":
                    answer = solver(inputs["instruction"], Path(inputs["task_dir"]), trusted=True)
                else:
                    answer = solver(inputs["instruction"], Path(inputs["task_dir"]))
            else:
                answer = solver(inputs["instruction"])
        except (UnsupportedMath, UnsupportedSearch, UnsupportedData, UnsupportedCoding) as exc:
            return {"status": "skipped", "answer": "", "reason": str(exc)}
        return {"status": "solved", "answer": str(answer), "reason": ""}


class FallbackTask(WorkflowComponent):
    def __init__(self, gateway=None, budget=None):
        super().__init__()
        self.gateway = gateway
        self.budget = budget if budget is not None else getattr(gateway, "budget", None)

    async def invoke(self, inputs, session, context):
        result = {key: inputs[key] for key in ("status", "answer", "reason")}
        if (
            result["status"] != "skipped" or self.gateway is None
            or inputs.get("route") == "local"
            or type(self.budget) is not int or self.budget <= 0 or not self.gateway.available
        ):
            return result
        # A code proposal cannot become a solution without isolated verification.
        if inputs["task_type"] not in {"math", "search", "data_analysis"}:
            return result
        if local_outcome(result["reason"]) != "unsupported":
            return result
        task_dir = Path(inputs["task_dir"]) if inputs.get("task_dir") else None
        if task_dir is not None and (task_dir / "answer.txt").exists():
            return result
        try:
            answer = await solve_fallback(
                self.gateway, inputs["task_type"], inputs["instruction"], task_dir,
                task_id=inputs.get("task_id") or str(task_dir or "workflow"),
            )
            return {"status": "solved", "answer": answer, "reason": ""}
        except LLMError as exc:
            return {"status": "skipped", "answer": "", "reason": str(exc)}
        except (
            InvalidPlan, UnsupportedSearch, UnsupportedData, OSError, UnicodeError,
            csv.Error, TypeError, KeyError, ValueError, RecursionError, ZeroDivisionError,
        ):
            return {"status": "skipped", "answer": "", "reason": "LLM plan or local evidence could not be validated"}


def build_workflow(*, llm=None, budget=None, manager=None) -> Workflow:
    """Local solving precedes the optional, bounded LLM translation node."""
    if manager is not None and llm is not None and manager is not llm.manager:
        raise ValueError("workflow and gateway must share one BudgetManager")
    install_openjiuwen_budget_guard()
    workflow = Workflow()
    workflow.set_start_comp(
        "start", Start(),
        inputs_schema={
            "task_type": "${task_type}", "instruction": "${instruction}",
            "task_dir": "${task_dir}",
            "task_id": "${task_id}",
            "trusted_code": "${trusted_code}",
            "route": "${route}", "local_reason": "${local_reason}",
        },
    )
    workflow.add_workflow_comp(
        "solve", SolveTask(),
        inputs_schema={
            "task_type": "${start.task_type}",
            "instruction": "${start.instruction}",
            "task_dir": "${start.task_dir}",
            "trusted_code": "${start.trusted_code}",
            "route": "${start.route}", "local_reason": "${start.local_reason}",
        },
    )
    workflow.set_end_comp(
        "end", End(),
        inputs_schema={
            "status": "${fallback.status}",
            "answer": "${fallback.answer}",
            "reason": "${fallback.reason}",
        },
    )
    workflow.add_workflow_comp(
        "fallback", FallbackTask(llm, budget),
        inputs_schema={
            "status": "${solve.status}", "answer": "${solve.answer}", "reason": "${solve.reason}",
            "task_type": "${start.task_type}", "instruction": "${start.instruction}",
            "task_dir": "${start.task_dir}",
            "task_id": "${start.task_id}",
            "route": "${start.route}",
        },
    )
    workflow.add_connection("start", "solve")
    workflow.add_connection("solve", "fallback")
    workflow.add_connection("fallback", "end")
    return workflow


def save_answer(directory: Path, answer: str) -> None:
    """Atomically publish an answer beside task.json, never in workspace/."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix=".answer-", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(answer + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / "answer.txt")
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


async def run_tasks(
    tasks_dir: Path, *, allow_trusted_code: bool = False, llm=None, budget=None,
    manager=None,
    strategy="priority", report=None,
) -> dict[str, int]:
    """Schedule local and fallback passes through the same openJiuwen workflow."""
    if not tasks_dir.is_dir():
        raise ValueError(f"tasks directory does not exist: {tasks_dir}")
    manager = manager if manager is not None else (llm.manager if llm is not None else BudgetManager(0))
    workflow = build_workflow(llm=llm, budget=budget, manager=manager)
    tasks = []
    malformed = 0
    for path in sorted(tasks_dir.glob("*/task.json")):
        try:
            tasks.append(load_task(path))
        except Exception as exc:
            malformed += 1
            print(f"{path.parent.name}: failed: {exc}", flush=True)
    planner = Planner(tasks, manager, llm, strategy=strategy, trusted_code=allow_trusted_code,
                      llm_enabled=budget is None or (type(budget) is int and budget > 0))
    printed = set()
    while (decision := planner.next()) is not None:
        task = decision.entry.task
        try:
            output = await workflow.invoke(
                {
                    "task_type": task.task_type, "instruction": task.instruction,
                    "task_dir": str(task.directory),
                    "task_id": str(task.directory.resolve()),
                    "trusted_code": allow_trusted_code,
                    "route": decision.phase, "local_reason": decision.entry.reason,
                },
                create_workflow_session(),
            )
            if output.state != WorkflowExecutionState.COMPLETED:
                raise RuntimeError(f"workflow did not complete: {output.state}")
            result = output.result["output"]
            status = result["status"]
            if status == "solved":
                save_answer(task.directory, result["answer"])
            elif status != "skipped":
                raise RuntimeError(f"unexpected solver status: {status}")
            planner.finish(decision, status, result["reason"])
            if status == "solved":
                print(f"{task.task_id}: solved: {result['answer']}", flush=True)
                printed.add(decision.entry.key)
        except Exception as exc:
            if not decision.entry.terminal:
                planner.finish(decision, "failed", str(exc))
            print(f"{task.task_id}: failed: {exc}", flush=True)
            printed.add(decision.entry.key)
    for entry in planner.entries:
        if entry.key not in printed:
            print(f"{entry.task.task_id}: skipped [{entry.state}]: {entry.reason}", flush=True)
    if report is not None:
        report.update(planner.report())
    return {
        "solved": sum(entry.state == "solved" for entry in planner.entries),
        "skipped": sum(entry.state not in {"solved", "failed"} for entry in planner.entries),
        "failed": malformed + sum(entry.state == "failed" for entry in planner.entries),
    }
