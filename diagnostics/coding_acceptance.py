"""Check visible operator assertions from AST data, without running task Python."""

import argparse
import ast
from dataclasses import replace
import json
import operator
from pathlib import Path
import sys

from diagnostics.coding_research import (
    MAX_SOURCE_BYTES,
    OperatorReturn,
    UnsupportedSource,
    apply_operator_edit,
    parse_bounded_module,
    parse_int_literal,
    parse_operator_return,
    source_digest,
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
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
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
    """The proposed edit does not change one supported operator."""


def _apply_patch(source: str, edit: object) -> str:
    if not isinstance(edit, dict) or edit.get("file") != SOURCE:
        raise InvalidPatch("expected an edit to engine.py")
    if edit.get("source_sha256") != source_digest(source):
        raise InvalidPatch("patch source hash is absent or does not match engine.py")
    try:
        return apply_operator_edit(source, edit)
    except ValueError as exc:
        raise InvalidPatch(str(exc)) from exc


def _visible_examples(text: str, function: OperatorReturn) -> list[tuple[int, tuple[int, ...], int | bool]]:
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
                             args=[ast.Starred(value=ast.Tuple(elts=arguments))], keywords=[]),
                    expected,
                ],
                keywords=[],
            )) if name == function.function and len(arguments) == len(function.parameters):
                inputs = tuple(parse_int_literal(argument) for argument in arguments)
                if function.threshold is None:
                    value = parse_int_literal(expected)
                elif isinstance(expected, ast.Constant) and type(expected.value) is bool:
                    value = expected.value
                else:
                    raise UnsupportedSource("expected a boolean result for the threshold comparison")
                examples.append((statement.lineno, inputs, value))
            case _:
                raise UnsupportedSource("unsupported visible assertion")
    return examples


def _passes(function: OperatorReturn, inputs: tuple[int, ...], expected: int | bool) -> bool:
    operands = inputs if function.threshold is None else (*inputs, function.threshold)
    try:
        return OPERATIONS[function.operator](*operands) == expected
    except ZeroDivisionError:
        return False


def check_visible_examples(original: str, tests: str, edit: object) -> dict:
    """Compare one proposed edit on every supported assertion, all or nothing."""
    try:
        before = parse_operator_return(original)
        after = parse_operator_return(_apply_patch(original, edit))
        if before.operator is after.operator or replace(before, operator=after.operator) != after:
            raise InvalidPatch("edit does not change the function's operator")
        examples = _visible_examples(tests, before)
    except InvalidPatch as exc:
        return {"status": "invalid_patch", "reason": str(exc)}
    except (SyntaxError, ValueError, RecursionError) as exc:
        return {"status": "unsupported", "reason": str(exc)}

    cases = [
        {"line": line, "before_passed": _passes(before, inputs, expected),
         "after_passed": _passes(after, inputs, expected)}
        for line, inputs, expected in examples
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
