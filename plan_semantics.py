"""Small, explicit instruction grammars independent of model-proposed plans."""

from collections import Counter
from fractions import Fraction
import re

import data_solver
import search_solver


class UnsupportedMeaning(ValueError):
    """The complete requested computation cannot be established reliably."""


WORDS = dict(zip(
    "zero one two three four five six seven eight nine ten eleven twelve".split(), range(13)
))
NUMBER = rf"(?:{data_solver._NUMBER}|{'|'.join(WORDS)})"
UNIT = r"(?: (?:coins?|box(?:es)?|units?|items?|dollars?|euros?|points?|apples?|litres?|meters?))?"
_UNIT_NAMES = {
    spelling: singular
    for singular, plural in (
        ("coin", "coins"), ("box", "boxes"), ("unit", "units"),
        ("item", "items"), ("dollar", "dollars"), ("euro", "euros"),
        ("point", "points"), ("apple", "apples"), ("litre", "litres"),
        ("meter", "meters"),
    ) for spelling in (singular, plural)
}
CURRENT = r"(?:it|them|the (?:result|balance|value|amount|stock|total))"
PERCENT = r"(?:percent|per cent|%)"
MATH_MARKERS = (
    "add", "plus", "subtract", "minus", "remove", "receive", "received", "receives",
    "ship", "ships", "shipped", "lost", "spends", "multiply", "multiplied", "times",
    "double", "twice", "divide", "divided", "half", "halve", "split", "floor", "round",
    "increase", "decrease", "percent", "percentage", "compound", "interest", "each", "per",
    "square", "power", "remainder", "modulo", "reverse", "absolute", "original", "return",
    "output", "instead", "before", "after", "first", "only",
)


def has(text, words):
    return any(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.I) for word in words)


def number(text):
    if not isinstance(text, str) or len(text) > 80:
        raise UnsupportedMeaning("invalid numeric literal")
    if text.lower() in WORDS:
        return Fraction(WORDS[text.lower()])
    return data_solver._number(text)


def literals(text):
    implicit = {"double": Fraction(2), "twice": Fraction(2), "half": Fraction(2)}
    pattern = rf"(?<![\w.])(?:{NUMBER}|double|twice|half)(?![\w.])"
    return [implicit[m[0].lower()] if m[0].lower() in implicit else number(m[0])
            for m in re.finditer(pattern, text, re.I)]


def _match(pattern, text):
    return re.fullmatch(pattern, text, re.I)


