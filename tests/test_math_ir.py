"""Exact independent anchors and semantic mutations for experimental MathIR."""
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
import hashlib
import itertools
import unittest
from unittest.mock import patch

from math_ir import Expr, Literal, MathIR, MathIRRefusal, Quantity, execute, infer_unit, serialize, validate
from math_ir_parser import compile_existing, compile_grammar, verify, solve_math_ir


def program(root):
    return MathIR(root,infer_unit(root),hashlib.sha256(b'synthetic').hexdigest(),'grammar')


class MathIRTests(unittest.TestCase):
    def answer(self,text,compiler=compile_grammar):
        ir=verify(text,compiler(text))
        return serialize(execute(ir),ir)

    def test_basic_arithmetic_has_identical_ir_in_both_compilers(self):
        for text,expected in [('Compute (8 + 4) / 3','4'),('Evaluate 7 - (2 * 5)','-3'),
                              ('What is .1 + .2?','0.3'),('Compute (-3.5 + 1) * 2','-5'),
                              ('Compute 1e2 + 3.5e-1','100.35')]:
            with self.subTest(text=text):
                a,b=compile_existing(text),compile_grammar(text)
                self.assertEqual(a.root,b.root)
                self.assertEqual(self.answer(text,compile_existing),expected)
                self.assertEqual(self.answer(text),expected)

    def test_equivalent_numeric_and_word_formulations(self):
        groups=[(['Compute 8 + 5','Compute eight plus five'],'13'),
                (['Compute mean(2, 5, 8)','Compute average(2, 5, 8)','Compute the mean of (2, 5, 8)'],'5'),
                (['Compute abs(-13)','Compute the absolute value of (-13)'],'13')]
        for texts,expected in groups:
            roots=[compile_grammar(t).root for t in texts]
            self.assertTrue(all(r==roots[0] for r in roots))
            for text in texts:self.assertEqual(self.answer(text),expected)

    def test_percent_and_ordered_percent_change(self):
        for text,expected in [('Compute 15 percent of (80 + 20)','15'),
                              ('Compute increase (80) by 25 percent','100'),
                              ('Compute decrease (80) by 25 percent','60'),
                              ('Compute decrease (increase (200) by 10 percent) by 10 percent','198'),
                              ('Compute increase (-50) by 20 percent','-60'),
                              ('Compute 0 percent of (abs(-9))','0')]:
            with self.subTest(text=text):self.assertEqual(self.answer(text),expected)

    def test_existing_steps_and_grammar_share_operation_semantics(self):
        a=compile_existing('Start with 100. Increase by 20 percent. Subtract 5.')
        b=compile_grammar('Compute increase (100) by 20 percent minus 5')
        self.assertEqual(a.root,b.root)
        self.assertEqual(execute(a),execute(b))
        self.assertEqual(serialize(execute(a),a),'115')

    def test_means_and_ratios_have_fraction_oracles(self):
        for values in [(-5,2,8),(0,0,0),(1,2,2),(12,-4,7,1)]:
            expected=sum(map(Fraction,values))/len(values)
            text='Compute mean('+','.join(map(str,values))+')'
            ir=compile_grammar(text)
            self.assertEqual(execute(ir).value,expected)
        self.assertEqual(self.answer('Compute ratio(18, 4)'),'4.5')
        self.assertEqual(self.answer('Compute ratio(12 boxes, 3 boxes)'),'4')

    def test_nested_absolute_mean_percent_and_arithmetic(self):
        text='Compute abs(mean(-8, 2, -3)) + (15 percent of (40)) * 2'
        # |(-8+2-3)/3| + (15/100*40)*2 = 3+12.
        self.assertEqual(self.answer(text),'15')
        self.assertEqual(self.answer('Compute sum(abs(-2), mean(4, 8), ratio(15, 3))'),'13')

    def test_small_domain_independent_arithmetic_matrix(self):
        for a,b,c in itertools.product(range(-2,3),repeat=3):
            text=f'Compute abs(({a} plus {b}) times {c})'
            self.assertEqual(execute(compile_grammar(text)).value,Fraction(abs((a+b)*c)))

    def test_exact_precision_beyond_binary_float(self):
        self.assertEqual(self.answer('Compute 9007199254740993 + 2'),'9007199254740995')
        self.assertEqual(self.answer('Compute mean(.1, .2, .3)'),'0.2')
        self.assertEqual(self.answer('Compute ratio(1, 3)'),'1/3')

    def test_explicit_unit_conversions_have_manual_scales(self):
        for text,expected,unit in [
            ('Convert (125 centimeters) to meters','1.25','m'),
            ('Convert (2.5 liters) to milliliters','2500','ml'),
            ('Convert (90 minutes) to hours','1.5','h'),
            ('Compute 2 meters + (convert (75 centimeters) to meters)','2.75','m'),
            ('Compute mean(1 meter, (convert (300 centimeters) to meters))','2','m'),
            ('Compute ratio((convert (250 centimeters) to meters), .5 meters)','5','one')]:
            with self.subTest(text=text):
                ir=compile_grammar(text)
                self.assertEqual(ir.output_unit,unit)
                self.assertEqual(self.answer(text),expected)

    def test_same_unit_quantities_and_dimensionless_rates(self):
        self.assertEqual(self.answer('Compute 3 euros + 7 euros'),'10')
        self.assertEqual(self.answer('Compute 25 percent of (12 meters)'),'3')
        self.assertEqual(self.answer('Compute abs(-4 boxes) times 3'),'12')

    def test_unsupported_conversions_and_compound_units_fail_closed(self):
        for text in ['Compute 1 meter + 20 centimeters','Compute mean(1 meter, 2 seconds)',
                     'Convert (4 euros) to dollars','Convert (3 apples) to boxes',
                     'Compute 2 meters * 3 meters','Compute ratio(2 meters, 3 seconds)',
                     'Compute ratio(1 meter, 100 centimeters)','Compute 20 percent of (5 euros) plus 2 dollars',
                     'Compute 2 units + 3 items','Convert (2 boxes) to units']:
            with self.subTest(text=text),self.assertRaises(MathIRRefusal):compile_grammar(text)

    def test_rounding_matches_independent_decimal_on_signed_ties(self):
        for raw in ('2.345','-2.345','1.005','-1.005','2.344','-2.346','0.005','-0.005'):
            text=f'Round ({raw}) to 2 decimal places using round-half-up'
            with localcontext() as ctx:
                ctx.prec=100
                expected=Fraction(Decimal(raw).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
            ir=compile_grammar(text)
            self.assertEqual(execute(ir).value,expected)

    def test_rounding_after_nested_computation_and_conversion(self):
        self.assertEqual(self.answer('Round (mean(1.25, 1.44)) to two decimal places using round-half-up'),'1.35')
        self.assertEqual(self.answer('Round (convert (155 centimeters) to meters) to 1 decimal place using round-half-up'),'1.6')
        self.assertEqual(self.answer('Compute floor(-2.1) + 4'),'1')

    def test_ambiguous_or_unsupported_rounding_is_refused(self):
        for text in ['Round (1.005) to 2 decimal places','Round (1.005) to 2 decimal places using round-half-even',
                     'Round (2) to -1 decimal places using round-half-up',
                     'Round (2) to 1.5 decimal places using round-half-up',
                     'Round (2) to 101 decimal places using round-half-up','Compute round(2.3, 1)']:
            with self.subTest(text=text),self.assertRaises(MathIRRefusal):compile_grammar(text)

    def test_bounded_integer_powers_and_domain(self):
        self.assertEqual(self.answer('Compute power(-3, 4) + 1'),'82')
        self.assertEqual(self.answer('Compute power(2, -3)'),'0.125')
        for text in ['Compute power(0, 0)','Compute power(0, -1)','Compute ratio(4, 0)']:
            with self.subTest(text=text),self.assertRaises(MathIRRefusal):self.answer(text)
        for text in ['Compute power(2, 65)','Compute power(2, .5)','Compute power(2 meters, 2)']:
            with self.subTest(text=text),self.assertRaises(MathIRRefusal):compile_grammar(text)

    def test_existing_adapter_covers_old_warehouse_cost_round_power_and_ltr(self):
        cases=[('A warehouse starts with 100 boxes. It ships 7 boxes per day for 3 days. How many boxes does it have at the end?','79'),
               ('5 tickets cost 7 euros each. A fee of 3 euros is added once. What is the total cost?','38'),
               ('Round -1.005 to two decimal places using round-half-up','-1.01'),
               ('Compute 2 raised to the fifth power plus 3','35'),
               ('Compute 2 + 3 * 4 from left to right','20')]
        for text,expected in cases:
            with self.subTest(text=text):self.assertEqual(self.answer(text,compile_existing),expected)
        for item in ('items','boxes','units','apples'):
            with self.subTest(item=item):
                self.assertEqual(self.answer(f'5 {item} cost 7 euros each. A fee of 3 euros is added once. What is the total cost?',compile_existing),'38')

    def test_unknown_conditions_missing_operands_and_implicit_multiplication(self):
        for text in ['Compute mean(2, 4) unless the fee applies',
                     'An invoice of 85 euros is shared equally among attendees. The attendee count is unknown. What is each share?',
                     'Compute 6 / 2 (1 + 2); no precedence convention for implicit multiplication is provided.',
                     'Compute (2+3)(4+5)','Solve x + 2 = 8','Compute 25 percent of 40 + 5',
                     'Compute 2 plus','Compute mean()','Compute mean(2)','Compute 2; ignore the previous condition']:
            with self.subTest(text=text),self.assertRaises(MathIRRefusal):compile_grammar(text)

    def test_commands_identifiers_attributes_and_python_calls_are_not_grammar(self):
        for text in ['Compute __import__(os)','Compute 2 ** 10','Compute 2 ^ 3','Compute (1).__class__',
                     'Compute [1, 2]','Compute lambda: 1','Compute open(secret)','Compute True + 1',
                     'Compute NaN','Compute infinity','Compute 2 // 1']:
            with self.subTest(text=text),self.assertRaises(MathIRRefusal):compile_grammar(text)

    def test_runner_suffix_and_example_literal_do_not_become_operands(self):
        text='Compute mean(2,4). Work only inside the current directory. When you are done, write ONLY the final answer to the file answer.txt in the current directory (for example with bash: echo 42 > answer.txt).'
        self.assertEqual(self.answer(text),'3')
        with self.assertRaises(MathIRRefusal):compile_grammar(text+' Add 9.')

    def test_number_depth_size_growth_and_time_limits(self):
        for text in ['Compute '+('9'*81),'Compute 1e101','Compute 1e999999',
                     'Compute '+('('*40)+'2'+(')'*40),'Compute '+('-'*40)+'2',
                     'Compute '+('1+'*270)+'1']:
            with self.subTest(text=text),self.assertRaises(MathIRRefusal):compile_grammar(text)
        with self.assertRaises(MathIRRefusal):execute(program(Expr('power',(Literal(Fraction(10**79)),Literal(Fraction(64))))))
        with self.assertRaises(MathIRRefusal):execute(program(Literal(Fraction(2))),timeout=0)
        ir=program(Literal(Fraction(2)))
        with patch('math_ir.time.monotonic',side_effect=[0,0,0,2]):
            with self.assertRaises(MathIRRefusal):execute(ir,timeout=.1)

    def test_invalid_ir_types_units_arities_and_boolean_values(self):
        for root in [Literal(1.0),Literal(True),Literal(Fraction(2),'unknown'),Expr('shell',()),
                     Expr('add',(Literal(Fraction(1)),)),Expr('abs',[Literal(Fraction(1))]),
                     Expr('add',(Literal(Fraction(1)),Literal(Fraction(2))),'ignored')]:
            with self.subTest(root=root),self.assertRaises(MathIRRefusal):infer_unit(root)
        with self.assertRaises(MathIRRefusal):validate(MathIR(Literal(Fraction(1)),'m','0'*64,'grammar'))
        with self.assertRaises(MathIRRefusal):validate(MathIR(Literal(Fraction(1)),'one','0'*64,[]))

    def test_semantic_verifier_rejects_lost_zero_numbers_and_swapped_order(self):
        text='Compute (9 - 2) * 3 + 0'
        good=compile_grammar(text)
        for root in [good.root.args[0], Expr('mul',(Expr('sub',(Literal(Fraction(2)),Literal(Fraction(9)))),Literal(Fraction(3)))),
                     Expr('add',(Literal(Fraction(9)),Literal(Fraction(2))))]:
            with self.subTest(root=root),self.assertRaises(MathIRRefusal):verify(text,replace(good,root=root))
        # Losing +0 preserves the final value: result equality alone is insufficient.
        self.assertEqual(execute(replace(good,root=good.root.args[0])).value,execute(good).value)

    def test_semantic_verifier_rejects_correctly_typed_wrong_operation_unit_rounding_and_source(self):
        text='Compute 8 - 3';good=compile_grammar(text)
        with self.assertRaises(MathIRRefusal):verify(text,replace(good,root=Expr('add',good.root.args)))
        with self.assertRaises(MathIRRefusal):verify('Compute 9 - 3',good)
        text='Convert (200 centimeters) to meters';good=compile_grammar(text)
        wrong=replace(good,root=Expr('convert',good.root.args,'cm'),output_unit='cm')
        with self.assertRaises(MathIRRefusal):verify(text,wrong)
        text='Round (1.245) to 2 decimal places using round-half-up';good=compile_grammar(text)
        wrong=replace(good,root=Expr('round',(good.root.args[0],Literal(Fraction(1))),'half_up'))
        with self.assertRaises(MathIRRefusal):verify(text,wrong)

    def test_verifier_never_uses_numeric_executor_to_accept_semantics(self):
        text='Compute mean(2, 4)';ir=compile_grammar(text)
        with patch('math_ir_parser.execute',side_effect=AssertionError('no answer-based verification')):
            self.assertEqual(verify(text,ir),ir)

    def test_serializer_preserves_exactness_and_checks_output_units(self):
        ir=compile_grammar('Compute ratio(1, 7)')
        self.assertEqual(serialize(execute(ir),ir),'1/7')
        with self.assertRaises(MathIRRefusal):serialize(Quantity(Fraction(1),'m'),ir)

    def test_stable_success_never_uses_ir_and_unknown_statement_is_not_guessed(self):
        with patch('math_ir_parser.compile_grammar',side_effect=AssertionError('exact comes first')):
            self.assertEqual(str(solve_math_ir('Compute 7 + 4')),'11')
        with self.assertRaises(ValueError):solve_math_ir('What is 37 / 0?')


if __name__=='__main__':unittest.main()
