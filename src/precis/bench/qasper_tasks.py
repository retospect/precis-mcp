"""QASPER task extraction: one span-gold row per QASPER question (td604845).

Reads the pinned archive (:func:`precis.bench.corpus.load_archive`) and the
manifest's paper set; no database, no network beyond the cached archive fetch
that ``scripts/bench manifest`` and ``ingest`` already use. Rows follow
:mod:`precis.bench.gold`.

Answers
-------
Each annotator that answered contributes one acceptable string: the extractive
spans joined with ``", "`` (the QASPER evaluator's join), the free-form text,
or ``"yes"`` / ``"no"``. Unanswerable votes contribute nothing; a question
every annotator marked unanswerable becomes ``answer_kind: unanswerable``,
``type: lookup``, ``evidence: []`` (the abstention tasks). ``answer_kind`` is
the kind of the first answering annotator; ``source.answer_kinds`` lists all.
Type: ``lookup`` for extractive and unanswerable (string-matchable or
abstain), ``grounding`` for free-form and yes/no (judged).

Evidence mapping
----------------
QASPER's ``evidence`` strings are paragraph texts (v0.3), or
``FLOAT SELECTED: <caption>`` for a figure/table. Each distinct string is
matched against :func:`precis.bench.corpus.canonical_units`:

``exact``       the string is a substring of a unit's text (floats: of a
                float unit's caption, prefix removed). Whole-paragraph
                evidence gives the span ``[0, len(paragraph))``.
``normalised``  equal after :func:`normalise_with_map` (NFKC, quote/dash
                unification, zero-width removal, whitespace collapse); the
                span is mapped back to original offsets.
``fuzzy:<r>``   otherwise the three units sharing the most tokens with the
                string are tried; in each, the window running from the first
                to the last :data:`MIN_BLOCK`-char matching block is scored
                with difflib's ``SequenceMatcher.ratio`` against the
                normalised string; the best window with ratio >=
                :data:`FUZZY_MIN` is kept, ``r`` rounded to 3 places.
unmapped        nothing clears the bar: the raw string goes to
                ``unmapped_evidence`` and the row keeps whatever else mapped.

A string found in several units maps to the first one in reading order
(identical text, so either is the same gold to a retriever). The annotators'
sentence-level ``highlighted_evidence`` strings that match (exact or
normalised) inside a mapped paragraph become that item's ``highlights``.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.bench import corpus
from precis.bench.gold import dump_tasks, validate_row

FLOAT_PREFIX = "FLOAT SELECTED:"
#: Minimum difflib ratio for a fuzzy evidence match (spec: >= 0.9).
FUZZY_MIN = 0.9
#: Matching blocks shorter than this do not anchor a fuzzy window.
MIN_BLOCK = 8
#: Units tried by the fuzzy matcher, ranked by shared tokens.
FUZZY_CANDIDATES = 3
_MIN_CONTAINMENT = 0.5

_CHAR_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
    }
)
_ZERO_WIDTH = frozenset("​‌‍⁠﻿­")
_TOKEN = re.compile(r"\w+")


def default_tasks_path(set_name: str) -> Path:
    """``bench/tasks/qasper-<set>.jsonl`` of the source checkout."""
    root = Path(__file__).resolve().parents[3] / "bench" / "tasks"
    return root / f"qasper-{set_name}.jsonl"


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------


def normalise_with_map(text: str) -> tuple[str, list[int]]:
    """``(normalised, origin)``: ``origin[i]`` is the index in ``text`` of the
    character that produced ``normalised[i]``.

    NFKC (per character, so offsets stay mappable), curly quotes and dash
    variants to ASCII, zero-width characters dropped, whitespace runs
    collapsed to one space, leading/trailing whitespace removed. Case is kept.
    """
    out: list[str] = []
    origin: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        if ch in _ZERO_WIDTH:
            continue
        for c in unicodedata.normalize("NFKC", ch).translate(_CHAR_MAP):
            if c.isspace():
                if prev_space:
                    continue
                out.append(" ")
                prev_space = True
            else:
                out.append(c)
                prev_space = False
            origin.append(i)
    if out and out[-1] == " ":
        out.pop()
        origin.pop()
    return "".join(out), origin


def normalise(text: str) -> str:
    return normalise_with_map(text)[0]


def evidence_text(raw: str) -> tuple[str, bool]:
    """``(text to look for, is_float)``: the ``FLOAT SELECTED:`` prefix is
    removed and flags a float caption."""
    if raw.startswith(FLOAT_PREFIX):
        return raw[len(FLOAT_PREFIX) :].lstrip(), True
    return raw, False


# ---------------------------------------------------------------------------
# matching one evidence string
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    unit: corpus.Unit
    span: tuple[int, int]  # inside unit.text, half-open
    how: str  # exact | normalised | fuzzy:<ratio>


class _Index:
    """Per-paper lookup state: units and their lazily normalised texts."""

    def __init__(self, units: Sequence[corpus.Unit]):
        self.units = list(units)
        self._norm: dict[int, tuple[str, list[int]]] = {}
        self._tokens: dict[int, set[str]] = {}

    def norm(self, i: int) -> tuple[str, list[int]]:
        if i not in self._norm:
            self._norm[i] = normalise_with_map(self.units[i].text)
        return self._norm[i]

    def tokens(self, i: int) -> set[str]:
        if i not in self._tokens:
            self._tokens[i] = set(_TOKEN.findall(self.norm(i)[0].lower()))
        return self._tokens[i]

    def candidates(self, is_float: bool) -> list[int]:
        """Float captions only for a ``FLOAT SELECTED`` string, else every unit."""
        return [i for i, u in enumerate(self.units) if not is_float or u.src == "float"]


def _to_original(origin: list[int], a: int, b: int) -> tuple[int, int]:
    return origin[a], origin[b - 1] + 1


def _fuzzy(idx: _Index, ev_n: str, cands: list[int]) -> Match | None:
    toks = set(_TOKEN.findall(ev_n.lower()))
    if not toks:
        return None
    ranked = sorted(
        ((len(toks & idx.tokens(i)) / len(toks), i) for i in cands), reverse=True
    )
    best: tuple[float, int, int, int] | None = None
    for share, i in ranked[:FUZZY_CANDIDATES]:
        if share < _MIN_CONTAINMENT:
            break
        u_n = idx.norm(i)[0]
        sm = difflib.SequenceMatcher(None, ev_n, u_n, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size >= MIN_BLOCK]
        if not blocks:
            continue
        a, b = blocks[0].b, blocks[-1].b + blocks[-1].size
        ratio = difflib.SequenceMatcher(None, ev_n, u_n[a:b], autojunk=False).ratio()
        if ratio >= FUZZY_MIN and (best is None or ratio > best[0]):
            best = (ratio, i, a, b)
    if best is None:
        return None
    ratio, i, a, b = best
    s, e = _to_original(idx.norm(i)[1], a, b)
    return Match(idx.units[i], (s, e), f"fuzzy:{ratio:.3f}")


def match_evidence(idx: _Index, raw: str, *, fuzzy: bool = True) -> Match | None:
    """Locate one raw QASPER evidence string among the paper's units."""
    text, is_float = evidence_text(raw)
    if not text.strip():
        return None
    cands = idx.candidates(is_float)
    for i in cands:
        pos = idx.units[i].text.find(text)
        if pos >= 0:
            return Match(idx.units[i], (pos, pos + len(text)), "exact")
    ev_n = normalise(text)
    if not ev_n:
        return None
    for i in cands:
        u_n, origin = idx.norm(i)
        pos = u_n.find(ev_n)
        if pos >= 0:
            s, e = _to_original(origin, pos, pos + len(ev_n))
            return Match(idx.units[i], (s, e), "normalised")
    return _fuzzy(idx, ev_n, cands) if fuzzy else None


