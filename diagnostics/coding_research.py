"""Review one local repair against a supplied exact-int contract."""

import argparse
import ast
from dataclasses import dataclass, replace
import hashlib
import io
import json
from pathlib import Path
import re
import tokenize

from diagnostics import static_coding
from runtime_limits import OperationTimeout, RuntimeLimits
from safe_files import read_text


MAX_SOURCE_BYTES = 64 * 1024
MAX_NODES = 256
EXPECTED_OPERATORS = {"add": ast.Add, "subtract": ast.Sub, "multiply": ast.Mult}
CONTRACTS = (*EXPECTED_OPERATORS, "at_least", "scale")
ARITHMETIC_OPERATORS = (*EXPECTED_OPERATORS.values(), ast.Div)
OPERATOR_TEXT = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Gt: ">", ast.GtE: ">=",
}
REPLACEMENT_TEXT = ("+", "-", "*", ">=")
DOMAIN = "two exact built-in int arguments, assuming sufficient resources"
UNARY_DOMAIN = "one exact built-in int argument, assuming sufficient resources"
PLAIN_HEADER = re.compile(r"def [A-Za-z_][A-Za-z0-9_]*\([ \t]*[A-Za-z_][A-Za-z0-9_]*[ \t]*\)(?=[ \t]*(?:#.*)?$)")


class UnsupportedSource(ValueError):
    """The source is outside the verifier's closed grammar."""


@dataclass(frozen=True)
class OperatorReturn:
    function: str
    parameters: tuple[str, ...]
    operator: type[ast.operator] | type[ast.cmpop]
    literal: int | None = None


def parse_bounded_module(source: str) -> ast.Module:
    """Parse UTF-8 text within the diagnostic's byte and AST limits."""
    if len(source) > MAX_SOURCE_BYTES:
        raise UnsupportedSource("source byte limit")
    encoded = source.encode("utf-8")
    if len(encoded) > MAX_SOURCE_BYTES:
        raise UnsupportedSource("source byte limit")
    encoding, _ = tokenize.detect_encoding(io.BytesIO(encoded).readline)
    if encoding != "utf-8":
        raise UnsupportedSource("expected UTF-8 source without a byte order mark")
    tree = ast.parse(encoded, type_comments=True)
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise UnsupportedSource("AST node limit")
    return tree


def parse_int_literal(node: ast.expr) -> int:
    """Read a signed integer literal as data."""
    match node:
        case ast.UnaryOp(op=ast.USub(), operand=ast.Constant(value=value)) if type(value) is int:
            value = -value
        case ast.Constant(value=value) if type(value) is int:
            pass
        case _:
            raise UnsupportedSource("expected a literal integer")
    if value.bit_length() > 128:
        raise UnsupportedSource("integer bit limit")
    return value


def parse_operator_return(source: str) -> OperatorReturn:
    """Recognize binary arithmetic, a threshold comparison or scaling by a literal."""
    tree = parse_bounded_module(source)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef) or tree.type_ignores:
        raise UnsupportedSource("expected one plain function")

    function = tree.body[0]
    args = function.args
    if (
        function.name.startswith("_")
        or function.decorator_list
        or function.returns is not None
        or function.type_comment is not None
        or getattr(function, "type_params", ())
        or args.posonlyargs
        or args.kwonlyargs
        or args.vararg is not None
        or args.kwarg is not None
        or args.defaults
        or len(args.args) not in (1, 2)
        or any(arg.annotation is not None or arg.type_comment is not None for arg in args.args)
    ):
        raise UnsupportedSource("expected one or two plain parameters")
    parameters = tuple(arg.arg for arg in args.args)
    if (
        len(set(parameters)) != len(parameters)
        or len(function.body) != 1
        or not isinstance(function.body[0], ast.Return)
    ):
        raise UnsupportedSource("expected distinct parameters and one return")

    match function.body[0].value:
        case ast.BinOp(
            left=ast.Name(id=left), op=op, right=ast.Name(id=right),
        ) if parameters == (left, right) and type(op) in ARITHMETIC_OPERATORS:
            return OperatorReturn(function.name, parameters, type(op))
        case ast.Compare(
            left=ast.Name(id=name), ops=[ast.Gt() | ast.GtE() as op], comparators=[limit],
        ) if parameters == (name,):
            return OperatorReturn(function.name, parameters, type(op), parse_int_literal(limit))
        case ast.BinOp(left=ast.Name(id=name), op=ast.Mult(), right=factor) if parameters == (name,):
            return OperatorReturn(function.name, parameters, ast.Mult, parse_int_literal(factor))
        case _:
            raise UnsupportedSource("expected supported arithmetic or a comparison with a literal threshold")


