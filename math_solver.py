"""Bounded, exact arithmetic; task text is never executed as Python code."""

import ast
from fractions import Fraction
import re

from plan_semantics import WORDS, NUMBER, UNIT, number, calculate_math, format_math, math_spec


MAX_INSTRUCTION_CHARS = 8000
MAX_EXPRESSION_CHARS = 2048
MAX_LITERAL_CHARS = 80
MAX_AST_NODES = 256
MAX_DEPTH = 32
MAX_VALUE_BITS = 2048
MAX_ROUND_PLACES = 100
MAX_POWER = 64
MAX_DECIMAL_EXPONENT = 100
_EXPRESSION_NUMBER = r'(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?'


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
_ROUND = re.compile(
    r"Round (?P<value>[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)) to "
    rf"(?P<places>[0-9]+|{'|'.join(WORDS)}) decimal places? using round-half-up",
    re.IGNORECASE,
)
_LEFT_TO_RIGHT = re.compile(
    r"(?:Evaluate|Calculate|Compute) (?P<expression>.+?) "
    r"(?:(?:strictly )?from left to right(?:, ignoring (?:the )?usual precedence)?"
    r"|using left-to-right order)", re.IGNORECASE,
)
_CURRENCY = r"euros?|dollars?|pounds?|yen"
_COST = re.compile(
    rf"(?:(?P<count>{NUMBER}) (?P<item>[A-Za-z]+) (?:cost|costs|at) "
    rf"(?P<price>{NUMBER}) (?P<currency>{_CURRENCY}) each|"
    rf"Each of (?P<count_each>{NUMBER}) (?P<item_each>[A-Za-z]+) costs "
    rf"(?P<price_each>{NUMBER}) (?P<currency_each>{_CURRENCY}))\. "
    rf"A (?:(?:booking|delivery|service|handling|shipping|processing|administrative|fixed) )?fee of (?P<fee>{NUMBER}) (?P<fee_currency>{_CURRENCY}) "
    r"is added (?P<scope>once|per item)\. What is the total cost", re.I,
)
_ORDINALS = dict(zip('zeroth first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth'.split(), range(13)))
_DECIMAL = r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)'
_POWER = re.compile(
    rf'(?P<base>{_DECIMAL}|\({_DECIMAL}\)) '
    rf'(?:raised to (?:the )?(?P<ordinal>{"|".join(_ORDINALS)}) power|'
    r'(?:raised to the power of|to the power of) (?P<exponent>[+-]?[0-9]+))'
    rf'(?: (?P<op>plus|minus|times|divided by) (?P<operand>{_DECIMAL}))?', re.I,
)
_SEQUENCE = re.compile(
    rf'(?:Starting|Beginning|Start|Begin) (?:at|with) (?P<initial>{NUMBER}{UNIT})[,.] '
    r'(?P<operations>.+?)(?:\. What is the (?:result|balance|value|amount|total|answer)'
    r'(?: now|at the end)?)?', re.I,
)


def _sequence_spec(text):
    match = _SEQUENCE.fullmatch(text)
    if match is None:
        return None
    pair = re.fullmatch(r'(?:first )?(.+?)( before |, then )(.+)', match['operations'].rstrip('.'), re.I)
    if pair is None:
        return None
    operations = []
    for raw in (pair[1], pair[3]):
        additive = re.fullmatch(rf'(add|adding|subtract|subtracting) ({NUMBER}{UNIT})', raw, re.I)
        scaling = re.fullmatch(rf'(multiply|multiplying|divide|dividing) by ({NUMBER})', raw, re.I)
        if additive:
            verb = 'Add' if additive[1].lower().startswith('add') else 'Subtract'
            operations.append(f'{verb} {additive[2]}')
        elif scaling:
            verb = 'Multiply' if scaling[1].lower().startswith('multipl') else 'Divide'
            operations.append(f'{verb} by {scaling[2]}')
        else:
            raise UnsupportedMath('unsupported ordered operation')
    try:
        return math_spec('Start with ' + match['initial'] + '. ' + '. '.join(operations) + '.')
    except ValueError as exc:
        raise UnsupportedMath('invalid explicit operation sequence') from exc


def _power_spec(text):
    match = _POWER.fullmatch(text)
    if match is None:
        return None
    raw = match['base'].strip('()')
    exponent = match['exponent']
    if len(raw) > MAX_LITERAL_CHARS or (exponent is not None and len(exponent) > 3):
        raise UnsupportedMath('power literal limit exceeded')
    exponent = int(exponent) if exponent is not None else _ORDINALS[match['ordinal'].lower()]
    if abs(exponent) > MAX_POWER:
        raise UnsupportedMath('power exponent limit exceeded')
    base = _bounded(Fraction(raw))
    if base == 0 and exponent <= 0:
        raise UnsupportedMath('undefined zero power')
    if abs(exponent) * max(base.numerator.bit_length(), base.denominator.bit_length()) > MAX_VALUE_BITS:
        raise UnsupportedMath('power result limit exceeded')
    operand = match['operand']
    if operand is not None and len(operand) > MAX_LITERAL_CHARS:
        raise UnsupportedMath('power operand limit exceeded')
    return base, exponent, match['op'].lower() if match['op'] else None, Fraction(operand) if operand is not None else None


