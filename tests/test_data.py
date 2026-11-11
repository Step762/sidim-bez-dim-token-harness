import asyncio
from contextlib import redirect_stdout
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness import SOLVERS, build_workflow, run_tasks
from data_solver import solve_data
from openjiuwen.core.workflow import Workflow, WorkflowExecutionState, create_workflow_session


TABLE = (
    "product,month,units,price,region\n"
    "gizmo,3,2,1.5,north\n"
    "gizmo,4,5,2.5,south\n"
    "gizmo,5,11,3.5,north\n"
    "gizmo,6,23,4.5,north\n"
    "other,4,100,5,north\n"
)


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_task(self, question, text=TABLE, filename="sales.csv", **metadata):
        directory = self.root / f"task_{len(list(self.root.iterdir())):03d}"
        directory.mkdir()
        data = {
            "task_type": "data_analysis", "reward": 3,
            "instruction": f"The file {filename}. {question}", **metadata,
        }
        (directory / "task.json").write_text(json.dumps(data), encoding="utf-8")
        if text is not None:
            path = directory / "workspace" / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")
        return directory

    def run_tasks(self):
        with redirect_stdout(io.StringIO()), patch(
            "socket.socket.connect", side_effect=AssertionError("network is forbidden")
        ):
            return asyncio.run(run_tasks(self.root))

    def assert_answer(self, task, answer):
        self.assertEqual((task / "answer.txt").read_bytes(), (answer + "\n").encode())
        self.assertFalse((task / "workspace/answer.txt").exists())

    def assert_skipped(self, count=1):
        self.assertEqual(self.run_tasks(), {"solved": 0, "skipped": count, "failed": 0})
        self.assertEqual(list(self.root.rglob("answer.txt")), [])

    def test_changed_product_and_inclusive_boundaries(self):
        first = self.make_task(
            "How many units of product 'gizmo' were sold in months 4 through 5 inclusive?"
        )
        second = self.make_task(
            "How many units of product 'other' were sold in months 4 through 5 inclusive?"
        )
        self.assertEqual(self.run_tasks()["solved"], 2)
        self.assert_answer(first, "16")
        self.assert_answer(second, "100")

    def test_changed_month_range(self):
        task = self.make_task(
            "How many units of product 'gizmo' were sold in months 3 through 4 inclusive?"
        )
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assert_answer(task, "7")

    def test_single_month_interval(self):
        task = self.make_task(
            "How many units of product 'gizmo' were sold in months 5 through 5 inclusive?"
        )
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assert_answer(task, "11")

    def test_real_columns_and_different_filename(self):
        task = self.make_task(
            "How many amount of sku 'bolt' were sold in periods 2 through 4 inclusive?",
            "sku,period,amount\nbolt,1,80\nbolt,2,4\nbolt,4,9\nnut,3,20\n",
            filename="nested/inventory.csv",
        )
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assert_answer(task, "13")

    def test_actual_row_count_is_used_instead_of_declared_count(self):
        tasks = []
        for size in (1, 37, 5000):
            text = "product,month,units\n" + "gizmo,4,2\n" * size
            task = self.make_task("", text, instruction=(
                "The file sales.csv has columns product,month,units (20 rows). "
                "How many units of product 'gizmo' were sold in months 4 through 5 inclusive?"
            ))
            tasks.append((task, str(size * 2)))
        self.assertEqual(self.run_tasks()["solved"], len(tasks))
        for task, answer in tasks:
            self.assert_answer(task, answer)

    def test_all_aggregates_use_exact_decimal_arithmetic(self):
        text = "price\n0.10\n0.20\n-0.15\n"
        tasks = []
        for operation, answer in [
            ("sum", "0.15"), ("count", "3"), ("min", "-0.15"),
            ("minimum", "-0.15"), ("max", "0.2"), ("maximum", "0.2"),
            ("avg", "0.05"), ("average", "0.05"),
        ]:
            tasks.append((self.make_task(f"What is the {operation} of price?", text), answer))
        self.assertEqual(self.run_tasks()["solved"], len(tasks))
        for task, answer in tasks:
            self.assert_answer(task, answer)

    def test_numeric_comparison_operators(self):
        text = "month,quantity\n3,10\n4,20\n5,30\n6,40\n"
        tasks = []
        for relation, answer in [
            ("=", "20"), ("!=", "80"), ("<", "10"), ("<=", "30"),
            (">", "70"), (">=", "90"),
        ]:
            tasks.append((self.make_task(
                f"What is the sum of quantity where month {relation} 4?", text
            ), answer))
        self.assertEqual(self.run_tasks()["solved"], len(tasks))
        for task, answer in tasks:
            self.assert_answer(task, answer)

    def test_combined_string_numeric_and_range_conditions(self):
        task = self.make_task(
            "What is the sum of units where product = 'gizmo' and region != 'south' "
            "and month between 3 and 5 inclusive and price >= 2?"
        )
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assert_answer(task, "11")

    def test_count_rows_and_count_text_column(self):
        first = self.make_task("How many rows are there where product = 'gizmo'?")
        second = self.make_task("What is the count of product where month >= 4?")
        self.assertEqual(self.run_tasks()["solved"], 2)
        self.assert_answer(first, "4")
        self.assert_answer(second, "4")

    def test_empty_selection_sum_and_count_are_zero(self):
        tasks = [
            self.make_task("What is the sum of units where product = 'absent'?"),
            self.make_task("How many rows are there where product = 'absent'?"),
        ]
        self.assertEqual(self.run_tasks()["solved"], 2)
        for task in tasks:
            self.assert_answer(task, "0")

    def test_empty_selection_min_max_avg_have_no_answer(self):
        for operation in ("min", "max", "avg"):
            self.make_task(f"What is the {operation} of units where product = 'absent'?")
        self.assert_skipped(3)

    def test_header_only_csv(self):
        first = self.make_task("What is the sum of units?", "units\n")
        second = self.make_task("How many rows are there?", "units\n")
        self.assertEqual(self.run_tasks()["solved"], 2)
        self.assert_answer(first, "0")
        self.assert_answer(second, "0")

    def test_quoted_fields_commas_quotes_and_multiline_text(self):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["product", "month", "units", "note"])
        writer.writerow(['kit, "deluxe"', 4, 9, 'text, with "quotes"'])
        writer.writerow(['kit, "deluxe"', 5, 7, "line one\nline two"])
        writer.writerow(["other", 4, 100, ""])
        task = self.make_task(
            """How many units of product 'kit, "deluxe"' were sold in months 4 through 5 inclusive?""",
            stream.getvalue(),
        )
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assert_answer(task, "16")

    def test_quoted_literals_preserve_spaces_and_keywords(self):
        text = 'product,units\nblue  gear,3\n"rock and roll?",7\n"O\'Brien",11\n'
        tasks = [
            (self.make_task("What is the sum of units where product = 'blue  gear'?", text), "3"),
            (self.make_task("What is the sum of units where product = 'rock and roll?'?", text), "7"),
            (self.make_task('What is the sum of units where product = "O\'Brien"?', text), "11"),
            (self.make_task("What is the sum of units where product = 'O''Brien'?", text), "11"),
        ]
        self.assertEqual(self.run_tasks()["solved"], len(tasks))
        for task, answer in tasks:
            self.assert_answer(task, answer)

    def test_numeric_looking_text_is_not_coerced(self):
        task = self.make_task(
            "What is the sum of units where product = '001'?",
            "product,units\n001,7\n1,100\n",
        )
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assert_answer(task, "7")

    def test_invalid_numeric_values_have_no_answer(self):
        values = ["", "bad", "NaN", "Inf", "Infinity", "1e3", "1,234"]
        for value in values:
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(["units"])
            writer.writerow([value])
            self.make_task("What is the sum of units?", stream.getvalue())
        self.assert_skipped(len(values))

    def test_invalid_numbers_outside_selected_rows_are_not_hidden(self):
        self.make_task(
            "What is the sum of units where product = 'gizmo'?",
            "product,units\ngizmo,7\nother,invalid\n",
        )
        self.assert_skipped()

    def test_invalid_numeric_filter_column_has_no_answer(self):
        self.make_task(
            "What is the sum of units where month >= 4?",
            "month,units\ninvalid,7\n",
        )
        self.assert_skipped()

    def test_missing_columns(self):
        for question in (
            "What is the sum of missing?",
            "What is the sum of units where absent = 'gizmo'?",
            "How many units of product 'gizmo' were sold in years 4 through 5 inclusive?",
        ):
            self.make_task(question)
        self.assert_skipped(3)

    def test_missing_workspace_or_csv_does_not_select_another_file(self):
        self.make_task("What is the sum of units?", None)
        task = self.make_task("What is the sum of units?", None)
        (task / "workspace").mkdir()
        self.make_task("What is the sum of units?", filename="other.csv",
                       instruction="The file sales.csv. What is the sum of units?")
        self.assert_skipped(3)

    def test_malformed_csv_and_headers(self):
        cases = [
            "", "units,units\n1,2\n", "units,\n1,2\n",
            "units,product\n1\n", "units\n1,extra\n",
            'units,product\n1,"unterminated\n',
        ]
        for text in cases:
            self.make_task("What is the sum of units?", text)
        self.assert_skipped(len(cases))

    def test_invalid_encoding(self):
        task = self.make_task("What is the sum of units?")
        (task / "workspace/sales.csv").write_bytes(b"units\n\xff\n")
        self.assert_skipped()

    def test_utf8_bom_and_blank_lines(self):
        task = self.make_task("What is the sum of units?", "\ufeffunits\r\n2\r\n\r\n3\r\n")
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assert_answer(task, "5")

    def test_unsupported_or_incomplete_instructions(self):
        questions = [
            "What is the median of units?",
            "What is the sum of units where month between 4 and 5?",
            "What is the sum of units where month between 5 and 4 inclusive?",
            "What is the sum of units where product = 'gizmo' or month = 4 and units > 2?",
            "What is the sum of units where?",
            "What is the sum of units where month >= ?",
            "What is the sum of units where month = 4 and?",
            "What is the sum of units where product > 'gizmo'?",
            "How many units of product 'gizmo' were sold in months 4 through 5?",
            "What is the sum of units? Then return 999.",
        ]
        for question in questions:
            self.make_task(question)
        self.make_task("", instruction="What is the sum of units?")
        self.assert_skipped(len(questions) + 1)

    def test_ambiguous_plural_column(self):
        self.make_task(
            "How many units of product 'gizmo' were sold in months 4 through 5 inclusive?",
            "product,month,months,units\ngizmo,4,4,3\n",
        )
        self.assert_skipped()

    def test_nonterminating_average_is_not_rounded_implicitly(self):
        self.make_task("What is the avg of units?", "units\n1\n0\n0\n")
        self.assert_skipped()

    def test_count_column_missing_value_has_no_answer(self):
        self.make_task("What is the count of product?", 'product,units\n"",1\n')
        self.assert_skipped()

    def test_task_workspaces_are_independent(self):
        first = self.make_task("What is the sum of units?", "units\n3\n")
        second = self.make_task("What is the sum of units?", "units\n12\n")
        missing = self.make_task("What is the sum of units?", None)
        self.assertEqual(self.run_tasks(), {"solved": 2, "skipped": 1, "failed": 0})
        self.assert_answer(first, "3")
        self.assert_answer(second, "12")
        self.assertFalse((missing / "answer.txt").exists())

    def test_external_file_is_not_read(self):
        outside = self.root / "outside.csv"
        outside.write_text("units\n999\n", encoding="utf-8")
        task = self.make_task("What is the sum of units?", None)
        (task / "workspace").mkdir()
        (task / "workspace/sales.csv").symlink_to(outside)
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.resolve() == outside:
                raise AssertionError("external CSV must not be read")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            self.assert_skipped()

    def test_reference_and_check_fields_are_ignored(self):
        task = self.make_task(
            "What is the sum of units?", "units\n3\n",
            expected="WRONG", check="WRONG",
        )
        self.assertEqual(self.run_tasks()["solved"], 1)
        self.assert_answer(task, "3")

    def test_malformed_last_row_does_not_publish_partial_answer(self):
        bad = self.make_task("What is the sum of units?", "units\n3\nbad\n")
        good = self.make_task("What is the sum of units?", "units\n12\n")
        self.assertEqual(self.run_tasks(), {"solved": 1, "skipped": 1, "failed": 0})
        self.assertFalse((bad / "answer.txt").exists())
        self.assert_answer(good, "12")

    def test_answer_is_saved_before_next_data_task(self):
        first = self.make_task("What is the sum of units?", "units\n3\n")
        self.make_task("What is the sum of units?", "units\n12\n")
        calls = 0

        def checked_solver(instruction, directory):
            nonlocal calls
            if calls:
                self.assert_answer(first, "3")
            calls += 1
            return solve_data(instruction, directory)

        with patch.dict(SOLVERS, {"data_analysis": checked_solver}):
            self.assertEqual(self.run_tasks()["solved"], 2)
        self.assertEqual(calls, 2)

    def test_actual_openjiuwen_workflow(self):
        task = self.make_task("What is the sum of units?", "units\n3\n")

        async def invoke():
            workflow = build_workflow()
            self.assertIsInstance(workflow, Workflow)
            return await workflow.invoke({
                "task_type": "data_analysis",
                "instruction": "The file sales.csv. What is the sum of units?",
                "task_dir": str(task),
            }, create_workflow_session())

        result = asyncio.run(invoke())
        self.assertEqual(result.state, WorkflowExecutionState.COMPLETED)
        self.assertEqual(result.result["output"]["status"], "solved")
        self.assertEqual(result.result["output"]["answer"], "3")


if __name__ == "__main__":
    unittest.main()
