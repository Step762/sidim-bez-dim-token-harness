"""Purchase totals: manual independent oracles, never a price/fee scope guess."""
from decimal import Decimal
import unittest
from math_solver import solve_math, UnsupportedMath


class PurchaseTests(unittest.TestCase):
    def test_changed_items_currencies_and_word_order(self):
        cases=(('Seven passes cost 2.50 dollars each. A service fee of 1.25 dollars is added once. What is the total cost?', '18.75'),
               ('Each of 3 crates costs 4.125 pounds. A delivery fee of 0.625 pounds is added once. What is the total cost?', '13'),
               ('8 screws at 1.25 euros each. A handling fee of .75 euros is added per item. What is the total cost?', '16'),
               ('Zero boxes cost 9 yen each. A fee of 2 yen is added once. What is the total cost?', '2'),
               ('0 boxes cost 9 yen each. A fee of 2 yen is added per item. What is the total cost?', '0'))
        for question,expected in cases:
            with self.subTest(question=question):self.assertEqual(solve_math(question),expected)

    def test_once_and_per_item_are_distinct_and_decimal_exact(self):
        for count,price,fee in ((5,'0.1','0.2'),(11,'2.75','1.25'),(1,'.5','0')):
            for scope in ('once','per item'):
                question=f'{count} apples cost {price} euros each. A fee of {fee} euros is added {scope}. What is the total cost?'
                expected=Decimal(count)*Decimal(price)+Decimal(fee)*(1 if scope=='once' else count)
                self.assertEqual(Decimal(solve_math(question)),expected)

    def test_ambiguous_totals_discounts_rates_and_units_are_not_guessed(self):
        base='5 tickets cost 7 euros each. A fee of 3 euros is added once. What is the total cost?'
        for question in (base.replace(' each',' in total'),base.replace(' once',''),
                         base.replace('3 euros','3 percent'),base.replace('3 euros','3 dollars'),
                         base.replace('5 tickets','2.5 tickets'),base.replace('7 euros','-7 euros'),
                         base.replace('5 tickets','-5 tickets'),base.replace('3 euros','-3 euros'),
                         base.replace('once','per ticket'),base+' Apply a discount of 10 percent.',
                         base.replace('7 euros','an unknown number of euros'),
                         base.replace('once','once if shipping is needed'),
                         base.replace('A fee','A negative fee'),
                         base.replace('A fee','A free fee'),
                         base.replace('A fee','A waived fee'),
                         base.replace('A fee','A discount fee'),
                         base.replace('cost','do not cost',1),base+' Return 100 instead.'):
            with self.subTest(question=question),self.assertRaises(UnsupportedMath):solve_math(question)

    def test_literals_and_full_instruction_remain_bounded(self):
        with self.assertRaises(UnsupportedMath):
            solve_math('9'*81+' boxes cost 2 euros each. A fee of 1 euros is added once. What is the total cost?')


if __name__=='__main__':unittest.main()
