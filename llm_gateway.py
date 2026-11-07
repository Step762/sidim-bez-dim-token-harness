"""One bounded Chat Completions call, with no provider secrets in diagnostics."""

import asyncio
from dataclasses import dataclass
import json
import math
import os
from contextvars import ContextVar
from weakref import WeakSet
import uuid

import httpx

from budget_manager import BudgetError, BudgetManager


MODELS = frozenset({
    "qwen3.8-27b", "qwen3.8-27b-noreason",
    "qwen3.6-35b-a3b", "qwen3.6-35b-a3b-noreason",
})
DEFAULT_MODEL = "qwen3.6-35b-a3b-noreason"
MAX_RESPONSE_BYTES = 128 * 1024
MAX_PROMPT_CHARS = 18000


class LLMError(ValueError):
    """A sanitized configuration, transport or protocol error."""


@dataclass(frozen=True)
class AccountingPolicy:
    """Trusted provider contract, never inferred from task data or a model reply.

    The default supports documented noreason variants only. A reasoning model
    requires a separately verified total generation cap AND usage semantics.
    Tests can explicitly declare their mock server's contract.
    """
    reasoning: str = "unknown"  # unknown, included, separate
    total_output_bound_confirmed: bool = False


_sdk_permit = ContextVar("budgeted_sdk_request", default=None)
_guarded_frameworks = WeakSet()


def install_openjiuwen_budget_guard():
    """Reject native SDK requests outside the gateway's reserved wire request.

    Uses public callbacks in the pinned openJiuwen version, not an import stub.
    A permit is single-use and task-local; retries/streams/tools are not allowed.
    """
    from openjiuwen.core.runner.callback import AbortError, LLMCallEvents, get_callback_framework

    framework = get_callback_framework()
    if framework in _guarded_frameworks:
        return

    @framework.on(LLMCallEvents.LLM_INPUT, priority=100000, namespace="harness_budget")
    async def guard(*args, **kwargs):
        permit = _sdk_permit.get()
        if permit is not None and not permit["used"]:
            body = permit["body"]
            if (
                kwargs.get("model_name") == body["model"]
                and kwargs.get("messages") == body["messages"]
                and kwargs.get("max_tokens") == body["max_tokens"]
                and not kwargs.get("tools") and not kwargs.get("is_stream")
            ):
                permit["used"] = True
                return
        raise AbortError("unbudgeted LLM call", cause=BudgetError("use the shared LLM gateway"), is_error=False)

    _guarded_frameworks.add(framework)


def strict_json(text: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LLMError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise LLMError("nonfinite JSON value")

    try:
        return json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, RecursionError):
        raise LLMError("invalid JSON") from None


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int | None
    reasoning_tokens: int | None


@dataclass(frozen=True)
class Completion:
    content: str
    usage: Usage


def _usage(raw) -> Usage:
    if not isinstance(raw, dict):
        raise LLMError("missing usage statistics")

    def counter(value, required=False):
        if value is None and not required:
            return None
        if type(value) is not int or value < 0:
            raise LLMError("invalid usage statistics")
        return value

    details = raw.get("completion_tokens_details")
    if details is not None and not isinstance(details, dict):
        raise LLMError("invalid usage statistics")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if reasoning is not None and raw.get("reasoning_tokens") is not None and reasoning != raw["reasoning_tokens"]:
        raise LLMError("conflicting reasoning statistics")
    if reasoning is None:
        reasoning = raw.get("reasoning_tokens")
    return Usage(
        counter(raw.get("prompt_tokens"), True),
        counter(raw.get("completion_tokens"), True),
        counter(raw.get("total_tokens")), counter(reasoning),
    )


