"""Independent, seeded metamorphic factory. No benchmark imports or network.

Reference worlds, formulas and SQLite aggregates are authored before rendering;
no tested parser/compiler/executor produces an oracle or a model wire plan.
"""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
import csv
import hashlib
import io
import json
from pathlib import Path
import random
import re
import sqlite3
import tempfile
from unittest.mock import patch

SEED = 20261110
DRAWS = 5


def number(value):
    return str(Fraction(value))


def _equal(actual, expected):
    if actual is None or expected is None:
        return False
    try:
        return Fraction(actual) == Fraction(expected)
    except (ValueError, ZeroDivisionError):
        return actual == expected


def reference(spec):
    """Small independent oracles; this function never consumes IR or question text."""
    kind = spec['kind']
    if kind == 'math':
        a, b, c = map(Fraction, spec['values'])
        mode = spec['mode']
        return number({'mean': lambda: (a+b+c)/3, 'abs': lambda: abs(a+b),
                       'ratio': lambda: a/b, 'percent': lambda: a*b/100,
                       'nested': lambda: abs((a+b)*c), 'add': lambda: a+b,
                       'ambiguous': lambda: None}[mode]()) if mode != 'ambiguous' else None
    if kind == 'search':
        people = spec['people']
        def bind(token):
            ids = [p['id'] for p in people if token in [p['label'], *p.get('aliases', [])]]
            return ids[0] if len(ids) == 1 else None
        current = bind(spec['token'])
        if current is None:
            return None
        for relation, direction in spec['path']:
            if direction == 'forward':
                targets = set(next(p for p in people if p['id'] == current)['fields'].get(relation, []))
                if len(targets) != 1:
                    return None
                current = next(iter(targets))
            else:
                candidates = [p for p in people if current in p['fields'].get(relation, [])]
                if len(candidates) != 1 or len(set(candidates[0]['fields'][relation])) != 1:
                    return None
                current = candidates[0]['id']
            matches = [p for p in people if p['id'] == current]
            if len(matches) != 1 or bind(matches[0]['label']) != current:
                return None
        entity = next(p for p in people if p['id'] == current)
        values = [entity['label']] if spec['projection'] == 'label' else entity['fields'].get(spec['projection'], [])
        return str(values[0]) if len(set(values)) == 1 else None
    if kind != 'data':
        raise ValueError('unknown independent oracle domain')
    rows = spec['records']; wanted = [v for sku, period, v in rows if sku == spec['sku'] and spec['lo'] <= period <= spec['hi']]
    assert wanted
    # Exact integer SQL sum/count/min/max, then a separate exact AVG calculation.
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE t (sku TEXT, period INTEGER, value INTEGER)')
        db.executemany('INSERT INTO t VALUES (?,?,?)', rows)
        aggregate = db.execute('SELECT SUM(value),COUNT(value),MIN(value),MAX(value) FROM t WHERE sku=? AND period BETWEEN ? AND ?',
                               (spec['sku'], spec['lo'], spec['hi'])).fetchone()
    assert aggregate == (sum(wanted), len(wanted), min(wanted), max(wanted))
    return number(dict(sum=aggregate[0], count=aggregate[1], min=aggregate[2], max=aggregate[3],
                       avg=Fraction(aggregate[0], aggregate[1]))[spec['operation']])


def render(spec):
    """Only render authored records; no interpretation by a production parser."""
    if spec['kind'] == 'math':
        return spec['text'], ()
    if spec['kind'] == 'search':
        labels = {p['id']: p['label'] for p in spec['people']}
        files = []
        for i, p in enumerate(spec['people']):
            lines = ['# '+p['label']]
            lines += ['Alias: '+alias for alias in p.get('aliases', [])]
            for relation in sorted(p['fields']):
                values = p['fields'][relation]
                lines += [relation.title()+': '+str(labels[v] if relation in ('mentor', 'employer') else v) for v in values]
            if spec.get('reverse_lines'):
                lines[1:] = reversed(lines[1:])
            text = '\n'.join(lines)+'\n'
            files.append((f'workspace/kb/{i:02d}.md', text))
        if spec.get('duplicate_document'):
            files.append(('workspace/kb/copy.md', files[0][1]))
        if spec.get('permute'):
            files = [(f'workspace/kb/reverse_{i:02d}.md', text) for i, (_, text) in enumerate(reversed(files))]
        return spec['question'], tuple(files)
    cols = spec['columns']; filename = spec['filename']
    output = io.StringIO(newline=''); writer = csv.writer(output)
    writer.writerow(cols); writer.writerows(spec['records'])
    sku = spec['sku'].replace("'", "''")
    question = (f"The file {filename}. What is the {spec['operation']} of {cols[2]} where {cols[0]} = '{sku}' "
                f"and {cols[1]} between {spec['lo']} and {spec['hi']} inclusive?")
    return question, ((f'workspace/{filename}', output.getvalue()),)


