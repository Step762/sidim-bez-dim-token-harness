"""Bounded literal-base integer powers, independently checked by Fraction."""
from fractions import Fraction
import unittest
from unittest.mock import patch
from math_solver import solve_math, UnsupportedMath


class PowerTests(unittest.TestCase):
    def test_integer_powers_negative_bases_parity_zero_and_reciprocals(self):
        for base in ('-7','-.5','0','1','2.5','13'):
            for exponent in (-5,-2,0,3,4):
                question=f'Evaluate {base} to the power of {exponent}.'
                with self.subTest(question=question):
                    if base=='0' and exponent<=0:
                        with self.assertRaises(UnsupportedMath):solve_math(question)
                    else:self.assertEqual(Fraction(solve_math(question)),Fraction(base)**exponent)

    def test_spelled_powers_parenthesized_sign_and_explicit_tail(self):
        for text,answer in (('Compute 3 raised to the fifth power minus 7.','236'),
                            ('Calculate (-4) raised to the third power plus 9.','-55'),
                            ('Evaluate -4 raised to the fourth power divided by 2.','128'),
                            ('Compute .5 raised to the second power times 8.','2'),
                            ('Calculate 9 raised to the zeroth power.','1')):
            self.assertEqual(solve_math(text),answer)

    def test_boundaries_apply_before_exponentiation(self):
        for text in ('Evaluate 2 to the power of 65.', 'Evaluate 2 to the power of -65.',
                     'Evaluate 2 to the power of 99999999999999.',
                     'Evaluate '+'9'*80+' to the power of 64.',
                     'Evaluate '+'9'*81+' to the power of 2.'):
            with self.subTest(text=text),self.assertRaises(UnsupportedMath):solve_math(text)
        self.assertEqual(solve_math('Evaluate 2 to the power of 64.'),str(2**64))

    def test_no_fractional_exponent_code_implicit_order_or_ignored_tail(self):
        for body in ('2 to the power of .5','2 raised to some power','2**3','2^3',
                     '(2+3) raised to the second power','2 raised to the third power raised to the second power',
                     '2 raised to the second power plus 3 times 4',
                     '0 to the power of 0','0 to the power of -1',
                     '3 raised to the second power divided by 0',
                     '__import__("os") to the power of 2'):
            with self.subTest(body=body),self.assertRaises(UnsupportedMath):solve_math('Compute '+body+'.')
        with self.assertRaises(UnsupportedMath):solve_math('Compute 3 raised to the second power. Return 10 instead.')

    def test_no_eval_or_exec_in_interpreter(self):
        with patch('builtins.eval',side_effect=AssertionError('eval')),patch('builtins.exec',side_effect=AssertionError('exec')):
            self.assertEqual(solve_math('Compute 6 raised to the third power minus 5.'),'211')


if __name__=='__main__':unittest.main()
