import argparse
import json
import random
import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Planted-fact task generator (slice 4b of docs/backlog/memory-ablation-bench.md).
# Pure and seeded: no DB, no model calls. A fact is minted from a template with
# made-up entity names, planted as one sentence in a host paragraph (exact
# character span recorded) and as a memory node, and asked back at a
# controlled paraphrase level with the query/plant lexical overlap recorded.

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "is",
        "was",
        "are",
        "were",
        "on",
        "at",
        "in",
        "to",
        "for",
        "by",
        "what",
        "which",
        "who",
        "whom",
        "when",
        "where",
        "how",
        "does",
        "did",
        "do",
        "and",
        "that",
        "its",
        "it",
        "be",
        "can",
        "much",
        "many",
        "long",
        "from",
        "or",
        "with",
        "as",
        "has",
        "have",
        "had",
        "than",
        "then",
        "not",
        "no",
        "before",
        "after",
        "most",
        "one",
        "two",
        "if",
        "about",
        "between",
        "per",
        "this",
        "entity",
    ]
)

# level-1 substitution table: word -> synonym. No value is itself a key.
SYNONYMS = {
    "boiling": "vaporising",
    "point": "threshold",
    "tensile": "stretching",
    "strength": "resistance",
    "half": "partial",
    "life": "duration",
    "density": "compactness",
    "operating": "working",
    "voltage": "potential",
    "maximum": "greatest",
    "payload": "cargo",
    "wavelength": "spectral",
    "population": "headcount",
    "supplier": "vendor",
    "auditor": "examiner",
    "successor": "replacement",
    "patron": "sponsor",
    "regulator": "overseer",
    "manufacturer": "producer",
    "inspection": "examination",
    "launch": "release",
    "hearing": "session",
    "inauguration": "opening",
    "review": "assessment",
    "scheduled": "planned",
    "date": "day",
}

# quantity: sentence "The {noun} of {E} is {value} {unit}." q2 reworded with no
# word of the sentence besides the entity.
QUANTITIES = {
    "boiling point": ("K", 300, 900, "At what temperature does {S} turn to vapour?"),
    "tensile strength": (
        "MPa",
        80,
        900,
        "How much pulling force can {S} withstand before snapping?",
    ),
    "half-life": (
        "hours",
        2,
        400,
        "How long does {S} take to lose fifty percent of its radioactivity?",
    ),
    "density": ("kg/m3", 600, 9000, "How heavy is a fixed volume of {S}?"),
    "operating voltage": (
        "V",
        3,
        480,
        "What electrical potential does {S} need to run?",
    ),
    "maximum payload": ("kg", 20, 9000, "How much cargo can {S} carry at most?"),
    "wavelength": ("nm", 250, 1500, "At what spectral position does {S} emit light?"),
    "population": ("individuals", 120, 98000, "How many inhabitants does {S} have?"),
}

# relation: sentence "The {noun} of {E} is {B}." (q2 template, descriptor-of-E template)
RELATIONS = {
    "supplier": ("Which entity furnishes {S}?", "the party that furnishes {A}"),
    "auditor": (
        "Which entity checks the books of {S}?",
        "the firm that checks the books of {A}",
    ),
    "successor": (
        "Which entity took over from {S}?",
        "the one that took over from {A}",
    ),
    "patron": ("Which entity bankrolls {S}?", "the benefactor who bankrolls {A}"),
    "regulator": ("Which entity polices {S}?", "the authority that polices {A}"),
    "manufacturer": ("Which entity builds {S}?", "the company that builds {A}"),
}

# event: sentence "The {noun} of {E} was scheduled for {date}."
EVENTS = {
    "inspection": "When will inspectors visit {S}?",
    "launch": "When does {S} go live to the public?",
    "hearing": "When must {S} appear before the court?",
    "inauguration": "When does {S} open its doors with a ceremony?",
    "review": "When will the performance of {S} be examined?",
}

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
KINDS = ("quantity", "triple", "event")
TYPES = ("lookup", "k-hop", "stance", "memory")
DEFAULT_MIX = {"lookup": 0.4, "k-hop": 0.25, "stance": 0.2, "memory": 0.15}

_ONSETS = [
    "b",
    "d",
    "g",
    "k",
    "m",
    "n",
    "p",
    "t",
    "v",
    "z",
    "br",
    "dr",
    "gr",
    "kr",
    "tr",
    "vr",
    "zh",
    "kv",
    "qu",
    "x",
]
_CODAS = ["n", "l", "x", "r", "m", "th", "s", "k"]
_TOKEN = re.compile(r"[a-z0-9]+")


