"""Streaming CSV aggregates and bounded first-row ranking for explicit instructions."""

import csv
from fractions import Fraction
import json
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
_CALCULATE_TOTAL = re.compile(
    rf"Calculate the total (?P<column>{_IDENT}) in (?P<filename>[A-Za-z0-9_./-]+\.csv) "
    rf"where (?P<field>{_IDENT}) is (?P<value>{_QUOTED}) and "
    rf"(?P<lower_field>{_IDENT}) at least (?P<lower>{_NUMBER})\.",
    re.IGNORECASE | re.DOTALL,
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
_GROUPED = re.compile(
    rf"Using (?P<filename>[A-Za-z0-9_./-]+\.csv), return a JSON object mapping each "
    rf"(?P<group>{_IDENT}) to its (?:"
    rf"(?P<rows>row count)|(?P<op>total|sum|count|min|minimum|max|maximum|avg|average)"
    rf"(?: of)? (?P<column>{_IDENT}))"
    rf"(?: where (?P<filters>.+?))?\.(?P<suffix>{_SUFFIX.pattern})",
    re.IGNORECASE | re.DOTALL,
)
_SORT_FIRST = re.compile(
    rf"(?:In|Using) (?P<filename>[A-Za-z0-9_./-]+\.csv), sort by "
    rf"(?P<primary>{_IDENT})(?: numerically)? (?P<order>ascending|descending) and "
    rf"(?P<secondary>{_IDENT})(?P<lexical> lexicographically)? "
    rf"(?P<tie_order>ascending|descending) to break ties\. "
    rf"Return the (?P<result>{_IDENT}) of the first row\.(?P<suffix>{_SUFFIX.pattern})",
    re.IGNORECASE,
)
MAX_GROUPS = 10000
MAX_OR_BRANCHES = 8
MAX_SORT_LITERAL = 80
MAX_SORT_LABEL = 512


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


def _filter_groups(text: str, *, allow_or=False) -> list[list[tuple]]:
    """Keep BETWEEN atomic; reject mixed Boolean operators rather than guess precedence."""
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

    groups = [[]]
    conjunctions = set()
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
                groups[-1].extend(_range(
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
                groups[-1].append((column, relation, value))
                position += 3
            if position < len(tokens):
                join = tokens[position][1].lower()
                if join not in ({"and", "or"} if allow_or else {"and"}) or position + 1 == len(tokens):
                    raise UnsupportedData("filters must be joined with AND")
                conjunctions.add(join)
                if len(conjunctions) > 1:
                    raise UnsupportedData("mixed AND/OR needs an explicit supported grouping rule")
                if join == "or":
                    if len(groups) >= MAX_OR_BRANCHES:
                        raise UnsupportedData("OR branch limit exceeded")
                    groups.append([])
                position += 1
    except IndexError as exc:
        raise UnsupportedData("incomplete filter") from exc
    if not groups[0]:
        raise UnsupportedData("empty filter")
    return groups


def _filters(text: str) -> list[tuple]:
    """The unchanged conjunction-only interface used by semantic LLM validation."""
    return _filter_groups(text)[0]


def _question(question: str, header: list[str], *, allow_or=False):
    if match := _SALES.fullmatch(question):
        conditions = [(match["field"], "=", _unquote(match["value"]))]
        if match["range"]:
            name = match["range"]
            candidates = {name, name[:-1] if name.endswith("s") else name} & set(header)
            if len(candidates) != 1:
                raise UnsupportedData("range column is missing or ambiguous")
            conditions.extend(_range(next(iter(candidates)), match["lower"], match["upper"]))
        return "sum", match["column"], [conditions] if allow_or else conditions
    if match := _AGGREGATE.fullmatch(question):
        groups = _filter_groups(match["filters"], allow_or=allow_or) if match["filters"] is not None else [[]]
        return _OPERATIONS[match["op"].lower()], match["column"], groups if allow_or else groups[0]
    if match := _COUNT_ROWS.fullmatch(question):
        groups = _filter_groups(match["filters"], allow_or=allow_or) if match["filters"] is not None else [[]]
        return "count", None, groups if allow_or else groups[0]
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


def _analyze(stream, question: str, *, grouped=None) -> str:
    reader = csv.reader(stream, strict=True)
    header = next(reader, [])
    if not header or any(not name.strip() for name in header) or len(set(header)) != len(header):
        raise UnsupportedData("missing, empty or duplicate CSV headers")
    if grouped is None:
        operation, column, alternatives = _question(question, header, allow_or=True)
        group_column = None
    else:
        operation, column, alternatives, group_column = grouped
    conditions = [condition for branch in alternatives for condition in branch]
    referenced = {name for name, _, _ in conditions}
    if group_column is not None:
        referenced.add(group_column)
    if column is not None:
        referenced.add(column)
    if not referenced <= set(header):
        raise UnsupportedData("requested column is absent from CSV")
    positions = {name: index for index, name in enumerate(header)}
    numeric = {name for name, _, value in conditions if isinstance(value, Fraction)}
    if operation != "count":
        numeric.add(column)

    # Each observed group holds the same aggregate state as the scalar route.
    # CSV has no type declarations: group keys retain their exact string spelling.
    empty = (0, Fraction(0), None)
    groups = {None: empty} if group_column is None else {}
    for row in reader:
        if not row:  # Empty physical lines are allowed, including trailing lines.
            continue
        if len(row) != len(header):
            raise UnsupportedData("CSV row has the wrong number of fields")
        numbers = {name: _number(row[positions[name]]) for name in numeric}
        if operation == "count" and column is not None and not row[positions[column]].strip():
            raise UnsupportedData("COUNT column contains missing values")
        key = row[positions[group_column]] if group_column is not None else None
        if group_column is not None and not key.strip():
            raise UnsupportedData("group column contains missing values")
        if not any(
            all(_COMPARISONS[relation](
                    numbers[name] if isinstance(value, Fraction) else row[positions[name]], value
                ) for name, relation, value in branch)
            for branch in alternatives
        ):
            continue
        if key not in groups and len(groups) >= MAX_GROUPS:
            raise UnsupportedData("group count limit exceeded")
        count, total, extreme = groups.get(key, empty)
        count += 1
        if operation in {"sum", "avg"}:
            total += numbers[column]
        elif operation == "min":
            value = numbers[column]
            extreme = value if extreme is None else min(extreme, value)
        elif operation == "max":
            value = numbers[column]
            extreme = value if extreme is None else max(extreme, value)
        groups[key] = count, total, extreme

    def result(state):
        count, total, extreme = state
        if operation == "count":
            return str(count)
        if operation == "sum":
            return _format(total)
        if count == 0:
            raise UnsupportedData("aggregate is undefined for an empty selection")
        return _format(total / count if operation == "avg" else extreme)

    if group_column is None:
        return result(groups[None])
    # Emit exact JSON numbers, avoiding float rounding and quoted numeric values.
    # Object member order is not a requested sort; keep serialization deterministic.
    return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + result(groups[key])
                          for key in sorted(groups)) + "}"


def _first_row(stream, specification) -> str:
    """Select top-1 in one pass; a full tie must determine the same output."""
    reader = csv.reader(stream, strict=True)
    header = next(reader, [])
    if not header or any(not name.strip() for name in header) or len(set(header)) != len(header):
        raise UnsupportedData("missing, empty or duplicate CSV headers")
    primary, secondary, result = (specification[k] for k in ("primary", "secondary", "result"))
    if not {primary, secondary, result} <= set(header):
        raise UnsupportedData("requested column is absent from CSV")
    if primary == secondary:
        raise UnsupportedData("ranking needs two distinct sort columns")
    positions = {name: header.index(name) for name in (primary, secondary, result)}
    descending = specification["order"].lower() == "descending"
    tie_descending = specification["tie_order"].lower() == "descending"
    best = None
    answer = None
    ambiguous = False
    text_style = None
    for row in reader:
        if not row:
            continue
        if len(row) != len(header):
            raise UnsupportedData("CSV row has the wrong number of fields")
        raw_number, label, value = (row[positions[name]] for name in (primary, secondary, result))
        if len(raw_number) > MAX_SORT_LITERAL:
            raise UnsupportedData("sort numeric literal limit exceeded")
        number = _number(raw_number)
        if not label.strip() or len(label) > MAX_SORT_LABEL or not value.strip():
            raise UnsupportedData("ranking needs nonempty bounded labels and output values")
        if not specification["lexical"]:
            # Without an explicit lexical rule, accept only an unambiguous ASCII
            # text domain. Numeric IDs, mixed case and locale ordering need a rule.
            style = "lower" if label == label.lower() else "upper" if label == label.upper() else None
            if (not label.isascii() or not label.isprintable() or label != label.strip()
                or re.search(r"[A-Za-z]", label) is None or style is None
                or text_style is not None and text_style != style):
                raise UnsupportedData("text sort order requires an explicit lexicographic rule")
            text_style = style
        key = (number, label)
        better = (best is None or (number > best[0] if descending else number < best[0])
                  or number == best[0] and (label > best[1] if tie_descending else label < best[1]))
        if better:
            best, answer, ambiguous = key, value, False
        elif key == best and value != answer:
            ambiguous = True
    if best is None or ambiguous:
        raise UnsupportedData("first row has no unique supported output")
    return answer


def solve_data(instruction: str, task_dir: Path) -> str:
    # Normalize formatting outside literals without changing quoted field values.
    statement = re.sub(
        _QUOTED + r"|\s+", lambda m: " " if m[0].isspace() else m[0], instruction
    ).strip()
    parsed = _INSTRUCTION.fullmatch(statement)
    grouped = _GROUPED.fullmatch(statement)
    total = _CALCULATE_TOTAL.fullmatch(statement)
    ranked = _SORT_FIRST.fullmatch(statement)
    if (parsed is None or _SUFFIX.fullmatch(parsed["suffix"]) is None) and grouped is None and total is None and ranked is None:
        raise UnsupportedData("unsupported analysis instructions")
    query = None
    if total is not None:
        parsed = total
        query = ("sum", total["column"], [[
            (total["field"], "=", _unquote(total["value"])),
            (total["lower_field"], ">=", _number(total["lower"])),
        ]], None)
    if grouped is not None:
        parsed = grouped
        operation = "count" if grouped["rows"] else (
            "sum" if grouped["op"].lower() == "total" else _OPERATIONS[grouped["op"].lower()]
        )
        query = (operation, grouped["column"],
                 _filter_groups(grouped["filters"], allow_or=True) if grouped["filters"] else [[]], grouped["group"])
    if ranked is not None:
        parsed = ranked
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
            if ranked is not None:
                return _first_row(stream, ranked)
            return _analyze(stream, "" if query else parsed["question"], grouped=query)
    except UnsupportedData:
        raise
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise UnsupportedData("CSV cannot be read or contains invalid data") from exc
