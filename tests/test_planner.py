"""Planner tests and reproducible, offline ordering comparisons.

Run comparisons: .venv/bin/python -B tests/test_planner.py --experiments
"""

import asyncio
from contextlib import redirect_stdout
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
from budget_manager import BudgetManager, cost_tenths
from harness import SOLVERS, load_task, run_tasks
from llm_fallback import prepare
from llm_gateway import LLMGateway
from math_solver import solve_math
from planner import Planner, features, local_outcome, STRATEGIES
from test_llm import MATH, MATH_PLAN, envelope

LOCAL = "A warehouse starts with 100 boxes. It ships 7 boxes per day for 3 days. How many boxes does it have at the end?"
IMPOSSIBLE = ("A warehouse starts with 100 boxes. Then the stock is split equally into 0 warehouses "
              "and it keeps one share (rounded down). How many boxes does it have at the end?")
EXAMPLES = ROOT / "codex-local" / "examples" / "task-dir"


def write_task(root, name, instruction=LOCAL, reward=1, task_type="math", **extra):
    directory = root / name
    directory.mkdir()
    (directory / "task.json").write_text(json.dumps({
        "task_type": task_type, "instruction": instruction, "reward": reward, **extra,
    }), encoding="utf-8")
    return load_task(directory / "task.json")


def gateway(manager, responses=None, handler=None):
    requests = []
    async def mock(request):
        requests.append(json.loads(request.content))
        if handler is not None:
            return await handler(request)
        payload = json.loads(requests[-1]["messages"][1]["content"])
        data = envelope(json.dumps(MATH_PLAN))
        if responses is not None:
            plan, usage = responses[payload["instruction"]]
            data = envelope(json.dumps(plan))
            data["usage"] = usage
        return httpx.Response(200, json=data)
    return LLMGateway("http://planner-test.invalid/v1", "local-mock-key", manager=manager,
                      max_tokens=64, timeout=0.05, transport=httpx.MockTransport(mock)), requests


def run(root, manager=None, llm=None, **kwargs):
    report = {}
    with redirect_stdout(io.StringIO()):
        counts = asyncio.run(run_tasks(root, manager=manager, llm=llm, report=report, **kwargs))
    return counts, report


@dataclass(frozen=True)
class Case:
    name: str
    instruction: str
    reward: int
    answer: str | None
    valid_response: bool = True


def llm_case(name, reward, padding=0, valid=True):
    # Extra prose increases context cost without changing the arithmetic.
    return Case(name, MATH + f" Ledger {name}. " + "Background detail. " * padding,
                reward, "45", valid)


def profile(case):
    estimator, _ = gateway(BudgetManager(0))
    prepared = prepare("math", case.instruction)
    inputs = estimator.estimate_input(prepared.system, prepared.payload)
    # Freeze these mock counters BEFORE the run; every strategy gets the same
    # response and usage for each task, independent of its position.
    return {"prompt_tokens": inputs, "completion_tokens": 64}, cost_tenths(inputs, 64)


def compare(cases, limit):
    rows = []
    profiles = {case.name: profile(case) for case in cases if case.instruction != LOCAL}
    for strategy in STRATEGIES:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            responses = {}
            for case in cases:
                write_task(root, case.name, case.instruction, case.reward)
                if case.name in profiles:
                    prepared = prepare("math", case.instruction)
                    responses[prepared.payload["instruction"]] = (
                        MATH_PLAN if case.valid_response else {"answer": "unverified"}, profiles[case.name][0],
                    )
            manager = BudgetManager(limit)
            llm, _ = gateway(manager, responses)
            _, report = run(root, manager, llm, strategy=strategy)
            # Independent evaluator: these reference values never enter task.json
            # or the planner/gateway. A wrong answer is not counted as a success.
            correct = set()
            for case in cases:
                path = root / case.name / "answer.txt"
                if case.answer is not None and path.exists() and path.read_text().strip() == case.answer:
                    correct.add(case.name)
            rows.append({
                "strategy": strategy, "budget": limit, "solved": len(correct),
                "reward": sum(case.reward for case in cases if case.name in correct),
                "spent": str(manager.snapshot()["spent"]),
                "estimated": round(report["estimated_cost"], 2),
                "unsuccessful_attempts": report["unsuccessful_attempts"],
                "llm_failures": report["llm_unsuccessful_attempts"],
                "missed_feasible": sum(case.answer is not None and case.name not in correct for case in cases),
            })
    return rows


