"""Run the read-only Coding diagnostics over the 16 development tasks."""

import argparse
import json
from pathlib import Path
import sys

from diagnostics.coding_acceptance import SOURCE, TESTS, check_task_examples
from diagnostics.coding_research import (
    CONTRACTS,
    parse_review_json,
    review_task,
    source_digest,
    supports_task_instruction,
)
from runtime_limits import OperationTimeout, RuntimeLimits
from safe_files import TASK_JSON_BYTES, read_text


TASK_IDS = tuple(f"coding_d{number:02}" for number in range(1, 17))
SOURCE_NAME = Path(SOURCE).name
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


def _read_task(task: Path, task_id: str) -> tuple[int, bool, str]:
    declaration = read_text(task, "task.json", maximum=TASK_JSON_BYTES)
    data = parse_review_json(declaration)
    if not isinstance(data, dict) or data.get("task_id") != task_id or data.get("task_type") != "coding":
        raise ValueError(f"unexpected task metadata for {task_id}")
    reward = data.get("reward")
    if type(reward) is not int or reward < 0:
        raise ValueError(f"invalid reward for {task_id}")
    supports_edit = supports_task_instruction(data.get("instruction"), SOURCE_NAME, test=Path(TESTS).name)
    return reward, supports_edit, source_digest(declaration)


def run_development(tasks_dir: Path, contracts: dict) -> dict:
    """Keep contest status separate from visible assertion counts and reward."""
    _validate_contracts(contracts)
    limits = RuntimeLimits(60, 2)
    rows = []
    for task_id in TASK_IDS:
        task = tasks_dir / task_id
        with limits.sync():
            reward, supports_edit, task_sha256 = _read_task(task, task_id)
        row = {"task_id": task_id, "reward": reward}
        options = contracts.get(task_id, {"function": None, "contract": None})
        if task_id in contracts and not supports_edit:
            review = {
                "status": "unsupported_task_instruction",
                "independently_verified_under_contract": False,
                "contest_solution_confirmed": False,
            }
        else:
            review = review_task(task, Path(SOURCE_NAME), **options)
        edit = review.get("patch")
        if edit is not None:
            if edit.get("task_sha256") != task_sha256:
                review = {
                    "status": "task_declaration_changed",
                    "independently_verified_under_contract": False,
                    "contest_solution_confirmed": False,
                }
            else:
                with limits.sync():
                    row["visible"] = check_task_examples(task, edit)
        row["review"] = review
        rows.append(row)

    candidates = [row for row in rows if "patch" in row["review"]]
    checked = [row for row in candidates if row["visible"]["status"] == "visible_assertions_checked"]
    passed = [row for row in checked if row["visible"]["after_passed"] == len(row["visible"]["cases"])]
    improved = [row for row in passed if row["visible"]["before_passed"] < row["visible"]["after_passed"]]
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
            "visible_improved": len(improved),
            "visible_already_passed": len(passed) - len(improved),
            "visible_failed": len(checked) - len(passed),
            "visible_unavailable": len(candidates) - len(checked),
            "visible_regressions": regressions,
            "visible_assertions_total": sum(len(row["visible"]["cases"]) for row in checked),
            "visible_assertions_before": sum(row["visible"]["before_passed"] for row in checked),
            "visible_assertions_after": sum(row["visible"]["after_passed"] for row in checked),
            "potential_reward_of_visible_passes": sum(row["reward"] for row in passed),
            "potential_reward_of_visible_improvements": sum(row["reward"] for row in improved),
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
            contracts = parse_review_json(raw)
        result = run_development(args.tasks_dir, contracts)
    except (OSError, ValueError, OperationTimeout, RecursionError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