def certify_text(spec):
    """Independent bounded fixture binder; never calls the tested NL grammar.

    Verifies that the human-readable condition actually states the oracle model,
    rather than trusting a number attached to arbitrary generated text.
    """
    if spec['kind'] == 'data':
        assert len(set(spec['columns'])) == 3
        assert spec['lo'] <= spec['hi']
        return
    if spec['kind'] == 'search':
        paths = {
            (('mentor','forward'),): ("When was {root}'s mentor born?", 'born'),
            (('employer','inverse'),('mentor','forward')): ('When was the mentor of the employee of {root} born?', 'born'),
            (('mentor','forward'),('employer','forward')): ("Where is {root}'s mentor's employer headquartered?", 'headquarters'),
            (('mentor','forward'),('mentor','forward'),('employer','forward')):
                ("Where is {root}'s mentor's mentor's employer headquartered?", 'headquarters'),
        }
        template, field = paths[tuple(map(tuple, spec['path']))]
        assert spec['projection'] == field
        assert spec['question'] == template.format(root=spec['token']), 'question/model binding'
        return
    n = r'([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?)'
    mode, text = spec['mode'], spec['text']
    patterns = {
        'mean': [(rf'Compute mean\({n}, {n}, {n}\)', (0,1,2)),
                 (rf'Evaluate the average of \({n}, {n}, {n}\)', (0,1,2)),
                 (rf'Find the average of {n}, {n} and {n}\.', (0,1,2))],
        'abs': [(rf'Compute abs\({n} \+ {n}\)', (0,1))],
        'ratio': [(rf'Compute ratio\({n}, {n}\)', (0,1))],
        'nested': [(rf'Compute abs\(\({n} \+ {n}\) \* {n}\)', (0,1,2)),
                   (rf'Compute the absolute value of \({n} \* \({n} \+ {n}\)\)', (2,0,1))],
        'add': [(rf'Compute {n} meters \+ {n} meters', (0,1))],
        'percent': [(rf'Compute {n} percent of \({n}\)', (1,0)),
                    (rf'Compute ratio\(\({n} \* {n}\), 100\)', (0,1))],
        'ambiguous': [(rf'Compute {n} meters \+ {n} seconds', (0,1)),
                      (rf'Compute {n} / {n} \({n}\); no precedence convention is specified', (0,1,2))],
    }
    for pattern, indices in patterns[mode]:
        match = re.fullmatch(pattern, text)
        if match:
            assert tuple(map(Fraction, match.groups())) == tuple(Fraction(spec['values'][i]) for i in indices), 'literal/model binding'
            return
    raise AssertionError('instruction/model binding')


@dataclass(frozen=True)
class Sample:
    id: str
    category: str
    family: str
    role: str
    spec_json: str
    instruction: str
    files: tuple
    oracle: str | None
    plan_json: str | None = None
    should_accept: bool = True
    in_contract: bool = True


def sample(key, category, family, role, spec, *, plan=None, should_accept=True, in_contract=True):
    instruction, files = render(spec)
    return Sample(key, category, family, role, json.dumps(spec, sort_keys=True), instruction, files,
                  reference(spec), json.dumps(plan) if isinstance(plan, dict) else plan, should_accept, in_contract)


@dataclass(frozen=True)
class Pair:
    base: Sample
    changed: Sample
    law: str
    delta: str = '0'


