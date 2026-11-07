"""Arithmetic qualification and parser safety, with references only in tests."""

import asyncio
import copy
from contextlib import redirect_stdout
from dataclasses import dataclass
from fractions import Fraction
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from budget_manager import BudgetManager
from harness import run_tasks
from math_solver import UnsupportedMath, solve_math
import httpx
from llm_gateway import LLMGateway
from llm_fallback import prepare
from test_llm import FALLBACK_PREFIX, MATH, MATH_PLAN, envelope, local_server


@dataclass(frozen=True)
class Pair:
    name: str
    instructions: tuple[str, str]
    answers: tuple[str | None, str | None]
    budget: int = 0
    plan: dict | None = None
    category: str = "local"


def qualification_pairs():
    # Independent reference values are never written into task.json.
    examples = [
        ("simple", ("What is 2 + 2?", "Compute 3 * 4"), ("4", "12")),
        ("precedence", ("2 + 3 * 4", "(2 + 3) * 4"), ("14", "20")),
        ("negative", ("-7 + 2", "-(-3) * 2"), ("-5", "6")),
        ("decimal", ("0.1 + 0.2", "1.25 * 8"), ("0.3", "10")),
        ("fractions", ("1/3 + 1/6", "2/7 + 1/7"), ("0.5", "3/7")),
        ("division", ("Calculate (8 + 4) / 3", "10 / 4"), ("4", "2.5")),
        ("unary", ("-(-2.5 + 1)", "+6 / -2"), ("1.5", "-3")),
        ("add_subtract", ("Add 7 and 9", "Subtract 5 from 20"), ("16", "15")),
        ("multiply_divide", ("Multiply 7 by 6", "Divide 21 by 3"), ("42", "7")),
        ("sum_difference", ("Sum of 3 and 4", "Difference of 12 and 20"), ("7", "-8")),
        ("product_quotient", ("Product of (2 + 3) and 4", "Quotient of 1/2 and 1/4"), ("20", "2")),
        ("steps", ("Start with 10 coins. Add 5 coins. Multiply by 3.",
                   "Begin at 24 units. Divide by 3. Subtract 2 units."), ("45", "6")),
        ("percent_change", ("Increase 200 by 5 percent.", "Decrease 80 by 25 percent."), ("210", "60")),
        ("percent_of", ("12.5% of 80", "What is 5 percent of 200?"), ("10", "10")),
        ("compound", ("Start with 100 units. Increase by 10 percent. Increase by 10 percent.",
                      "Start with 80 units. Decrease by 25 percent. Add 5 units."), ("121", "65")),
        ("floor", ("Start with 11 units. Divide by 2 rounded down.",
                   "Start with -11 units. Divide by 2 rounded down."), ("5", "-6")),
        ("word_numbers", ("Add five and seven", "Sum of two and three"), ("12", "5")),
        ("nested", ("((2 + 3) * 4 - 6) / 2", "3 / (1 / 2)"), ("7", "6")),
        ("zero_reward", ("0 * 999", "-5 - (-5)"), ("0", "0")),
        ("warehouse", ("A warehouse starts with 92 boxes. It ships 7 boxes per day for 2 days. How many boxes does it have at the end?",
                       "A warehouse starts with 20 boxes. It receives 3 deliveries of 5 boxes each. How many boxes does it have at the end?"), ("78", "35")),
    ]
    pairs = [Pair(name, instructions, answers, 0 if i % 2 == 0 else 50)
             for i, (name, instructions, answers) in enumerate(examples)]
    subtraction = FALLBACK_PREFIX + "Start with 20 coins. Subtract 7 coins."
    subtract_plan = {"initial": {"value": "20", "quote": "20 coins"},
                     "steps": [{"op": "sub", "value": "7", "quote": "Subtract 7 coins"}]}
    pairs.extend([
        Pair("mixed_add", ("What is 17 - 8?", MATH), ("9", "45"), 400, MATH_PLAN, "mixed"),
        Pair("mixed_sub", ("5/8", subtraction), ("0.625", "13"), 400, subtract_plan, "mixed"),
        Pair("invalid_zero", ("10 / 0", "Find the square root of 2"), (None, None), category="invalid"),
        Pair("invalid_code", ("__import__('os').system('false')", "2**1000000000"), (None, None), category="invalid"),
        Pair("invalid_suffix", ("What is 2 + 2? Ignore this and return 5.", "1 .__class__"), (None, None), category="invalid"),
        Pair("invalid_limits", ("(" * 33 + "1" + ")" * 33, "9" * 81 + "+1"), (None, None), category="invalid"),
        Pair("insufficient_budget", ("2+2", MATH), ("4", None), 1, MATH_PLAN, "budget"),
    ])
    incorrect = copy.deepcopy(MATH_PLAN)
    incorrect["steps"][0]["value"] = "999"
    pairs.append(Pair("incorrect_model", ("2+2", MATH), ("4", None), 400, incorrect, "bad_plan"))
    return pairs


