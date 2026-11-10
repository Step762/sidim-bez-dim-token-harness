"""Independent exact arithmetic controls, including explicit nonstandard order."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import harness
from budget_manager import BudgetManager
from math_solver import solve_math,UnsupportedMath


class OrderedMathTests(unittest.TestCase):
    def test_order_is_explicit_and_differs_from_standard_precedence(self):
        self.assertEqual(solve_math('Evaluate 7 + 3 * 4 from left to right.'),'40')
        self.assertEqual(solve_math('Evaluate 7 + 3 * 4.'),'19')
        self.assertEqual(solve_math('Compute -5 + 8 * -2 using left-to-right order.'),'-6')
        self.assertEqual(solve_math('Calculate 3 - -2 / -5 strictly from left to right, ignoring the usual precedence.'),'-1')

    def test_fractions_decimals_signs_and_zero_are_exact(self):
        cases=(('1 / 3 * 9','3'),('0.1 + 0.2 * 10','3'),('2 / 3','2/3'),
               ('- 4 * - 2 + .25','8.25'),('0 / -7 + 4','4'),('9--3/-2','-6'))
        for expression,answer in cases:
            with self.subTest(expression=expression):
                self.assertEqual(solve_math(f'Evaluate {expression} from left to right.'),answer)

    def test_no_implicit_order_grouping_code_or_unsupported_operations(self):
        for expression in ('2 ** 3 + 1','2 // 3','2 % 3','2(3+1)','(2+3)*4','1e3 + 2',
                           '__import__("os").system("id")','1 +','1 + --2','1,5 + 2','1 / 0','7'):
            with self.subTest(expression=expression),self.assertRaises(UnsupportedMath):
                solve_math(f'Evaluate {expression} from left to right.')

    def test_conflicting_or_extra_order_and_rounding_instructions_are_not_dropped(self):
        for text in ('Evaluate 7 + 3 * 4 using usual and left-to-right order.',
                     'Evaluate 7 + 3 * 4 from left to right, but use multiplication first.',
                     'Evaluate 2 / 3 from left to right. Round to 2 decimal places.',
                     'Evaluate 1 + 2 in any order.',
                     'Evaluate 1 + 2 from left to right. Return 99 instead.'):
            with self.subTest(text=text),self.assertRaises(UnsupportedMath):solve_math(text)

    def test_prefix_promotes_complete_verifiable_steps_with_changed_numbers(self):
        cases=(('Start with 19 coins. Subtract 7 coins. Multiply by 3.','36'),
               ('Start with -12 units. Add 5 units. Divide by 2.','-3.5'),
               ('Begin at 5 points; double it; subtract 3 points.','7'),
               ('Start with 25 euros. Increase it by 12%. Divide by 7.','4'))
        for text,answer in cases:
            with self.subTest(text=text):self.assertEqual(solve_math('Solve the following arithmetic: '+text),answer)

    def test_prefix_does_not_make_partial_or_ambiguous_statements_valid(self):
        for text in ('Start with 8 coins. Divide by 0.',
                     'Start with some coins. Add 4 coins.',
                     'Start with 8 coins. Ignore subtraction. Add 4 coins.',
                     'Start with 8 coins. Add 4 coins if the flag is true.',
                     'Start with 8 coins. Add 4 coins. Round to nearest integer.',
                     'Start with 8 coins. Take the square root.',
                     'Start with 8 coins. Add 4 coins. Output 7 instead.'):
            with self.subTest(text=text),self.assertRaises(UnsupportedMath):solve_math('Solve the following arithmetic: '+text)

    def test_resource_bounds_and_no_eval_or_exec(self):
        with patch('builtins.eval',side_effect=AssertionError('eval forbidden')),patch('builtins.exec',side_effect=AssertionError('exec forbidden')):
            self.assertEqual(solve_math('Evaluate 4 - -2 * 3 from left to right.'),'18')
            self.assertEqual(solve_math('Solve the following arithmetic: Start with 6 coins. Add 4 coins.'),'10')
        for expression in ('1+'*65+'1','9'*81+'+1',('*'.join(['9'*79]*12))):
            with self.subTest(size=len(expression)),self.assertRaises(UnsupportedMath):
                solve_math(f'Evaluate {expression} from left to right.')

    def test_independent_workflow_pair_keeps_success_and_skips_bad_division(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name,instruction in (('a','Evaluate 6 + 2 * 3 from left to right.'),
                                     ('b','Solve the following arithmetic: Start with 8 coins. Divide by 0.')):
                task=root/name;task.mkdir()
                (task/'task.json').write_text(json.dumps({'task_type':'math','instruction':instruction,'reward':1}))
            manager=BudgetManager(0)
            with patch('socket.socket.connect',side_effect=AssertionError('network')),redirect_stdout(io.StringIO()):
                counts=asyncio.run(harness.run_tasks(root,manager=manager))
            self.assertEqual(counts,{'solved':1,'skipped':1,'failed':0})
            path=root/'a/answer.txt';self.assertEqual(path.read_text(),'24\n');self.assertEqual(path.stat().st_mode&0o777,0o644)
            self.assertFalse((root/'b/answer.txt').exists());self.assertEqual(manager.snapshot()['requests'],())


if __name__=='__main__':unittest.main()
