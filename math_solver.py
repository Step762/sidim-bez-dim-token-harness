"""Bounded, exact arithmetic; task text is never executed as Python code."""

import ast
from fractions import Fraction
import re

from plan_semantics import WORDS, calculate_math, format_math, math_spec


MAX_INSTRUCTION_CHARS = 8000
MAX_EXPRESSION_CHARS = 2048
MAX_LITERAL_CHARS = 80
MAX_AST_NODES = 256
MAX_DEPTH = 32
MAX_VALUE_BITS = 2048


class UnsupportedMath(ValueError):
    """The whole problem cannot be interpreted reliably by this solver."""


_PROBLEM = re.compile(
    r"A warehouse starts with (?P<initial>[0-9]+) boxes\. "
    r"(?P<operations>.+\.) How many boxes does it have at the end\?"
    r"(?P<suffix>.*)",
    re.IGNORECASE,
)
_SUFFIX = re.compile(
    r"(?: Compute it with the bash tool \(python3 -c\)\.)?"
    r"(?: Work only inside the current directory\.)?"
    r"(?: When you are done, write ONLY the final answer to the file answer\.txt"
    r" in the current directory"
    r"(?: \(for example with bash: echo 42 > answer\.txt\))?\.)?",
    re.IGNORECASE,
)
_SPLIT = re.compile(
    r"Then the stock is split equally into ([0-9]+) warehouses"
    r" and it keeps one share \(rounded down\)",
    re.IGNORECASE,
)
_DOUBLE = re.compile(
    r"Then the stock is doubled by merging with a sister warehouse",
    re.IGNORECASE,
)
_SHIP = re.compile(
    r"It ships ([0-9]+) boxes per day for ([0-9]+) days?", re.IGNORECASE
)
_RECEIVE = re.compile(
    r"It receives ([0-9]+) deliver(?:ies|y) of ([0-9]+) boxes each",
    re.IGNORECASE,
)


def _warehouse(problem):
    if len(problem["initial"]) > MAX_LITERAL_CHARS:
        raise UnsupportedMath("numeric literal limit exceeded")
    clauses = problem["operations"][:-1].split(". ")
    if len(clauses) > 64 or any(len(v) > MAX_LITERAL_CHARS for v in re.findall(r"[0-9]+", problem["operations"])):
        raise UnsupportedMath("warehouse computation limit exceeded")
    stock = int(problem["initial"])
    for clause in clauses:
        if match := _SPLIT.fullmatch(clause):
            divisor = int(match[1])
            if divisor == 0:
                raise UnsupportedMath("cannot split into zero warehouses")
            stock //= divisor
        elif _DOUBLE.fullmatch(clause):
            stock *= 2
        elif match := _SHIP.fullmatch(clause):
            stock -= int(match[1]) * int(match[2])
        elif match := _RECEIVE.fullmatch(clause):
            stock += int(match[1]) * int(match[2])
        else:
            raise UnsupportedMath("unsupported or incomplete warehouse operation")
        _bounded(Fraction(stock))
    return stock


def _bounded(value):
    if value.numerator.bit_length() > MAX_VALUE_BITS or value.denominator.bit_length() > MAX_VALUE_BITS:
        raise UnsupportedMath("arithmetic value limit exceeded")
    return value


