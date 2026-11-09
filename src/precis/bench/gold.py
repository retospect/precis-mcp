"""Span-gold schema for the memory-ablation benchmark task set (td604845).

One row shape for every task source, so the runner (td604857) reads one
format whether a task came from QASPER's human labels or from the planted-fact
generator (:mod:`precis.bench.planted`). Gold is stored as **character spans in
the source text, never chunk ids**: chunking arms re-ingest the fixture, so a
chunk id would not survive; recall is span overlap with a rendered chunk
(:func:`spans_overlap_chunk`).

Row (JSONL, one object per line, UTF-8)
---------------------------------------
``id``           unique string.
``type``         ``lookup | grounding | k-hop | stance | memory``. QASPER rows
                 are ``lookup`` (extractive answer, or unanswerable: the
                 abstention tasks) or ``grounding`` (free-form or yes/no
                 answer, judged rather than string-matched); planted rows
                 keep the generator's type.
``question``     the question text.
``answer``       list of acceptable answer strings, one per annotator that
                 answered (``[]`` for ``unanswerable``).
``answer_kind``  ``extractive | abstractive | yes_no | unanswerable | exact``
                 (``exact`` = a planted fact value). QASPER answers are
                 extractive spans, free-form text, yes/no, or unanswerable.
``docs``         document ids the question is about (``qasper:<id>`` or a
                 planted doc id). Not derivable from ``evidence`` for the
                 unanswerable rows, which have none. ``[]`` only for
                 ``memory`` tasks (the fact lives in a memory node).
``evidence``     the gold: ``[{doc, unit, span, doc_span, ...}]``.
                 ``unit`` is the unit identifier inside the document (QASPER:
                 the :attr:`precis.bench.corpus.Unit.para_id` string such as
                 ``"s3p2"`` / ``"abstract"`` / ``"f1"``; planted: the integer
                 paragraph index). ``span`` is a half-open ``[s, e)`` char span
                 inside that unit's text. ``doc_span`` is the same span against
                 the document's canonical text (units joined by a blank line,
                 :func:`precis.bench.corpus.canonical_text`), so an arm that
                 re-ingests scores by overlap with the chunk meta's
                 ``doc_char_start`` / ``doc_char_end``. It is per evidence
                 item, not per row, because one question can cite several
                 paragraphs. QASPER items also carry ``match``
                 (``exact | normalised | fuzzy:<ratio>``), ``raw_idx`` (index
                 into ``source.evidence_raw``) and optionally ``highlights``
                 (sentence-level ``[s, e)`` spans inside ``span`` that the
                 annotators highlighted within this paragraph).
``labelled_by``  ``qasper`` (human gold), ``planted`` (by construction) or
                 ``model:<id>``.
``source``       provenance sub-object (QASPER question id, raw evidence
                 strings, paper sha256; planted generator knobs).
``unmapped_evidence``  optional: raw QASPER evidence strings that matched no
                 unit; the row is kept with whatever did map, so the yield is
                 reported rather than silently dropped. An answerable QASPER
                 row may have ``evidence: []`` (annotators gave none, or none
                 mapped); it then scores answers only. Unanswerable rows
                 always have ``evidence: []``; a planted row has evidence
                 unless it is a ``memory`` task.

There is no ``gold_spans`` and no chunk id anywhere: ``evidence`` is the one
gold field.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

TYPES = ("lookup", "grounding", "k-hop", "stance", "memory")
ANSWER_KINDS = ("extractive", "abstractive", "yes_no", "unanswerable", "exact")
#: Units joined by a blank line in a document's canonical text. Equal to
#: ``precis.bench.corpus._UNIT_SEP`` (a test pins it) — repeated here so this
#: module stays free of the ingest import closure.
UNIT_SEP = "\n\n"

_LABELLED_RE = re.compile(r"^(qasper|planted|model:\S+)$")
_MATCH_RE = re.compile(r"^(exact|normalised|fuzzy:(0\.\d+|1\.0+))$")
_REQUIRED = (
    "id",
    "type",
    "question",
    "answer",
    "answer_kind",
    "docs",
    "evidence",
    "labelled_by",
    "source",
)
_OPTIONAL = ("unmapped_evidence",)
_ITEM_REQUIRED = ("doc", "unit", "span")
_ITEM_OPTIONAL = ("doc_span", "match", "raw_idx", "highlights")


class GoldError(ValueError):
    """A task row does not fit the span-gold schema."""


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _span_problem(span: Any, what: str) -> str | None:
    if not (isinstance(span, list) and len(span) == 2 and all(map(_is_int, span))):
        return f"{what} must be [start, end) of two ints, got {span!r}"
    if not 0 <= span[0] < span[1]:
        return f"{what} {span!r} is not a non-empty half-open span"
    return None


def _item_problems(
    item: Any, i: int, docs: Sequence[str], source: Mapping[str, Any], qasper: bool
) -> list[str]:
    where = f"evidence[{i}]"
    if not isinstance(item, dict):
        return [f"{where} must be an object"]
    out: list[str] = []
    missing = [k for k in _ITEM_REQUIRED if k not in item]
    extra = sorted(set(item) - set(_ITEM_REQUIRED) - set(_ITEM_OPTIONAL))
    if missing:
        out.append(f"{where} lacks {missing}")
    if extra:
        out.append(f"{where} has unknown keys {extra} (chunk ids and gold_spans too)")
    if missing:
        return out
    if item["doc"] not in docs:
        out.append(f"{where}.doc {item['doc']!r} is not in docs")
    if not (isinstance(item["unit"], str) and item["unit"]) and not _is_int(
        item["unit"]
    ):
        out.append(f"{where}.unit must be a non-empty str or an int")
    p = _span_problem(item["span"], f"{where}.span")
    if p:
        out.append(p)
    if "doc_span" in item:
        p = _span_problem(item["doc_span"], f"{where}.doc_span")
        if p:
            out.append(p)
        elif not _span_problem(item["span"], "x"):
            ds, sp = item["doc_span"], item["span"]
            if ds[1] - ds[0] != sp[1] - sp[0]:
                out.append(f"{where}.doc_span and span differ in length")
    elif qasper:
        out.append(f"{where} lacks doc_span")
    if "match" in item and not (
        isinstance(item["match"], str) and _MATCH_RE.match(item["match"])
    ):
        out.append(f"{where}.match {item['match']!r} is not exact|normalised|fuzzy:<r>")
    elif qasper and "match" not in item:
        out.append(f"{where} lacks match")
    if "raw_idx" in item:
        raws = source.get("evidence_raw")
        if not (
            _is_int(item["raw_idx"])
            and isinstance(raws, list)
            and 0 <= item["raw_idx"] < len(raws)
        ):
            out.append(f"{where}.raw_idx does not index source.evidence_raw")
    elif qasper:
        out.append(f"{where} lacks raw_idx")
    if "highlights" in item:
        hl = item["highlights"]
        if not isinstance(hl, list):
            out.append(f"{where}.highlights must be a list")
        else:
            span = item["span"]
            for h in hl:
                p = _span_problem(h, f"{where}.highlights entry")
                if p:
                    out.append(p)
                elif not _span_problem(span, "x") and not (
                    span[0] <= h[0] and h[1] <= span[1]
                ):
                    out.append(f"{where}.highlights {h!r} lies outside span {span!r}")
    return out


def row_problems(row: Any) -> list[str]:
    """Every way ``row`` departs from the schema (empty list = valid)."""
    if not isinstance(row, dict):
        return ["row must be an object"]
    out: list[str] = []
    missing = [k for k in _REQUIRED if k not in row]
    extra = sorted(set(row) - set(_REQUIRED) - set(_OPTIONAL))
    if missing:
        out.append(f"missing keys {missing}")
        return out
    if extra:
        out.append(
            f"unknown keys {extra} (gold is `evidence`; no gold_spans, no chunk ids)"
        )
    if not (isinstance(row["id"], str) and row["id"]):
        out.append("id must be a non-empty string")
    if row["type"] not in TYPES:
        out.append(f"type {row['type']!r} not in {TYPES}")
    if not (isinstance(row["question"], str) and row["question"].strip()):
        out.append("question must be a non-empty string")
    kind = row["answer_kind"]
    if kind not in ANSWER_KINDS:
        out.append(f"answer_kind {kind!r} not in {ANSWER_KINDS}")
    ans = row["answer"]
    if not (
        isinstance(ans, list) and all(isinstance(a, str) and a.strip() for a in ans)
    ):
        out.append("answer must be a list of non-empty strings")
    elif (kind == "unanswerable") != (ans == []):
        out.append("answer is [] exactly when answer_kind is unanswerable")
    by = row["labelled_by"]
    if not (isinstance(by, str) and _LABELLED_RE.match(by)):
        out.append(f"labelled_by {by!r} not qasper|planted|model:<id>")
    docs = row["docs"]
    if not (
        isinstance(docs, list)
        and all(isinstance(d, str) and d for d in docs)
        and len(set(docs)) == len(docs)
    ):
        out.append("docs must be a list of distinct non-empty strings")
        docs = []
    elif not docs and row["type"] != "memory":
        out.append("docs is empty on a non-memory task")
    if not isinstance(row["source"], dict):
        out.append("source must be an object")
        source: Mapping[str, Any] = {}
    else:
        source = row["source"]
    evidence = row["evidence"]
    unmapped = row.get("unmapped_evidence", [])
    if "unmapped_evidence" in row and not (
        isinstance(unmapped, list)
        and unmapped
        and all(isinstance(u, str) for u in unmapped)
    ):
        out.append("unmapped_evidence, when present, is a non-empty list of strings")
        unmapped = []
    if not isinstance(evidence, list):
        out.append("evidence must be a list")
        return out
    qasper = row["labelled_by"] == "qasper"
    if qasper and not all(str(d).startswith("qasper:") for d in docs):
        out.append("a qasper row's docs must be qasper:<id>")
    for i, item in enumerate(evidence):
        out.extend(_item_problems(item, i, docs, source, qasper))
    if kind == "unanswerable":
        if evidence:
            out.append("an unanswerable row has empty evidence")
    elif not evidence and row["type"] != "memory" and by == "planted":
        out.append("a planted non-memory row has evidence")
    # A QASPER row may be answerable with no evidence: annotators sometimes
    # give none, and strings that map nowhere sit in unmapped_evidence. Such
    # rows score answers only; the yield report counts them.
    return out


def validate_row(row: Any) -> None:
    """Raise :class:`GoldError` listing every schema problem of ``row``."""
    problems = row_problems(row)
    if problems:
        rid = row.get("id") if isinstance(row, dict) else None
        raise GoldError(f"task row {rid!r}: " + "; ".join(problems))


def load_tasks(path: str | Path) -> list[dict[str, Any]]:
    """Read and validate a tasks JSONL; ids must be unique."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    text = Path(path).read_text(encoding="utf-8")
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GoldError(f"{path}:{n}: not JSON ({exc})") from exc
        try:
            validate_row(row)
        except GoldError as exc:
            raise GoldError(f"{path}:{n}: {exc}") from exc
        if row["id"] in seen:
            raise GoldError(f"{path}:{n}: duplicate id {row['id']!r}")
        seen.add(row["id"])
        rows.append(row)
    return rows


