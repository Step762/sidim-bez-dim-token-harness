"""Check the development runner's accounting against synthetic inputs."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from diagnostics.coding_development import TASK_IDS, run_development


VISIBLE_SUITE = """import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(lib.total(*(2, 3)), 5)

if __name__ == "__main__":
    unittest.main()
"""

INSTRUCTION = (
    "Run `python3 test_engine.py` in the current directory; some tests fail. "
    "Fix the bugs in engine.py (do not edit the tests) until all tests pass. "
    "Work only inside the current directory. When you are done, write ONLY "
    "the word DONE to the file answer.txt in the current directory."
)


class DevelopmentRunTests(unittest.TestCase):
    def test_metrics_separate_improvements_existing_passes_regressions_and_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for task_id in TASK_IDS:
                task = root / task_id
                task.mkdir()
                instruction = INSTRUCTION
                if task_id == "coding_d05":
                    instruction = INSTRUCTION.replace("test_engine.py", "test_other.py").replace(
                        "in engine.py", "in other.py",
                    )
                elif task_id == "coding_d06":
                    instruction += " Preserve the subtraction behavior."
                task.joinpath("task.json").write_text(json.dumps({
                    "task_id": task_id, "task_type": "coding", "reward": 3,
                    "instruction": instruction,
                }), encoding="utf-8")
            for task_id in TASK_IDS[:6]:
                symbol = "+" if task_id == "coding_d02" else "-"
                workspace = root / task_id / "workspace"
                workspace.mkdir()
                workspace.joinpath("engine.py").write_text(
                    f"def total(a, b):\n    return a {symbol} b\n", encoding="utf-8",
                )
                if task_id != "coding_d03":
                    suite = VISIBLE_SUITE.replace("(2, 3)", "(5, 0)") if task_id == "coding_d04" else VISIBLE_SUITE
                    workspace.joinpath("test_engine.py").write_text(suite, encoding="utf-8")

            contracts = {task_id: {"function": "total", "contract": "add"} for task_id in TASK_IDS[:6]}
            contracts["coding_d02"]["contract"] = "multiply"
            with patch("subprocess.Popen", side_effect=AssertionError("task execution forbidden")):
                result = run_development(root, contracts)
            self.assertFalse(any((root / task_id / "answer.txt").exists() for task_id in TASK_IDS))

        self.assertEqual(result["summary"], {
            "tasks": 16, "contest_correct": 0, "contest_wrong": 0,
            "contest_skipped": 12, "contest_unverified": 4, "contest_points": 0,
            "visible_all_passed": 2, "visible_improved": 1, "visible_already_passed": 1,
            "visible_failed": 1, "visible_unavailable": 1,
            "visible_regressions": 1, "visible_assertions_total": 3,
            "visible_assertions_before": 2, "visible_assertions_after": 2,
            "potential_reward_of_visible_passes": 6, "potential_reward_of_visible_improvements": 3,
        })
        self.assertEqual([row["task_id"] for row in result["tasks"]], list(TASK_IDS))
        for row in result["tasks"][4:6]:
            self.assertEqual(row["review"]["status"], "unsupported_task_instruction")
            self.assertNotIn("patch", row["review"])
            self.assertNotIn("visible", row)

    def test_ambiguous_task_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / TASK_IDS[0]
            task.mkdir()
            (task / "task.json").write_text(
                '{"task_id":"coding_d01","task_type":"coding","reward":0,"reward":3}', encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                run_development(root, {})

    def test_out_of_scope_ids_and_malformed_contracts_fail_before_reads(self):
        for contracts in (
            {"coding_h01": {"function": "total", "contract": "add"}},
            {"../coding_d01": {"function": "total", "contract": "add"}},
            {"coding_d01": {"function": "total", "contract": "at_least"}},
            {"coding_d01": {"function": "total", "contract": "scale", "factor": True}},
            {"coding_d01": {"function": "total", "contract": "remainder_equals", "modulus": 2}},
            {"coding_d01": {"function": "total", "contract": "add", "threshold": 1}},
        ):
            with (
                self.subTest(contracts=contracts),
                patch("diagnostics.coding_development.read_text", side_effect=AssertionError("unexpected read")),
            ):
                with self.assertRaises(ValueError):
                    run_development(Path("."), contracts)
