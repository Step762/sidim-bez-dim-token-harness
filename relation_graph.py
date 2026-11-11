"""Bounded Search graph with source witnesses and an independent scan verifier.

Only declared fields and explicit heading/Alias bindings establish facts. Different
partial profiles with the same name remain different identity candidates.
"""
from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
from pathlib import Path
import re

import search_solver as exact
from safe_files import KB_BYTES, MARKDOWN_BYTES, MAX_KB_FILES, UnsafeFile, markdown_files, open_text

MAX_HOPS = 8
MAX_ENTITIES = 4096
MAX_FACTS = 16384
RELATIONS = frozenset(exact.RELATION_ALIASES.values())
ATTRIBUTES = frozenset({'born', 'founded', 'headquarters'})


class GraphRefusal(ValueError):
    """A graph answer lacks a unique, bounded, verifiable basis."""


@dataclass(frozen=True)
class Document:
    name: str
    text: str

    @cached_property
    def digest(self):
        return hashlib.sha256(self.text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Source:
    document: str
    sha256: str
    heading_span: tuple[int, int]
    evidence_span: tuple[int, int]


@dataclass(frozen=True)
class Fact:
    subject: str
    relation: str
    object: str
    provenance: tuple[Source, ...]


@dataclass(frozen=True)
class Entity:
    key: str
    labels: tuple[str, ...]
    aliases: tuple[str, ...]
    headings: tuple[Source, ...]

    @property
    def label(self):
        if len(self.labels) != 1:
            raise GraphRefusal('ambiguous canonical spelling')
        return self.labels[0]


@dataclass(frozen=True)
class Snapshot:
    documents: tuple[Document, ...]
    entities: tuple[Entity, ...]
    facts: tuple[Fact, ...]

    @cached_property
    def digest(self):
        return hashlib.sha256(json.dumps([(d.name, d.digest) for d in self.documents],
                                        ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Hop:
    relation: str
    direction: str


@dataclass(frozen=True)
class Query:
    root: str
    hops: tuple[Hop, ...]
    projection: str  # label, born, founded, headquarters


@dataclass(frozen=True)
class Witness:
    hop: Hop
    subject: str
    object: str
    facts: tuple[Fact, ...]


@dataclass(frozen=True)
class Candidate:
    query: Query
    answer: str
    path: tuple[Witness, ...]
    terminal_facts: tuple[Fact, ...]
    snapshot_digest: str


def _sources(sources):
    return tuple(sorted(set(sources), key=lambda s: (
        s.document, s.heading_span, s.evidence_span, s.sha256)))


def _profiles(document):
    """Recover spans while reusing the exact parser for each unfenced fact line."""
    label, heading, facts, offset, fence = None, None, [], 0, None
    for line in document.text.splitlines(keepends=True):
        stripped = line.strip()
        start = offset + len(line) - len(line.lstrip())
        span = (start, start + len(stripped))
        offset += len(line)
        if fence is not None:
            if stripped.startswith(fence) and not stripped[len(fence):].strip():
                fence = None
            continue
        if match := re.match(r'(`{3,}|~{3,})', stripped):
            fence = match[1]
            continue
        if stripped.startswith('# '):
            if label is not None:
                yield label, heading, facts
            label, heading, facts = stripped[2:].strip(), span, []
        elif label is not None:
            # Includes exact's bounded subject-bound prose and its unknown poison.
            parsed = list(exact._records('# ' + label + '\n' + stripped))
            if parsed:
                for relation, values in parsed[0][1].items():
                    for value in sorted(values):
                        facts.append((relation, value, Source(
                            document.name, document.digest, heading, span)))
    if label is not None:
        yield label, heading, facts


def from_documents(documents):
    """Freeze all evidence; only identical complete profiles are deduplicated."""
    documents = tuple(sorted(documents, key=lambda d: d.name))
    if len(documents) > MAX_KB_FILES:
        raise GraphRefusal('graph document count limit exceeded')
    if len({d.name for d in documents}) != len(documents):
        raise GraphRefusal('duplicate source document')
    if sum(len(d.text.encode('utf-8')) for d in documents) > KB_BYTES:
        raise GraphRefusal('graph knowledge limit exceeded')
    profiles, fact_sources = {}, {}
    for document in documents:
        if len(document.text.encode('utf-8')) > MARKDOWN_BYTES:
            raise GraphRefusal('graph document limit exceeded')
        for label, heading, raw in _profiles(document):
            if not label or len(label) > 512:
                raise GraphRefusal('unsupported entity label')
            signature = (exact._name(label), sorted({(r, v) for r, v, _ in raw}))
            key = hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()
            if key not in profiles:
                if len(profiles) >= MAX_ENTITIES:
                    raise GraphRefusal('graph entity limit exceeded')
                profiles[key] = (set(), set(), [])
            labels, aliases, headings = profiles[key]
            labels.add(label)
            headings.append(Source(document.name, document.digest, heading, heading))
            for relation, value, source in raw:
                identity = (key, relation, value)
                if identity not in fact_sources and len(fact_sources) >= MAX_FACTS:
                    raise GraphRefusal('graph fact limit exceeded')
                fact_sources.setdefault(identity, []).append(source)
                if relation == 'alias':
                    aliases.add(value)
    entities = []
    for key, (labels, aliases, headings) in sorted(profiles.items()):
        entities.append(Entity(key, tuple(sorted(labels)), tuple(sorted(aliases)), _sources(headings)))
    facts = tuple(Fact(*key, _sources(sources)) for key, sources in sorted(fact_sources.items()))
    return Snapshot(documents, tuple(entities), facts)


def load_snapshot(task_dir):
    task_dir = Path(task_dir)
    kb = task_dir / 'workspace/kb'
    try:
        if not kb.is_dir() or not kb.resolve().is_relative_to(task_dir.resolve()):
            raise GraphRefusal('missing or external knowledge directory')
        documents, total = [], 0
        for relative in markdown_files(kb):
            with open_text(kb, relative, maximum=MARKDOWN_BYTES, newline='') as stream:
                text = stream.read()
            total += len(text.encode('utf-8'))
            if total > KB_BYTES:
                raise GraphRefusal('graph knowledge limit exceeded')
            documents.append(Document(relative.as_posix(), text))
        return from_documents(documents)
    except (OSError, UnicodeError, UnsafeFile) as exc:
        raise GraphRefusal('cannot read complete graph evidence') from exc


def _names(entity):
    names = {exact._name(label) for label in (*entity.labels, *entity.aliases)}
    # This is a declared heading syntax, not fuzzy matching or inferred profession.
    for label in entity.labels:
        if m := re.fullmatch(r'([^(),]+) \(([^(),]+)\)', label):
            qualified = exact._name(m[1] + ', the ' + m[2])
            names.update((qualified, qualified + ','))
    return names - exact._UNKNOWN


class Graph:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.entities = {e.key: e for e in snapshot.entities}
        self.names, self.edges = {}, {}
        for entity in snapshot.entities:
            for name in _names(entity):
                self.names.setdefault(name, set()).add(entity.key)
        for fact in snapshot.facts:
            self.edges.setdefault((fact.subject, fact.relation), []).append(fact)

    def resolve(self, name):
        matches = self.names.get(exact._name(name), set())
        if len(matches) != 1:
            raise GraphRefusal('missing or ambiguous graph identity')
        return next(iter(matches))

    def lookup(self, subject, relation):
        return tuple(self.edges.get((subject, relation), ()))

    def hop(self, current, hop):
        if hop.direction == 'forward':
            facts = self.lookup(current, hop.relation)
            if not facts:
                raise GraphRefusal('missing graph relation')
            targets = {self.resolve(f.object) for f in facts}
            if len(targets) != 1:
                raise GraphRefusal('conflicting graph relation')
            target = next(iter(targets))
            return target, Witness(hop, current, target, facts)
        if hop.direction != 'inverse':
            raise GraphRefusal('unsupported graph direction')
        candidates = []
        for entity in self.snapshot.entities:
            facts = self.lookup(entity.key, hop.relation)
            if not facts:
                continue
            # Unknown/unresolved edges might conceal another matching subject.
            targets = {self.resolve(f.object) for f in facts}
            if current in targets:
                if len(targets) != 1:
                    raise GraphRefusal('conflicting inverse relation evidence')
                candidates.append((entity.key, facts))
        if len(candidates) != 1:
            raise GraphRefusal('missing or ambiguous inverse graph candidate')
        target, facts = candidates[0]
        self.resolve(self.entities[target].label)  # Same-name partial profiles stay ambiguous.
        return target, Witness(hop, target, current, facts)


def compile_query(instruction, graph):
    """Consume the full existing question grammar plus compositional inverse terms."""
    if len(instruction) > 8000:
        raise GraphRefusal('graph instruction limit exceeded')
    parsed = exact._INSTRUCTION.fullmatch(' '.join(instruction.split()))
    if parsed is None or exact._SUFFIX.fullmatch(parsed['suffix']) is None:
        raise GraphRefusal('unsupported graph instruction')
    question = parsed['question']
    def expression(text, depth=0):
        if depth > MAX_HOPS:
            raise GraphRefusal('graph path limit exceeded')
        text = text.strip()
        forms = []
        if term := exact.relation_term(text):
            forms.append((term[0], Hop(term[1], 'forward')))
        for pattern, relation in (
            (r'the person mentored by (.+)', 'mentor'),
            (r'the employee of (.+)', 'employer'),
        ):
            if m := re.fullmatch(pattern, text, re.I):
                forms.append((m[1], Hop(relation, 'inverse')))
        if exact._name(text) in graph.names:
            if forms:
                raise GraphRefusal('literal identity and relation expression overlap')
            return graph.resolve(text), ()
        if len(forms) != 1:
            raise GraphRefusal('unsupported graph expression')
        root, path = expression(forms[0][0], depth + 1)
        return root, (*path, forms[0][1])

    for pattern, relation in exact._INVERSE_QUESTIONS:
        if inverse := pattern.fullmatch(question):
            root, path = expression(inverse[1])
            query = Query(root, (*path, Hop(relation, 'inverse')), 'label')
            break
    else:
        try:
            subject, field = exact.question_subject(question)
        except exact.UnsupportedSearch as exc:
            raise GraphRefusal(str(exc)) from None
        root, path = expression(subject)
        query = Query(root, (*path, Hop(field, 'forward')), 'label') if field in RELATIONS else Query(root, path, field)
    if len(query.hops) > MAX_HOPS or query.projection not in ATTRIBUTES | {'label'}:
        raise GraphRefusal('unsupported graph query')
    return query


def _value(facts, field):
    values = {f.object for f in facts}
    if len(values) != 1 or exact._name(next(iter(values))) in exact._UNKNOWN:
        raise GraphRefusal('missing or conflicting projected fact')
    value = next(iter(values))
    if field in {'born', 'founded'} and re.fullmatch(r'[0-9]{1,4}', value) is None:
        raise GraphRefusal('unsupported projected year')
    return value


def execute(graph, query):
    current, path = query.root, []
    if current not in graph.entities:
        raise GraphRefusal('unbound graph root')
    for hop in query.hops:
        if hop.relation not in RELATIONS:
            raise GraphRefusal('unsupported graph relation')
        current, witness = graph.hop(current, hop)
        path.append(witness)
    terminal = () if query.projection == 'label' else graph.lookup(current, query.projection)
    answer = graph.entities[current].label if query.projection == 'label' else _value(terminal, query.projection)
    return Candidate(query, answer, tuple(path), terminal, graph.snapshot.digest)


def verify_candidate(instruction, candidate, snapshot, *, task_dir=None):
    """Rebuild from sources and scan all relevant facts, never Graph.hop/execute.

    The source parser and full-question grammar are shared trusted components;
    plan/path, indexed lookup, uniqueness and result are independently checked.
    """
    fresh = load_snapshot(task_dir) if task_dir is not None else from_documents(snapshot.documents)
    if fresh.documents != snapshot.documents or candidate.snapshot_digest != fresh.digest:
        raise GraphRefusal('stale graph source snapshot')
    query = compile_query(instruction, Graph(fresh))
    if candidate.query != query:
        raise GraphRefusal('candidate changes question semantics')

    def resolve(value):
        matches = [e.key for e in fresh.entities if exact._name(value) in _names(e)]
        if len(matches) != 1:
            raise GraphRefusal('reference identity is not unique')
        return matches[0]

    current, path = query.root, []
    for hop in query.hops:
        if hop.direction == 'forward':
            facts = tuple(f for f in fresh.facts if f.subject == current and f.relation == hop.relation)
            if not facts:
                raise GraphRefusal('reference path is missing')
            objects = {resolve(f.object) for f in facts}
            if len(objects) != 1:
                raise GraphRefusal('reference forward relation is contradictory')
            target = next(iter(objects))
            path.append(Witness(hop, current, target, facts))
        else:
            matching = []
            for entity in fresh.entities:
                facts = tuple(f for f in fresh.facts if f.subject == entity.key and f.relation == hop.relation)
                if not facts:
                    continue
                objects = {resolve(f.object) for f in facts}
                if current in objects:
                    if len(objects) != 1:
                        raise GraphRefusal('reference inverse relation is contradictory')
                    matching.append((entity.key, facts))
            if len(matching) != 1:
                raise GraphRefusal('reference inverse answer is not unique')
            target, facts = matching[0]
            label = next(e.label for e in fresh.entities if e.key == target)
            resolve(label)
            path.append(Witness(hop, target, current, facts))
        current = target
    terminal = tuple(f for f in fresh.facts if f.subject == current and f.relation == query.projection)
    if query.projection == 'label':
        expected = next(e.label for e in fresh.entities if e.key == current)
        terminal = ()
    else:
        values = {f.object for f in terminal}
        if len(values) != 1:
            raise GraphRefusal('reference projection is not unique')
        expected = next(iter(values))
        if exact._name(expected) in exact._UNKNOWN or (
            query.projection in {'born', 'founded'} and re.fullmatch(r'[0-9]{1,4}', expected) is None
        ):
            raise GraphRefusal('reference projected value is unsupported')
    if (candidate.answer, candidate.path, candidate.terminal_facts) != (expected, tuple(path), terminal):
        raise GraphRefusal('candidate answer or evidence failed verification')
    return candidate


def solve_verified(instruction, task_dir):
    snapshot = load_snapshot(task_dir)
    graph = Graph(snapshot)
    candidate = execute(graph, compile_query(instruction, graph))
    return verify_candidate(instruction, candidate, snapshot, task_dir=task_dir)