def audit(pair):
    """Fail generation before SUT execution if model, rendering or law is wrong."""
    for case in (pair.base, pair.changed):
        spec = json.loads(case.spec_json)
        certify_text(spec)
        assert (case.instruction, case.files) == render(spec), 'render/source mutation'
        assert case.oracle == reference(spec), 'oracle mutation'
        if spec['kind'] == 'search':
            assert len({p['id'] for p in spec['people']}) == len(spec['people'])
            ids = {p['id'] for p in spec['people']}
            assert all(v in ids for p in spec['people'] for rel, vs in p['fields'].items() if rel in ('mentor','employer') for v in vs)
        if spec['kind'] == 'data':
            table = list(csv.reader(io.StringIO(case.files[0][1], newline='')))
            assert table[0] == spec['columns']
            assert table[1:] == [[str(v) for v in row] for row in spec['records']]
    assert pair.base.instruction != pair.changed.instruction or pair.base.files != pair.changed.files or pair.base.plan_json != pair.changed.plan_json
    a, b = pair.base.oracle, pair.changed.oracle
    if pair.law == 'same':
        assert a is not None and _equal(a, b)
    elif pair.law == 'delta':
        assert Fraction(b)-Fraction(a) == Fraction(pair.delta)
    elif pair.law == 'refuse':
        assert a is not None and b is None
    elif pair.law == 'plan_reject':
        assert pair.base.should_accept and not pair.changed.should_accept
        assert pair.base.plan_json != pair.changed.plan_json or pair.base.spec_json != pair.changed.spec_json
    elif pair.law != 'recompute':
        raise AssertionError('unknown metamorphic law')
    return pair


def lit(value): return {'value': str(value), 'unit': 'one'}
def op(name, *args): return {'op': name, 'args': list(args)}
def envelope(kind, ir): return {'version': 1, 'kind': kind, 'ir': ir}
def math_plan(root): return envelope('math', {'expr': root, 'unit': 'one'})


def worlds(rng):
    suffix = ''.join(rng.choice('abcdefghijklmnpqrstuvwxyz') for _ in range(9))
    p, m, co = 'Nexa '+suffix+' (pilot)', 'Bora '+suffix, 'Cedar '+suffix
    year = rng.randrange(1920, 1980)
    people = [dict(id='p', label=p, fields={'mentor':['m'], 'employer':['co'], 'born':[str(year+21)]}),
              dict(id='m', label=m, fields={'born':[str(year)], 'employer':['co']}),
              dict(id='co', label=co, fields={'headquarters':['Port '+suffix], 'founded':[str(year+32)]})]
    spec = dict(kind='search', people=people, token=p, path=[['mentor','forward']], projection='born',
                question=f"When was {p}'s mentor born?")
    return spec


def search_pairs(rng, draws):
    families = ('rename', 'qualified_homonym', 'ambiguous_homonym', 'permutation', 'alias', 'duplicate',
                'irrelevant_conflict', 'relevant_conflict', 'inverse', 'chain')
    for family in families:
        for i in range(draws):
            a=worlds(rng); b=deepcopy(a); law='same'
            p,m,co=[v['label'] for v in a['people']]
            if family=='rename':
                mapping={v['label']:v['label'].replace('Nexa','Vela').replace('Bora','Rhea').replace('Cedar','Amber') for v in b['people']}
                assert len(set(mapping.values()))==len(mapping)
                for entity in b['people']:entity['label']=mapping[entity['label']]
                b['token']=mapping[p];b['question']=f"When was {b['token']}'s mentor born?"
                assert all(x['fields']==y['fields'] for x,y in zip(a['people'],b['people']))
            elif family=='qualified_homonym':
                b['people'].append(dict(id='other',label=p.replace('(pilot)','(painter)'),fields={'born':['2001']}))
            elif family=='ambiguous_homonym':
                b['people'].append(dict(id='other',label=p,fields={'mentor':['co'],'born':['2001']}));law='refuse'
            elif family=='permutation':
                b['permute']=True;b['reverse_lines']=True
            elif family=='alias':
                alias='Kite '+m.split()[-1];b['people'][0]['aliases']=[alias];b['token']=alias
                b['question']=f"When was {alias}'s mentor born?"
            elif family=='duplicate':
                b['people'][0]['fields']['mentor'] *= 2;b['duplicate_document']=True
            elif family=='irrelevant_conflict':
                b['people'][0]['fields']['born'] += ['2009']
                b['people'].append(dict(id='noise',label='Remote '+m,fields={'born':['1900','1901']}))
            elif family=='relevant_conflict':
                b['people'][1]['fields']['born'] += ['2009'];law='refuse'
            elif family=='inverse':
                # Only p is employed by co: move m's employer to a distinct company.
                for s in (a,b):
                    s['people'][1]['fields'].pop('employer')
                b['token']=co;b['path']=[['employer','inverse'],['mentor','forward']]
                b['question']=f'When was the mentor of the employee of {co} born?'
            elif family=='chain':
                for s in (a,b):
                    s['path']=[['mentor','forward'],['employer','forward']];s['projection']='headquarters'
                    s['question']=f"Where is {p}'s mentor's employer headquartered?"
                b['people'].append(dict(id='middle',label='Middle '+m,fields={'mentor':['m']}))
                b['people'][0]['fields']['mentor']=['middle']
                b['path'].insert(0,['mentor','forward']);b['question']=f"Where is {p}'s mentor's mentor's employer headquartered?"
            key=f'search_{family}_{i}'
            yield audit(Pair(sample(key+'_base','search',family,'base',a),sample(key+'_changed','search',family,'changed',b),law))


