"""Independent instruction/plan pairs exercised through the actual workflow."""

import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from budget_manager import BudgetManager
from harness import run_tasks
from llm_gateway import LLMGateway
from test_llm import envelope, local_server


FACTS = ("# Ada\nBorn: 1980\nMentor: Bea\nEmployer: Acme\n"
         "# Bea\nEmployer: Bravo\nMentor: Cora\n"
         "# Cora\nEmployer: Bravo\n"
         "# Acme\nMentor: Cobalt\n"
         "# Bravo\nHeadquarters: Tokyo\n"
         "# Cobalt\nHeadquarters: Paris\n")


def math_plan(initial, *steps):
    return {"initial": {"value": initial[0], "quote": initial[1]},
            "steps": [{"op": op, "value": value, "quote": quote} for op, value, quote in steps]}


def condition(column, op, value, quote, kind="number"):
    return {"column": column, "op": op, "type": kind, "value": value, "quote": quote}


def data_plan(*filters, op="sum", column="units"):
    return {"file": "sales.csv", "operation": op, "column": column, "filters": list(filters)}


class SemanticTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def check_case(self, kind, instruction, plan, answer=None, *, files=None, wire=False):
        directory = self.root / str(len(list(self.root.iterdir())))
        task = directory / "task"
        task.mkdir(parents=True)
        (task / "task.json").write_text(json.dumps({
            "task_type": kind, "reward": 1, "instruction": instruction,
        }))
        for name, content in (files or {}).items():
            path = task / "workspace" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        manager = BudgetManager(100000)
        requests = []

        async def handle(request):
            requests.append(request)
            return httpx.Response(200, json=envelope(json.dumps(plan)))

        def invoke(gateway):
            with redirect_stdout(io.StringIO()):
                return asyncio.run(run_tasks(directory, llm=gateway, manager=manager))

        if wire:
            with local_server(json.dumps(plan)) as server:
                gateway = LLMGateway(server["base"], server["key"], manager=manager)
                counts = invoke(gateway)
                calls = len(server["requests"])
        else:
            gateway = LLMGateway("http://semantics.invalid/v1", "local-mock-key", manager=manager,
                                 transport=httpx.MockTransport(handle))
            counts = invoke(gateway)
            calls = len(requests)
        self.assertLessEqual(calls, 1, "validation must not make a second LLM request")
        self.assertEqual(manager.snapshot()["spent"], 30 if calls else 0)
        if answer is None:
            self.assertEqual(counts, {"solved": 0, "skipped": 1, "failed": 0})
            self.assertFalse((task / "answer.txt").exists())
        else:
            self.assertEqual(counts, {"solved": 1, "skipped": 0, "failed": 0})
            self.assertEqual((task / "answer.txt").read_text(), answer + "\n")
            self.assertEqual((task / "answer.txt").stat().st_mode & 0o777, 0o644)
        return calls

    def test_negative_audit_percentage_is_not_plain_addition(self):
        self.check_case("math", "The balance is 200. Add 5 percent. What is the balance?",
                        math_plan(("200", "200"), ("add", "5", "Add 5 percent")), wire=True)
        self.check_case("math", "Increase 200 by 5 percent.",
                        math_plan(("200", "200"), ("add", "5", "Increase 200 by 5 percent")), wire=True)

    def test_negative_audit_equal_range_requires_upper_bound(self):
        self.check_case("data_analysis",
                        "Calculate total units in sales.csv for month from 5 to 5 inclusive.",
                        data_plan(condition("month", ">=", "5", "month from 5 to 5 inclusive")),
                        files={"sales.csv": "month,units\n5,7\n6,100\n"}, wire=True)

    def test_negative_audit_search_relation_order_is_not_interchangeable(self):
        self.check_case("search", "What city is Ada's mentor's employer based in?",
                        {"entity": "Ada", "relations": ["employer", "mentor"],
                         "field": "headquarters", "answer": "Paris"},
                        files={"kb/facts.md": FACTS}, wire=True)

    def test_positive_percentage_change_over_real_local_http(self):
        self.assertEqual(self.check_case("math", "Increase 200 by 5 percent.",
                         math_plan(("200", "200"),
                                   ("increase_percent", "5", "Increase 200 by 5 percent")),
                         "210", wire=True), 1)

    def test_positive_percentage_equivalent_multiplier(self):
        self.check_case("math", "The balance is 200. Add 5 percent. What is the balance?",
                        math_plan(("200", "200"), ("mul", "1.05", "Add 5 percent")), "210")

    def test_positive_percent_decrease_and_percent_of_value(self):
        for instruction, op, value, answer in (
            ("Decrease 200 by 5 percent.", "decrease_percent", "5", "190"),
            ("What is 5 percent of 200?", "percent", "5", "10"),
            ("Increase 200 by 2.5%.", "increase_percent", "2.5", "205"),
            ("Increase 200 by 0 percent.", "increase_percent", "0", "200"),
        ):
            with self.subTest(instruction=instruction):
                self.check_case("math", instruction,
                                math_plan(("200", "200"), (op, value, instruction.rstrip(".?!"))), answer)

    def test_positive_sequential_percentage_changes_compound(self):
        self.check_case("math", "Start with 200 units. Increase by 10 percent. Decrease by 5 percent.",
                        math_plan(("200", "200 units"),
                                  ("increase_percent", "10", "Increase by 10 percent"),
                                  ("decrease_percent", "5", "Decrease by 5 percent")), "209")

    def test_negative_percentage_rate_cannot_change_direction(self):
        self.check_case("math", "Decrease 200 by 5 percent.",
                        math_plan(("200", "200"), ("increase_percent", "5", "Decrease 200 by 5 percent")))

    def test_negative_percentage_points_and_conditional_changes_are_unsupported(self):
        for instruction in ("Increase 200 by 5 percentage points.",
                            "Increase 200 by 5 percent if the balance is positive."):
            with self.subTest(instruction=instruction):
                self.check_case("math", instruction,
                                math_plan(("200", "200"), ("increase_percent", "5", instruction.rstrip("."))))

    def test_positive_operand_order_subtraction_and_division(self):
        for instruction, initial, operand, op, answer in (
            ("Subtract 5 from 20.", "20", "5", "sub", "15"),
            ("Divide 12 by 3.", "12", "3", "div", "4"),
        ):
            with self.subTest(instruction=instruction):
                self.check_case("math", instruction,
                                math_plan((initial, initial), (op, operand, instruction.rstrip("."))), answer)

    def test_negative_reversed_operands(self):
        for instruction, initial, operand, op in (
            ("Subtract 5 from 20.", "5", "20", "sub"),
            ("Divide 12 by 3.", "3", "12", "div"),
        ):
            with self.subTest(instruction=instruction):
                self.check_case("math", instruction,
                                math_plan((initial, initial), (op, operand, instruction.rstrip("."))))

    def test_positive_floor_division(self):
        self.check_case("math", "Start with 11 units. Divide by 2 rounded down.",
                        math_plan(("11", "11 units"), ("floordiv", "2", "Divide by 2 rounded down")), "5")

    def test_positive_common_existing_verb_forms_and_then(self):
        self.check_case("math", "Initially, there are 10 coins. Then it added 5 coins. "
                        "Then it was multiplied by 2.",
                        math_plan(("10", "10 coins"), ("add", "5", "it added 5 coins"),
                                  ("mul", "2", "it was multiplied by 2")), "30")
        self.check_case("math", "You have 10 coins. It lost 2 coins. It is divided by 2.",
                        math_plan(("10", "10 coins"), ("sub", "2", "It lost 2 coins"),
                                  ("div", "2", "It is divided by 2")), "4")
        self.check_case("math", "Start with 10 coins. It shipped 2 coins. It is multiplied by 3.",
                        math_plan(("10", "10 coins"), ("sub", "2", "It shipped 2 coins"),
                                  ("mul", "3", "It is multiplied by 3")), "24")

    def test_negative_floor_division_cannot_be_plain_division(self):
        self.check_case("math", "Start with 11 units. Divide by 2 rounded down.",
                        math_plan(("11", "11 units"), ("div", "2", "Divide by 2 rounded down")))

    def test_negative_division_by_zero(self):
        self.check_case("math", "Divide 12 by 0.",
                        math_plan(("12", "12"), ("div", "0", "Divide 12 by 0")))

    def test_negative_nested_or_reordered_operations(self):
        self.check_case("math", "Start with 10 coins. Add the product of 5 and 3.",
                        math_plan(("10", "10 coins"), ("add", "5", "Add the product of 5 and 3")))
        self.check_case("math", "Start with 10 coins. Add 5 coins. Multiply by 3.",
                        math_plan(("10", "10 coins"), ("mul", "3", "Multiply by 3"),
                                  ("add", "5", "Add 5 coins")))

    def test_negative_unknown_clause_cannot_silently_change_the_result(self):
        self.check_case("math", "Start with 10 coins. Add 5 coins. Give them all away.",
                        math_plan(("10", "10 coins"), ("add", "5", "Add 5 coins")))

    def test_negative_runner_directions_cannot_hide_an_extra_operation(self):
        self.check_case("math", "Start with 10 coins. Add 5 coins. "
                        "Work only inside the current directory. Subtract 3 coins.",
                        math_plan(("10", "10 coins"), ("add", "5", "Add 5 coins")))

    def test_positive_known_runner_directions_are_preserved(self):
        self.check_case("math", "Start with 10 coins. Add 5 coins. "
                        "Work only inside the current directory. When you are done, write ONLY the final answer "
                        "to the file answer.txt in the current directory (for example with bash: echo 42 > answer.txt).",
                        math_plan(("10", "10 coins"), ("add", "5", "Add 5 coins")), "15")

    def test_negative_wrong_percentage_multiplier(self):
        self.check_case("math", "Increase 200 by 5 percent.",
                        math_plan(("200", "200"), ("mul", "1.5", "Increase 200 by 5 percent")))

    def test_positive_equal_interval_both_bounds_over_real_http(self):
        self.assertEqual(self.check_case(
            "data_analysis", "Calculate total units in sales.csv for month from 5 to 5 inclusive.",
            data_plan(condition("month", ">=", "5", "month from 5 to 5 inclusive"),
                      condition("month", "<=", "5", "month from 5 to 5 inclusive")), "7",
            files={"sales.csv": "month,units\n4,300\n5,7\n6,100\n"}, wire=True), 1)

    def test_positive_equal_interval_can_use_exact_equality(self):
        self.check_case("data_analysis", "Calculate total units in sales.csv for month from 5 to 5 inclusive.",
                        data_plan(condition("month", "=", "5", "month from 5 to 5 inclusive")), "7",
                        files={"sales.csv": "month,units\n5,7\n6,100\n"})

    def test_positive_exclusive_range_and_empty_selection(self):
        for low, high, answer in (("4", "6", "7"), ("5", "5", "0")):
            quote = f"month between {low} and {high} exclusive"
            self.check_case("data_analysis", f"Calculate total units in sales.csv where {quote}.",
                            data_plan(condition("month", ">", low, quote),
                                      condition("month", "<", high, quote)), answer,
                            files={"sales.csv": "month,units\n4,3\n5,7\n6,100\n"})

    def test_negative_exclusive_range_cannot_be_made_inclusive(self):
        quote = "month between 4 and 6 exclusive"
        self.check_case("data_analysis", f"Calculate total units in sales.csv where {quote}.",
                        data_plan(condition("month", ">=", "4", quote), condition("month", "<=", "6", quote)),
                        files={"sales.csv": "month,units\n4,3\n5,7\n6,100\n"})

    def test_positive_all_aggregates_without_filters(self):
        for word, op, answer in (("sum", "sum", "10"), ("count", "count", "2"),
                                 ("minimum", "min", "2"), ("maximum", "max", "8"),
                                 ("average", "avg", "5")):
            with self.subTest(operation=op):
                self.check_case("data_analysis", f"Calculate the {word} of units in sales.csv.",
                                data_plan(op=op), answer, files={"sales.csv": "units\n2\n8\n"})

    def test_positive_row_count_and_single_condition(self):
        self.check_case("data_analysis", "How many rows are there in sales.csv where product = 'gear'?",
                        data_plan(condition("product", "=", "gear", "product = 'gear'", "string"),
                                  op="count", column=None), "2",
                        files={"sales.csv": "product,units\ngear,2\ngear,8\nother,100\n"})
        self.check_case("data_analysis", "Calculate total units in sales.csv where units at least 5.",
                        data_plan(condition("units", ">=", "5", "units at least 5")), "13",
                        files={"sales.csv": "units\n4\n5\n8\n"})

    def test_positive_all_rows_and_mixed_boundary_comparisons(self):
        self.check_case("data_analysis", "Calculate total units in sales.csv for all rows.",
                        data_plan(), "110", files={"sales.csv": "units\n2\n8\n100\n"})
        self.check_case("data_analysis", "Calculate total units in sales.csv where month > 4 and month <= 6.",
                        data_plan(condition("month", ">", "4", "month > 4"),
                                  condition("month", "<=", "6", "month <= 6")), "107",
                        files={"sales.csv": "month,units\n4,3\n5,7\n6,100\n7,900\n"})

    def test_positive_count_of_column_named_rows(self):
        self.check_case("data_analysis", "Calculate count of rows in sales.csv.",
                        data_plan(op="count", column="rows"), "2", files={"sales.csv": "rows\nA\nB\n"})
        self.check_case("data_analysis", "Calculate count of rows in sales.csv.",
                        data_plan(op="count", column="ROWS"), "2", files={"sales.csv": "ROWS\nA\nB\n"})

    def test_negative_wrong_aggregate_target_or_invented_filter(self):
        instruction = "Calculate total units in sales.csv where price at least 5."
        files = {"sales.csv": "units,price\n7,10\n100,2\n"}
        predicate = condition("price", ">=", "5", "price at least 5")
        self.check_case("data_analysis", instruction, data_plan(predicate, column="price"), files=files)
        self.check_case("data_analysis", instruction, data_plan(predicate, op="max"), files=files)
        self.check_case("data_analysis", "Calculate total units in sales.csv.",
                        data_plan(predicate), files=files)

    def test_positive_multiple_conditions_in_different_plan_order(self):
        instruction = ("Calculate total units in sales.csv for product 'gear' and region not equal to 'south' "
                       "and month from 4 to 6 inclusive and units > 5.")
        filters = [condition("product", "=", "gear", "product 'gear'", "string"),
                   condition("region", "!=", "south", "region not equal to 'south'", "string"),
                   condition("month", ">=", "4", "month from 4 to 6 inclusive"),
                   condition("month", "<=", "6", "month from 4 to 6 inclusive"),
                   condition("units", ">", "5", "units > 5")]
        self.check_case("data_analysis", instruction, data_plan(*reversed(filters)), "18", files={
            "sales.csv": "product,region,month,units\ngear,north,4,2\ngear,north,5,7\n"
                         "gear,north,6,11\ngear,south,5,100\nother,north,5,500\n"})

    def test_negative_equal_numbers_do_not_hide_an_omitted_condition(self):
        self.check_case("data_analysis", "Calculate total units in sales.csv where month >= 5 and units >= 5.",
                        data_plan(condition("month", ">=", "5", "month >= 5")),
                        files={"sales.csv": "month,units\n5,2\n5,7\n"})

    def test_negative_numeric_value_cannot_be_assigned_to_another_column(self):
        self.check_case("data_analysis", "Calculate total units in sales.csv where month >= 5 and price <= 5.",
                        data_plan(condition("month", "<=", "5", "month >= 5"),
                                  condition("price", ">=", "5", "price <= 5")),
                        files={"sales.csv": "month,price,units\n5,4,7\n4,6,100\n"})

    def test_positive_string_literals_keep_type_and_embedded_keywords(self):
        self.check_case("data_analysis", "Calculate total units in sales.csv where product = '005'.",
                        data_plan(condition("product", "=", "005", "product = '005'", "string")), "7",
                        files={"sales.csv": "product,units\n005,7\n5,100\n"})
        self.check_case("data_analysis", "Calculate total units in sales.csv for product 'rock and roll'.",
                        data_plan(condition("product", "=", "rock and roll", "product 'rock and roll'", "string")),
                        "7", files={"sales.csv": "product,units\nrock and roll,7\nother,100\n"})

    def test_negative_filter_type_cannot_change(self):
        self.check_case("data_analysis", "Calculate total units in sales.csv where product = '005'.",
                        data_plan(condition("product", "=", "005", "product = '005'")),
                        files={"sales.csv": "product,units\n005,7\n5,100\n"})

    def test_negative_or_grouping_and_derived_columns_are_unsupported(self):
        for query in ("Calculate total units in sales.csv where month = 5 or month = 6.",
                      "Calculate total units in sales.csv group by month.",
                      "Calculate total units * price in sales.csv."):
            with self.subTest(query=query):
                self.check_case("data_analysis", query, data_plan(),
                                files={"sales.csv": "month,units,price\n5,7,2\n"})

    def test_positive_correct_search_order_over_real_http(self):
        self.assertEqual(self.check_case("search", "What city is Ada's mentor's employer based in?",
                         {"entity": "Ada", "relations": ["mentor", "employer"],
                          "field": "headquarters", "answer": "Tokyo"}, "Tokyo",
                         files={"kb/facts.md": FACTS}, wire=True), 1)

    def test_positive_different_question_has_different_correct_order(self):
        self.check_case("search", "What city is Ada's employer's mentor based in?",
                        {"entity": "Ada", "relations": ["employer", "mentor"],
                         "field": "headquarters", "answer": "Paris"}, "Paris", files={"kb/facts.md": FACTS})

    def test_positive_search_nested_of_and_three_hops(self):
        self.check_case("search", "Where is the employer of the mentor of the mentor of Ada based?",
                        {"entity": "Ada", "relations": ["mentor", "mentor", "employer"],
                         "field": "headquarters", "answer": "Tokyo"}, "Tokyo", files={"kb/facts.md": FACTS})

    def test_positive_search_employer_of_mentor_and_mentor_of_employer(self):
        for question, relations, field, answer in (
            ("What is the employer of the mentor of Ada?", ["mentor"], "employer", "Bravo"),
            ("Who is the mentor of the employer of Ada?", ["employer"], "mentor", "Cobalt"),
            ("What is the birth year of Ada?", [], "born", "1980"),
        ):
            with self.subTest(question=question):
                self.check_case("search", question, {"entity": "Ada", "relations": relations,
                                "field": field, "answer": answer}, answer, files={"kb/facts.md": FACTS})

    def test_negative_inverse_search_is_not_a_forward_employer_link(self):
        self.check_case("search", "Who is the mentor of an employee of Acme?",
                        {"entity": "Ada", "relations": [], "field": "mentor", "answer": "Bea"},
                        files={"kb/facts.md": FACTS})

    def test_negative_search_wrong_field_and_missing_hop(self):
        for relations, field, answer in ((["mentor", "employer"], "born", "1980"),
                                         (["mentor"], "headquarters", "Tokyo")):
            self.check_case("search", "What city is Ada's mentor's employer based in?",
                            {"entity": "Ada", "relations": relations, "field": field, "answer": answer},
                            files={"kb/facts.md": FACTS})

    def test_negative_ambiguous_and_overlong_search_chains(self):
        for question in ("What city is Ada's mentor or employer based in?",
                         "What city is Ada's mentor's mentor's mentor's employer based in?"):
            self.check_case("search", question, {"entity": "Ada", "relations": ["mentor", "employer"],
                            "field": "headquarters", "answer": "Tokyo"}, files={"kb/facts.md": FACTS})

    def test_negative_literal_entity_cannot_hide_an_ambiguous_relation(self):
        self.check_case("search", "What city is Ada's mentor based in?",
                        {"entity": "Ada's mentor", "relations": [], "field": "headquarters", "answer": "Paris"},
                        files={"kb/facts.md": FACTS + "# Ada's mentor\nHeadquarters: Paris\n"})


if __name__ == "__main__":
    unittest.main()