def experiments():
    local = Case("z_local", LOCAL, 1, "79")
    mixed = [llm_case("a_expensive", 8, 110), llm_case("b_short", 6), llm_case("c_short", 6), local]
    short_cost = profile(mixed[1])[1]
    scarcity = (2 * short_cost + 9) // 10
    miss = [llm_case("a_long", 6, 90), llm_case("b_short_failure", 5, valid=False), local]
    miss_limit = (profile(miss[0])[1] + 9) // 10
    return {
        "scarcity": compare(mixed, scarcity),
        "ample": compare(mixed, 100000),
        "heuristic_miss": compare(miss, miss_limit),
        "zero": compare(mixed, 0),
        "tiny": compare(mixed, 1),
    }


class PlannerTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_two_task_qualification_including_low_and_zero_reward(self):
        first = write_task(self.root, "a", reward=1000)
        second = write_task(self.root, "b", reward=0)
        counts, report = run(self.root, BudgetManager(0))
        self.assertEqual(counts, {"solved": 2, "skipped": 0, "failed": 0})
        for task in (first, second):
            self.assertEqual((task.directory / "answer.txt").read_text(), "79\n")
        self.assertEqual(report["budget"]["spent"], 0)

    def test_single_task_and_empty_set(self):
        self.assertEqual(run(self.root)[0], {"solved": 0, "skipped": 0, "failed": 0})
        write_task(self.root, "one", reward=0)
        self.assertEqual(run(self.root)[0]["solved"], 1)

    def test_two_paid_qualification_tasks_keep_the_low_reward_task(self):
        for name, reward in (("high", 1000), ("low", 0)):
            write_task(self.root, name, MATH, reward)
        llm, requests = gateway(BudgetManager(100000))
        self.assertEqual(run(self.root, llm.manager, llm)[0], {"solved": 2, "skipped": 0, "failed": 0})
        self.assertEqual(len(requests), 2)

    def test_already_exhausted_or_frozen_manager_does_not_stop_local_work(self):
        write_task(self.root, "local")
        write_task(self.root, "paid", MATH)
        for uncertain in (False, True):
            manager = BudgetManager(1000)
            manager.reserve("previous", "previous", 0, 1000)
            if uncertain:
                manager.mark_uncertain("previous")
            else:
                manager.settle("previous", 0, 1000)
            llm, requests = gateway(manager)
            self.assertEqual(run(self.root, manager, llm)[0]["solved"], 1)
            self.assertEqual(requests, [])

    def test_unsupported_wording_is_not_a_proof_of_impossibility(self):
        task = write_task(self.root, "unknown", "An impossible-looking unsupported geometry puzzle.")
        _, report = run(self.root, BudgetManager(0))
        outcome = report["outcomes"][str(task.directory.resolve())]
        self.assertEqual(outcome["local_outcome"], "unsupported")
        self.assertNotEqual(outcome["outcome"], "unsolvable")

    def test_hundred_tasks_with_twenty_undefined_problems(self):
        for index in range(100):
            write_task(self.root, f"task_{index:03}", IMPOSSIBLE if index % 5 == 0 else LOCAL, index % 7)
        counts, report = run(self.root, BudgetManager(1))
        self.assertEqual(counts, {"solved": 80, "skipped": 20, "failed": 0})
        self.assertEqual(sum(v["outcome"] == "unsolvable" for v in report["outcomes"].values()), 20)
        self.assertEqual(report["reward_solved"], sum(i % 7 for i in range(100) if i % 5))

    def test_original_eight_examples_and_material_integrity(self):
        material = ROOT / "codex-local"
        before = {str(p): hashlib.sha256(p.read_bytes()).digest() for p in material.rglob("*") if p.is_file()}
        references = {"math_0000": "389", "math_0004": "1735", "search_0001": "1983",
                      "search_0005": "Osaka", "data_analysis_0002": "158", "data_analysis_0006": "284",
                      "coding_0003": "DONE", "coding_0007": "DONE"}
        # Only supplied organizer examples are explicitly trusted in this test.
        for trusted, expected_solved in ((False, 6), (True, 8)):
            target = self.root / ("trusted" if trusted else "untrusted")
            shutil.copytree(EXAMPLES, target)
            for task_path in target.glob("*/task.json"):
                data = json.loads(task_path.read_text())
                data.pop("expected", None)
                data.pop("check", None)
                task_path.write_text(json.dumps(data))
            counts, _ = run(target, BudgetManager(0), allow_trusted_code=trusted)
            self.assertEqual(counts, {"solved": expected_solved, "skipped": 8 - expected_solved, "failed": 0})
            for name, answer in references.items():
                path = target / name / "answer.txt"
                if trusted or not name.startswith("coding"):
                    self.assertEqual(path.read_text().strip(), answer)
                else:
                    self.assertFalse(path.exists())
        after = {str(p): hashlib.sha256(p.read_bytes()).digest() for p in material.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_free_work_precedes_paid_work_and_complexity_cannot_veto_probe(self):
        unknown = write_task(self.root, "a_llm", MATH, 10000)
        local = write_task(self.root, "z_local", LOCAL, 1)
        llm, requests = gateway(BudgetManager(100000))
        calls = []
        def checked(instruction):
            calls.append(instruction)
            return solve_math(instruction)
        with patch.dict(SOLVERS, {"math": checked}):
            counts, report = run(self.root, llm.manager, llm)
        self.assertEqual(counts["solved"], 2)
        self.assertEqual(calls.count(MATH), 1)
        self.assertEqual(calls.count(LOCAL), 1)
        self.assertEqual(report["order"][0], (str(local.directory.resolve()), "local"))
        self.assertEqual(report["order"][-1], (str(unknown.directory.resolve()), "fallback"))
        self.assertEqual(len(requests), 1)

    def test_local_answer_is_saved_before_any_paid_request(self):
        local = write_task(self.root, "local")
        write_task(self.root, "paid", MATH)
        async def handler(request):
            self.assertEqual((local.directory / "answer.txt").read_text(), "79\n")
            return httpx.Response(200, json=envelope(json.dumps(MATH_PLAN)))
        llm, _ = gateway(BudgetManager(100000), handler=handler)
        self.assertEqual(run(self.root, llm.manager, llm)[0]["solved"], 2)

    def test_reward_probability_and_cost_all_affect_priority(self):
        tasks = [write_task(self.root, "short", MATH, 5),
                 write_task(self.root, "long", MATH + " Background detail." * 150, 6)]
        llm, _ = gateway(BudgetManager(100000))
        planner = Planner(tasks, llm.manager, llm)
        for _ in tasks:
            decision = planner.next()
            planner.finish(decision, "skipped", "unsupported math statement or instructions")
        short, long = planner.entries
        short_estimate, long_estimate = planner.estimate(short), planner.estimate(long)
        self.assertGreater(short_estimate.probability, long_estimate.probability)
        self.assertLess(short_estimate.cost_tenths, long_estimate.cost_tenths)
        self.assertEqual(planner.next().entry.task.directory.name, "short")

    def test_replanning_uses_real_usage_refunds_and_never_repeats_success(self):
        for index in range(4):
            write_task(self.root, str(index), MATH, 1)
        llm, requests = gateway(BudgetManager(100000))
        counts, report = run(self.root, llm.manager, llm)
        self.assertEqual(counts["solved"], 4)
        self.assertEqual(len(requests), 4)
        self.assertEqual(report["budget"]["spent"], 120)
        self.assertEqual(report["budget"]["reserved"], 0)
        self.assertTrue(all(v["llm_attempts"] == 1 for v in report["outcomes"].values()))

    def test_three_confirmed_observations_update_forecast_and_probability(self):
        tasks = [write_task(self.root, str(i), MATH) for i in range(4)]
        llm, _ = gateway(BudgetManager(100000))
        planner = Planner(tasks, llm.manager, llm)
        for _ in tasks:
            planner.finish(planner.next(), "skipped", "unsupported math statement or instructions")
        before = planner.estimate(planner.entries[-1])
        for index in range(3):
            decision = planner.next()
            asyncio.run(llm.complete(prepare("math", MATH).system, prepare("math", MATH).payload,
                                     task_id=decision.entry.key))
            planner.finish(decision, "solved" if index < 2 else "skipped", "invalid plan")
        decision = planner.next()
        self.assertEqual(decision.estimate.source, "observed:3+prior")
        self.assertLess(decision.estimate.cost_tenths, before.cost_tenths)
        self.assertNotEqual(decision.estimate.probability, before.probability)
        self.assertEqual(decision.estimate.cost_tenths, 300)

    def test_observed_costs_can_change_the_next_choice(self):
        tasks = [write_task(self.root, str(i), MATH, 100) for i in range(3)]
        tasks.append(write_task(self.root, "small_math", MATH, 1))
        search = write_task(self.root, "search", "What city is Lena Noor's employer based in?", 2, "search")
        kb = search.directory / "workspace" / "kb"
        kb.mkdir(parents=True)
        (kb / "a.md").write_text("# Lena Noor\nEmployer: Arc Studio\n# Arc Studio\nHeadquarters: Faro\n")
        tasks.append(search)
        llm, _ = gateway(BudgetManager(100000))
        planner = Planner(tasks, llm.manager, llm)
        for _ in tasks:
            decision = planner.next()
            planner.finish(decision, "skipped", "unsupported formulation")
        small, search_entry = planner.entries[-2:]
        before_math, before_search = planner.estimate(small), planner.estimate(search_entry)
        self.assertLess(before_math.probability / before_math.cost_tenths,
                        2 * before_search.probability / before_search.cost_tenths)
        for _ in range(3):
            decision = planner.next()
            self.assertEqual(decision.entry.task.reward, 100)
            prepared = prepare("math", MATH)
            asyncio.run(llm.complete(prepared.system, prepared.payload, task_id=decision.entry.key))
            planner.finish(decision, "solved")
        self.assertEqual(planner.next().entry.task.directory.name, "small_math")

    def test_zero_and_tiny_budget_do_not_prevent_local_work(self):
        write_task(self.root, "local")
        write_task(self.root, "paid", MATH)
        for limit in (0, 1):
            llm, requests = gateway(BudgetManager(limit))
            counts, report = run(self.root, llm.manager, llm)
            self.assertEqual(counts, {"solved": 1, "skipped": 1, "failed": 0})
            self.assertEqual(requests, [])
            self.assertEqual(report["outcomes"][str((self.root / "paid").resolve())]["outcome"], "budget_blocked")

    def test_semantic_statuses_are_distinct(self):
        for reason, category in (("cannot split into zero warehouses", "unsolvable"),
                                 ("unsupported math statement or instructions", "unsupported"),
                                 ("missing or ambiguous born fact", "insufficient_evidence"),
                                 ("CSV row has the wrong number of fields", "invalid_data"),
                                 ("coding execution requires trusted opt-in", "unsafe")):
            self.assertEqual(local_outcome(reason), category)

    def test_missing_files_and_unsafe_coding_never_execute(self):
        write_task(self.root, "missing", "What year was Ada born?", task_type="search")
        write_task(self.root, "code", "Repair code", task_type="coding")
        llm, requests = gateway(BudgetManager(100000))
        with patch("subprocess.Popen", side_effect=AssertionError("untrusted execution")):
            counts, report = run(self.root, llm.manager, llm)
        self.assertEqual(counts, {"solved": 0, "skipped": 2, "failed": 0})
        self.assertEqual(requests, [])
        self.assertEqual({v["outcome"] for v in report["outcomes"].values()}, {"unsafe", "insufficient_evidence"})

    def test_unknown_cost_freezes_paid_work_but_not_existing_local_solutions(self):
        for mode in ("error", "timeout", "missing_usage"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                for name in ("a", "b"):
                    write_task(root, name, MATH)
                local = write_task(root, "local")
                async def handler(request):
                    if mode == "timeout":
                        raise httpx.ReadTimeout("mock timeout")
                    if mode == "error":
                        return httpx.Response(500)
                    data = envelope("{}")
                    data.pop("usage")
                    return httpx.Response(200, json=data)
                llm, requests = gateway(BudgetManager(100000), handler=handler)
                counts, report = run(root, llm.manager, llm)
                self.assertEqual(counts, {"solved": 1, "skipped": 2, "failed": 0})
                self.assertEqual(len(requests), 1)
                self.assertTrue(report["budget"]["frozen"])
                self.assertEqual((local.directory / "answer.txt").read_text(), "79\n")

    def test_invalid_plan_is_not_retried(self):
        write_task(self.root, "bad", MATH)
        async def invalid(request):
            return httpx.Response(200, json=envelope("{}"))
        llm, requests = gateway(BudgetManager(100000), handler=invalid)
        counts, report = run(self.root, llm.manager, llm)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(report["llm_unsuccessful_attempts"], 1)
        self.assertEqual(report["budget"]["spent"], 30)

    def test_inexpensive_estimate_never_replaces_full_reservation(self):
        task = write_task(self.root, "task", MATH)
        llm, _ = gateway(BudgetManager(100000))
        planner = Planner([task], llm.manager, llm)
        planner.finish(planner.next(), "skipped", "unsupported math statement or instructions")
        entry = planner.entries[0]
        estimate = planner.estimate(entry)
        self.assertLess(estimate.cost_tenths, estimate.reservation_tenths)
        # Leave enough for the forecast but too little for the safe reserve.
        remaining = int(estimate.cost_tenths) + 1
        llm.manager.reserve("other", "other", llm.manager.limit * 10 - remaining, 0)
        self.assertIsNone(planner.next())
        self.assertEqual(entry.state, "budget_blocked")

    def test_deterministic_ties_and_zero_reward_have_no_division_by_zero(self):
        tasks = [write_task(self.root, name, MATH, 0) for name in ("z", "a")]
        orders = []
        for _ in range(2):
            llm, _ = gateway(BudgetManager(100000))
            planner = Planner(list(reversed(tasks)), llm.manager, llm)
            while (decision := planner.next()) is not None:
                planner.finish(decision, "solved" if decision.phase == "fallback" else "skipped", "unsupported formulation")
            orders.append([(Path(key).name, phase) for key, phase in planner.order])
        self.assertEqual(orders[0], orders[1])
        self.assertEqual(orders[0], [("a", "local"), ("z", "local"), ("a", "fallback"), ("z", "fallback")])

    def test_reference_fields_cannot_influence_order_or_answers(self):
        first = write_task(self.root, "a", MATH, expected="999", check="invented")
        second = write_task(self.root, "b", MATH, expected="0", check="different")
        self.assertFalse(hasattr(first, "expected"))
        llm, _ = gateway(BudgetManager(100000))
        self.assertEqual(run(self.root, llm.manager, llm)[0]["solved"], 2)
        self.assertEqual((first.directory / "answer.txt").read_bytes(), (second.directory / "answer.txt").read_bytes())

    def test_different_manager_and_duplicate_task_directory_are_rejected(self):
        task = write_task(self.root, "task")
        llm, _ = gateway(BudgetManager(100000))
        with self.assertRaises(ValueError):
            Planner([task], BudgetManager(100000), llm)
        with self.assertRaises(ValueError):
            Planner([task, task], llm.manager, llm)

    def test_finished_decision_cannot_be_repeated(self):
        task = write_task(self.root, "task")
        planner = Planner([task], BudgetManager(0))
        decision = planner.next()
        planner.finish(decision, "solved")
        with self.assertRaises(ValueError):
            planner.finish(decision, "solved")
        self.assertIsNone(planner.next())

    def test_preflight_handles_external_workspace_without_reading_it(self):
        task = write_task(self.root, "search", "What year was Ada born?", task_type="search")
        with tempfile.TemporaryDirectory() as external:
            (task.directory / "workspace").symlink_to(external, target_is_directory=True)
            self.assertEqual(features(task).blocker, "unsafe")

    def test_baseline_experiments_include_wins_ties_and_losses(self):
        result = experiments()
        scarcity = {row["strategy"]: row for row in result["scarcity"]}
        self.assertGreater(scarcity["priority"]["reward"], scarcity["reward"]["reward"])
        self.assertEqual(len({row["reward"] for row in result["ample"]}), 1)
        miss = {row["strategy"]: row for row in result["heuristic_miss"]}
        self.assertLess(miss["priority"]["reward"], miss["reward"]["reward"])


if __name__ == "__main__":
    if "--experiments" in sys.argv:
        print(json.dumps(experiments(), indent=2, ensure_ascii=False))
    else:
        unittest.main()
