"""Offline JSONL traces and deterministic scheduling replay. Never a live client.

Production-policy replay calls the real Planner.next/estimate/finish for paid
attempts. Local results/eligibility come from prior observations. No task code,
model prompts, responses or grading answers are stored in traces.
"""
import argparse
from collections import Counter
from fractions import Fraction
import json
import hashlib
import math
from pathlib import Path
import random
from types import SimpleNamespace
from unittest.mock import patch

import harness
from benchmark.evaluate import DATASET, evaluate, runtime_fingerprint
from budget_manager import BudgetManager
from llm_gateway import DEFAULT_MODEL, MODELS
from planner import Planner, Features, features
from safe_files import read_text


POLICIES=('production','reward_first','cost_first','expected_reward','local_first')
BUDGETS=(100000,50000,20000,10000)
TRACE_FIELDS=frozenset('schema_version task_id task_type reward trace_source scope model route complexity '
    'native_form blocker local_validated local_correct local_seconds local_terminal eligible llm_validated '
    'llm_correct llm_seconds estimated_input max_tokens estimated_cost estimated_success probability_source '
    'input_tokens output_tokens reasoning_tokens usage_known output_token_semantics cost calls refusal '
    'time_scope seconds correct protocol_contract quality_sample_for_tasks source_note'.split())


def failure_class(row,mode,frozen=False):
    """Evidence classes; a skipped answer alone does not prove infeasibility."""
    if row['status']=='correct':return 'correct'
    if row['task_type']=='coding' and row['outcome']=='unsafe':return 'coding_safe_disabled'
    if row['outcome'] in {'timed_out','time_exhausted'}:return 'time'
    reasons=[o['reason'] for o in row.get('observations',[])]
    if row['outcome']=='insufficient_evidence':return 'missing_or_ambiguous_evidence'
    if row['outcome']=='invalid_data':return 'invalid_input'
    if row['outcome']=='unsolvable':return 'supported_unsolvable'
    if row['outcome']=='budget_blocked':
        if mode=='local':return 'fallback_not_enabled_in_local_run'
        return 'unknown_usage_frozen' if frozen else 'full_reservation_unavailable'
    if row['outcome']=='unsupported':return 'unsupported_preparation_or_semantics'
    if row['outcome']=='llm_failed':
        if any('could not be validated' in r for r in reasons):return 'plan_validation_or_execution_rejected'
        return 'api_or_protocol_failure'
    if row['outcome']=='failed':return 'workflow_or_publication_failure'
    return 'other_not_proven'


def write_jsonl(path,rows):
    rows=list(rows)
    if any(not isinstance(row,dict) or set(row)-TRACE_FIELDS for row in rows):
        raise ValueError('only allowlisted trace metadata can be saved')
    with Path(path).open('x') as stream:
        for row in rows:stream.write(json.dumps(row,sort_keys=True,allow_nan=False)+'\n')


def read_jsonl(path):
    path=Path(path).absolute()
    text=read_text(path.parent,path.name,maximum=10*1024*1024)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def forecast_error(rows):
    """Evaluation after the fact, never a source for ranking or fitted priors."""
    paid=[r for r in rows if r.get('eligible')]
    known=[r for r in paid if r['usage_known']]
    success=lambda r:int(r['llm_validated'] and r['llm_correct'] and r['usage_known'])
    actual=lambda r:Fraction(r['input_tokens'],10)+r['output_tokens']
    return {'eligible_tape_rows':len(paid),'known_cost_rows':len(known),
            'brier':float(sum((Fraction(r['estimated_success'])-success(r))**2 for r in paid)/len(paid)) if paid else None,
            'mean_estimated_success':float(sum(Fraction(r['estimated_success']) for r in paid)/len(paid)) if paid else None,
            'observed_pipeline_success_on_tape':sum(success(r) for r in paid)/len(paid) if paid else None,
            'mean_estimated_cost':float(sum(Fraction(r['estimated_cost']) for r in known)/len(known)) if known else None,
            'mean_actual_cost_on_tape':float(sum(actual(r) for r in known)/len(known)) if known else None,
            'cost_mae':float(sum(abs(Fraction(r['estimated_cost'])-actual(r)) for r in known)/len(known)) if known else None,
            'scope':'conditional eligible tape; NOT real Qwen calibration; no forecasts updated'}


