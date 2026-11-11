"""Autonomous Coding through the normal workflow with synthetic task inputs."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import coding_static
from harness import run_tasks, save_answer
from test_coding import instruction


def _suite(module, assertion_lines):
    methods = "".join(
        f"    def test_case_{index}(self):\n        {assertion}\n"
        for index, assertion in enumerate(assertion_lines, 1)
    )
    return (
        f"import unittest\nimport {module} as subject\n\n"
        f"class DifferentChecks(unittest.TestCase):\n{methods}\n"
        'if __name__ == "__main__":\n    unittest.main()\n'
    )


class StaticCodingWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tasks = Path(directory.name)

    def task(self, name, source, assertions, *, module="repair_me"):
        task = self.tasks / name
        workspace = task / "workspace"
        workspace.mkdir(parents=True)
        task.joinpath("task.json").write_text(json.dumps({
            "task_type": "coding", "reward": 1, "instruction": instruction(f"{module}.py", "checks.py"),
        }), encoding="utf-8")
        source_file, test_file = workspace / f"{module}.py", workspace / "checks.py"
        source_file.write_bytes(source.encode("utf-8"))
        test_file.write_text(_suite(module, assertions), encoding="utf-8")
        return task, source_file, test_file

    def run_static(self):
        with patch("subprocess.Popen", side_effect=AssertionError("task execution forbidden")) as process:
            result = asyncio.run(run_tasks(self.tasks, allow_static_coding=True))
            process.assert_not_called()
        return result

    def test_unseen_tasks_are_repaired_without_per_task_options(self):
        cases = (
            ("arithmetic", "def combine(left, right):\r\n    return left-right\r\n", "-", "+",
             ["self.assertEqual(subject.combine(11, 4), 15)", "self.assertEqual(subject.combine(-8, 2), -6)"]),
            ("product", "def merge(x, y):\n    return x/y\n", "/", "*",
             ["self.assertEqual(subject.merge(5, 3), 15)", "self.assertEqual(subject.merge(-2, 7), -14)"]),
            ("predicate", "def marker(value):\n    return value % 3 != 1\n", "!=", "==",
             ["self.assertEqual(subject.marker(-2), True)", "self.assertEqual(subject.marker(5), False)"]),
            ("threshold", "def qualifies(value):\n    return value > -4\n", ">", ">=",
             ["self.assertEqual(subject.qualifies(-4), True)", "self.assertEqual(subject.qualifies(-5), False)"]),
            ("syntax", "def scale(value)\r\n    return value * -5\r\n", "value)", "value):",
             ["self.assertEqual(subject.scale(-3), 15)", "self.assertEqual(subject.scale(2), -10)"]),
            ("text", "def convert(upper):\n    return upper.upper()\n", ".upper", ".lower",
             ['self.assertEqual(subject.convert("Écho"), "écho")', 'self.assertEqual(subject.convert(""), "")']),
            ("mean", "def average(values):\n    return sum(values)//len(values)\n", "//", "/",
             ["self.assertEqual(subject.average([-4, -1]), -2.5)", "self.assertEqual(subject.average([2, 6]), 4)"]),
            ("endpoint", "def end(items):\n    return items[0]\n", "[0]", "[-1]",
             ["self.assertEqual(subject.end([8, 3, -4]), -4)", "self.assertEqual(subject.end([6]), 6)"]),
        )
        expected = []
        for name, source, before, after, assertions in cases:
            task, source_file, test_file = self.task(name, source, assertions)
            expected.append((task, source_file, source.replace(before, after).encode("utf-8"),
                             test_file, test_file.read_bytes()))
        self.assertEqual(self.run_static(), {"solved": len(cases), "skipped": 0, "failed": 0})
        for task, source_file, candidate, test_file, tests in expected:
            with self.subTest(task=task.name):
                self.assertEqual(source_file.read_bytes(), candidate)
                self.assertEqual(test_file.read_bytes(), tests)
                self.assertEqual(task.joinpath("answer.txt").read_text(), "DONE\n")
        self.assertEqual(self.run_static(), {"solved": len(cases), "skipped": 0, "failed": 0})

    def test_ambiguous_or_unsupported_tasks_never_publish(self):
        source = "def total(a,b):\n    return a-b\n"
        ambiguous = self.task("ambiguous", source, ["self.assertEqual(subject.total(2, 2), 4)"])
        dynamic = self.task("dynamic", source, ["self.assertEqual(subject.total(3, 7), compute())"])
        shadowed = self.task("shadowed", source, ["self.assertEqual(subject.total(3, 7), 10)"])
        shadowed[1].with_name("unittest.py").write_text("raise AssertionError('must never run')\n")
        active = self.task("active", "raise AssertionError('must never run')\n" + source,
                           ["self.assertEqual(subject.total(3, 7), 10)"])
        cases = (ambiguous, dynamic, shadowed, active)
        originals = [case[1].read_bytes() for case in cases]
        self.assertEqual(self.run_static(), {"solved": 0, "skipped": len(cases), "failed": 0})
        for (task, source_file, _), original in zip(cases, originals):
            self.assertEqual(source_file.read_bytes(), original)
            self.assertFalse(task.joinpath("answer.txt").exists())

    def test_direct_import_and_boolean_assertions(self):
        original = "def qualifies(value):\n    return value % 5 != 2\n"
        task, source, tests = self.task("direct", original, [
            "self.assertTrue(check(7))", "self.assertFalse(check(8))",
        ])
        direct_import = tests.read_text(encoding="utf-8").replace(
            "import repair_me as subject", "from repair_me import qualifies as check",
        )
        tests.write_text(direct_import, encoding="utf-8")
        self.assertEqual(self.run_static(), {"solved": 1, "skipped": 0, "failed": 0})
        self.assertEqual(source.read_text(encoding="utf-8"), original.replace("!=", "=="))
        self.assertEqual(task.joinpath("answer.txt").read_text(), "DONE\n")

    def test_test_method_parameter_cannot_shadow_import(self):
        original = "def total(a,b):\n    return a-b\n"
        cases = (
            ("module-alias", "import repair_me as self", "self.total(3, 7)"),
            ("function-alias", "from repair_me import total as self", "self(3, 7)"),
        )
        paths = []
        for name, import_statement, call in cases:
            task, source, tests = self.task(name, original, [f"self.assertEqual({call}, 10)"])
            suite = tests.read_text(encoding="utf-8")
            tests.write_text(suite.replace("import repair_me as subject", import_statement),
                             encoding="utf-8")
            paths.append((task, source))

        self.assertEqual(self.run_static(), {"solved": 0, "skipped": len(cases), "failed": 0})
        for task, source in paths:
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertFalse((task / "answer.txt").exists())

    def test_stale_visible_tests_prevent_publication(self):
        source = "def total(a,b):\n    return a-b\n"
        task, source_file, tests = self.task("changed", source, ["self.assertEqual(subject.total(3, 7), 10)"])
        select = coding_static._select_repair

        def change_tests(*inputs):
            candidate = select(*inputs)
            with tests.open("a", encoding="utf-8") as handle:
                handle.write("# changed during evaluation\n")
            return candidate

        with patch("coding_static._select_repair", side_effect=change_tests):
            self.assertEqual(self.run_static(), {"solved": 0, "skipped": 1, "failed": 0})
        self.assertEqual(source_file.read_text(encoding="utf-8"), source)
        self.assertFalse(task.joinpath("answer.txt").exists())

    def test_skipped_rerun_removes_previous_done(self):
        original = "def total(a,b):\n    return a-b\n"
        task, source, tests = self.task("rerun", original, ["self.assertEqual(subject.total(3, 7), 10)"])
        answer = task / "answer.txt"
        self.assertEqual(self.run_static(), {"solved": 1, "skipped": 0, "failed": 0})
        self.assertEqual(answer.read_text(encoding="utf-8"), "DONE\n")

        updated_tests = tests.read_text(encoding="utf-8").replace(", 10)", ", 11)")
        tests.write_text(updated_tests, encoding="utf-8")

        self.assertEqual(asyncio.run(run_tasks(self.tasks)), {"solved": 0, "skipped": 1, "failed": 0})
        self.assertEqual(answer.read_text(encoding="utf-8"), "DONE\n")
        self.assertEqual(self.run_static(), {"solved": 0, "skipped": 1, "failed": 0})
        self.assertFalse(answer.exists())
        self.assertEqual(source.read_text(encoding="utf-8"), original.replace("-", "+"))
        self.assertEqual(tests.read_text(encoding="utf-8"), updated_tests)

    def test_reset_removes_answer_link_without_touching_target(self):
        original = "def total(a,b):\n    return a-b\n"
        task, _, _ = self.task("linked-answer", original, ["self.assertEqual(subject.total(3, 7), 11)"])
        answer = task / "answer.txt"
        archived_answer = self.tasks / "archived-answer.txt"
        archived_answer.write_text("DONE\n", encoding="utf-8")
        answer.symlink_to(archived_answer)

        self.assertEqual(self.run_static(), {"solved": 0, "skipped": 1, "failed": 0})
        self.assertFalse(answer.is_symlink())
        self.assertFalse(answer.exists())
        self.assertEqual(archived_answer.read_text(encoding="utf-8"), "DONE\n")

    def test_tests_changed_at_answer_publication_leave_source_and_answer_untouched(self):
        original = "def total(a,b):\n    return a-b\n"
        task, source, tests = self.task("late-change", original, ["self.assertEqual(subject.total(3, 7), 10)"])

        def change_tests_before_save(directory, answer, before_replace):
            tests.write_text(tests.read_text(encoding="utf-8").replace(", 10)", ", 11)"), encoding="utf-8")
            return save_answer(directory, answer, before_replace)

        with patch("harness.save_answer", side_effect=change_tests_before_save):
            self.assertEqual(self.run_static(), {"solved": 0, "skipped": 1, "failed": 0})
        self.assertEqual(source.read_text(encoding="utf-8"), original)
        self.assertFalse(task.joinpath("answer.txt").exists())
        self.assertFalse(list(task.glob(".answer-*")))

    def test_answer_failure_after_source_publication_can_be_retried(self):
        original = "def total(a,b):\n    return a-b\n"
        task, source, _ = self.task("retry", original, ["self.assertEqual(subject.total(3, 7), 10)"])
        replace_file = os.replace

        def fail_answer(source_name, target_name, **options):
            if target_name == "answer.txt":
                raise OSError("synthetic answer publication error")
            return replace_file(source_name, target_name, **options)

        with patch("harness.os.replace", side_effect=fail_answer):
            self.assertEqual(self.run_static(), {"solved": 0, "skipped": 0, "failed": 1})
        self.assertEqual(source.read_text(encoding="utf-8"), original.replace("-", "+"))
        self.assertFalse(task.joinpath("answer.txt").exists())
        self.assertFalse(list(task.glob(".answer-*")))
        self.assertEqual(self.run_static(), {"solved": 1, "skipped": 0, "failed": 0})
        self.assertEqual(task.joinpath("answer.txt").read_text(), "DONE\n")

    def test_modified_staged_answer_is_rejected(self):
        original = "def total(a,b):\n    return a-b\n"
        task, _, _ = self.task("staged-answer", original, ["self.assertEqual(subject.total(3, 7), 10)"])
        publish = coding_static._publish

        def change_staged_answer(*args):
            installed = publish(*args)
            staged, = task.glob(".answer-*")
            staged.write_text("CHANGED\n", encoding="utf-8")
            return installed

        with patch("coding_static._publish", side_effect=change_staged_answer):
            self.assertEqual(self.run_static(), {"solved": 0, "skipped": 0, "failed": 1})
        self.assertFalse(task.joinpath("answer.txt").exists())
        self.assertFalse(list(task.glob(".answer-*")))

    def test_reserved_modules_cannot_receive_static_done(self):
        original = "def total(a,b):\n    return a-b\n"
        cases = [
            self.task(module, original, ["self.assertEqual(subject.total(3, 7), 10)"], module=module)
            for module in ("__main__", "sitecustomize", "usercustomize")
        ]
        self.assertEqual(self.run_static(), {"solved": 0, "skipped": len(cases), "failed": 0})
        for task, source, _ in cases:
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertFalse(task.joinpath("answer.txt").exists())

    def test_division_is_considered_when_checking_ambiguity(self):
        original = "def total(a,b):\n    return a*b\n"
        task, source, _ = self.task("divide-or-subtract", original, ["self.assertEqual(subject.total(4, 2), 2)"])
        unique, unique_source, _ = self.task("unique-division", original, [
            "self.assertEqual(subject.total(9, 3), 3)", "self.assertEqual(subject.total(12, 4), 3)",
        ])
        self.assertEqual(self.run_static(), {"solved": 1, "skipped": 1, "failed": 0})
        self.assertEqual(source.read_text(encoding="utf-8"), original)
        self.assertFalse(task.joinpath("answer.txt").exists())
        self.assertEqual(unique_source.read_text(encoding="utf-8"), original.replace("*", "/"))
        self.assertEqual(unique.joinpath("answer.txt").read_text(), "DONE\n")

    def test_passing_original_still_needs_distinguishing_checks(self):
        original = "def total(a,b):\n    return a-b\n"
        task, source, _ = self.task("passing-but-ambiguous", original, [
            "self.assertEqual(subject.total(5, 0), 5)",
        ])
        distinct, distinct_source, _ = self.task("passing-and-distinct", original, [
            "self.assertEqual(subject.total(7, 2), 5)",
        ])

        self.assertEqual(self.run_static(), {"solved": 1, "skipped": 1, "failed": 0})
        self.assertEqual(source.read_text(encoding="utf-8"), original)
        self.assertFalse((task / "answer.txt").exists())
        self.assertEqual(distinct_source.read_text(encoding="utf-8"), original)
        self.assertEqual((distinct / "answer.txt").read_text(encoding="utf-8"), "DONE\n")
