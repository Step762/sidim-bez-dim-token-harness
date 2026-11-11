"""Independent oracle certificates and bounded acceptance gates, not test-count padding."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from diagnostics.metamorphic import audit, certify_text, factory, reference, run


class FactoryTests(unittest.TestCase):
    def test_bounded_commutativity_keeps_units_and_source_without_numeric_guessing(self):
        from math_ir import Expr, execute
        from math_ir_parser import compile_grammar, verify
        for text in ('Compute (8 + 3) * 5','Compute (1 meter + 2 meters) * 3',
                     'Compute (4 euros + 7 euros) times 2'):
            good=compile_grammar(text)
            swapped=replace(good,root=Expr('mul',tuple(reversed(good.root.args))))
            with patch('math_ir_parser.execute',side_effect=AssertionError('no answer-based acceptance')):
                self.assertEqual(verify(text,swapped),swapped)
            self.assertEqual(execute(good),execute(swapped))

    def test_noncommutative_reorders_and_value_only_equivalence_stay_rejected(self):
        from fractions import Fraction
        from math_ir import Expr,Literal,MathIRRefusal
        from math_ir_parser import compile_grammar,verify
        for text in ('Compute 8 - 3','Compute ratio(8, 3)','Compute 25 percent of (80)',
                     'Compute power(2, 3)','Compute increase (80) by 25 percent'):
            good=compile_grammar(text)
            with self.assertRaises(MathIRRefusal):verify(text,replace(good,root=Expr(good.root.op,tuple(reversed(good.root.args)),good.root.option)))
        text='Compute 8 + 3';good=compile_grammar(text)
        same_number=Expr('sub',(Literal(Fraction(8)),Literal(Fraction(-3))))
        with self.assertRaises(MathIRRefusal):verify(text,replace(good,root=same_number))
        text='Round (1.2 + 2.3) to 1 decimal place using round-half-up';good=compile_grammar(text)
        add,places=good.root.args
        relocated=Expr('add',(Expr('round',(add.args[0],places),'half_up'),add.args[1]))
        with self.assertRaises(MathIRRefusal):verify(text,replace(good,root=relocated))

    def test_fixed_seeds_produce_audited_nonidentity_transformations_without_sut(self):
        with patch('math_ir_parser.compile_grammar', side_effect=AssertionError('oracle must not use compiler')), \
             patch('relation_graph.compile_query', side_effect=AssertionError('oracle must not use graph parser')), \
             patch('data_solver.solve_data', side_effect=AssertionError('oracle must not use solver')):
            first=factory()
            self.assertEqual(first,factory())
            self.assertEqual(len({(p.base.category,p.base.family) for p in first}),34)
            self.assertEqual(len(first),170)
            for seed in (17,93):
                for pair in factory(seed=seed,draws=1):audit(pair)

    def test_oracles_have_independent_manual_anchors(self):
        math=dict(kind='math',mode='percent',values=[-50,25,3],text='Compute 25 percent of (-50)')
        self.assertEqual(reference(math),'-25/2')
        world=dict(kind='search',people=[dict(id='p',label='Ava',fields={'mentor':['m'],'employer':['c']}),
             dict(id='m',label='Bex',fields={'born':['1973']}),dict(id='c',label='Co',fields={'founded':['2010']})],
             token='Ava',path=[['mentor','forward']],projection='born',question="When was Ava's mentor born?")
        self.assertEqual(reference(world),'1973')
        world.update(token='Co',path=[['employer','inverse'],['mentor','forward']],question='When was the mentor of the employee of Co born?')
        certify_text(world);self.assertEqual(reference(world),'1973')
        data=dict(kind='data',records=[['x',2,-4],['x',3,8],['y',2,999]],sku='x',lo=2,hi=3)
        for operation,expected in (('sum','4'),('count','2'),('min','-4'),('max','8'),('avg','2')):
            data['operation']=operation;self.assertEqual(reference(data),expected)

    def test_changed_condition_cannot_keep_a_stale_model_or_oracle(self):
        pair=next(p for p in factory(draws=1) if p.base.category=='math' and p.base.family=='substitution')
        with self.assertRaises(AssertionError):audit(replace(pair,changed=replace(pair.changed,oracle='999')))
        spec=json.loads(pair.changed.spec_json)
        spec['text']=spec['text'].replace('mean','sum')
        # Even if arbitrary text is stored consistently in both places, the
        # independent fixture binder must reject the wrong operation/model.
        corrupted=replace(pair.changed,spec_json=json.dumps(spec),instruction=spec['text'])
        with self.assertRaises(AssertionError):audit(replace(pair,changed=corrupted))
        graph=next(p for p in factory(draws=1) if p.base.category=='search')
        spec=json.loads(graph.changed.spec_json);spec['question']=spec['question'].replace('born','founded')
        with self.assertRaises(AssertionError):audit(replace(graph,changed=replace(graph.changed,spec_json=json.dumps(spec),instruction=spec['question'])))

    def test_csv_corruption_and_false_metamorphic_law_are_detected(self):
        pair=next(p for p in factory(draws=1) if p.base.category=='data')
        changed=replace(pair.changed,files=((pair.changed.files[0][0],pair.changed.files[0][1]+'x,0,999\n'),))
        with self.assertRaises(AssertionError):audit(replace(pair,changed=changed))
        shifted=next(p for p in factory(draws=1) if p.base.category=='math' and p.base.family=='substitution')
        with self.assertRaises(AssertionError):audit(replace(shifted,delta='0'))
        self.assertNotEqual(shifted.base.oracle,shifted.changed.oracle)


class AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.report=run()

    def test_accuracy_and_positive_coverage_prevent_refuse_everything(self):
        r=self.report
        self.assertEqual(len(r['rows']),540)
        self.assertEqual(r['distinct_task_inputs'],300)
        for row in r['rows']:
            with self.subTest(id=row['id'],engine=row['engine'],variant=row['variant']):
                self.assertFalse(row['wrong']);self.assertFalse(row['false_accept']);self.assertIsNone(row['error'])
                self.assertFalse(row['executor_wrong']);self.assertFalse(row['verifier_error'])
                if row['in_contract'] and row['expected_accept']:self.assertTrue(row['correct'])
        for engine,metrics in r['by_engine'].items():
            self.assertGreater(metrics['correct'],0,engine)
            self.assertEqual(metrics['precision'],1,engine)
            self.assertEqual(metrics['in_contract_false_refusal'],0,engine)
        for variant,metrics in r['compiler_by_variant'].items():
            self.assertEqual((metrics['accepted'],metrics['false_accept']),(55,0),variant)

    def test_coverage_frontiers_remain_visible_not_counted_as_success(self):
        refusals=[row for row in self.report['rows'] if row['false_refusal']]
        self.assertEqual(len(refusals),10)
        self.assertEqual({(r['engine'],r['family']) for r in refusals},
                         {('portfolio','irrelevant_conflict'),('math','paraphrase')})
        self.assertEqual(self.report['consistency'],{'passed':260,'evaluated':270})
        for metrics in self.report['compiler_by_variant'].values():
            self.assertEqual(metrics['false_refusal'],0)
            self.assertEqual(metrics['valid_proposal_coverage'],1)
        for row in self.report['rows']:
            if row['engine']=='compiler' and row['role']=='changed' and row['family'] in ('operator','omission','order','source_number','identity','invented_fact'):
                self.assertTrue(row['schema_valid']);self.assertFalse(row['semantic_valid'])
                self.assertFalse(row['executed'])

    def test_independent_graph_verifier_rejects_tampered_answer_and_provenance(self):
        from diagnostics.metamorphic import _materialize
        from relation_graph import Graph, GraphRefusal, compile_query, execute, load_snapshot, verify_candidate
        case=next(p.base for p in factory(draws=1) if p.base.category=='search')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'task';_materialize(case,path)
            snapshot=load_snapshot(path);graph=Graph(snapshot);candidate=execute(graph,compile_query(case.instruction,graph))
            with patch.object(Graph,'hop',side_effect=AssertionError('scan verifier cannot rely on indexed hops')):
                self.assertEqual(verify_candidate(case.instruction,candidate,snapshot,task_dir=path).answer,case.oracle)
                for invalid in (replace(candidate,answer='9999'),replace(candidate,path=()),replace(candidate,terminal_facts=())):
                    with self.assertRaises(GraphRefusal):verify_candidate(case.instruction,invalid,snapshot,task_dir=path)

    def test_production_defaults_and_compiler_transport_remain_off(self):
        import harness
        from math_solver import solve_math
        from search_solver import solve_search
        from data_solver import solve_data
        self.assertIs(harness.SOLVERS['math'],solve_math)
        self.assertIs(harness.SOLVERS['search'],solve_search)
        self.assertIs(harness.SOLVERS['data_analysis'],solve_data)
        self.assertEqual(self.report['real_requests'],0)

    def test_independent_pair_publishes_through_real_workflow_at_zero_budget(self):
        import asyncio
        from contextlib import redirect_stdout
        import io
        import harness
        import math_ir_parser
        import relation_graph
        from budget_manager import BudgetManager
        from diagnostics.metamorphic import _materialize
        from math_ir_parser import solve_math_ir
        from search_solver import solve_search_graph
        cases=factory(draws=1)
        math=next(p.base for p in cases if p.base.category=='math' and p.base.family=='substitution')
        search=next(p.changed for p in cases if p.base.category=='search' and p.base.family=='inverse')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for i,case in enumerate((math,search)):
                path=root/str(i);_materialize(case,path)
                (path/'task.json').write_text(json.dumps({'task_type':case.category,'instruction':case.instruction,'reward':1}))
            manager=BudgetManager(0)
            with patch.dict(harness.SOLVERS,{'math':solve_math_ir,'search':solve_search_graph}), \
                 patch('relation_graph.verify_candidate',wraps=relation_graph.verify_candidate) as gv, \
                 patch('math_ir_parser.verify',wraps=math_ir_parser.verify) as mv, \
                 patch('socket.socket.connect',side_effect=AssertionError('network forbidden')),redirect_stdout(io.StringIO()):
                counts=asyncio.run(harness.run_tasks(root,budget=0,manager=manager))
            self.assertEqual(counts,{'solved':2,'skipped':0,'failed':0})
            self.assertGreater(gv.call_count,0);self.assertGreater(mv.call_count,0)
            from diagnostics.metamorphic import _equal
            for i,case in enumerate((math,search)):
                output=root/str(i)/'answer.txt'
                self.assertTrue(_equal(output.read_text().strip(),case.oracle))
                self.assertEqual(output.stat().st_mode&0o777,0o644)
            self.assertEqual(manager.snapshot()['requests'],())


if __name__=='__main__':unittest.main()
