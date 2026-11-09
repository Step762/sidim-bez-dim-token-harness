"""Independent development pairs: alias-named plans over unambiguous local facts.

Only mock HTTP. These are new synthetic controls, not changes to frozen pairs.
"""
import argparse
import asyncio
from contextlib import redirect_stdout
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


# Explicit independent answers; no solver/reference-file lookup constructs them.
CASES = (
    ('Rowan Hale','Finch','Prairie Lab','Riga'),
    ('Mira Noel','Sable','Tide Works','Lima'),
    ('Ivo Reed','Comet','Harbor Group','Bern'),
    ('Sana Vale','Lark','Orbit Labs','Turin'),
    ('Leon Frost','Maple','Cedar Tools','Bergen'),
    ('Nora Elm','Quartz','Lumen Office','Dakar'),
)


def knowledge(person,alias,company,city):
    return f'# {person}\nAlias: {alias}\nEmployer: {company}\n# {company}\nHeadquarters: {city}\n'


def run_probe():
    pairs=[]
    for index in range(3):
        cases=CASES[index*2:index*2+2]
        with tempfile.TemporaryDirectory(prefix='q1-alias-') as temp,patch.dict(os.environ,{},clear=True):
            root=Path(temp);plans={}
            for offset,(person,alias,company,city) in enumerate(cases):
                task=root/str(offset);(task/'workspace/kb').mkdir(parents=True)
                instruction=f"What city is {alias}'s employer based in?"
                (task/'task.json').write_text(json.dumps({'task_type':'search','instruction':instruction,'reward':offset+1}))
                (task/'workspace/kb/facts.md').write_text(knowledge(person,alias,company,city))
                plans[instruction]={'entity':alias,'relations':['employer'],'field':'headquarters','answer':city}
            calls=[]
            async def reply(request):
                body=json.loads(request.content);calls.append(body)
                instruction=json.loads(body['messages'][-1]['content'])['instruction']
                return httpx.Response(200,json={'model':body['model'],'usage':{'prompt_tokens':80,'completion_tokens':48},
                    'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(plans[instruction])}}]})
            manager=BudgetManager(2000)
            gateway=LLMGateway('http://q1-alias.invalid/v1','synthetic-only',manager=manager,
                               max_tokens=128,transport=httpx.MockTransport(reply))
            report={}
            with redirect_stdout(io.StringIO()):
                counts=asyncio.run(harness.run_tasks(root,manager=manager,llm=gateway,budget=2000,report=report))
            correct=sum((root/str(i)/'answer.txt').is_file() and
                        (root/str(i)/'answer.txt').read_text().strip()==case[-1] for i,case in enumerate(cases))
            emitted=sum((root/str(i)/'answer.txt').is_file() for i in range(2))
            pairs.append({'pair_id':f'new_alias_{index+1}','source':'synthetic','split':'independent_development',
                          'correct':correct,'wrong':emitted-correct,'counts':counts,'calls':len(calls),
                          'spent':str(manager.snapshot()['spent']),'reserved':str(manager.snapshot()['reserved']),
                          'frozen':manager.snapshot()['frozen']})
    return {'source':'synthetic','real_requests':0,'pairs':pairs,'2/2':sum(p['correct']==2 for p in pairs),
            'correct':sum(p['correct'] for p in pairs),'wrong':sum(p['wrong'] for p in pairs)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=run_probe()
    with args.output.open('x') as stream:json.dump(result,stream,indent=2)
    print(json.dumps(result,indent=2))