def math_pairs(rng, draws):
    families=('substitution','negative','fractions','percent','scientific','nested','units','ambiguous','paraphrase')
    for family in families:
        for i in range(draws):
            a,b,c=rng.randrange(-999,-20),rng.randrange(21,900),rng.randrange(2,13)
            law='same';delta='0';mode='mean';values=[a,b,c]
            text=f'Compute mean({a}, {b}, {c})';changed=text;other=values[:];mode2=None
            if family=='substitution':
                d=rng.randrange(1,30);other=[x+d for x in values];changed='Compute mean('+', '.join(map(str,other))+')';law='delta';delta=str(d)
            elif family=='negative':
                mode='abs';text=f'Compute abs({a} + {b})';other=[-a,-b,c];changed=f'Compute abs({-a} + {-b})'
            elif family=='fractions':
                mode='ratio';text=f'Compute ratio({a}, {b})';k=rng.randrange(2,10)
                other=[a*k,b*k,c];changed=f'Compute ratio({a*k}, {b*k})';assert a*other[1]==other[0]*b
            elif family=='percent':
                mode='percent';b=rng.randrange(1,99);values=[a,b,c]
                text=f'Compute {b} percent of ({a})';changed=f'Compute ratio(({a} * {b}), 100)'
            elif family=='scientific':
                text=f'Compute mean({a}, {b}, {c})'
                changed=f'Evaluate the average of ({a}e0, {b*10}e-1, {c*100}e-2)'
                assert all(Fraction(raw)==v for raw,v in zip((f'{a}e0',f'{b*10}e-1',f'{c*100}e-2'),values))
            elif family=='nested':
                mode='nested';text=f'Compute abs(({a} + {b}) * {c})'
                changed=f'Compute the absolute value of ({c} * ({a} + {b}))'
            elif family=='units':
                mode='add';text=f'Compute {a} meters + {b} meters';changed=f'Compute {a} meters + {b} seconds';mode2='ambiguous';law='refuse'
            elif family=='ambiguous':
                mode='ratio';text=f'Compute ratio({a}, {b})';changed=f'Compute {a} / {b} ({c}); no precedence convention is specified';mode2='ambiguous';law='refuse'
            elif family=='paraphrase':
                changed=f'Find the average of {a}, {b} and {c}.'
            one=dict(kind='math',mode=mode,values=values,text=text)
            two=dict(kind='math',mode=mode2 or mode,values=other if family in ('negative','substitution','fractions') else values,text=changed)
            key=f'math_{family}_{i}'
            yield audit(Pair(sample(key+'_base','math',family,'base',one),sample(key+'_changed','math',family,'changed',two,in_contract=family!='paraphrase'),law,delta))


