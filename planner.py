"""Deterministic local-first scheduling with explicit, inspectable heuristics."""

from dataclasses import dataclass, field
from fractions import Fraction
import csv
import re

import math_solver
import search_solver
import data_solver
import coding_solver
from budget_manager import cost_tenths
from llm_fallback import prepare, InvalidPlan
from llm_gateway import LLMError
from safe_files import UnsafeFile, markdown_files, open_directory
from runtime_limits import OperationTimeout


STRATEGIES = ("priority", "original", "reward")


def local_outcome(reason: str) -> str:
    """Classify evidence, not arbitrary unparseable text, as a hard stop.

    Missing/contradictory inputs do not prove a problem universally impossible.
    Only the supported division-by-zero case is explicitly unsolvable here.
    """
    if reason == "local operation time limit reached":
        return "timed_out"
    if reason == "cannot split into zero warehouses":
        return "unsolvable"
    if any(word in reason for word in ("outside", "external", "symbolic link", "trusted", "repair targets")):
        return "unsafe"
    if reason.startswith("missing or ambiguous ") or reason in {
        "conflicting or ambiguous entity definitions", "requested column is absent from CSV",
        "required Python files are missing", "aggregate is undefined for an empty selection",
        "year is not a single supported integer value",
    }:
        return "insufficient_evidence"
    if reason in {
        "invalid or unsupported numeric value", "CSV row has the wrong number of fields",
        "missing, empty or duplicate CSV headers", "COUNT column contains missing values",
        "CSV cannot be read or contains invalid data", "cannot read the complete knowledge base",
    }:
        return "invalid_data"
    return "unsupported"


@dataclass(frozen=True)
class Features:
    complexity: int
    native_form: bool
    files_available: bool
    blocker: str = ""


def _features(task, trusted_code=False, *, static_coding=False) -> Features:
    """Only syntax and file metadata; never run a solver or execute task text."""
    text = " ".join(task.instruction.split())
    numbers = len(re.findall(r"(?<!\w)[0-9]+", text))
    relations = len(re.findall(r"\b(?:mentor|employer)\b", text, re.I))
    conditions = len(re.findall(r"\b(?:and|where|through|between)\b", text, re.I))
    complexity = 1 + numbers // 3 + relations + conditions + len(text) // 1000
    native = False
    if task.task_type == "math":
        native = math_solver.local_form(text)
    elif task.task_type == "search":
        parsed = search_solver._INSTRUCTION.fullmatch(text)
        native = parsed is not None and any(p.fullmatch(parsed["question"]) for p, _ in search_solver._QUESTIONS)
    elif task.task_type == "data_analysis":
        native = data_solver._INSTRUCTION.fullmatch(text) is not None
    elif task.task_type == "coding":
        native = coding_solver._INSTRUCTION.fullmatch(text) is not None
        if not trusted_code and static_coding is not True:
            return Features(complexity, native, False, "unsafe")
    else:
        return Features(complexity, False, False, "unsupported_type")
    if task.task_type == "math":
        return Features(complexity, native, True)
    workspace = task.directory / "workspace"
    if not workspace.resolve().is_relative_to(task.directory.resolve()):
        return Features(complexity, native, False, "unsafe")
    if not workspace.is_dir():
        return Features(complexity, native, False, "insufficient_evidence")
    if task.task_type == "search":
        kb = workspace / "kb"
        if not kb.resolve().is_relative_to(workspace.resolve()):
            return Features(complexity, native, False, "unsafe")
        if not kb.is_dir() or not markdown_files(kb):
            return Features(complexity, native, False, "insufficient_evidence")
    elif task.task_type == "data_analysis":
        names = set(re.findall(r"(?<![\w./-])([A-Za-z0-9_./-]+\.csv)(?![\w./-])", text))
        if len(names) == 1:
            path = workspace / next(iter(names))
            if not path.resolve().is_relative_to(workspace.resolve()):
                return Features(complexity, native, False, "unsafe")
            if not path.is_file():
                return Features(complexity, native, False, "insufficient_evidence")
    elif task.task_type == "coding" and native:
        parsed = coding_solver._INSTRUCTION.fullmatch(text)
        for key in ("source", "test"):
            path = workspace / parsed[key]
            if not path.resolve().is_relative_to(workspace.resolve()):
                return Features(complexity, native, False, "unsafe")
            if not path.is_file():
                return Features(complexity, native, False, "insufficient_evidence")
    return Features(complexity, native, True)