def collect_benchmark(split='development'):
    """Actual SDK workflow; grader answers never enter the exported metadata."""
    local=evaluate(split=split,mode='local')
    mock=evaluate(split=split,mode='mock-good',budget=100000)
    first={r['task_id']:r for r in local['rows']}
    requests={Path(r['task_id']).name:r for r in mock['budget_state']['requests']}
    rows=[]
    for r in mock['rows']:
        key=r['task_id'];old=first[key];record=requests.get(key)
        task=harness.load_task(DATASET/'tasks'/key/'task.json');f=features(task)
        local_ok=old['status'] in {'correct','wrong','unverified'}
        inp=record['estimated_input'] if record else 0
        cap=record['max_output'] if record else 256
        complexity=f.complexity
        p=Fraction({'math':75,'search':80,'data_analysis':75}.get(r['task_type'],0),100)/(1+Fraction(complexity,10))
        rows.append({'schema_version':1,'task_id':key,'task_type':r['task_type'],'reward':r['reward'],
          'trace_source':'mock' if record else 'local','scope':r['split'],'model':DEFAULT_MODEL if record else None,
          'route':'fallback' if record else 'local' if old['observations'] else 'blocked',
          'complexity':complexity,'native_form':f.native_form,'blocker':f.blocker,
          'local_validated':local_ok,'local_correct':old['status']=='correct',
          'local_seconds':sum(o['seconds'] for o in old['observations']),'eligible':record is not None,
          'local_terminal':r['outcome'] if record is None else 'unsupported',
          'llm_validated':record is not None and any(o['phase']=='fallback' and o['status']=='solved' for o in r['observations']),
          'llm_correct':record is not None and r['status']=='correct',
          'llm_seconds':sum(o['seconds'] for o in r['observations'] if o['phase']=='fallback'),
          'estimated_input':inp,'max_tokens':cap,'estimated_cost':str(Fraction(inp,10)+Fraction(cap,2)),
          'estimated_success':str(p),'probability_source':'uncalibrated_engineering_prior',
          'input_tokens':record['input_tokens'] if record else 0,'output_tokens':record['output_tokens'] if record else 0,
          'reasoning_tokens':None if record else 0,'usage_known':record is None or record['state']=='settled',
          'output_token_semantics':'accounted_total_including_reasoning',
          'cost':r['llm_spent'] if record is None or record['state']=='settled' else None,
          'calls':r['llm_calls'],'refusal':None if r['status']=='correct' else failure_class(r,'mock-good',mock['budget_state']['frozen']),
          'time_scope':'workflow phases; excludes startup and scheduler preparation'})
    return rows,{'runtime_sha256':runtime_fingerprint(),'local':local['summary'],'mock':mock['summary'],
                 'actual_production_order':mock['order'],
                 'source':'local SDK workflow and mock HTTP; NOT measured Qwen quality/cost',
                 'real_requests':0,'seed':None}


def historical_summaries():
    """Only usage/outcomes already explicitly reported by the team, no receipts."""
    return [{'schema_version':1,'task_id':key,'task_type':kind,'scope':scope,'trace_source':'real',
             'model':DEFAULT_MODEL,'calls':1,'input_tokens':inp,'output_tokens':out,
             'reasoning_tokens':None,'cost':str(Fraction(inp,10)+out),'seconds':None,
             'correct':correct,'protocol_contract':contract,'quality_sample_for_tasks':scope=='development',
             'source_note':'public team-confirmed aggregate; original response/payload not included'}
            for key,kind,scope,inp,out,correct,contract in (
                ('a1_ack','protocol_probe','protocol',35,11,None,False),
                ('a1_math','math','protocol',53,7,True,True),
                ('math_d15','math','development',181,111,True,True))]