def data_pairs(rng, draws):
    for family in ('row_order','row_duplicate','boundaries','rename_columns','irrelevant_rows'):
        for i in range(draws):
            sku='gear, "'+str(rng.randrange(1000,9999))+'"';lo=rng.randrange(-3,3);hi=lo+3
            amounts=[rng.randrange(-30,30) for _ in range(4)];amounts[-1] -= sum(amounts)%3
            rows=[(sku,p,v) for p,v in zip(range(lo,hi+1),amounts)]+[('other',lo,999),(sku,lo-1,999),(sku,hi+1,999)]
            a=dict(kind='data',records=rows,sku=sku,lo=lo,hi=hi,columns=['sku','period','amount'],filename='ledger.csv',
                   operation=('sum','count','min','max','avg')[i%5]);b=deepcopy(a);law='same'
            if family=='row_order':
                rng.shuffle(b['records'])
                if b['records']==a['records']:b['records']=b['records'][1:]+b['records'][:1]
                assert Counter(map(tuple,a['records']))==Counter(map(tuple,b['records']))
            elif family=='row_duplicate':b['records'] *= 2;law='recompute'
            elif family=='boundaries':b['records'] += [(sku,lo,3),(sku,hi,6)];law='recompute'
            elif family=='rename_columns':b['columns']=['product','month','units'];b['filename']='nested/renamed.csv'
            else:b['records'] += [('alien',lo,9000),(sku,hi+2,-9000)]
            key=f'data_{family}_{i}'
            pair=Pair(sample(key+'_base','data',family,'base',a),sample(key+'_changed','data',family,'changed',b),law)
            if family=='row_duplicate':
                assert _equal(pair.changed.oracle,number(Fraction(pair.base.oracle)*(2 if a['operation'] in ('sum','count') else 1)))
            yield audit(pair)


def compiler_pairs(rng, draws):
    families=('operator','omission','order','source_number','identity','invented_fact','depth','json','source_conflict','equivalent_plan')
    for family in families:
        for i in range(draws):
            key=f'compiler_{family}_{i}'
            if family in ('identity','invented_fact','source_conflict'):
                a=worlds(rng);b=deepcopy(a);root=a['token']
                good=envelope('search',{'root':root,'hops':[{'relation':'mentor','direction':'forward'}],'projection':'born'})
                bad=deepcopy(good)
                if family=='identity':
                    year=int(b['people'][1]['fields']['born'][0])
                    artist=root.replace('(pilot)','(artist)')
                    b['people'].append(dict(id='artist',label=artist,fields={'mentor':['alien']}))
                    b['people'].append(dict(id='alien',label='Other '+b['people'][1]['label'],fields={'born':[str(year+7)]}))
                    a=deepcopy(b);bad['ir']['root']=artist
                    # Both paths are executable and yield different years: a
                    # rejection must establish identity, not rely on missing data.
                    other=deepcopy(b);other['token']=artist
                    assert reference(other)!=reference(b)
                elif family=='invented_fact':bad['ir']['hops'].append({'relation':'mentor','direction':'forward'})
                else:b['people'][1]['fields']['born'].append('2009')
            else:
                x,y,z=rng.randrange(10,999),rng.randrange(1,9),rng.randrange(2,12)
                a=dict(kind='math',mode='nested',values=[x,y,z],text=f'Compute abs(({x} + {y}) * {z})');b=deepcopy(a)
                good=math_plan(op('abs',op('mul',op('add',lit(x),lit(y)),lit(z))))
                bad=deepcopy(good)
                if family=='operator':bad=math_plan(op('abs',op('mul',op('sub',lit(x),lit(y)),lit(z))));assert abs((x-y)*z)!=abs((x+y)*z)
                elif family=='omission':bad=math_plan(op('abs',op('add',lit(x),lit(y))));assert abs(x+y)!=abs((x+y)*z)
                elif family=='order':bad=math_plan(op('abs',op('add',lit(x),op('mul',lit(y),lit(z)))));assert abs(x+y*z)!=abs((x+y)*z)
                elif family=='source_number':b['values'][0]+=1;b['text']=f'Compute abs(({x+1} + {y}) * {z})'
                elif family=='depth':
                    node=lit(x)
                    for _ in range(40):node=op('abs',node)
                    bad=math_plan(node)
                elif family=='json':bad='{"version":1,'
                elif family=='equivalent_plan':
                    bad=math_plan(op('abs',op('mul',op('add',lit(y),lit(x)),lit(z))))
                    assert (x+y)*z==(y+x)*z  # Independent semantic equivalence, not the compiler's canonical contract.
            one=sample(key+'_base','compiler',family,'base',a,plan=good)
            two=sample(key+'_changed','compiler',family,'changed',b,plan=bad,
                       should_accept=family=='equivalent_plan')
            yield audit(Pair(one,two,'same' if family=='equivalent_plan' else 'plan_reject'))