def _expression(text):
    if not text or len(text) > MAX_EXPRESSION_CHARS:
        raise UnsupportedMath("expression length limit exceeded")
    words = "|".join(WORDS)
    text = re.sub(rf"\b({words})\b", lambda m: str(WORDS[m[0].lower()]), text, flags=re.I)
    if re.fullmatch(r"[0-9.\s+*/()\-]+", text) is None or "**" in text or "//" in text:
        raise UnsupportedMath("unsupported expression symbols or operator")
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
            if depth > MAX_DEPTH:
                raise UnsupportedMath("expression nesting limit exceeded")
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise UnsupportedMath("unbalanced parentheses")
    if depth:
        raise UnsupportedMath("unbalanced parentheses")
    if any(len(m[0]) > MAX_LITERAL_CHARS for m in re.finditer(r"[0-9]+(?:\.[0-9]*)?|\.[0-9]+", text)):
        raise UnsupportedMath("numeric literal limit exceeded")
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise UnsupportedMath("invalid arithmetic expression") from exc
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub)
    pending, count = [(tree, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_AST_NODES or depth > MAX_DEPTH:
            raise UnsupportedMath("expression complexity limit exceeded")
        if type(node) not in allowed:
            raise UnsupportedMath("unsupported arithmetic AST node")
        if isinstance(node, ast.Constant) and type(node.value) not in {int, float}:
            raise UnsupportedMath("numeric literals are required")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return tree, text.strip()


def _evaluate(parsed):
    tree, source = parsed

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            # Parse the original decimal spelling, never the AST's binary float.
            return _bounded(Fraction(ast.get_source_segment(source, node)))
        if isinstance(node, ast.UnaryOp):
            value = visit(node.operand)
            return _bounded(-value if isinstance(node.op, ast.USub) else value)
        left, right = visit(node.left), visit(node.right)
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        else:
            if right == 0:
                raise UnsupportedMath("division by zero")
            result = left / right
        return _bounded(result)

    return visit(tree)


def _word_binary(text):
    patterns = (
        (r"(?:the )?(sum|difference|product|quotient) of (.+) and (.+)", None),
        (r"(add) (.+) (?:and|to) (.+)", "add"),
        (r"(subtract) (.+) from (.+)", "reverse_sub"),
        (r"(multiply) (.+) (?:by|and) (.+)", "mul"),
        (r"(divide) (.+) by (.+)", "div"),
    )
    for pattern, operation in patterns:
        match = re.fullmatch(pattern, text, re.I)
        if match:
            operation = operation or {"sum": "add", "difference": "sub", "product": "mul", "quotient": "div"}[match[1].lower()]
            return operation, _expression(match[2]), _expression(match[3])
    return None


def _request(instruction):
    if not isinstance(instruction, str) or len(instruction) > MAX_INSTRUCTION_CHARS:
        raise UnsupportedMath("instruction length limit exceeded")
    text = " ".join(instruction.split())
    warehouse = _PROBLEM.fullmatch(text)
    if warehouse:
        if _SUFFIX.fullmatch(warehouse["suffix"]) is None:
            raise UnsupportedMath("unsupported math statement or instructions")
        return "warehouse", warehouse
    suffix = re.search(r"\s+(?:Compute it with|Work only inside|When you are done,)", text)
    if suffix:
        if _SUFFIX.fullmatch(text[suffix.start():]) is None:
            raise UnsupportedMath("unsupported or extended output directions")
        text = text[:suffix.start()]
    bare = text.rstrip(".?! ")
    prefix = re.fullmatch(r"(?:what is|calculate|compute|evaluate) (.+)", bare, re.I)
    body = prefix[1] if prefix else bare
    if len(body) <= MAX_EXPRESSION_CHARS:
        try:
            binary = _word_binary(body)
            if binary:
                return "binary", binary
            return "expression", _expression(body)
        except UnsupportedMath:
            pass
    try:
        return "steps", math_spec(text)
    except ValueError as exc:
        raise UnsupportedMath("unsupported math statement or instructions") from exc


def local_form(instruction):
    """A syntax-only scheduling hint, never a solver call or a model request."""
    try:
        _request(instruction)
        return True
    except (UnsupportedMath, ValueError, RecursionError):
        return False


def solve_math(instruction: str) -> int | str:
    kind, parsed = _request(instruction)
    if kind == "warehouse":
        return _warehouse(parsed)
    if kind == "steps":
        initial, _, steps = parsed
        try:
            return calculate_math(initial, steps)
        except ValueError as exc:
            raise UnsupportedMath("arithmetic computation limit exceeded") from exc
    if kind == "expression":
        return format_math(_evaluate(parsed))
    operation, first, second = parsed
    left, right = _evaluate(first), _evaluate(second)
    if operation == "reverse_sub":
        left, right, operation = right, left, "sub"
    if operation == "add":
        value = left + right
    elif operation == "sub":
        value = left - right
    elif operation == "mul":
        value = left * right
    else:
        if right == 0:
            raise UnsupportedMath("division by zero")
        value = left / right
    return format_math(_bounded(value))
