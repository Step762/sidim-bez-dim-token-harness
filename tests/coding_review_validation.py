"""Run a synthetic smoke test and the available checkout regressions."""

import argparse
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ORGANIZER_EXAMPLES = ROOT / "codex-local" / "examples" / "task-dir"


def smoke() -> None:
    fixtures = {
        "math": (
            "A warehouse starts with 31 boxes. It ships 4 boxes per day for 3 days. "
            "How many boxes does it have at the end?",
            {},
            "19",
        ),
        "search": (
            "In which year was Sima Vale born?",
            {"kb/person.md": "# Sima Vale\nBorn: 1986\n"},
            "1986",
        ),
        "data_analysis": (
            "The file sales.csv. What is the sum of units?",
            {"sales.csv": "units\n3\n9\n"},
            "12",
        ),
        "coding": (
            "Run `python3 test_engine.py` in the current directory; some tests fail. "
            "Fix the bugs in engine.py (do not edit the tests) until all tests pass. "
            "Work only inside the current directory. When you are done, write ONLY "
            "the word DONE to the file answer.txt in the current directory.",
            {"engine.py": "raise AssertionError('task source executed')\n",
             "test_engine.py": "raise AssertionError('task tests executed')\n"},
            None,
        ),
    }
    with tempfile.TemporaryDirectory(prefix="coding-review-") as directory:
        tasks = Path(directory)
        for kind, (instruction, files, _) in fixtures.items():
            task = tasks / kind
            task.mkdir()
            (task / "task.json").write_text(json.dumps({
                "task_id": kind, "task_type": kind, "instruction": instruction, "reward": 1,
            }), encoding="utf-8")
            for name, content in files.items():
                path = task / "workspace" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

        run = subprocess.run([
            sys.executable, "-B", str(ROOT / "run_agent.py"), "--tasks-dir", str(tasks), "--disable-llm",
        ], capture_output=True, text=True, timeout=60)
        if run.returncode or "solved=3 skipped=1 failed=0" not in run.stdout:
            raise AssertionError(f"unexpected smoke run\n{run.stdout}\n{run.stderr}")
        for kind, (_, files, expected) in fixtures.items():
            task = tasks / kind
            answer = task / "answer.txt"
            if expected is None:
                if answer.exists():
                    raise AssertionError("unsafe Coding task published an answer")
            elif (
                answer.read_text(encoding="utf-8") != expected + "\n"
                or stat.S_IMODE(answer.stat().st_mode) != 0o644
            ):
                raise AssertionError(f"unexpected answer or permissions for {kind}")
            for name, content in files.items():
                if (task / "workspace" / name).read_text(encoding="utf-8") != content:
                    raise AssertionError(f"modified input for {kind}")
    print("Synthetic smoke passed: math, search and CSV solved; Coding skipped.")


def regressions() -> bool:
    sys.path.insert(0, str(ROOT))
    if not ORGANIZER_EXAMPLES.is_dir():
        from test_planner import PlannerTests

        name = "test_original_eight_examples_and_material_integrity"
        method = getattr(PlannerTests, name)
        setattr(PlannerTests, name, unittest.skip("organizer fixtures are absent from this checkout")(method))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    return unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("smoke", "regressions"))
    args = parser.parse_args()
    if args.mode == "smoke":
        smoke()
        return 0
    return 0 if regressions() else 1


if __name__ == "__main__":
    raise SystemExit(main())
