"""Task loading, openJiuwen execution and immediate answer persistence."""

from dataclasses import dataclass
import asyncio
import json
import math
import os
from pathlib import Path
import secrets
import stat
import tempfile

from safe_files import TASK_JSON_BYTES, directory_guards, identity, open_directory, read_text

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
from coding_static import solve_static_coding
from llm_gateway import LLMError
from llm_gateway import install_openjiuwen_budget_guard
from budget_manager import BudgetManager
from llm_fallback import InvalidPlan, solve_fallback
from planner import Planner, local_outcome
from runtime_limits import RuntimeLimits, OperationTimeout
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
    path = path.absolute()
    with open_directory(path.parent) as directory:
        directory_identity = identity(os.fstat(directory))
    with directory_guards({path.parent: directory_identity}):
        data = json.loads(read_text(path.parent, path.name, maximum=TASK_JSON_BYTES))
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
    def __init__(self, limits, *, static_coding=False):
        super().__init__()
        self.limits = limits
        self.static_coding = static_coding is True

    async def invoke(self, inputs, session, context):
        if inputs.get("route") == "fallback":
            return {"status": "skipped", "answer": "", "reason": inputs.get("local_reason") or "unsupported local formulation"}
        solver = SOLVERS.get(inputs["task_type"])
        if solver is None:
            return {"status": "skipped", "answer": "", "reason": "unsupported task type"}
        try:
            if inputs["task_type"] == "coding" and not self.static_coding and inputs.get("trusted_code") is not True:
                raise UnsupportedCoding("coding execution requires --trusted-code for trusted copies")
            with self.limits.sync():
                if inputs["task_type"] in {"search", "data_analysis", "coding"}:
                    if not inputs.get("task_dir"):
                        return {"status": "skipped", "answer": "", "reason": "missing task directory"}
                    if inputs["task_type"] == "coding":
                        if self.static_coding:
                            answer = solve_static_coding(inputs["instruction"], Path(inputs["task_dir"]))
                        else:
                            answer = solver(inputs["instruction"], Path(inputs["task_dir"]), trusted=True)
                    else:
                        answer = solver(inputs["instruction"], Path(inputs["task_dir"]))
                else:
                    answer = solver(inputs["instruction"])
        except OperationTimeout:
            return {"status":"skipped","answer":"","reason":"local operation time limit reached"}
        except (UnsupportedMath, UnsupportedSearch, UnsupportedData, UnsupportedCoding) as exc:
            return {"status": "skipped", "answer": "", "reason": str(exc)}
        return {"status": "solved", "answer": str(answer), "reason": ""}


class FallbackTask(WorkflowComponent):
    def __init__(self, gateway=None, budget=None, limits=None):
        super().__init__()
        self.gateway = gateway
        self.limits = limits
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
                limits=self.limits,
            )
            return {"status": "solved", "answer": answer, "reason": ""}
        except OperationTimeout:
            return {"status": "skipped", "answer": "", "reason": "local operation time limit reached"}
        except LLMError as exc:
            return {"status": "skipped", "answer": "", "reason": str(exc)}
        except (
            InvalidPlan, UnsupportedSearch, UnsupportedData, OSError, UnicodeError,
            csv.Error, TypeError, KeyError, ValueError, RecursionError, ZeroDivisionError,
        ):
            return {"status": "skipped", "answer": "", "reason": "LLM plan or local evidence could not be validated"}


