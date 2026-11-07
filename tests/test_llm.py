import asyncio
import copy
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import httpx

from harness import build_workflow, run_tasks
from llm_gateway import AccountingPolicy, DEFAULT_MODEL, LLMError, LLMGateway, MODELS
from budget_manager import BudgetManager
from llm_fallback import InvalidPlan, propose_coding
from openjiuwen.core.workflow import WorkflowExecutionState, create_workflow_session


FALLBACK_PREFIX = "Solve the following arithmetic: "
MATH = FALLBACK_PREFIX + "Start with 10 coins. Add 5 coins. Multiply by 3. What is the result?"
MATH_PLAN = {
    "initial": {"value": "10", "quote": "10 coins"},
    "steps": [
        {"op": "add", "value": "5", "quote": "Add 5 coins"},
        {"op": "mul", "value": "3", "quote": "Multiply by 3"},
    ],
}
SEARCH = "What city is Lena Noor's employer based in?"
SEARCH_PLAN = {"entity": "Lena Noor", "relations": ["employer"], "field": "headquarters", "answer": "Faro"}
DATA = "Calculate the total units in sales.csv for product 'gear' and month between 4 and 5 inclusive."
DATA_PLAN = {
    "file": "sales.csv", "operation": "sum", "column": "units", "filters": [
        {"column": "product", "op": "=", "type": "string", "value": "gear", "quote": "product 'gear'"},
        {"column": "month", "op": ">=", "type": "number", "value": "4", "quote": "month between 4 and 5 inclusive"},
        {"column": "month", "op": "<=", "type": "number", "value": "5", "quote": "month between 4 and 5 inclusive"},
    ],
}
ROOT = Path(__file__).resolve().parents[1]


def envelope(content, *, reasoning=False):
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    if reasoning:
        usage["completion_tokens_details"] = {"reasoning_tokens": 7}
    return {
        "id": "local-test", "object": "chat.completion", "model": DEFAULT_MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": usage, "unknown_provider_field": {"ignored": True},
    }


