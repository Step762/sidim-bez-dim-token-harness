"""Autonomous Coding through the normal workflow with synthetic task inputs."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import coding_static
from harness import run_tasks
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

    def task(self, name, source, assertions):
        task = self.tasks / name
        workspace = task / "workspace"
        workspace.mkdir(parents=True)
        task.joinpath("task.json").write_text(json.dumps({
            "task_type": "coding", "reward": 1, "instruction": instruction("repair_me.py", "checks.py"),
        }), encoding="utf-8")
        source_file, test_file = workspace / "repair_me.py", workspace / "checks.py"
        source_file.write_bytes(source.encode("utf-8"))
        test_file.write_text(_suite("repair_me", assertions), encoding="utf-8")
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