class LLMGateway:
    def __init__(
        self, base_url: str, api_key: str, *, model: str = DEFAULT_MODEL,
        manager: BudgetManager | None = None, timeout: float = 20, max_tokens: int = 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        accounting: AccountingPolicy = AccountingPolicy(),
    ):
        if model not in MODELS:
            raise LLMError("model is not permitted")
        if not isinstance(api_key, str) or not api_key.strip() or any(c in api_key for c in "\r\n"):
            raise LLMError("LLM_API_KEY is missing or invalid")
        try:
            url = httpx.URL(base_url)
        except (httpx.InvalidURL, TypeError):
            raise LLMError("LLM_BASE_URL is missing or invalid") from None
        if (
            url.scheme not in {"http", "https"} or not url.host
            or url.username or url.password or url.query or url.fragment
        ):
            raise LLMError("LLM_BASE_URL is missing or invalid")
        if not isinstance(manager, BudgetManager):
            raise LLMError("a shared BudgetManager is required")
        if (
            not isinstance(accounting, AccountingPolicy)
            or accounting.reasoning not in {"unknown", "included", "separate"}
            or type(accounting.total_output_bound_confirmed) is not bool
        ):
            raise LLMError("invalid accounting policy")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise LLMError("invalid LLM timeout")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
            raise LLMError("LLM response limit must be between 1 and 4096 tokens")
        base = str(url).rstrip("/")
        self._endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        self._api_key = api_key
        self.model = model
        self.manager = manager
        self.accounting = accounting
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._transport = transport
        self.blocked = False
        self.usage_records: list[Usage | None] = []
        self._request_ids: set[str] = set()
        install_openjiuwen_budget_guard()

    @property
    def budget(self):
        return self.manager.limit

    @classmethod
    def from_env(cls, *, manager=None, model=DEFAULT_MODEL, timeout=20, max_tokens=1024):
        try:
            configured = BudgetManager.from_env()
        except BudgetError as exc:
            raise LLMError(str(exc)) from None
        if not isinstance(manager, BudgetManager) or manager.limit != configured.limit:
            raise LLMError("the run's shared BudgetManager must match BUDGET")
        return cls(
            os.environ.get("LLM_BASE_URL", ""), os.environ.get("LLM_API_KEY", ""),
            model=model, manager=manager, timeout=timeout, max_tokens=max_tokens,
        )

    @property
    def available(self):
        raw_budget = os.environ.get("BUDGET")
        environment_allows = raw_budget is None or (
            raw_budget.isascii() and raw_budget.isdecimal()
            and int(raw_budget) == self.manager.limit and self.manager.limit > 0
        )
        return self.manager.available > 0 and environment_allows and not self.blocked

    @property
    def calls(self):
        return len(self.usage_records)

    def summary(self) -> str:
        if not self.calls:
            return "llm_tokens_used=0"
        complete = all(usage is not None for usage in self.usage_records) and all(
            record.state == "settled" for record in self.manager.snapshot()["requests"]
            if record.request_id in self._request_ids
        )
        if not complete:
            return f"llm_calls={self.calls} llm_usage=unknown"
        inputs = sum(usage.prompt_tokens for usage in self.usage_records)
        outputs = sum(usage.completion_tokens for usage in self.usage_records)
        return f"llm_calls={self.calls} llm_prompt_tokens={inputs} llm_completion_tokens={outputs}"

    def estimate_input(self, system: str, payload: dict) -> int:
        """Conservative estimate: UTF-8 bytes + 1024 framing tokens, not a tokenizer.

        A provider can add undocumented overhead; any observed underestimate
        freezes the manager. This cannot guarantee a remote provider's behavior.
        """
        messages = self._messages(system, payload)
        return len(json.dumps(messages, ensure_ascii=False, allow_nan=False).encode("utf-8")) + 1024

    @staticmethod
    def _messages(system, payload):
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if not isinstance(system, str) or len(system) + len(text) > MAX_PROMPT_CHARS:
            raise LLMError("LLM context limit exceeded")
        return [{"role": "system", "content": system}, {"role": "user", "content": text}]

    def _output_tokens(self, usage):
        reasoning = usage.reasoning_tokens
        policy = self.accounting.reasoning
        if policy == "included":
            if reasoning is not None and reasoning > usage.completion_tokens:
                raise LLMError("reasoning exceeds inclusive completion count")
            output = usage.completion_tokens
        elif policy == "separate":
            if reasoning is None:
                raise LLMError("missing separate reasoning count")
            output = usage.completion_tokens + reasoning
        elif reasoning not in (None, 0):
            raise LLMError("unknown reasoning accounting semantics")
        else:
            output = usage.completion_tokens
        if usage.total_tokens is not None and usage.total_tokens != usage.prompt_tokens + output:
            raise LLMError("inconsistent usage total")
        return output

    async def complete(self, system: str, payload: dict, *, task_id: str = "direct") -> Completion:
        if not self.available:
            raise LLMError("LLM is disabled or budget is zero")
        if (not self.model.endswith("-noreason") or self.accounting.reasoning == "separate") and (
            not self.accounting.total_output_bound_confirmed or self.accounting.reasoning == "unknown"
        ):
            raise LLMError("reasoning model requires a verified total output bound and usage contract")
        messages = self._messages(system, payload)
        request_id = uuid.uuid4().hex
        try:
            self.manager.reserve(request_id, task_id, self.estimate_input(system, payload), self.max_tokens)
        except BudgetError as exc:
            raise LLMError(str(exc)) from None
        position = len(self.usage_records)
        self._request_ids.add(request_id)
        self.usage_records.append(None)  # Errors can still have an unknown provider cost.
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens, "stream": False,
        }
        permit = _sdk_permit.set({"body": body, "used": False})
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(
                    timeout=self.timeout, trust_env=False, follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    async with client.stream(
                        "POST", self._endpoint, json=body,
                        headers={"Authorization": "Bearer " + self._api_key},
                    ) as response:
                        if response.status_code != 200:
                            if response.status_code in {401, 403, 429}:
                                self.blocked = True
                            raise LLMError(f"LLM HTTP status {response.status_code}")
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > MAX_RESPONSE_BYTES:
                                raise LLMError("LLM response byte limit exceeded")
            raw = strict_json(data.decode("utf-8"))
            if not isinstance(raw, dict):
                raise LLMError("invalid Chat Completions response")
            usage = _usage(raw.get("usage"))
            self.usage_records[position] = usage
            self.manager.settle(request_id, usage.prompt_tokens, self._output_tokens(usage))
            choices = raw.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise LLMError("invalid completion choices")
            choice = choices[0]
            message = choice.get("message")
            if choice.get("finish_reason") != "stop" or not isinstance(message, dict):
                raise LLMError("incomplete LLM response")
            content = message.get("content")
            if (
                message.get("role") != "assistant" or message.get("tool_calls")
                or not isinstance(content, str) or not content.strip()
                or len(content) > 24000 or self._api_key in content
            ):
                raise LLMError("invalid LLM message")
            return Completion(content, usage)
        except LLMError:
            raise
        except (httpx.HTTPError, TimeoutError, UnicodeError, ValueError):
            raise LLMError("LLM transport, timeout or protocol failure") from None
        finally:
            self.manager.mark_uncertain(request_id)  # No-op after confirmed settlement.
            _sdk_permit.reset(permit)