def build_workflow(*, llm=None, budget=None, manager=None, limits=None, static_coding=False) -> Workflow:
    """Local solving precedes the optional, bounded LLM translation node."""
    if manager is not None and llm is not None and manager is not llm.manager:
        raise ValueError("workflow and gateway must share one BudgetManager")
    install_openjiuwen_budget_guard()
    limits = limits or RuntimeLimits()
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
        "solve", SolveTask(limits, static_coding=static_coding),
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
        "fallback", FallbackTask(llm, budget, limits),
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
    with open_directory(directory) as parent:
        temporary = f".answer-{secrets.token_hex(16)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(answer + "\n")
                output.flush()
                os.fchmod(output.fileno(), 0o644)
                os.fsync(output.fileno())
            # Refuse publication if the pathname was replaced during the write.
            with open_directory(directory) as current:
                if identity(os.fstat(current)) != identity(os.fstat(parent)):
                    raise ValueError("answer directory was replaced")
            os.replace(temporary, "answer.txt", src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


async def run_tasks(
    tasks_dir: Path, *, allow_trusted_code: bool = False, allow_static_coding: bool = False, llm=None, budget=None,
    manager=None,
    strategy="priority", report=None,
    limits=None,
) -> dict[str, int]:
    """Schedule local and fallback passes through the same openJiuwen workflow."""
    if not tasks_dir.is_dir():
        raise ValueError(f"tasks directory does not exist: {tasks_dir}")
    manager = manager if manager is not None else (llm.manager if llm is not None else BudgetManager(0))
    limits = limits or RuntimeLimits()
    workflow = build_workflow(llm=llm, budget=budget, manager=manager, limits=limits,
                              static_coding=allow_static_coding)
    tasks_dir = tasks_dir.resolve(strict=True)
    with open_directory(tasks_dir) as root:
        with directory_guards({tasks_dir: identity(os.fstat(root))}) as guards:
            tasks, malformed, seen = [], 0, set()
            try:
                with limits.sync(): names = sorted(os.listdir(root))
            except OperationTimeout:
                names = []
            for name in names:
                if limits.expired: break
                directory = tasks_dir / name
                try:
                    with limits.sync():
                        info = os.stat(name, dir_fd=root, follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            raise ValueError("symbolic task directory links are unsupported")
                        if not stat.S_ISDIR(info.st_mode):
                            continue
                        with open_directory(directory) as child:
                            try:
                                os.stat("task.json", dir_fd=child, follow_symlinks=False)
                            except FileNotFoundError:
                                continue
                            key = identity(os.fstat(child))
                            if key in seen:
                                raise ValueError("duplicate task directory")
                            guards[directory] = key
                            task = load_task(directory / "task.json")
                    seen.add(key)
                    tasks.append(task)
                except Exception as exc:
                    malformed += 1
                    print(f"{name}: failed: {exc}", flush=True)
            return await _execute_tasks(tasks, malformed, workflow, allow_trusted_code,
                                        llm, budget, manager, strategy, report, limits,
                                        static_coding=allow_static_coding)


async def _execute_tasks(tasks, malformed, workflow, allow_trusted_code,
                         llm, budget, manager, strategy, report, limits, *, static_coding=False):
    planner = Planner(tasks, manager, llm, strategy=strategy, trusted_code=allow_trusted_code,
                      static_coding=static_coding,
                      llm_enabled=budget is None or (type(budget) is int and budget > 0),limits=limits)
    printed = set()
    while not limits.expired:
        try:
            with limits.sync(): decision = planner.next()
        except OperationTimeout:
            break
        if decision is None: break
        task = decision.entry.task
        try:
            with open_directory(task.directory):
                pass  # Guards reject replacement since discovery, before any solver runs.
            async with asyncio.timeout(min(limits.operation,limits.remaining)):
                output = await workflow.invoke(
                {
                    "task_type": task.task_type, "instruction": task.instruction,
                    "task_dir": str(task.directory),
                    "task_id": decision.entry.key,
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
                with limits.sync(): save_answer(task.directory, result["answer"])
            elif status != "skipped":
                raise RuntimeError(f"unexpected solver status: {status}")
            with limits.sync(): planner.finish(decision, status, result["reason"])
            if status == "solved":
                print(f"{task.task_id}: solved: {result['answer']}", flush=True)
                printed.add(decision.entry.key)
        except (OperationTimeout,TimeoutError):
            if decision.entry.state!="solved":
                decision.entry.state = "timed_out"
                decision.entry.reason = "operation or run deadline reached"
            print(f"{task.task_id}: skipped [timed_out]",flush=True)
            printed.add(decision.entry.key)
        except Exception as exc:
            if not decision.entry.terminal:
                planner.finish(decision, "failed", str(exc))
            print(f"{task.task_id}: failed: {exc}", flush=True)
            printed.add(decision.entry.key)
    for entry in planner.entries:
        if not entry.terminal:
            entry.state = "time_exhausted"
            entry.reason = "run deadline reached"
        if entry.key not in printed:
            print(f"{entry.task.task_id}: skipped [{entry.state}]: {entry.reason}", flush=True)
    if report is not None:
        report.update(planner.report())
        report["time_exhausted"] = limits.expired
    return {
        "solved": sum(entry.state == "solved" for entry in planner.entries),
        "skipped": sum(entry.state not in {"solved", "failed"} for entry in planner.entries),
        "failed": malformed + sum(entry.state == "failed" for entry in planner.entries),
    }
