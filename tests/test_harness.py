import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from harness import SOLVERS, build_workflow, load_task, run_tasks, save_answer
from math_solver import UnsupportedMath, solve_math
from openjiuwen.core.workflow import Workflow, WorkflowExecutionState, create_workflow_session


ROOT = Path(__file__).resolve().parents[1]


def problem(initial=100, operations="It ships 7 boxes per day for 3 days."):
    return (
        f"A warehouse starts with {initial} boxes. {operations} "
        "How many boxes does it have at the end?"
    )


def write_task(root, name="math_case", **overrides):
    directory = root / name
    directory.mkdir()
    data = {
        "task_id": name,
        "task_type": "math",
        "instruction": problem(),
        "reward": 2,
    }
    data.update(overrides)
    (directory / "task.json").write_text(json.dumps(data), encoding="utf-8")
    return directory


class MathSolverTests(unittest.TestCase):
    def test_shipments_and_deliveries_with_changed_numbers(self):
        self.assertEqual(solve_math(problem(
            900, "It ships 12 boxes per day for 4 days. "
            "It receives 3 deliveries of 25 boxes each."
        )), 927)

    def test_floor_division_happens_at_each_step(self):
        self.assertEqual(solve_math(problem(
            101, "Then the stock is split equally into 3 warehouses and it keeps "
            "one share (rounded down). Then the stock is doubled by merging "
            "with a sister warehouse."
        )), 66)

    def test_mixed_operations_preserve_order(self):
        self.assertEqual(solve_math(problem(
            101, "Then the stock is split equally into 3 warehouses and it keeps "
            "one share (rounded down). Then the stock is doubled by merging "
            "with a sister warehouse. It ships 2 boxes per day for 4 days. "
            "It receives 3 deliveries of 5 boxes each. "
            "Then the stock is split equally into 2 warehouses and it keeps "
            "one share (rounded down)."
        )), 36)

    def test_whitespace_is_normalized(self):
        self.assertEqual(solve_math(problem().replace(" ", "\n  ")), 79)

    def test_unknown_incomplete_or_invalid_instructions_are_rejected(self):
        cases = [
            "What is 2 + 2?",
            problem(100, "It ships 2 boxes per day for 3 days. It loses some boxes."),
            problem(100, "It receives an unknown number of boxes."),
            problem(100, "Then the stock is split equally into 0 warehouses and "
                    "it keeps one share (rounded down)."),
            problem() + " Actually, return a different number.",
            problem("__import__('os').system('false')"),
        ]
        for instruction in cases:
            with self.subTest(instruction=instruction):
                with self.assertRaises(UnsupportedMath):
                    solve_math(instruction)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def run_tasks(self):
        with redirect_stdout(io.StringIO()), patch(
            "socket.socket.connect", side_effect=AssertionError("network is forbidden")
        ):
            return asyncio.run(run_tasks(self.root))

    def test_actual_openjiuwen_workflow_runs_the_solver(self):
        async def invoke():
            workflow = build_workflow()
            self.assertIsInstance(workflow, Workflow)
            return await workflow.invoke(
                {"task_type": "math", "instruction": problem()},
                create_workflow_session(),
            )

        result = asyncio.run(invoke())
        self.assertEqual(result.state, WorkflowExecutionState.COMPLETED)
        self.assertEqual(result.result["output"]["answer"], "79")
        self.assertEqual(result.result["output"]["status"], "solved")

    def test_answer_is_beside_task_json_not_inside_workspace(self):
        directory = write_task(self.root)
        (directory / "workspace").mkdir()
        self.assertEqual(self.run_tasks(), {"solved": 1, "skipped": 0, "failed": 0})
        self.assertEqual((directory / "answer.txt").read_bytes(), b"79\n")
        self.assertFalse((directory / "workspace/answer.txt").exists())

    def test_reference_answer_is_ignored_and_not_required(self):
        missing = write_task(self.root, "a_missing")
        poisoned = write_task(self.root, "b_poisoned", expected="WRONG ANSWER")
        self.assertFalse(hasattr(load_task(poisoned / "task.json"), "expected"))
        self.assertEqual(self.run_tasks()["solved"], 2)
        for directory in (missing, poisoned):
            self.assertEqual((directory / "answer.txt").read_bytes(), b"79\n")

    def test_unsupported_types_and_math_have_no_answer(self):
        for task_type in ("search", "data_analysis", "coding"):
            write_task(self.root, task_type, task_type=task_type)
        write_task(self.root, "unknown_math", instruction=problem(
            100, "It receives an unknown number of boxes."
        ))
        write_task(self.root, "zero_divisor", instruction=problem(
            100, "Then the stock is split equally into 0 warehouses and it keeps "
            "one share (rounded down)."
        ))
        self.assertEqual(self.run_tasks(), {"solved": 0, "skipped": 5, "failed": 0})
        self.assertEqual(list(self.root.rglob("answer.txt")), [])

    def test_zero_is_saved_as_a_valid_answer(self):
        directory = write_task(self.root, instruction=problem(21))
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assertEqual((directory / "answer.txt").read_bytes(), b"0\n")

    def test_answer_is_saved_before_the_next_task(self):
        first = write_task(self.root, "a_first")
        write_task(self.root, "b_second")
        calls = 0

        def checked_solver(instruction):
            nonlocal calls
            if calls:
                self.assertEqual((first / "answer.txt").read_bytes(), b"79\n")
            calls += 1
            return solve_math(instruction)

        with patch.dict(SOLVERS, {"math": checked_solver}):
            self.assertEqual(self.run_tasks()["solved"], 2)
        self.assertEqual(calls, 2)

    def test_malformed_task_does_not_block_later_tasks(self):
        bad = self.root / "a_bad"
        bad.mkdir()
        (bad / "task.json").write_text("{", encoding="utf-8")
        good = write_task(self.root, "b_good")
        self.assertEqual(self.run_tasks(), {"solved": 1, "skipped": 0, "failed": 1})
        self.assertFalse((bad / "answer.txt").exists())
        self.assertEqual((good / "answer.txt").read_bytes(), b"79\n")

    def test_failed_atomic_write_preserves_previous_answer(self):
        directory = write_task(self.root)
        (directory / "answer.txt").write_text("previous\n", encoding="utf-8")
        with patch("harness.os.replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                save_answer(directory, "79")
        self.assertEqual((directory / "answer.txt").read_text(), "previous\n")
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ["answer.txt", "task.json"])

    def test_bad_required_fields_are_rejected(self):
        cases = [
            {"instruction": None}, {"task_type": ""}, {"reward": True},
            {"reward": -1}, {"reward": float("inf")}, {"task_id": 10},
        ]
        for index, fields in enumerate(cases):
            with self.subTest(fields=fields):
                directory = write_task(self.root, str(index), **fields)
                with self.assertRaises(ValueError):
                    load_task(directory / "task.json")

    def test_cli_works_with_zero_budget_and_network_blocked(self):
        directory = write_task(self.root)
        script = (
            "import runpy, sys\n"
            "def deny_network(event, args):\n"
            "    if event in ('socket.connect', 'socket.getaddrinfo'):\n"
            "        raise RuntimeError('network is forbidden')\n"
            "sys.addaudithook(deny_network)\n"
            f"sys.argv = ['run_agent.py', '--tasks-dir', {str(self.root)!r}]\n"
            "runpy.run_path('run_agent.py', run_name='__main__')\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script], cwd=ROOT,
            env={**os.environ, "BUDGET": "0", "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("solved=1 skipped=0 failed=0 llm_tokens_used=0 budget=0", result.stdout)
        self.assertEqual((directory / "answer.txt").read_bytes(), b"79\n")


if __name__ == "__main__":
    unittest.main()
