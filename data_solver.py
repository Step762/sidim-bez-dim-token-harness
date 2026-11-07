"""Streaming CSV aggregates for explicitly supported instructions."""

import csv
from fractions import Fraction
import operator
from pathlib import Path
import re

from safe_files import CSV_BYTES, open_text


class UnsupportedData(ValueError):
    """The requested result cannot be established reliably."""


_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
_QUOTED = r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\""
_INSTRUCTION = re.compile(
    rf"The file (?P<filename>[A-Za-z0-9_./-]+\.csv)"
    rf"(?: has columns {_IDENT}(?:\s*,\s*{_IDENT})*"
    r"(?: \([0-9]+ rows?\))?)?\. (?P<question>.+)\?(?P<suffix>.*)",
    re.IGNORECASE | re.DOTALL,
)
_SUFFIX = re.compile(
    r"(?: Use bash with python3 to compute it\.)?"
    r"(?: Work only inside the current directory\.)?"
    r"(?: When you are done, write ONLY the final answer to the file answer\.txt"
    r" in the current directory"
    r"(?: \(for example with bash: echo 42 > answer\.txt\))?\.)?",
    re.IGNORECASE,
)
_SALES = re.compile(
    rf"How many (?P<column>{_IDENT}) of (?P<field>{_IDENT}) "
    rf"(?P<value>{_QUOTED}) were sold"
    rf"(?: in (?P<range>{_IDENT}) (?P<lower>{_NUMBER})"
    rf" through (?P<upper>{_NUMBER}) inclusive)?",
    re.IGNORECASE | re.DOTALL,
)
_AGGREGATE = re.compile(
    rf"What is the (?P<op>sum|count|min|minimum|max|maximum|avg|average)"
    rf" of (?P<column>{_IDENT})(?: where (?P<filters>.+))?",
    re.IGNORECASE | re.DOTALL,
)
_COUNT_ROWS = re.compile(
    r"How many rows are there(?: where (?P<filters>.+))?", re.IGNORECASE | re.DOTALL
)
_TOKEN = re.compile(
    rf"(?P<string>{_QUOTED})|(?P<number>{_NUMBER})|"
    rf"(?P<identifier>{_IDENT})|(?P<comparison>!=|<=|>=|=|<|>)"
)
_COMPARISONS = {
    "=": operator.eq, "!=": operator.ne, "<": operator.lt,
    "<=": operator.le, ">": operator.gt, ">=": operator.ge,
}
_OPERATIONS = {
    "sum": "sum", "count": "count", "min": "min", "minimum": "min",
    "max": "max", "maximum": "max", "avg": "avg", "average": "avg",
}


def _number(value: str) -> Fraction:
    value = value.strip()
    if re.fullmatch(_NUMBER, value) is None:
        raise UnsupportedData("invalid or unsupported numeric value")
    return Fraction(value)


def _unquote(value: str) -> str:
    return value[1:-1].replace(value[0] * 2, value[0])


def _range(column, lower, upper):
    lower, upper = _number(lower), _number(upper)
    if lower > upper:
        raise UnsupportedData("reversed interval")
    return [(column, ">=", lower), (column, "<=", upper)]


def _filters(text: str) -> list[tuple]:
    """Tokenize quoted literals intact; support conjunctions, not arbitrary code."""
    tokens = []
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        match = _TOKEN.match(text, position)
        if match is None:
            raise UnsupportedData("unsupported filter syntax")
        tokens.append((match.lastgroup, match[0]))
        position = match.end()

    conditions = []
    position = 0
    try:
        while position < len(tokens):
            kind, column = tokens[position]
            if kind != "identifier":
                raise UnsupportedData("filter must start with a column")
            relation = tokens[position + 1][1].lower()
            if relation == "between":
                if (
                    tokens[position + 2][0] != "number"
                    or tokens[position + 3][1].lower() != "and"
                    or tokens[position + 4][0] != "number"
                    or tokens[position + 5][1].lower() != "inclusive"
                ):
                    raise UnsupportedData("range must specify inclusive bounds")
                conditions.extend(_range(
                    column, tokens[position + 2][1], tokens[position + 4][1]
                ))
                position += 6
            else:
                kind, raw = tokens[position + 2]
                if relation not in _COMPARISONS or kind not in {"string", "number"}:
                    raise UnsupportedData("unsupported comparison")
                if kind == "string" and relation not in {"=", "!="}:
                    raise UnsupportedData("string ordering is unsupported")
                value = _unquote(raw) if kind == "string" else _number(raw)
                conditions.append((column, relation, value))
                position += 3
            if position < len(tokens):
                if tokens[position][1].lower() != "and" or position + 1 == len(tokens):
                    raise UnsupportedData("filters must be joined with AND")
                position += 1
    except IndexError as exc:
        raise UnsupportedData("incomplete filter") from exc
    if not conditions:
        raise UnsupportedData("empty filter")
    return conditions