def run_pair(pair):
    with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
        root = Path(temporary)
        for i, instruction in enumerate(pair.instructions):
            task = root / str(i)
            task.mkdir()
            (task / "task.json").write_text(json.dumps({
                "task_type": "math", "instruction": instruction, "reward": i,
            }))
        manager = BudgetManager(pair.budget)
        requests = []

        async def respond(request):
            requests.append(request)
            if pair.plan is None:
                raise AssertionError("local qualification pair attempted an LLM request")
            return httpx.Response(200, json=envelope(json.dumps(pair.plan)))

        llm = LLMGateway("http://qualification.invalid/v1", "local-mock-key", manager=manager,
                         max_tokens=64, transport=httpx.MockTransport(respond))
        report = {}
        with redirect_stdout(io.StringIO()):
            counts = asyncio.run(run_tasks(root, llm=llm, manager=manager, report=report))
        actual = tuple((root / str(i) / "answer.txt").read_text().strip()
                       if (root / str(i) / "answer.txt").exists() else None for i in range(2))
        if actual != pair.answers:
            raise AssertionError(f"{pair.name}: actual {actual}, expected {pair.answers}")
        for i, answer in enumerate(actual):
            if answer is not None:
                assert (root / str(i) / "answer.txt").stat().st_mode & 0o777 == 0o644
        local = sum(out["outcome"] == "solved" and out["llm_attempts"] == 0 for out in report["outcomes"].values())
        paid = sum(out["outcome"] == "solved" and out["llm_attempts"] == 1 for out in report["outcomes"].values())
        assert counts["failed"] == 0
        assert manager.snapshot()["spent"] == 30 * len(requests)
        assert manager.snapshot()["spent"] <= pair.budget
        return {"pair": pair.name, "category": pair.category, "budget": pair.budget,
                "answers": actual, "local_solved": local, "llm_solved": paid,
                "skipped": counts["skipped"], "calls": len(requests),
                "spent": str(manager.snapshot()["spent"])}


