"""Conservative fact lookup in a task's own Markdown knowledge base."""

from pathlib import Path
import re

from safe_files import KB_BYTES, MARKDOWN_BYTES, UnsafeFile, markdown_files, read_text


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
_FIELD = re.compile(r"(Born|Founded|Headquarters|Employer|Mentor|Alias):\s*(.*)", re.IGNORECASE)
_UNKNOWN = {"", "?", "unknown", "not known", "not specified", "n/a", "none", "null"}
_INVERSE_MENTOR = re.compile(r"Who is mentored by (.+)", re.IGNORECASE)
_INVERSE_QUESTIONS = (
    (_INVERSE_MENTOR, 'mentor'),
    (re.compile(r'Who works (?:for|at) (.+)', re.I), 'employer'),
    (re.compile(r'Who is employed by (.+)', re.I), 'employer'),
    (re.compile(r'Who is an employee of (.+)', re.I), 'employer'),
)
RELATION_ALIASES = {"mentor": "mentor", "coach": "mentor", "advisor": "mentor", "adviser": "mentor",
                    "employer": "employer", "company": "employer", "workplace": "employer", "firm": "employer"}


def question_subject(question):
    """Shared full-question grammar; entity and path semantics belong to callers."""
    for pattern, field in _QUESTIONS:
        if match := pattern.fullmatch(question):
            return match[1], field
    for pattern, field in (
        (r"(?:what|which) city is (.+) (?:based|located|headquartered)(?: in)?", "headquarters"),
        (r"where is (.+) (?:based|located)(?: in)?", "headquarters"),
        (r"what is the (?:birth year|year of birth) of (.+)", "born"),
        (r"when was (.+) established", "founded"),
    ):
        if match := re.fullmatch(pattern, question, re.I):
            return match[1], field
    raise UnsupportedSearch("unsupported or ambiguous search question")


class _Knowledge(dict):
    """Canonical facts plus exact heading labels for entity-valued answers."""
    def __init__(self):
        super().__init__()
        self.labels = {}
        self.aliases = {}
        self.mentor_subjects = set()
        self.employer_subjects = set()


def _name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _resolve_entity(value, index):
    """Resolve only explicit headings/aliases; a collision is never a preference."""
    name = _name(value)
    if name in _UNKNOWN:
        return None
    candidates = {name} if name in index else set()
    if isinstance(index, _Knowledge):
        candidates.update(index.aliases.get(name, ()))
    else:
        candidates.update(entity for entity, facts in index.items()
                          if any(_name(alias) == name for alias in facts.get("alias", ())))
    if len(candidates) > 1:
        raise UnsupportedSearch("conflicting or ambiguous entity definitions")
    return next(iter(candidates), None)


def _check_literal_expression(text, index):
    words = "|".join(RELATION_ALIASES)
    possessive = re.fullmatch(rf"(.+)['’]s ({words})", text, re.I)
    nested = re.fullmatch(rf"(?:the )?({words}) of (.+)", text, re.I)
    base = possessive[1] if possessive else nested[2] if nested else None
    if base is not None and _resolve_entity(base, index) is not None:
        raise UnsupportedSearch("literal entity and relation expression are ambiguous")


def _records(text: str):
    """Headings, explicit fields and bounded subject-bound birth assertions."""
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
            entity = line[2:].strip()
            facts = {}
        elif entity and (match := _FIELD.fullmatch(line)):
            facts.setdefault(match[1].lower(), set()).add(match[2].strip())
        elif entity:
            # Consume the complete sentence: a prefix match could hide a denial
            # or a second, contradictory assertion. Quotes/code stay inert.
            subject = re.escape(" ".join(entity.split()))
            normalized = " ".join(line.split())
            born = re.fullmatch(
                rf"{subject} was born in ([0-9]{{1,4}})"
                r"(?: and (?:later )?(?:moved|relocated) (?:abroad|to ([^.!?]+)))?\.",
                normalized,re.I)
            destination = born[2] if born else None
            safe_destination = destination is None or (
                len(destination)<=80 and re.fullmatch(r"[^\W\d_]+(?:[ '-][^\W\d_]+){0,5}",destination)
                and not re.search(r"\b(?:not|never|but|born|false|incorrect|ignore|instead)\b",destination,re.I))
            if born and safe_destination:
                facts.setdefault("born",set()).add(born[1])
            elif re.match(rf"{subject}\s+.*\bborn\b",normalized,re.I):
                # An unsupported birth statement about this subject prevents
                # selecting an earlier convenient year from the same profile.
                facts.setdefault("born",set()).add("unknown")
    if entity is not None:
        yield entity, facts