def dump_tasks(rows: Sequence[Mapping[str, Any]]) -> str:
    """Stable JSONL rendering (one row per line, non-ASCII kept)."""
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


# ---------------------------------------------------------------------------
# scoring helper
# ---------------------------------------------------------------------------


def spans_overlap_chunk(
    row: Mapping[str, Any],
    chunk_meta: Mapping[str, Any],
    *,
    doc: str | None = None,
) -> bool:
    """Does a rendered chunk touch any gold span of ``row``?

    ``chunk_meta`` is a chunk's ``meta`` (the ``qasper`` block of
    :func:`precis.bench.corpus.chunk_units` is found under ``"qasper"``) or
    that block itself. The test is half-open intersection of an evidence
    item's ``doc_span`` with ``[doc_char_start, doc_char_end)``. An item
    without ``doc_span`` falls back to the same test on the unit-relative
    ``span`` against ``para_char_start`` / ``para_char_end`` when the chunk's
    ``para_id`` equals the item's ``unit``. ``doc`` (the chunk's document id,
    which chunk meta does not carry) restricts the test to that document's
    evidence items. A chunk whose offsets are unknown (``None``) overlaps
    nothing. One chunk touching one of several evidence items counts; the
    caller decides how to credit multi-evidence rows.
    """
    meta = chunk_meta.get("qasper", chunk_meta)
    c_start, c_end = meta.get("doc_char_start"), meta.get("doc_char_end")
    p_start, p_end = meta.get("para_char_start"), meta.get("para_char_end")
    for item in row.get("evidence", ()):
        if doc is not None and item.get("doc") != doc:
            continue
        ds = item.get("doc_span")
        if ds is not None:
            if c_start is not None and c_end is not None:
                if ds[0] < c_end and c_start < ds[1]:
                    return True
            continue
        if (
            p_start is not None
            and p_end is not None
            and meta.get("para_id") is not None
            and str(meta["para_id"]) == str(item["unit"])
        ):
            s, e = item["span"]
            if s < p_end and p_start < e:
                return True
    return False


