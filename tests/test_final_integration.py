"""Mixed-category integration with fresh task data and no task-code execution."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from harness import run_tasks
from test_coding import instruction


class FinalIntegrationTests(unittest.TestCase):
    def test_official_cli_uses_static_coding_and_explicit_disable_skips_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            for mode in ('default', 'disabled'):
                root = Path(temporary) / mode
                task = root / 'repair'
                workspace = task / 'workspace'
                workspace.mkdir(parents=True)
                (task / 'task.json').write_text(json.dumps({
                    'task_type': 'coding', 'reward': 1,
                    'instruction': instruction('engine.py', 'test_engine.py'),
                }))
                (workspace / 'engine.py').write_text('def combine(a, b):\n    return a-b\n')
                (workspace / 'test_engine.py').write_text(
                    'import unittest\nimport engine as lib\n\n'
                    'class Checks(unittest.TestCase):\n'
                    '    def test_values(self):\n'
                    '        self.assertEqual(lib.combine(13, 6), 19)\n'
                    '        self.assertEqual(lib.combine(-5, 8), 3)\n\n'
                    'if __name__ == "__main__":\n    unittest.main()\n')
                command = [sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'run_agent.py'),
                           '--tasks-dir', str(root)]
                if mode == 'disabled':
                    command.append('--disable-static-coding')
                result = subprocess.run(command, env={'PATH': os.defpath, 'BUDGET': '0'},
                                        capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                answer = task / 'answer.txt'
                if mode == 'default':
                    self.assertEqual(answer.read_text(), 'DONE\n')
                    self.assertEqual((workspace / 'engine.py').read_text(), 'def combine(a, b):\n    return a+b\n')
                    self.assertEqual(answer.stat().st_mode & 0o777, 0o644)
                else:
                    self.assertFalse(answer.exists())
                    self.assertEqual((workspace / 'engine.py').read_text(), 'def combine(a, b):\n    return a-b\n')

    def test_static_coding_and_other_routes_share_workflow_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def task(name, kind, text, files):
                directory = root / name
                directory.mkdir()
                (directory / 'task.json').write_text(json.dumps({
                    'task_type': kind, 'instruction': text, 'reward': 1,
                }))
                for name, content in files.items():
                    target = directory / 'workspace' / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
                return directory
            bad = task('00_unsafe', 'coding', instruction('engine.py', 'test_engine.py'), {
                'engine.py': "raise RuntimeError('must not execute')\n",
                'test_engine.py': "raise RuntimeError('must not execute')\n",
            })
            math = task('10_math', 'math', 'Compute 7 + 8.', {})
            search = task('20_search', 'search', "Who is the employer of Kira Moss's mentor?", {
                'kb/first.md': '# Kira Moss\nMentor: Ravi Lake\n',
                'kb/second.md': '# Ravi Lake\nEmployer: River Works\n',
            })
            data = task('30_data', 'data_analysis',
                        'Using readings.csv, what is the average of amount where day >= 2?', {
                            'readings.csv': 'amount,day\n9,1\n24,2\n34,3\n',
                        })
            coding = task('40_coding', 'coding', instruction('engine.py', 'test_engine.py'), {
                'engine.py': 'def combine(a, b):\n    return a-b\n',
                'test_engine.py': 'import unittest\nimport engine as lib\n\n'
                    'class Examples(unittest.TestCase):\n'
                    '    def test_examples(self):\n'
                    '        self.assertEqual(lib.combine(13, 6), 19)\n'
                    '        self.assertEqual(lib.combine(-5, 8), 3)\n\n'
                    'if __name__ == "__main__":\n    unittest.main()\n',
            })
            expected = {math: '15', search: 'River Works', data: '29', coding: 'DONE'}
            unchanged = (coding / 'workspace/test_engine.py').read_bytes()
            with (patch('subprocess.Popen', side_effect=AssertionError('no task execution')),
                  patch('socket.socket.connect', side_effect=AssertionError('no network'))):
                result = asyncio.run(run_tasks(root, allow_static_coding=True, strategy='original'))
            self.assertEqual(result, {'solved': 4, 'skipped': 1, 'failed': 0})
            for directory, answer in expected.items():
                path = directory / 'answer.txt'
                self.assertEqual(path.read_text(), answer + '\n')
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertFalse((bad / 'answer.txt').exists())
            self.assertEqual((coding / 'workspace/engine.py').read_text(), 'def combine(a, b):\n    return a+b\n')
            self.assertEqual((coding / 'workspace/test_engine.py').read_bytes(), unchanged)


if __name__ == '__main__':
    unittest.main()
