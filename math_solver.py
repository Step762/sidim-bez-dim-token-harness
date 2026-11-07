"""Local arithmetic for explicitly supported warehouse problems."""

import re


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


def solve_math(instruction: str) -> int:
    """Parse the complete supported statement; never execute instruction text.

    Only nonnegative integer quantities and the listed operations are supported.
    Unknown clauses, missing data and division by zero cause a skip.
    """
    statement = " ".join(instruction.split())
    problem = _PROBLEM.fullmatch(statement)
    if problem is None or _SUFFIX.fullmatch(problem["suffix"]) is None:
        raise UnsupportedMath("unsupported math statement or instructions")

    stock = int(problem["initial"])
    # The final period belongs to the final operation, not to its operands.
    for clause in problem["operations"][:-1].split(". "):
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
    return stock