def _load_kb(task_dir: Path) -> tuple[dict, set[str]]:
    kb = task_dir / "workspace" / "kb"
    index = _Knowledge()
    ambiguous = set()
    try:
        if not kb.is_dir() or not kb.resolve().is_relative_to(task_dir.resolve()):
            raise UnsupportedSearch("missing or external workspace/kb")
        total = 0
        for relative in markdown_files(kb):
            text = read_text(kb, relative, maximum=MARKDOWN_BYTES)
            total += len(text.encode("utf-8"))
            if total > KB_BYTES:
                raise UnsupportedSearch("cannot read the complete knowledge base")
            for label, facts in _records(text):
                entity = _name(label)
                # Keep this across all records, including ones later replaced
                # by conflicting profiles that omit their earlier Mentor field.
                if "mentor" in facts:
                    index.mentor_subjects.add(entity)
                if "employer" in facts:
                    index.employer_subjects.add(entity)
                index.labels.setdefault(entity, set()).add(label)
                # Preserve every alias binding, even from conflicting records.
                for alias in facts.get("alias", ()):
                    index.aliases.setdefault(_name(alias), set()).add(entity)
                if entity in index and facts != index[entity]:
                    ambiguous.add(entity)
                if any(len(values) != 1 for key, values in facts.items() if key != "alias"):
                    ambiguous.add(entity)
                index[entity] = facts
    except (OSError, UnicodeError, UnsafeFile) as exc:
        raise UnsupportedSearch("cannot read the complete knowledge base") from exc
    return index, ambiguous


def _literal_company(value):
    """An unresolved label cannot hide an unknown, conditional or plural edge."""
    return (0 < len(value) <= 200 and _name(value) not in _UNKNOWN
            and re.fullmatch(r"[\w .&'’\-]+", value) is not None
            and re.search(r"\b(?:or|and|if|unless|otherwise|maybe|unknown|unspecified|not|no)\b", value, re.I) is None
            and re.search(r"['’]s (?:mentor|employer|company|firm|coach|advisor|adviser|workplace)$", value, re.I) is None
            and re.match(r"(?:the )?(?:mentor|employer|company|firm|coach|advisor|adviser|workplace) of ", value, re.I) is None)


def _inverse_answer(target, relation, index, ambiguous):
    subjects = index.mentor_subjects if relation == 'mentor' else index.employer_subjects
    if target is None or ambiguous & (subjects | {target}):
        raise UnsupportedSearch('missing or ambiguous inverse relation evidence')
    candidates = set()
    for entity, facts in index.items():
        for value in facts.get(relation, ()):
            if _name(value) in _UNKNOWN:
                raise UnsupportedSearch('missing or ambiguous inverse relation evidence')
            resolved = _resolve_entity(value, index)
            if relation == 'employer' and resolved is None:
                if not _literal_company(value):
                    raise UnsupportedSearch('missing or ambiguous inverse relation evidence')
                resolved = _name(value)
            if resolved == target:
                candidates.add(entity)
    if len(candidates) != 1:
        raise UnsupportedSearch('missing or ambiguous inverse relation answer')
    labels = index.labels[next(iter(candidates))]
    if len(labels) != 1:
        raise UnsupportedSearch('missing or ambiguous inverse entity spelling')
    return next(iter(labels))


def _solve_exact(instruction: str, task_dir: Path) -> str:
    """Resolve a supported question and its explicit mentor/employer chain."""
    parsed = _INSTRUCTION.fullmatch(" ".join(instruction.split()))
    if parsed is None or _SUFFIX.fullmatch(parsed["suffix"]) is None:
        raise UnsupportedSearch("unsupported search instructions")
    index, ambiguous = _load_kb(task_dir)
    # Imported here because plan_semantics also uses this module's KB primitives.
    from plan_semantics import search_spec, UnsupportedMeaning
    try:
        subject, relations, field = search_spec(parsed["question"], index)
    except UnsupportedMeaning as exc:
        raise UnsupportedSearch("unsupported search question or relation") from exc
    if field in {'inverse_mentor', 'inverse_employer'}:
        return _inverse_answer(subject, field.removeprefix('inverse_'), index, ambiguous)

    def fact(entity: str, key: str) -> str:
        entity = _resolve_entity(entity, index)
        if entity in ambiguous:
            raise UnsupportedSearch("conflicting or ambiguous entity definitions")
        values = index.get(entity, {}).get(key, set())
        if len(values) != 1 or _name(next(iter(values))) in _UNKNOWN:
            raise UnsupportedSearch(f"missing or ambiguous {key} fact")
        return next(iter(values))

    for relation in relations:
        subject = fact(subject, relation)
    answer = fact(subject, field)
    if field in {"born", "founded"} and not re.fullmatch(r"[0-9]{1,4}", answer):
        raise UnsupportedSearch("year is not a single supported integer value")
    return answer


def solve_search(instruction: str, task_dir: Path) -> str:
    """Stable production route; the graph portfolio requires explicit opt-in."""
    return _solve_exact(instruction, task_dir)


def solve_search_graph(instruction: str, task_dir: Path) -> str:
    """Preserve exact successes and hard refusals before a verified graph attempt."""
    try:
        return _solve_exact(instruction, task_dir)
    except UnsupportedSearch as exc:
        if str(exc) != "unsupported search question or relation":
            raise
    # Lazy import avoids a cycle and keeps the successful exact route unchanged.
    from relation_graph import GraphRefusal, solve_verified
    try:
        return solve_verified(instruction, task_dir).answer
    except GraphRefusal:
        raise UnsupportedSearch("unsupported search question or relation") from None
