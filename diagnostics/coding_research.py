"""Review one binary arithmetic repair against a supplied exact-int contract."""

import argparse
import ast
from dataclasses import dataclass
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
OPERATOR_TEXT = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
DOMAIN = "two exact built-in int arguments, assuming sufficient resources"


class UnsupportedSource(ValueError):
    """The source is outside the verifier's closed grammar."""


@dataclass(frozen=True)
class BinaryReturn:
    function: str
    parameters: tuple[str, str]
    operator: type[ast.operator]


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


def parse_binary_return(source: str) -> BinaryReturn:
    """Recognize one plain function that applies an operator to its parameters."""
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
        or len(args.args) != 2
        or any(arg.annotation is not None or arg.type_comment is not None for arg in args.args)
    ):
        raise UnsupportedSource("expected two plain parameters")
    left, right = (arg.arg for arg in args.args)
    if left == right or len(function.body) != 1 or not isinstance(function.body[0], ast.Return):
        raise UnsupportedSource("expected distinct parameters and one return")

    value = function.body[0].value
    if (
        not isinstance(value, ast.BinOp)
        or type(value.op) not in OPERATOR_TEXT
        or not isinstance(value.left, ast.Name)
        or value.left.id != left
        or not isinstance(value.right, ast.Name)
        or value.right.id != right
    ):
        raise UnsupportedSource("expected one binary operation on parameters in signature order")
    return BinaryReturn(function.name, (left, right), type(value.op))


def verify_candidate(original: str, candidate: str, *, function: str, contract: str) -> bool:
    """Prove an operator-only edit without using C3's polynomial normalization."""
    expected = EXPECTED_OPERATORS.get(contract)
    if expected is None:
        return False
    try:
        before = parse_binary_return(original)
        after = parse_binary_return(candidate)
    except (SyntaxError, ValueError, RecursionError):
        return False
    # On exact ints, these AST operators directly implement the supplied operation
    # for every pair of inputs. Division is allowed only as the original defect.
    return (
        before.function == after.function == function
        and before.parameters == after.parameters
        and before.operator is not expected
        and after.operator is expected
    )


def _operator_patch(source: str, before: type[ast.operator], after: type[ast.operator]) -> dict:
    old, new = OPERATOR_TEXT[before], OPERATOR_TEXT[after]
    tokens = [
        token for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.OP and token.string == old
    ]
    if len(tokens) != 1:
        raise UnsupportedSource("expected a unique operator token")
    line, column = tokens[0].start
    return {"line": line, "column": column + 1, "old": old, "new": new}


def review_task(task_dir: Path, source: Path, *, function: str | None, contract: str | None) -> dict:
    """Read one workspace file and return a one-token edit or a reason to skip."""
    report = {
        "function": function,
        "contract": contract,
        "domain": DOMAIN,
        "independently_verified_under_contract": False,
        "contest_solution_confirmed": False,
    }
    if not function or contract not in EXPECTED_OPERATORS:
        return {**report, "status": "missing_or_unsupported_contract"}
    if source.suffix != ".py" or source != Path(source.name):
        return {**report, "status": "invalid_source_path"}

    try:
        with RuntimeLimits(5, 2).sync():
            original = read_text(task_dir, Path("workspace") / source, maximum=MAX_SOURCE_BYTES)
            before = parse_binary_return(original)
            if before.function != function:
                return {**report, "status": "function_mismatch"}
            if before.operator is EXPECTED_OPERATORS[contract]:
                return {**report, "status": "already_conforms"}
            proposal = static_coding.review_source(
                original, experimental=True, function=function, contract=contract,
            )
            candidate = proposal.get("proposal_source")
            if not isinstance(candidate, str):
                return {**report, "status": "no_candidate"}
            if not verify_candidate(original, candidate, function=function, contract=contract):
                return {**report, "status": "candidate_rejected"}
            patch = _operator_patch(original, before.operator, EXPECTED_OPERATORS[contract])
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
    parser.add_argument("--contract", choices=EXPECTED_OPERATORS, required=True)
    args = parser.parse_args()
    report = review_task(args.task_dir, args.source, function=args.function, contract=args.contract)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
