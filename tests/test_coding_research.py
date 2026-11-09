"""Exercise the review-only pipeline without running task source or tests."""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from diagnostics.coding_research import apply_missing_colon, apply_operator_edit, review_task, verify_candidate
from runtime_limits import OperationTimeout


SOURCE = "def product(left, right):\n    return left / right\n"
CANDIDATE = "def product(left, right):\n    return left * right\n"


class CodingResearchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.task = Path(directory.name)
        self.workspace = self.task / "workspace"
        self.workspace.mkdir()
        self.source = self.workspace / "engine.py"

    def review(self, *, source=Path("engine.py"), function="product", contract="multiply"):
        return review_task(self.task, source, function=function, contract=contract)

    def test_single_token_patch_preserves_layout_and_comments(self):
        original = "def product(слева, справа):\n    return (слева/справа)  # / is wrong\n"
        self.source.write_text(original, encoding="utf-8")
        test_source = self.workspace / "test_engine.py"
        test_source.write_text("raise AssertionError('task tests must not run')\n", encoding="utf-8")

        with patch("subprocess.Popen", side_effect=AssertionError("task execution forbidden")):
            report = self.review()

        self.assertEqual(report["status"], "verified_under_supplied_contract")
        self.assertTrue(report["independently_verified_under_contract"])
        self.assertFalse(report["contest_solution_confirmed"])
        edit = report["patch"]
        self.assertEqual(edit, {
            "file": "workspace/engine.py", "line": 2, "column": 18, "old": "/", "new": "*",
            "source_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        })
        lines = original.splitlines(keepends=True)
        row, column = edit["line"] - 1, edit["column"] - 1
        self.assertEqual(lines[row][column], edit["old"])
        lines[row] = lines[row][:column] + edit["new"] + lines[row][column + 1:]
        self.assertTrue(verify_candidate(original, "".join(lines), function="product", contract="multiply"))
        self.assertEqual(self.source.read_text(encoding="utf-8"), original)
        self.assertIn("task tests must not run", test_source.read_text(encoding="utf-8"))
        self.assertFalse((self.task / "answer.txt").exists())

    def test_independent_proof_for_all_supported_operations(self):
        original = "def result(first, second):\n    return first / second\n"
        for contract, operator in (("add", "+"), ("subtract", "-"), ("multiply", "*")):
            with self.subTest(contract=contract):
                candidate = original.replace("/", operator)
                self.assertTrue(verify_candidate(original, candidate, function="result", contract=contract))
                self.assertFalse(verify_candidate(candidate, candidate, function="result", contract=contract))

    def test_wrong_candidate_does_not_inherit_generator_claim(self):
        self.source.write_text(SOURCE, encoding="utf-8")
        with patch("diagnostics.coding_research.static_coding.review_source", return_value={
            "proposal_source": SOURCE.replace("/", "+"), "contract_proved": True,
        }):
            report = self.review()
        self.assertEqual(report["status"], "candidate_rejected")
        self.assertFalse(report["independently_verified_under_contract"])
        self.assertNotIn("patch", report)

    def test_verifier_rejects_changed_api_or_behavior_outside_contract(self):
        candidates = [
            CANDIDATE.replace("product", "other"),
            CANDIDATE.replace("left, right", "right, left"),
            CANDIDATE.replace("left * right", "right * left"),
            CANDIDATE.replace("left * right", "left * right + 1"),
            CANDIDATE.replace("left * right", "left ** right"),
            CANDIDATE.replace("left * right", "left.__mul__(right)"),
            CANDIDATE + "print('extra statement')\n",
            "@decorate()\n" + CANDIDATE,
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertFalse(verify_candidate(SOURCE, candidate, function="product", contract="multiply"))

    def test_no_contract_never_opens_the_source(self):
        with patch("diagnostics.coding_research.read_text", side_effect=AssertionError("unexpected read")):
            report = self.review(contract=None)
        self.assertEqual(report["status"], "missing_or_unsupported_contract")
        self.assertNotIn("patch", report)

    def test_unsupported_source_is_skipped_without_generating_a_candidate(self):
        originals = [
            "import os\n" + SOURCE,
            SOURCE.replace("right):", "right=run()):"),
            SOURCE.replace("left, right", "left: run(), right"),
            SOURCE.replace("left, right", "left, left"),
            SOURCE.replace("left / right", "left // right"),
            SOURCE.replace("left / right", "run(left, right)"),
            SOURCE.replace("left / right", "left.__class__"),
            SOURCE.replace("left / right", "left if left > right else right"),
            SOURCE.replace("return left / right", "return"),
            SOURCE.replace("return left / right", "return " + " + ".join(["left"] * 128)),
            "# coding: latin-1\n" + SOURCE,
        ]
        with patch("diagnostics.coding_research.static_coding.review_source",
                   side_effect=AssertionError("unexpected candidate generation")):
            for original in originals:
                with self.subTest(original=original[:80]):
                    self.source.write_text(original, encoding="utf-8")
                    report = self.review()
                    self.assertEqual(report["status"], "unsupported_source")
                    self.assertNotIn("patch", report)

    def test_syntax_mismatch_and_already_conforming_source_are_skipped(self):
        self.source.write_text(SOURCE.replace(":", ""), encoding="utf-8")
        self.assertEqual(self.review()["status"], "syntax_invalid")
        self.source.write_text(CANDIDATE, encoding="utf-8")
        self.assertEqual(self.review(function="other")["status"], "function_mismatch")
        self.assertEqual(self.review()["status"], "already_conforms")

    def test_missing_external_and_oversized_files_are_skipped(self):
        self.assertEqual(self.review()["status"], "source_unavailable")
        outside = self.task / "outside.py"
        outside.write_text(SOURCE, encoding="utf-8")
        for path in (Path("../outside.py"), outside, Path("sub/engine.py")):
            with self.subTest(path=path):
                self.assertEqual(self.review(source=path)["status"], "invalid_source_path")
        self.source.symlink_to(outside)
        self.assertEqual(self.review()["status"], "source_unavailable")
        self.source.unlink()
        os.mkfifo(self.source)
        self.assertEqual(self.review()["status"], "source_unavailable")
        self.source.unlink()
        self.source.write_text("#" * (64 * 1024 + 1), encoding="utf-8")
        self.assertEqual(self.review()["status"], "source_unavailable")
        self.assertFalse((self.task / "answer.txt").exists())

    def test_analysis_timeout_never_returns_a_patch(self):
        with patch("diagnostics.coding_research.read_text", side_effect=OperationTimeout):
            report = self.review()
        self.assertEqual(report["status"], "analysis_limit")
        self.assertNotIn("patch", report)

    def test_threshold_repair_uses_an_explicit_matching_limit(self):
        original = "def qualifies(amount):\n    return amount > -4  # inclusive limit\n"
        self.source.write_text(original, encoding="utf-8")
        options = {"function": "qualifies", "contract": "at_least"}
        self.assertEqual(
            review_task(self.task, Path("engine.py"), **options)["status"],
            "missing_or_unsupported_contract",
        )
        self.assertEqual(
            review_task(self.task, Path("engine.py"), threshold=True, **options)["status"],
            "missing_or_unsupported_contract",
        )
        self.assertEqual(
            review_task(self.task, Path("engine.py"), threshold=-3, **options)["status"],
            "contract_mismatch",
        )
        with patch("diagnostics.static_coding.review_source", side_effect=AssertionError("C3 invoked")):
            result = review_task(self.task, Path("engine.py"), threshold=-4, **options)
        self.assertEqual(result["status"], "verified_under_supplied_contract")
        self.assertFalse(result["contest_solution_confirmed"])
        candidate = apply_operator_edit(original, result["patch"])
        self.assertEqual(candidate, original.replace("> -4", ">= -4"))
        self.assertEqual(self.source.read_text(encoding="utf-8"), original)
        self.assertFalse((self.task / "answer.txt").exists())
        self.source.write_text(candidate, encoding="utf-8")
        self.assertEqual(
            review_task(self.task, Path("engine.py"), threshold=-4, **options)["status"],
            "already_conforms",
        )

    def test_threshold_verifier_rejects_changed_limit_and_unsupported_shapes(self):
        original = "def qualifies(value):\n    return value > 5\n"
        options = {"function": "qualifies", "contract": "at_least", "threshold": 5}
        self.assertTrue(verify_candidate(original, original.replace(">", ">="), **options))
        for candidate in (
            original.replace("> 5", ">= 6"),
            original.replace("> 5", ">= 5 >= 0"),
            original.replace("> 5", ">= limit()"),
            original.replace("> 5", ">= True"),
            original.replace("> 5", ">= 5.0"),
            original.replace("> 5", "* 5"),
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(verify_candidate(original, candidate, **options))
        scale = original.replace("> 5", "* 5")
        self.assertFalse(verify_candidate(scale, original.replace(">", ">="), **options))

    def test_syntax_repair_preserves_source_and_requires_an_explicit_factor(self):
        original = "def scale(value)  # keep comment\n    return value * -3\n"
        self.source.write_text(original, encoding="utf-8")
        options = {"function": "scale", "contract": "scale"}
        self.assertEqual(self.review(**options)["status"], "missing_or_unsupported_contract")
        self.assertEqual(
            review_task(self.task, Path("engine.py"), factor=-2, **options)["status"],
            "contract_mismatch",
        )
        report = review_task(self.task, Path("engine.py"), factor=-3, **options)
        self.assertEqual(report["status"], "verified_under_supplied_contract")
        self.assertFalse(report["contest_solution_confirmed"])
        self.assertEqual(
            apply_missing_colon(original, report["patch"]),
            original.replace(")  #", "):  #"),
        )
        self.assertEqual(self.source.read_text(encoding="utf-8"), original)
        self.assertFalse((self.task / "answer.txt").exists())

    def test_syntax_repair_refuses_non_scaling_and_unsupported_headers(self):
        for original in (
            "def scale(x)\n    return x * other()\n",
            "def scale(x)\n    return x * True\n",
            "def scale(x)\n    return x * 2\nprint('unexpected')\n",
            "def scale(x=run())\n    return x * 2\n",
            "def scale(x)\n    return x > 2\n",
        ):
            with self.subTest(original=original):
                self.source.write_text(original, encoding="utf-8")
                result = review_task(self.task, Path("engine.py"), function="scale", contract="scale", factor=2)
                self.assertFalse(result["independently_verified_under_contract"])
                self.assertNotIn("patch", result)
