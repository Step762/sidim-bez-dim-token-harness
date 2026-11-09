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


class DevelopmentRunTests(unittest.TestCase):
    def test_metrics_separate_visible_pass_regression_and_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for task_id in TASK_IDS:
                task = root / task_id
                task.mkdir()
                task.joinpath("task.json").write_text(json.dumps({
                    "task_id": task_id, "task_type": "coding", "reward": 3,
                }), encoding="utf-8")
            for task_id, symbol in (("coding_d01", "-"), ("coding_d02", "+"), ("coding_d03", "-")):
                workspace = root / task_id / "workspace"
                workspace.mkdir()
                workspace.joinpath("engine.py").write_text(
                    f"def total(a, b):\n    return a {symbol} b\n", encoding="utf-8",
                )
                if task_id != "coding_d03":
                    workspace.joinpath("test_engine.py").write_text(VISIBLE_SUITE, encoding="utf-8")

            contracts = {
                "coding_d01": {"function": "total", "contract": "add"},
                "coding_d02": {"function": "total", "contract": "multiply"},
                "coding_d03": {"function": "total", "contract": "add"},
            }
            with patch("subprocess.Popen", side_effect=AssertionError("task execution forbidden")):
                result = run_development(root, contracts)
            self.assertFalse(any((root / task_id / "answer.txt").exists() for task_id in TASK_IDS))

        self.assertEqual(result["summary"], {
            "tasks": 16, "contest_correct": 0, "contest_wrong": 0,
            "contest_skipped": 13, "contest_unverified": 3, "contest_points": 0,
            "visible_all_passed": 1, "visible_failed": 1, "visible_unavailable": 1,
            "visible_regressions": 1, "visible_assertions_total": 2,
            "visible_assertions_before": 1, "visible_assertions_after": 1,
            "potential_reward_of_visible_passes": 3,
        })
        self.assertEqual([row["task_id"] for row in result["tasks"]], list(TASK_IDS))

    def test_out_of_scope_ids_and_malformed_contracts_fail_before_reads(self):
        for contracts in (
            {"coding_h01": {"function": "total", "contract": "add"}},
            {"../coding_d01": {"function": "total", "contract": "add"}},
            {"coding_d01": {"function": "total", "contract": "at_least"}},
            {"coding_d01": {"function": "total", "contract": "scale", "factor": True}},
            {"coding_d01": {"function": "total", "contract": "add", "threshold": 1}},
        ):
            with (
                self.subTest(contracts=contracts),
                patch("diagnostics.coding_development.read_text", side_effect=AssertionError("unexpected read")),
            ):
                with self.assertRaises(ValueError):
                    run_development(Path("."), contracts)