def _contract_operator(contract: str | None, threshold: int | None):
    if contract == "at_least":
        if type(threshold) is int and threshold.bit_length() <= 128:
            return ast.GtE
        return None
    return EXPECTED_OPERATORS.get(contract) if threshold is None else None


def verify_candidate(
    original: str, candidate: str, *, function: str, contract: str, threshold: int | None = None,
) -> bool:
    """Prove an operator-only edit on the declared exact-int domain."""
    expected = _contract_operator(contract, threshold)
    if expected is None:
        return False
    try:
        before = parse_operator_return(original)
        after = parse_operator_return(candidate)
    except (SyntaxError, ValueError, RecursionError):
        return False
    # The approved arithmetic operator implements the contract directly. For a
    # threshold, >= is false below the fixed limit and true at or above it.
    return (
        before.function == function
        and before.literal == threshold
        and (contract != "at_least" or before.operator is ast.Gt)
        and before.operator is not expected
        and replace(before, operator=expected) == after
    )


def _operator_patch(source: str, before, after) -> dict:
    old, new = OPERATOR_TEXT[before], OPERATOR_TEXT[after]
    tokens = [
        token for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.OP and token.string == old
    ]
    if len(tokens) != 1:
        raise UnsupportedSource("expected a unique operator token")
    line, column = tokens[0].start
    return {"line": line, "column": column + 1, "old": old, "new": new}


def apply_operator_edit(source: str, edit: dict) -> str:
    """Apply a structured operator edit in memory, rejecting stale coordinates."""
    line, column = edit.get("line"), edit.get("column")
    old, new = edit.get("old"), edit.get("new")
    if (
        type(line) is not int or type(column) is not int or line < 1 or column < 1
        or old not in OPERATOR_TEXT.values() or new not in REPLACEMENT_TEXT or old == new
    ):
        raise ValueError("invalid edit coordinates or operator")
    lines = source.split("\n")
    start = column - 1
    end = start + len(old)
    if line > len(lines) or lines[line - 1][start:end] != old:
        raise ValueError("edit does not match the source")
    lines[line - 1] = lines[line - 1][:start] + new + lines[line - 1][end:]
    return "\n".join(lines)