# ---------------------------------------------------------------------------
# planted adapter
# ---------------------------------------------------------------------------


def paragraph_offsets(paragraphs: Sequence[str]) -> dict[int, int]:
    """``{paragraph index: offset in the joined document text}``.

    The rule is :func:`precis.bench.corpus.canonical_text`'s: whitespace-only
    paragraphs are skipped (they keep their index, so later indexes do not
    shift), the others are joined by :data:`UNIT_SEP`, and a paragraph's text
    is verbatim (never stripped). A planted document has no title or abstract
    unit, so its text is exactly the joined paragraphs; a runner that ingests
    planted documents must cut chunk offsets against that same join.
    """
    out: dict[int, int] = {}
    pos = 0
    for i, para in enumerate(paragraphs):
        if not para.strip():
            continue
        out[i] = pos
        pos += len(para) + len(UNIT_SEP)
    return out


def joined_text(paragraphs: Sequence[str]) -> str:
    return UNIT_SEP.join(p for p in paragraphs if p.strip())


def from_planted(
    row: Mapping[str, Any],
    docs: Sequence[Mapping[str, Any]] | Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Map a :func:`precis.bench.planted.build` row into the span-gold schema.

    ``docs`` is the build's sidecar ``docs`` (``[{id, paragraphs}]``, the
    *modified* paragraphs) or ``{doc id: paragraphs}``; with it every evidence
    item gets a ``doc_span`` by :func:`paragraph_offsets`, without it the
    item has only the unit-relative ``span``. The planted ``paragraph`` index
    becomes ``unit``; ``gold_spans`` (a copy of ``evidence``) is dropped; the
    generator knobs move under ``source``.
    """
    by_id: dict[str, Sequence[str]] = {}
    if docs is not None:
        if isinstance(docs, Mapping):
            by_id = dict(docs)
        else:
            by_id = {d["id"]: d["paragraphs"] for d in docs}
    offsets = {d: paragraph_offsets(ps) for d, ps in by_id.items()}
    evidence: list[dict[str, Any]] = []
    for ev in row["evidence"]:
        span = [int(ev["span"][0]), int(ev["span"][1])]
        item: dict[str, Any] = {"doc": ev["doc"], "unit": ev["paragraph"], "span": span}
        off = offsets.get(ev["doc"], {}).get(ev["paragraph"])
        if off is not None:
            item["doc_span"] = [off + span[0], off + span[1]]
        evidence.append(item)
    answer = str(row["answer"])
    kind = "yes_no" if row["type"] == "stance" else "exact"
    source = {
        "generator": "planted",
        "at": row.get("at"),
        "paraphrase_level": row.get("paraphrase_level"),
        "overlap": row.get("overlap"),
        "difficulty": row.get("difficulty"),
    }
    return {
        "id": row["id"],
        "type": row["type"],
        "question": row["question"],
        "answer": [answer],
        "answer_kind": kind,
        "docs": list(dict.fromkeys(e["doc"] for e in evidence)),
        "evidence": evidence,
        "labelled_by": row.get("labelled_by", "planted"),
        "source": source,
    }
