"""Development-only acceptance helpers, never imported by the production agent.

The physical test view has no mixed corpus, original oracles, secrets or LIVE state.
Only reviewed synthetic unit fixtures may execute code; development Coding stays off.
"""
import argparse
import asyncio
from contextlib import ExitStack, redirect_stdout
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
IDS = frozenset(f'{kind}_d{i:02d}' for kind in ('math','search','data_analysis','coding') for i in range(1,17))
TESTS = (
    'test_a1_resume', 'test_batch_feasibility', 'test_benchmark', 'test_budget',
    'test_coding', 'test_coding_security', 'test_data', 'test_data_groups',
    'test_data_or', 'test_data_paraphrase', 'test_data_ranking', 'test_diagnostics',
    'test_fallback_aliases', 'test_fallback_pilot', 'test_files', 'test_harness',
    'test_llm', 'test_math_before', 'test_math_cost', 'test_math_extended',
    'test_math_ir', 'test_math_ir_workflow', 'test_math_ordered', 'test_math_power',
    'test_math_rounding', 'test_math_scientific', 'test_math_units', 'test_metamorphic',
    'test_planner', 'test_q45_stress', 'test_q4_qualification', 'test_q5_workflow', 'test_qwen_profile',
    'test_relation_graph', 'test_reward_routing', 'test_runtime', 'test_runtime_limits',
    'test_search', 'test_search_data_qa', 'test_search_employer', 'test_search_extensions', 'test_search_grounded',
    'test_search_inverse_scope', 'test_search_pilot', 'test_semantic_compiler', 'test_semantics',
    'test_static_coding', 'test_synthetic_history', 'test_token_economics',
)
CLOSED_TESTS = frozenset('test_benchmark.BenchmarkTests.' + name for name in (
    'test_distribution_split_and_task_input_separation',
    'test_generation_is_byte_reproducible_and_has_no_agent_imports',
    'test_manifest_detects_mutation_and_generator_refuses_overwrite',
    'test_sql_references_recompute_from_csv_not_solver',
    'test_authored_numeric_anchors_have_second_checks',
    'test_pairs_are_predeclared_diverse_and_answerable',
    'test_coding_fixtures_remain_data_and_worker_has_no_trust_flag',
))
DIAGNOSTICS = (
    'a1_resume.py','batch_feasibility.py','compiler_replay.py','cycle_a.py',
    'fallback_pilot.py','metamorphic.py','q1_alias_probe.py','q2_coverage_probe.py',
    'q45_stress.py','q4_qualification_probe.py','q5_probe.py','qualification_audit.py','qwen_profile.py',
    'reward_routing.py','selection.py','static_coding.py','token_economics.py','trace.py',
)
EXPERIMENTS = ('math_ir.py','math_ir_parser.py','semantic_compiler.py')


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.write('\n')


def copy_regular(source, target):
    if source.is_symlink() or not source.is_file():
        raise ValueError('acceptance requires an ordinary source file')
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,target)


