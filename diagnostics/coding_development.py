"""Run the read-only Coding diagnostics over the 16 development tasks."""

import argparse
import json
from pathlib import Path
import sys

from diagnostics.coding_acceptance import SOURCE, TESTS, check_visible_examples
from diagnostics.coding_research import CONTRACTS, MAX_SOURCE_BYTES, review_task
from runtime_limits import OperationTimeout, RuntimeLimits
from safe_files import TASK_JSON_BYTES, read_text


TASK_IDS = tuple(f"coding_d{number:02}" for number in range(1, 17))
MAX_CONTRACT_BYTES = 8 * 1024
CONTRACT_PARAMETERS = {
    "at_least": ("threshold",), "remainder_equals": ("modulus", "residue"), "scale": ("factor",),
}


def _validate_contracts(contracts: object) -> None:
    if not isinstance(contracts, dict) or not contracts.keys() <= set(TASK_IDS):
        raise ValueError("contracts must be keyed by coding_d01 through coding_d16")
    for options in contracts.values():
        if not isinstance(options, dict):
            raise ValueError("expected a contract object")
        function, contract = options.get("function"), options.get("contract")
        if not isinstance(function, str) or not function.isidentifier() or contract not in CONTRACTS:
            raise ValueError("expected a function name and a supported contract")
        parameters = CONTRACT_PARAMETERS.get(contract, ())
        if options.keys() != {"function", "contract", *parameters}:
            raise ValueError("unexpected or missing contract fields")
        for parameter in parameters:
            value = options.get(parameter)
            if type(value) is not int or value.bit_length() > 128:
                raise ValueError("expected a bounded integer contract parameter")


def _task_reward(task: Path, task_id: str) -> int:
    data = json.loads(read_text(task, "task.json", maximum=TASK_JSON_BYTES))
    if not isinstance(data, dict) or data.get("task_id") != task_id or data.get("task_type") != "coding":
        raise ValueError(f"unexpected task metadata for {task_id}")
    reward = data.get("reward")
    if type(reward) is not int or reward < 0:
        raise ValueError(f"invalid reward for {task_id}")
    return reward


def run_development(tasks_dir: Path, contracts: dict) -> dict:
    """Keep contest status separate from visible assertion counts and reward."""
    _validate_contracts(contracts)
    limits = RuntimeLimits(60, 2)
    rows = []
    for task_id in TASK_IDS:
        task = tasks_dir / task_id
        with limits.sync():
            row = {"task_id": task_id, "reward": _task_reward(task, task_id)}
        options = contracts.get(task_id, {"function": None, "contract": None})
        review = review_task(task, Path("engine.py"), **options)
        row["review"] = review
        if "patch" in review:
            try:
                with limits.sync():
                    source = read_text(task, SOURCE, maximum=MAX_SOURCE_BYTES)
                    tests = read_text(task, TESTS, maximum=MAX_SOURCE_BYTES)
                    row["visible"] = check_visible_examples(source, tests, review["patch"])
            except (OSError, ValueError):
                row["visible"] = {"status": "source_or_tests_unavailable"}
        rows.append(row)

    candidates = [row for row in rows if "patch" in row["review"]]
    checked = [row for row in candidates if row["visible"]["status"] == "visible_assertions_checked"]
    passed = [row for row in checked if row["visible"]["after_passed"] == len(row["visible"]["cases"])]
    regressions = sum(
        any(case["before_passed"] and not case["after_passed"] for case in row["visible"]["cases"])
        for row in checked
    )
    return {
        "summary": {
            "tasks": len(rows),
            "contest_correct": 0,
            "contest_wrong": 0,
            "contest_skipped": len(rows) - len(candidates),
            "contest_unverified": len(candidates),
            "contest_points": 0,
            "visible_all_passed": len(passed),
            "visible_failed": len(checked) - len(passed),
            "visible_unavailable": len(candidates) - len(checked),
            "visible_regressions": regressions,
            "visible_assertions_total": sum(len(row["visible"]["cases"]) for row in checked),
            "visible_assertions_before": sum(row["visible"]["before_passed"] for row in checked),
            "visible_assertions_after": sum(row["visible"]["after_passed"] for row in checked),
            "potential_reward_of_visible_passes": sum(row["reward"] for row in passed),
        },
        "tasks": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experimental-static-coding", action="store_true", required=True)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        with RuntimeLimits(5, 2).sync():
            raw = sys.stdin.buffer.read(MAX_CONTRACT_BYTES + 1)
            if len(raw) > MAX_CONTRACT_BYTES:
                parser.error("contract JSON exceeds size limit")
            contracts = json.loads(raw)
        result = run_development(args.tasks_dir, contracts)
    except (OSError, ValueError, OperationTimeout, RecursionError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