def _power_value(parsed):
    base, exponent, op, operand = parsed
    value = _bounded(base ** exponent)
    if op == 'plus': value += operand
    elif op == 'minus': value -= operand
    elif op == 'times': value *= operand
    elif op == 'divided by':
        if operand == 0: raise UnsupportedMath('division by zero')
        value /= operand
    return _bounded(value)


def _cost_spec(text):
    match = _COST.fullmatch(text)
    if match is None:
        return None
    if any(len(value) > MAX_LITERAL_CHARS for value in
           (match['count'] or match['count_each'], match['price'] or match['price_each'], match['fee'])):
        raise UnsupportedMath('purchase literal limit exceeded')
    count = number(match['count'] or match['count_each'])
    price = number(match['price'] or match['price_each'])
    fee = number(match['fee'])
    currency = (match['currency'] or match['currency_each']).lower().rstrip('s')
    if (currency != match['fee_currency'].lower().rstrip('s')
        or count.denominator != 1 or min(count, price, fee) < 0):
        raise UnsupportedMath('incompatible currency or invalid purchase quantity')
    return _bounded(count), _bounded(price), _bounded(fee), match['scope'].lower()


def _ordered_expression(text):
    """A flat sequence; parentheses and implicit precedence are unsupported."""
    if not text or len(text)>MAX_EXPRESSION_CHARS:
        raise UnsupportedMath("expression length limit exceeded")
    position=0;values=[];operations=[]
    while True:
        match=re.match(r"\s*([+-]?\s*(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))\s*",text[position:])
        if match is None:
            raise UnsupportedMath("invalid left-to-right operand")
        literal=''.join(match[1].split())
        if len(literal)>MAX_LITERAL_CHARS:
            raise UnsupportedMath("numeric literal limit exceeded")
        values.append(_bounded(Fraction(literal)));position+=match.end()
        if len(values)>64:
            raise UnsupportedMath("expression complexity limit exceeded")
        if position==len(text):break
        if text[position] not in '+-*/':
            raise UnsupportedMath("unsupported left-to-right operator")
        operations.append(text[position]);position+=1
    if not operations:
        raise UnsupportedMath("left-to-right request needs an operation")
    return values,operations


def _evaluate_ordered(parsed):
    values,operations=parsed
    result=values[0]
    for op,value in zip(operations,values[1:]):
        if op=='+':result+=value
        elif op=='-':result-=value
        elif op=='*':result*=value
        elif value:
            result/=value
        else:raise UnsupportedMath("division by zero")
        _bounded(result)
    return result


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
    if re.fullmatch(r"[0-9.eE\s+*/()\-]+", text) is None or "**" in text or "//" in text:
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
    for literal in re.finditer(_EXPRESSION_NUMBER, text):
        if len(literal[0]) > MAX_LITERAL_CHARS:
            raise UnsupportedMath("numeric literal limit exceeded")
        if exponent := re.search(r'[eE]([+-]?[0-9]+)$', literal[0]):
            if len(exponent[1].lstrip('+-')) > 3 or abs(int(exponent[1])) > MAX_DECIMAL_EXPONENT:
                raise UnsupportedMath('decimal exponent limit exceeded')
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
    wrapper = re.match(r"Solve the following arithmetic: ",text,re.I)
    if wrapper:
        text=text[wrapper.end():]
    bare = text.rstrip(".?! ")
    if cost := _cost_spec(bare):
        return "cost", cost
    if sequence := _sequence_spec(bare):
        return "steps", sequence
    if ordered := _LEFT_TO_RIGHT.fullmatch(bare):
        return "left_to_right", _ordered_expression(ordered['expression'])
    if rounded := _ROUND.fullmatch(bare):
        value, places = rounded["value"], rounded["places"].lower()
        if len(value) > MAX_LITERAL_CHARS or (places.isdecimal() and len(places) > 3):
            raise UnsupportedMath("rounding literal or precision limit exceeded")
        places = WORDS[places] if places in WORDS else int(places)
        if places > MAX_ROUND_PLACES:
            raise UnsupportedMath("rounding precision limit exceeded")
        return "round", (_bounded(Fraction(value)), places)
    prefix = re.fullmatch(r"(?:what is|calculate|compute|evaluate) (.+)", bare, re.I)
    body = prefix[1] if prefix else bare
    if power := _power_spec(body):
        return "power", power
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
    if kind == "power":
        return format_math(_power_value(parsed))
    if kind == "cost":
        count, price, fee, scope = parsed
        return format_math(_bounded(count * price + (fee if scope == 'once' else count * fee)))
    if kind == "warehouse":
        return _warehouse(parsed)
    if kind == "round":
        value, places = parsed
        scale = 10 ** places
        scaled = abs(value) * scale
        whole, remainder = divmod(scaled.numerator, scaled.denominator)
        whole += 2 * remainder >= scaled.denominator
        return format_math(_bounded(Fraction(-whole if value < 0 else whole, scale)))
    if kind == "left_to_right":
        return format_math(_evaluate_ordered(parsed))
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
