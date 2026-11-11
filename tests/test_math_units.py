"""Reject unsupported dimensional conversions in local and model-plan routes."""
from pathlib import Path
import tempfile
import unittest

from llm_fallback import InvalidPlan, prepare
from math_solver import solve_math, UnsupportedMath, local_form
from plan_semantics import math_spec, UnsupportedMeaning


class MathUnitsTests(unittest.TestCase):
    def test_incompatible_units_are_rejected_in_every_step_position(self):
        for text in ('Start with 7 coins. Add 3 dollars.',
                     'Start with 8 apples. Subtract 2 items.',
                     'Subtract 2 litres from 9 meters.',
                     'Add two euros to nine dollars.',
                     'Start with 8 units. Add 4 units. Subtract 2 points.',
                     'Increase 30 euros by 10 percent. Add 2 dollars.'):
            with self.subTest(text=text):
                with self.assertRaises(UnsupportedMeaning): math_spec(text)
                with self.assertRaises(UnsupportedMath): solve_math(text)
                self.assertFalse(local_form(text))

    def test_plural_and_case_are_equivalent_without_conversion(self):
        for singular,plural in (('coin','coins'),('box','boxes'),('unit','units'),('item','items'),
                               ('dollar','dollars'),('euro','euros'),('point','points'),
                               ('apple','apples'),('litre','litres'),('meter','meters')):
            with self.subTest(unit=singular):
                self.assertEqual(solve_math(f'Start with 1 {singular}. Add 6 {plural.upper()}. Divide by 2.'),'3.5')

    def test_scalar_percentage_and_implicit_unit_preserve_valid_arithmetic(self):
        for text,expected in (
            ('Start with 10 dollars. Add 5. Increase by 20 percent.', '18'),
            ('Start with -7 meters. Subtract 2 meters. Divide by 4.', '-2.25'),
            ('What is 25 percent of 12 euros?', '3'),
            ('Add two euros to nine euros.', '11'),
            ('Start with 2.5 litres. Add .25 litres. Multiply by 2.', '5.5')):
            with self.subTest(text=text): self.assertEqual(solve_math(text),expected)

    def test_llm_preparation_cannot_validate_a_plan_that_drops_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            for text in ('Start with 12 dollars. Add 3 euros.', 'Subtract 2 dollars from 9 euros.'):
                with self.subTest(text=text), self.assertRaises(InvalidPlan):
                    prepare('math', text, Path(temporary))


if __name__ == '__main__': unittest.main()