def prepare_view(destination):
    """Copy only explicit development inputs and public/synthetic test support."""
    destination = Path(destination)
    destination.mkdir()  # Never overwrite an earlier acceptance run.
    dataset=ROOT/'benchmark/dataset'
    original=json.loads((dataset/'manifest.json').read_text())  # Metadata only.
    files={name:sha for name,sha in original['files'].items()
           if len(Path(name).parts)>2 and Path(name).parts[:1]==('tasks',) and Path(name).parts[1] in IDS}
    for name,sha in files.items():
        parts=Path(name).parts
        if '..' in parts or Path(name).is_absolute():raise ValueError('unsafe manifest path')
        source=dataset/name
        if any(p.is_symlink() for p in (source,*source.parents)):
            raise ValueError('linked acceptance input')
        if hashlib.sha256(source.read_bytes()).hexdigest()!=sha:raise ValueError('development input changed')
        copy_regular(source,destination/'benchmark/dataset'/name)
    assert {Path(name).parts[1] for name in files}==IDS
    prior=json.loads((ROOT/'diagnostics/results/v4_d/measurements.json').read_text())['comparison']['development']['q5']['rows']
    assert len(prior)==64 and {r['task_id'] for r in prior}==IDS
    refs={}
    def add(key,value):
        if key not in IDS:raise ValueError('non-development reference')
        task=json.loads((dataset/'tasks'/key/'task.json').read_text())
        if {'expected','check'} & set(task):raise ValueError('reference fields in task input')
        kind=task['task_type']
        policy='exact' if kind=='search' else 'coding' if kind=='coding' else 'json' if isinstance(value,str) and value.startswith('{') else 'numeric'
        row=dict(task_id=key,task_type=kind,split='development',answer=value,answerable=value is not None,
                 policy=policy,reward=task['reward'],family='not_retained',difficulty='not_retained',oracle=None)
        if key in refs and refs[key]['answer']!=value:raise ValueError('published oracles disagree')
        refs[key]=row
    for row in prior:
        if row['status']=='correct':add(row['task_id'],row['actual'])
    offline=json.loads((ROOT/'diagnostics/results/offline.json').read_text())
    assert offline['live'] is False and offline['real_api_requests']==0
    for row in offline['experiments'][0]['rows']:add(row['task_id'],row['expected'])
    add('coding_d01','DONE')  # Explicit marker in that input; never grade by executing it.
    assert len(refs)==39
    pairs=[p for p in original['pairs'] if set(p['tasks'])<=IDS]
    assert len(pairs)==7
    manifest={'version':original['version'],'files':files,'pairs':pairs}
    base=destination/'benchmark/dataset'
    dump(base/'manifest.json',manifest);dump(base/'references.json',refs)
    plans={
        'math_d15':{'initial':{'value':'27','quote':'27 coins'},'steps':[
            {'op':'sub','value':'8','quote':'Subtract 8 coins'},{'op':'mul','value':'4','quote':'Multiply by 4'}]},
        'search_d15':{'entity':'Vela Nox','relations':['mentor','employer'],'field':'headquarters','answer':'Porto'},
        'data_analysis_d14':{'file':'observations.csv','operation':'sum','column':'units','filters':[
            {'column':'region','op':'=','type':'string','value':'east','quote':"region is 'east'"},
            {'column':'period','op':'>=','type':'number','value':'2','quote':'period at least 2'}]},
    }
    mocks={hashlib.sha256(json.loads((base/'tasks'/key/'task.json').read_text())['instruction'].encode()).hexdigest():
           {'task_id':key,'plan':plan} for key,plan in plans.items()}
    dump(base/'mocks.json',mocks)
    history=json.loads((ROOT/'diagnostics/results/plan.json').read_text())['plan']['selection']
    assert all(r['task_id'] in IDS for r in history)
    dump(destination/'benchmark/results/baseline/A-local.json',{'rows':[
        dict(task_id=r['task_id'],status=r['local_status'],outcome=r['local_outcome'],actual=r['local_answer']) for r in history]})
    for name in ('evaluate.py','worker.py'):
        copy_regular(ROOT/'benchmark'/name,destination/'benchmark'/name)
    for name in (*TESTS,'synthetic_history','coding_isolation_probe','allowed_acceptance'):
        copy_regular(ROOT/'tests'/(name+'.py'),destination/'tests'/(name+'.py'))
    for name in ('a1_ack_synthetic.json','a1_math_synthetic.json','a1_math_intent_synthetic.json'):
        copy_regular(ROOT/'tests/fixtures'/name,destination/'tests/fixtures'/name)
    for name in DIAGNOSTICS:copy_regular(ROOT/'diagnostics'/name,destination/'diagnostics'/name)
    for name in EXPERIMENTS:copy_regular(ROOT/name,destination/name)
    # Organizer example copies are test mounts only, never build inputs or Git additions.
    examples=ROOT/'codex-local/examples/task-dir'
    for key in ('math_0000','math_0004','search_0001','search_0005','data_analysis_0002','data_analysis_0006','coding_0003','coding_0007'):
        source=examples/key;target=destination/'codex-local/examples/task-dir'/key
        for file in source.rglob('*'):
            if file.is_file():copy_regular(file,target/file.relative_to(source))
        task=target/'task.json';data=json.loads(task.read_text());data.pop('expected',None);data.pop('check',None)
        task.write_text(json.dumps(data))
    h=json.loads((ROOT/'diagnostics/results/v4_h/measurements.json').read_text())
    expected={k:v for k,v in h['development']['arms'].items() if k!='q5'}
    assert all({r['task_id'] for r in a['rows']}==IDS for a in expected.values())
    dump(destination/'acceptance_expected.json',expected)
    return {'development_tasks':len(IDS),'permitted_pairs':len(pairs),'oracle_view':len(refs),'test_modules':len(TESTS)}


