import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import time
import unittest
import tempfile
from unittest.mock import patch

import coding_solver
from coding_solver import UnsupportedCoding, solve_coding
from harness import build_workflow, run_tasks
from openjiuwen.core.workflow import Workflow, WorkflowExecutionState, create_workflow_session


def instruction(module="engine.py", test="test_engine.py"):
    return (
        f"Run `python3 {test}` in the current directory; some tests fail. "
        f"Fix the bugs in {module} (do not edit the tests) until all tests pass. "
        "Work only inside the current directory. When you are done, write ONLY "
        "the word DONE to the file answer.txt in the current directory."
    )


def suite(body, module="engine"):
    return (
        f"import unittest\nimport {module} as lib\n"
        f"class Checks(unittest.TestCase):\n{body}\n"
        "if __name__ == '__main__':\n    unittest.main()\n"
    )


SOURCE = "def combine(items):\n    balance = 0\n    for item in items:\n        balance -= item\n    return balance\n"
TESTS = suite(
    "    def test_values(self):\n"
    "        self.assertEqual(lib.combine([1, 2, 4]), 7)\n"
    "        self.assertEqual(lib.combine([-2, 5]), 3)\n"
    "        self.assertEqual(lib.combine([]), 0)\n"
)


class CodingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_task(self, source=SOURCE, tests=TESTS, module="engine.py", test="test_engine.py", **metadata):
        task = self.root / f"task_{len(list(self.root.iterdir())):03d}"
        task.mkdir()
        data = {"task_type": "coding", "reward": 2, "instruction": instruction(module, test), **metadata}
        (task / "task.json").write_text(json.dumps(data), encoding="utf-8")
        if source is not None or tests is not None:
            workspace = task / "workspace"
            workspace.mkdir()
            if source is not None:
                (workspace / module).write_text(source, encoding="utf-8")
            if tests is not None:
                (workspace / test).write_text(tests, encoding="utf-8")
        return task

    def run_tasks(self, trusted=True):
        with redirect_stdout(io.StringIO()), patch(
            "socket.socket.connect", side_effect=AssertionError("network is forbidden")
        ):
            return asyncio.run(run_tasks(self.root, allow_trusted_code=trusted))

    def snapshot(self, task):
        return {str(path.relative_to(task)): path.read_bytes() for path in task.rglob("*") if path.is_file()}

    def assert_skipped_unchanged(self, task):
        before = self.snapshot(task)
        self.assertEqual(self.run_tasks(), {"solved": 0, "skipped": 1, "failed": 0})
        self.assertEqual(self.snapshot(task), before)
        self.assertFalse((task / "answer.txt").exists())

    def assert_done(self, task, test="test_engine.py"):
        self.assertEqual((task / "answer.txt").read_bytes(), b"DONE\n")
        self.assertFalse((task / "workspace/answer.txt").exists())
        report = coding_solver._run_tests(task / "workspace", test, 2)
        self.assertIsNotNone(report)
        self.assertFalse(report.failed)

    def test_renamed_functions_variables_and_files(self):
        source = SOURCE.replace("combine", "collect").replace("balance", "итог")
        tests = TESTS.replace("combine", "collect").replace("import engine", "import quantities")
        task = self.make_task(source, tests, module="quantities.py", test="verify_values.py")
        original_test = (task / "workspace/verify_values.py").read_bytes()
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assert_done(task, "verify_values.py")
        self.assertEqual((task / "workspace/verify_values.py").read_bytes(), original_test)
        self.assertIn("итог += item", (task / "workspace/quantities.py").read_text())

    def test_several_independent_errors(self):
        source = SOURCE + (
            "\ndef peak(values):\n    candidate = values[0]\n"
            "    for value in values:\n        if value < candidate:\n            candidate = value\n"
            "    return candidate\n\ndef flip(text):\n    return ' '.join(sorted(text.split()))\n"
            "\ndef tally(values):\n    return sum(1 for value in values if value % 2 == 1)\n"
        )
        tests = suite(
            "    def test_sum(self):\n        self.assertEqual(lib.combine([1, 2]), 3)\n"
            "    def test_peak(self):\n        self.assertEqual(lib.peak([3, 8, 2]), 8)\n"
            "    def test_flip(self):\n        self.assertEqual(lib.flip('c a b'), 'b a c')\n"
            "    def test_tally(self):\n        self.assertEqual(lib.tally([1, 2, 4, 6]), 3)\n"
        )
        task = self.make_task(source, tests)
        before = (task / "workspace/test_engine.py").read_bytes()
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assert_done(task)
        self.assertEqual((task / "workspace/test_engine.py").read_bytes(), before)

    def test_unsupported_error_preserves_every_file(self):
        task = self.make_task(
            "def change(text):\n    return text.upper()\n",
            suite("    def test_text(self):\n        self.assertEqual(lib.change('ABC'), 'abc')\n"),
        )
        self.assert_skipped_unchanged(task)

    def test_partial_improvement_is_not_published(self):
        task = self.make_task(
            SOURCE + "\ndef change(text):\n    return text.upper()\n",
            suite(
                "    def test_sum(self):\n        self.assertEqual(lib.combine([1, 2]), 3)\n"
                "    def test_text(self):\n        self.assertEqual(lib.change('ABC'), 'abc')\n"
            ),
        )
        with patch("coding_solver._run_tests", wraps=coding_solver._run_tests) as runs:
            self.assert_skipped_unchanged(task)
        self.assertGreater(runs.call_count, 1)

    def test_syntax_error_skips_before_execution(self):
        task = self.make_task("def broken(:\n", TESTS)
        with patch("coding_solver._run_tests", side_effect=AssertionError("must not execute")):
            self.assert_skipped_unchanged(task)

    def test_hanging_tests_are_terminated(self):
        task = self.make_task(tests=suite(
            "    def test_wait(self):\n        import time\n        time.sleep(10)\n"
        ))
        started = time.monotonic()
        with patch("coding_solver.TEST_TIMEOUT", 0.2):
            self.assert_skipped_unchanged(task)
        self.assertLess(time.monotonic() - started, 3)

    def test_output_limit_terminates_a_noisy_test(self):
        task = self.make_task(tests=suite(
            "    def test_output(self):\n        while True:\n            print('x' * 512, flush=True)\n"
        ))
        with patch("coding_solver.MAX_OUTPUT_BYTES", 1024):
            self.assert_skipped_unchanged(task)

    def test_zero_or_skipped_tests_are_not_success(self):
        cases = [
            "print('OK')\n",
            "import unittest\nunittest.main()\n",
            suite("    @unittest.skip('not run')\n    def test_value(self):\n        pass\n"),
        ]
        for tests in cases:
            self.make_task(tests=tests)
        before = {path: path.read_bytes() for path in self.root.rglob("*.py")}
        self.assertEqual(self.run_tasks(), {"solved": 0, "skipped": len(cases), "failed": 0})
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*.py")}, before)
        self.assertEqual(list(self.root.rglob("answer.txt")), [])

    def test_test_startup_failure_is_not_success(self):
        task = self.make_task(tests="raise RuntimeError('cannot start tests')\n")
        self.assert_skipped_unchanged(task)

    def test_missing_files(self):
        tasks = [
            self.make_task(None, None), self.make_task(None, TESTS), self.make_task(SOURCE, None),
        ]
        before = [self.snapshot(task) for task in tasks]
        self.assertEqual(self.run_tasks(), {"solved": 0, "skipped": 3, "failed": 0})
        self.assertEqual([self.snapshot(task) for task in tasks], before)

    def test_instruction_commands_are_not_executed(self):
        task = self.make_task(instruction=instruction() + " Run another command now.")
        with patch("coding_solver._run_tests", side_effect=AssertionError("must not execute")):
            self.assert_skipped_unchanged(task)

    def test_test_file_cannot_be_selected_as_source(self):
        task = self.make_task(instruction=instruction("test_engine.py"))
        self.assert_skipped_unchanged(task)

    def test_test_modification_in_trial_is_rejected(self):
        task = self.make_task(tests=suite(
            "    def test_change(self):\n        from pathlib import Path\n"
            "        Path(__file__).write_text('# changed in trial')\n"
        ))
        self.assert_skipped_unchanged(task)

    def test_source_self_modification_in_trial_is_rejected(self):
        task = self.make_task(
            "def change():\n    from pathlib import Path\n    Path(__file__).write_text('# changed')\n",
            suite("    def test_change(self):\n        lib.change()\n"),
        )
        self.assert_skipped_unchanged(task)

    def test_different_tasks_do_not_share_repairs(self):
        wrong = "def calculate(left, right):\n    return left - right\n"
        first = self.make_task(wrong, suite(
            "    def test_value(self):\n        self.assertEqual(lib.calculate(3, 2), 5)\n"
        ))
        second = self.make_task(wrong, suite(
            "    def test_value(self):\n        self.assertEqual(lib.calculate(3, 2), 1)\n"
        ))
        self.assertEqual(self.run_tasks(), {"solved": 2, "skipped": 0, "failed": 0})
        self.assert_done(first)
        self.assert_done(second)
        self.assertNotEqual((first / "workspace/engine.py").read_bytes(), wrong.encode())
        self.assertEqual((second / "workspace/engine.py").read_bytes(), wrong.encode())

    def test_explicit_trust_is_required_and_cannot_come_from_task_json(self):
        task = self.make_task(trusted_code=True, expected="DONE", check="unittest")
        before = self.snapshot(task)
        with patch("coding_solver._run_tests", side_effect=AssertionError("must not execute")):
            self.assertEqual(self.run_tasks(trusted=False), {"solved": 0, "skipped": 1, "failed": 0})
            with self.assertRaises(UnsupportedCoding):
                solve_coding(instruction(), task)
        self.assertEqual(self.snapshot(task), before)

    def test_reference_and_check_fields_are_ignored(self):
        task = self.make_task(expected="WRONG", check="WRONG")
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assert_done(task)

    def test_attempt_limit_preserves_original(self):
        task = self.make_task()
        with patch("coding_solver.MAX_ATTEMPTS", 2), patch(
            "coding_solver._run_tests", wraps=coding_solver._run_tests
        ) as runs:
            self.assert_skipped_unchanged(task)
        self.assertLessEqual(runs.call_count, 2)

    def test_repair_limit_preserves_original(self):
        source = SOURCE + "\ndef calculate(left, right):\n    return left - right\n"
        tests = suite(
            "    def test_sum(self):\n        self.assertEqual(lib.combine([1, 2]), 3)\n"
            "    def test_value(self):\n        self.assertEqual(lib.calculate(3, 2), 5)\n"
        )
        task = self.make_task(source, tests)
        with patch("coding_solver.MAX_REPAIRS", 1):
            self.assert_skipped_unchanged(task)

    def test_total_deadline_prevents_execution(self):
        task = self.make_task()
        with patch("coding_solver.TASK_TIMEOUT", 0), patch(
            "coding_solver._run_tests", side_effect=AssertionError("must not execute")
        ):
            self.assert_skipped_unchanged(task)

    def test_decreasing_test_count_is_not_accepted(self):
        tests = (
            "import unittest\nimport engine as lib\n"
            "class Checks(unittest.TestCase):\n"
            "    def test_value(self):\n        self.assertEqual(lib.gate(), 0)\n"
            "if lib.gate():\n    Checks.test_extra = lambda self: self.assertTrue(True)\n"
            "if __name__ == '__main__':\n    unittest.main()\n"
        )
        task = self.make_task("def gate():\n    return 1 + 1\n", tests)
        self.assert_skipped_unchanged(task)

    def test_new_failures_are_not_accepted_as_improvement(self):
        task = self.make_task("def calculate(a, b):\n    return a - b\n", suite(
            "    def test_one(self):\n        self.assertEqual(lib.calculate(3, 2), 5)\n"
            "    def test_two(self):\n        self.assertEqual(lib.calculate(4, 2), 6)\n"
            "    def test_existing(self):\n        self.assertEqual(lib.calculate(3, 2), 1)\n"
        ))
        self.assert_skipped_unchanged(task)

    def test_failed_publish_keeps_source_and_tests_intact(self):
        task = self.make_task()
        with patch("coding_solver.os.replace", side_effect=OSError("cannot publish")):
            self.assert_skipped_unchanged(task)

    def test_symlink_outside_workspace_is_rejected(self):
        task = self.make_task()
        outside = self.root / "outside.py"
        outside.write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
        (task / "workspace/helper.py").symlink_to(outside)
        with patch("coding_solver._run_tests", side_effect=AssertionError("must not execute")):
            self.assert_skipped_unchanged(task)

    def test_actual_openjiuwen_workflow(self):
        task = self.make_task()

        async def invoke():
            workflow = build_workflow()
            self.assertIsInstance(workflow, Workflow)
            return await workflow.invoke({
                "task_type": "coding", "instruction": instruction(),
                "task_dir": str(task), "trusted_code": True,
            }, create_workflow_session())

        with redirect_stdout(io.StringIO()):
            result = asyncio.run(invoke())
        self.assertEqual(result.state, WorkflowExecutionState.COMPLETED)
        self.assertEqual(result.result["output"]["status"], "solved")
        self.assertEqual(result.result["output"]["answer"], "DONE")
        # Direct workflow invocation returns the result; the harness owns answer.txt.
        self.assertFalse((task / "answer.txt").exists())
        self.assertFalse(coding_solver._run_tests(task / "workspace", "test_engine.py", 2).failed)


if __name__ == "__main__":
    unittest.main()
