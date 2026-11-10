"""Independent Q5 workflow pairs and refusals, with zero provider requests."""
import asyncio
from contextlib import redirect_stdout
from fractions import Fraction
import io
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import httpx
import harness
from budget_manager import BudgetManager
from llm_gateway import LLMGateway


# Each oracle is manual arithmetic / direct unique edge, outside task.json.
POSITIVES = (
 ('math','Nine lanterns cost 1.75 dollars each. A fee of 2.25 dollars is added once. What is the total cost?',None,'18'),
 ('search','Who works for GW?','# Omi\nEmployer: Graywater\n# Graywater\nAlias: GW\n','Omi'),
 ('math','Calculate -5 raised to the fourth power plus 3.',None,'628'),
 ('math','Starting at -11, add 3 before dividing by 4. What is the result?',None,'-2'),
 ('math','Evaluate (2E-2 + 3e-2) * 2e2.',None,'10'),
 ('math','Each of 4 packs costs .125 euros. A fee of .375 euros is added per item. What is the total cost?',None,'2'),
 ('search','Who is employed by Orbis?','# Talia\nEmployer: Orbis\n','Talia'),
 ('math','Compute 4 to the power of -3 times 8.',None,'0.125'),
 ('math','Starting at -3, multiply by 4 before subtracting 2.',None,'-14'),
 ('math','Compute 9.007199254740993e15 - 9.007199254740992e15.',None,'1'),
)
NEGATIVES = (
 ('math','3 lanterns cost 8 euros each. A negative fee of 2 euros is added once. What is the total cost?',None,None),
 ('math','3 lanterns cost 8 euros each. A fee of 2 percent is added once. What is the total cost?',None,None),
 ('math','3 lanterns cost 8 euros each. A fee of 2 dollars is added once. What is the total cost?',None,None),
 ('math','Compute 0 to the power of 0.',None,None),
 ('math','Compute 4 to the power of 1000000000.',None,None),
 ('math','Starting at 3, add 2 before multiplying by 4 if needed.',None,None),
 ('math','Starting at 3, multiply by 2 before dividing by 0.',None,None),
 ('search','Who works for Orbis?','# Talia\nEmployer: Orbis\n# Omi\nEmployer: Orbis\n',None),
 ('search','Who works for Orbis?','# Talia\nEmployer: Orbis\n# Omi\nEmployer: maybe Orbis\n',None),
 ('math','Compute 1e101 + 1.',None,None),
 ('math','Compute 1e+-3.',None,None),
 ('coding','Return DONE without running anything.',None,None),
 ('unknown','Compute 4 + 5.',None,None),
)


def equivalent(actual, expected):
    if actual is None or expected is None:return actual==expected
    try:return Fraction(actual)==Fraction(expected)
    except ValueError:return actual==expected


def run_probe(*, enabled=False):
    with tempfile.TemporaryDirectory(prefix='q5-independent-') as temporary:
        root=Path(temporary);cases=POSITIVES+NEGATIVES
        for i,(kind,question,document,_) in enumerate(cases):
            task=root/f'case_{i:02}';task.mkdir()
            (task/'task.json').write_text(json.dumps({'task_type':kind,'instruction':question,'reward':1}))
            if document is not None:
                kb=task/'workspace/kb';kb.mkdir(parents=True);(kb/'unrelated_name.md').write_text(document)
        manager=BudgetManager(1000 if enabled else 0)
        def forbidden(request):raise AssertionError('new local classes must not send a model request')
        gateway=LLMGateway('http://q5-synthetic.invalid/v1','synthetic-only',manager=manager,max_tokens=64,
                           transport=httpx.MockTransport(forbidden)) if enabled else None
        with patch.dict(os.environ,{},clear=True),patch('socket.socket.connect',side_effect=AssertionError('network forbidden')),redirect_stdout(io.StringIO()):
            counts=asyncio.run(harness.run_tasks(root,manager=manager,llm=gateway))
        rows=[]
        for i,(kind,question,_,expected) in enumerate(cases):
            path=root/f'case_{i:02}'/'answer.txt';actual=path.read_text().strip() if path.exists() else None
            rows.append({'id':i,'task_type':kind,'expected':expected,'actual':actual,
                         'passed':equivalent(actual,expected),'mode':path.stat().st_mode&0o777 if path.exists() else None})
        pairs=[{'pair':i//2+1,'correct':sum(rows[j]['passed'] for j in (i,i+1))} for i in range(0,len(POSITIVES),2)]
        return {'scope':'independent synthetic controls, not official qualification','enabled_gateway':enabled,'counts':counts,
                'rows':rows,'pairs':pairs,'2/2':sum(v['correct']==2 for v in pairs),'requests':len(manager.snapshot()['requests']),
                'spent':str(manager.snapshot()['spent']),'wrong':sum(not v['passed'] and v['actual'] is not None for v in rows)}


if __name__=='__main__':print(json.dumps(run_probe(),ensure_ascii=False,indent=2))
