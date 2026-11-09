"""Independent visible assertions exercise the verifier with synthetic inputs."""

import unittest
from unittest.mock import patch

from diagnostics.coding_acceptance import check_visible_examples


SOURCE = "def combine(a, b):\n    return a/b\n"
EDIT = {"file": "workspace/engine.py", "line": 2, "column": 13, "old": "/", "new": "*"}
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
        edit = {**EDIT, "column": source.splitlines()[1].rindex("/") + 1}
        self.assertEqual(check_visible_examples(source, TESTS, edit)["status"], "invalid_patch")

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
