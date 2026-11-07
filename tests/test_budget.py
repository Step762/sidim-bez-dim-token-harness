import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from decimal import Decimal, localcontext
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from budget_manager import BudgetError, BudgetManager, cost_tenths
from harness import build_workflow, run_tasks
from llm_gateway import AccountingPolicy, DEFAULT_MODEL, LLMError, LLMGateway
from llm_fallback import prepare
from test_llm import MATH, MATH_PLAN, envelope


class ManagerTests(unittest.TestCase):
    def test_environment_requires_explicit_nonnegative_integer(self):
        for raw in (None, "", "-1", "1.5", "nan", "+5", "1_000", "１２", " 5"):
            with self.subTest(raw=raw), patch.dict(os.environ, {}, clear=True):
                if raw is not None:
                    os.environ["BUDGET"] = raw
                with self.assertRaises(BudgetError):
                    BudgetManager.from_env()
        with patch.dict(os.environ, {"BUDGET": "100000"}):
            self.assertEqual(BudgetManager.from_env().limit, 100000)

    def test_invalid_counts_cannot_change_state(self):
        manager = BudgetManager(10)
        for value in (-1, True, 1.5, "3", None):
            with self.subTest(value=value), self.assertRaises(BudgetError):
                manager.reserve("bad", "task", value, 1)
        self.assertEqual(manager.snapshot()["requests"], ())

    def test_exact_fractional_costs_and_no_rounding_accumulation(self):
        manager = BudgetManager(100000)
        with localcontext() as context:
            context.prec = 2
            for index in range(123):
                manager.reserve(str(index), "task", 3, 2)
                manager.settle(str(index), 3, 2)
            stats = manager.snapshot()
        self.assertEqual(cost_tenths(123, 45), 573)
        self.assertEqual(stats["spent"], Decimal("282.9"))
        self.assertEqual(stats["available"], Decimal("99717.1"))
        self.assertEqual(stats["reserved"], 0)

    def test_exactly_sufficient_reservation_and_refund(self):
        manager = BudgetManager(10)
        manager.reserve("r", "a", 10, 9)
        self.assertEqual(manager.available, 0)
        self.assertEqual(manager.snapshot()["reserved"], 10)
        manager.settle("r", 3, 2)
        self.assertEqual(manager.available, Decimal("7.7"))

    def test_zero_small_and_insufficient_budget(self):
        for limit, inputs, outputs in ((0, 0, 0), (1, 1, 1), (10, 11, 9)):
            with self.subTest(limit=limit), self.assertRaises(BudgetError):
                BudgetManager(limit).reserve("r", "a", inputs, outputs)

    def test_duplicate_reservation_does_not_replace_original(self):
        manager = BudgetManager(100)
        original = manager.reserve("r", "a", 10, 9)
        with self.assertRaises(BudgetError):
            manager.reserve("r", "b", 0, 1)
        self.assertEqual(manager.snapshot()["requests"], (original,))

    def test_settlement_is_idempotent_and_conflicting_settlement_rejected(self):
        manager = BudgetManager(100)
        manager.reserve("r", "a", 10, 9)
        manager.settle("r", 3, 2)
        manager.settle("r", 3, 2)
        with self.assertRaises(BudgetError):
            manager.settle("r", 4, 2)
        with self.assertRaises(BudgetError):
            manager.reserve("r", "a", 10, 9)
        manager.mark_uncertain("r")
        self.assertEqual(manager.snapshot()["spent"], Decimal("2.3"))
        self.assertFalse(manager.snapshot()["frozen"])

    def test_uncertain_reservation_is_retained_and_cannot_be_refunded(self):
        manager = BudgetManager(100)
        manager.reserve("r", "task", 10, 9)
        manager.mark_uncertain("r")
        manager.mark_uncertain("r")
        stats = manager.snapshot()
        self.assertEqual((stats["spent"], stats["reserved"], stats["available"]), (0, 10, 0))
        self.assertEqual(stats["uncertain_calls"], 1)
        with self.assertRaises(BudgetError):
            manager.settle("r", 0, 0)
        with self.assertRaises(BudgetError):
            manager.reserve("s", "other", 0, 1)

    def test_observed_overrun_is_not_hidden_by_clamping_spent(self):
        manager = BudgetManager(10)
        manager.reserve("r", "a", 0, 10)
        manager.settle("r", 10, 11)
        self.assertEqual(manager.snapshot()["spent"], 12)
        self.assertEqual(manager.available, 0)
        self.assertTrue(manager.snapshot()["frozen"])

    def test_per_task_totals_and_snapshot_are_independent(self):
        manager = BudgetManager(100000)
        for request, task in (("1", "alpha"), ("2", "beta"), ("3", "alpha")):
            manager.reserve(request, task, 100, 20)
            manager.settle(request, 100, 20)
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["spent"], 90)
        self.assertEqual(snapshot["tasks"]["alpha"], {"calls": 2, "spent_tenths": 600, "reserved_tenths": 0})
        snapshot["tasks"]["alpha"]["spent_tenths"] = 999
        self.assertEqual(manager.snapshot()["tasks"]["alpha"]["spent_tenths"], 600)

    def test_competing_reservations_never_exceed_limit(self):
        manager = BudgetManager(10)
        def attempt(index):
            try:
                manager.reserve(str(index), "task", 0, 1)
                return True
            except BudgetError:
                return False
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(attempt, range(100)))
        self.assertEqual(sum(results), 10)
        self.assertEqual(manager.snapshot()["reserved"], 10)
        self.assertEqual(manager.available, 0)


class GatewayBudgetTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def gateway(self, manager=None, *, usage=None, status=200, handler=None, **kwargs):
        manager = manager if manager is not None else BudgetManager(100000)
        requests = []
        async def transport(request):
            requests.append(request)
            if handler is not None:
                return await handler(request)
            data = envelope("{}")
            if usage is not None:
                data["usage"] = usage
            return httpx.Response(status, json=data)
        gateway = LLMGateway("http://budget-test.invalid/v1", "local-test-key",
                             manager=manager, transport=httpx.MockTransport(transport), **kwargs)
        return gateway, requests

    def call(self, gateway, *, task="direct"):
        return asyncio.run(gateway.complete("JSON", {}, task_id=task))

    def test_sequential_requests_use_actual_usage_and_shared_state(self):
        manager = BudgetManager(100000)
        first, requests = self.gateway(manager)
        second, others = self.gateway(manager)
        self.call(first, task="alpha")
        self.call(second, task="beta")
        self.assertEqual(len(requests) + len(others), 2)
        stats = manager.snapshot()
        self.assertEqual((stats["spent"], stats["reserved"], stats["available"]), (60, 0, 99940))
        self.assertEqual(set(stats["tasks"]), {"alpha", "beta"})

    def test_direct_call_without_manager_is_rejected(self):
        with self.assertRaises(LLMError):
            LLMGateway("http://budget-test.invalid/v1", "local-test-key")
        with patch.dict(os.environ, {"BUDGET": "100000"}), self.assertRaises(LLMError):
            LLMGateway.from_env()

    def test_conflicting_or_invalid_environment_cannot_expand_shared_limit(self):
        gateway, requests = self.gateway()
        for raw in ("1", "200000", "0", "invalid", "+100000"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"BUDGET": raw}), self.assertRaises(LLMError):
                self.call(gateway)
        self.assertEqual(requests, [])

    def test_zero_and_small_budget_make_no_http_request(self):
        for limit in (0, 1):
            gateway, requests = self.gateway(BudgetManager(limit))
            with self.subTest(limit=limit), self.assertRaises(LLMError):
                self.call(gateway)
            self.assertEqual(requests, [])
            self.assertEqual(gateway.manager.snapshot()["requests"], ())

    def test_real_estimate_and_exact_available_reservation(self):
        gateway, _ = self.gateway()
        estimate = gateway.estimate_input("JSON", {})
        messages = [{"role": "system", "content": "JSON"}, {"role": "user", "content": "{}"}]
        self.assertEqual(estimate, 1024 + len(json.dumps(messages, ensure_ascii=False).encode()))
        needed = cost_tenths(estimate, gateway.max_tokens)
        manager = BudgetManager((needed + 9) // 10)
        remainder = manager.limit * 10 - needed
        manager.reserve("setup", "setup", remainder, 0)
        manager.settle("setup", remainder, 0)
        sufficient, requests = self.gateway(manager)
        self.call(sufficient)
        self.assertEqual(len(requests), 1)

    def test_insufficient_available_fails_before_transport(self):
        gateway, requests = self.gateway(BudgetManager(100000))
        manager = gateway.manager
        manager.reserve("occupied", "other-task", 0, 99999)
        with self.assertRaises(LLMError):
            self.call(gateway)
        self.assertEqual(requests, [])

    def test_api_failure_keeps_reservation_and_freezes_shared_manager(self):
        first, requests = self.gateway(status=500)
        second, others = self.gateway(first.manager)
        with self.assertRaises(LLMError):
            self.call(first)
        with self.assertRaises(LLMError):
            self.call(second)
        stats = first.manager.snapshot()
        self.assertEqual((len(requests), len(others)), (1, 0))
        self.assertEqual(stats["spent"], 0)
        self.assertGreater(stats["reserved"], 0)
        self.assertEqual(stats["available"], 0)

    def test_timeout_holds_unknown_cost(self):
        async def timeout(request):
            raise httpx.ReadTimeout("private provider detail")
        gateway, requests = self.gateway(handler=timeout)
        with self.assertRaises(LLMError):
            self.call(gateway)
        self.assertEqual(len(requests), 1)
        self.assertEqual(gateway.manager.snapshot()["uncertain_calls"], 1)
        self.assertGreater(gateway.manager.snapshot()["reserved"], 0)

    def test_cancellation_keeps_reservation(self):
        async def cancel(request):
            raise asyncio.CancelledError
        gateway, _ = self.gateway(handler=cancel)
        with self.assertRaises(asyncio.CancelledError):
            self.call(gateway)
        self.assertEqual(gateway.manager.snapshot()["uncertain_calls"], 1)

    def test_missing_invalid_and_inconsistent_usage_hold_reserve(self):
        for usage in ({}, {"prompt_tokens": -1, "completion_tokens": 20},
                      {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 119},
                      {"prompt_tokens": 100, "completion_tokens": 20, "completion_tokens_details": []}):
            with self.subTest(usage=usage):
                gateway, _ = self.gateway(usage=usage)
                with self.assertRaises(LLMError):
                    self.call(gateway)
                stats = gateway.manager.snapshot()
                self.assertEqual(stats["spent"], 0)
                self.assertGreater(stats["reserved"], 0)
                self.assertEqual(stats["available"], 0)
        async def missing(request):
            data = envelope("{}")
            del data["usage"]
            return httpx.Response(200, json=data)
        gateway, _ = self.gateway(handler=missing)
        with self.assertRaises(LLMError):
            self.call(gateway)
        self.assertEqual(gateway.manager.snapshot()["uncertain_calls"], 1)

    def test_inclusive_reasoning_is_counted_once(self):
        usage = {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168,
                 "completion_tokens_details": {"reasoning_tokens": 30}}
        gateway, _ = self.gateway(usage=usage, model="qwen3.8-27b",
                                 accounting=AccountingPolicy("included", True))
        self.call(gateway)
        self.assertEqual(gateway.manager.snapshot()["spent"], Decimal("57.3"))
        self.assertEqual(gateway.manager.snapshot()["requests"][0].output_tokens, 45)

    def test_separate_reasoning_requires_declared_contract(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "reasoning_tokens": 7, "total_tokens": 127}
        gateway, _ = self.gateway(usage=usage, model="qwen3.8-27b",
                                 accounting=AccountingPolicy("separate", True))
        self.call(gateway)
        self.assertEqual(gateway.manager.snapshot()["spent"], 37)

    def test_unknown_reasoning_semantics_cannot_be_reported_as_exact_cost(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 20,
                 "completion_tokens_details": {"reasoning_tokens": 7}}
        gateway, _ = self.gateway(usage=usage)
        with self.assertRaises(LLMError):
            self.call(gateway)
        self.assertEqual(gateway.manager.snapshot()["uncertain_calls"], 1)
        self.assertEqual(gateway.manager.snapshot()["spent"], 0)

    def test_conflicting_reasoning_and_inclusive_overflow_are_rejected(self):
        for details, top in ((7, 8), (21, 21)):
            gateway, _ = self.gateway(
                usage={"prompt_tokens": 100, "completion_tokens": 20, "reasoning_tokens": top,
                       "completion_tokens_details": {"reasoning_tokens": details}},
                accounting=AccountingPolicy("included", True))
            with self.assertRaises(LLMError):
                self.call(gateway)
            self.assertEqual(gateway.manager.snapshot()["uncertain_calls"], 1)

    def test_unverified_reasoning_output_bound_prevents_network(self):
        for policy in (AccountingPolicy(), AccountingPolicy("included", False), AccountingPolicy("unknown", True)):
            gateway, requests = self.gateway(model="qwen3.8-27b", accounting=policy)
            with self.assertRaises(LLMError):
                self.call(gateway)
            self.assertEqual(requests, [])

    def test_cached_input_is_charged_in_full(self):
        gateway, _ = self.gateway(usage={"prompt_tokens": 100, "completion_tokens": 20,
                                        "prompt_tokens_details": {"cached_tokens": 100}})
        self.call(gateway)
        self.assertEqual(gateway.manager.snapshot()["spent"], 30)

    def test_bad_content_with_confirmed_usage_still_costs_tokens(self):
        async def bad(request):
            data = envelope("{}")
            data["choices"] = []
            return httpx.Response(200, json=data)
        gateway, _ = self.gateway(handler=bad)
        with self.assertRaises(LLMError):
            self.call(gateway)
        self.assertEqual(gateway.manager.snapshot()["spent"], 30)
        self.assertEqual(gateway.manager.snapshot()["reserved"], 0)

    def test_observed_input_underestimate_freezes_future_calls(self):
        gateway, requests = self.gateway(usage={"prompt_tokens": 99999, "completion_tokens": 20})
        self.call(gateway)
        self.assertEqual(gateway.manager.snapshot()["spent"], Decimal("10019.9"))
        with self.assertRaises(LLMError):
            self.call(gateway)
        self.assertEqual(len(requests), 1)

    def test_concurrent_gateway_calls_reserve_before_sending(self):
        gateway, _ = self.gateway(max_tokens=64)
        needed = cost_tenths(gateway.estimate_input("JSON", {}), 64)
        manager = BudgetManager((needed + 9) // 10)
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def stalled(request):
                entered.set()
                await release.wait()
                return httpx.Response(200, json=envelope("{}"))
            first, requests = self.gateway(manager, max_tokens=64, handler=stalled)
            second, others = self.gateway(manager, max_tokens=64)
            task = asyncio.create_task(first.complete("JSON", {}, task_id="alpha"))
            await entered.wait()
            try:
                with self.assertRaises(LLMError):
                    await second.complete("JSON", {}, task_id="beta")
            finally:
                release.set()
            await task
            self.assertEqual((len(requests), len(others)), (1, 0))
        asyncio.run(scenario())

    def test_workflow_rejects_different_state_object(self):
        gateway, _ = self.gateway()
        with self.assertRaises(ValueError):
            build_workflow(llm=gateway, manager=BudgetManager(100000))

    def test_untracked_openjiuwen_client_stops_before_transport(self):
        from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig, OpenAIModelClient
        self.gateway()  # Install the actual framework callback guard.
        client = OpenAIModelClient(
            model_config=ModelRequestConfig(model=DEFAULT_MODEL, max_tokens=64),
            model_client_config=ModelClientConfig(
                client_provider="OpenAI", api_base="http://blocked.invalid/v1", api_key="mock-key",
                max_retries=0, use_shared_llm_http_client=False,
            ),
        )
        with patch.object(client, "_create_async_openai_client", side_effect=AssertionError("transport reached")) as transport:
            with self.assertRaises(Exception) as error:
                asyncio.run(client.invoke([{"role": "user", "content": "untracked"}]))
        self.assertNotIsInstance(error.exception, AssertionError)
        transport.assert_not_called()

    def test_multiple_tasks_exhaust_shared_budget_and_local_work_continues(self):
        async def handler(request):
            body = json.loads(request.content)
            inputs = len(json.dumps(body["messages"], ensure_ascii=False).encode()) + 1024
            outputs = body["max_tokens"]
            data = envelope(json.dumps(MATH_PLAN))
            data["usage"] = {"prompt_tokens": inputs, "completion_tokens": outputs}
            return httpx.Response(200, json=data)
        provisional, _ = self.gateway()
        plan = prepare("math", MATH, None)
        amount = cost_tenths(provisional.estimate_input(plan.system, plan.payload), provisional.max_tokens)
        manager = BudgetManager((amount + 9) // 10)
        gateway, requests = self.gateway(manager, handler=handler)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = "A warehouse starts with 100 boxes. It ships 7 boxes per day for 3 days. How many boxes does it have at the end?"
            for name, instruction in (("a", MATH), ("b", MATH), ("c", local)):
                task = root / name
                task.mkdir()
                (task / "task.json").write_text(json.dumps({"task_type": "math", "reward": 1, "instruction": instruction}))
            with redirect_stdout(io.StringIO()):
                counts = asyncio.run(run_tasks(root, llm=gateway, manager=manager))
            self.assertEqual(counts, {"solved": 2, "skipped": 1, "failed": 0})
            self.assertEqual((root / "a" / "answer.txt").read_text(), "45\n")
            self.assertFalse((root / "b" / "answer.txt").exists())
            self.assertEqual((root / "c" / "answer.txt").read_text(), "79\n")
            self.assertEqual(len(requests), 1)
            self.assertIn(str((root / "a").resolve()), manager.snapshot()["tasks"])

    def test_stats_do_not_store_secret_or_prompt(self):
        gateway, _ = self.gateway()
        asyncio.run(gateway.complete("private document contents", {"secret": "confidential"}, task_id="task"))
        text = repr(gateway.manager.snapshot()) + gateway.manager.summary()
        for forbidden in ("private document", "confidential", "local-test-key", "budget-test.invalid"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