def _question(question: str, header: list[str]):
    if match := _SALES.fullmatch(question):
        conditions = [(match["field"], "=", _unquote(match["value"]))]
        if match["range"]:
            name = match["range"]
            candidates = {name, name[:-1] if name.endswith("s") else name} & set(header)
            if len(candidates) != 1:
                raise UnsupportedData("range column is missing or ambiguous")
            conditions.extend(_range(next(iter(candidates)), match["lower"], match["upper"]))
        return "sum", match["column"], conditions
    if match := _AGGREGATE.fullmatch(question):
        conditions = _filters(match["filters"]) if match["filters"] is not None else []
        return _OPERATIONS[match["op"].lower()], match["column"], conditions
    if match := _COUNT_ROWS.fullmatch(question):
        conditions = _filters(match["filters"]) if match["filters"] is not None else []
        return "count", None, conditions
    raise UnsupportedData("unsupported or ambiguous analysis question")


def _format(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise UnsupportedData("result requires an unspecified rounding rule")
    places = max(twos, fives)
    whole, remainder = divmod(abs(value.numerator), value.denominator)
    decimal = str(remainder * 10**places // value.denominator).zfill(places).rstrip("0")
    return ("-" if value < 0 else "") + f"{whole}.{decimal}"


def _analyze(stream, question: str) -> str:
    reader = csv.reader(stream, strict=True)
    header = next(reader, [])
    if not header or any(not name.strip() for name in header) or len(set(header)) != len(header):
        raise UnsupportedData("missing, empty or duplicate CSV headers")
    operation, column, conditions = _question(question, header)
    referenced = {name for name, _, _ in conditions}
    if column is not None:
        referenced.add(column)
    if not referenced <= set(header):
        raise UnsupportedData("requested column is absent from CSV")
    positions = {name: index for index, name in enumerate(header)}
    numeric = {name for name, _, value in conditions if isinstance(value, Fraction)}
    if operation != "count":
        numeric.add(column)

    count = 0
    total = Fraction(0)
    extreme = None
    for row in reader:
        if not row:  # Empty physical lines are allowed, including trailing lines.
            continue
        if len(row) != len(header):
            raise UnsupportedData("CSV row has the wrong number of fields")
        numbers = {name: _number(row[positions[name]]) for name in numeric}
        if operation == "count" and column is not None and not row[positions[column]].strip():
            raise UnsupportedData("COUNT column contains missing values")
        if not all(
            _COMPARISONS[relation](
                numbers[name] if isinstance(value, Fraction) else row[positions[name]], value
            )
            for name, relation, value in conditions
        ):
            continue
        count += 1
        if operation in {"sum", "avg"}:
            total += numbers[column]
        elif operation == "min":
            value = numbers[column]
            extreme = value if extreme is None else min(extreme, value)
        elif operation == "max":
            value = numbers[column]
            extreme = value if extreme is None else max(extreme, value)
    if operation == "count":
        return str(count)
    if operation == "sum":
        return _format(total)
    if count == 0:
        raise UnsupportedData("aggregate is undefined for an empty selection")
    return _format(total / count if operation == "avg" else extreme)


def solve_data(instruction: str, task_dir: Path) -> str:
    # Normalize formatting outside literals without changing quoted field values.
    statement = re.sub(
        _QUOTED + r"|\s+", lambda m: " " if m[0].isspace() else m[0], instruction
    ).strip()
    parsed = _INSTRUCTION.fullmatch(statement)
    if parsed is None or _SUFFIX.fullmatch(parsed["suffix"]) is None:
        raise UnsupportedData("unsupported analysis instructions")
    try:
        workspace = task_dir / "workspace"
        relative = Path(parsed["filename"])
        if relative.is_absolute() or ".." in relative.parts:
            raise UnsupportedData("CSV must be inside the current workspace")
        path = workspace / relative
        if (
            not workspace.resolve().is_relative_to(task_dir.resolve())
            or not path.resolve().is_relative_to(workspace.resolve())
        ):
            raise UnsupportedData("CSV points outside the current workspace")
        with open_text(task_dir, Path("workspace") / relative, maximum=CSV_BYTES,
                       encoding="utf-8-sig", newline="") as stream:
            return _analyze(stream, parsed["question"])
    except UnsupportedData:
        raise
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise UnsupportedData("CSV cannot be read or contains invalid data") from exc