def features(task, trusted_code=False, *, static_coding=False) -> Features:
    try:
        with open_directory(task.directory):
            return _features(task, trusted_code, static_coding=static_coding)
    except (OSError, RuntimeError, UnsafeFile):
        return Features(1, False, False, "invalid_data")


@dataclass(frozen=True)
class Estimate:
    probability: Fraction
    cost_tenths: Fraction
    reservation_tenths: int
    source: str


@dataclass
class Entry:
    task: object
    features: Features
    state: str = "pending_local"
    local_result: str = ""
    reason: str = ""
    local_attempts: int = 0
    llm_attempts: int = 0
    estimated_input: int = 0
    model: str = ""
    attempts: list = field(default_factory=list)

    @property
    def key(self):
        # Stable identifier: a later link replacement must not change scheduler keys.
        return str(self.task.directory.absolute())

    @property
    def terminal(self):
        return self.state not in {"pending_local", "running_local", "deferred", "running_llm"}


@dataclass(frozen=True)
class Decision:
    entry: Entry
    phase: str
    estimate: Estimate | None = None


class Planner:
    """One local probe and at most one LLM attempt per task, with no paid planning.

    original/reward are controlled ordering baselines: they retain the same
    local-first execution, budget admission and safety checks as priority.
    """
    def __init__(self, tasks, manager, gateway=None, *, strategy="priority", trusted_code=False,
                 static_coding=False, llm_enabled=True, limits=None):
        if strategy not in STRATEGIES:
            raise ValueError("unknown planning strategy")
        if gateway is not None and gateway.manager is not manager:
            raise ValueError("planner and gateway must share one BudgetManager")
        self.manager, self.gateway = manager, gateway
        self.strategy, self.llm_enabled = strategy, llm_enabled
        self.entries = []
        feature_options = {"static_coding": True} if static_coding is True else {}
        for task in tasks:
            try:
                if limits is None:
                    value = features(task, trusted_code, **feature_options)
                else:
                    with limits.sync():
                        value = features(task, trusted_code, **feature_options)
            except OperationTimeout:
                value = Features(1,False,False,"timed_out")
            self.entries.append(Entry(task,value))
        keys = [entry.key for entry in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate task directory")
        self.order = []

    def _observations(self, entry, snapshot):
        # Observed costs come exclusively from BudgetManager, never a second ledger.
        peers = {e.key: e for e in self.entries if e.model == self.gateway.model
                 and e.task.task_type == entry.task.task_type and e.llm_attempts}
        return [(record, peers[record.task_id]) for record in snapshot["requests"]
                if record.state == "settled" and record.task_id in peers
                and peers[record.task_id].terminal]

    def estimate(self, entry, snapshot=None):
        snapshot = self.manager.snapshot() if snapshot is None else snapshot
        reserve = cost_tenths(entry.estimated_input, self.gateway.max_tokens)
        prior = {"math": Fraction(3, 4), "search": Fraction(4, 5), "data_analysis": Fraction(3, 4)}[entry.task.task_type]
        probability = prior / (1 + Fraction(entry.features.complexity, 10))
        # Explicit heuristic forecast; admission still requires the FULL reserve.
        predicted = Fraction(entry.estimated_input) + 5 * self.gateway.max_tokens
        observations = self._observations(entry, snapshot)
        source = "heuristic"
        if len(observations) >= 3:
            total_input_estimates = sum(record.estimated_input for record, _ in observations)
            input_ratio = Fraction(sum(record.input_tokens for record, _ in observations), total_input_estimates)
            output_mean = Fraction(sum(record.output_tokens for record, _ in observations), len(observations))
            learned = entry.estimated_input * input_ratio + 10 * output_mean
            if learned > 0:
                predicted = min(Fraction(reserve), learned)
            successes = sum(peer.state == "solved" for _, peer in observations)
            probability = (4 * probability + successes) / (4 + len(observations))
            source = f"observed:{len(observations)}+prior"
        return Estimate(probability, predicted, reserve, source)

    def _sort_key(self, entry, estimate=None):
        reward = Fraction(str(entry.task.reward))
        if self.strategy == "original":
            return (entry.key,)
        if self.strategy == "reward":
            return (-reward, entry.key)
        if estimate is None:
            # A separate free tier; no division by zero or artificial epsilon.
            probability = Fraction(9, 10) if entry.features.native_form else Fraction(1, 4)
            return (not entry.features.native_form, -reward * probability, entry.features.complexity, entry.key)
        return (-reward * estimate.probability / estimate.cost_tenths, entry.key)

    def next(self):
        snapshot = self.manager.snapshot()  # Fresh even when only local work remains.
        for entry in self.entries:
            if entry.state == "pending_local" and entry.features.blocker:
                entry.state = entry.features.blocker
                entry.reason = f"preflight: {entry.state}"
        local = [entry for entry in self.entries if entry.state == "pending_local"]
        if local:
            entry = min(local, key=self._sort_key)
            entry.state = "running_local"
            entry.local_attempts += 1
            self.order.append((entry.key, "local"))
            return Decision(entry, "local")
        choices = []
        for entry in self.entries:
            if entry.state != "deferred":
                continue
            estimate = self.estimate(entry, snapshot)
            if not self.gateway.available or Fraction(snapshot["available"]) * 10 < estimate.reservation_tenths:
                entry.state = "budget_blocked"
                entry.reason = "full safe reservation unavailable; local formulation was unsupported"
            else:
                choices.append((self._sort_key(entry, estimate), entry, estimate))
        if not choices:
            return None
        _, entry, estimate = min(choices, key=lambda item: item[0])
        entry.state = "running_llm"
        entry.llm_attempts += 1
        entry.model = self.gateway.model
        self.order.append((entry.key, "fallback"))
        return Decision(entry, "fallback", estimate)

    def finish(self, decision, status, reason=""):
        entry = decision.entry
        expected_state = "running_local" if decision.phase == "local" else "running_llm"
        if entry.state != expected_state:
            raise ValueError("attempt already finished or was not selected")
        entry.reason = reason
        entry.attempts.append((decision.phase, status, decision.estimate))
        if status == "solved":
            entry.state = "solved"
            return
        if status == "failed":
            entry.state = "failed"
            return
        if reason == "local operation time limit reached":
            entry.state = "timed_out"
            return
        if decision.phase == "fallback":
            if reason in {"LLM is disabled or budget is zero", "insufficient or uncertain LLM budget"}:
                entry.state = "budget_blocked"
            else:
                entry.state = "llm_failed"
            return
        entry.local_result = local_outcome(reason)
        entry.state = entry.local_result
        if entry.local_result != "unsupported" or entry.task.task_type == "coding":
            return
        if (entry.task.directory / "answer.txt").exists():
            entry.state = "existing_answer"  # An unverified previous file is not claimed as solved.
            return
        if not self.llm_enabled or self.gateway is None:
            entry.state = "budget_blocked" if self.manager.limit == 0 else "llm_disabled"
            return
        try:
            self.gateway.validate_generation_contract()
            prepared = prepare(entry.task.task_type, entry.task.instruction, entry.task.directory)
            entry.estimated_input = self.gateway.estimate_input(prepared.system, prepared.payload)
        except LLMError:
            entry.state = "unsafe"
            entry.reason = "LLM generation bound or context cannot be established safely"
        except (InvalidPlan, ValueError, OSError, UnicodeError, csv.Error):
            # Preparation failure is lack of supported evidence, not a proof of impossibility.
            entry.state = "unsupported"
            entry.reason = "fallback context cannot be established reliably"
        else:
            entry.state = "deferred"  # Economically deferred, never discarded for low reward.

    def report(self):
        estimates = [estimate.cost_tenths for entry in self.entries for _, _, estimate in entry.attempts if estimate]
        return {
            "strategy": self.strategy, "order": tuple(self.order),
            "reward_solved": sum(entry.task.reward for entry in self.entries if entry.state == "solved"),
            "unsuccessful_attempts": sum(status != "solved" for e in self.entries for _, status, _ in e.attempts),
            "llm_unsuccessful_attempts": sum(phase == "fallback" and status != "solved"
                                            for e in self.entries for phase, status, _ in e.attempts),
            "estimated_cost": float(sum(estimates, Fraction()) / 10),
            "budget": self.manager.snapshot(),
            "outcomes": {entry.key: {"outcome": entry.state, "local_outcome": entry.local_result,
                                     "local_attempts": entry.local_attempts, "llm_attempts": entry.llm_attempts}
                         for entry in self.entries},
        }
