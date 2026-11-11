"""Choose and publish a bounded Coding repair using literal unittest assertions."""

import ast
from dataclasses import replace
import os
from pathlib import Path
import secrets
import stat
import sys
import tokenize

from coding_solver import UnsupportedCoding, _INSTRUCTION
from diagnostics.coding_acceptance import _only_visible_files, _passes, _visible_examples
from diagnostics.coding_research import (
    ARITHMETIC_OPERATORS,
    CASE_METHOD_EDITS,
    EXPECTED_OPERATORS,
    LIST_EDITS,
    MAX_SOURCE_BYTES,
    PREDICATE_EDITS,
    _missing_colon_edit,
    _operator_patch,
    apply_missing_colon,
    apply_operator_edit,
    parse_operator_return,
    parse_review_json,
    read_task_declaration,
)
from safe_files import _version, directory_guards, identity, open_directory, open_text


def _select_repair(source: str, tests: str, module: str) -> str:
    try:
        original = parse_operator_return(source)
    except SyntaxError:
        candidate = apply_missing_colon(source, _missing_colon_edit(source))
        repaired = parse_operator_return(candidate)
        if repaired.operator is not ast.Mult or repaired.literal is None:
            raise UnsupportedCoding("unsupported syntax repair")
        examples = _visible_examples(tests, repaired, module=module)
        if all(_passes(repaired, inputs, expected) for _, inputs, expected in examples):
            return candidate
        raise UnsupportedCoding("restored syntax does not satisfy the visible tests")

    examples = _visible_examples(tests, original, module=module)
    if all(_passes(original, inputs, expected) for _, inputs, expected in examples):
        return source

    if original.operator in ARITHMETIC_OPERATORS and original.literal is None:
        alternatives = EXPECTED_OPERATORS.values()
    else:
        replacement = (
            CASE_METHOD_EDITS.get(original.operator)
            or LIST_EDITS.get(original.operator)
            or PREDICATE_EDITS.get(original.operator)
        )
        alternatives = (replacement,) if replacement is not None else ()
    winner = None
    for operator in alternatives:
        if operator == original.operator:
            continue
        repaired = replace(original, operator=operator)
        if all(_passes(repaired, inputs, expected) for _, inputs, expected in examples):
            if winner is not None:
                raise UnsupportedCoding("visible tests do not distinguish the supported repairs")
            winner = repaired
    if winner is None:
        raise UnsupportedCoding("no supported repair satisfies the visible tests")

    candidate = apply_operator_edit(source, _operator_patch(source, original.operator, winner.operator))
    if parse_operator_return(candidate) != winner:
        raise UnsupportedCoding("repair did not preserve the verified function shape")
    return candidate


def _publish(workspace: Path, directory: int, source: str, content: str, inputs: dict, declaration: str) -> None:
    temporary = f".coding-{secrets.token_hex(16)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content.encode("utf-8"))
            output.flush()
            os.fchmod(output.fileno(), stat.S_IMODE(inputs[source].st_mode) & 0o777)
            os.fsync(output.fileno())
        if (
            read_task_declaration(workspace.parent) != declaration
            or not _only_visible_files(workspace, frozenset((*inputs, temporary)))
            or any(_version(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _version(info)
                   for name, info in inputs.items())
        ):
            raise UnsupportedCoding("coding inputs changed before publication")
        os.replace(temporary, source, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def solve_static_coding(instruction: str, task_dir: Path) -> str:
    """Infer a local repair from the declared tests without importing task modules."""
    normalized = " ".join(instruction.split())
    parsed = _INSTRUCTION.fullmatch(normalized)
    if parsed is None:
        raise UnsupportedCoding("unsupported coding instructions")
    source, test = parsed["source"], parsed["test"]
    names = frozenset((source, test))
    module = Path(source).stem
    if (
        len(names) != 2 or module.startswith("test") or module.endswith("_test")
        or any(Path(name).stem in sys.stdlib_module_names for name in names)
    ):
        raise UnsupportedCoding("unsupported source or test filename")

    root = task_dir.absolute()
    workspace = root / "workspace"
    try:
        with open_directory(root) as task_fd, open_directory(workspace) as workspace_fd:
            guards = {root: identity(os.fstat(task_fd)), workspace: identity(os.fstat(workspace_fd))}
            with directory_guards(guards):
                declaration = read_task_declaration(root)
                metadata = parse_review_json(declaration) if declaration is not None else None
                if (
                    not isinstance(metadata, dict) or metadata.get("task_type") != "coding"
                    or not isinstance(metadata.get("instruction"), str)
                    or " ".join(metadata["instruction"].split()) != normalized
                ):
                    raise UnsupportedCoding("coding declaration changed or is invalid")
                if not _only_visible_files(workspace, names):
                    raise UnsupportedCoding("coding workspace contains unsupported files")
                inputs = {name: os.stat(name, dir_fd=workspace_fd, follow_symlinks=False) for name in names}
                with (
                    open_text(workspace, source, maximum=MAX_SOURCE_BYTES, newline="") as source_file,
                    open_text(workspace, test, maximum=MAX_SOURCE_BYTES) as test_file,
                ):
                    original = source_file.read()
                    candidate = _select_repair(original, test_file.read(), module)
                if candidate != original:
                    _publish(workspace, workspace_fd, source, candidate, inputs, declaration)
                elif (
                    read_task_declaration(root) != declaration or not _only_visible_files(workspace, names)
                    or any(_version(os.stat(name, dir_fd=workspace_fd, follow_symlinks=False)) != _version(info)
                           for name, info in inputs.items())
                ):
                    raise UnsupportedCoding("coding inputs changed before completion")
        return "DONE"
    except UnsupportedCoding:
        raise
    except (OSError, SyntaxError, ValueError, RecursionError, tokenize.TokenError) as exc:
        raise UnsupportedCoding("coding input is outside the static verifier's supported subset") from exc
