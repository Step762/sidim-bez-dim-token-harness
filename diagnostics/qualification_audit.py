"""Offline cycle B audit: frozen pairs, all ordered types, local mock failures."""

import argparse
import asyncio
from collections import Counter
from contextlib import redirect_stdout
import io
from itertools import product
import json
from pathlib import Path
import shutil
import tempfile

from benchmark.evaluate import DATASET,compare,evaluate,load_dataset,runtime_fingerprint,save
import harness


def cause(row,mode,budget_state):
    if row['status']=='correct':return 'correct'
    state=row['outcome']
    if state in {'timed_out','time_exhausted'}:return 'time'
    if state=='unsafe' and row['task_type']=='coding':return 'coding_disabled'
    if state=='insufficient_evidence':return 'routing_or_grounding'
    if state in {'unsupported','unsupported_type','invalid_data'}:return 'unsupported_validated_route'
    if state=='budget_blocked':
        if mode=='local':return 'fallback_disabled_local_mode'
        return 'api_unknown_budget_frozen' if budget_state['frozen'] else 'budget'
    if state=='llm_failed':return 'api_or_plan_failure'
    return 'runtime_or_publication'


def ordered_types(references):
    # Supported controls, not another estimate of coverage on difficult tasks.
    ids={'math':['math_d01','math_d02'],'search':['search_d01','search_d02'],
         'data_analysis':['data_analysis_d01','data_analysis_d02'],'coding':['coding_d01','coding_d02']}
    rows=[]
    for first,second in product(ids,repeat=2):
        chosen=[ids[first][0],ids[second][1]]
        with tempfile.TemporaryDirectory(prefix='qualification-types-') as temporary:
            root=Path(temporary)
            for name,key in zip(('a_first','b_second'),chosen):shutil.copytree(DATASET/'tasks'/key,root/name)
            trace={}
            with redirect_stdout(io.StringIO()):
                counts=asyncio.run(harness.run_tasks(root,strategy='original',report=trace))
            grades=[]
            for name,key in zip(('a_first','b_second'),chosen):
                answer=root/name/'answer.txt'
                grades.append(compare(answer.read_text().strip() if answer.exists() else None,references[key]))
            rows.append({'types':[first,second],'tasks':chosen,'correct':grades.count('correct'),
                         'grades':grades,'counts':counts,'coding_trusted':False})
    return rows


def run(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    manifest,references=load_dataset(DATASET)
    pairs=[]
    for mode in ('local','mock-good'):
        for pair in manifest['pairs']:
            report=evaluate(DATASET,mode=mode,budget=0 if mode=='local' else 1000,task_ids=pair['tasks'])
            save(report,output/'pairs'/f"{mode}-{pair['pair_id']}")
            pairs.append({**pair,'mode':mode,'correct':report['summary']['correct'],
                          'rows':[{k:row[k] for k in ('task_id','task_type','status','actual','outcome','llm_calls')}
                                  | {'cause':cause(row,mode,report['budget_state'])} for row in report['rows']],
                          'llm_calls':report['summary']['llm_calls'],'spent':report['summary']['llm_spent']})
            print(mode,pair['pair_id'],report['summary']['correct'],flush=True)
    contingencies=[]
    for label,mode,ids,budget in (
        ('api_unavailable','unavailable',['math_d01','math_d15'],1000),
        ('unknown_usage','mock-no-usage',['math_d01','math_d15'],1000),
        ('api_timeout','mock-timeout',['math_d01','math_d15'],1000),
        ('scarce_budget','mock-good',['math_d15','search_d15'],100)):
        report=evaluate(DATASET,mode=mode,budget=budget,task_ids=ids)
        save(report,output/'faults'/label)
        contingencies.append({'case':label,'correct':report['summary']['correct'],
                              'llm_calls':report['summary']['llm_calls'],'budget_state':report['budget_state']})
    baseline=evaluate(DATASET,mode='local');save(baseline,output/'local-baseline')
    groups={}
    for mode in ('local','mock-good'):
        group=[p for p in pairs if p['mode']==mode]
        counter=Counter(p['correct'] for p in group)
        groups[mode]={'2/2':counter[2],'1/2':counter[1],'0/2':counter[0],
                      'causes':dict(Counter(row['cause'] for p in group for row in p['rows']))}
    controls=ordered_types(references)
    summary={'mode':'offline_cycle_b','runtime_sha256':runtime_fingerprint(),'pairs':pairs,'summary':groups,
             'ordered_type_controls':controls,'contingencies':contingencies,'baseline':baseline['summary'],
             'real_llm_requests':0,'scope':'Mock plans are not measurements of the real model. Type controls use simple supported inputs.'}
    (output/'overview.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    lines=['# Cycle B: квалификационные пары','',
           '| Пара | Типы | Local | Mock-good |','| --- | --- | ---: | ---: |']
    for pair in manifest['pairs']:
        values=[next(p['correct'] for p in pairs if p['pair_id']==pair['pair_id'] and p['mode']==mode) for mode in ('local','mock-good')]
        lines.append(f"| {pair['pair_id']} | {pair['category']} | {values[0]}/2 | {values[1]}/2 |")
    lines+=['',json.dumps(groups,ensure_ascii=False,indent=2),'',summary['scope']]
    (output/'overview.md').write_text('\n'.join(lines)+'\n')
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    run(args.output)