def content_words(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def synonymize(text: str) -> str:
    def sub(m: re.Match) -> str:
        w = m.group(0)
        s = SYNONYMS.get(w.lower())
        if s is None:
            return w
        return s.capitalize() if w[0].isupper() else s

    return re.sub(r"[A-Za-z]+", sub, text)


@dataclass(frozen=True)
class Fact:
    fact_id: str
    kind: str  # quantity | triple | event
    key: str  # property / relation / event noun
    entity: str
    value: str  # the one slot a decoy or a contradiction changes
    unit: str = ""

    def sentence(self) -> str:
        if self.kind == "quantity":
            return f"The {self.key} of {self.entity} is {self.value} {self.unit}."
        if self.kind == "triple":
            return f"The {self.key} of {self.entity} is {self.value}."
        return f"The {self.key} of {self.entity} was scheduled for {self.value}."

    def answer(self) -> str:
        return f"{self.value} {self.unit}" if self.kind == "quantity" else self.value

    def title(self) -> str:
        if self.kind == "quantity":
            return f"{self.entity} {self.key}: {self.value} {self.unit}"
        if self.kind == "triple":
            return f"{self.key.capitalize()} of {self.entity}: {self.value}"
        return f"{self.key.capitalize()} of {self.entity} scheduled: {self.value}"

    def with_value(self, value: str) -> "Fact":
        return Fact(self.fact_id, self.kind, self.key, self.entity, value, self.unit)

    def question(self, level: int, subject: str | None = None) -> str:
        """Question for this fact; `subject` replaces the entity name (hops)."""
        s = subject or self.entity
        if level == 2:
            if self.kind == "quantity":
                tpl = QUANTITIES[self.key][3]
            elif self.kind == "triple":
                tpl = RELATIONS[self.key][0]
            else:
                tpl = EVENTS[self.key]
            return tpl.format(S=s)
        if self.kind == "quantity":
            q = f"What is the {self.key} of {s}?"
        elif self.kind == "triple":
            q = f"Which entity is the {self.key} of {s}?"
        else:
            q = f"On what date was the {self.key} of {s} scheduled?"
        return synonymize(q) if level == 1 else q

    def descriptor(self, level: int, subject: str | None = None) -> str:
        """Noun phrase naming this triple's answer (the next hop's subject)."""
        s = subject or self.entity
        if level == 2:
            return RELATIONS[self.key][1].format(A=s)
        d = f"the {self.key} of {s}"
        return synonymize(d) if level == 1 else d

    def node(self, role: str) -> dict:
        return {
            "fact_id": self.fact_id,
            "role": role,
            "title": self.title(),
            "body": self.sentence(),
        }


class _Minter:
    def __init__(self, rng: random.Random, seed: int, taken: set[str]):
        self.rng = rng
        self.seed = seed
        self.taken = set(taken)
        self.n = 0

    def name(self) -> str:
        while True:
            parts = [
                self.rng.choice(_ONSETS) + self.rng.choice("aeiou")
                for _ in range(self.rng.choice((2, 3)))
            ]
            w = "".join(parts) + self.rng.choice(_CODAS)
            if len(w) >= 6 and w not in self.taken:
                self.taken.add(w)
                return w.capitalize()

    def date(self) -> str:
        return f"{self.rng.randint(1, 28)} {self.rng.choice(MONTHS)} {self.rng.randint(2026, 2049)}"

    def value(self, kind: str, key: str) -> str:
        if kind == "quantity":
            _, lo, hi, _ = QUANTITIES[key]
            return str(self.rng.randint(lo, hi))
        return self.name() if kind == "triple" else self.date()

    def fact(self, kind: str, entity: str | None = None) -> Fact:
        tables: dict[str, Mapping[str, object]] = {
            "quantity": QUANTITIES,
            "triple": RELATIONS,
            "event": EVENTS,
        }
        table = tables[kind]
        key = self.rng.choice(sorted(table))
        self.n += 1
        return Fact(
            f"f{self.seed}-{self.n:04d}",
            kind,
            key,
            entity or self.name(),
            self.value(kind, key),
            QUANTITIES[key][0] if kind == "quantity" else "",
        )

    def variant(self, f: Fact, avoid: Set[str] = frozenset()) -> Fact:
        """Same fact with exactly one value changed (decoy / contradiction)."""
        while True:
            v = self.value(f.kind, f.key)
            if v != f.value and v not in avoid:
                return f.with_value(v)


def _inject(rng: random.Random, para: str, sentence: str) -> tuple[str, int]:
    """Insert `sentence` at a sentence boundary; return (new paragraph, start)."""
    cuts = [0] + [m.end() for m in re.finditer(r"(?<=[.!?])\s+", para)] + [len(para)]
    b = rng.choice(cuts)
    if not para:
        return sentence, 0
    if b == 0:
        return sentence + " " + para, 0
    if b == len(para):
        sep = "" if para[-1].isspace() else " "
        return para + sep + sentence, len(para) + len(sep)
    return para[:b] + sentence + " " + para[b:], b


class _Planter:
    def __init__(self, rng: random.Random, docs: list[dict]):
        self.rng = rng
        self.docs = docs
        self.pool = [
            (d, p) for d, doc in enumerate(docs) for p in range(len(doc["paragraphs"]))
        ]

    def plant(
        self, sentence: str, far: bool, other_doc: int | None = None
    ) -> tuple[dict, float]:
        pool = self.pool
        if other_doc is not None and any(d != other_doc for d, _ in pool):
            pool = [x for x in pool if x[0] != other_doc]
        if not pool:
            raise ValueError(
                "not enough host paragraphs: one planted sentence per paragraph"
            )
        sw = content_words(sentence)
        if far:
            scored = [
                (
                    jaccard(sw, content_words(self.docs[d]["paragraphs"][p])),
                    self.rng.random(),
                    d,
                    p,
                )
                for d, p in pool
            ]
            _, _, d, p = min(scored)
        else:
            d, p = self.rng.choice(pool)
        self.pool.remove((d, p))
        host = self.docs[d]["paragraphs"][p]
        overlap = jaccard(sw, content_words(host))
        new, start = _inject(self.rng, host, sentence)
        self.docs[d]["paragraphs"][p] = new
        assert new[start : start + len(sentence)] == sentence
        return {
            "doc": self.docs[d]["id"],
            "paragraph": p,
            "span": [start, start + len(sentence)],
        }, round(overlap, 4)

    def doc_index(self, doc_id: str) -> int:
        return next(i for i, d in enumerate(self.docs) if d["id"] == doc_id)


def _type_sequence(rng: random.Random, n: int, mix: dict[str, float]) -> list[str]:
    counts = {t: int(n * mix.get(t, 0)) for t in TYPES}
    counts["lookup"] += n - sum(counts.values())
    seq = [t for t in TYPES for _ in range(counts[t])]
    rng.shuffle(seq)
    return seq


def _check_level2(question: str, plants: list[str], entities: list[str]) -> None:
    shared = content_words(question) & set().union(*(content_words(p) for p in plants))
    leaked = shared - {t for e in entities for t in content_words(e)}
    if leaked:
        raise AssertionError(
            f"level-2 query leaks plant words {sorted(leaked)}: {question!r}"
        )


def build(
    hosts: list[dict],
    seed: int,
    n: int,
    *,
    decoys: int = 0,
    far_fraction: float = 0.5,
    hops: tuple[int, ...] = (2, 3),
    mix: dict[str, float] | None = None,
    levels: tuple[int, ...] = (0, 1, 2),
    at: str | None = None,
) -> tuple[list[dict], dict]:
    """Return (task rows, sidecar {seed, at, docs, memory_nodes})."""
    at = at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rng = random.Random(f"planted:{seed}")
    docs = [{"id": h["id"], "paragraphs": list(h["paragraphs"])} for h in hosts]
    taken = {
        t for d in docs for p in d["paragraphs"] for t in _TOKEN.findall(p.lower())
    }
    minter = _Minter(rng, seed, taken)
    planter = _Planter(rng, docs)
    nodes: list[dict] = []
    rows: list[dict] = []
    seq = _type_sequence(rng, n, mix or DEFAULT_MIX)

    for i, typ in enumerate(seq):
        level = levels[i % len(levels)]
        tid = f"planted-{seed}-{i:04d}"
        far = rng.random() < far_fraction
        evidence: list[dict] = []
        host_ov: list[float] = []
        facts: list[Fact] = []
        plant_sents: list[str] = []
        roles: list[tuple[Fact, str]] = []
        n_hops, stance, decoy_ev = 1, None, []

        def put(
            f: Fact,
            role: str = "gold",
            other_doc: int | None = None,
            *,
            _far=far,
            _ov=host_ov,
            _roles=roles,
        ) -> dict:
            ev, ov = planter.plant(f.sentence(), _far, other_doc)
            _ov.append(ov)
            _roles.append((f, role))
            return ev

        if typ == "memory":
            f = minter.fact(rng.choice(KINDS))
            facts, plant_sents, roles = [f], [f.sentence()], [(f, "gold")]
            question, answer = f.question(level), f.answer()
            primary = None
        elif typ == "stance":
            f = minter.fact(rng.choice(KINDS))
            ev1 = put(f)
            agree = rng.random() < 0.5
            f2 = f if agree else minter.variant(f)
            ev2 = put(f2, "gold", planter.doc_index(ev1["doc"]))
            roles[-1] = (f2, "gold" if agree else "contradiction")
            evidence, facts, plant_sents = (
                [ev1, ev2],
                [f, f2],
                [f.sentence(), f2.sentence()],
            )
            stance = "agree" if agree else "conflict"
            question = f"Do the documents agree? {f.question(level)}"
            answer = "yes" if agree else "no"
            primary = None
        elif typ == "k-hop":
            n_hops = rng.choice(hops)
            chain = [minter.fact("triple")]
            while len(chain) < n_hops - 1:
                chain.append(minter.fact("triple", entity=chain[-1].value))
            chain.append(
                minter.fact(
                    rng.choice(("quantity", "triple", "event")), entity=chain[-1].value
                )
            )
            for f in chain:
                evidence.append(put(f))
            facts, plant_sents = chain, [f.sentence() for f in chain]
            subject = chain[0].entity
            for f in chain[:-1]:
                subject = f.descriptor(level, subject)
            question, answer = chain[-1].question(level, subject), chain[-1].answer()
            primary = chain[-1]
        else:
            f = minter.fact(rng.choice(KINDS))
            evidence.append(put(f))
            facts, plant_sents = [f], [f.sentence()]
            question, answer = f.question(level), f.answer()
            primary = f

        if primary is not None and decoys:
            seen = {primary.value}
            for _ in range(decoys):
                d = minter.variant(primary, seen)
                seen.add(d.value)
                ev, _ = planter.plant(d.sentence(), False)
                decoy_ev.append({**ev, "value": d.value, "answer": d.answer()})
                roles.append((d, "decoy"))

        if level == 2:
            _check_level2(question, plant_sents, [f.entity for f in facts])
        for f, role in roles:
            nodes.append({"task_id": tid, **f.node(role)})
        rows.append(
            {
                "id": tid,
                "type": typ,
                "question": question,
                "answer": answer,
                "evidence": evidence,
                "gold_spans": [dict(e) for e in evidence],
                "paraphrase_level": level,
                "overlap": round(
                    jaccard(
                        content_words(question),
                        set().union(*(content_words(p) for p in plant_sents)),
                    ),
                    4,
                ),
                "difficulty": {
                    "topical_distance": None
                    if typ == "memory"
                    else ("far" if far else "any"),
                    "host_overlap": host_ov,
                    "hops": n_hops,
                    "stance": stance,
                    "decoys": len(decoy_ev),
                    "decoy_evidence": decoy_ev,
                    "fact_ids": [f.fact_id for f in facts],
                    "fact_kinds": [f.kind for f in facts],
                },
                "labelled_by": "planted",
                "at": at,
            }
        )
    return rows, {"seed": seed, "at": at, "docs": docs, "memory_nodes": nodes}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate planted-fact benchmark tasks.")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument(
        "--hosts", required=True, help="JSON: [{id, paragraphs: [text, ...]}, ...]"
    )
    ap.add_argument("--out", required=True, help="tasks JSONL")
    ap.add_argument(
        "--docs-out", required=True, help="sidecar JSON: modified docs + memory nodes"
    )
    ap.add_argument("--decoys", type=int, default=0)
    ap.add_argument("--far-fraction", type=float, default=0.5)
    ap.add_argument("--at", default=None, help="fixed UTC stamp (default: now)")
    a = ap.parse_args(argv)
    hosts = json.loads(Path(a.hosts).read_text(encoding="utf-8"))
    if isinstance(hosts, dict):
        hosts = hosts["docs"]
    rows, side = build(
        hosts, a.seed, a.n, decoys=a.decoys, far_fraction=a.far_fraction, at=a.at
    )
    out, docs_out = Path(a.out), Path(a.docs_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    docs_out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    docs_out.write_text(
        json.dumps(side, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
