"""New qualification controls remain free and never dispatch unsafe Coding."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from diagnostics.q4_qualification_probe import run_probe
from diagnostics.selection import experiment_plan
import harness


class Q4QualificationTests(unittest.TestCase):
    def test_four_new_independent_pairs_are_local_at_zero_budget(self):
        report=run_probe()
        self.assertEqual((report['2/2'],report['correct'],report['wrong']),(4,8,0))
        self.assertFalse(report['forced_local_refusal'])
        self.assertTrue(all(p['calls']==0 and p['spent']=='0.0' for p in report['pairs']))

    def test_coding_and_unknown_type_do_not_stop_new_free_routes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name,kind,text in (('a','coding','Repair the program.'),('b','unknown','Return anything.'),
                                   ('c','math','Evaluate 6 + 3 * 2 from left to right.')):
                task=root/name;task.mkdir()
                (task/'task.json').write_text(json.dumps({'task_type':kind,'instruction':text,'reward':1}))
            report={}
            with patch.dict(harness.SOLVERS,{'coding':lambda *a,**k: (_ for _ in ()).throw(AssertionError('Coding dispatched'))}),patch('socket.socket.connect',side_effect=AssertionError('network')),redirect_stdout(io.StringIO()):
                counts=asyncio.run(harness.run_tasks(root,report=report))
            self.assertEqual(counts,{'solved':1,'skipped':2,'failed':0})
            self.assertEqual((root/'c/answer.txt').read_text(),'18\n')
            self.assertFalse((root/'a/answer.txt').exists());self.assertFalse((root/'b/answer.txt').exists())
            self.assertEqual(report['outcomes'][str(root/'a')]['outcome'],'unsafe')
            self.assertEqual(report['outcomes'][str(root/'b')]['outcome'],'unsupported_type')

    def test_diagnostic_selection_does_not_pay_for_promoted_forms(self):
        with patch('socket.socket.connect',side_effect=AssertionError('network')):
            plan=experiment_plan()
        self.assertEqual(plan['A2']['planned_reservation'],'0')
        self.assertFalse(any(row['natural_llm_candidate'] for row in plan['selection']))
        for row in plan['selection']:
            if row['task_id'] in ('math_d15','search_d15'):
                self.assertEqual(row['local_status'],'solved')
                self.assertNotEqual(row['local_status'],row['historical_local_status'])


if __name__=='__main__':unittest.main()