def synthetic_cohort(profile='cheap',seed=20261109,count=100):
    if profile not in {'cheap','long_output','time_pressure','unknown_usage'}:raise ValueError('unknown profile')
    rng=random.Random(seed);rows=[]
    for i in range(count):
        kind=('math','search','data_analysis','coding')[i%4]
        complexity=rng.randint(1,9);reward=rng.choice((0,1,2,3,5,8,13)) # Independent of difficulty.
        local=rng.random()<0.25 and kind!='coding'
        inp=rng.randint(1000,4000);cap=4096 if profile=='long_output' else 1536
        output_draw=rng.random()
        output=(2600 if profile=='long_output' else 100)+int(1101*output_draw)
        valid=rng.random()<0.82
        eligible=kind!='coding' and not local
        latency=rng.uniform(1,15)
        rows.append({'schema_version':1,'task_id':f'synthetic_{i:03d}','task_type':kind,'reward':reward,
          'trace_source':'synthetic','scope':'hypothesis_'+profile,'model':DEFAULT_MODEL,
          'route':'local' if local else 'fallback','complexity':complexity,'native_form':local,
          'blocker':'unsafe' if kind=='coding' else '', 'local_validated':local,'local_correct':local,
          'local_seconds':29 if profile=='time_pressure' else 0.01,'eligible':eligible,
          'llm_validated':valid,'llm_correct':valid,'llm_seconds':29.5 if profile=='time_pressure' else latency,
          'estimated_input':inp+500,'max_tokens':cap,'estimated_cost':str(Fraction(inp+500,10)+Fraction(cap,2)),
          'estimated_success':str(Fraction(3,4)/(1+Fraction(complexity,10))),
          'probability_source':'uncalibrated_engineering_prior','input_tokens':inp,'output_tokens':output,
          'reasoning_tokens':0,'usage_known':True,'cost':str(Fraction(inp,10)+output),
          'output_token_semantics':'accounted_total_including_reasoning',
          'calls':int(eligible),'refusal':None,'time_scope':'invented operation time including local preparation'})
    if profile=='unknown_usage':
        fault=next((row for row in rows if row['eligible']),None)
        if fault is not None:fault.update(usage_known=False,cost=None)
    return rows


class ReplayPlanner(Planner):
    def _sort_key(self,entry,estimate=None):
        row=self.rows[entry.task.task_id]
        if self.policy=='production':return super()._sort_key(entry,estimate)
        if self.policy=='local_first':return (entry.key,)
        if self.policy=='reward_first':return (-Fraction(str(entry.task.reward)),entry.key)
        if estimate is None:return super()._sort_key(entry,estimate)
        if self.policy=='cost_first':return (Fraction(row['estimated_cost']),entry.key)
        return (-Fraction(str(entry.task.reward))*Fraction(row['estimated_success'])/Fraction(row['estimated_cost']),entry.key)


