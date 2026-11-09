"""Review one operator repair against a supplied exact-int contract."""

import argparse
import ast
from dataclasses import dataclass, replace
import io
import json
from pathlib import Path
import tokenize

from diagnostics import static_coding
from runtime_limits import OperationTimeout, RuntimeLimits
from safe_files import read_text


MAX_SOURCE_BYTES = 64 * 1024
MAX_NODES = 256
EXPECTED_OPERATORS = {"add": ast.Add, "subtract": ast.Sub, "multiply": ast.Mult}
ARITHMETIC_OPERATORS = (*EXPECTED_OPERATORS.values(), ast.Div)
OPERATOR_TEXT = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Gt: ">", ast.GtE: ">=",
}
REPLACEMENT_TEXT = ("+", "-", "*", ">=")
DOMAIN = "two exact built-in int arguments, assuming sufficient resources"
THRESHOLD_DOMAIN = "one exact built-in int argument, assuming sufficient resources"


class UnsupportedSource(ValueError):
    """The source is outside the verifier's closed grammar."""


@dataclass(frozen=True)
class OperatorReturn:
    function: str
    parameters: tuple[str, ...]
    operator: type[ast.operator] | type[ast.cmpop]
    threshold: int | None = None


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
    """Recognize a binary arithmetic return or a unary threshold comparison."""
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
        case _:
            raise UnsupportedSource("expected one arithmetic operator or a comparison with a literal threshold")


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
        and before.threshold == threshold
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


def review_task(
    task_dir: Path, source: Path, *, function: str | None, contract: str | None,
    threshold: int | None = None,
) -> dict:
    """Read one workspace file and return a one-token edit or a reason to skip."""
    report = {
        "function": function,
        "contract": contract,
        "domain": THRESHOLD_DOMAIN if contract == "at_least" else DOMAIN,
        "independently_verified_under_contract": False,
        "contest_solution_confirmed": False,
    }
    if contract == "at_least":
        report["threshold"] = threshold
    expected = _contract_operator(contract, threshold)
    if not function or expected is None:
        return {**report, "status": "missing_or_unsupported_contract"}
    if source.suffix != ".py" or source != Path(source.name):
        return {**report, "status": "invalid_source_path"}

    try:
        with RuntimeLimits(5, 2).sync():
            original = read_text(task_dir, Path("workspace") / source, maximum=MAX_SOURCE_BYTES)
            before = parse_operator_return(original)
            if before.function != function:
                return {**report, "status": "function_mismatch"}
            if before.threshold != threshold:
                return {**report, "status": "contract_mismatch"}
            if before.operator is expected:
                return {**report, "status": "already_conforms"}
            if contract != "at_least":
                proposal = static_coding.review_source(
                    original, experimental=True, function=function, contract=contract,
                )
                candidate = proposal.get("proposal_source")
                if not isinstance(candidate, str):
                    return {**report, "status": "no_candidate"}
                if not verify_candidate(original, candidate, function=function, contract=contract):
                    return {**report, "status": "candidate_rejected"}
            patch = _operator_patch(original, before.operator, expected)
            candidate = apply_operator_edit(original, patch)
            if not verify_candidate(original, candidate, function=function, contract=contract, threshold=threshold):
                return {**report, "status": "candidate_rejected"}
    except SyntaxError:
        return {**report, "status": "syntax_invalid"}
    except (UnsupportedSource, tokenize.TokenError) as exc:
        return {**report, "status": "unsupported_source", "reason": str(exc)}
    except (TimeoutError, OperationTimeout, RecursionError):
        return {**report, "status": "analysis_limit"}
    except (OSError, ValueError):
        return {**report, "status": "source_unavailable"}

    return {
        **report,
        "status": "verified_under_supplied_contract",
        "independently_verified_under_contract": True,
        "unverified": ["the supplied contract is complete and authoritative for this task"],
        "patch": {"file": f"workspace/{source.name}", **patch},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experimental-static-coding", action="store_true", required=True)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--contract", choices=(*EXPECTED_OPERATORS, "at_least"), required=True)
    parser.add_argument("--threshold", type=int)
    args = parser.parse_args()
    report = review_task(
        args.task_dir, args.source, function=args.function, contract=args.contract, threshold=args.threshold,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
