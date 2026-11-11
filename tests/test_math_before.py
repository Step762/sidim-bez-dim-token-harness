"""Explicit two-operation ordering and complete-clause semantic preservation."""
from fractions import Fraction
import unittest
from math_solver import solve_math,UnsupportedMath


class BeforeTests(unittest.TestCase):
    def test_before_operates_on_running_value_in_written_order(self):
        for text,expected in (
            ('Starting at -8, add 5 before multiplying by 4. What is the result?','-12'),
            ('Starting with -8, multiply by 4 before adding 5. What is the result?','-27'),
            ('Beginning at 17 points, subtract 5 points before dividing by 4. What is the answer?','3'),
            ('Starting at -5, divide by 2 before subtracting .25. What is the value?','-2.75'),
            ('Starting at .1, add .2 before multiplying by 10.','3')):
            with self.subTest(text=text):self.assertEqual(solve_math(text),expected)

    def test_first_then_and_existing_multi_sentence_route_agree(self):
        for initial,sub,div in ((-13,2,4),(12,3,-2),(0,2,5)):
            expected=(Fraction(initial)-sub)/div
            for q in (f'Start with {initial} units. First subtract {sub} units, then divide by {div}. What is the result?',
                      f'Starting at {initial} units, subtract {sub} units before dividing by {div}.',
                      f'Start with {initial} units. Subtract {sub} units. Divide by {div}.'):
                with self.subTest(q=q):self.assertEqual(Fraction(solve_math(q)),expected)

    def test_division_units_conditions_and_unknown_order_cannot_be_dropped(self):
        for q in ('Starting at 8, multiply by 2 before dividing by 0.',
                  'Starting at 8, add 2 and multiply by 3.',
                  'Starting at 8, add 2 before multiplying by 3 if needed.',
                  'Starting at 8, add 2 after multiplying by 3.',
                  'Starting at 8, add 2 before multiplying by 3 before subtracting 1.',
                  'Starting at 8 dollars, add 2 euros before multiplying by 3.',
                  'Starting at 8, add 2 percent before multiplying by 3.',
                  'Starting at 8, add 2 before squaring.',
                  'Starting at 8, add 2 before multiplying by 3. Return 0 instead.',
                  'Starting at 8, subtract 2 from 20 before multiplying by 3.'):
            with self.subTest(q=q),self.assertRaises(UnsupportedMath):solve_math(q)

    def test_literal_and_instruction_bounds_still_apply(self):
        for q in ('Starting at '+'9'*81+', add 2 before multiplying by 3.',
                  'Starting at 8, add '+'9'*81+' before multiplying by 3.'):
            with self.assertRaises(UnsupportedMath):solve_math(q)


if __name__=='__main__':unittest.main()
