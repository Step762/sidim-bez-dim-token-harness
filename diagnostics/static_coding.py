"""Opt-in review-only polynomial repairs; never execute or publish task code."""

import argparse
import ast
import copy
import hashlib
import json
from pathlib import Path

from runtime_limits import RuntimeLimits, OperationTimeout
from safe_files import read_text


MAX_SOURCE_BYTES = 64 * 1024
MAX_NODES = 256
CONTRACTS = {
    'add': {(1, 0): 1, (0, 1): 1},
    'subtract': {(1, 0): 1, (0, 1): -1},
    'multiply': {(1, 1): 1},
}


class UnsupportedStatic(ValueError):
    pass


def _polynomial(node, names, depth=0):
    """Static coefficient normalization, not a Python AST interpreter."""
    if depth > 12:
        raise UnsupportedStatic('expression depth limit')
    if isinstance(node, ast.Name) and node.id in names:
        return {(1, 0) if node.id == names[0] else (0, 1): 1}
    if isinstance(node, ast.Constant) and type(node.value) is int and node.value.bit_length() <= 128:
        return {(0, 0): node.value} if node.value else {}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        factor = -1 if isinstance(node.op, ast.USub) else 1
        return {key: factor * value for key, value in _polynomial(node.operand, names, depth + 1).items()}
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        raise UnsupportedStatic('not a supported integer polynomial')
    left = _polynomial(node.left, names, depth + 1)
    right = _polynomial(node.right, names, depth + 1)
    result = dict(left) if not isinstance(node.op, ast.Mult) else {}
    if isinstance(node.op, ast.Mult):
        for (x, y), a in left.items():
            for (u, v), b in right.items():
                key = x + u, y + v
                if sum(key) > 2:
                    raise UnsupportedStatic('polynomial degree limit')
                result[key] = result.get(key, 0) + a * b
    else:
        sign = -1 if isinstance(node.op, ast.Sub) else 1
        for key, value in right.items():
            result[key] = result.get(key, 0) + sign * value
    if any(abs(value).bit_length() > 256 for value in result.values()):
        raise UnsupportedStatic('coefficient limit')
    return {key: value for key, value in result.items() if value}


def _closed_module(source):
    if not isinstance(source, str) or len(source.encode('utf-8')) > MAX_SOURCE_BYTES:
        raise UnsupportedStatic('source size limit')
    tree = ast.parse(source)
    if len(list(ast.walk(tree))) > MAX_NODES or not tree.body:
        raise UnsupportedStatic('module size limit')
    seen = set()
    for fn in tree.body:
        if (not isinstance(fn, ast.FunctionDef) or fn.name.startswith('_') or fn.name in seen
            or fn.decorator_list or fn.returns is not None or getattr(fn, 'type_params', [])
            or fn.type_comment is not None or len(fn.body) != 1 or not isinstance(fn.body[0], ast.Return)
            or fn.args.posonlyargs or fn.args.kwonlyargs or fn.args.defaults or fn.args.kw_defaults
            or fn.args.vararg is not None or fn.args.kwarg is not None or len(fn.args.args) != 2
            or any(a.annotation is not None or a.type_comment is not None for a in fn.args.args)):
            raise UnsupportedStatic('module is outside the closed two-integer-function subset')
        seen.add(fn.name)
        names = [a.arg for a in fn.args.args]
        if len(set(names)) != 2:
            raise UnsupportedStatic('duplicate argument')
        # Division may be the faulty operator, but is never accepted as proof.
        allowed = (ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UnaryOp,
                   ast.UAdd, ast.USub, ast.Name, ast.Load, ast.Constant)
        for node in ast.walk(fn.body[0].value):
            if (not isinstance(node, allowed)
                or isinstance(node, ast.Name) and node.id not in names
                or isinstance(node, ast.Constant) and (type(node.value) is not int or node.value.bit_length() > 128)):
                raise UnsupportedStatic('expression contains unsupported constructs')
    return tree


def review_source(source, *, experimental=False, function=None, contract=None):
    if experimental is not True:
        raise UnsupportedStatic('explicit experimental mode is required')
    report = {'mode':'experimental_static_coding','production_enabled':False,
              'task_code_executed':False,'workspace_modified':False,'answer_created':False,
              'contest_solution_confirmed':False,'independently_verified':False,
              'domain':'two exact built-in Python int arguments; no other input types',
              'contract_origin':'external reviewer argument, never inferred from task tests or function name'}
    try:
        tree = _closed_module(source)
    except SyntaxError:
        return {**report, 'status':'syntax_invalid','syntax_valid':False}
    except (UnsupportedStatic, RecursionError, ValueError, TypeError):
        return {**report, 'status':'unsupported_source','syntax_valid':None}
    report.update(source_sha256=hashlib.sha256(source.encode()).hexdigest(), syntax_valid=True,
                  functions=[fn.name for fn in tree.body])
    if contract is None:
        return {**report,'status':'missing_independent_contract','shape_supported':True}
    if contract not in CONTRACTS or function not in report['functions']:
        return {**report,'status':'unsupported_contract'}
    fn = next(fn for fn in tree.body if fn.name == function)
    names = [a.arg for a in fn.args.args]
    target = CONTRACTS[contract]
    try:
        if _polynomial(fn.body[0].value, names) == target:
            return {**report,'status':'already_conforms','contract':contract,'contract_proved':True}
    except UnsupportedStatic:
        pass
    candidates = {}
    operations = [n for n in ast.walk(tree) if isinstance(n, ast.BinOp)]
    for position, original in enumerate(operations):
        for replacement in (ast.Add, ast.Sub, ast.Mult):
            if isinstance(original.op, replacement):
                continue
            proposed = copy.deepcopy(tree)
            nodes = [n for n in ast.walk(proposed) if isinstance(n, ast.BinOp)]
            nodes[position].op = replacement()
            changed = next(n for n in proposed.body if n.name == function)
            try:
                if _polynomial(changed.body[0].value, names) != target:
                    continue
            except UnsupportedStatic:
                continue
            # Reject changes outside the named function and require one edit.
            if ast.dump(changed) == ast.dump(fn):
                continue
            if any(ast.dump(a) != ast.dump(b) for a, b in zip(tree.body, proposed.body) if a.name != function):
                continue
            candidates[ast.dump(proposed)] = ast.unparse(proposed) + '\n'
    if len(candidates) != 1:
        return {**report,'status':'ambiguous_or_no_repair','candidate_count':len(candidates)}
    return {**report,'status':'proposal_proved_under_external_contract','contract':contract,
            'contract_proved':True,'proof':'exact polynomial coefficient identity in the stated integer domain',
            'proposal_source':next(iter(candidates.values())),
            'note':'Not independent contest verification. No supplied tests were read or run; no DONE is issued.'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experimental-static-coding',action='store_true')
    parser.add_argument('--task-dir',type=Path,required=True)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--function')
    parser.add_argument('--contract',choices=tuple(CONTRACTS))
    args=parser.parse_args(argv)
    if not args.experimental_static_coding:
        parser.error('explicit experimental mode required; production never imports this module')
    if args.source.suffix != '.py':
        parser.error('source must be a Python file within this task workspace')
    try:
        with RuntimeLimits(5,2).sync():
            source=read_text(args.task_dir,Path('workspace')/args.source,maximum=MAX_SOURCE_BYTES)
            report=review_source(source,experimental=True,function=args.function,contract=args.contract)
    except (OSError,ValueError,TimeoutError,OperationTimeout,RecursionError):
        parser.error('source could not be reviewed safely')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