def guard():
    sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
    for key in list(os.environ):
        if key.startswith(('LLM_','NEURALDEEP_','OPENAI_')):os.environ.pop(key)
    os.environ['BUDGET']='0'
    stub=types.ModuleType('benchmark.corpus')
    def closed(*args,**kwargs):raise AssertionError('mixed corpus is unavailable')
    stub.cases=stub.generate=stub.sql_reference=closed
    sys.modules['benchmark.corpus']=stub
    net={'loopback_connects':0,'external_blocked':0}
    def audit(event,args):
        if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
            path=Path(os.fsdecode(args[0])).absolute()
            tasks=ROOT/'benchmark/dataset/tasks'
            if path.is_relative_to(tasks) and path.relative_to(tasks).parts and path.relative_to(tasks).parts[0] not in IDS:
                raise PermissionError('closed task blocked')
            if path.name=='.env' or path.name.startswith('.env.') or any(s in {'.a1-resume-state','.fallback-pilot-state','.search-pilot-state'} for s in path.parts):
                raise PermissionError('private file blocked')
        if event in ('socket.connect','socket.bind','socket.getaddrinfo'):
            address=args[0] if event=='socket.getaddrinfo' else args[1]
            host=address[0] if isinstance(address,tuple) else address
            if isinstance(host,str) and host not in ('127.0.0.1','::1','localhost'):
                net['external_blocked']+=1;raise PermissionError('external network forbidden')
            if event=='socket.connect':net['loopback_connects']+=1
    sys.addaudithook(audit)
    return net


def suite(output):
    net=guard();tests=[]
    def flatten(items):
        for item in items:
            if isinstance(item,unittest.TestSuite):yield from flatten(item)
            else:yield item
    for name in TESTS:
        tests.extend(flatten(unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module(name))))
    skipped=set()
    for test in tests:
        if test.id() in CLOSED_TESTS:
            skipped.add(test.id())
            def skip():raise unittest.SkipTest('mixed/closed corpus explicitly excluded')
            setattr(test,test._testMethodName,skip)
    assert skipped==CLOSED_TESTS
    started=time.monotonic();result=unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(tests))
    data=dict(discovered=result.testsRun,passed=result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
              failures=[t.id() for t,_ in result.failures],errors=[t.id() for t,_ in result.errors],
              skipped=[t.id() for t,_ in result.skipped],seconds=time.monotonic()-started,network=net)
    dump(output,data);print(json.dumps(data,indent=2))
    return 0 if result.wasSuccessful() else 1