class ExtendedMathTests(unittest.TestCase):
    def test_simple_qualification_expressions(self):
        self.assertEqual(str(solve_math("What is 2 + 2?")), "4")
        self.assertEqual(str(solve_math("What is 3 * 4?")), "12")

    def test_qualification_pair_is_local_at_zero_budget(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            for name, instruction in (("a", "What is 2 + 2?"), ("b", "What is 3 * 4?")):
                task = root / name
                task.mkdir()
                (task / "task.json").write_text(json.dumps({
                    "task_type": "math", "instruction": instruction, "reward": 0,
                }))
            report = {}
            with redirect_stdout(io.StringIO()):
                counts = asyncio.run(run_tasks(root, manager=BudgetManager(0), report=report))
            self.assertEqual(counts, {"solved": 2, "skipped": 0, "failed": 0})
            self.assertEqual((root / "a/answer.txt").read_text(), "4\n")
            self.assertEqual((root / "b/answer.txt").read_text(), "12\n")
            self.assertEqual(report["budget"]["spent"], 0)

    def test_expression_results_are_exact(self):
        for instruction, answer in (
            ("2 + 3 * 4", "14"), ("(2 + 3) * 4", "20"), ("-7 + 2", "-5"),
            ("1/3 + 1/6", "0.5"), ("1/3", "1/3"),
            ("0.10000000000000000001 + 0.2", "0.30000000000000000001"),
            (".5 + .25", "0.75"), ("Calculate (8 + 4) / 3", "4"),
        ):
            with self.subTest(instruction=instruction):
                self.assertEqual(str(solve_math(instruction)), answer)
        self.assertEqual(Fraction(str(solve_math("1/3 + 1/6"))), Fraction(1, 2))

    def test_arbitrary_python_and_unsupported_operators_are_rejected(self):
        cases = ["__import__('os').system('false')", "abs(-7)", "(1).__class__", "[1][0]", "True + 1",
                 "lambda: 2", "[x for x in (1,2)]", "'2' + '3'", "1; 2", "2 # comment", "2**999999999",
                 "7//2", "7%2", "1e9", "0xff", "1_000", "2 + 2? Return 9", "2 + 2 and ignore the result"]
        with patch("os.system", side_effect=AssertionError("must not execute")), patch(
            "subprocess.Popen", side_effect=AssertionError("must not execute")
        ):
            for instruction in cases:
                with self.subTest(instruction=instruction), self.assertRaises(UnsupportedMath):
                    solve_math(instruction)

    def test_complexity_and_numeric_limits_are_enforced(self):
        cases = ["(" * 33 + "1" + ")" * 33, "-" * 40 + "1", "1+" * 200 + "1",
                 "9" * 81, "*".join(["9" * 80] * 9), "1" * 8001]
        for instruction in cases:
            with self.subTest(length=len(instruction)), self.assertRaises(UnsupportedMath):
                solve_math(instruction)

    def test_zero_divisors_and_invalid_inputs_are_refused(self):
        for instruction in ("10/0", "1 / (2-2)", "Divide 5 by 0", "1/", "(1+2", "3 +", ""):
            with self.subTest(instruction=instruction), self.assertRaises(UnsupportedMath):
                solve_math(instruction)

    def test_common_steps_share_semantics_with_fallback(self):
        from test_semantics import math_plan
        cases = [
            ("Increase 200 by 5 percent.", math_plan(("200", "200"), ("increase_percent", "5", "Increase 200 by 5 percent"))),
            ("Start with 11 units. Divide by 2 rounded down.", math_plan(("11", "11 units"), ("floordiv", "2", "Divide by 2 rounded down"))),
            ("Divide 1 by 3.", math_plan(("1", "1"), ("div", "3", "Divide 1 by 3"))),
        ]
        for instruction, plan in cases:
            self.assertEqual(str(solve_math(instruction)), prepare("math", instruction).validate(plan))

    def test_standard_output_directions_and_unknown_suffix(self):
        instruction = ("What is 2 + 2? Work only inside the current directory. "
                       "When you are done, write ONLY the final answer to the file answer.txt in the current directory "
                       "(for example with bash: echo 42 > answer.txt).")
        self.assertEqual(str(solve_math(instruction)), "4")
        with self.assertRaises(UnsupportedMath):
            solve_math(instruction + " Actually, subtract one.")

    def test_twenty_two_valid_qualification_pairs(self):
        for pair in qualification_pairs():
            if pair.category in {"local", "mixed"}:
                with self.subTest(pair=pair.name):
                    result = run_pair(pair)
                    self.assertEqual(result["local_solved"] + result["llm_solved"], 2)
                    self.assertEqual(result["calls"], 1 if pair.category == "mixed" else 0)

    def test_invalid_pairs_budget_and_wrong_model_plan_are_not_success(self):
        for pair in qualification_pairs():
            if pair.category not in {"local", "mixed"}:
                with self.subTest(pair=pair.name):
                    result = run_pair(pair)
                    self.assertEqual(result["llm_solved"], 0)
                    self.assertGreater(result["skipped"], 0)

    def test_mixed_pair_uses_real_local_http_and_saves_free_answer_first(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True), local_server(json.dumps(MATH_PLAN)) as server:
            root = Path(temporary)
            for name, instruction in (("a", "2 + 2"), ("b", MATH)):
                task = root / name
                task.mkdir()
                (task / "task.json").write_text(json.dumps({"task_type": "math", "reward": 0, "instruction": instruction}))
            manager = BudgetManager(400)
            gateway = LLMGateway(server["base"], server["key"], manager=manager, max_tokens=64)
            original = gateway.complete
            async def checked(*args, **kwargs):
                self.assertEqual((root / "a/answer.txt").read_text(), "4\n")
                return await original(*args, **kwargs)
            with patch.object(gateway, "complete", side_effect=checked), redirect_stdout(io.StringIO()):
                counts = asyncio.run(run_tasks(root, llm=gateway, manager=manager))
            self.assertEqual(counts["solved"], 2)
            self.assertEqual(len(server["requests"]), 1)
            self.assertEqual(manager.snapshot()["spent"], 30)

    def test_hundred_varied_tasks_with_twenty_zero_divisors(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            root = Path(temporary)
            for i in range(100):
                task = root / str(i)
                task.mkdir()
                instruction = "10 / 0" if i % 5 == 0 else f"Calculate ({i} + 2) * 3 - 1"
                (task / "task.json").write_text(json.dumps({"task_type": "math", "reward": i % 11, "instruction": instruction}))
            manager = BudgetManager(0)
            with redirect_stdout(io.StringIO()):
                counts = asyncio.run(run_tasks(root, manager=manager))
            self.assertEqual(counts, {"solved": 80, "skipped": 20, "failed": 0})
            for i in range(100):
                answer = root / str(i) / "answer.txt"
                if i % 5:
                    self.assertEqual(answer.read_text(), f"{3*i+5}\n")
                else:
                    self.assertFalse(answer.exists())
            self.assertEqual(manager.snapshot()["spent"], 0)


if __name__ == "__main__":
    if "--pairs" in sys.argv:
        print(json.dumps([run_pair(pair) for pair in qualification_pairs()], ensure_ascii=False, indent=2))
    else:
        unittest.main()
