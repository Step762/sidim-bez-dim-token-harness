"""Bounded, test-guided repairs for explicitly trusted Python workspaces."""

import ast
import copy
from dataclasses import dataclass
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time


class UnsupportedCoding(ValueError):
    """The supported repairs cannot produce a verified solution."""


TEST_TIMEOUT = 2.0
TASK_TIMEOUT = 20.0
MAX_OUTPUT_BYTES = 64 * 1024
MAX_ATTEMPTS = 40
MAX_REPAIRS = 4

_INSTRUCTION = re.compile(
    r"Run `?python3 (?P<test>[A-Za-z_][A-Za-z0-9_]*\.py)`? in the current directory; "
    r"some tests fail\. Fix the bugs in (?P<source>[A-Za-z_][A-Za-z0-9_]*\.py) "
    r"\(do not edit the tests\) until all tests pass\. Work only inside the current "
    r"directory\. When you are done, write ONLY the word DONE to the file answer\.txt "
    r"in the current directory(?: \(for example with bash: echo 42 > answer\.txt\))?\.",
)
_OPERATORS = {
    ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div,
    ast.Div: ast.Mult, ast.FloorDiv: ast.Mult,
    ast.Lt: ast.Gt, ast.Gt: ast.Lt, ast.LtE: ast.GtE, ast.GtE: ast.LtE,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
}


@dataclass(frozen=True)
class TestReport:
    total: int
    failed: frozenset[str]


def _run_tests(directory: Path, test: str, timeout: float) -> TestReport | None:
    """Run the named unittest script with bounded combined output and lifetime."""
    if timeout <= 0:
        return None
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        [sys.executable, "-B", "-E", "-s", test], cwd=directory,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        shell=False, start_new_session=True,
    ) as process:
        try:
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    if not selector.select(remaining):
                        return None
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT_BYTES:
                        return None
            try:
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                return None
        finally:
            # Only this invocation's process group is terminated, including children.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    text = output.decode("utf-8", errors="replace")
    totals = re.findall(r"^Ran ([0-9]+) tests? in [0-9.]+s$", text, re.MULTILINE)
    if len(totals) != 1 or int(totals[0]) == 0:
        return None
    failed = frozenset(re.findall(r"^(?:FAIL|ERROR): (.+)$", text, re.MULTILINE))
    passed = re.search(r"^OK$", text, re.MULTILINE)
    unsuccessful = re.search(
        r"^FAILED \((?:failures|errors)=[0-9]+(?:, (?:failures|errors)=[0-9]+)*\)$",
        text, re.MULTILINE,
    )
    if (code == 0 and passed and not failed) or (code != 0 and unsuccessful and failed):
        return TestReport(int(totals[0]), failed)
    return None  # Unsupported runners, skipped checks and incomplete output are not success.