def development(output):
    guard()
    import harness
    from budget_manager import BudgetManager
    from runtime_limits import RuntimeLimits
    expected=json.loads((ROOT/'acceptance_expected.json').read_text());arms={}
    for mode,old in expected.items():
        def run(ids):
            with tempfile.TemporaryDirectory(prefix='allowed-development-') as temporary,ExitStack() as stack:
                tasks=Path(temporary)/'tasks';tasks.mkdir()
                for key in ids:
                    if key not in IDS:raise ValueError('task outside allowlist')
                    shutil.copytree(ROOT/'benchmark/dataset/tasks'/key,tasks/key)
                before={str(p.relative_to(tasks)):hashlib.sha256(p.read_bytes()).hexdigest() for p in tasks.rglob('*') if p.is_file()}
                compiler=mode.startswith('compiler');manager=BudgetManager(100000 if compiler else 0);report={};calls=[]
                stack.enter_context(patch.dict(os.environ,{'BUDGET':str(int(manager.limit))}))
                stack.enter_context(patch('socket.socket.connect',side_effect=AssertionError('offline development')))
                if mode in ('graph','both'):
                    from search_solver import solve_search_graph
                    stack.enter_context(patch.dict(harness.SOLVERS,search=solve_search_graph))
                if mode in ('math','both'):
                    from math_ir_parser import solve_math_ir
                    stack.enter_context(patch.dict(harness.SOLVERS,math=solve_math_ir))
                gateway=None
                if compiler:
                    import httpx
                    from semantic_compiler import OfflineRoute,Contract
                    from llm_gateway import LLMGateway,DEFAULT_MODEL
                    variant=mode[-1];stack.enter_context(OfflineRoute(variant).installed())
                    def respond(request):
                        p=json.loads(json.loads(request.content)['messages'][-1]['content']);calls.append(1)
                        assert p['instruction']=='What year was Rin Vale, the engineer, born?'
                        body={'version':1,'kind':'search','ir':{'root':'Rin Vale (engineer)','hops':[],'projection':'born'}}
                        if variant=='B':
                            c=Contract('search',p['instruction'],tasks/'search_d12',variant='B')
                            body['evidence']=[{'source':r[0],'sha256':r[1],'span':[r[2],r[3]]} for r in sorted(c.required)]
                        return httpx.Response(200,json={'model':DEFAULT_MODEL,'usage':{'prompt_tokens':100,'completion_tokens':100},
                            'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(body)}}]})
                    gateway=LLMGateway('http://compiler.invalid/v1','synthetic-only',manager=manager,max_tokens=512,transport=httpx.MockTransport(respond))
                with redirect_stdout(io.StringIO()):
                    counts=asyncio.run(harness.run_tasks(tasks,manager=manager,budget=int(manager.limit),llm=gateway,report=report,limits=RuntimeLimits(120,30)))
                rows=[]
                for key in sorted(ids):
                    path=tasks/key/'answer.txt';actual=path.read_text().strip() if path.exists() else None
                    reference=next(r for r in old['rows'] if r['task_id']==key)
                    assert actual==reference['answer'],(mode,key,'WSL/Docker answer mismatch')
                    answer_mode=path.stat().st_mode & 0o777 if path.exists() else None
                    assert answer_mode==reference['answer_mode'],(mode,key,'mode mismatch')
                    assert not {'expected','check'} & set(json.loads((tasks/key/'task.json').read_text()))
                    rows.append(dict(task_id=key,answer=actual,answer_mode=answer_mode,status=reference['status'],reward=reference['reward']))
                assert all(hashlib.sha256((tasks/name).read_bytes()).hexdigest()==sha for name,sha in before.items())
                b=manager.snapshot()
                return dict(counts=counts,rows=rows,correct=sum(r['status']=='correct' for r in rows),wrong=0,
                    skipped=sum(r['status']=='skipped' for r in rows),reward=sum(r['reward'] for r in rows if r['status']=='correct'),
                    mock_requests=len(calls),spent=str(b['spent']),reserved=str(b['reserved']),time_exhausted=report['time_exhausted'])
        value=run(sorted(IDS));value['qualification']=[]
        assert all(value[k]==old[k] for k in ('correct','wrong','skipped','reward','mock_requests'))
        for pair in old['qualification']:
            observed=run(pair['tasks']);assert observed['correct']==pair['correct']
            value['qualification'].append(dict(pair_id=pair['pair_id'],correct=observed['correct']))
        arms[mode]=value;print(mode,value['correct'],value['reward'],flush=True)
    dump(output,{'scope':'64 allowlisted development inputs, 7 allowed pairs per arm; exact comparison to WSL V4-H','real_requests':0,'arms':arms})
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('prepare','suite','development'));parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.mode=='prepare':print(json.dumps(prepare_view(args.output)));return 0
    return suite(args.output) if args.mode=='suite' else development(args.output)


if __name__=='__main__':raise SystemExit(main())
