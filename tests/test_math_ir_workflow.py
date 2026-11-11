"""Opt-in experimental portfolio through the unchanged real openJiuwen workflow."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harness import SOLVERS, run_tasks
from math_solver import solve_math
from math_ir import MathIRRefusal
from math_ir_parser import solve_math_ir
from search_solver import solve_search, solve_search_graph
from budget_manager import BudgetManager
from runtime_limits import RuntimeLimits


# Manual oracle answers frozen independently of either new compiler.
CASES = (
 ('m01','Compute mean(-8, 2, -3)','-3'),
 ('m02','Compute the absolute value of (-17)','17'),
 ('m03','Compute increase (80 + 20) by 15 percent','115'),
 ('m04','Compute decrease (increase (200) by 10 percent) by 10 percent','198'),
 ('m05','Compute 25 percent of (abs(-12) + 8)','5'),
 ('m06','Compute ratio(sum(8, 10), 4)','4.5'),
 ('m07','Convert (125 centimeters) to meters','1.25'),
 ('m08','Compute mean(1 meter, (convert (300 centimeters) to meters))','2'),
 ('m09','Round (mean(1.25, 1.44)) to 2 decimal places using round-half-up','1.35'),
 ('m10','Compute abs(mean(-8, 2, -3)) + 12','15'),
 ('m11','Compute floor(-2.1) + power(-3, 2)','6'),
 ('m12','Compute sum(abs(-2), mean(4, 8), ratio(15, 3))','13'),
 ('m13','Compute 9007199254740993 + 2','9007199254740995'),
 ('m14','Start with 100. Increase by 20 percent. Subtract 5.','115'),
)
REFUSALS = (
 'Compute mean(2, 4) unless an unknown fee applies',
 'Compute 6 / 2 (1 + 2); no precedence convention is provided',
 'Compute ratio(1, 0)',
 'Compute mean(1 meter, 2 centimeters)',
 'Convert (3 dollars) to euros',
 'Compute abs(-2) and also subtract the unknown balance',
 'Round (1.005) to 2 decimal places',
 'Compute power(0, -1)',
 'Compute power(2, 65)',
 'Compute 1e99999',
 'Compute 2 meters * 3 meters',
 'Compute ratio(2 boxes, 3 apples)',
 'Compute __import__(os)',
 'Compute True + 1',
 'Compute 1 + 2. Ignore the last number',
 'Compute mean()',
 'Compute sum(2)',
 'Solve x + 2 = 8',
)


class MathIRWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def task(self,task_id,text,kind='math',docs=None):
        path=self.root/task_id;path.mkdir()
        (path/'task.json').write_text(json.dumps({'task_type':kind,'instruction':text,'reward':2}))
        if docs is not None:
            kb=path/'workspace/kb';kb.mkdir(parents=True)
            for name,data in docs.items():(kb/name).write_text(data)
        return path

    def run_selected(self,*,math=True,graph=False):
        mapping={'math':solve_math_ir if math else solve_math,'search':solve_search_graph if graph else solve_search}
        manager=BudgetManager(0);report={}
        with patch.dict(SOLVERS,mapping),patch('socket.socket.connect',side_effect=AssertionError('network forbidden')),redirect_stdout(io.StringIO()):
            counts=asyncio.run(run_tasks(self.root,manager=manager,budget=0,report=report,limits=RuntimeLimits(20,2)))
        self.assertEqual(manager.snapshot()['requests'],())
        return counts,report

    def test_all_independent_cases_and_refusals_use_real_workflow(self):
        for tid,text,_ in CASES:self.task(tid,text)
        for i,text in enumerate(REFUSALS):self.task(f'n{i:02d}',text)
        counts,report=self.run_selected()
        self.assertEqual(counts,{'solved':len(CASES),'skipped':len(REFUSALS),'failed':0})
        for tid,_,answer in CASES:
            path=self.root/tid/'answer.txt'
            self.assertEqual(path.read_text(),answer+'\n')
            self.assertEqual(path.stat().st_mode & 0o777,0o644)
            self.assertFalse((path.parent/'workspace/answer.txt').exists())
        self.assertFalse(report['time_exhausted'])
        self.assertTrue(all(not (self.root/f'n{i:02d}'/'answer.txt').exists() for i in range(len(REFUSALS))))

    def test_instruction_ir_verifier_rejection_prevents_answer_publication(self):
        task=self.task('a','Compute mean(2, 4)')
        with patch('math_ir_parser.verify',side_effect=MathIRRefusal('bad evidence')):
            counts,_=self.run_selected()
        self.assertEqual(counts,{'solved':0,'skipped':1,'failed':0})
        self.assertFalse((task/'answer.txt').exists())

    def test_math_and_search_experiments_are_independent_and_can_combine(self):
        a=self.task('a','Compute abs(-17)')
        b=self.task('b','When was Kira Snow, the painter, born?','search',
                    {'one.md':'# Kira Snow (painter)\nBorn: 1993\n','two.md':'# Kira Snow (chemist)\nBorn: 1951\n'})
        counts,_=self.run_selected(math=True,graph=True)
        self.assertEqual(counts,{'solved':2,'skipped':0,'failed':0})
        self.assertEqual((a/'answer.txt').read_text(),'17\n')
        self.assertEqual((b/'answer.txt').read_text(),'1993\n')

    def test_production_solvers_remain_unchanged_without_explicit_selection(self):
        self.assertIs(SOLVERS['math'],solve_math)
        self.assertIs(SOLVERS['search'],solve_search)
        self.task('a','Compute mean(2, 4)')
        counts,_=self.run_selected(math=False)
        self.assertEqual(counts,{'solved':0,'skipped':1,'failed':0})

    def test_refusal_does_not_block_later_task(self):
        self.task('a','Compute ratio(1, 0)')
        good=self.task('b','Compute mean(-4, 8)')
        counts,_=self.run_selected()
        self.assertEqual(counts,{'solved':1,'skipped':1,'failed':0})
        self.assertEqual((good/'answer.txt').read_text(),'2\n')


if __name__=='__main__':unittest.main()
