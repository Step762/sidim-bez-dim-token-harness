"""Check visible arithmetic assertions from AST data, without running task Python."""

import argparse
import ast
import json
import operator
from pathlib import Path
import sys

from diagnostics.coding_research import (
    MAX_SOURCE_BYTES,
    UnsupportedSource,
    parse_binary_return,
    parse_bounded_module,
)
from runtime_limits import OperationTimeout, RuntimeLimits
from safe_files import read_text


SOURCE = "workspace/engine.py"
TESTS = "workspace/test_engine.py"
MAX_REPORT_BYTES = 8 * 1024
OPERATIONS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

_SCAFFOLD = """
import unittest
import engine as lib

class Examples(unittest.TestCase):
    def test_examples(self):
        pass

if __name__ == "__main__":
    unittest.main()
"""


class InvalidPatch(ValueError):
    """The proposed edit does not change one arithmetic operator."""


def _apply_patch(source: str, edit: object) -> str:
    if not isinstance(edit, dict) or edit.get("file") != SOURCE:
        raise InvalidPatch("expected an edit to engine.py")
    line, column = edit.get("line"), edit.get("column")
    old, new = edit.get("old"), edit.get("new")
    if (
        type(line) is not int or type(column) is not int or line < 1 or column < 1
        or old not in ("+", "-", "*", "/") or new not in ("+", "-", "*") or old == new
    ):
        raise InvalidPatch("invalid edit coordinates or operator")
    lines = source.split("\n")
    if line > len(lines) or lines[line - 1][column - 1:column] != old:
        raise InvalidPatch("edit does not match the source")
    lines[line - 1] = lines[line - 1][:column - 1] + new + lines[line - 1][column:]
    return "\n".join(lines)


def _small_int(node: ast.expr) -> int:
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


def _visible_examples(text: str, function: str) -> list[tuple[int, int, int, int]]:
    tree = parse_bounded_module(text)
    # Validate the entire surrounding module as well as each assertion. An
    # unsupported fixture, hook or extra statement invalidates the whole suite.
    if len(tree.body) != 4 or not isinstance(tree.body[2], ast.ClassDef):
        raise UnsupportedSource("unsupported unittest scaffold")
    suite = tree.body[2]
    if len(suite.body) != 1 or not isinstance(suite.body[0], ast.FunctionDef):
        raise UnsupportedSource("expected one plain test method")
    method = suite.body[0]
    statements, method.body = method.body, [ast.Pass()]
    if ast.dump(tree) != ast.dump(ast.parse(_SCAFFOLD)):
        raise UnsupportedSource("unsupported unittest scaffold")

    examples = []
    for statement in statements:
        match statement:
            case ast.Expr(value=ast.Call(
                func=ast.Attribute(value=ast.Name(id="self"), attr="assertEqual"),
                args=[
                    ast.Call(func=ast.Attribute(value=ast.Name(id="lib"), attr=name),
                             args=[ast.Starred(value=ast.Tuple(elts=[left, right]))], keywords=[]),
                    expected,
                ],
                keywords=[],
            )) if name == function:
                examples.append((statement.lineno, _small_int(left), _small_int(right), _small_int(expected)))
            case _:
                raise UnsupportedSource("unsupported visible assertion")
    return examples


def _passes(operation: type[ast.operator], left: int, right: int, expected: int) -> bool:
    try:
        return OPERATIONS[operation](left, right) == expected
    except ZeroDivisionError:
        return False


def check_visible_examples(original: str, tests: str, edit: object) -> dict:
    """Compare one proposed edit on every supported assertion, all or nothing."""
    try:
        before = parse_binary_return(original)
        after = parse_binary_return(_apply_patch(original, edit))
        if (
            before.operator is after.operator
            or before.parameters != after.parameters
            or before.function != after.function
        ):
            raise InvalidPatch("edit does not change the function's operator")
        examples = _visible_examples(tests, before.function)
    except InvalidPatch as exc:
        return {"status": "invalid_patch", "reason": str(exc)}
    except (SyntaxError, ValueError, RecursionError) as exc:
        return {"status": "unsupported", "reason": str(exc)}

    cases = [
        {"line": line, "before_passed": _passes(before.operator, left, right, expected),
         "after_passed": _passes(after.operator, left, right, expected)}
        for line, left, right, expected in examples
    ]
    return {"status": "visible_assertions_checked", "cases": cases,
            "before_passed": sum(case["before_passed"] for case in cases),
            "after_passed": sum(case["after_passed"] for case in cases)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experimental-static-coding", action="store_true", required=True)
    parser.add_argument("--task-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        with RuntimeLimits(5, 2).sync():
            raw = sys.stdin.buffer.read(MAX_REPORT_BYTES + 1)
            if len(raw) > MAX_REPORT_BYTES:
                parser.error("review JSON exceeds size limit")
            try:
                report = json.loads(raw)
            except (ValueError, RecursionError):
                parser.error("expected review JSON on stdin")
            if not isinstance(report, dict):
                parser.error("expected a review JSON object")
            original = read_text(args.task_dir, SOURCE, maximum=MAX_SOURCE_BYTES)
            tests = read_text(args.task_dir, TESTS, maximum=MAX_SOURCE_BYTES)
            result = check_visible_examples(original, tests, report.get("patch"))
    except (TimeoutError, OperationTimeout):
        result = {"status": "analysis_limit"}
    except (OSError, ValueError):
        result = {"status": "source_or_tests_unavailable"}
    print(json.dumps({**result, "contest_solution_confirmed": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
