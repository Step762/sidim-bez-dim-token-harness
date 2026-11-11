"""One synthetic profile for fallback_pilot, reusing its durable primitives.

Real transport requires an explicit --live plus a current private approval.
An approval is an owner attestation, not evidence about a provider.
"""
import argparse
import asyncio
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import time

import httpx
import harness  # Configure the pinned SDK before constructing the shared gateway.
from budget_manager import BudgetManager
from diagnostics.a1_resume import ResumeBlocked, state_directory, write_once
from llm_gateway import LLMError, LLMGateway, strict_json
from safe_files import open_directory, read_text

PROFILE = 'p3_proxy_direct_v1'
MODEL = 'qwen3.6-35b-a3b-noreason'
LIVE_ENDPOINT = 'https://api.neuraldeep.ru/v1/chat/completions'
SYSTEM = 'Return only one JSON object with a string field value containing the arithmetic result.'
PAYLOAD = {'instruction': 'Compute (7 - 3)'}
# OS account home, not HOME/cwd/output: changing clone or HOME must not reset a slot.
STATE_ROOT = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/sidim-bez-dim' / PROFILE
INTENT = 'attempt.json'
RESULT = 'result.json'
ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def contract():
    inputs = [s[1:] for s in (ROOT / '.dockerignore').read_text().splitlines() if s.startswith('!')]
    inputs += ['diagnostics/qwen_profile.py', 'diagnostics/fallback_pilot.py', 'diagnostics/a1_resume.py']
    return {'profile': PROFILE, 'model': MODEL, 'budget': 200, 'max_tokens': 32,
            'timeout': 20, 'max_new_requests': 1, 'input_limit': 1500,
            'live_endpoint_sha256': hashlib.sha256(LIVE_ENDPOINT.encode()).hexdigest(),
            'prompt_sha256': digest([SYSTEM, PAYLOAD]),
            'source_sha256': {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in inputs}}


def _record(name):
    with open_directory(STATE_ROOT) as parent:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ResumeBlocked('unsafe record permissions')
    return strict_json(read_text(STATE_ROOT, name, maximum=16384))


def _unused():
    with open_directory(STATE_ROOT.parent) as parent:
        info = os.fstat(parent)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ResumeBlocked('persistent parent must be private and owned by this user')
    if os.path.lexists(STATE_ROOT):
        with state_directory(root=STATE_ROOT) as parent:
            if os.listdir(parent):
                raise ResumeBlocked('attempt already claimed or state unknown; recover only')


def _history(rows):
    if type(rows) is not list or len(rows) > 100:
        raise ResumeBlocked('explicit complete usage history is required')
    manager = BudgetManager(200)
    for i, row in enumerate(rows):
        if type(row) is not dict or set(row) != {'input_tokens', 'accounted_output_tokens'}:
            raise ResumeBlocked('invalid history')
        key = f'history:{i}'
        manager.reserve(key, key, row['input_tokens'], row['accounted_output_tokens'])
        manager.settle(key, row['input_tokens'], row['accounted_output_tokens'])
    return manager


