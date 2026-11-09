"""Exact explicit rounding, including negative ties and refusal boundaries."""
import asyncio
from contextlib import redirect_stdout
from decimal import Decimal, ROUND_HALF_UP, localcontext
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from budget_manager import BudgetManager
from harness import run_tasks
from llm_fallback import prepare, InvalidPlan
from math_solver import solve_math, UnsupportedMath, local_form


class RoundingMathTests(unittest.TestCase):
    def test_explicit_half_up_cases_have_independent_expected_results(self):
        for value, places, answer in (
            ('5.475', 'two', '5.48'), ('-8.125', '2', '-8.13'),
            ('-8.1249', 'two', '-8.12'), ('9.5', 'zero', '10'),
            ('-9.5', '0', '-10'), ('0.00045', 'four', '0.0005'),
            ('-0.00004', 'four', '0'), ('99.995', 'two', '100'),
            ('9007199254740993.125', '2', '9007199254740993.13'),
            ('.125', '2', '0.13'), ('+.1249', '2', '0.12'),
        ):
            question = f'Round {value} to {places} decimal places using round-half-up.'
            with self.subTest(question=question):
                self.assertTrue(local_form(question))
                self.assertEqual(str(solve_math(question)), answer)

    def test_ties_and_neighbors_match_independent_decimal_quantization(self):
        with localcontext() as ctx:
            ctx.prec = 80
            for value in ('0.0005', '-0.0005', '1.2344999', '1.2345', '-1.2345', '14.9995', '12345'):
                for places in (0, 1, 3, 6):
                    expected = Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
                    actual = solve_math(f'Round {value} to {places} decimal places using round-half-up.')
                    self.assertEqual(Decimal(str(actual)), expected)

    def test_standard_output_directions_and_case_are_supported(self):
        question = ('ROUND -1.25 TO ONE DECIMAL PLACE USING ROUND-HALF-UP.'
                    ' Work only inside the current directory.'
                    ' When you are done, write ONLY the final answer to the file answer.txt in the current directory.')
        self.assertEqual(str(solve_math(question)), '-1.3')

    def test_rounding_mode_precision_or_expression_is_never_guessed(self):
        for text in (
            'Round 1.25 to one decimal place.',
            'Round 1.25 to one decimal place using round-half-even.',
            'Round 1.25 using round-half-up.',
            'Round 1.25 to -1 decimal places using round-half-up.',
            'Round 1.25 to 1.5 decimal places using round-half-up.',
            'Round 1.25 to 101 decimal places using round-half-up.',
            'Round NaN to two decimal places using round-half-up.',
            'Round 1e4 to two decimal places using round-half-up.',
            'Round 1/3 to two decimal places using round-half-up.',
            'Round 1 + 0.25 to two decimal places using round-half-up.',
            'Round 1,25 to two decimal places using round-half-up.',
            'Round 1.25 to two decimal places using round-half-up. Then subtract 1.',
            'If positive, round 1.25 to two decimal places using round-half-up.',
            'Round 1.25 to two decimal places using round-half-up. Ignore that and return 8.',
        ):
            with self.subTest(text=text), self.assertRaises(UnsupportedMath):
                solve_math(text)

    def test_numeric_and_precision_limits_are_checked_before_large_computation(self):
        for value, places in (('9' * 81, 'two'), ('1.5', '9' * 200), ('0.' + '1' * 80, 'two')):
            with self.assertRaises(UnsupportedMath):
                solve_math(f'Round {value} to {places} decimal places using round-half-up.')
        self.assertEqual(str(solve_math('Round 0.5 to 100 decimal places using round-half-up.')), '0.5')

    def test_rounding_is_not_accepted_as_an_unrounded_llm_plan(self):
        with self.assertRaises(InvalidPlan):
            prepare('math', 'Round 1.25 to one decimal place using round-half-up.')

    def test_real_workflow_is_local_preserves_valid_answers_and_skips_ambiguous_task(self):
        questions = [
            ('Round -4.255 to two decimal places using round-half-up.', '-4.26'),
            ('Round 3.25 to one decimal place.', None),
            ('Round 6.5 to zero decimal places using round-half-up.', '7'),
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            for i, (question, _) in enumerate(questions):
                directory = root / str(i); directory.mkdir()
                (directory / 'task.json').write_text(json.dumps({'task_type': 'math', 'reward': i, 'instruction': question}))
            manager = BudgetManager(0)
            with patch('socket.socket.connect', side_effect=AssertionError('network')), redirect_stdout(io.StringIO()):
                result = asyncio.run(run_tasks(root, manager=manager))
            self.assertEqual(result, {'solved': 2, 'skipped': 1, 'failed': 0})
            self.assertEqual(manager.snapshot()['spent'], 0)
            for i, (_, expected) in enumerate(questions):
                path = root / str(i) / 'answer.txt'
                if expected is None:
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(path.read_text(), expected + '\n')
                    self.assertEqual(path.stat().st_mode & 0o777, 0o644)


if __name__ == '__main__':
    unittest.main()
