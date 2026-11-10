"""Review one local repair against a supplied exact-int contract."""

import argparse
import ast
from dataclasses import dataclass, replace
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tokenize

from coding_solver import _INSTRUCTION
from diagnostics import static_coding
from runtime_limits import OperationTimeout, RuntimeLimits
from safe_files import TASK_JSON_BYTES, directory_guards, identity, open_directory, read_text


MAX_SOURCE_BYTES = 64 * 1024
MAX_NODES = 256
EXPECTED_OPERATORS = {"add": ast.Add, "subtract": ast.Sub, "multiply": ast.Mult}
CONTRACTS = (*EXPECTED_OPERATORS, "at_least", "remainder_equals", "scale")
ARITHMETIC_OPERATORS = (*EXPECTED_OPERATORS.values(), ast.Div)
PREDICATE_EDITS = {ast.Gt: ast.GtE, ast.NotEq: ast.Eq}
OPERATOR_TEXT = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Gt: ">", ast.GtE: ">=",
    ast.NotEq: "!=", ast.Eq: "==",
}
REPLACEMENT_TEXT = ("+", "-", "*", ">=", "==")
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
    modulus: int | None = None


def parse_review_json(text: str | bytes) -> object:
    """Read diagnostic JSON without silently choosing among conflicting keys."""
    def unique(pairs):
        fields = {}
        for key, value in pairs:
            if key in fields:
                raise ValueError("duplicate JSON key")
            fields[key] = value
        return fields

    def reject_constant(_):
        raise ValueError("non-finite JSON constant")

    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return value

    try:
        return json.loads(
            text, object_pairs_hook=unique, parse_constant=reject_constant, parse_float=finite_float,
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the parser limit") from exc


def supports_task_instruction(instruction: object, source: str, *, test: str | None = None) -> bool:
    if not isinstance(instruction, str):
        return False
    parsed = _INSTRUCTION.fullmatch(" ".join(instruction.split()))
    return (
        parsed is not None
        and parsed["source"] == source
        and parsed["test"] != source
        and (test is None or parsed["test"] == test)
    )


def read_task_declaration(task_dir: Path) -> str | None:
    try:
        return read_text(task_dir, "task.json", maximum=TASK_JSON_BYTES)
    except FileNotFoundError:
        # Standalone synthetic fixtures can consist of workspace files only.
        return None


def task_allows_review(declaration: str | None, source: str, *, test: str | None = None) -> bool:
    if declaration is None:
        return True
    data = parse_review_json(declaration)
    return (
        isinstance(data, dict)
        and data.get("task_type") == "coding"
        and supports_task_instruction(data.get("instruction"), source, test=test)
    )


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


def _valid_remainder(modulus: int | None, residue: int | None) -> bool:
    return (
        type(modulus) is int and type(residue) is int
        and modulus.bit_length() <= 128 and 0 <= residue < modulus
    )


def parse_operator_return(source: str) -> OperatorReturn:
    """Recognize a supported arithmetic return or one bounded integer predicate."""
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
    # CPython rejects this binding after ast.parse has already accepted it.
    if "__debug__" in parameters:
        raise UnsupportedSource("cannot bind __debug__ as a parameter")
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
        case ast.Compare(
            left=ast.BinOp(left=ast.Name(id=name), op=ast.Mod(), right=divisor),
            ops=[ast.NotEq() | ast.Eq() as op], comparators=[remainder],
        ) if parameters == (name,):
            modulus, residue = parse_int_literal(divisor), parse_int_literal(remainder)
            if not _valid_remainder(modulus, residue):
                raise UnsupportedSource("expected a positive modulus and a canonical residue")
            return OperatorReturn(function.name, parameters, type(op), residue, modulus)
        case ast.BinOp(left=ast.Name(id=name), op=ast.Mult(), right=factor) if parameters == (name,):
            return OperatorReturn(function.name, parameters, ast.Mult, parse_int_literal(factor))
        case _:
            raise UnsupportedSource("expected supported arithmetic or a comparison with a literal threshold")


def _contract_operator(
    contract: str | None, threshold: int | None, modulus: int | None, residue: int | None,
):
    if contract == "remainder_equals":
        return ast.Eq if threshold is None and _valid_remainder(modulus, residue) else None
    if modulus is not None or residue is not None:
        return None
    if contract == "at_least":
        if type(threshold) is int and threshold.bit_length() <= 128:
            return ast.GtE
        return None
    return EXPECTED_OPERATORS.get(contract) if threshold is None else None


def verify_candidate(
    original: str, candidate: str, *, function: str, contract: str, threshold: int | None = None,
    modulus: int | None = None, residue: int | None = None,
) -> bool:
    """Prove an operator-only edit on the declared exact-int domain."""
    expected = _contract_operator(contract, threshold, modulus, residue)
    if expected is None:
        return False
    try:
        before = parse_operator_return(original)
        after = parse_operator_return(candidate)
    except (SyntaxError, ValueError, RecursionError):
        return False
    # For exact ints the operator implements the specified comparison directly.
    # A remainder contract preserves the positive divisor and canonical residue.
    return (
        before.function == function
        and before.literal == (residue if modulus is not None else threshold)
        and before.modulus == modulus
        and (before.literal is None or PREDICATE_EDITS.get(before.operator) is expected)
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


def _operator_proposal(
    source: str, function: str, contract: str, expected, *,
    threshold: int | None, modulus: int | None, residue: int | None,
) -> dict:
    before = parse_operator_return(source)
    if before.function != function:
        return {"status": "function_mismatch"}
    literal = residue if modulus is not None else threshold
    if before.literal != literal or before.modulus != modulus:
        return {"status": "contract_mismatch"}
    if before.operator is expected:
        return {"status": "already_conforms"}
    if literal is not None:
        if PREDICATE_EDITS.get(before.operator) is not expected:
            return {"status": "contract_mismatch"}
    else:
        proposal = static_coding.review_source(source, experimental=True, function=function, contract=contract)
        candidate = proposal.get("proposal_source")
        if not isinstance(candidate, str):
            return {"status": "no_candidate"}
        if not verify_candidate(source, candidate, function=function, contract=contract):
            return {"status": "candidate_rejected"}
    patch = _operator_patch(source, before.operator, expected)
    candidate = apply_operator_edit(source, patch)
    if not verify_candidate(
        source, candidate, function=function, contract=contract, threshold=threshold,
        modulus=modulus, residue=residue,
    ):
        return {"status": "candidate_rejected"}
    return {"patch": patch}


def review_task(
    task_dir: Path, source: Path, *, function: str | None, contract: str | None,
    threshold: int | None = None, factor: int | None = None,
    modulus: int | None = None, residue: int | None = None,
) -> dict:
    """Read one workspace file and return a one-token edit or a reason to skip."""
    report = {
        "function": function,
        "contract": contract,
        "domain": UNARY_DOMAIN if contract in ("at_least", "remainder_equals", "scale") else DOMAIN,
        "independently_verified_under_contract": False,
        "contest_solution_confirmed": False,
    }
    if contract == "at_least":
        report["threshold"] = threshold
    if contract == "remainder_equals":
        report.update(modulus=modulus, residue=residue)
    if contract == "scale":
        report["factor"] = factor
        valid_contract = (
            type(factor) is int and factor.bit_length() <= 128
            and threshold is None and modulus is None and residue is None
        )
    else:
        expected = _contract_operator(contract, threshold, modulus, residue)
        valid_contract = expected is not None and factor is None
    if not function or not valid_contract:
        return {**report, "status": "missing_or_unsupported_contract"}
    if source.suffix != ".py" or source != Path(source.name):
        return {**report, "status": "invalid_source_path"}

    try:
        task_dir = task_dir.absolute()
        with RuntimeLimits(5, 2).sync(), open_directory(task_dir) as directory:
            with directory_guards({task_dir: identity(os.fstat(directory))}):
                declaration = read_task_declaration(task_dir)
                if not task_allows_review(declaration, source.name):
                    return {**report, "status": "unsupported_task_instruction"}
                original = read_text(task_dir, Path("workspace") / source, maximum=MAX_SOURCE_BYTES)
                if contract == "scale":
                    proposal = _scale_proposal(original, function, factor)
                else:
                    proposal = _operator_proposal(
                        original, function, contract, expected, threshold=threshold, modulus=modulus, residue=residue,
                    )
                if "patch" in proposal and read_task_declaration(task_dir) != declaration:
                    return {**report, "status": "task_declaration_changed"}
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
    patch = {
        "file": f"workspace/{source.name}",
        "source_sha256": source_digest(original),
        **proposal["patch"],
    }
    if declaration is not None:
        patch["task_sha256"] = source_digest(declaration)
    return {
        **report,
        "status": "verified_under_supplied_contract",
        "independently_verified_under_contract": True,
        "unverified": ["the supplied contract is complete and authoritative for this task"],
        "patch": patch,
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
    parser.add_argument("--modulus", type=int)
    parser.add_argument("--residue", type=int)
    args = parser.parse_args()
    report = review_task(
        args.task_dir, args.source, function=args.function, contract=args.contract,
        threshold=args.threshold, factor=args.factor, modulus=args.modulus, residue=args.residue,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
