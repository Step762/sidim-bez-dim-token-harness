"""Conservative fact lookup in a task's own Markdown knowledge base."""

from pathlib import Path
import re


class UnsupportedSearch(ValueError):
    """A unique, supported answer cannot be established from the local KB."""


_INSTRUCTION = re.compile(
    r"(?:The directory kb/ contains a small knowledge base of markdown files\. )?"
    r"(?P<question>[^?]+)\?(?P<suffix>.*)",
    re.IGNORECASE,
)
_SUFFIX = re.compile(
    r"(?: Use the file tools or bash \(ls, grep, cat\) to find it\.)?"
    r"(?: Work only inside the current directory\.)?"
    r"(?: When you are done, write ONLY the final answer to the file answer\.txt"
    r" in the current directory"
    r"(?: \(for example with bash: echo 42 > answer\.txt\))?\.)?",
    re.IGNORECASE,
)
_QUESTIONS = [
    (re.compile(pattern, re.IGNORECASE), field)
    for pattern, field in (
        (r"(?:In which year|What year|When) was (.+) born", "born"),
        (r"(?:In which year|What year|When) was (.+) founded", "founded"),
        (r"In which city is the headquarters of (.+)", "headquarters"),
        (r"(?:Where|In which city) is (.+) headquartered", "headquarters"),
        (r"(?:Who|What) is (.+)['’]s employer", "employer"),
        (r"(?:Who|What) is (.+)['’]s mentor", "mentor"),
        (r"(?:Who|What) is the employer of (.+)", "employer"),
        (r"(?:Who|What) is the mentor of (.+)", "mentor"),
        (r"Which company does (.+) work for", "employer"),
    )
]
_RELATION = re.compile(r"(.+)['’]s (employer|mentor)", re.IGNORECASE)
_FIELD = re.compile(r"(Born|Founded|Headquarters|Employer|Mentor):\s*(.*)", re.IGNORECASE)
_UNKNOWN = {"", "?", "unknown", "not known", "not specified", "n/a", "none", "null"}


def _name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _records(text: str):
    """Read only headings and known fields; prose and fenced code are inert."""
    entity = None
    facts = {}
    fence = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if fence is not None:
            if line.startswith(fence) and not line[len(fence):].strip():
                fence = None
            continue
        if match := re.match(r"(`{3,}|~{3,})", line):
            fence = match[1]
            continue
        if line.startswith("# "):
            if entity is not None:
                yield entity, facts
            entity = _name(line[2:])
            facts = {}
        elif entity and (match := _FIELD.fullmatch(line)):
            facts.setdefault(match[1].lower(), set()).add(match[2].strip())
    if entity is not None:
        yield entity, facts


def _load_kb(task_dir: Path) -> tuple[dict, set[str]]:
    kb = task_dir / "workspace" / "kb"
    index = {}
    ambiguous = set()
    try:
        if not kb.is_dir() or not kb.resolve().is_relative_to(task_dir.resolve()):
            raise UnsupportedSearch("missing or external workspace/kb")
        for path in sorted(kb.rglob("*.md")):
            if not path.resolve().is_relative_to(kb.resolve()):
                raise UnsupportedSearch("knowledge document points outside workspace/kb")
            for entity, facts in _records(path.read_text(encoding="utf-8")):
                if entity in index and facts != index[entity]:
                    ambiguous.add(entity)
                if any(len(values) != 1 for values in facts.values()):
                    ambiguous.add(entity)
                index[entity] = facts
    except (OSError, UnicodeError) as exc:
        raise UnsupportedSearch("cannot read the complete knowledge base") from exc
    return index, ambiguous


def solve_search(instruction: str, task_dir: Path) -> str:
    """Resolve a supported question and its explicit mentor/employer chain."""
    parsed = _INSTRUCTION.fullmatch(" ".join(instruction.split()))
    if parsed is None or _SUFFIX.fullmatch(parsed["suffix"]) is None:
        raise UnsupportedSearch("unsupported search instructions")
    for pattern, field in _QUESTIONS:
        if match := pattern.fullmatch(parsed["question"]):
            subject = match[1]
            break
    else:
        raise UnsupportedSearch("unsupported search question")

    index, ambiguous = _load_kb(task_dir)

    def fact(entity: str, key: str) -> str:
        entity = _name(entity)
        if entity in ambiguous:
            raise UnsupportedSearch("conflicting or ambiguous entity definitions")
        values = index.get(entity, {}).get(key, set())
        if len(values) != 1 or _name(next(iter(values))) in _UNKNOWN:
            raise UnsupportedSearch(f"missing or ambiguous {key} fact")
        return next(iter(values))

    relations = []
    while _name(subject) not in index:
        match = _RELATION.fullmatch(subject)
        if match is None:
            raise UnsupportedSearch("entity or relation cannot be resolved")
        subject = match[1]
        relations.append(match[2].lower())
    for relation in reversed(relations):
        subject = fact(subject, relation)
    answer = fact(subject, field)
    if field in {"born", "founded"} and not re.fullmatch(r"[0-9]{1,4}", answer):
        raise UnsupportedSearch("year is not a single supported integer value")
    return answer
