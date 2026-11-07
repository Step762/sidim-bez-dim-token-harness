"""Fail-closed regressions. Attack strings are DATA and are NEVER executed.

Only the explicitly identified benign fixtures in the trusted comparison run
Python. Kernel probes live in a separate, opt-in diagnostic script.
"""

import asyncio
import ast
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

import coding_isolation_probe
import coding_solver
from budget_manager import BudgetManager
from harness import build_workflow, run_tasks
from llm_gateway import LLMGateway
from openjiuwen.core.workflow import WorkflowExecutionState, create_workflow_session
from test_coding import instruction, suite


class CodingSecurityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="coding-security-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.outside = self.root / "outside"
        self.outside.write_text("synthetic canary; no real credentials")

    def task(self, name="untrusted", source="pass\n", tests="pass\n", **metadata):
        directory = self.tasks / name
        workspace = directory / "workspace"
        workspace.mkdir(parents=True)
        (directory / "task.json").write_text(json.dumps({
            "task_type": "coding", "reward": 100, "instruction": instruction(), **metadata,
        }))
        (workspace / "engine.py").write_text(source)
        (workspace / "test_engine.py").write_text(tests)
        return directory

    def assert_refused(self, directory, **options):
        math = self.tasks / "math"
        math.mkdir(exist_ok=True)
        (math / "task.json").write_text(json.dumps({
            "task_type": "math", "reward": 0, "instruction": "What is 2 + 2?",
        }))
        original = {path: path.read_bytes() for path in directory.rglob("*")
                    if not path.is_symlink() and stat.S_ISREG(path.lstat().st_mode)}
        canary = self.outside.read_bytes()
        stdout = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(stdout))
            guards = [stack.enter_context(patch(name, side_effect=AssertionError("untrusted execution")))
                      for name in ("subprocess.Popen", "socket.socket.connect",
                                   "coding_solver._run_tests", "coding_solver._snapshot",
                                   "coding_solver._publish", "coding_solver._mutants")]
            result = asyncio.run(run_tasks(self.tasks, **options))
            for guard in guards:
                guard.assert_not_called()
        self.assertEqual(result, {"solved": 1, "skipped": 1, "failed": 0})
        self.assertFalse((directory / "answer.txt").exists())
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertEqual(self.outside.read_bytes(), canary)
        self.assertEqual((math / "answer.txt").read_text(), "4\n")
        self.assertEqual(stat.S_IMODE((math / "answer.txt").stat().st_mode), 0o644)
        return stdout.getvalue()

    def test_neighbor_task_read_is_never_executed(self):
        self.assert_refused(self.task(source="from pathlib import Path\nprint(Path('../math/answer.txt').read_text())\n"))

    def test_environment_and_parent_secrets_are_never_read(self):
        payload = "import os\nprint(os.environ.get('LLM_API_KEY'))\nopen('/proc/1/environ').read()\n"
        with patch.dict(os.environ, {"LLM_API_KEY": "synthetic-parent-canary"}):
            output = self.assert_refused(self.task(source=payload, tests=payload))
        self.assertNotIn("synthetic-parent-canary", output)

    def test_agent_source_and_config_are_never_read(self):
        self.assert_refused(self.task(tests="open('/app/harness.py').read()\nopen('/app/.env').read()\n"))

    def test_write_outside_workspace_is_never_executed(self):
        payload = f"from pathlib import Path\nPath({str(self.outside)!r}).write_text('changed')\n"
        self.assert_refused(self.task(source=payload))

    def test_fork_and_detached_children_are_never_executed(self):
        self.assert_refused(self.task(tests="import os\nos.fork()\nos.setsid()\n"))

    def test_network_attempt_is_never_executed(self):
        self.assert_refused(self.task(source="import socket\nsocket.create_connection(('127.0.0.1', 1))\n"))

    def test_infinite_loop_is_never_executed(self):
        self.assert_refused(self.task(source="while True:\n    pass\n"))

    def test_memory_exhaustion_is_never_executed(self):
        self.assert_refused(self.task(tests="data = bytearray(2**40)\n"))

    def test_process_interruption_payload_is_never_executed(self):
        self.assert_refused(self.task(source="import os, signal\nos.kill(os.getppid(), signal.SIGKILL)\n"))

    def test_damaged_python_does_not_block_next_task(self):
        self.assert_refused(self.task(source="def broken(:\n", tests="\x00\n"))

    def test_external_symlink_is_not_followed(self):
        directory = self.task()
        (directory / "workspace/helper.py").symlink_to(self.outside)
        self.assert_refused(directory)

    def test_fifo_is_not_opened_or_executed(self):
        directory = self.task()
        os.mkfifo(directory / "workspace/helper.py")
        self.assert_refused(directory)

    def test_missing_workspace_does_not_block_next_task(self):
        directory = self.task()
        (directory / "workspace").rename(directory / "unused")
        self.assert_refused(directory)

    def test_direct_workflow_keeps_its_own_trust_gate(self):
        directory = self.task()

        async def invoke():
            return await build_workflow().invoke({
                "task_type": "coding", "instruction": instruction(),
                "task_dir": str(directory), "trusted_code": False,
            }, create_workflow_session())

        with patch("harness.SOLVERS", {"coding": Mock(side_effect=AssertionError("must not run"))}):
            result = asyncio.run(invoke())
        self.assertEqual(result.state, WorkflowExecutionState.COMPLETED)
        self.assertEqual(result.result["output"]["status"], "skipped")
        self.assertFalse((directory / "answer.txt").exists())

    def test_task_metadata_environment_and_truthy_values_cannot_grant_trust(self):
        directory = self.task(trusted_code=True, isolated=True, sandbox_verified=True)
        with patch.dict(os.environ, {"TRUSTED_CODE": "1", "CODING_SANDBOX": "1"}):
            self.assert_refused(directory)
        with patch("coding_solver._snapshot", side_effect=AssertionError("must not read")):
            for value in (False, None, 1, "true", "--trusted-code"):
                with self.subTest(value=value), self.assertRaises(coding_solver.UnsupportedCoding):
                    coding_solver.solve_coding(instruction(), directory, trusted=value)

    def test_budget_or_configured_llm_cannot_enable_coding(self):
        directory = self.task()
        requests = []

        async def reject(request):
            requests.append(request)
            raise AssertionError("coding must not call a model")

        for budget in (0, 1, 100000):
            with self.subTest(budget=budget):
                manager = BudgetManager(budget)
                gateway = LLMGateway("https://unused.invalid/v1", "synthetic-key", manager=manager,
                                     transport=httpx.MockTransport(reject))
                self.assert_refused(directory, manager=manager, budget=budget, llm=gateway)
                self.assertEqual(manager.snapshot()["spent"], 0)
        self.assertEqual(requests, [])

    def test_failed_repair_publication_does_not_write_done(self):
        directory = self.task(source="def f(a, b):\n    return a - b\n")
        before = (directory / "workspace/engine.py").read_bytes()
        # Entire test execution is simulated; no unknown source reaches Python.
        reports = [coding_solver.TestReport(1, frozenset({"test"})),
                   coding_solver.TestReport(1, frozenset()), coding_solver.TestReport(1, frozenset())]
        with patch("coding_solver._run_tests", side_effect=reports), patch(
            "coding_solver.os.replace", side_effect=OSError("simulated publish error")
        ), patch("subprocess.Popen", side_effect=AssertionError("simulation only")):
            with redirect_stdout(io.StringIO()):
                result = asyncio.run(run_tasks(self.tasks, allow_trusted_code=True))
        self.assertEqual(result, {"solved": 0, "skipped": 1, "failed": 0})
        self.assertEqual((directory / "workspace/engine.py").read_bytes(), before)
        self.assertFalse((directory / "answer.txt").exists())
        self.assertEqual(list((directory / "workspace").glob(".coding-*")), [])

    def test_interrupted_wait_kills_the_owned_group_without_publishing(self):
        # No process is started or signalled: exercise the existing finally path.
        process = Mock(pid=987654321, stdout=io.BytesIO())
        selector = Mock()
        selector.select.side_effect = KeyboardInterrupt
        with patch("coding_solver.subprocess.Popen") as popen, patch(
            "coding_solver.selectors.DefaultSelector"
        ) as selectors, patch("coding_solver.os.killpg") as killpg:
            popen.return_value.__enter__.return_value = process
            selectors.return_value.__enter__.return_value = selector
            with self.assertRaises(KeyboardInterrupt):
                coding_solver._run_tests(self.root, "unused.py", 1)
            killpg.assert_called_once_with(process.pid, signal.SIGKILL)
            process.wait.assert_called_once()
        self.assertEqual(list(self.tasks.rglob("answer.txt")), [])

    def test_diverse_known_fixtures_compare_safe_and_trusted_modes(self):
        # These five source/test pairs are reviewed, benign fixtures, not attacks.
        fixtures = [
            ("def merge(a, b):\n    return a - b\n", "merge", [("(2, 3)", "5"), ("(-4, 7)", "3")]),
            ("def scale(a, b):\n    return a / b\n", "scale", [("(4, 2)", "8"), ("(-3, 4)", "-12")]),
            ("def smaller(a, b):\n    return a if a > b else b\n", "smaller", [("(4, 2)", "2"), ("(-3, 4)", "-3")]),
            ("def flip(xs):\n    return list(sorted(xs))\n", "flip", [("([2, 1, 3],)", "[3, 1, 2]"), ("([],)", "[]")]),
            ("def even(n):\n    return n % 2 != 0\n", "even", [("(2,)", "True"), ("(7,)", "False")]),
        ]
        originals = {}
        for index, (source, function, cases) in enumerate(fixtures):
            tests = suite("    def test_values(self):\n" + "".join(
                f"        self.assertEqual(lib.{function}(*{arguments}), {answer})\n"
                for arguments, answer in cases
            ))
            directory = self.task(f"fixture_{index}", source, tests)
            originals[directory] = (source, tests)
        with redirect_stdout(io.StringIO()), patch("subprocess.Popen", side_effect=AssertionError("untrusted execution")):
            safe = asyncio.run(run_tasks(self.tasks, manager=BudgetManager(0)))
        self.assertEqual(safe, {"solved": 0, "skipped": 5, "failed": 0})
        self.assertEqual(list(self.tasks.rglob("answer.txt")), [])
        with redirect_stdout(io.StringIO()), patch("socket.socket.connect", side_effect=AssertionError("no network")):
            trusted = asyncio.run(run_tasks(self.tasks, allow_trusted_code=True, manager=BudgetManager(0)))
        self.assertEqual(trusted, {"solved": 5, "skipped": 0, "failed": 0})
        for directory, (source, tests) in originals.items():
            self.assertEqual((directory / "answer.txt").read_text(), "DONE\n")
            self.assertEqual((directory / "workspace/test_engine.py").read_text(), tests)
            self.assertNotEqual((directory / "workspace/engine.py").read_text(), source)
            self.assertFalse(coding_solver._run_tests(directory / "workspace", "test_engine.py", 2).failed)

    def test_syntax_and_possible_mutants_are_not_proof_of_correctness(self):
        source = b"def combine(a, b):\n    return a - b\n"
        variants = [source, *coding_solver._mutants(source)]
        self.assertGreater(len(variants), 1)
        for variant in variants:
            ast.parse(variant)  # Multiple conflicting meanings are syntactically valid.
        self.assert_refused(self.task(source=source.decode()))

    def test_probe_never_claims_a_production_sandbox(self):
        with patch("coding_isolation_probe.platform.machine", return_value="unsupported"), patch(
            "coding_isolation_probe.run_child", side_effect=AssertionError("must not probe unknown ABI")
        ):
            result = coding_isolation_probe.audit()
        self.assertFalse(result["sandbox_confirmed"])
        self.assertFalse(result["task_code_executed"])
        self.assertIn("unsupported", result)


if __name__ == "__main__":
    unittest.main()