def _prepare(approval_path, model, transport=None):
    _unused()  # A repeat is denied even with changed parameters/credentials.
    if model != MODEL or BudgetManager.from_env().limit != 200:
        raise ResumeBlocked('explicit reviewed model and BUDGET=200 are required')
    path = Path(approval_path).absolute()
    with open_directory(path.parent) as parent:
        directory = os.fstat(parent)
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (directory.st_uid != os.geteuid() or stat.S_IMODE(directory.st_mode) != 0o700
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise ResumeBlocked('approval must be owner-private, not a public task input')
    approval = strict_json(read_text(path.parent, path.name, maximum=16384))
    flags = {'proxy_confirmed', 'model_confirmed', 'output_cap_confirmed', 'history_complete'}
    fields = flags | {'contract_sha256', 'endpoint_sha256', 'quota_scope_sha256', 'history', 'usage_contract'}
    if (type(approval) is not dict or set(approval) != fields
            or any(approval[k] is not True for k in flags)
            or approval['usage_contract'] != 'noreason_completion'
            or approval['contract_sha256'] != digest(contract())
            or any(type(approval[k]) is not str or re.fullmatch('[0-9a-f]{64}', approval[k]) is None
                   for k in ('endpoint_sha256', 'quota_scope_sha256'))):
        raise ResumeBlocked('Proxy, history and current contract must be explicitly confirmed')
    manager = _history(approval['history'])
    gateway = LLMGateway(os.environ.get('LLM_BASE_URL', ''), os.environ.get('LLM_API_KEY', ''),
                         model=model, manager=manager, max_tokens=32, timeout=20, transport=transport)
    gateway.validate_generation_contract()
    if hashlib.sha256(gateway._endpoint.encode()).hexdigest() != approval['endpoint_sha256']:
        raise ResumeBlocked('endpoint differs from confirmed Proxy')
    estimate = gateway.estimate_input(SYSTEM, PAYLOAD)
    reserve = Decimal(estimate) / 10 + 32
    if estimate > 1500 or reserve > min(manager.available, Decimal('182.0')):
        raise ResumeBlocked('full safe reservation unavailable')
    intent = {'profile': PROFILE, 'version': 1, 'contract_sha256': approval['contract_sha256'],
              'endpoint_sha256': approval['endpoint_sha256'], 'quota_scope_sha256': approval['quota_scope_sha256'],
              'history': approval['history'], 'estimated_input': estimate, 'reservation': str(reserve),
              'request_status': 'may_have_been_sent'}
    return intent, manager, gateway


def preflight(approval_path, *, model):
    intent, manager, gateway = _prepare(approval_path, model)
    return {'profile': PROFILE, 'ready_for_mock': True, 'live_enabled': False,
            'live_endpoint_matches': gateway._endpoint == LIVE_ENDPOINT,
            'estimated_input': intent['estimated_input'], 'reservation': intent['reservation'],
            'spent_before': str(manager.snapshot()['spent']), 'available_before': str(manager.available)}


def _layers(content):
    try:
        value = strict_json(content)
    except LLMError:
        return False, False, False
    schema = type(value) is dict and set(value) == {'value'} and type(value['value']) is str
    return True, schema, schema and value['value'] == '4'


def run_mock(approval_path, *, model, transport):
    """Exercise the full intent/gateway/settlement path, never a real transport."""
    if not isinstance(transport, httpx.MockTransport):
        raise ResumeBlocked('mock route requires MockTransport')
    return _run(approval_path, model=model, transport=transport, live=False)


def run_live(approval_path, *, model, live=False):
    """Future operator invocation only; check/import/environment never enables it."""
    if live is not True:
        raise ResumeBlocked('explicit LIVE invocation is required')
    return _run(approval_path, model=model, transport=None, live=True)


def _run(approval_path, *, model, transport, live):
    intent, manager, gateway = _prepare(approval_path, model, transport)
    if live and gateway._endpoint != LIVE_ENDPOINT:
        raise ResumeBlocked('LIVE endpoint is not the reviewed NeuralDeep API')
    spent_before = manager.snapshot()['spent']
    started = time.monotonic()
    with state_directory(root=STATE_ROOT, create=True) as parent:
        try:
            write_once(parent, INTENT, intent)  # Exclusive claim + file/dir fsync BEFORE complete.
        except FileExistsError:
            raise ResumeBlocked('another process already claimed the attempt') from None
        layers = (None, None, None)
        reply = None

        async def complete_once():
            if live:
                # Construct the zero-retry real transport only AFTER durable intent.
                async with httpx.AsyncHTTPTransport(retries=0) as wire:
                    gateway._transport = wire
                    return await gateway.complete(SYSTEM, PAYLOAD, task_id=PROFILE)
            return await gateway.complete(SYSTEM, PAYLOAD, task_id=PROFILE)

        try:
            reply = asyncio.run(complete_once())
            layers = _layers(reply.content)
        except LLMError:
            pass  # Gateway retains/finalizes the reservation; no retry or error/body logging.
        stats = manager.snapshot()
        confirmed = any(r.task_id == PROFILE and r.state == 'settled' for r in stats['requests'])
        usage = gateway.usage_records[-1] if gateway.usage_records else None
        report = {'profile': PROFILE, 'version': 1, 'intent_sha256': digest(intent),
                  'status': 'completed' if reply and all(layers) and not stats['frozen'] else 'rejected' if confirmed else 'uncertain',
                  'spent': str(stats['spent']), 'reserved': str(stats['reserved']),
                  'new_cost': str(stats['spent'] - spent_before) if confirmed else None,
                  'frozen': stats['frozen'], 'usage_confirmed': confirmed,
                  'prompt_tokens': usage.prompt_tokens if usage else None,
                  'completion_tokens': usage.completion_tokens if usage else None,
                  'reasoning_tokens': usage.reasoning_tokens if usage else None,
                  'json_valid': layers[0], 'schema_valid': layers[1], 'value_correct': layers[2],
                  'seconds': time.monotonic() - started, 'attempts_upper_bound': 1,
                  'live_enabled': live, 'retry_permitted': False}
        write_once(parent, RESULT, report)
    return report


def recover():
    """Read-only summary; a damaged/unknown state never releases the claim."""
    if not os.path.lexists(STATE_ROOT):
        return {'state': 'unclaimed', 'retry_permitted': False, 'live_enabled': False}
    with state_directory(root=STATE_ROOT) as parent:
        names = set(os.listdir(parent))
    if not names:
        return {'state': 'unclaimed', 'retry_permitted': False, 'live_enabled': False}
    blocked = {'state': 'uncertain', 'attempts_upper_bound': 1, 'available_for_new_requests': '0.0',
               'retry_permitted': False, 'live_enabled': False}
    if names != {INTENT, RESULT}:
        return blocked
    try:
        intent, result = _record(INTENT), _record(RESULT)
        # Reconstruct only numeric accounting, never return arbitrary persisted objects.
        expected_intent = {'profile', 'version', 'contract_sha256', 'endpoint_sha256', 'quota_scope_sha256',
                           'history', 'estimated_input', 'reservation', 'request_status'}
        if (type(intent) is not dict or set(intent) != expected_intent or intent['profile'] != PROFILE
                or type(intent['version']) is not int or intent['version'] != 1
                or intent['request_status'] != 'may_have_been_sent'
                or type(intent['estimated_input']) is not int or not 0 <= intent['estimated_input'] <= 1500
                or intent['reservation'] != str(Decimal(intent['estimated_input']) / 10 + 32)
                or type(result) is not dict or result.get('intent_sha256') != digest(intent)
                or result.get('profile') != PROFILE or type(result.get('version')) is not int or result['version'] != 1
                or result.get('reasoning_tokens') not in (None, 0)):
            return blocked
        manager = _history(intent['history'])
        confirmed = result.get('usage_confirmed') is True
        if confirmed:
            manager.reserve(PROFILE, PROFILE, intent['estimated_input'], 32)
            manager.settle(PROFILE, result['prompt_tokens'], result['completion_tokens'])
        else:
            return blocked
        if result.get('new_cost') != str(manager.snapshot()['spent'] - _history(intent['history']).snapshot()['spent']):
            return blocked
        return {'state': 'consumed', 'attempts_upper_bound': 1, 'usage_confirmed': True,
                'spent': str(manager.snapshot()['spent']), 'retry_permitted': False, 'live_enabled': False}
    except (ValueError, OSError, TypeError, KeyError, AttributeError, RecursionError):
        return blocked


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--recover', action='store_true')
    mode.add_argument('--describe', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--model')
    args = parser.parse_args(argv)
    try:
        if args.describe:
            print(json.dumps({'contract': contract(), 'contract_sha256': digest(contract()),
                              'live_enabled': False, 'approval_required': True}, sort_keys=True))
        elif args.recover:
            print(json.dumps(recover(), sort_keys=True))
        else:
            if args.approval is None:
                raise ResumeBlocked('an explicit approval record is required')
            if args.live:
                report = run_live(args.approval, model=args.model, live=True)
                print(json.dumps(report, sort_keys=True))
                return 0 if report['status'] == 'completed' else 1
            print(json.dumps(preflight(args.approval, model=args.model), sort_keys=True))
        return 0
    except (ValueError, OSError, TypeError, httpx.HTTPError):
        print(json.dumps({'ready': False, 'live_enabled': False,
                          'retry_permitted': False,
                          'error': 'preflight/state/configuration rejected; no automatic retry'}))
        return 2