def replay(rows,policy='production',budget=100000,deadline=3540,operation_timeout=30,pairs=(),*,evaluation_only=False):
    """Simulation: outcome/usage tape frozen before ordering; actuals never rank."""
    if policy not in POLICIES or type(budget) is not int or budget<0:raise ValueError('invalid policy/budget')
    if not math.isfinite(deadline) or not 0<deadline<=3540 or not math.isfinite(operation_timeout) or operation_timeout<=0:
        raise ValueError('invalid deadline')
    if not rows:raise ValueError('empty trace cohort')
    keys=[r['task_id'] for r in rows]
    if len(set(keys))!=len(keys) or any(not __import__('re').fullmatch(r'[A-Za-z0-9_-]+',k) for k in keys):raise ValueError('invalid/duplicate task ID')
    if any(len(pair)!=2 or len(set(pair))!=2 or not set(pair)<=set(keys) for pair in pairs):raise ValueError('invalid evaluation pair')
    caps={r['max_tokens'] for r in rows};models={r['model'] or DEFAULT_MODEL for r in rows}
    if (len(caps)!=1 or len(models)!=1 or not models<=MODELS
        or any(type(cap) is not int or not 1<=cap<=4096 for cap in caps)):
        raise ValueError('replay requires one allowed model/output cap per cohort')
    for r in rows:
        if r.get('schema_version')!=1 or r['task_type'] not in {'math','search','data_analysis','coding'}:
            raise ValueError('unsupported trace schema/type')
        if any(type(r[k]) is not bool for k in ('native_form','local_validated','local_correct','eligible','llm_validated','llm_correct','usage_known')):
            raise ValueError('invalid trace boolean')
        if r.get('scope')=='protocol' or r['trace_source']=='real' or (r.get('scope')=='holdout' and not evaluation_only):
            raise ValueError('not a replay quality sample; holdout requires evaluation-only mode')
        if not 0<=Fraction(r['estimated_success'])<=1 or Fraction(r['estimated_cost'])<=0:raise ValueError('invalid prediction')
        if any(not math.isfinite(r[k]) or r[k]<0 for k in ('local_seconds','llm_seconds')):raise ValueError('invalid duration')
        if type(r['estimated_input']) is not int or r['estimated_input']<0:raise ValueError('invalid input estimate')
        if r['eligible'] and r['estimated_input']==0:raise ValueError('eligible trace needs positive input estimate')
        if isinstance(r['reward'],bool) or not isinstance(r['reward'],(int,float)) or not math.isfinite(r['reward']) or r['reward']<0:
            raise ValueError('invalid reward')
    manager=BudgetManager(budget)
    class Gateway:
        model=next(iter(models));max_tokens=next(iter(caps))
        @property
        def available(self):return manager.available>0
    gateway=Gateway();gateway.manager=manager
    by_id={r['task_id']:r for r in rows}
    tasks=[SimpleNamespace(task_id=r['task_id'],task_type=r['task_type'],reward=r['reward'],
                           directory=Path('/replay')/r['task_id']) for r in rows]
    def replay_features(task,trusted):
        r=by_id[task.task_id]
        blocker='unsafe' if task.task_type=='coding' else r['blocker']
        return Features(r['complexity'],r['native_form'],not blocker,blocker)
    with patch('planner.features',replay_features):
        planner=ReplayPlanner(tasks,manager,gateway)
    planner.rows=by_id;planner.policy=policy
    for e in planner.entries:e.estimated_input=by_id[e.task.task_id]['estimated_input']
    elapsed=0.;correct=set();wrong=set();calls=0;order=[]
    while elapsed<deadline:
        decision=planner.next()
        if decision is None:break
        entry=decision.entry;r=by_id[entry.task.task_id];phase=decision.phase
        duration=r['local_seconds' if phase=='local' else 'llm_seconds'];remaining=deadline-elapsed
        order.append([r['task_id'],phase])
        if phase=='fallback':
            calls+=1;request=f'replay:{calls}'
            manager.reserve(request,entry.key,entry.estimated_input,gateway.max_tokens)
        if duration>=min(remaining,operation_timeout):
            elapsed+=min(remaining,operation_timeout);entry.state='timed_out'
            if phase=='fallback':manager.mark_uncertain(request)
            continue
        elapsed+=duration
        if phase=='local':
            if r['local_validated']:
                entry.state='solved';(correct if r['local_correct'] else wrong).add(r['task_id'])
            else:
                entry.local_result='unsupported'
                entry.state='deferred' if r['eligible'] else r.get('local_terminal','unsupported')
        else:
            if r['usage_known']:manager.settle(request,r['input_tokens'],r['output_tokens'])
            else:manager.mark_uncertain(request)
            planner.finish(decision,'solved' if r['llm_validated'] and r['usage_known'] else 'skipped','replay proposal rejected')
            if entry.state=='solved':(correct if r['llm_correct'] else wrong).add(r['task_id'])
    for entry in planner.entries:
        if not entry.terminal:entry.state='time_exhausted'
    snapshot=manager.snapshot();states=Counter(e.state for e in planner.entries)
    pair_counts=Counter(sum(key in correct for key in pair) for pair in pairs)
    return {'source':'simulation','policy':policy,'budget':budget,'deadline':deadline,'correct':len(correct),'wrong':len(wrong),
            'skipped':len(rows)-len(correct)-len(wrong),'reward':sum(by_id[k]['reward'] for k in correct),
            'spent':str(snapshot['spent']),'reserved':str(snapshot['reserved']),'cost_known':snapshot['uncertain_calls']==0,
            'frozen':snapshot['frozen'],'calls':calls,'simulated_seconds':round(elapsed,6),'stop_reasons':dict(states),
            'qualification_scope':'pair coverage within this run; isolated frozen pairs are evaluated separately',
            'qualification':{'pairs':len(pairs),'2/2':pair_counts[2],'1/2':pair_counts[1],'0/2':pair_counts[0],
                             'pass_fraction':pair_counts[2]/len(pairs) if pairs else None},'order':order}


def compare(rows,*,evaluation_only=False):
    # Disjoint adjacent controls are hypothetical pairs, not the frozen20.
    ids=[r['task_id'] for r in rows];pairs=[ids[i:i+2] for i in range(0,len(ids)-1,2)]
    return [replay(rows,p,b,pairs=pairs,evaluation_only=evaluation_only) for b in BUDGETS for p in POLICIES]


