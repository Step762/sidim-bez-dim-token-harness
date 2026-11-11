"""Run-scoped token accounting. All amounts are exact integer tenths internally."""

from dataclasses import dataclass, replace
from decimal import Decimal
import os
from threading import RLock


class BudgetError(ValueError):
    """A budget/configuration error without request contents or credentials."""


def _count(value):
    if type(value) is not int or value < 0:
        raise BudgetError("token counts and BUDGET must be nonnegative integers")
    return value


def cost_tenths(input_tokens: int, output_tokens: int) -> int:
    return _count(input_tokens) + 10 * _count(output_tokens)


def units(tenths: int) -> Decimal:
    # Decimal construction from text also avoids the global precision context.
    return Decimal(f"{tenths // 10}.{tenths % 10}")


@dataclass(frozen=True)
class RequestCost:
    request_id: str
    task_id: str
    estimated_input: int
    max_output: int
    reserved_tenths: int
    state: str = "reserved"
    input_tokens: int | None = None
    output_tokens: int | None = None
    spent_tenths: int | None = None


class BudgetManager:
    def __init__(self, limit: int):
        self._limit = _count(limit)
        self._records: dict[str, RequestCost] = {}
        self._lock = RLock()
        self._frozen = False

    @classmethod
    def from_env(cls):
        raw = os.environ.get("BUDGET", "")
        if not raw.isascii() or not raw.isdecimal():
            raise BudgetError("an explicit nonnegative integer BUDGET is required")
        return cls(int(raw))

    @property
    def limit(self):
        return self._limit

    def _totals(self):
        spent = sum(r.spent_tenths or 0 for r in self._records.values())
        reserved = sum(r.reserved_tenths for r in self._records.values() if r.state != "settled")
        available = 0 if self._frozen else max(0, self.limit * 10 - spent - reserved)
        return spent, reserved, available

    @property
    def available(self):
        with self._lock:
            return units(self._totals()[2])

    def reserve(self, request_id: str, task_id: str, estimated_input: int, max_output: int):
        amount = cost_tenths(estimated_input, max_output)
        if not request_id or not isinstance(request_id, str) or not task_id or not isinstance(task_id, str):
            raise BudgetError("nonempty request and task identifiers are required")
        with self._lock:
            if request_id in self._records:
                raise BudgetError("request already reserved")
            if self.limit == 0 or self._frozen or amount > self._totals()[2]:
                raise BudgetError("insufficient or uncertain LLM budget")
            record = RequestCost(request_id, task_id, estimated_input, max_output, amount)
            self._records[request_id] = record
            return record

    def settle(self, request_id: str, input_tokens: int, output_tokens: int):
        amount = cost_tenths(input_tokens, output_tokens)
        with self._lock:
            record = self._records[request_id]
            if record.state == "settled":
                if (record.input_tokens, record.output_tokens) != (input_tokens, output_tokens):
                    raise BudgetError("conflicting settlement")
                return record  # Idempotent: the same response cannot be charged twice.
            if record.state != "reserved":
                raise BudgetError("uncertain request cannot be silently settled")
            record = replace(record, state="settled", input_tokens=input_tokens,
                             output_tokens=output_tokens, spent_tenths=amount)
            self._records[request_id] = record
            # A provider/estimator contract violation is visible, never clamped away.
            if input_tokens > record.estimated_input or output_tokens > record.max_output:
                self._frozen = True
            return record

    def mark_uncertain(self, request_id: str):
        with self._lock:
            record = self._records[request_id]
            if record.state == "reserved":
                self._records[request_id] = replace(record, state="uncertain")
                self._frozen = True
            return self._records[request_id]

    def snapshot(self):
        """Independent snapshot containing counts/identifiers, never prompts or keys."""
        with self._lock:
            spent, reserved, available = self._totals()
            tasks = {}
            for record in self._records.values():
                row = tasks.setdefault(record.task_id, {"spent_tenths": 0, "reserved_tenths": 0, "calls": 0})
                row["calls"] += 1
                row["spent_tenths"] += record.spent_tenths or 0
                if record.state != "settled":
                    row["reserved_tenths"] += record.reserved_tenths
            return {
                "limit": self.limit, "spent": units(spent), "reserved": units(reserved),
                "available": units(available), "frozen": self._frozen,
                "uncertain_calls": sum(r.state == "uncertain" for r in self._records.values()),
                "tasks": tasks, "requests": tuple(self._records.values()),
            }

    def summary(self):
        stats = self.snapshot()
        return " ".join(f"budget_{key}={stats[key]}" for key in
                        ("limit", "spent", "reserved", "available", "uncertain_calls", "frozen"))
