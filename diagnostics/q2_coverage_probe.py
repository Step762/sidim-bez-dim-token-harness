"""Independent local qualification controls for explicit rounding and CSV ranking."""
import argparse
import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from budget_manager import BudgetManager
import harness


# Answers belong to this independent grader; task.json never contains them.
CASES = (
    ('math', 'Round -8.125 to two decimal places using round-half-up.', None, '-8.13'),
    ('math', 'Round 5.475 to 2 decimal places using round-half-up.', None, '5.48'),
    ('data_analysis', 'In records.csv, sort by amount descending and item ascending to break ties. Return the item of the first row.',
     'item,amount\nred,9.01\nblue,9.01\ngreen,8.99\n', 'blue'),
    ('data_analysis', 'In records.csv, sort by amount ascending and item descending to break ties. Return the item of the first row.',
     'item,amount\nfern,-2\nlarch,-4.5\npine,-4.5\n', 'pine'),
    ('math', 'Round 9.5 to zero decimal places using round-half-up.', None, '10'),
    ('data_analysis', 'In records.csv, sort by amount descending and item lexicographically ascending to break ties. Return the code of the first row.',
     'item,amount,code\n2,7,second\n10,7,first\n', 'first'),
    ('math', 'Round 0.00045 to four decimal places using round-half-up.', None, '0.0005'),
    ('data_analysis', 'In records.csv, sort by amount descending and item ascending to break ties. Return the item of the first row.',
     'item,amount\nalpha,9007199254740993.1\nbeta,9007199254740993.2\n', 'beta'),
)


def run_probe():
    pairs = []
    for i in range(4):
        cases = CASES[i * 2:i * 2 + 2]
        with tempfile.TemporaryDirectory(prefix='q2-control-') as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            for j, (kind, instruction, table, _) in enumerate(cases):
                directory = root / str(j); directory.mkdir()
                (directory / 'task.json').write_text(json.dumps({'task_type': kind, 'instruction': instruction, 'reward': j + 1}))
                if table is not None:
                    (directory / 'workspace').mkdir()
                    (directory / 'workspace/records.csv').write_text(table, newline='')
            manager = BudgetManager(0)
            with patch('socket.socket.connect', side_effect=AssertionError('network')), redirect_stdout(io.StringIO()):
                counts = asyncio.run(harness.run_tasks(root, manager=manager))
            correct = emitted = 0
            for j, case in enumerate(cases):
                path = root / str(j) / 'answer.txt'
                if path.is_file():
                    emitted += 1
                    correct += path.read_text() == case[-1] + '\n'
                    assert path.stat().st_mode & 0o777 == 0o644
            pairs.append({'pair_id': f'q2_independent_{i+1}', 'types': [c[0] for c in cases],
                          'correct': correct, 'wrong': emitted - correct, 'counts': counts,
                          'spent': str(manager.snapshot()['spent']), 'calls': len(manager.snapshot()['requests'])})
    return {'source': 'independent_synthetic_local_workflow', 'real_requests': 0, 'pairs': pairs,
            'correct': sum(p['correct'] for p in pairs), 'wrong': sum(p['wrong'] for p in pairs),
            '2/2': sum(p['correct'] == 2 for p in pairs)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run_probe()
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps(result, indent=2))