def compare_fixed_pairs(rows,pairs):
    """Frozen manifest unchanged: each pair has its own complete budget/run."""
    index={r['task_id']:r for r in rows};results=[]
    for budget in BUDGETS:
        for policy in POLICIES:
            runs=[replay([index[k] for k in pair['tasks']],policy,budget,
                         pairs=[pair['tasks']],evaluation_only=True) for pair in pairs]
            count=Counter(r['correct'] for r in runs)
            results.append({'source':'simulation','scope':'fixed pairs, independent budget per pair',
                'policy':policy,'budget':budget,'qualification':{'pairs':len(runs),'2/2':count[2],
                  '1/2':count[1],'0/2':count[0],'pass_fraction':count[2]/len(runs) if runs else None},
                'correct':sum(r['correct'] for r in runs),'wrong':sum(r['wrong'] for r in runs),
                'skipped':sum(r['skipped'] for r in runs),'reward':sum(r['reward'] for r in runs),
                'spent':str(sum(Fraction(r['spent']) for r in runs)),
                'reserved':str(sum(Fraction(r['reserved']) for r in runs)),
                'calls':sum(r['calls'] for r in runs),'simulated_seconds':sum(r['simulated_seconds'] for r in runs),
                'stop_reasons':dict(sum((Counter(r['stop_reasons']) for r in runs),Counter()))})
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--collect-development',action='store_true')
    parser.add_argument('--collect-frozen',action='store_true',help='evaluation only; never fit priors from holdout labels')
    parser.add_argument('--replay',type=Path,help='replay saved development/synthetic JSONL without HTTP')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    if args.replay and (args.collect_development or args.collect_frozen):parser.error('replay cannot collect new traces')
    cohorts={'replay':read_jsonl(args.replay)} if args.replay else {
        name:synthetic_cohort(name) for name in ('cheap','long_output','time_pressure','unknown_usage')}
    if args.collect_development:
        rows,meta=collect_benchmark();cohorts['development_mock']=rows
        (args.output/'collection.json').write_text(json.dumps(meta,indent=2))
    if args.collect_frozen:
        rows,meta=collect_benchmark('all');cohorts['frozen_evaluation_only']=rows
        (args.output/'frozen_collection.json').write_text(json.dumps(meta,indent=2))
    write_jsonl(args.output/'real_summaries.jsonl',historical_summaries())
    results={}
    for name,rows in cohorts.items():
        write_jsonl(args.output/(name+'.jsonl'),rows);results[name]=compare(rows,evaluation_only=name=='frozen_evaluation_only')
    if args.collect_frozen:
        manifest=json.loads((DATASET/'manifest.json').read_text())
        results['fixed_pairs_evaluation_only']=compare_fixed_pairs(cohorts['frozen_evaluation_only'],manifest['pairs'])
    for name,filename in (('development_mock','collection.json'),('frozen_evaluation_only','frozen_collection.json')):
        if name not in cohorts:continue
        actual=json.loads((args.output/filename).read_text())
        replayed=next(r for r in results[name] if r['budget']==100000 and r['policy']=='production')
        if (replayed['correct']!=actual['mock']['correct'] or replayed['wrong']!=actual['mock']['wrong']
            or replayed['reward']!=actual['mock']['reward'] or replayed['calls']!=actual['mock']['llm_calls']
            or Fraction(replayed['spent'])!=Fraction(str(actual['mock']['llm_spent']))
            or replayed['order']!=actual['actual_production_order']):
            raise ValueError('production replay differs from actual workflow; do not trust comparison')
    (args.output/'comparison.json').write_text(json.dumps(results,indent=2))
    (args.output/'forecasts.json').write_text(json.dumps({name:forecast_error(rows) for name,rows in cohorts.items()},indent=2))
    (args.output/'manifest.json').write_text(json.dumps({'schema_version':1,'synthetic_seed':20261109,
        'runtime_sha256':runtime_fingerprint(),
        'research_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'traces_sha256':{name:hashlib.sha256((args.output/(name+'.jsonl')).read_bytes()).hexdigest() for name in cohorts},
        'source':'offline measured local/mock and explicitly synthetic tapes; not Qwen predictions',
        'training_on_holdout':False,'real_requests':0},indent=2))
    print(json.dumps({name:[{k:r[k] for k in ('policy','budget','correct','wrong','reward','calls','spent','reserved','simulated_seconds')} for r in rows if r['budget']==100000] for name,rows in results.items()},indent=2))


if __name__=='__main__':main()
