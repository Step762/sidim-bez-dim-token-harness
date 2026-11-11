"""Opt-in offline semantic compiler using existing IR, graph, validators and gateway.

This module is not imported by production. No real transport is admitted here;
future LIVE use requires a separately authorized, durable pilot integration.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
from unittest.mock import patch

import httpx
import harness
import planner
import math_solver
import search_solver
from llm_fallback import InvalidPlan, Prepared, _SYSTEM, prepare as legacy_prepare, solve_fallback as legacy_fallback
from llm_gateway import LLMError, LLMGateway, strict_json
from math_ir import Expr, Literal, MathIR, MathIRRefusal, Quantity, UNITS, MAX_DEPTH, MAX_NODES
from math_ir import execute as math_execute, serialize as math_serialize, validate as math_validate
from math_ir_parser import compile_existing, compile_grammar, verify as math_verify
from relation_graph import (
    ATTRIBUTES, RELATIONS, MAX_HOPS, Graph, GraphRefusal, Hop, Query,
    compile_query, execute as graph_execute, load_snapshot, verify_candidate,
)

MAX_PLAN_CHARS = 24000
MAX_EVIDENCE = 128


@dataclass(frozen=True)
class Outcome:
    status: str
    json_valid: bool | None = None
    schema_valid: bool | None = None
    semantic_valid: bool | None = None
    evidence_valid: bool | None = None
    executed: bool = False
    verification_valid: bool | None = None
    executor_answer: str | None = None
    answer: str | None = None


class CompilerRefusal(InvalidPlan):
    def __init__(self, outcome):
        self.outcome = outcome
        super().__init__(outcome.status)


class _SchemaError(ValueError):
    pass


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise _SchemaError('unexpected JSON keys')


def _number(raw):
    if type(raw) is not str or len(raw)>161:
        raise _SchemaError('numeric string required')
    if '/' in raw:
        if re.fullmatch(r'[+-]?[0-9]{1,80}/[+-]?[0-9]{1,80}',raw) is None:
            raise _SchemaError('invalid rational literal')
    else:
        if len(raw)>80 or re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?',raw) is None:
            raise _SchemaError('invalid exact literal')
        if exponent:=re.search(r'[eE]([+-]?[0-9]+)$',raw):
            if len(exponent[1].lstrip('+-'))>3 or abs(int(exponent[1]))>100:
                raise _SchemaError('exponent limit')
    try:
        value=Fraction(raw)
    except (ValueError,ZeroDivisionError):raise _SchemaError('invalid literal domain') from None
    if max(value.numerator.bit_length(),value.denominator.bit_length())>2048:
        raise _SchemaError('literal value limit')
    return value


def decode_math(raw):
    _keys(raw,{'expr','unit'})
    if type(raw['unit']) is not str or raw['unit'] not in UNITS:
        raise _SchemaError('unknown result unit')
    count=0
    arities={'add':2,'sub':2,'mul':2,'div':2,'floordiv':2,'neg':1,'abs':1,'floor':1,
             'percent':2,'increase_percent':2,'decrease_percent':2,'power':2,'round':2,'convert':1}
    def visit(node,depth):
        nonlocal count
        count+=1
        if depth>MAX_DEPTH or count>MAX_NODES:raise _SchemaError('IR structure limit')
        if type(node) is not dict:raise _SchemaError('IR object required')
        if 'value' in node:
            _keys(node,{'value','unit'})
            if type(node['unit']) is not str or node['unit'] not in UNITS:raise _SchemaError('unknown unit')
            return Literal(_number(node['value']),node['unit'])
        op=node.get('op')
        if type(op) is not str or op not in {*arities,'mean','sum'}:raise _SchemaError('unknown operator')
        _keys(node,{'op','args','option'} if op in {'round','convert'} else {'op','args'})
        args=node['args']
        if type(args) is not list or (not 2<=len(args)<=16 if op in {'mean','sum'} else len(args)!=arities[op]):
            raise _SchemaError('invalid arity')
        option=node.get('option','')
        if type(option) is not str:raise _SchemaError('invalid option')
        return Expr(op,tuple(visit(arg,depth+1) for arg in args),option)
    return visit(raw['expr'],0),raw['unit']


def decode_query(raw):
    _keys(raw,{'root','hops','projection'})
    if type(raw['root']) is not str or not 0<len(raw['root'])<=512:
        raise _SchemaError('invalid query root')
    if type(raw['projection']) is not str or raw['projection'] not in ATTRIBUTES|{'label'}:
        raise _SchemaError('unknown projection')
    if type(raw['hops']) is not list or len(raw['hops'])>MAX_HOPS:raise _SchemaError('invalid hop list')
    hops=[]
    for item in raw['hops']:
        _keys(item,{'relation','direction'})
        if type(item['relation']) is not str or item['relation'] not in RELATIONS:
            raise _SchemaError('unknown relation')
        if item['direction'] not in ('forward','inverse'):raise _SchemaError('unknown direction')
        hops.append(Hop(item['relation'],item['direction']))
    return raw['root'],tuple(hops),raw['projection']


def _ref(source,sha,start,end):return (source,sha,start,end)
def _source_ref(source):return _ref('kb:'+source.document,source.sha256,*source.evidence_span)
def _wire_ref(ref):return {'source':ref[0],'sha256':ref[1],'span':[ref[2],ref[3]]}


class Contract:
    """Trusted admission and one validation path, exposed as the existing Prepared."""
    def __init__(self,task_type,instruction,task_dir=None,*,variant='A'):
        if variant not in ('A','B') or task_type not in ('math','search'):
            raise CompilerRefusal(Outcome('preflight_refused'))
        if type(instruction) is not str or not 0<len(instruction)<=8000:
            raise CompilerRefusal(Outcome('preflight_refused'))
        self.kind,self.instruction,self.variant=task_type,instruction,variant
        self.task_dir=Path(task_dir) if task_dir is not None else None
        try:self.instruction_hash=hashlib.sha256(instruction.encode('utf-8')).hexdigest()
        except UnicodeError as exc:raise CompilerRefusal(Outcome('preflight_refused')) from exc
        instruction_ref=_ref('instruction',self.instruction_hash,0,len(instruction))
        self.allowed={instruction_ref};self.required={instruction_ref}
        self.payload={'type':task_type,'instruction':instruction,'compiler_version':1,'variant':variant}
        try:
            if task_type=='math':
                self.expected=None
                for compiler in (compile_grammar,compile_existing):
                    try:self.expected=compiler(instruction);break
                    except MathIRRefusal:pass
                if self.expected is None:raise MathIRRefusal('unproved mathematical meaning')
                math_verify(instruction,self.expected)
                # Domain/preflight reference remains private, never in the prompt.
                self.reference_value=math_execute(self.expected)
                self.payload['units']=list(UNITS)
                schema='Math: ir={"expr":NODE,"unit":"canonical output unit"}. NODE is {"value":"exact number string","unit":"canonical unit"} or {"op":"allowed op","args":[NODE,...]}; round/convert also require option. Allowed operations: add,sub,mul,div,floordiv,neg,abs,floor,sum,mean,percent,increase_percent,decrease_percent,power,round,convert. Percent nodes take [value,rate]. round option=half_up; convert option=target unit.'
            else:
                if self.task_dir is None:raise GraphRefusal('missing task directory')
                self.snapshot=load_snapshot(self.task_dir);self.graph=Graph(self.snapshot)
                self.query=compile_query(instruction,self.graph)
                self.reference_candidate=graph_execute(self.graph,self.query)
                verify_candidate(instruction,self.reference_candidate,self.snapshot,task_dir=self.task_dir)
                visited={self.query.root}
                for step in self.reference_candidate.path:visited.update((step.subject,step.object))
                for entity in self.snapshot.entities:
                    for source in entity.headings:
                        ref=_source_ref(source);self.allowed.add(ref)
                        if entity.key in visited:self.required.add(ref)
                for fact in self.snapshot.facts:
                    for source in fact.provenance:self.allowed.add(_source_ref(source))
                    if fact.relation=='alias' and fact.subject in visited:
                        self.required.update(_source_ref(s) for s in fact.provenance)
                for fact in [*(f for step in self.reference_candidate.path for f in step.facts),*self.reference_candidate.terminal_facts]:
                    self.required.update(_source_ref(s) for s in fact.provenance)
                self.payload['entities']=[{'key':e.key,'labels':list(e.labels),'aliases':list(e.aliases)} for e in self.snapshot.entities]
                self.payload['facts']=[{'subject_key':f.subject,'relation':f.relation,'object':f.object,
                    'sources':[_wire_ref(_source_ref(s)) for s in f.provenance]} for f in self.snapshot.facts]
                self.payload['snapshot_sha256']=self.snapshot.digest
                schema='Search: ir={"root":"exact entity label or declared alias","hops":[{"relation":"mentor|employer","direction":"forward|inverse"}],"projection":"label|born|founded|headquarters"}. Join fact.subject_key to entity.key; root must be a label/alias, not a key. Never merge same-name profiles or invent facts.'
            if variant=='B' and len(self.required)>MAX_EVIDENCE:raise ValueError('evidence limit')
            self.payload['evidence_catalog']=[_wire_ref(r) for r in sorted(self.allowed)]
            extra=' Include evidence=[{"source":"catalog source","sha256":"catalog hash","span":[start,end]},...], covering instruction, all used facts, headings and aliases.' if variant=='B' else ' Do not include evidence or any other fields.'
            # Reuse the common safety policy without its legacy, unversioned
            # abstention envelope, which conflicts with this strict contract.
            safety=_SYSTEM.replace('return {"status":"unsolvable"}',
                                   'return the versioned unsolvable envelope specified below')
            self.system=safety+'Return {"version":1,"kind":"'+task_type+'","ir":...}.'+schema+extra+' Alternatively return only {"version":1,"kind":"'+task_type+'","status":"unsolvable"}.'
            LLMGateway._messages(self.system,self.payload)  # Same context admission, no HTTP/client/key.
        except (MathIRRefusal,GraphRefusal,LLMError,ValueError,OSError,UnicodeError) as exc:
            raise CompilerRefusal(Outcome('semantic_unverified')) from exc

    def prepared(self):return Prepared(self.system,self.payload,self.validate_plan)

    def assess_content(self,content):
        if type(content) is not str or len(content)>MAX_PLAN_CHARS:
            return Outcome('schema_rejected',schema_valid=False)
        try:plan=strict_json(content)
        except LLMError:return Outcome('json_rejected',json_valid=False,schema_valid=False)
        return self.assess(plan)

    def assess(self,plan):
        try:
            if type(plan) is dict and set(plan)=={'version','kind','status'}:
                if type(plan['version']) is not int or plan['version']!=1 or plan['kind']!=self.kind or plan['status']!='unsolvable':
                    raise _SchemaError('invalid abstention')
                return Outcome('model_abstained',True,True)
            _keys(plan,{'version','kind','ir','evidence'} if self.variant=='B' else {'version','kind','ir'})
            if type(plan['version']) is not int or plan['version']!=1 or plan['kind']!=self.kind:
                raise _SchemaError('invalid version/domain')
            decoded=decode_math(plan['ir']) if self.kind=='math' else decode_query(plan['ir'])
            refs=None
            if self.variant=='B':
                if type(plan['evidence']) is not list or not 1<=len(plan['evidence'])<=MAX_EVIDENCE:
                    raise _SchemaError('invalid evidence list')
                refs=[]
                for item in plan['evidence']:
                    _keys(item,{'source','sha256','span'})
                    span=item['span']
                    if (type(item['source']) is not str or len(item['source'])>512 or type(item['sha256']) is not str
                        or re.fullmatch('[0-9a-f]{64}',item['sha256']) is None or type(span) is not list
                        or len(span)!=2 or any(type(v) is not int for v in span) or not 0<=span[0]<span[1]):
                        raise _SchemaError('invalid evidence reference')
                    refs.append(_ref(item['source'],item['sha256'],*span))
        except _SchemaError:return Outcome('schema_rejected',True,False)
        try:
            if self.kind=='math':
                candidate=MathIR(decoded[0],decoded[1],self.expected.source_sha256,self.expected.parser)
                math_validate(candidate);math_verify(self.instruction,candidate)
            else:
                root,hops,field=decoded
                candidate=Query(self.graph.resolve(root),hops,field)
                if candidate!=self.query:raise GraphRefusal('query changes full-question meaning')
        except (MathIRRefusal,GraphRefusal):return Outcome('semantic_unverified',True,True,False)
        if refs is not None and (len(refs)!=len(set(refs)) or not set(refs)<=self.allowed or not self.required<=set(refs)):
            return Outcome('evidence_rejected',True,True,True,False)
        evidence=True if refs is not None else None
        try:
            if self.kind=='math':
                value=math_execute(candidate);answer=math_serialize(value,candidate)
            else:
                value=graph_execute(self.graph,candidate);answer=value.answer
        except (MathIRRefusal,GraphRefusal):return Outcome('executor_rejected',True,True,True,evidence,True)
        try:
            if self.kind=='math':
                math_verify(self.instruction,candidate)
                if value!=self.reference_value:raise MathIRRefusal('result differs from trusted canonical computation')
            else:verify_candidate(self.instruction,value,self.snapshot,task_dir=self.task_dir)
        except (MathIRRefusal,GraphRefusal):
            return Outcome('verification_rejected',True,True,True,evidence,True,False,answer)
        return Outcome('accepted',True,True,True,evidence,True,True,answer,answer)

    def validate_plan(self,plan):
        outcome=self.assess(plan)
        if outcome.status!='accepted':raise CompilerRefusal(outcome)
        return outcome.answer


class OfflineRoute:
    """Explicit context adapter; keeps real workflow, planner, Prepared and ledger.

    A transport must be a MockTransport. This is NOT durable LIVE one-shot state.
    No CLI/env activation or production default is added.
    """
    def __init__(self,variant='A'):
        if variant not in ('A','B'):raise ValueError('unknown compiler hypothesis')
        self.variant=variant;self.attempted=set();self.events=[]

    def prepare(self,kind,instruction,task_dir=None):
        if kind not in ('math','search'):return legacy_prepare(kind,instruction,task_dir)
        try:return Contract(kind,instruction,task_dir,variant=self.variant).prepared()
        except CompilerRefusal as exc:
            key=str(Path(task_dir).absolute()) if task_dir is not None else 'math-direct'
            self.events.append((key,exc.outcome))
            raise

    async def fallback(self,gateway,task_type,instruction,task_dir=None,*,task_id=None,limits=None):
        if not isinstance(gateway,LLMGateway) or not isinstance(gateway._transport,httpx.MockTransport):
            raise CompilerRefusal(Outcome('live_disabled'))
        if task_type not in ('math','search'):
            return await legacy_fallback(gateway,task_type,instruction,task_dir,task_id=task_id,limits=limits)
        key=task_id or str(Path(task_dir).absolute() if task_dir is not None else 'math-direct')
        if key in self.attempted:raise CompilerRefusal(Outcome('attempt_already_consumed'))
        from contextlib import nullcontext
        with limits.sync() if limits is not None else nullcontext():
            contract=Contract(task_type,instruction,task_dir,variant=self.variant)
        self.attempted.add(key)  # Before any await/transport; ambiguous failure cannot retry in this route.
        try:reply=await gateway.complete(contract.system,contract.payload,task_id=key)
        except LLMError:
            self.events.append((key,Outcome('gateway_rejected')))
            raise
        with limits.sync() if limits is not None else nullcontext():outcome=contract.assess_content(reply.content)
        self.events.append((key,outcome))
        if outcome.status!='accepted':raise CompilerRefusal(outcome)
        return outcome.answer

    @contextmanager
    def installed(self):
        with patch.object(planner,'prepare',self.prepare),patch.object(harness,'solve_fallback',self.fallback):
            yield self