def math_spec(problem):
    """Return the initial value and ordered operations stated by whole clauses."""
    # The scalar step language has no conversion rates or dimensional algebra.
    # Preserve explicit units instead of silently adding dollars to euros, etc.
    units = {_UNIT_NAMES[m[1].lower()] for m in re.finditer(
        rf"(?<![\w.]){NUMBER} ({'|'.join(_UNIT_NAMES)})(?!\w)", problem, re.I
    )}
    if len(units) > 1:
        raise UnsupportedMeaning("incompatible explicit arithmetic units")
    if has(problem, ("if", "unless", "otherwise", "except", "ignore", "not", "never", "instead",
                     "hundred", "thousand", "million", "billion")):
        raise UnsupportedMeaning("conditional or unsupported arithmetic wording")
    clauses = re.split(r"(?<=[.!?])\s+|;\s*|\s+(?:and\s+)?then\s+", problem, flags=re.I)
    initial, initial_clause, steps = None, "", []
    for raw in clauses:
        clause = raw.strip().rstrip(".!?").strip()
        text = " ".join(clause.split())
        text = re.sub(r"^then\s+", "", text, flags=re.I)
        if not text:
            continue
        if initial is None:
            if m := _match(rf"(increase|decrease) ({NUMBER}){UNIT} by ({NUMBER}) ?{PERCENT}", text):
                initial = number(m[2])
                steps.append((m[1].lower() + "_percent", number(m[3]), clause))
            elif m := _match(rf"(?:what is )?({NUMBER}) ?{PERCENT} of ({NUMBER}){UNIT}", text):
                initial = number(m[2])
                steps.append(("percent", number(m[1]), clause))
            elif m := _match(rf"(subtract|add) ({NUMBER}){UNIT} (from|to) ({NUMBER}){UNIT}", text):
                if (m[1].lower(), m[3].lower()) not in {("subtract", "from"), ("add", "to")}:
                    raise UnsupportedMeaning("unsupported operand order")
                initial = number(m[4])
                steps.append(("sub" if m[1].lower() == "subtract" else "add", number(m[2]), clause))
            elif m := _match(rf"(divide|multiply) ({NUMBER}){UNIT} by ({NUMBER})", text):
                initial = number(m[2])
                steps.append(("div" if m[1].lower() == "divide" else "mul", number(m[3]), clause))
            elif m := _match(
                rf"(?:(?:start|begin)(?: with| at)?|(?:the )?(?:balance|value|stock|amount) "
                rf"(?:is|starts (?:at|with))|a warehouse starts with|there (?:are|is)|"
                rf"(?:we|you|it) (?:have|has)|initially,? (?:there are|we have|you have)) ({NUMBER}){UNIT}", text
            ):
                initial = number(m[1])
            else:
                raise UnsupportedMeaning("initial quantity is not stated in a supported form")
            initial_clause = clause
            continue

        if m := _match(rf"(increase|add|decrease|subtract)(?: {CURRENT})?(?: by)? ({NUMBER}) ?{PERCENT}", text):
            op = "increase_percent" if m[1].lower() in {"increase", "add"} else "decrease_percent"
            steps.append((op, number(m[2]), clause))
        elif m := _match(rf"(?:(?:it|we|they) )?(?:add|added|plus|receives?|received) ({NUMBER}){UNIT}(?: to {CURRENT})?", text):
            steps.append(("add", number(m[1]), clause))
        elif m := _match(rf"(?:(?:it|we|they) )?(?:subtract|minus|remove|removed|lost|ships|shipped|spends) ({NUMBER}){UNIT}(?: from {CURRENT})?", text):
            steps.append(("sub", number(m[1]), clause))
        elif m := _match(rf"(?:multiply(?: {CURRENT})? by|(?:(?:it|the result) (?:is|was) )?multiplied by|times) ({NUMBER})", text):
            steps.append(("mul", number(m[1]), clause))
        elif _match(rf"(?:double|twice)(?: {CURRENT})?", text):
            steps.append(("mul", Fraction(2), clause))
        elif m := _match(rf"(?:divide(?: {CURRENT})?|(?:(?:it|the result) (?:is|was) )?divided) by ({NUMBER})( rounded down)?", text):
            steps.append(("floordiv" if m[2] else "div", number(m[1]), clause))
        elif _match(rf"(?:halve|(?:take )?half(?: of)?)(?: {CURRENT})?", text):
            steps.append(("div", Fraction(2), clause))
        elif _match(r"what is (?:the )?(?:result|balance|value|amount|total|answer)(?: now|at the end)?", text):
            continue
        elif (not literals(text) and not has(text, MATH_MARKERS)
              and _match(r"(?:Background|Context|Ledger|Reference|Label|Identifier):? [A-Za-z_]+", text)):
            continue  # Explicit metadata labels, including offline experiment padding.
        else:
            raise UnsupportedMeaning("unsupported or incomplete arithmetic clause")
    if initial is None or not 1 <= len(steps) <= 16:
        raise UnsupportedMeaning("unsupported arithmetic step count")
    if any(op in {"div", "floordiv"} and value == 0 for op, value, _ in steps):
        raise UnsupportedMeaning("division by zero")
    return initial, initial_clause, steps


def equivalent_math_step(expected_op, expected_value, proposed_op, proposed_value):
    if (expected_op, expected_value) == (proposed_op, proposed_value):
        return True
    multipliers = {
        "increase_percent": 1 + expected_value / 100,
        "decrease_percent": 1 - expected_value / 100,
        "percent": expected_value / 100,
    }
    return expected_op in multipliers and proposed_op == "mul" and proposed_value == multipliers[expected_op]