def _mutants(source: bytes):
    """Change one AST operation at a time; keep names, literals and other text."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    bound_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
    }
    bound_names.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
    bound_names.update(
        node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    )
    bound_names.update(
        node.asname or node.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.alias)
    )
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            replacements = []
            if isinstance(node, (ast.BinOp, ast.AugAssign)) and type(node.op) in _OPERATORS:
                changed = copy.deepcopy(node)
                changed.op = _OPERATORS[type(node.op)]()
                replacements.append(changed)
            elif isinstance(node, ast.Compare):
                for index, operation in enumerate(node.ops):
                    if type(operation) in _OPERATORS:
                        changed = copy.deepcopy(node)
                        changed.ops[index] = _OPERATORS[type(operation)]()
                        replacements.append(changed)
            elif (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in {"sorted", "reversed"}
                and not {"sorted", "reversed"} & bound_names
                and len(node.args) == 1 and not node.keywords
            ):
                changed = copy.deepcopy(node)
                changed.func.id = "reversed" if node.func.id == "sorted" else "sorted"
                replacements.append(changed)
            for changed in replacements:
                start = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
                end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
                candidate = source[:start] + ast.unparse(changed).encode("utf-8") + source[end:]
                try:
                    ast.parse(candidate)
                except SyntaxError:
                    continue
                yield candidate


def _snapshot(directory: Path) -> dict[Path, bytes]:
    snapshot = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise UnsupportedCoding("symbolic links in a coding workspace are unsupported")
        if path.is_file() and path.suffix == ".py":
            if not path.resolve().is_relative_to(directory.resolve()):
                raise UnsupportedCoding("source points outside workspace")
            snapshot[path.relative_to(directory)] = path.read_bytes()
    return snapshot


def _publish(path: Path, content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".coding-", delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), stat.S_IMODE(path.stat().st_mode))
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def solve_coding(instruction: str, task_dir: Path, *, trusted: bool = False) -> str:
    """Repair one declared module. Trust opt-in is required even for direct calls."""
    if trusted is not True:
        raise UnsupportedCoding("coding execution requires explicit trusted-code opt-in")
    parsed = _INSTRUCTION.fullmatch(" ".join(instruction.split()))
    if parsed is None:
        raise UnsupportedCoding("unsupported coding instructions")
    test, target = Path(parsed["test"]), Path(parsed["source"])
    if target == test or target.stem.startswith("test") or target.stem.endswith("_test"):
        raise UnsupportedCoding("test files cannot be repair targets")
    workspace = task_dir / "workspace"
    try:
        if not workspace.is_dir() or not workspace.resolve().is_relative_to(task_dir.resolve()):
            raise UnsupportedCoding("missing or external workspace")
        originals = _snapshot(workspace)
        if target not in originals or test not in originals:
            raise UnsupportedCoding("required Python files are missing")
        # Source-span edits use UTF-8 byte offsets. Other source encodings are unsupported.
        originals[target].decode("utf-8")
        ast.parse(originals[target])
        attempts = 0
        deadline = time.monotonic() + TASK_TIMEOUT

        def evaluate(content: bytes) -> TestReport | None:
            nonlocal attempts
            if attempts >= MAX_ATTEMPTS or time.monotonic() >= deadline:
                return None
            attempts += 1
            with tempfile.TemporaryDirectory(prefix="coding-trial-") as temporary:
                trial = Path(temporary)
                expected = {**originals, target: content}
                for relative, data in expected.items():
                    path = trial / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                report = _run_tests(trial, str(test), min(TEST_TIMEOUT, deadline - time.monotonic()))
                if _snapshot(trial) != expected:
                    return None  # Neither tests nor source may rewrite themselves during a trial.
                return report

        current = originals[target]
        report = evaluate(current)
        if report is None:
            raise UnsupportedCoding("tests timed out, exceeded output limits or use an unsupported runner")
        print(f"coding: {report.total} tests; failing: {sorted(report.failed)}", flush=True)
        visited = {current}
        for _ in range(MAX_REPAIRS):
            if not report.failed:
                break
            for candidate in _mutants(current):
                if attempts >= MAX_ATTEMPTS - 1 or time.monotonic() >= deadline:
                    break
                if candidate in visited:
                    continue
                visited.add(candidate)
                tested = evaluate(candidate)
                if tested is not None and tested.total == report.total and tested.failed < report.failed:
                    current, report = candidate, tested
                    break
            else:
                break
            if report.failed and (attempts >= MAX_ATTEMPTS - 1 or time.monotonic() >= deadline):
                break
        if report.failed:
            raise UnsupportedCoding("supported repairs did not make all tests pass")
        verified = evaluate(current)
        if verified is None or verified.failed or verified.total != report.total:
            raise UnsupportedCoding("final fresh-copy verification failed")
        if _snapshot(workspace) != originals:
            raise UnsupportedCoding("workspace changed during repair; refusing to overwrite it")
        if current != originals[target]:
            _publish(workspace / target, current)
        print(f"coding: verified all {report.total} tests after {attempts} runs", flush=True)
        return "DONE"
    except UnsupportedCoding:
        raise
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        raise UnsupportedCoding("source cannot be read, parsed or safely updated") from exc