@contextmanager
def local_server(content):
    """The only real HTTP used by these tests is an ephemeral loopback server."""
    state = {"requests": [], "key": uuid.uuid4().hex}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer " + state["key"]:
                self.send_response(401)
                self.end_headers()
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(request)
            body = json.dumps(envelope(content)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    state["base"] = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"BUDGET": "100000"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def gateway(self, plan=MATH_PLAN, *, model=DEFAULT_MODEL, handler=None, budget=100000):
        requests = []
        key = uuid.uuid4().hex

        async def handle(request):
            requests.append(request)
            if handler is not None:
                return await handler(request)
            return httpx.Response(200, json=envelope(json.dumps(plan)))

        gateway = LLMGateway(
            "https://" + uuid.uuid4().hex + ".invalid/v1", key,
            model=model, manager=BudgetManager(budget), timeout=0.5,
            accounting=AccountingPolicy(reasoning="included", total_output_bound_confirmed=True),
            transport=httpx.MockTransport(handle),
        )
        return gateway, requests, key

    def make_task(self, task_type="math", instruction=MATH, files=None):
        # Keep these tests on the real fallback node as the free solver expands.
        # Warehouse fixtures in test_local_successes still exercise the local route.
        if task_type == "math" and not instruction.startswith((FALLBACK_PREFIX, "A warehouse starts with")):
            instruction = FALLBACK_PREFIX + instruction
        task = self.root / f"task_{len(list(self.root.iterdir())):03d}"
        task.mkdir()
        data = {"task_type": task_type, "instruction": instruction, "reward": 3}
        (task / "task.json").write_text(json.dumps(data), encoding="utf-8")
        for name, text in (files or {}).items():
            path = task / "workspace" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return task

    def run_tasks(self, gateway, *, budget=100000, trusted=False):
        with redirect_stdout(io.StringIO()):
            return asyncio.run(run_tasks(self.root, llm=gateway, budget=budget, allow_trusted_code=trusted))

    def assert_answer(self, task, answer):
        self.assertEqual((task / "answer.txt").read_bytes(), (answer + "\n").encode())

    def test_each_permitted_model_uses_confirmed_wire_contract(self):
        for model in sorted(MODELS):
            with self.subTest(model=model):
                gateway, requests, key = self.gateway(model=model)
                reply = asyncio.run(gateway.complete("JSON only", {"question": "local test"}))
                self.assertEqual(json.loads(reply.content), MATH_PLAN)
                self.assertEqual(reply.usage.prompt_tokens, 100)
                self.assertIsNone(reply.usage.reasoning_tokens)
                self.assertEqual(len(requests), 1)
                request = requests[0]
                body = json.loads(request.content)
                self.assertEqual(request.url.path, "/v1/chat/completions")
                self.assertEqual(request.headers["Authorization"], "Bearer " + key)
                self.assertEqual(body["model"], model)
                self.assertEqual(len(body["messages"]), 2)
                self.assertEqual(body["max_tokens"], 1024)
                self.assertFalse(body["stream"])

    def test_observed_reasoning_usage_is_preserved_without_double_counting(self):
        async def handler(request):
            return httpx.Response(200, json=envelope("{}", reasoning=True))
        gateway, _, _ = self.gateway(handler=handler)
        reply = asyncio.run(gateway.complete("JSON", {}))
        self.assertEqual(reply.usage.completion_tokens, 20)
        self.assertEqual(reply.usage.reasoning_tokens, 7)
        self.assertIn("llm_completion_tokens=20", gateway.summary())

    def test_http_failures_are_not_retried_or_logged_with_secrets(self):
        for status in (401, 429, 500):
            with self.subTest(status=status):
                key_holder = {}

                async def handler(request):
                    return httpx.Response(status, json={"error": {"message": key_holder["key"]}})

                gateway, requests, key = self.gateway(handler=handler)
                key_holder["key"] = key
                with self.assertLogs("httpx", level="INFO") as logs:
                    with self.assertRaises(LLMError) as error:
                        asyncio.run(gateway.complete("JSON", {}))
                self.assertNotIn(key, str(error.exception))
                self.assertNotIn(key, " ".join(logs.output))
                self.assertEqual(len(requests), 1)
                self.assertEqual(gateway.summary(), "llm_calls=1 llm_usage=unknown")
                if status in {401, 429}:
                    with self.assertRaises(LLMError):
                        asyncio.run(gateway.complete("JSON", {}))
                    self.assertEqual(len(requests), 1)

    def test_transport_timeout_is_sanitized_and_not_retried(self):
        async def handler(request):
            raise httpx.ReadTimeout("sensitive provider detail", request=request)
        gateway, requests, _ = self.gateway(handler=handler)
        with self.assertRaises(LLMError) as error:
            asyncio.run(gateway.complete("JSON", {}))
        self.assertNotIn("sensitive", str(error.exception))
        self.assertEqual(len(requests), 1)

    def test_total_timeout_bounds_a_stalled_transport(self):
        async def handler(request):
            await asyncio.sleep(2)
            return httpx.Response(200, json=envelope("{}"))
        gateway, requests, _ = self.gateway(handler=handler)
        gateway.timeout = 0.03
        started = time.monotonic()
        with self.assertRaises(LLMError):
            asyncio.run(gateway.complete("JSON", {}))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(requests), 1)

    def test_invalid_outer_json_and_protocol_shapes(self):
        valid = envelope("{}")
        cases = [
            b"not json", b"[]", b'{"choices": [], "choices": []}',
            {**valid, "choices": []}, {**valid, "usage": None},
            {**valid, "usage": {"prompt_tokens": True, "completion_tokens": 20}},
            {**valid, "choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "length"}]},
            {**valid, "choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "stop"}]},
            {**valid, "choices": [{"message": {"role": "assistant", "content": "{}", "tool_calls": [{}]}, "finish_reason": "stop"}]},
        ]
        for value in cases:
            with self.subTest(value=value):
                async def handler(request):
                    return httpx.Response(200, content=value) if isinstance(value, bytes) else httpx.Response(200, json=value)
                gateway, requests, _ = self.gateway(handler=handler)
                with self.assertRaises(LLMError):
                    asyncio.run(gateway.complete("JSON", {}))
                self.assertEqual(len(requests), 1)

    def test_response_byte_cap_and_prompt_cap(self):
        async def handler(request):
            return httpx.Response(200, content=b"x" * 200000)
        gateway, requests, _ = self.gateway(handler=handler)
        with self.assertRaises(LLMError):
            asyncio.run(gateway.complete("JSON", {}))
        self.assertEqual(len(requests), 1)
        gateway, requests, _ = self.gateway()
        with self.assertRaises(LLMError):
            asyncio.run(gateway.complete("JSON", {"text": "x" * 20000}))
        self.assertEqual(len(requests), 0)

    def test_missing_environment_and_unknown_model_fail_without_http(self):
        base = "https://" + uuid.uuid4().hex + ".invalid/v1"
        key = uuid.uuid4().hex
        for environment in ({}, {"BUDGET": "1"}, {"BUDGET": "1", "LLM_BASE_URL": base},
                            {"BUDGET": "1", "LLM_API_KEY": key}):
            with self.subTest(environment_keys=list(environment)):
                with patch.dict(os.environ, environment, clear=True), self.assertRaises(LLMError):
                    LLMGateway.from_env(manager=BudgetManager(int(environment.get("BUDGET", "0"))))
        with self.assertRaises(LLMError):
            LLMGateway(base, key, model="not-permitted", manager=BudgetManager(1))

    def test_zero_budget_blocks_direct_and_workflow_requests(self):
        task = self.make_task()
        gateway, requests, _ = self.gateway(budget=0)
        with self.assertRaises(LLMError):
            asyncio.run(gateway.complete("JSON", {}))
        self.assertEqual(self.run_tasks(gateway), {"solved": 0, "skipped": 1, "failed": 0})
        gateway, requests, _ = self.gateway()
        self.assertEqual(self.run_tasks(gateway, budget=0)["skipped"], 1)
        with patch.dict(os.environ, {"BUDGET": "0"}), self.assertRaises(LLMError):
            asyncio.run(gateway.complete("JSON", {}))
        self.assertEqual(requests, [])
        self.assertFalse((task / "answer.txt").exists())

    def test_local_successes_do_not_call_llm(self):
        self.make_task("math", "A warehouse starts with 100 boxes. It ships 7 boxes per day for 3 days. How many boxes does it have at the end?")
        self.make_task("search", "In which year was Lena Noor born?", {"kb/a.md": "# Lena Noor\nBorn: 1977\n"})
        self.make_task("data_analysis", "The file sales.csv. What is the sum of units?", {"sales.csv": "units\n3\n"})
        code_instruction = (
            "Run `python3 test_engine.py` in the current directory; some tests fail. "
            "Fix the bugs in engine.py (do not edit the tests) until all tests pass. "
            "Work only inside the current directory. When you are done, write ONLY the word "
            "DONE to the file answer.txt in the current directory."
        )
        self.make_task("coding", code_instruction, {
            "engine.py": "def value():\n    return 3\n",
            "test_engine.py": "import unittest\nimport engine\nclass T(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(engine.value(), 3)\nif __name__ == '__main__':\n    unittest.main()\n",
        })
        gateway, requests, _ = self.gateway()
        self.assertEqual(self.run_tasks(gateway, trusted=True)["solved"], 4)
        self.assertEqual(requests, [])

    def test_new_math_formulation_is_computed_locally(self):
        task = self.make_task()
        gateway, requests, _ = self.gateway()
        self.assertEqual(self.run_tasks(gateway)["solved"], 1)
        self.assert_answer(task, "45")
        self.assertEqual(len(requests), 1)
        self.assertEqual(gateway.usage_records[0].completion_tokens, 20)

    def test_small_word_numbers_are_grounded_and_calculated(self):
        task = self.make_task(instruction="Start with ten coins. Add five coins. Double them.")
        plan = {
            "initial": {"value": "10", "quote": "ten coins"},
            "steps": [
                {"op": "add", "value": "5", "quote": "Add five coins"},
                {"op": "mul", "value": "2", "quote": "Double them"},
            ],
        }
        gateway, _, _ = self.gateway(plan)
        self.assertEqual(self.run_tasks(gateway)["solved"], 1)
        self.assert_answer(task, "30")

    def test_duplicate_operands_cannot_be_hidden_in_one_quote(self):
        task = self.make_task(instruction="Start with 10 coins. Add 5 and 5 coins.")
        plan = {"initial": MATH_PLAN["initial"], "steps": [
            {"op": "add", "value": "5", "quote": "Add 5 and 5 coins"},
        ]}
        gateway, _, _ = self.gateway(plan)
        self.assertEqual(self.run_tasks(gateway)["skipped"], 1)
        self.assertFalse((task / "answer.txt").exists())

    def test_bad_math_plans_cannot_execute_or_produce_answers(self):
        marker = self.root / "must_not_exist"
        cases = []
        plan = copy.deepcopy(MATH_PLAN)
        plan["steps"][0]["op"] = f"__import__('pathlib').Path({str(marker)!r}).touch()"
        cases.append(plan)
        plan = copy.deepcopy(MATH_PLAN)
        plan["initial"]["value"] = "999"
        cases.append(plan)
        plan = copy.deepcopy(MATH_PLAN)
        plan["steps"][0]["op"] = "mul"
        cases.append(plan)
        plan = copy.deepcopy(MATH_PLAN)
        plan["steps"] = plan["steps"][:1]
        cases.extend([plan, {"answer": "45"}, {"code": "raise RuntimeError('must not execute')"}])
        for index, bad in enumerate(cases):
            with self.subTest(index=index):
                root = self.root / str(index)
                root.mkdir()
                (root / "task.json").write_text(json.dumps({"task_type": "math", "reward": 1, "instruction": MATH}))
                gateway, requests, _ = self.gateway(bad)
                with patch("subprocess.Popen", side_effect=AssertionError("must not execute")):
                    self.assertEqual(self.run_tasks(gateway)["solved"], 0)
                self.assertFalse((root / "answer.txt").exists())
        self.assertFalse(marker.exists())

    def test_search_uses_only_relevant_facts_and_validates_source(self):
        files = {"kb/person.md": "# Lena Noor\nEmployer: Arc Studio\nBorn: 1977\n",
                 "kb/company.md": "# Arc Studio\nHeadquarters: Faro\n"}
        files.update({f"kb/unused_{i}.md": f"# Unrelated Person {i}\nBorn: 1999\n" for i in range(100)})
        task = self.make_task("search", SEARCH, files)
        gateway, requests, _ = self.gateway(SEARCH_PLAN)
        self.assertEqual(self.run_tasks(gateway)["solved"], 1)
        self.assert_answer(task, "Faro")
        payload = json.loads(json.loads(requests[0].content)["messages"][1]["content"])
        self.assertEqual(set(payload["records"]), {"lena noor", "arc studio"})
        self.assertNotIn("Unrelated", json.dumps(payload))

    def test_incorrect_search_answer_or_omitted_relation_is_rejected(self):
        for change in ({"answer": "Invented City"}, {"entity": "Other Person"}, {"relations": []}, {"field": "born"}):
            task = self.make_task("search", SEARCH, {
                "kb/a.md": "# Lena Noor\nEmployer: Arc Studio\nHeadquarters: Wrong Root City\n",
                "kb/b.md": "# Arc Studio\nHeadquarters: Faro\n",
            })
            gateway, _, _ = self.gateway({**SEARCH_PLAN, **change})
            self.assertEqual(self.run_tasks(gateway)["solved"], 0)
            self.assertFalse((task / "answer.txt").exists())

    def test_missing_or_conflicting_evidence_causes_no_request(self):
        self.make_task("search", SEARCH)
        self.make_task("search", SEARCH, {"kb/a.md": "# Lena Noor\nBorn: 1977\n"})
        self.make_task("search", SEARCH, {"kb/a.md": "# Lena Noor\nEmployer: A\nEmployer: B\n"})
        self.make_task("data_analysis", DATA)
        self.make_task("data_analysis", DATA, {"sales.csv": "product,units\ngear\n"})
        gateway, requests, _ = self.gateway()
        self.assertEqual(self.run_tasks(gateway), {"solved": 0, "skipped": 5, "failed": 0})
        self.assertEqual(requests, [])

    def test_new_data_question_sends_schema_not_rows(self):
        task = self.make_task("data_analysis", DATA, {
            "sales.csv": "product,month,units\ngear,4,123456\ngear,5,7\nother,4,100\n",
        })
        gateway, requests, _ = self.gateway(DATA_PLAN)
        self.assertEqual(self.run_tasks(gateway)["solved"], 1)
        self.assert_answer(task, "123463")
        body = json.loads(requests[0].content)
        self.assertNotIn("123456", json.dumps(body))
        self.assertEqual(json.loads(body["messages"][1]["content"])["columns"], ["product", "month", "units"])

    def test_data_plural_range_and_inclusive_numeric_comparison(self):
        question = "Get total units from sales.csv for product 'gear' in months 4 through 5 inclusive."
        task = self.make_task("data_analysis", question, {
            "sales.csv": "product,month,units\ngear,4,5\ngear,5,7\ngear,6,100\n",
        })
        plan = copy.deepcopy(DATA_PLAN)
        for condition in plan["filters"][1:]:
            condition["quote"] = "months 4 through 5 inclusive"
        gateway, _, _ = self.gateway(plan)
        self.assertEqual(self.run_tasks(gateway)["solved"], 1)
        self.assert_answer(task, "12")

    def test_inclusive_comparison_cannot_be_made_exclusive(self):
        task = self.make_task("data_analysis", "Calculate total units in sales.csv where units at least 5.", {
            "sales.csv": "units\n4\n5\n6\n",
        })
        plan = {"file": "sales.csv", "operation": "sum", "column": "units", "filters": [
            {"column": "units", "op": ">", "type": "number", "value": "5", "quote": "units at least 5"},
        ]}
        gateway, _, _ = self.gateway(plan)
        self.assertEqual(self.run_tasks(gateway)["solved"], 0)
        self.assertFalse((task / "answer.txt").exists())
        plan["filters"][0]["op"] = ">="
        gateway, _, _ = self.gateway(plan)
        self.assertEqual(self.run_tasks(gateway)["solved"], 1)
        self.assert_answer(task, "11")

    def test_native_corrupted_data_does_not_trigger_llm(self):
        self.make_task("data_analysis", "The file sales.csv. What is the sum of units?", {
            "sales.csv": "units\nbad-number\n",
        })
        gateway, requests, _ = self.gateway(DATA_PLAN)
        self.assertEqual(self.run_tasks(gateway)["skipped"], 1)
        self.assertEqual(requests, [])

    def test_invalid_data_plans_and_missing_conditions_are_rejected(self):
        changes = [
            {"filters": []}, {"filters": DATA_PLAN["filters"][:1]},
            {"operation": "max"}, {"column": "price"}, {"file": "../outside.csv"},
            {"sql": "DROP TABLE sales"}, {"python": "raise RuntimeError('do not execute')"},
        ]
        for change in changes:
            task = self.make_task("data_analysis", DATA, {"sales.csv": "product,month,units\ngear,4,5\n"})
            gateway, _, _ = self.gateway({**DATA_PLAN, **change})
            self.assertEqual(self.run_tasks(gateway)["solved"], 0)
            self.assertFalse((task / "answer.txt").exists())

    def test_invalid_model_json_is_not_retried(self):
        task = self.make_task()
        async def handler(request):
            return httpx.Response(200, json=envelope("not JSON"))
        gateway, requests, _ = self.gateway(handler=handler)
        self.assertEqual(self.run_tasks(gateway)["skipped"], 1)
        self.assertEqual(len(requests), 1)
        self.assertFalse((task / "answer.txt").exists())

    def test_existing_answer_and_disabled_fallback_do_not_spend(self):
        task = self.make_task()
        (task / "answer.txt").write_text("45\n")
        gateway, requests, _ = self.gateway()
        self.assertEqual(self.run_tasks(gateway)["skipped"], 1)
        self.assertEqual(requests, [])
        self.assert_answer(task, "45")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(run_tasks(self.root))["skipped"], 1)

    def test_untrusted_coding_does_not_call_llm_or_execute(self):
        task = self.make_task("coding", "Repair an arbitrary program.", {"engine.py": "raise RuntimeError()"})
        gateway, requests, _ = self.gateway({"source": "arbitrary code"})
        with patch("subprocess.Popen", side_effect=AssertionError("must not execute")):
            self.assertEqual(self.run_tasks(gateway)["skipped"], 1)
        self.assertEqual(requests, [])
        self.assertFalse((task / "answer.txt").exists())

    def test_coding_proposal_is_review_only_even_when_python_is_valid(self):
        instruction = (
            "Run `python3 test_engine.py` in the current directory; some tests fail. "
            "Fix the bugs in engine.py (do not edit the tests) until all tests pass. "
            "Work only inside the current directory. When you are done, write ONLY the word "
            "DONE to the file answer.txt in the current directory."
        )
        task = self.make_task("coding", instruction, {"engine.py": "def change(s):\n    return s.upper()\n", "test_engine.py": "# trusted test context\n"})
        before = {path: path.read_bytes() for path in task.rglob("*") if path.is_file()}
        proposal = {"file": "engine.py", "source": "def change(s):\n    return s.lower()\n"}
        gateway, requests, _ = self.gateway(proposal)
        with patch("subprocess.Popen", side_effect=AssertionError("must not execute")):
            result = asyncio.run(propose_coding(gateway, instruction, task, trusted=True))
        self.assertEqual(result, proposal)
        self.assertEqual(len(requests), 1)
        self.assertEqual({path: path.read_bytes() for path in task.rglob("*") if path.is_file()}, before)
        self.assertFalse((task / "answer.txt").exists())
        with self.assertRaises(InvalidPlan):
            asyncio.run(propose_coding(gateway, instruction, task))

    def test_workflow_really_controls_fallback(self):
        gateway, requests, _ = self.gateway()
        async def invoke():
            return await build_workflow(llm=gateway).invoke(
                {"task_type": "math", "instruction": MATH}, create_workflow_session()
            )
        output = asyncio.run(invoke())
        self.assertEqual(output.state, WorkflowExecutionState.COMPLETED)
        self.assertEqual(output.result["output"]["answer"], "45")
        self.assertEqual(len(requests), 1)


class LocalHTTPTests(unittest.TestCase):
    def test_local_server_exercises_real_cli_without_external_api(self):
        with local_server(json.dumps(MATH_PLAN)) as server, tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "math_local"
            task.mkdir()
            (task / "task.json").write_text(json.dumps({"task_type": "math", "reward": 1, "instruction": MATH}))
            result = subprocess.run(
                [sys.executable, "-B", "run_agent.py", "--tasks-dir", temporary, "--enable-llm"],
                cwd=ROOT, env={
                    "PATH": os.defpath, "BUDGET": "100000", "LLM_BASE_URL": server["base"],
                    "LLM_API_KEY": server["key"],
                }, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((task / "answer.txt").read_bytes(), b"45\n")
            self.assertIn("llm_calls=1", result.stdout)
            self.assertEqual(len(server["requests"]), 1)
            self.assertNotIn(server["key"], result.stdout + result.stderr)

    def test_same_contract_is_compatible_with_installed_openjiuwen_client(self):
        from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig, OpenAIModelClient
        with local_server("local compatibility check") as server, patch.dict(os.environ, {}, clear=True):
            client = OpenAIModelClient(
                model_config=ModelRequestConfig(model=DEFAULT_MODEL, max_tokens=64),
                model_client_config=ModelClientConfig(
                    client_provider="OpenAI", api_base=server["base"], api_key=server["key"],
                    timeout=2, stream_first_chunk_timeout=None, stream_idle_timeout=None,
                    max_retries=0, use_shared_llm_http_client=False,
                ),
            )
            # Forward a reserved gateway request through the actual SDK. The
            # SDK sees the exact model/messages/output cap that was reserved.
            observed = []
            async def sdk_transport(request):
                body = json.loads(request.content)
                response = await client.invoke(body["messages"], timeout=2)
                observed.append(response)
                return httpx.Response(200, json=envelope(response.content))

            manager = BudgetManager(100000)
            gateway = LLMGateway(server["base"], server["key"], manager=manager,
                                 max_tokens=64, transport=httpx.MockTransport(sdk_transport))
            response = asyncio.run(gateway.complete("Local check", {"check": "compatibility"}))
            self.assertEqual(response.content, "local compatibility check")
            self.assertEqual(observed[0].usage_metadata.input_tokens, 100)
            self.assertEqual(manager.snapshot()["spent"], 30)
            self.assertEqual(len(server["requests"]), 1)


if __name__ == "__main__":
    unittest.main()
