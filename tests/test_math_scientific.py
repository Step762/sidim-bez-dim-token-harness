"""Scientific numeric spellings stay exact, bounded and separate from prose."""
from decimal import Decimal,localcontext
from fractions import Fraction
import unittest
from math_solver import solve_math,UnsupportedMath


class ScientificTests(unittest.TestCase):
    def test_literals_signs_zero_and_binary_float_precision(self):
        for literal in ('1e2','2.5e-3','3E+4','-1.25e2','+0e-99','-.5e-2',
                        '9007199254740993e-1','1e9','1e-100','1e100'):
            with self.subTest(literal=literal):
                self.assertEqual(Fraction(solve_math(literal)),Fraction(Decimal(literal)))

    def test_arithmetic_precedence_and_parentheses_remain_standard(self):
        for expression,expected in (('1e2 + .5','100.5'),('2.5e-3 * 4E+2','1'),
                                    ('-2e1 - -3e0 * 2','-14'),('(1e-2 + 2e-2) / 3','0.01'),
                                    ('1e-100 / 3',str(Fraction(1,3*10**100)))):
            with self.subTest(expression=expression):self.assertEqual(Fraction(solve_math('Evaluate '+expression+'.')),Fraction(expected))

    def test_malformed_or_unbounded_notations_and_text_are_rejected(self):
        for expression in ('1e','e2','1e+-2','1e2.3','1e2e3','1e 2','1 e2',
                           '1e101','1e-101','0e999999999','1e0000001','1e1_0','nan','inf',
                           '0x1p2','1e2(3)','2e3 units','two elephants','1e2; print(3)',
                           '9'*81+'e1','1e2 / 0'):
            with self.subTest(expression=expression),self.assertRaises(UnsupportedMath):solve_math(expression)

    def test_scientific_does_not_expand_other_instruction_grammars(self):
        for text in ('Evaluate 1e2 + 2 from left to right.',
                     'Round 1e-2 to two decimal places using round-half-up.',
                     'Start with 1e2 coins. Add 2 coins.',
                     'Compute 1e2 to the power of 2.',
                     'Compute 1e2. Return 7 instead.'):
            with self.subTest(text=text),self.assertRaises(UnsupportedMath):solve_math(text)


if __name__=='__main__':unittest.main()