def source_digest(source: str) -> str:
    """Bind a patch to the UTF-8 text read by the diagnostic."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _missing_colon_edit(source: str) -> dict:
    header = PLAIN_HEADER.match(source.partition("\n")[0])
    if header is None:
        raise UnsupportedSource("expected a one-line plain function header missing its colon")
    return {"line": 1, "column": header.end() + 1, "old": "", "new": ":"}


def apply_missing_colon(source: str, edit: dict) -> str:
    """Insert a colon only at the end of a recognized incomplete header."""
    expected = _missing_colon_edit(source)
    if (
        type(edit.get("line")) is not int
        or type(edit.get("column")) is not int
        or any(edit.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("edit does not restore the function header")
    offset = expected["column"] - 1
    return source[:offset] + ":" + source[offset:]


def _scale_proposal(source: str, function: str, factor: int) -> dict:
    try:
        parsed = parse_operator_return(source)
    except SyntaxError:
        patch = _missing_colon_edit(source)
        parsed = parse_operator_return(apply_missing_colon(source, patch))
    else:
        patch = None
    if parsed.function != function or parsed.operator is not ast.Mult or parsed.literal != factor:
        return {"status": "contract_mismatch"}
    if patch is None:
        return {"status": "already_conforms"}
    return {"patch": patch}


def _operator_proposal(source: str, function: str, contract: str, threshold: int | None, expected) -> dict:
    before = parse_operator_return(source)
    if before.function != function:
        return {"status": "function_mismatch"}
    if before.literal != threshold or (contract == "at_least" and before.operator not in (ast.Gt, ast.GtE)):
        return {"status": "contract_mismatch"}
    if before.operator is expected:
        return {"status": "already_conforms"}
    if contract != "at_least":
        proposal = static_coding.review_source(source, experimental=True, function=function, contract=contract)
        candidate = proposal.get("proposal_source")
        if not isinstance(candidate, str):
            return {"status": "no_candidate"}
        if not verify_candidate(source, candidate, function=function, contract=contract):
            return {"status": "candidate_rejected"}
    patch = _operator_patch(source, before.operator, expected)
    candidate = apply_operator_edit(source, patch)
    if not verify_candidate(source, candidate, function=function, contract=contract, threshold=threshold):
        return {"status": "candidate_rejected"}
    return {"patch": patch}


def review_task(
    task_dir: Path, source: Path, *, function: str | None, contract: str | None,
    threshold: int | None = None, factor: int | None = None,
) -> dict:
    """Read one workspace file and return a one-token edit or a reason to skip."""
    report = {
        "function": function,
        "contract": contract,
        "domain": UNARY_DOMAIN if contract in ("at_least", "scale") else DOMAIN,
        "independently_verified_under_contract": False,
        "contest_solution_confirmed": False,
    }
    if contract == "at_least":
        report["threshold"] = threshold
    if contract == "scale":
        report["factor"] = factor
        valid_contract = type(factor) is int and factor.bit_length() <= 128 and threshold is None
    else:
        expected = _contract_operator(contract, threshold)
        valid_contract = expected is not None and factor is None
    if not function or not valid_contract:
        return {**report, "status": "missing_or_unsupported_contract"}
    if source.suffix != ".py" or source != Path(source.name):
        return {**report, "status": "invalid_source_path"}

    try:
        with RuntimeLimits(5, 2).sync():
            original = read_text(task_dir, Path("workspace") / source, maximum=MAX_SOURCE_BYTES)
            if contract == "scale":
                proposal = _scale_proposal(original, function, factor)
            else:
                proposal = _operator_proposal(original, function, contract, threshold, expected)
    except SyntaxError:
        return {**report, "status": "syntax_invalid"}
    except (UnsupportedSource, tokenize.TokenError) as exc:
        return {**report, "status": "unsupported_source", "reason": str(exc)}
    except (TimeoutError, OperationTimeout, RecursionError):
        return {**report, "status": "analysis_limit"}
    except (OSError, ValueError):
        return {**report, "status": "source_unavailable"}

    if "patch" not in proposal:
        return {**report, **proposal}
    return {
        **report,
        "status": "verified_under_supplied_contract",
        "independently_verified_under_contract": True,
        "unverified": ["the supplied contract is complete and authoritative for this task"],
        "patch": {
            "file": f"workspace/{source.name}",
            "source_sha256": source_digest(original),
            **proposal["patch"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experimental-static-coding", action="store_true", required=True)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--contract", choices=CONTRACTS, required=True)
    parser.add_argument("--threshold", type=int)
    parser.add_argument("--factor", type=int)
    args = parser.parse_args()
    report = review_task(
        args.task_dir, args.source, function=args.function, contract=args.contract,
        threshold=args.threshold, factor=args.factor,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