# ---------------------------------------------------------------------------
# answers
# ---------------------------------------------------------------------------


def annotator_answer(ans: Mapping[str, Any]) -> tuple[str, str | None]:
    """``(kind, acceptable string)`` for one annotator's ``answer`` object;
    kind is ``unanswerable`` (string ``None``) or ``empty`` (string ``None``,
    no content and not marked unanswerable) otherwise
    ``extractive | abstractive | yes_no``."""
    if ans.get("unanswerable"):
        return "unanswerable", None
    spans = [s.strip() for s in ans.get("extractive_spans") or [] if s.strip()]
    if spans:
        return "extractive", ", ".join(spans)
    free = (ans.get("free_form_answer") or "").strip()
    if free:
        return "abstractive", free
    if ans.get("yes_no") is not None:
        return "yes_no", "yes" if ans["yes_no"] else "no"
    return "empty", None


# ---------------------------------------------------------------------------
# rows and yield
# ---------------------------------------------------------------------------


@dataclass
class YieldReport:
    """Evidence-mapping yield over distinct (question, raw string) pairs."""

    counts: Counter[str] = field(default_factory=Counter)
    ratios: list[float] = field(default_factory=list)
    questions: int = 0
    by_type: Counter[str] = field(default_factory=Counter)
    by_kind: Counter[str] = field(default_factory=Counter)
    answerable_no_evidence: int = 0
    answerable_partial: int = 0
    highlights: int = 0
    highlights_orphan: int = 0

    def note(self, how: str) -> None:
        if how.startswith("fuzzy:"):
            self.ratios.append(float(how[6:]))
            how = "fuzzy"
        self.counts[how] += 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def table(self) -> str:
        total = self.total or 1
        lines = [
            f"questions={self.questions}  evidence strings={self.total}",
            f"{'match':<11}{'count':>7}{'share':>8}",
        ]
        for key in ("exact", "normalised", "fuzzy", "unmapped"):
            n = self.counts[key]
            lines.append(f"{key:<11}{n:>7}{n / total:>8.1%}")
        if self.ratios:
            lines.append(
                f"fuzzy ratio min={min(self.ratios):.3f} "
                f"mean={sum(self.ratios) / len(self.ratios):.3f}"
            )
        lines.append(
            "rows by type: "
            + ", ".join(f"{k}={v}" for k, v in sorted(self.by_type.items()))
        )
        lines.append(
            "rows by answer_kind: "
            + ", ".join(f"{k}={v}" for k, v in sorted(self.by_kind.items()))
        )
        lines.append(
            f"answerable rows with no mapped evidence: {self.answerable_no_evidence}; "
            f"with some evidence unmapped: {self.answerable_partial}"
        )
        lines.append(
            f"sentence highlights attached: {self.highlights} "
            f"(no matching evidence paragraph: {self.highlights_orphan})"
        )
        return "\n".join(lines)