def factory(seed=SEED, draws=DRAWS):
    if type(draws) is not int or not 1 <= draws <= 20:
        raise ValueError('draw count must be between1and20')
    rng=random.Random(seed)
    return list(search_pairs(rng,draws))+list(math_pairs(rng,draws))+list(data_pairs(rng,draws))+list(compiler_pairs(rng,draws))


def evidence(case):
    refs=[{'source':'instruction','sha256':hashlib.sha256(case.instruction.encode()).hexdigest(),'span':[0,len(case.instruction)]}]
    for path,text in case.files:
        offset=0
        for line in text.splitlines(keepends=True):
            stripped=line.strip();start=offset+len(line)-len(line.lstrip());offset+=len(line)
            if stripped:
                refs.append({'source':'kb:'+path.removeprefix('workspace/kb/'),
                             'sha256':hashlib.sha256(text.encode()).hexdigest(),'span':[start,start+len(stripped)]})
    return refs


def _materialize(case,root):
    root.mkdir()
    for name,text in case.files:
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,newline='')


def evaluate(case,root,engine,variant=None):
    # Imports intentionally local: reference()/factory()/audit() require no SUT.
    from math_ir import MathIRRefusal, execute as math_execute, serialize
    from math_ir_parser import compile_grammar, compile_existing, verify
    from math_solver import UnsupportedMath
    from search_solver import solve_search_graph, UnsupportedSearch
    from relation_graph import Graph, GraphRefusal, compile_query, execute, load_snapshot, verify_candidate
    from data_solver import solve_data, UnsupportedData
    from semantic_compiler import Contract, CompilerRefusal
    answer=None;executor_answer=None;status='skipped';verifier_error=False;unexpected=None
    try:
        if engine=='search':
            snap=load_snapshot(root);graph=Graph(snap);candidate=execute(graph,compile_query(case.instruction,graph));executor_answer=candidate.answer
            try:answer=verify_candidate(case.instruction,candidate,snap,task_dir=root).answer
            except GraphRefusal:
                verifier_error=case.oracle is not None;raise
        elif engine=='portfolio':answer=solve_search_graph(case.instruction,root)
        elif engine=='math':
            program=None
            for compiler in (compile_grammar,compile_existing):
                try:program=compiler(case.instruction);break
                except MathIRRefusal:pass
            if program is None:raise MathIRRefusal('not admitted')
            try:verified=verify(case.instruction,program)
            except MathIRRefusal:
                verifier_error=case.in_contract and case.oracle is not None;raise
            answer=serialize(math_execute(verified),program);executor_answer=answer
        elif engine=='data':answer=solve_data(case.instruction,root)
        else:
            try:
                contract=Contract(json.loads(case.spec_json)['kind'],case.instruction,root,variant=variant)
                try:plan=json.loads(case.plan_json)
                except ValueError:body=case.plan_json
                else:
                    if variant=='B':plan['evidence']=evidence(case)
                    body=json.dumps(plan)
                outcome=contract.assess_content(body)
            except CompilerRefusal as exc:outcome=exc.outcome
            answer=outcome.answer;executor_answer=outcome.executor_answer;status=outcome.status
            verifier_error=outcome.status=='verification_rejected' and case.should_accept
        if engine!='compiler':status='accepted'
    except (MathIRRefusal,UnsupportedMath,GraphRefusal,UnsupportedSearch,UnsupportedData):pass
    except Exception as exc:unexpected=type(exc).__name__;status='error'
    accepted=answer is not None
    expected=case.should_accept and case.oracle is not None
    phases={name:getattr(outcome,name) for name in ('json_valid','schema_valid','semantic_valid','executed')} if engine=='compiler' else {}
    input_hash=hashlib.sha256(json.dumps([case.instruction,case.files,case.plan_json],ensure_ascii=False).encode()).hexdigest()
    return {'id':case.id,'engine':engine,'variant':variant,'family':case.family,'role':case.role,'oracle':case.oracle,'input_sha256':input_hash,
            'answer':str(answer) if accepted else None,'status':status,'accepted':accepted,
            'correct':accepted and _equal(str(answer),case.oracle),'wrong':accepted and not _equal(str(answer),case.oracle),
            'false_accept':accepted and not expected,'false_refusal':not accepted and expected,
            'expected_accept':expected,
            'in_contract':case.in_contract and not (engine=='portfolio' and case.family=='irrelevant_conflict' and case.role=='changed'),
            'verifier_error':verifier_error,'error':unexpected,
            'executor_wrong':executor_answer is not None and not _equal(str(executor_answer),case.oracle),**phases}


