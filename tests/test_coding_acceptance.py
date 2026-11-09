"""Independent visible assertions exercise the verifier with synthetic inputs."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from diagnostics.coding_acceptance import check_visible_examples
from diagnostics.coding_research import review_task


SOURCE = "def combine(a, b):\n    return a/b\n"
EDIT = {
    "file": "workspace/engine.py", "line": 2, "column": 13, "old": "/", "new": "*",
    "source_sha256": hashlib.sha256(SOURCE.encode("utf-8")).hexdigest(),
}
TESTS = """import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.combine(*(6, 2)), 12)
        self.assertEqual(lib.combine(*(-5, 0)), 0)
        self.assertEqual(lib.combine(*(7, 1)), 7)

if __name__ == "__main__":
    unittest.main()
"""


class VisibleAcceptanceTests(unittest.TestCase):
    def test_visible_assertions_are_compared_independently(self):
        with (
            patch("subprocess.Popen", side_effect=AssertionError("task execution forbidden")),
            patch("diagnostics.static_coding.review_source", side_effect=AssertionError("generator called")),
        ):
            report = check_visible_examples(SOURCE, TESTS, EDIT)
        self.assertEqual(report, {
            "status": "visible_assertions_checked",
            "before_passed": 1,
            "after_passed": 3,
            "cases": [
                {"line": 6, "before_passed": False, "after_passed": True},
                {"line": 7, "before_passed": False, "after_passed": True},
                {"line": 8, "before_passed": True, "after_passed": True},
            ],
        })

    def test_valid_but_wrong_replacement_can_fail_visible_assertions(self):
        result = check_visible_examples(SOURCE, TESTS, {**EDIT, "new": "+"})
        self.assertEqual(result["status"], "visible_assertions_checked")
        self.assertEqual(result["after_passed"], 0)
        self.assertEqual(result["before_passed"], 1)

    def test_stale_and_non_source_edits_are_rejected(self):
        for edit in (
            None, {**EDIT, "line": True}, {**EDIT, "column": 12},
            {**EDIT, "new": "/"}, {**EDIT, "file": "workspace/test_engine.py"},
        ):
            with self.subTest(edit=edit):
                self.assertEqual(check_visible_examples(SOURCE, TESTS, edit)["status"], "invalid_patch")
        source = SOURCE.rstrip() + "  # /\n"
        edit = {
            **EDIT, "column": source.splitlines()[1].rindex("/") + 1,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        }
        self.assertEqual(check_visible_examples(source, TESTS, edit)["status"], "invalid_patch")

    def test_patch_rejects_changed_source_even_if_operator_has_not_moved(self):
        changed = SOURCE + "# changed since review\n"
        result = check_visible_examples(changed, TESTS, EDIT)
        self.assertEqual(result["status"], "invalid_patch")
        self.assertIn("hash", result["reason"])
        unbound = {key: value for key, value in EDIT.items() if key != "source_sha256"}
        self.assertEqual(check_visible_examples(SOURCE, TESTS, unbound)["status"], "invalid_patch")

    def test_unsupported_assertion_discards_all_partial_results(self):
        for expression in ("danger()", "True", "1.0", str(2**129)):
            with self.subTest(expression=expression):
                suite = TESTS.replace("*(7, 1)", f"*({expression}, 1)")
                result = check_visible_examples(SOURCE, suite, EDIT)
                self.assertEqual(result["status"], "unsupported")
                self.assertNotIn("cases", result)

    def test_extra_setup_imports_and_wrong_function_are_rejected(self):
        for suite in (
            "import os\n" + TESTS,
            TESTS.replace("    def test_examples", "    @decorator\n    def test_examples"),
            TESTS.replace("class Examples", "class Other"),
            TESTS.replace("lib.combine", "lib.different"),
            TESTS.replace("    unittest.main()", "    setup()"),
        ):
            with self.subTest(suite=suite):
                self.assertEqual(check_visible_examples(SOURCE, suite, EDIT)["status"], "unsupported")


class FreshArithmeticContractsTests(unittest.TestCase):
    def check_contract(self, name, source_operator, contract, examples):
        original = f"def {name}(first, second):\n    return first {source_operator} second\n"
        assertions = "\n".join(
            f"        self.assertEqual(lib.{name}(*({left}, {right})), {expected})"
            for left, right, expected in examples
        )
        start = TESTS.index("        self.assertEqual")
        end = TESTS.index("\n\nif __name__")
        suite = TESTS[:start] + assertions + TESTS[end:]
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)
            source = task / "workspace" / "engine.py"
            source.parent.mkdir()
            source.write_text(original, encoding="utf-8")
            review = review_task(task, Path("engine.py"), function=name, contract=contract)
            self.assertTrue(review["independently_verified_under_contract"])
            self.assertFalse(review["contest_solution_confirmed"])
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertFalse((task / "answer.txt").exists())
        return check_visible_examples(original, suite, review["patch"])

    def test_three_fresh_contracts_with_explicit_expected_values(self):
        for name, original, contract, examples in (
            ("credit", "-", "add", ((-7, -5, -12), (9, 0, 9), (11, -4, 7))),
            ("delta", "+", "subtract", ((-2, -9, 7), (1, 8, -7), (7, 0, 7))),
            ("scale", "/", "multiply", ((-9, -3, 27), (12, 0, 0), (1, 16, 16))),
        ):
            with self.subTest(contract=contract):
                report = self.check_contract(name, original, contract, examples)
                self.assertEqual(report["status"], "visible_assertions_checked")
                self.assertLess(report["before_passed"], len(examples))
                self.assertEqual(report["after_passed"], len(examples))

    def test_external_expected_values_catch_a_wrong_supplied_contract(self):
        report = self.check_contract(
            "delta", "/", "add", ((5, 2, 3), (0, -7, 7), (-4, -3, -1)),
        )
        self.assertEqual(report["status"], "visible_assertions_checked")
        self.assertEqual(report["after_passed"], 0)


class ThresholdAcceptanceTests(unittest.TestCase):
    def test_predicate_preserves_values_on_both_sides_and_fixes_equality(self):
        original = "def qualifies(n):\n    return n > -3\n"
        start = TESTS.index("        self.assertEqual")
        end = TESTS.index("\n\nif __name__")
        suite = TESTS[:start] + (
            "        self.assertEqual(lib.qualifies(*(-4,)), False)\n"
            "        self.assertEqual(lib.qualifies(*(-3,)), True)\n"
            "        self.assertEqual(lib.qualifies(*(-2,)), True)"
        ) + TESTS[end:]
        edit = {
            **EDIT, "column": 14, "old": ">", "new": ">=",
            "source_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        }
        report = check_visible_examples(original, suite, edit)
        self.assertEqual(report["status"], "visible_assertions_checked")
        self.assertEqual(report["before_passed"], 2)
        self.assertEqual(report["after_passed"], 3)
        self.assertEqual(
            [case["before_passed"] for case in report["cases"]], [True, False, True],
        )
        for unsupported in (suite.replace("True", "1"), suite.replace("-3,", "True,")):
            self.assertEqual(check_visible_examples(original, unsupported, edit)["status"], "unsupported")