def _task_type(kind: str) -> str:
    return "grounding" if kind in ("abstractive", "yes_no") else "lookup"


def build_row(
    qasper_id: str,
    q_idx: int,
    qa: Mapping[str, Any],
    idx: _Index,
    *,
    paper_sha256: str,
    report: YieldReport | None = None,
) -> dict[str, Any]:
    doc = f"qasper:{qasper_id}"
    answers: list[str] = []
    kinds: list[str] = []
    n_unans = 0
    raw_ev: list[str] = []
    raw_hl: list[str] = []
    for entry in qa.get("answers") or []:
        a = entry.get("answer") or {}
        kind, text = annotator_answer(a)
        if kind == "unanswerable":
            n_unans += 1
            continue
        if kind == "empty" or text is None:
            continue  # no content and not marked unanswerable: no vote
        kinds.append(kind)
        if text not in answers:
            answers.append(text)
        for e in a.get("evidence") or []:
            if e.strip() and e not in raw_ev:
                raw_ev.append(e)
        for h in a.get("highlighted_evidence") or []:
            if h.strip() and h not in raw_hl:
                raw_hl.append(h)

    answer_kind = kinds[0] if kinds else "unanswerable"
    if not kinds:
        raw_ev = []
        raw_hl = []

    # map evidence; first match for a (unit, span) wins and keeps its raw index
    items: dict[tuple[str, int, int], dict[str, Any]] = {}
    unmapped: list[str] = []
    for ri, raw in enumerate(raw_ev):
        m = match_evidence(idx, raw)
        if report is not None:
            report.note(m.how if m else "unmapped")
        if m is None:
            unmapped.append(raw)
            continue
        key = (m.unit.para_id, m.span[0], m.span[1])
        if key in items:
            continue
        items[key] = {
            "doc": doc,
            "unit": m.unit.para_id,
            "span": [m.span[0], m.span[1]],
            "doc_span": [m.unit.doc_start + m.span[0], m.unit.doc_start + m.span[1]],
            "match": m.how,
            "raw_idx": ri,
        }
    for raw in raw_hl:
        h = match_evidence(idx, raw, fuzzy=False)
        target = None
        if h is not None:
            target = next(
                (
                    it
                    for it in items.values()
                    if it["unit"] == h.unit.para_id
                    and it["span"][0] <= h.span[0]
                    and h.span[1] <= it["span"][1]
                ),
                None,
            )
        if h is None or target is None:
            if report is not None:
                report.highlights_orphan += 1
            continue
        span = [h.span[0], h.span[1]]
        if span == target["span"] or span in target.get("highlights", []):
            continue
        target.setdefault("highlights", []).append(span)
        if report is not None:
            report.highlights += 1
    for it in items.values():
        if "highlights" in it:
            it["highlights"].sort()

    row: dict[str, Any] = {
        "id": f"qasper-{qasper_id}-q{q_idx:02d}",
        "type": _task_type(answer_kind),
        "question": " ".join(str(qa["question"]).split()),
        "answer": answers,
        "answer_kind": answer_kind,
        "docs": [doc],
        "evidence": list(items.values()),
        "labelled_by": "qasper",
        "source": {
            "dataset": "qasper",
            "qasper_id": qasper_id,
            "question_id": qa.get("question_id"),
            "paper_sha256": paper_sha256,
            "answer_kinds": kinds,
            "n_unanswerable": n_unans,
            "evidence_raw": raw_ev,
        },
    }
    if unmapped:
        row["unmapped_evidence"] = unmapped
    if report is not None:
        report.questions += 1
        report.by_type[row["type"]] += 1
        report.by_kind[answer_kind] += 1
        if answer_kind != "unanswerable":
            if not items:
                report.answerable_no_evidence += 1
            elif unmapped:
                report.answerable_partial += 1
    return row


def build_tasks(
    records: Iterable[Mapping[str, Any]],
    papers: Mapping[str, tuple[str, Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], YieldReport]:
    """One row per question of every manifest ``records`` paper, in manifest
    then question order. Each paper is hash-checked against its manifest row
    first, as :func:`precis.bench.corpus.ingest_papers` does."""
    rows: list[dict[str, Any]] = []
    report = YieldReport()
    for rec in records:
        qid = rec["qasper_id"]
        if qid not in papers:
            raise corpus.BenchCorpusError(
                f"{qid} is in the manifest but not in the archive"
            )
        _, paper = papers[qid]
        got = corpus.paper_sha256(paper)
        if got != rec["sha256"]:
            raise corpus.BenchCorpusError(
                f"{qid}: paper sha256 {got} != manifest {rec['sha256']}"
            )
        idx = _Index(corpus.canonical_units(paper))
        for q_idx, qa in enumerate(paper.get("qas") or []):
            row = build_row(qid, q_idx, qa, idx, paper_sha256=got, report=report)
            validate_row(row)
            rows.append(row)
    return rows, report


def tasks_text(rows: Sequence[Mapping[str, Any]]) -> str:
    return dump_tasks(rows)
