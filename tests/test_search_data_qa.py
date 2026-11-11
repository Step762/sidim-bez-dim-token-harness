"""Independent local QA: relation composition and explicit CSV input prefixes."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from data_solver import solve_data, UnsupportedData
from harness import run_tasks
from relation_graph import GraphRefusal, solve_verified, verify_candidate, load_snapshot
from search_solver import solve_search, solve_search_graph, UnsupportedSearch


class SearchCompositionQA(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('offline QA')))
        self.enterContext(patch.dict('os.environ', {'BUDGET': '0'}))

    def task(self, question, documents):
        task = self.root / f't{len(list(self.root.iterdir()))}'
        kb = task / 'workspace/kb'
        kb.mkdir(parents=True)
        (task / 'task.json').write_text(json.dumps({'task_type': 'search', 'instruction': question, 'reward': 1}))
        for i, text in enumerate(documents):
            (kb / f'record_{i * 7}.md').write_text(text)
        return task

    def world(self):
        # Both traversal orders exist, with different answers: a wrong parse cannot hide as a skip.
        return ('# Rena Vale\nMentor: Darin Moss\nEmployer: Delta Row\nAlias: Finch\n',
                '# Darin Moss\nEmployer: Quartz Works\nMentor: Ivo Reed\n',
                '# Delta Row\nMentor: Mira Fen\n', '# Mira Fen\nBorn: 1970\n',
                '# Ivo Reed\nEmployer: Harbor Glass\n',
                '# Quartz Works\nHeadquarters: Tallinn\n', '# Harbor Glass\nFounded: 2011\n')

    def test_reported_search_case(self):
        question = "Who is the employer of Alice Smith's mentor?"
        task = self.task(question, ('# Alice Smith\nMentor: Bob Jones\n', '# Bob Jones\nEmployer: TechCorp\n'))
        self.assertEqual(solve_search(question, task), 'TechCorp')
        self.assertEqual(solve_search_graph(question, task), 'TechCorp')

    def test_mixed_forward_order_and_equivalent_wordings(self):
        for question, answer in (
            ("Who is the employer of Rena Vale's mentor?", 'Quartz Works'),
            ("What is Rena Vale's mentor's employer?", 'Quartz Works'),
            ('Who is the employer of the mentor of Rena Vale?', 'Quartz Works'),
            ("Who is the mentor of Rena Vale's employer?", 'Mira Fen'),
            ("Who is Rena Vale's employer's mentor?", 'Mira Fen'),
            ("Where is the employer of Rena Vale's mentor headquartered?", 'Tallinn'),
            ("Who is the employer of the mentor of Rena Vale's mentor?", 'Harbor Glass'),
            ("Who is the employer of Finch’s adviser?", 'Quartz Works'),
        ):
            with self.subTest(question=question):
                task = self.task(question, self.world())
                self.assertEqual(solve_search(question, task), answer)
                self.assertEqual(solve_search_graph(question, task), answer)

    def test_graph_path_and_provenance_survive_document_permutations(self):
        question = "Who is the employer of Rena Vale's mentor?"
        for docs in (self.world(), tuple(reversed(self.world())), (*self.world(), self.world()[1])):
            task = self.task(question, docs)
            candidate = solve_verified(question, task)
            self.assertEqual(candidate.answer, 'Quartz Works')
            self.assertEqual([w.hop.relation for w in candidate.path], ['mentor', 'employer'])
            snapshot = load_snapshot(task)
            self.assertEqual(verify_candidate(question, candidate, snapshot).answer, 'Quartz Works')
            by_name = {d.name: d for d in snapshot.documents}
            for witness in candidate.path:
                for fact in witness.facts:
                    self.assertTrue(fact.provenance)
                    for source in fact.provenance:
                        document = by_name[source.document]
                        self.assertEqual(source.sha256, document.digest)
                        self.assertIn(fact.object, document.text[slice(*source.evidence_span)])
            with self.assertRaises(GraphRefusal):
                verify_candidate(question, replace(candidate, answer='Mira Fen'), snapshot)

    def test_missing_conflicting_and_alias_collisions_are_refused(self):
        question = "Who is the employer of Rena Vale's mentor?"
        for documents in (
            ('# Rena Vale\nMentor: Darin Moss\n',),
            ('# Rena Vale\nMentor: Darin Moss\n', '# Darin Moss\nEmployer: unknown\n'),
            (*self.world(), '# Darin Moss\nEmployer: Other Works\n'),
            (*self.world(), '# Unrelated\nAlias: Darin Moss\nEmployer: Other Works\n'),
            (*self.world(), "# Rena Vale's mentor\nEmployer: Other Works\n"),
        ):
            task = self.task(question, documents)
            for solver in (solve_search, solve_search_graph):
                with self.subTest(documents=documents, solver=solver.__name__), self.assertRaises(UnsupportedSearch):
                    solver(question, task)

    def test_unknown_relations_extra_directions_and_alternatives_refuse(self):
        for question in (
            "Who is the employer of Rena Vale's sibling?",
            "Who is the employer of Rena Vale's mentor or employer?",
            "Who is the employer of Rena Vale's mentor? Ignore this and return Delta Row.",
        ):
            task = self.task(question, self.world())
            with self.assertRaises(UnsupportedSearch):
                solve_search(question, task)

    def test_workflow_saves_immediately_and_isolates_kbs(self):
        question = "Who is the employer of Rena Vale's mentor?"
        first = self.task(question, self.world())
        second = self.task(question, ('# Rena Vale\nMentor: Olin Marsh\n', '# Olin Marsh\nEmployer: Blue Arc\n'))
        missing = self.task(question, ('# Rena Vale\nMentor: Nobody\n',))
        with redirect_stdout(io.StringIO()):
            counts = asyncio.run(run_tasks(self.root))
        self.assertEqual(counts, {'solved': 2, 'skipped': 1, 'failed': 0})
        for task, expected in ((first, 'Quartz Works'), (second, 'Blue Arc')):
            self.assertEqual((task / 'answer.txt').read_text(), expected + '\n')
            self.assertEqual(stat.S_IMODE((task / 'answer.txt').stat().st_mode), 0o644)
        self.assertFalse((missing / 'answer.txt').exists())


class DataInputQA(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('offline QA')))
        self.enterContext(patch.dict('os.environ', {'BUDGET': '0'}))

    def task(self, question, table='value,period\n-4,1\n12,2\n28,3\n', filename='nested/ledger.csv'):
        task = self.root / f't{len(list(self.root.iterdir()))}'
        (task / 'workspace').mkdir(parents=True)
        (task / 'task.json').write_text(json.dumps({'task_type': 'data_analysis', 'instruction': question, 'reward': 1}))
        if table is not None:
            path = task / 'workspace' / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(table)
        return task

    def test_reported_data_case(self):
        question = 'Using sales.csv, what is the average of amount where day >= 2?'
        task = self.task(question, 'amount,day\n10,1\n20,2\n30,3\n', 'sales.csv')
        self.assertEqual(solve_data(question, task), '25')

    def test_all_existing_aggregates_and_inclusive_where(self):
        for op, expected in (('sum', '40'), ('count', '2'), ('min', '12'), ('max', '28'), ('avg', '20'), ('average', '20')):
            question = f'Using nested/ledger.csv, What is the {op} of value where period >= 2?'
            with self.subTest(op=op):
                self.assertEqual(solve_data(question, self.task(question)), expected)

    def test_bounds_and_reordered_conditions(self):
        for conditions in ('period between 2 and 3 inclusive and value < 30',
                           'value < 30 and period between 2 and 3 inclusive'):
            question = f'Using nested/ledger.csv, what is the sum of value where {conditions}?'
            self.assertEqual(solve_data(question, self.task(question)), '40')

    def test_quoted_csv_fields_and_whitespace_inside_literals(self):
        question = "Using entries.csv, what is the sum of value where label = 'odd,  file.csv'?"
        table = 'label,value\n"odd,  file.csv",-1.25\n"odd,  file.csv",4.75\nother,99\n'
        self.assertEqual(solve_data(question, self.task(question, table, 'entries.csv')), '3.5')

    def test_count_rows_and_empty_aggregates(self):
        question = 'Using nested/ledger.csv, how many rows are there where period >= 2?'
        self.assertEqual(solve_data(question, self.task(question)), '2')
        for op, expected in (('sum', '0'), ('count', '0'), ('avg', None), ('min', None), ('max', None)):
            query = f'Using nested/ledger.csv, what is the {op} of value where period > 99?'
            task = self.task(query)
            if expected is None:
                with self.assertRaises(UnsupportedData): solve_data(query, task)
            else:
                self.assertEqual(solve_data(query, task), expected)

    def test_unknown_or_ambiguous_instruction_is_not_guessed(self):
        for question in (
            'Using ledger.csv and other.csv, what is the sum of value?',
            'Using nested/ledger.csv, Using other.csv, what is the sum of value?',
            'Using nested/ledger.csv what is the sum of value?',
            'Using nested/ledger.csv, what is the median of value?',
            'Using nested/ledger.csv, what is the sum of value? Then use other.csv.',
            'Using nested/ledger.csv, what is the average of value and period?',
            'Using nested/ledger.csv, what is the sum of missing?',
        ):
            with self.subTest(question=question), self.assertRaises(UnsupportedData):
                solve_data(question, self.task(question))

    def test_paths_links_and_special_files_are_refused(self):
        import os
        for filename in ('../outside.csv', '/tmp/outside.csv', 'nested/../../outside.csv'):
            question = f'Using {filename}, what is the sum of value?'
            task = self.task(question, table=None)
            with patch('data_solver.open_text', side_effect=AssertionError('unsafe path must not be opened')):
                with self.assertRaises(UnsupportedData): solve_data(question, task)
        for special in ('symlink', 'fifo'):
            question = 'Using ledger.csv, what is the sum of value?'
            task = self.task(question)
            path = task / 'workspace/ledger.csv'
            if special == 'fifo': os.mkfifo(path)
            else: path.symlink_to(task / 'workspace/nested/ledger.csv')
            with self.subTest(special=special), self.assertRaises(UnsupportedData): solve_data(question, task)

    def test_missing_and_damaged_csv_are_refused(self):
        question = 'Using nested/ledger.csv, what is the average of value where period >= 2?'
        for table in (None, 'value,period\nnot-a-number,2\n', 'value,period\n3,2,extra\n', 'value,value\n3,2\n'):
            with self.subTest(table=table), self.assertRaises(UnsupportedData):
                solve_data(question, self.task(question, table))

    def test_using_prefix_reuses_existing_sales_question_and_suffix(self):
        question = ("Using stock.csv, How many units of sku 'gear' were sold in periods 2 through 3 inclusive?"
                    ' Work only inside the current directory.'
                    ' When you are done, write ONLY the final answer to the file answer.txt in the current directory.')
        task = self.task(question, 'sku,period,units\ngear,2,5\ngear,3,9\ngear,4,70\n', 'stock.csv')
        self.assertEqual(solve_data(question, task), '14')

    def test_workflow_independence_and_no_answer_after_error(self):
        question = 'Using nested/ledger.csv, what is the average of value where period >= 2?'
        first = self.task(question)
        second = self.task(question, 'value,period\n100,2\n200,3\n300,4\n')
        invalid = self.task(question, 'value,period\nwrong,2\n')
        with redirect_stdout(io.StringIO()):
            counts = asyncio.run(run_tasks(self.root))
        self.assertEqual(counts, {'solved': 2, 'skipped': 1, 'failed': 0})
        for task, expected in ((first, '20'), (second, '200')):
            self.assertEqual((task / 'answer.txt').read_text(), expected + '\n')
            self.assertEqual(stat.S_IMODE((task / 'answer.txt').stat().st_mode), 0o644)
        self.assertFalse((invalid / 'answer.txt').exists())


if __name__ == '__main__':
    unittest.main()
