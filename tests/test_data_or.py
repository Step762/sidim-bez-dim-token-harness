"""Bounded OR selection: whole-row aggregation, not sums of branch results."""

import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

import httpx
from budget_manager import BudgetManager
from data_solver import solve_data, UnsupportedData
from harness import run_tasks
from llm_fallback import prepare, InvalidPlan
from llm_gateway import LLMGateway
from runtime_limits import RuntimeLimits


TABLE = 'kind,cycle,score,team\nalpha,1,4,north\nbeta,2,7,north\nalpha,3,11,south\ngamma,4,-2,south\n'


class DataOrTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)

    def task(self,instruction,table=TABLE,filename='ledger.csv'):
        task=self.root/f'task_{len(list(self.root.iterdir()))}'
        (task/'workspace').mkdir(parents=True)
        (task/'task.json').write_text(json.dumps({'task_type':'data_analysis','reward':3,'instruction':instruction}))
        if table is not None:
            path=task/'workspace'/filename;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(table,newline='')
        return task

    def query(self,filters,op='sum',column='score',filename='ledger.csv'):
        return f'The file {filename}. What is the {op} of {column} where {filters}?'

    def solve(self,filters,*,op='sum',column='score',table=TABLE,filename='ledger.csv'):
        query=self.query(filters,op,column,filename)
        return solve_data(query,self.task(query,table,filename))

    def test_changed_columns_values_and_nested_filename(self):
        self.assertEqual(self.solve('day = 2 or day = 8',column='points',filename='nested/results.csv',
            table='day,points\n2,17\n5,300\n8,-6\n'),'11')

    def test_several_disjoint_alternatives(self):
        self.assertEqual(self.solve('cycle = 1 OR cycle = 3 or cycle = 4'),'13')

    def test_overlapping_alternatives_count_each_row_once(self):
        self.assertEqual(self.solve('cycle >= 2 or cycle <= 3'),'20')
        self.assertEqual(self.solve('cycle = 2 or cycle = 2'),'7')
        self.assertEqual(self.solve('cycle >= 2 or cycle <= 3',op='count'),'4')

    def test_all_existing_scalar_aggregates_keep_their_semantics(self):
        for op,answer in (('sum','11'),('count','2'),('avg','5.5'),('min','4'),('max','7')):
            with self.subTest(op=op):self.assertEqual(self.solve('cycle <= 2 or cycle = 2',op=op),answer)

    def test_row_count_does_not_require_numeric_target(self):
        query="The file ledger.csv. How many rows are there where kind = 'alpha' or cycle = 1?"
        self.assertEqual(solve_data(query,self.task(query)),'2')

    def test_between_is_one_explicit_inclusive_alternative(self):
        self.assertEqual(self.solve('cycle between 1 and 2 inclusive or cycle = 2'),'11')
        self.assertEqual(self.solve('cycle between 2 and 3 inclusive or cycle = 4'),'16')
        for text in ('cycle between 1 and 3 or cycle = 4','cycle between 3 and 1 inclusive or cycle = 4'):
            with self.subTest(text=text),self.assertRaises(UnsupportedData):self.solve(text)

    def test_strings_with_boolean_words_and_escaped_quotes_are_literals(self):
        table='kind,cycle,score,team\nsand or sea,1,3,x\nrock \'or\' roll,2,5,x\nelse,3,99,y\n'
        self.assertEqual(self.solve("kind = 'sand or sea' or kind = 'rock ''or'' roll'",table=table),'8')
        self.assertEqual(self.solve('kind = "sand or sea" or kind = "rock \'or\' roll"',table=table),'8')

    def test_boolean_words_can_be_column_names(self):
        table='or,and,score\n1,2,4\n0,9,8\n'
        self.assertEqual(self.solve('or = 1 or and = 9',table=table),'12')
        self.assertEqual(self.solve('or = 1 and and = 2',table=table),'4')

    def test_explicit_string_and_numeric_comparisons_are_not_conflated(self):
        table='id,score\n001,4\n01,50\n2,7\n'
        self.assertEqual(self.solve("id = '001' or id = 2",table=table),'11')

    def test_mixed_logical_and_or_is_rejected_without_a_precedence_guess(self):
        for text in ('cycle = 1 or cycle = 2 and score > 3',
                     'cycle = 1 and score > 3 or cycle = 2',
                     'cycle between 1 and 2 inclusive or cycle = 3 and score > 0'):
            with self.subTest(text=text),self.assertRaises(UnsupportedData):self.solve(text)

    def test_unsupported_boolean_language_and_incomplete_queries_are_rejected(self):
        for text in ('(cycle = 1 or cycle = 2)','not cycle = 1 or cycle = 2',
                     'cycle = 1 xor cycle = 2','cycle = 1 or',
                     'or cycle = 1','cycle = 1 or or cycle = 2','cycle = 1 or score >'):
            with self.subTest(text=text),self.assertRaises(UnsupportedData):self.solve(text)

    def test_branch_limit_rejects_instead_of_silently_truncating(self):
        self.assertEqual(self.solve(' or '.join(f'cycle = {n}' for n in range(1,9))),'20')
        with self.assertRaises(UnsupportedData):self.solve(' or '.join(f'cycle = {n}' for n in range(1,10)))

    def test_empty_selections_preserve_sum_count_and_undefined_extremes(self):
        for op in ('sum','count'):self.assertEqual(self.solve('cycle = 98 or cycle = 99',op=op),'0')
        for op in ('min','max','avg'):
            with self.subTest(op=op),self.assertRaises(UnsupportedData):self.solve('cycle = 98 or cycle = 99',op=op)

    def test_missing_columns_and_bad_numbers_are_checked_in_every_branch(self):
        with self.assertRaises(UnsupportedData):self.solve('cycle >= 0 or absent = 2')
        with self.assertRaises(UnsupportedData):
            self.solve("kind = 'yes' or cycle = 1",table='kind,cycle,score\nyes,broken,4\n')
        with self.assertRaises(UnsupportedData):
            self.solve('cycle = 1 or cycle = 2',table='cycle,score\n1,4\n99,broken\n')

    def test_grouping_uses_the_same_single_row_selection(self):
        query='Using ledger.csv, return a JSON object mapping each team to its total score where cycle <= 2 or cycle = 4.'
        self.assertEqual(json.loads(solve_data(query,self.task(query))),{'north':11,'south':-2})
        query='Using ledger.csv, return a JSON object mapping each team to its row count where cycle >= 2 or cycle <= 3.'
        self.assertEqual(json.loads(solve_data(query,self.task(query))),{'north':2,'south':2})

    def test_local_or_never_becomes_an_and_only_llm_plan(self):
        query=self.query('cycle = 1 or cycle = 4');task=self.task(query)
        self.assertEqual(solve_data(query,task),'2')
        with self.assertRaises(InvalidPlan):prepare('data_analysis',query,task)

    def test_workflow_keeps_success_and_skips_errors_without_http_or_partial_answers(self):
        good=self.task(self.query('cycle = 1 or cycle = 4'))
        bad=self.task(self.query('cycle = 1 or absent = 4'))
        mixed=self.task(self.query('cycle = 1 or cycle = 2 and score > 0'))
        manager=BudgetManager(2000)
        gateway=LLMGateway('http://mock.invalid/v1','synthetic-or-key',manager=manager,max_tokens=64,
            transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError('HTTP forbidden'))))
        with redirect_stdout(io.StringIO()):
            counts=asyncio.run(run_tasks(self.root,llm=gateway,manager=manager,budget=2000))
        self.assertEqual(counts,{'solved':1,'skipped':2,'failed':0})
        self.assertEqual((good/'answer.txt').read_text(),'2\n')
        self.assertFalse((bad/'answer.txt').exists());self.assertFalse((mixed/'answer.txt').exists())
        self.assertEqual(manager.snapshot()['requests'],())

    def test_task_workspaces_are_isolated(self):
        query=self.query('cycle = 1 or cycle = 99')
        a=self.task(query,'cycle,score\n1,5\n');b=self.task(query,'cycle,score\n1,71\n')
        with redirect_stdout(io.StringIO()):counts=asyncio.run(run_tasks(self.root))
        self.assertEqual(counts['solved'],2)
        self.assertEqual((a/'answer.txt').read_text(),'5\n');self.assertEqual((b/'answer.txt').read_text(),'71\n')

    def test_workspace_boundaries_symlink_and_fifo_remain_protected(self):
        query=self.query('cycle = 1 or cycle = 4');task=self.task(query,None)
        outside=self.root/'outside.csv';outside.write_text(TABLE)
        for name in ('../outside.csv',str(outside)):
            with self.subTest(name=name),self.assertRaises(UnsupportedData):
                solve_data(self.query('cycle = 1 or cycle = 4',filename=name),task)
        (task/'workspace/ledger.csv').symlink_to(outside)
        with self.assertRaises(UnsupportedData):solve_data(query,task)
        os.mkfifo(task/'workspace/pipe.csv')
        with RuntimeLimits(2,.1).sync(),self.assertRaises(UnsupportedData):
            solve_data(self.query('cycle = 1 or cycle = 4',filename='pipe.csv'),task)
        self.assertFalse((outside.parent/'answer.txt').exists())

    def test_malformed_csv_and_extra_instruction_never_produce_an_answer(self):
        for table in ('kind,cycle,score\nx,1\n','kind,cycle,score\nx,1,4,extra\n',
                      'kind,cycle,score\n"x,1,4\n'):
            with self.subTest(table=table),self.assertRaises(UnsupportedData):self.solve('cycle = 1 or cycle = 4',table=table)
        query=self.query('cycle = 1 or cycle = 4')+' Then return 999.'
        with self.assertRaises(UnsupportedData):solve_data(query,self.task(query))