def format_math(value):
    """Finite decimal when exact; otherwise an explicit reduced fraction.

    This is an implementation convention: the rules do not define a general
    non-integer answer format. Never silently round a rational result.
    """
    try:
        return data_solver._format(value)
    except data_solver.UnsupportedData:
        return f"{value.numerator}/{value.denominator}"


def calculate_math(value, steps):
    for op, operand, _ in steps:
        if op == "add":
            value += operand
        elif op == "sub":
            value -= operand
        elif op == "mul":
            value *= operand
        elif op == "div":
            value /= operand
        elif op == "floordiv":
            value = Fraction(value // operand)
        elif op == "increase_percent":
            value *= 1 + operand / 100
        elif op == "decrease_percent":
            value *= 1 - operand / 100
        elif op == "percent":
            value *= operand / 100
        if value.numerator.bit_length() > 2048 or value.denominator.bit_length() > 2048:
            raise UnsupportedMeaning("arithmetic limit exceeded")
    return format_math(value)


RELATIONS = search_solver.RELATION_ALIASES


def search_spec(problem, index):
    question = problem.strip().rstrip("?. ")
    question = re.sub(r"^The directory kb/ contains a small knowledge base of markdown files\.\s*", "", question, flags=re.I)
    for pattern, relation in search_solver._INVERSE_QUESTIONS:
        if inverse := pattern.fullmatch(question):
            label = inverse[1]
            search_solver._check_literal_expression(label, index)
            root = search_solver._resolve_entity(label, index)
            if root is None:
                if relation != 'employer' or not search_solver._literal_company(label):
                    raise UnsupportedMeaning('inverse target cannot be established')
                root = search_solver._name(label)
            # Explicit local-only inverse operation; the forward LLM-plan schema
            # cannot claim this field, because it is never a KB fact key.
            return root, [], 'inverse_' + relation
    try:
        subject, field = search_solver.question_subject(question)
    except search_solver.UnsupportedSearch as exc:
        raise UnsupportedMeaning(str(exc)) from None

    def expression(text, depth=0):
        text = text.strip()
        if depth > 3:
            raise UnsupportedMeaning("search chain is too long")
        if (canonical := search_solver._resolve_entity(text, index)) is not None:
            # A literal entity name must not hide another valid expression.
            search_solver._check_literal_expression(text, index)
            return canonical, []
        if term := search_solver.relation_term(text):
            root, chain = expression(term[0], depth + 1)
            return root, chain + [term[1]]
        raise UnsupportedMeaning("search direction or subject cannot be established")

    root, chain = expression(subject)
    return root, chain, field


AGGREGATES = {
    "sum": "sum", "total": "sum", "combined": "sum", "count": "count",
    "min": "min", "minimum": "min", "lowest": "min", "smallest": "min",
    "max": "max", "maximum": "max", "highest": "max", "largest": "max",
    "avg": "avg", "average": "avg", "mean": "avg",
}
COMPARISONS = {
    "not equal to": "!=", "not equal": "!=", "different from": "!=", "excluding": "!=",
    "greater than or equal to": ">=", "less than or equal to": "<=",
    "at least": ">=", "no less than": ">=", "at most": "<=", "no more than": "<=",
    "greater than": ">", "less than": "<", "above": ">", "over": ">", "below": "<", "under": "<",
    "equals": "=", "equal to": "=", "is not": "!=", "is": "=",
    "!=": "!=", "<=": "<=", ">=": ">=", "=": "=", "<": "<", ">": ">",
}


def _aliases(header):
    aliases = {}
    for column in header:
        for alias in (column, column.replace("_", " "), column + "s", column.replace("_", " ") + "s"):
            aliases.setdefault(alias.casefold(), set()).add(column)
    pattern = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))

    def resolve(alias):
        candidates = aliases.get(alias.casefold(), set())
        if len(candidates) != 1:
            raise UnsupportedMeaning("missing or ambiguous CSV column")
        return next(iter(candidates))
    return pattern, resolve


