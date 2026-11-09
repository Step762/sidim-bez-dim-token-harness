"""Independent offline history contract. Never reads private receipts or keys.

The LIVE flag below exercises the receipt schema, not a real network claim.
Only tests bind these values; ordinary diagnostic entrypoints keep their pins.
"""
from contextlib import ExitStack
from decimal import Decimal
import hashlib
import json
from unittest.mock import patch


BASE_URL = 'https://synthetic-a1.invalid/v1'
MODEL = 'qwen3.6-35b-a3b-noreason'  # Public protocol model, not a provider identity.
ACK_USAGE = (17, 18)
MATH_USAGE = (29, 9)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2)+'\n').encode()


RUNTIME = {'run_agent.py':digest('fictional runtime alpha'),
           'harness.py':digest('fictional runtime beta')}
ACK_DIAGNOSTICS = {name:digest('fictional ack '+name)
                   for name in ('cycle_a.py','selection.py','trace.py')}
MATH_DIAGNOSTICS = {name:digest('fictional math '+name)
                    for name in ('a1_resume.py','cycle_a.py','selection.py','trace.py')}
PARAMETERS = digest('fictional probe parameters version one')


def cost(usage):
    return Decimal(usage[0])/10+usage[1]


def budget(spent, limit=600):
    return {'limit':limit, 'spent':str(spent), 'reserved':'0.0',
            'available':str(Decimal(limit)-spent), 'frozen':False, 'uncertain_calls':0}


def fixtures():
    """Construct from public schema and invented values, not from a LIVE file."""
    ack_cost = cost(ACK_USAGE)
    ack = {'synthetic_fixture':True, 'mode':'A1', 'live':True, 'model':MODEL,
           'request_attempts':1, 'contract_observed':False,
           'endpoint_digest':digest(BASE_URL), 'runtime_sha256':RUNTIME,
           'diagnostic_sha256':ACK_DIAGNOSTICS, 'budget':budget(ack_cost),
           'probes':[{'id':'a1_ack','request_sent':True,'usage_state':'confirmed',
                      'prompt_tokens':ACK_USAGE[0],'completion_tokens':ACK_USAGE[1],
                      'max_tokens':32,'finish_reason':'stop'}]}
    source = hashlib.sha256(encode(ack)).hexdigest()
    math = {'synthetic_fixture':True, 'mode':'A1_resume','live':True,'model':MODEL,
            'source_sha256':source,'endpoint_digest':digest(BASE_URL),
            'runtime_sha256':RUNTIME,'diagnostic_sha256':MATH_DIAGNOSTICS,
            'parameters_sha256':PARAMETERS,'probe_id':'a1_math',
            'historical_attempts':1,'new_transport_attempts':1,'series_attempts_upper_bound':2,
            'safety_stop':False,'contract_observed':False,'retry_permitted':False,
            'budget':budget(ack_cost+cost(MATH_USAGE)),
            'new_probe':{'contract_observed':True,'usage_state':'confirmed',
                         'prompt_tokens':MATH_USAGE[0],'completion_tokens':MATH_USAGE[1],
                         'confirmed_cost':str(cost(MATH_USAGE)),
                         'checks':{'http_api':True,'inner_json':True,'schema':True,'value':True}}}
    intent = {'synthetic_fixture':True, 'mode':'A1_resume_intent','version':1,
              'source_sha256':source,'probe_id':'a1_math','model':MODEL,'max_tokens':64,
              'endpoint_digest':digest(BASE_URL),'runtime_sha256':RUNTIME,
              'diagnostic_sha256':MATH_DIAGNOSTICS,'parameters_sha256':PARAMETERS,
              'historical_confirmed_cost':str(ack_cost),'budget_before':budget(ack_cost),
              'estimated_input':900,'pending_reserve':'154.0',
              'series_attempts_upper_bound':2,'request_status':'may_have_been_sent'}
    return {'a1_ack_synthetic.json':ack,'a1_math_synthetic.json':math,
            'a1_math_intent_synthetic.json':intent}


def patch_history():
    """Bind test pins, not validators. No CLI/environment flag enables this."""
    from diagnostics import a1_resume as resume, fallback_pilot as pilot
    values = fixtures()
    source = hashlib.sha256(encode(values['a1_ack_synthetic.json'])).hexdigest()
    stack = ExitStack()
    settings = [(resume, {'SOURCE_SHA256':source,'ACK_USAGE':ACK_USAGE,'BASE_URL':BASE_URL,
                          'LEGACY_DIAGNOSTICS':ACK_DIAGNOSTICS,'PARAMETERS_SHA256':PARAMETERS,
                          'INTENT_NAME':source+'.a1_math.intent.json',
                          'RESULT_NAME':source+'.a1_math.result.json'}),
                (pilot, {'MATH_USAGE':MATH_USAGE,'HISTORY_DIAGNOSTICS':MATH_DIAGNOSTICS,
                         'MATH_RESULT_SHA':hashlib.sha256(encode(values['a1_math_synthetic.json'])).hexdigest(),
                         'MATH_INTENT_SHA':hashlib.sha256(encode(values['a1_math_intent_synthetic.json'])).hexdigest()})]
    for module, attributes in settings:
        for name,value in attributes.items():stack.enter_context(patch.object(module,name,value))
    stack.enter_context(patch.object(resume,'runtime_fingerprint',return_value=RUNTIME))
    stack.enter_context(patch.object(pilot,'runtime_fingerprint',return_value=RUNTIME))
    stack.enter_context(patch.object(resume,'diagnostic_fingerprint',return_value=ACK_DIAGNOSTICS))
    stack.enter_context(patch.object(resume,'parameters_fingerprint',return_value=PARAMETERS))
    return stack
