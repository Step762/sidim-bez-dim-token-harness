"""New independent local pairs; no frozen task names, model plans or HTTP."""
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


# Answers belong to this independent grader, never to task.json.
CASES = (
    ('math','Evaluate -4 + 11 * 3 from left to right.',None,'21'),
    ('search',"What city is Lark's coach's firm based in?",
     '# Eda\nAlias: Lark\nMentor: Taro\n# Taro\nEmployer: Tide Group\n# Tide Group\nHeadquarters: Cádiz\n','Cádiz'),
    ('search','What is the birth year of Mina?', '# Mina\nMina was born in 1946 and later relocated to Kyoto.\n','1946'),
    ('math','Solve the following arithmetic: Start with -15 points. Add 6 points. Divide by 4.',None,'-2.25'),
    ('search','Who is mentored by Sol?', '# Lyra\nMentor: Sol\n# Sol\nBorn: 1946\n','Lyra'),
    ('search',"What city is Blue's mentor's mentor's workplace located in?",
     '# Eda\nAlias: Blue\nMentor: Taro\n# Taro\nMentor: Mina\n# Mina\nEmployer: Tide Group\n# Tide Group\nHeadquarters: Siena\n','Siena'),
    ('math','Evaluate -3 - -9 / 8 using left-to-right order.',None,'0.75'),
    ('math','Solve the following arithmetic: Begin at 35 units. Decrease by 20 percent. Subtract 4 units.',None,'24'),
)


def run_probe():
    pairs=[]
    for offset in range(0,len(CASES),2):
        cases=CASES[offset:offset+2]
        with tempfile.TemporaryDirectory(prefix='q4-independent-') as temp,patch.dict(os.environ,{},clear=True):
            root=Path(temp)
            for i,(kind,question,document,_) in enumerate(cases):
                task=root/str(i);task.mkdir()
                (task/'task.json').write_text(json.dumps({'task_type':kind,'instruction':question,'reward':i}))
                if document is not None:
                    kb=task/'workspace/kb';kb.mkdir(parents=True);(kb/'arbitrary.md').write_text(document)
            manager=BudgetManager(0)
            with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')),redirect_stdout(io.StringIO()):
                counts=asyncio.run(harness.run_tasks(root,manager=manager))
            correct=wrong=0
            for i,case in enumerate(cases):
                path=root/str(i)/'answer.txt'
                if path.exists():
                    assert path.stat().st_mode&0o777==0o644
                    if path.read_text()==case[-1]+'\n':correct+=1
                    else:wrong+=1
            pairs.append({'pair_id':f'q4_independent_{offset//2+1}','types':[c[0] for c in cases],
                          'correct':correct,'wrong':wrong,'counts':counts,
                          'calls':len(manager.snapshot()['requests']),'spent':str(manager.snapshot()['spent'])})
    return {'mode':'independent_local_controls','forced_local_refusal':False,'real_requests':0,'pairs':pairs,
            '2/2':sum(p['correct']==2 for p in pairs),'correct':sum(p['correct'] for p in pairs),
            'wrong':sum(p['wrong'] for p in pairs)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with args.output.open('x') as stream:json.dump(run_probe(),stream,indent=2)