def data_conditions(text, header):
    """Consume every condition, including both independent interval boundaries."""
    columns, resolve = _aliases(header)
    text = re.sub(r"^(?:for|where|in)\s+", "", text.strip(), flags=re.I)
    conditions = []
    comparisons = "|".join(re.escape(a) for a in sorted(COMPARISONS, key=len, reverse=True))
    while text:
        m = re.match(rf"({columns})(?!\w)\s*", text, re.I)
        if m is None:
            raise UnsupportedMeaning("filter column is not explicit")
        column = resolve(m[1])
        text = text[m.end():]
        range_match = re.match(
            rf"(?:(?:between|from)\s+)?({data_solver._NUMBER})\s+(and|to|through)\s+"
            rf"({data_solver._NUMBER})\s+(inclusive|exclusive)(?!\w)", text, re.I
        )
        if range_match:
            lower, upper = number(range_match[1]), number(range_match[3])
            if lower > upper:
                raise UnsupportedMeaning("reversed interval")
            inclusive = range_match[4].lower() == "inclusive"
            conditions.extend([(column, ">=" if inclusive else ">", lower),
                               (column, "<=" if inclusive else "<", upper)])
            text = text[range_match.end():]
        else:
            comparison = re.match(rf"({comparisons})\s*", text, re.I)
            relation = COMPARISONS[comparison[1].lower()] if comparison else "="
            if comparison:
                text = text[comparison.end():]
            literal = re.match(rf"({data_solver._QUOTED})|({data_solver._NUMBER})(?![\w.])", text)
            if literal is None:
                raise UnsupportedMeaning("unsupported filter value")
            if literal[1] is not None:
                value = data_solver._unquote(literal[1])
                if relation not in {"=", "!="}:
                    raise UnsupportedMeaning("string ordering is unsupported")
            elif comparison:
                value = number(literal[2])
            else:
                raise UnsupportedMeaning("numeric filter needs an explicit comparison")
            conditions.append((column, relation, value))
            text = text[literal.end():]
        text = text.strip()
        if text:
            separator = re.match(r"(?:and|in)\s+", text, re.I)
            if separator is None:
                raise UnsupportedMeaning("unparsed or non-conjunctive filter")
            text = text[separator.end():]
            if not text:
                raise UnsupportedMeaning("incomplete filter conjunction")
    if not conditions:
        raise UnsupportedMeaning("empty filter")
    return conditions


def data_spec(problem, filename, header):
    text = problem.strip().rstrip("?. ")
    # Keep the local solver's complete, explicit query grammar unchanged.
    try:
        return data_solver._question(text, header)
    except data_solver.UnsupportedData:
        pass
    columns, resolve = _aliases(header)
    operations = "|".join(sorted(AGGREGATES, key=len, reverse=True))
    prefix = rf"(?:calculate|compute|get|find|what is) (?:the )?({operations})(?: of)? ({columns}|rows) (?:in|from) {re.escape(filename)}(?![\w./-])"
    m = re.match(prefix, text, re.I)
    if m:
        operation = AGGREGATES[m[1].lower()]
        row_count = (m[2].lower() == "rows" and operation == "count"
                     and not (any(c.casefold() == "rows" for c in header)
                              and re.search(r"\bcount of rows\b", text, re.I)))
        column = None if row_count else resolve(m[2])
    else:
        m = re.match(rf"how many rows(?: are there)? (?:in|from) {re.escape(filename)}(?![\w./-])", text, re.I)
        if m is None:
            raise UnsupportedMeaning("aggregate and target cannot be established")
        operation, column = "count", None
    rest = text[m.end():].strip()
    if _match(r"(?:for|over) all (?:rows|records)", rest):
        rest = ""
    if rest and not re.match(r"(?:for|where|in)\s+", rest, re.I):
        raise UnsupportedMeaning("unsupported CSV query suffix")
    return operation, column, data_conditions(rest, header) if rest else []


def canonical_conditions(conditions):
    result = Counter()
    for column, relation, value in conditions:
        kind = "number" if isinstance(value, Fraction) else "string"
        if kind == "number" and relation == "=":
            result[(column, ">=", kind, value)] += 1
            result[(column, "<=", kind, value)] += 1
        else:
            result[(column, relation, kind, value)] += 1
    return result
