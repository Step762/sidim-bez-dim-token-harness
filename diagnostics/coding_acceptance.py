"""Check visible operator assertions from AST data, without running task Python."""

import argparse
import ast
from dataclasses import replace
import json
import math
import operator
import os
from pathlib import Path
import sys

from diagnostics.coding_research import (
    CASE_METHOD_EDITS,
    LIST_OPERATORS,
    MAX_SOURCE_BYTES,
    OperatorReturn,
    PREDICATE_EDITS,
    UnsupportedSource,
    apply_missing_colon,
    apply_operator_edit,
    parse_bounded_module,
    parse_int_literal,
    parse_operator_return,
    parse_review_json,
    read_task_declaration,
    source_digest,
    task_allows_review,
)
from runtime_limits import OperationTimeout, RuntimeLimits
from safe_files import directory_guards, identity, open_directory, open_text


SOURCE = "workspace/engine.py"
TESTS = "workspace/test_engine.py"
VISIBLE_FILES = frozenset((Path(SOURCE).name, Path(TESTS).name))
COMPARISONS = (ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
MAX_REPORT_BYTES = 8 * 1024
OPERATIONS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    "upper": str.upper,
    "lower": str.lower,
    "mean_floor": lambda values: sum(values) // len(values),
    "mean_true": lambda values: sum(values) / len(values),
    "first_item": operator.itemgetter(0),
    "last_item": operator.itemgetter(-1),
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
    """The proposed edit is not a supported local change."""


def _mean_expectation(node: ast.expr) -> int | float:
    match node:
        case ast.Constant(value=value) if type(value) is float and math.isfinite(value):
            return value
        case ast.UnaryOp(op=ast.USub(), operand=ast.Constant(value=value)) \
                if type(value) is float and math.isfinite(value):
            return -value
        case _:
            return parse_int_literal(node)


def _apply_patch(source: str, edit: object) -> str:
    if not isinstance(edit, dict) or edit.get("file") != SOURCE:
        raise InvalidPatch("expected an edit to engine.py")
    if edit.get("source_sha256") != source_digest(source):
        raise InvalidPatch("patch source hash is absent or does not match engine.py")
    try:
        if edit.get("old") == "" and edit.get("new") == ":":
            return apply_missing_colon(source, edit)
        return apply_operator_edit(source, edit)
    except ValueError as exc:
        raise InvalidPatch(str(exc)) from exc


def _read_assertion(
    statement: ast.stmt, binding: str, function: OperatorReturn, *, direct_import: bool,
) -> tuple[list[ast.expr], ast.expr]:
    match statement:
        case ast.Expr(value=ast.Call(
            func=ast.Attribute(value=ast.Name(id="self"), attr="assertEqual"),
            args=[ast.Call() as call, expected], keywords=[],
        )) | ast.Expr(value=ast.Call(
            func=ast.Attribute(value=ast.Name(id="self"), attr="assertEqual"),
            args=[expected, ast.Call() as call], keywords=[],
        )):
            pass
        case ast.Expr(value=ast.Call(
            func=ast.Attribute(value=ast.Name(id="self"), attr="assertTrue" | "assertFalse" as method),
            args=[ast.Call() as call], keywords=[],
        )) if function.operator in COMPARISONS:
            expected = ast.Constant(value=method == "assertTrue")
        case _:
            raise UnsupportedSource("unsupported visible assertion")
    target = (
        ast.Name(id=binding, ctx=ast.Load()) if direct_import
        else ast.Attribute(value=ast.Name(id=binding, ctx=ast.Load()), attr=function.function, ctx=ast.Load())
    )
    if call.keywords or ast.dump(call.func) != ast.dump(target):
        raise UnsupportedSource("visible assertion must call the declared function")
    arguments = call.args
    if len(arguments) == 1 and isinstance(arguments[0], ast.Starred):
        argument_tuple = arguments[0].value
        if not isinstance(argument_tuple, ast.Tuple):
            raise UnsupportedSource("expected a literal tuple of arguments")
        arguments = argument_tuple.elts
    if len(arguments) != len(function.parameters):
        raise UnsupportedSource("visible assertion has the wrong arity")
    return arguments, expected


def _visible_examples(
    text: str, function: OperatorReturn, *, module: str = "engine",
) -> list[tuple[int, tuple[int | str | tuple[int, ...], ...], int | bool | float | str]]:
    tree = parse_bounded_module(text)
    # Validate the entire surrounding module as well as each assertion. An
    # unsupported fixture, hook or extra statement invalidates the whole suite.
    if len(tree.body) != 4 or not isinstance(tree.body[2], ast.ClassDef):
        raise UnsupportedSource("unsupported unittest scaffold")
    match tree.body[1]:
        case ast.Import(names=[ast.alias(name=name, asname=alias)]) if name == module:
            binding = alias or module
            direct_import = False
        case ast.ImportFrom(
            module=name, names=[ast.alias(name=symbol, asname=alias)], level=0,
        ) if name == module and symbol == function.function:
            binding = alias or symbol
            direct_import = True
        case _:
            raise UnsupportedSource("test must import the declared source module")
    suite = tree.body[2]
    reserved = {"unittest", "load_tests", "setUpModule", "tearDownModule"}
    if (
        binding == "self" or binding in reserved or binding.startswith("_")
        or suite.name in reserved or suite.name == binding or suite.name.startswith("_")
    ):
        raise UnsupportedSource("test scaffold shadows an import or reserved binding")
    expected_tree = ast.parse(_SCAFFOLD)
    expected_suite = expected_tree.body[2]
    expected_method = expected_suite.body[0]
    statements, method_names = [], set()
    for method in suite.body:
        if (
            not isinstance(method, ast.FunctionDef)
            or not method.name.startswith("test")
            or method.name in method_names
        ):
            raise UnsupportedSource("expected distinct plain unittest methods")
        method_names.add(method.name)
        statements.extend(method.body)
        method.name, method.body = expected_method.name, [ast.Pass()]
        if ast.dump(method) != ast.dump(expected_method):
            raise UnsupportedSource("unsupported unittest method")
    tree.body[1] = expected_tree.body[1]
    suite.name, suite.body = expected_suite.name, [expected_method]
    if ast.dump(tree) != ast.dump(expected_tree):
        raise UnsupportedSource("unsupported unittest scaffold")

    examples = []
    for statement in statements:
        arguments, expected = _read_assertion(statement, binding, function, direct_import=direct_import)
        if function.operator in CASE_METHOD_EDITS:
            argument = arguments[0]
            if not (
                isinstance(argument, ast.Constant) and type(argument.value) is str
                and isinstance(expected, ast.Constant) and type(expected.value) is str
            ):
                raise UnsupportedSource("expected literal string input and result")
            examples.append((statement.lineno, (argument.value,), expected.value))
            continue
        if function.operator in LIST_OPERATORS:
            argument = arguments[0]
            if not isinstance(argument, ast.List):
                raise UnsupportedSource("expected a literal list of integers")
            values = tuple(parse_int_literal(item) for item in argument.elts)
            if function.operator in ("mean_floor", "mean_true"):
                value = _mean_expectation(expected)
            else:
                value = parse_int_literal(expected)
            examples.append((statement.lineno, (values,), value))
            continue
        inputs = tuple(parse_int_literal(argument) for argument in arguments)
        if function.operator not in COMPARISONS:
            value = parse_int_literal(expected)
        elif isinstance(expected, ast.Constant) and type(expected.value) is bool:
            value = expected.value
        else:
            raise UnsupportedSource("expected a boolean result for the comparison")
        examples.append((statement.lineno, inputs, value))
    return examples


def _passes(
    function: OperatorReturn, inputs: tuple[int | str | tuple[int, ...], ...], expected: int | bool | float | str,
) -> bool:
    if function.modulus is not None:
        operands = (inputs[0] % function.modulus, function.literal)
    else:
        operands = inputs if function.literal is None else (*inputs, function.literal)
    try:
        return OPERATIONS[function.operator](*operands) == expected
    except (IndexError, ZeroDivisionError):
        return False


def check_visible_examples(original: str, tests: str, edit: object) -> dict:
    """Compare one proposed edit on every supported assertion, all or nothing."""
    try:
        after = parse_operator_return(_apply_patch(original, edit))
        if edit["new"] == ":":
            if after.operator is not ast.Mult or after.literal is None:
                raise InvalidPatch("colon repair must restore a unary scaling function")
            before = None
        else:
            before = parse_operator_return(original)
            if (
                before.operator == after.operator
                or replace(before, operator=after.operator) != after
                or (before.literal is not None and PREDICATE_EDITS.get(before.operator) is not after.operator)
            ):
                raise InvalidPatch("edit does not change the function's operator within its supported form")
        examples = _visible_examples(tests, after)
    except InvalidPatch as exc:
        return {"status": "invalid_patch", "reason": str(exc)}
    except (SyntaxError, ValueError, RecursionError) as exc:
        return {"status": "unsupported", "reason": str(exc)}

    cases = [
        {"line": line, "before_passed": before is not None and _passes(before, inputs, expected),
         "after_passed": _passes(after, inputs, expected)}
        for line, inputs, expected in examples
    ]
    result = {"status": "visible_assertions_checked", "cases": cases,
              "tests_sha256": source_digest(tests),
              "before_passed": sum(case["before_passed"] for case in cases),
              "after_passed": sum(case["after_passed"] for case in cases)}
    if before is None:
        result["before_status"] = "syntax_invalid"
    return result


def _only_visible_files(workspace: Path, names: frozenset[str] = VISIBLE_FILES) -> bool:
    with open_directory(workspace) as directory, os.scandir(directory) as entries:
        return all(entry.name in names and entry.is_file(follow_symlinks=False) for entry in entries)


def check_task_examples(task_dir: Path, edit: object) -> dict:
    """Validate the bound declaration and read one isolated visible-test snapshot."""
    root = task_dir.absolute()
    workspace = root / "workspace"
    try:
        with open_directory(root) as task_fd, open_directory(workspace) as workspace_fd:
            guards = {root: identity(os.fstat(task_fd)), workspace: identity(os.fstat(workspace_fd))}
            with directory_guards(guards):
                declaration = read_task_declaration(root)
                if not task_allows_review(declaration, Path(SOURCE).name, test=Path(TESTS).name):
                    return {"status": "unsupported_task_instruction"}
                task_sha256 = source_digest(declaration) if declaration is not None else None
                if not isinstance(edit, dict) or edit.get("task_sha256") != task_sha256:
                    return {"status": "invalid_patch", "reason": "patch does not match the task declaration"}
                if not _only_visible_files(workspace):
                    return {"status": "unsupported_workspace_files"}
                with (
                    open_text(root, SOURCE, maximum=MAX_SOURCE_BYTES) as source,
                    open_text(root, TESTS, maximum=MAX_SOURCE_BYTES) as tests,
                ):
                    result = check_visible_examples(source.read(), tests.read(), edit)
                    if read_task_declaration(root) != declaration or not _only_visible_files(workspace):
                        return {"status": "visible_inputs_changed"}
                return result
    except (OSError, ValueError):
        return {"status": "source_or_tests_unavailable"}


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
                report = parse_review_json(raw)
            except (ValueError, RecursionError) as exc:
                parser.error(f"invalid review JSON on stdin: {exc}")
            if not isinstance(report, dict):
                parser.error("expected a review JSON object")
            result = check_task_examples(args.task_dir, report.get("patch"))
    except (TimeoutError, OperationTimeout):
        result = {"status": "analysis_limit"}
    except (OSError, ValueError):
        result = {"status": "source_or_tests_unavailable"}
    print(json.dumps({**result, "contest_solution_confirmed": False}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