def summary(rows):
    accepted=[r for r in rows if r['accepted']];solvable=[r for r in rows if r['oracle'] is not None]
    valid=[r for r in rows if r['expected_accept']]
    return {'checks':len(rows),'accepted':len(accepted),'correct':sum(r['correct'] for r in rows),
            'wrong':sum(r['wrong'] for r in rows),'safe_skip':sum(not r['accepted'] and r['error'] is None for r in rows),
            'precision':sum(r['correct'] for r in accepted)/len(accepted) if accepted else None,
            'solvable':len(solvable),'coverage':sum(r['correct'] for r in solvable)/len(solvable) if solvable else None,
            'valid_proposals':len(valid),'valid_proposal_coverage':sum(r['correct'] for r in valid)/len(valid) if valid else None,
            'false_accept':sum(r['false_accept'] for r in rows),'false_refusal':sum(r['false_refusal'] for r in rows),
            'in_contract_false_refusal':sum(r['false_refusal'] and r['in_contract'] for r in rows),
            'verifier_errors':sum(r['verifier_error'] for r in rows),'executor_wrong':sum(r['executor_wrong'] for r in rows),
            'errors':sum(r['error'] is not None for r in rows)}


def run(seed=SEED,draws=DRAWS):
    pairs=factory(seed,draws);rows=[];relations=[]
    with tempfile.TemporaryDirectory(prefix='metamorphic-') as tmp, patch('socket.socket.connect',side_effect=AssertionError('network forbidden')):
        for index,pair in enumerate(pairs):
            audit(pair)
            engines=[('compiler','A'),('compiler','B')] if pair.base.category=='compiler' else [('search',None),('portfolio',None)] if pair.base.category=='search' else [(pair.base.category,None)]
            for engine,variant in engines:
                observed=[]
                for case in (pair.base,pair.changed):
                    root=Path(tmp)/f'{index}_{engine}_{variant}_{case.role}';_materialize(case,root)
                    observed.append(evaluate(case,root,engine,variant))
                rows.extend(observed)
                ok=observed[0]['correct'] and (not observed[1]['accepted'] if pair.changed.oracle is None or not pair.changed.should_accept else observed[1]['correct'])
                relations.append({'id':pair.base.id.removesuffix('_base'),'engine':engine,'variant':variant,'family':pair.base.family,'consistent':ok})
    by={e:summary([r for r in rows if r['engine']==e]) for e in sorted({r['engine'] for r in rows})}
    families=sorted({(p.base.category,p.base.family) for p in pairs})
    return {'scope':'independent authored metamorphic scenarios; NOT real model or holdout accuracy','seed':seed,'draws':draws,
            'real_requests':0,'families':[list(f) for f in families],'family_count':len(families),'pairs':len(pairs),
            'rows':rows,'relations':relations,'by_engine':by,'summary':summary(rows),
            'compiler_by_variant':{v:summary([r for r in rows if r['engine']=='compiler' and r['variant']==v]) for v in ('A','B')},
            'consistency':{'passed':sum(r['consistent'] for r in relations),'evaluated':len(relations)},
            'distinct_task_inputs':len({(c.instruction,c.files) for p in pairs for c in (p.base,p.changed)})}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.output.exists():parser.error('refusing to overwrite an existing report')
    result=run()
    with args.output.open('x') as stream:json.dump(result,stream,ensure_ascii=False,indent=2);stream.write('\n')
    print(json.dumps({'by_engine':result['by_engine'],'families':result['family_count'],'consistency':result['consistency']},indent=2))


if __name__=='__main__':main()
