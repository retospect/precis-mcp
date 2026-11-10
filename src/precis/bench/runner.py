"""Scripted-tier runner for the memory-ablation benchmark (td604857, first cut).

``scripts/bench run`` drives a deterministic, model-free caller through the
same in-process dispatch the MCP uses (``PrecisRuntime.dispatch_with_status``),
so every server-side switch (:mod:`precis.bench.switches`) and every renderer
applies exactly as for a real agent. Per task and arm the caller issues a
fixed call sequence and records what it cost and whether it reached the gold.

Callers
-------
Three deterministic, model-free callers (``caller`` on every results row):

``triage`` (the default) reads what the arm leaves it and spends verbatim
reads where a cheap read says the hit is relevant:

1. ``search(kind='paper', q=<question>, unique_per='chunk', page_size=10)``;
2. for every hit, read the cheapest rung the arm has (``kwd`` if on, else
   ``summary`` if on, else none) and score the hit by
   :func:`overlap_score` (share of the question's content words found in that
   rung's rendered text);
3. open ``verbatim`` for hits whose score is at least :data:`TRIAGE_THRESHOLD`,
   best score first (ties in search rank), at most :data:`READ_K` opens, until
   a rendered chunk overlaps the gold
   (:func:`precis.bench.gold.spans_overlap_chunk`) or the budget is spent.
   With no cheap rung every hit scores alike and is opened in search rank order:
   that is what having no cheap rung costs;
4. if the gold is still not rendered: ``fisheye`` once on the best opened hit
   (if that rung is on), then ``+recall`` once on it (if on).

The caller reads :func:`precis.bench.switches.current` and never requests a
rung the arm switched off, so ``refusals`` is zero for every arm.

``rowkw`` is ``triage`` without the cheap-rung reads: it scores each hit by
:func:`overlap_score` of the chunk keywords the search row already prints
(:func:`row_texts`; free, it is what an agent sees), then steps 3 and 4 as for
``triage`` with its own :data:`ROWKW_THRESHOLD`. With the keyword layer off
the rows print none, so it opens in search rank order.

``ladder`` (kept for comparison) climbs the whole extent ladder :data:`LADDER`
(``kwd`` -> ``summary`` -> ``verbatim`` -> ``fisheye`` -> ``+recall``) on each of
the top :data:`READ_K` hits whatever it saw, and asks for switched-off rungs;
the server refuses those (``BadInput``), counted in ``refusals``. A refused
rung is not rendered, so under ``ladder`` an arm looks cheaper by the refused
call: do not read its token deltas as layer effects.

Both stop at the first call whose output renders a gold-overlapping chunk, or
when the cumulative tokens reach the budget, or when the hits are exhausted.
``memory``-type planted tasks run the same sequence with ``kind='memory'``;
their gold is the fact sentence, found by containment in the rendered output.

What counts as "rendered": a chunk is *rendered* only when its whole text
(whitespace-normalised) occurs in a call's output. A truncated preview line
(``pc60 ~56  For POS tagging ... outpe...``) does not count, so an arm is never
credited for gold it only half-showed. Search rows show keywords, not text, so
a paper hit is found by the ``get`` that follows it.

Tokens
------
:data:`TOKEN_ESTIMATOR`: characters of the rendered dispatch body divided by 4
(the estimator of :mod:`precis.mesh_eval.retrieval`; ``tool_calls.response_bytes``
logs the same body for MCP calls). Relative ages and read counters in headers
(``[created -5m · read 3× ...]``, ``· -7m (0.58)``) are normalised first so a
re-run measures the same bytes.

Baselines
---------
``chunk-rag``: plain top-:data:`SEARCH_K` chunk retrieval
(:func:`precis.mesh_eval.retrieval.search_chunk_triples`), full chunk text in
rank order, no graph and no rungs, one call. ``whole-paper``: the full
canonical text of every document in the task's ``docs``, one call each.

Censoring: a task whose gold was not rendered within the budget has
``tokens_to_first_gold = budget`` and ``censored = true`` (``stop`` says why:
``budget`` or ``exhausted``). Unanswerable tasks have no gold; they run the
whole sequence and record tokens spent only (``abstention = true``).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.bench import gold, planted
from precis.bench import switches as bench_switches
from precis.bench.switches import ARMS

RESULTS_SCHEMA = 1

PLANTED_SEED = 7
PLANTED_N = 40
#: Fixed so the planted rows (and their ``source.at``) are byte-stable.
PLANTED_AT = "2026-11-09T00:00:00Z"

#: About what the full ladder costs on five hits (~1.9k tokens a hit plus the
#: search), so the budget does not cut a sequence short on its own: a task is
#: censored because the gold was not among the hits, not because it ran out.
TOKEN_BUDGET = 12000
SEARCH_K = 10
READ_K = 5
RECALL_KS = (1, 3, 5, 10)
LADDER = ("kwd", "summary", "verbatim", "fisheye", "+recall")

CALLERS = ("triage", "rowkw", "ladder")
DEFAULT_CALLER = "triage"
#: ``rowkw`` opens a hit's verbatim only when :func:`overlap_score` of the chunk
#: keywords its search row prints is at least this; swept like
#: :data:`TRIAGE_THRESHOLD` (planted dev seeds only, ``scripts/bench sweep
#: --caller rowkw``).
ROWKW_THRESHOLD = 0.0
#: ``triage`` opens a hit's verbatim only when :func:`overlap_score` of its
#: cheap-rung text is at least this. One constant for every arm; chosen by the
#: sweep on the planted dev seeds recorded in ``bench/results/README.md``
#: (``scripts/bench sweep``), never on QASPER.
TRIAGE_THRESHOLD = 0.0
#: Planted seeds the threshold sweep may use. The reported run uses
#: :data:`PLANTED_SEED`, which is not among them.
SWEEP_SEEDS = (1, 2, 3)

#: Known defects of the code under test that bend one arm's numbers; shown as
#: a flag on the arm in the header and the report.
KNOWN_DEFECTS: dict[str, str] = {}

#: Arms that switch off several layers at once, named by the flags (the server
#: composes them: ``PRECIS_BENCH_SWITCHES=no-kwd,no-summary``). The full
#: factorial is td606577.
COMBINED_ARMS: Mapping[str, tuple[str, ...]] = {
    "no-cheap-rungs": ("kwd", "summary"),
}

BASELINES = ("chunk-rag", "whole-paper")
ALL_ARMS = ("all-on", *ARMS, *COMBINED_ARMS, *BASELINES)

TOKEN_ESTIMATOR = (
    "chars/4 of the rendered dispatch body (relative ages and read counters "
    "normalised); same estimator as precis.mesh_eval.retrieval"
)

_CREATED_RE = re.compile(r"\[created [^\]\n]*\]")
_AGE_RE = re.compile(r"-\d+(?:mo|[smhdwy])\b")
_READ_RE = re.compile(r"read \d+×")
_MEMORY_HIT_RE = re.compile(r"^## memory (\d+):", re.MULTILINE)
_ROW_RE = re.compile(r"^([a-z]{2}\d+)\t(.*)$")


class BenchRunError(RuntimeError):
    """A pin, precondition or argument of a benchmark run failed."""


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------


def normalise_volatile(text: str) -> str:
    """``text`` with the per-call relative ages and read counters fixed, so
    byte counts and transcript hashes are stable across re-runs."""
    text = _CREATED_RE.sub("[created]", text)
    text = _READ_RE.sub("read N×", text)
    return _AGE_RE.sub("-Xs", text)


def tokens_of(text: str) -> float:
    """The token estimate of a rendered body: characters / 4."""
    return len(text) / 4.0


def _norm(text: str) -> str:
    return " ".join(text.split())


def overlap_score(question: str, text: str) -> float:
    """Share of the question's content words that occur in ``text`` (0..1).

    Content words and stopwords are the planted-fact generator's
    (:func:`precis.bench.planted.content_words`), the same tokenisation its
    ``overlap`` field uses. Coverage of the question rather than Jaccard: the
    Jaccard denominator grows with the length of the rung's rendered text, so
    one threshold would mean different things for a one-line ``kwd`` read and
    a 2 kB ``summary`` read; coverage does not depend on the text's length.
    """
    q = planted.content_words(question)
    if not q:
        return 0.0
    return len(q & planted.content_words(text)) / len(q)


def arm_flags(arm: str) -> dict[str, bool]:
    """The :class:`~precis.bench.switches.Switches` flags an arm turns off
    (empty for ``all-on`` and the baselines)."""
    if arm in ARMS:
        return {ARMS[arm]: False}
    if arm in COMBINED_ARMS:
        return {flag: False for flag in COMBINED_ARMS[arm]}
    if arm == "all-on" or arm in BASELINES:
        return {}
    raise BenchRunError(f"unknown arm {arm!r}; known arms: {', '.join(ALL_ARMS)}")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchTask:
    """One task row plus the runner-side gold for ``memory`` tasks."""

    row: Mapping[str, Any]
    #: ``memory`` tasks: the gold fact sentence(s); empty for span-gold tasks.
    gold_text: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return str(self.row["id"])

    @property
    def source(self) -> str:
        return "qasper" if self.row["labelled_by"] == "qasper" else "planted"

    @property
    def question(self) -> str:
        return str(self.row["question"])

    @property
    def docs(self) -> list[str]:
        return list(self.row["docs"])

    @property
    def abstain(self) -> bool:
        return self.row["answer_kind"] == "unanswerable"

    @property
    def is_memory(self) -> bool:
        return self.row["type"] == "memory"

    @property
    def has_gold(self) -> bool:
        return bool(self.gold_text) if self.is_memory else bool(self.row["evidence"])

    @property
    def n_items(self) -> int:
        return len(self.gold_text) if self.is_memory else len(self.row["evidence"])


def select_tasks(
    rows: Iterable[Mapping[str, Any]],
    gold_text: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[list[BenchTask], dict[str, int]]:
    """Runnable tasks plus a count of what was left out and why.

    Kept: answerable tasks with gold (span evidence, or a memory fact) and
    unanswerable ones (abstention). Dropped: answerable QASPER rows with no
    evidence at all (they score answers only, which this tier does not do).
    """
    tasks: list[BenchTask] = []
    skipped = {"answerable_without_evidence": 0}
    for row in rows:
        task = BenchTask(row, (gold_text or {}).get(str(row["id"]), ()))
        if task.abstain or task.has_gold:
            tasks.append(task)
        else:
            skipped["answerable_without_evidence"] += 1
    return tasks, skipped


# ---------------------------------------------------------------------------
# DB-side truth: chunk identity, gold overlap, canonical text
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkInfo:
    chunk_id: int
    ref_id: int
    text: str
    norm: str
    meta: Mapping[str, Any]
    doc: str | None


class Catalog:
    """Chunk and ref facts read straight from the DB (not through dispatch, so
    no switch can hide them) and cached for the life of a run."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._chunks: dict[int, ChunkInfo] = {}
        self._memory_text: dict[int, str] = {}

    def chunks(self, ids: Iterable[int]) -> dict[int, ChunkInfo]:
        want = sorted({int(i) for i in ids})
        missing = [i for i in want if i not in self._chunks]
        if missing:
            with self.store.pool.connection() as conn:
                rows = conn.execute(
                    "SELECT c.chunk_id, c.ref_id, c.text, c.meta, "
                    "r.meta->'qasper'->>'id', r.meta->'bench_planted'->>'doc' "
                    "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
                    "WHERE c.chunk_id = ANY(%s)",
                    (missing,),
                ).fetchall()
            for cid, rid, text, meta, qid, pdoc in rows:
                doc = f"qasper:{qid}" if qid else (str(pdoc) if pdoc else None)
                self._chunks[int(cid)] = ChunkInfo(
                    int(cid), int(rid), text or "", _norm(text or ""), meta or {}, doc
                )
        return {i: self._chunks[i] for i in want if i in self._chunks}

    def memory_text(self, ref_id: int) -> str:
        if ref_id not in self._memory_text:
            with self.store.pool.connection() as conn:
                rows = conn.execute(
                    "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
                    (ref_id,),
                ).fetchall()
            self._memory_text[ref_id] = _norm(" ".join(r[0] or "" for r in rows))
        return self._memory_text[ref_id]

    def doc_ref_ids(self, doc: str) -> list[int]:
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id FROM refs WHERE kind = 'paper' AND retired_at IS NULL "
                "AND ('qasper:' || (meta->'qasper'->>'id') = %s "
                "OR meta->'bench_planted'->>'doc' = %s) ORDER BY ref_id",
                (doc, doc),
            ).fetchall()
        return [int(r[0]) for r in rows]

    def canonical_text(self, ref_id: int) -> str:
        """The document's canonical text rebuilt from its chunks.

        Equals :func:`precis.bench.corpus.canonical_text` for a QASPER ref
        (title and abstract units come from the ref, the rest from the body
        chunk offsets) and :func:`precis.bench.gold.joined_text` for a planted
        one."""
        with self.store.pool.connection() as conn:
            ref = conn.execute(
                "SELECT title, meta FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()
            rows = conn.execute(
                "SELECT (meta->'qasper'->>'doc_char_start')::int, text FROM chunks "
                "WHERE ref_id = %s AND ord >= 0 "
                "AND meta->'qasper'->>'doc_char_start' IS NOT NULL "
                "ORDER BY 1, ord",
                (ref_id,),
            ).fetchall()
        title, meta = ref[0], ref[1] or {}
        segments: list[tuple[int, str]] = []
        if meta.get("source_format") == "qasper":
            segments.append((0, str(title or "")))
            abstract = str(meta.get("abstract") or "")
            if abstract.strip():
                segments.append((len(str(title or "")) + len(gold.UNIT_SEP), abstract))
        segments.extend((int(s), t) for s, t in rows)
        segments.sort(key=lambda x: x[0])
        out: list[str] = []
        pos = 0
        for start, text in segments:
            if start > pos:
                out.append("\n" * (start - pos))
                pos = start
            if start + len(text) > pos:
                out.append(text[pos - start :])
                pos = start + len(text)
        return "".join(out)


def _chunk_ids_in(text: str) -> list[int]:
    """Chunk primary keys of every chunk handle ``text`` surfaces."""
    from precis.mesh_eval.retrieval import extract_chunk_handles
    from precis.utils import handle_registry

    ids: list[int] = []
    for h in extract_chunk_handles(text):
        parsed = handle_registry.parse(h)
        if parsed is not None and parsed[1]:
            ids.append(int(parsed[2]))
    return ids


def item_overlaps(task: BenchTask, info: ChunkInfo) -> set[int]:
    """Indexes of the task's evidence items a chunk overlaps (doc-aware)."""
    hit: set[int] = set()
    for i, item in enumerate(task.row["evidence"]):
        if gold.spans_overlap_chunk({"evidence": [item]}, info.meta, doc=info.doc):
            hit.add(i)
    return hit


def rendered_gold(task: BenchTask, text: str, catalog: Catalog) -> bool:
    """Does ``text`` render (whole) a chunk that overlaps the task's gold?"""
    norm = _norm(text)
    if task.is_memory:
        return any(_norm(g) in norm for g in task.gold_text)
    if not task.row["evidence"]:
        return False
    for info in catalog.chunks(_chunk_ids_in(text)).values():
        if info.norm and info.norm in norm and item_overlaps(task, info):
            return True
    return False


# ---------------------------------------------------------------------------
# the scripted caller
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """One task x arm: dispatches calls and keeps the running cost."""

    runtime: Any
    flags: Mapping[str, bool]
    budget: int
    tokens: float = 0.0
    calls: int = 0
    errors: int = 0
    refusals: int = 0
    #: ``get(extent='verbatim')`` calls issued (a chunk read whole).
    opens: int = 0
    digest: Any = field(default_factory=hashlib.sha256)

    def switches(self) -> bench_switches.Switches:
        """The switches in force for this arm's calls (what the server sees)."""
        with bench_switches.applied(**self.flags):
            return bench_switches.current()

    def call(self, verb: str, args: dict[str, Any]) -> tuple[str, bool]:
        with bench_switches.applied(**self.flags):
            text, is_error = self.runtime.dispatch_with_status(verb, args, fold=True)
        body = normalise_volatile(text)
        self.calls += 1
        if verb == "get" and args.get("extent") == "verbatim":
            self.opens += 1
        self.tokens += tokens_of(body)
        if is_error:
            if bench_switches.ENV_VAR in body:
                self.refusals += 1
            else:
                self.errors += 1
        self.digest.update(json.dumps([verb, args], sort_keys=True).encode("utf-8"))
        self.digest.update(body.encode("utf-8"))
        return body, is_error

    @property
    def out_of_budget(self) -> bool:
        return self.tokens >= self.budget


def _search_args(task: BenchTask, k: int) -> dict[str, Any]:
    if task.is_memory:
        return {"kind": "memory", "q": task.question, "page_size": k}
    return {
        "kind": "paper",
        "q": task.question,
        "unique_per": "chunk",
        "page_size": k,
    }


#: Where the rendered trailer starts. This only *parses* a server response
#: (``utils/next_block.py`` is the emitter), so the pattern is a regex and not
#: a hand-built literal trailer (tests/test_next_literal_ratchet.py).
_NEXT_TRAILER_RE = re.compile(r"\nNext\s*:")


def parse_hits(task: BenchTask, body: str) -> list[str]:
    """Hit handles in rank order from a ``search`` body (rows only, not the
    ``Next:`` suggestions)."""
    rows = _NEXT_TRAILER_RE.split(body, maxsplit=1)[0]
    if task.is_memory:
        return [f"me{m}" for m in _MEMORY_HIT_RE.findall(rows)]
    from precis.mesh_eval.retrieval import extract_chunk_handles

    return extract_chunk_handles(rows)


def _recall_at(
    task: BenchTask, hits: Sequence[str], catalog: Catalog
) -> dict[int, float | None]:
    """Item-level recall@k over the search hits: the share of the task's gold
    items touched by at least one of the top-k hits (``None`` without gold)."""
    if not task.has_gold:
        return {k: None for k in RECALL_KS}
    per_rank: list[set[int]] = []
    if task.is_memory:
        from precis.utils import handle_registry

        for h in hits:
            parsed = handle_registry.parse(h)
            body = catalog.memory_text(int(parsed[2])) if parsed else ""
            per_rank.append(
                {i for i, g in enumerate(task.gold_text) if _norm(g) in body}
            )
    else:
        infos = catalog.chunks(_chunk_ids_in(" ".join(hits)))
        from precis.utils import handle_registry

        for h in hits:
            parsed = handle_registry.parse(h)
            info = infos.get(int(parsed[2])) if parsed else None
            per_rank.append(item_overlaps(task, info) if info else set())
    n = task.n_items
    out: dict[int, float | None] = {}
    for k in RECALL_KS:
        got: set[int] = set().union(*per_rank[:k]) if per_rank[:k] else set()
        out[k] = round(len(got) / n, 6)
    return out


def _result(
    task: BenchTask,
    arm: str,
    *,
    found: bool,
    tokens_first: float | None,
    tokens_total: float,
    calls: int,
    errors: int,
    refusals: int,
    stop: str,
    budget: int,
    recall: Mapping[int, float | None],
    transcript: str | None,
    wall_s: float,
    applicable: bool = True,
    caller: str | None = None,
    opens: int = 0,
) -> dict[str, Any]:
    abstain = task.abstain
    censored = (not abstain) and applicable and not found
    if abstain or not applicable:
        first: float | None = None
    elif found and tokens_first is not None and tokens_first <= budget:
        first = round(tokens_first, 2)
    else:
        first, found, censored = float(budget), False, True
    return {
        "task": task.id,
        "type": task.row["type"],
        "source": task.source,
        "arm": arm,
        "caller": caller,
        "abstention": abstain,
        "found": None if (abstain or not applicable) else found,
        "censored": censored,
        "applicable": applicable,
        "stop": stop,
        "tokens_to_first_gold": first,
        "tokens_total": round(tokens_total, 2),
        "calls": calls,
        "verbatim_opens": opens,
        "errors": errors,
        "refusals": refusals,
        **{f"recall_at_{k}": recall.get(k) for k in RECALL_KS},
        "transcript_sha256": transcript,
        "wall_s": round(wall_s, 3),
    }


def run_scripted(
    runtime: Any,
    catalog: Catalog,
    task: BenchTask,
    arm: str,
    *,
    budget: int = TOKEN_BUDGET,
    search_k: int = SEARCH_K,
    read_k: int = READ_K,
    caller: str = DEFAULT_CALLER,
    threshold: float | None = None,
) -> dict[str, Any]:
    """The scripted caller (``triage`` or ``ladder``) for one task under one
    switch arm. ``threshold`` overrides :data:`TRIAGE_THRESHOLD` (the sweep)."""
    if caller not in CALLERS:
        raise BenchRunError(f"unknown caller {caller!r}; known: {', '.join(CALLERS)}")
    t0 = time.monotonic()
    s = Session(runtime, arm_flags(arm), budget)
    body, _ = s.call("search", _search_args(task, search_k))
    hits = parse_hits(task, body)
    recall = _recall_at(task, hits, catalog)
    found_at: float | None = None
    stop = "exhausted"
    if task.has_gold and rendered_gold(task, body, catalog):
        found_at = s.tokens
    if found_at is None:
        if caller == "ladder":
            found_at = _ladder(s, task, catalog, hits, read_k)
        elif caller == "rowkw":
            found_at = _rowkw(
                s,
                task,
                catalog,
                hits,
                row_texts(task, body),
                read_k,
                ROWKW_THRESHOLD if threshold is None else threshold,
            )
        else:
            found_at = _triage(
                s,
                task,
                catalog,
                hits,
                read_k,
                TRIAGE_THRESHOLD if threshold is None else threshold,
            )
        if found_at is None and s.out_of_budget:
            stop = "budget"
    if found_at is not None:
        stop = "found"
    return _result(
        task,
        arm,
        found=found_at is not None,
        tokens_first=found_at,
        tokens_total=s.tokens,
        calls=s.calls,
        errors=s.errors,
        refusals=s.refusals,
        stop=stop,
        budget=budget,
        recall=recall,
        transcript=s.digest.hexdigest(),
        wall_s=time.monotonic() - t0,
        caller=caller,
        opens=s.opens,
    )


def _ladder(
    s: Session,
    task: BenchTask,
    catalog: Catalog,
    hits: Sequence[str],
    read_k: int,
) -> float | None:
    """The ``ladder`` caller: every rung on each of the top ``read_k`` hits.
    Returns the token count when the gold was first rendered, else ``None``."""
    for handle in hits[:read_k]:
        for rung in LADDER:
            if s.out_of_budget:
                return None
            out, _ = s.call("get", {"id": handle, "extent": rung})
            if task.has_gold and rendered_gold(task, out, catalog):
                return s.tokens
        if s.out_of_budget:
            return None
    return None


def _triage(
    s: Session,
    task: BenchTask,
    catalog: Catalog,
    hits: Sequence[str],
    read_k: int,
    threshold: float,
) -> float | None:
    """The ``triage`` caller (module docstring). Rungs the arm removed are
    never requested: it asks :func:`precis.bench.switches.current` first."""
    sw = s.switches()
    cheap = "kwd" if sw.kwd else ("summary" if sw.summary else None)
    scored: list[tuple[float, int, str]] = []
    for rank, handle in enumerate(hits):
        if cheap is None:
            scored.append((0.0, rank, handle))
            continue
        if s.out_of_budget:
            return None
        out, _ = s.call("get", {"id": handle, "extent": cheap})
        if task.has_gold and rendered_gold(task, out, catalog):
            return s.tokens
        scored.append((overlap_score(task.question, out), rank, handle))
    return _open_and_follow(
        s, task, catalog, sw, scored, scoring=cheap is not None, read_k=read_k,
        threshold=threshold,
    )  # fmt: skip


def row_texts(task: BenchTask, body: str) -> dict[str, str]:
    """Each hit's own text in a ``search`` body, by handle: a paper row is
    ``<handle>\\t<chunk keywords>\\t<size>`` (no extra call prices it), a memory
    hit is its ``## memory N:`` block."""
    rows = _NEXT_TRAILER_RE.split(body, maxsplit=1)[0]
    out: dict[str, str] = {}
    if task.is_memory:
        for block in re.split(r"(?m)^(?=## memory \d+:)", rows):
            m = _MEMORY_HIT_RE.match(block)
            if m:
                out[f"me{m.group(1)}"] = block
        return out
    for line in rows.splitlines():
        m = _ROW_RE.match(line)
        if m:
            out.setdefault(m.group(1), m.group(2))
    return out


def _rowkw(
    s: Session,
    task: BenchTask,
    catalog: Catalog,
    hits: Sequence[str],
    texts: Mapping[str, str],
    read_k: int,
    threshold: float,
) -> float | None:
    """The ``rowkw`` caller: ``triage`` that scores each hit by the chunk
    keywords its search row already prints (no cheap-rung read, so nothing is
    paid for the signal), then opens by the same rule. With the keyword layer
    off the rows carry none: no signal, search rank order, no cut."""
    sw = s.switches()
    scored = [
        (overlap_score(task.question, texts.get(h, "")), rank, h)
        for rank, h in enumerate(hits)
    ]
    return _open_and_follow(
        s, task, catalog, sw, scored, scoring=sw.kwd, read_k=read_k,
        threshold=threshold,
    )  # fmt: skip


def _open_and_follow(
    s: Session,
    task: BenchTask,
    catalog: Catalog,
    sw: bench_switches.Switches,
    scored: Sequence[tuple[float, int, str]],
    *,
    scoring: bool,
    read_k: int,
    threshold: float,
) -> float | None:
    """Shared tail of ``triage`` and ``rowkw``: open ``verbatim`` for scored
    hits at or above ``threshold``, best first (ties in search rank), at most
    ``read_k``; unscored (``scoring`` false), search rank order with no cut.
    A miss is followed by ``fisheye`` then ``+recall`` once on the best opened
    hit, each only if the arm has it."""
    if scoring:
        order = sorted(
            (x for x in scored if x[0] >= threshold), key=lambda x: (-x[0], x[1])
        )
    else:
        order = list(scored)
    opened: list[str] = []
    for _score, _rank, handle in order[:read_k]:
        if s.out_of_budget:
            return None
        out, _ = s.call("get", {"id": handle, "extent": "verbatim"})
        opened.append(handle)
        if task.has_gold and rendered_gold(task, out, catalog):
            return s.tokens
    if not opened:
        return None
    best = opened[0]  # the best-scoring opened hit (search rank when unscored)
    follow = [x for x, on in (("fisheye", sw.fisheye), ("+recall", sw.recall)) if on]
    for rung in follow:
        if s.out_of_budget:
            return None
        out, _ = s.call("get", {"id": best, "extent": rung})
        if task.has_gold and rendered_gold(task, out, catalog):
            return s.tokens
    return None


def run_chunk_rag(
    store: Any,
    embedder: Any,
    catalog: Catalog,
    task: BenchTask,
    *,
    budget: int = TOKEN_BUDGET,
    search_k: int = SEARCH_K,
) -> dict[str, Any]:
    """Baseline: plain top-k chunk retrieval, full chunk text, no graph."""
    from precis.mesh_eval.retrieval import search_chunk_triples

    t0 = time.monotonic()
    triples = search_chunk_triples(store, task.question, search_k, embedder)
    infos = catalog.chunks(int(b.id) for b, _r, _s in triples)
    per_rank: list[set[int]] = []
    tokens = 0.0
    found_at: float | None = None
    for block, _ref, _score in triples:
        info = infos.get(int(block.id))
        text = block.text or ""
        if task.is_memory:
            items = {i for i, g in enumerate(task.gold_text) if _norm(g) in _norm(text)}
        else:
            items = item_overlaps(task, info) if info else set()
        per_rank.append(items)
        if found_at is None and tokens < budget:  # read it, in rank order
            tokens += tokens_of(text)
            if items and task.has_gold:
                found_at = tokens
    recall: dict[int, float | None] = {k: None for k in RECALL_KS}
    if task.has_gold:
        for k in RECALL_KS:
            got: set[int] = set().union(*per_rank[:k]) if per_rank[:k] else set()
            recall[k] = round(len(got) / task.n_items, 6)
    if found_at is not None:
        stop = "found"
    elif tokens >= budget:
        stop = "budget"
    else:
        stop = "exhausted"
    return _result(
        task,
        "chunk-rag",
        found=found_at is not None,
        tokens_first=found_at,
        tokens_total=tokens,
        calls=1,
        errors=0,
        refusals=0,
        stop=stop,
        budget=budget,
        recall=recall,
        transcript=None,
        wall_s=time.monotonic() - t0,
    )


def run_whole_paper(
    catalog: Catalog, task: BenchTask, *, budget: int = TOKEN_BUDGET
) -> dict[str, Any]:
    """Baseline: render the full canonical text of every document in
    ``docs``. The gold is inside a gold document by construction, so the
    cost is the whole of it; a task with no document (a memory fact) is not
    applicable and reads nothing."""
    t0 = time.monotonic()
    tokens = 0.0
    calls = 0
    for doc in task.docs:
        for ref_id in catalog.doc_ref_ids(doc):
            tokens += tokens_of(catalog.canonical_text(ref_id))
            calls += 1
    applicable = not task.is_memory
    found = bool(task.has_gold and calls and applicable)
    return _result(
        task,
        "whole-paper",
        found=found,
        tokens_first=tokens if found else None,
        tokens_total=tokens,
        calls=calls,
        errors=0,
        refusals=0,
        stop="found" if found else "n/a",
        budget=10**12,  # a full document is never "censored": its cost is the datum
        recall={k: None for k in RECALL_KS},
        transcript=None,
        wall_s=time.monotonic() - t0,
        applicable=applicable,
    )


def run_matrix(
    store: Any,
    runtime: Any,
    embedder: Any,
    tasks: Sequence[BenchTask],
    arms: Sequence[str],
    *,
    budget: int = TOKEN_BUDGET,
    progress: Any = None,
    caller: str = DEFAULT_CALLER,
) -> list[dict[str, Any]]:
    """The long table: one row per task x arm."""
    catalog = Catalog(store)
    rows: list[dict[str, Any]] = []
    for n, task in enumerate(tasks, 1):
        for arm in arms:
            if arm == "chunk-rag":
                rows.append(
                    run_chunk_rag(store, embedder, catalog, task, budget=budget)
                )
            elif arm == "whole-paper":
                rows.append(run_whole_paper(catalog, task, budget=budget))
            else:
                rows.append(
                    run_scripted(
                        runtime, catalog, task, arm, budget=budget, caller=caller
                    )
                )
        if progress is not None and (n % 10 == 0 or n == len(tasks)):
            progress(f"{n}/{len(tasks)} tasks")
    return rows


# ---------------------------------------------------------------------------
# results file
# ---------------------------------------------------------------------------


def identical_to_all_on(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Per scripted arm, the share of tasks whose rendered transcript equals
    the ``all-on`` transcript byte for byte (after normalisation). 1.0 means
    the arm changed nothing the caller could see."""
    base = {r["task"]: r["transcript_sha256"] for r in rows if r["arm"] == "all-on"}
    seen: dict[str, list[bool]] = {}
    for r in rows:
        if (r["arm"] in ARMS or r["arm"] in COMBINED_ARMS) and r["task"] in base:
            seen.setdefault(r["arm"], []).append(
                r["transcript_sha256"] == base[r["task"]]
            )
    return {a: round(sum(v) / len(v), 6) for a, v in seen.items() if v}


def arm_table(
    arms: Sequence[str],
    cov: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    embedder_id: str | None,
) -> dict[str, dict[str, Any]]:
    """Per-arm header block: switches, vacuity (predicted from coverage and
    observed as transcript identity), degeneracy, embedder caveat."""
    from precis.bench import layers

    same = identical_to_all_on(rows)
    out: dict[str, dict[str, Any]] = {}
    for arm in arms:
        vac = layers.arm_vacuity(arm, cov, embedder_id=embedder_id)
        if arm in COMBINED_ARMS:
            # vacuous only if every layer it removes is empty
            parts = [
                layers.arm_vacuity(a, cov, embedder_id=embedder_id)
                for a, flag in ARMS.items()
                if flag in COMBINED_ARMS[arm]
            ]
            if parts and all(p["vacuous"] for p in parts):
                vac = {**vac, "vacuous": True, "reason": parts[0]["reason"]}
        info: dict[str, Any] = {"switches_off": sorted(arm_flags(arm)), **vac}
        if arm in KNOWN_DEFECTS:
            info["known_defect"] = KNOWN_DEFECTS[arm]
        if arm in same:
            info["identical_to_all_on_frac"] = same[arm]
            if same[arm] >= 1.0 and not info["vacuous"]:
                info["vacuous"] = True
                info["reason"] = (
                    "observed: every task's rendered transcript equals all-on's"
                )
        out[arm] = info
    return out


#: Cap on each ``git`` call in :func:`git_sha` (a hung git reads as unknown).
_GIT_TIMEOUT_S = 30


def git_sha() -> tuple[str, bool | None]:
    """``(sha, dirty)`` of the checkout: ``PRECIS_BENCH_GIT_SHA`` /
    ``PRECIS_BENCH_GIT_DIRTY`` (set by ``scripts/bench``, since the container
    cannot read the worktree's git dir), else ``git`` itself, else unknown."""
    sha = os.environ.get("PRECIS_BENCH_GIT_SHA", "").strip()
    dirty_env = os.environ.get("PRECIS_BENCH_GIT_DIRTY", "").strip()
    if sha:
        return sha, (dirty_env not in ("", "0")) if dirty_env != "" else None
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=_GIT_TIMEOUT_S,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                timeout=_GIT_TIMEOUT_S,
            ).stdout.strip()
        )
        return sha, dirty
    except (OSError, subprocess.SubprocessError):
        return "unknown", None


def results_filename(
    started: str, sha: str, set_name: str, caller: str | None = None
) -> str:
    day = started[:10].replace("-", "")
    tail = f"-{caller}" if caller else ""
    return f"{day}Z-{sha[:7]}-{set_name}{tail}.json"


def default_results_dir() -> Path:
    env = os.environ.get("PRECIS_BENCH_RESULTS")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "bench" / "results"


def build_results(
    *,
    header: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {"header": dict(header), "rows": list(rows)}


def write_results(results: Mapping[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(results, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def load_results(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "header" not in data or "rows" not in data:
        raise BenchRunError(f"{path} is not a benchmark results file")
    return data


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _mean(xs: Sequence[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def aggregate(
    rows: Sequence[Mapping[str, Any]], arm: str, *, source: str | None = None
) -> dict[str, Any]:
    """Per-arm answerable-task summary: n, recall@5, median tokens to first
    gold over the tasks that reached it (as ``mesh_eval.retrieval`` does; a
    censored task would just contribute the budget, and when more than half
    are censored the median would be the budget itself), the censored count,
    mean calls, refusals and errors."""
    sel = [
        r
        for r in rows
        if r["arm"] == arm
        and not r["abstention"]
        and r.get("applicable", True)
        and (source is None or r["source"] == source)
    ]
    r5 = [r["recall_at_5"] for r in sel if r["recall_at_5"] is not None]
    toks = [
        r["tokens_to_first_gold"]
        for r in sel
        if r["tokens_to_first_gold"] is not None and not r["censored"]
    ]
    return {
        "n": len(sel),
        "recall_at_5": _mean(r5),
        "median_tokens": statistics.median(toks) if toks else None,
        "censored": sum(1 for r in sel if r["censored"]),
        "calls": _mean([r["calls"] for r in sel]),
        "opens": _mean(
            [r["verbatim_opens"] for r in sel if r.get("verbatim_opens") is not None]
        ),
        "refusals": sum(r["refusals"] for r in sel),
        "errors": sum(r["errors"] for r in sel),
    }


def _fmt(x: float | None, nd: int = 3, signed: bool = False) -> str:
    if x is None:
        return "—"
    return f"{x:+.{nd}f}" if signed else f"{x:.{nd}f}"


def _table(results: Mapping[str, Any], *, source: str | None, title: str) -> list[str]:
    rows = results["rows"]
    arms_hdr = results["header"].get("arms", {})
    present = [a for a in ALL_ARMS if any(r["arm"] == a for r in rows)]
    base = aggregate(rows, "all-on", source=source) if "all-on" in present else None
    lines = [
        f"### {title}",
        "",
        "| arm | n | recall@5 | median tok→gold (reached) | censored | mean calls "
        "| mean opens | refusals | Δ recall@5 | Δ median tok | flags |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for arm in present:
        a = aggregate(rows, arm, source=source)
        if a["n"] == 0:
            continue
        d_r = d_t = None
        if base is not None and arm != "all-on":
            if a["recall_at_5"] is not None and base["recall_at_5"] is not None:
                d_r = a["recall_at_5"] - base["recall_at_5"]
            if a["median_tokens"] is not None and base["median_tokens"] is not None:
                d_t = a["median_tokens"] - base["median_tokens"]
        meta = arms_hdr.get(arm, {})
        flags = []
        if meta.get("vacuous"):
            flags.append("VACUOUS")
        if meta.get("degenerate"):
            flags.append("degenerate")
        if meta.get("embedder_note"):
            flags.append(meta["embedder_note"])
        if meta.get("known_defect"):
            flags.append(f"KNOWN DEFECT ({meta['known_defect']})")
        lines.append(
            f"| {arm} | {a['n']} | {_fmt(a['recall_at_5'])} | "
            f"{_fmt(a['median_tokens'], 0)} | {a['censored']} | "
            f"{_fmt(a['calls'], 1)} | {_fmt(a['opens'], 1)} | {a['refusals']} | "
            f"{_fmt(d_r, 3, True)} | {_fmt(d_t, 0, True)} | {'; '.join(flags)} |"
        )
    return lines


def _abstention_table(results: Mapping[str, Any]) -> list[str]:
    rows = results["rows"]
    present = [a for a in ALL_ARMS if any(r["arm"] == a for r in rows)]
    lines = [
        "### Unanswerable tasks (abstention): tokens spent, no gold to find",
        "",
        "| arm | n | mean tokens | mean calls |",
        "|---|---:|---:|---:|",
    ]
    for arm in present:
        sel = [r for r in rows if r["arm"] == arm and r["abstention"]]
        if not sel:
            continue
        lines.append(
            f"| {arm} | {len(sel)} | {_fmt(_mean([r['tokens_total'] for r in sel]), 0)} "
            f"| {_fmt(_mean([r['calls'] for r in sel]), 1)} |"
        )
    return lines


def report_markdown(results: Mapping[str, Any]) -> str:
    """The markdown report: header summary, the per-arm table (all answerable
    tasks, then per source) and the abstention table."""
    h = results["header"]
    cov = h.get("layer_coverage", {})
    emb = h.get("embedder", {})
    caller = h.get("caller", {})
    caller_line = f"- caller `{caller.get('name', 'ladder')}`" + (
        f" · triage threshold {caller['threshold']}" if "threshold" in caller else ""
    )
    out = [
        f"## Memory-ablation run {h.get('set')} — precis {str(h.get('precis_sha', ''))[:9]}"
        f"{' (dirty)' if h.get('git_dirty') else ''}",
        "",
        f"- started {h.get('started_at')} · ended {h.get('ended_at')}",
        caller_line,
        f"- corpus_sha256 `{str(h.get('corpus_sha256', ''))[:16]}…` · tasks_sha256 "
        f"`{str(h.get('tasks_sha256', ''))[:16]}…` · planted seed {h.get('planted', {}).get('seed')}",
        f"- embedder {emb.get('name')} ({'meaningful' if emb.get('meaningful') else 'hash, not meaningful'}) "
        f"· token estimator: {h.get('token_estimator')} · budget {h.get('token_budget')}",
        f"- coverage: keywords {_fmt(cov.get('chunks_with_keywords_frac'))} · llm summaries "
        f"{_fmt(cov.get('chunks_with_llm_summary_frac'))} · embeddings "
        f"{_fmt(cov.get('chunks_with_embedding_frac'))} · categorizer tags "
        f"{_fmt(cov.get('refs_with_categorizer_tag_frac'))} · refs with edges "
        f"{_fmt(cov.get('refs_with_edge_frac'))}",
        "",
    ]
    out += _table(results, source=None, title="All answerable tasks")
    for src in ("qasper", "planted"):
        if any(r["source"] == src and not r["abstention"] for r in results["rows"]):
            out += [""] + _table(results, source=src, title=f"{src} tasks only")
    if any(r["abstention"] for r in results["rows"]):
        out += [""] + _abstention_table(results)
    vac = [a for a, m in h.get("arms", {}).items() if m.get("vacuous")]
    if vac:
        out += [
            "",
            f"**Vacuous arms: {', '.join(vac)}.** The layer is (nearly) empty in "
            "the DB, so there is nothing to remove: a delta on these rows comes "
            "from the rungs and hint lines the caller still pays for, not from "
            "the layer. Do not read it as a layer effect.",
        ]
    return "\n".join(out) + "\n"


def compare_markdown(runs: Sequence[Mapping[str, Any]]) -> str:
    """The same arms under several callers side by side: per source, one row
    per arm and, per caller (the results files in the order given), recall@5,
    median tokens to gold over the tasks that reached it, censored count, mean
    calls, mean verbatim opens and refusals."""
    names = [str(r["header"].get("caller", {}).get("name", "ladder")) for r in runs]
    out: list[str] = []
    for src, title in (
        (None, "All answerable tasks"),
        ("qasper", "QASPER only"),
        ("planted", "Planted only"),
    ):
        present = [
            a
            for a in ALL_ARMS
            if any(a == row["arm"] for r in runs for row in r["rows"])
        ]
        head = "| arm |" + "".join(
            f" {n}: recall@5 | median tok | cens. | calls | opens | refus. |"
            for n in names
        )
        sep = "|---|" + "---:|" * (6 * len(names))
        block = [f"### {title}", "", head, sep]
        for arm in present:
            cells = []
            n_any = 0
            for r in runs:
                a = aggregate(r["rows"], arm, source=src)
                n_any += a["n"]
                cells.append(
                    f" {_fmt(a['recall_at_5'])} | {_fmt(a['median_tokens'], 0)} | "
                    f"{a['censored']} | {_fmt(a['calls'], 1)} | {_fmt(a['opens'], 1)} | "
                    f"{a['refusals']} |"
                )
            if n_any:
                block.append(f"| {arm} |" + "".join(cells))
        out += block + [""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# orchestration (needs the DB; imports are local so `report` stays light)
# ---------------------------------------------------------------------------


def _quick_ref_ids(conn: Any, qasper_ids: Sequence[str]) -> list[int]:
    rows = conn.execute(
        "SELECT ref_id, meta->'qasper'->>'id' FROM refs WHERE kind = 'paper' "
        "AND retired_at IS NULL AND meta->'qasper'->>'id' = ANY(%s) ORDER BY ref_id",
        (list(qasper_ids),),
    ).fetchall()
    have = {r[1] for r in rows}
    missing = [q for q in qasper_ids if q not in have]
    if missing:
        raise BenchRunError(
            f"{len(missing)} manifest papers are not in the DB (e.g. {missing[0]}); "
            "run scripts/bench ingest first"
        )
    return [int(r[0]) for r in rows]


def _planted_sha(rows: Sequence[Mapping[str, Any]], side: Mapping[str, Any]) -> str:
    blob = json.dumps(
        {"tasks": list(rows), "docs": side["docs"], "nodes": side["memory_nodes"]},
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _plant(
    runtime: Any,
    conn: Any,
    qasper_refs: Sequence[int],
    qasper_task_rows: Sequence[Mapping[str, Any]],
    *,
    seed: int,
    n: int,
    n_docs: int,
    per_doc: int,
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, ...]], list[int], dict[str, Any]]:
    """Generate and ingest one seed's planted facts (documents and memory
    nodes; idempotent). Returns ``(task rows, memory gold text, planted ref
    ids, header block)``."""
    from precis.bench import planted_ingest

    hosts = planted_ingest.pick_hosts(
        conn,
        qasper_refs,
        protected=planted_ingest.evidence_units(qasper_task_rows),
        seed=seed,
        n_docs=n_docs,
        per_doc=per_doc,
    )
    built, side = planted.build(hosts, seed, n, at=PLANTED_AT)
    rows = [gold.from_planted(r, side["docs"]) for r in built]
    for r in rows:
        gold.validate_row(r)
    doc_refs = planted_ingest.ingest_docs(conn, side["docs"], seed=seed, at=PLANTED_AT)
    planted_ingest.ingest_memory(runtime, conn, side["memory_nodes"])
    gold_text = planted_ingest.gold_sentences(rows, side["memory_nodes"])
    meta = {
        "seed": seed,
        "n": len(rows),
        "at": PLANTED_AT,
        "docs": len(side["docs"]),
        "memory_nodes": len(side["memory_nodes"]),
        "sha256": _planted_sha(built, side),
    }
    return rows, gold_text, list(doc_refs.values()), meta


def run_cli(
    *,
    set_name: str,
    arms: Sequence[str] | None,
    seed: int,
    out: Path | None,
    embedder_name: str,
    manifest_path: Path | None,
    tasks_path: Path | None,
    planted_n: int,
    budget: int,
    limit: int | None,
    dsn: str,
    fill: bool = True,
    host_docs: int | None = None,
    host_paras: int | None = None,
    caller: str = DEFAULT_CALLER,
) -> Path:
    """Everything behind ``scripts/bench run``. Returns the results path."""
    from precis.bench import corpus, layers, planted_ingest, qasper_tasks
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import make_embedder
    from precis.runtime import PrecisRuntime
    from precis.store import Store

    if caller not in CALLERS:
        raise BenchRunError(f"unknown caller {caller!r}; known: {', '.join(CALLERS)}")
    arm_list = list(arms) if arms else list(ALL_ARMS)
    for a in arm_list:
        arm_flags(a)  # validates
    if "all-on" not in arm_list:
        arm_list.insert(0, "all-on")  # every delta and the vacuity check need it
    started = utc_stamp()
    manifest = corpus.load_manifest(manifest_path)
    records = manifest[set_name]
    corpus_hash = manifest["corpus_sha256"][set_name]
    if corpus.corpus_sha256(records) != corpus_hash:
        raise BenchRunError(f"manifest {set_name!r} does not match its corpus_sha256")
    t_path = tasks_path or qasper_tasks.default_tasks_path(set_name)
    all_qasper_rows = gold.load_tasks(t_path)
    qasper_rows = all_qasper_rows if limit is None else all_qasper_rows[:limit]

    store = Store.connect(dsn)
    try:
        embedder = make_embedder(embedder_name, dim=store.embedding_dim())
        embedder.warmup()
        hub = boot(store=store, embedder=embedder, boot_writes=False)
        runtime = PrecisRuntime(config=PrecisConfig(), hub=hub)

        planted_rows: list[dict[str, Any]] = []
        gold_text: dict[str, tuple[str, ...]] = {}
        planted_meta: dict[str, Any] = {"seed": seed, "n": 0, "at": PLANTED_AT}
        with store.pool.connection() as conn:
            qasper_refs = _quick_ref_ids(conn, [r["qasper_id"] for r in records])
            planted_refs: list[int] = []
            if planted_n > 0:
                planted_rows, gold_text, planted_refs, planted_meta = _plant(
                    runtime,
                    conn,
                    qasper_refs,
                    all_qasper_rows,
                    seed=seed,
                    n=planted_n,
                    n_docs=host_docs or planted_ingest.HOST_DOCS,
                    per_doc=host_paras or planted_ingest.HOST_PARAS_PER_DOC,
                )
        if fill:
            print("fill: embeddings + keywords for anything missing …", file=sys.stderr)
            fill_totals = layers.fill(store, embedder)
        else:
            fill_totals = None
        with store.pool.connection() as conn:
            cov = layers.coverage(conn, qasper_refs + planted_refs)
            cov["memory_nodes"] = len(planted_ingest.existing_memory_refs(conn))

        tasks, skipped = select_tasks(qasper_rows + planted_rows, gold_text)
        if not tasks:
            raise BenchRunError("no runnable tasks")
        rows = run_matrix(
            store,
            runtime,
            embedder,
            tasks,
            arm_list,
            budget=budget,
            progress=lambda m: print(m, file=sys.stderr),
            caller=caller,
        )
    finally:
        store.close()

    ended = utc_stamp()
    sha, dirty = git_sha()
    emb_id = getattr(embedder, "model", embedder_name)
    meaningful = not layers.hash_embedder(emb_id)
    arms_block = arm_table(arm_list, cov, rows, embedder_id=emb_id)
    header = {
        "schema": RESULTS_SCHEMA,
        "tier": "scripted",
        "set": set_name,
        "precis_sha": sha,
        "git_dirty": dirty,
        "corpus_sha256": corpus_hash,
        "tasks_file": str(t_path.name),
        "tasks_sha256": file_sha256(t_path),
        "tasks": {
            "qasper": sum(1 for t in tasks if t.source == "qasper"),
            "planted": sum(1 for t in tasks if t.source == "planted"),
            "abstention": sum(1 for t in tasks if t.abstain),
            "skipped": skipped,
            "limit": limit,
        },
        "planted": planted_meta,
        "embedder": {
            "name": embedder_name,
            "model": emb_id,
            "backend": getattr(embedder, "backend", None),
            "dim": getattr(embedder, "dim", None),
            "meaningful": meaningful,
            "stored": cov.get("embedder_counts", {}),
        },
        "models": None,
        "judge": None,
        "token_estimator": TOKEN_ESTIMATOR,
        "token_budget": budget,
        "caller": {
            "name": caller,
            "search": {"unique_per": "chunk", "page_size": SEARCH_K},
            "read_k": READ_K,
            "ladder": list(LADDER),
            **(
                {
                    "threshold": TRIAGE_THRESHOLD
                    if caller == "triage"
                    else ROWKW_THRESHOLD,
                    "score": "question content-word coverage of "
                    + (
                        "the kwd rung text"
                        if caller == "triage"
                        else "the chunk keywords in the search row"
                    ),
                }
                if caller in ("triage", "rowkw")
                else {}
            ),
            "recall_ks": list(RECALL_KS),
            "stop": "first rendered chunk overlapping gold, else budget or hits exhausted",
        },
        "switches_env": bench_switches.ENV_VAR,
        "arms": arms_block,
        "layer_coverage": cov,
        "fill": fill_totals,
        "machine": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "started_at": started,
        "ended_at": ended,
    }
    for arm, meta in arms_block.items():
        if meta.get("vacuous"):
            print(
                f"WARNING: arm {arm!r} is vacuous — {meta['reason']}", file=sys.stderr
            )
        if meta.get("degenerate"):
            print(
                f"WARNING: arm {arm!r} is degenerate — {meta['reason']}",
                file=sys.stderr,
            )
    if not meaningful:
        print(
            "WARNING: embedder is the hash/mock embedder — semantic and hybrid "
            "results are not meaningful",
            file=sys.stderr,
        )
    path = out or default_results_dir() / results_filename(
        started, sha, set_name, caller
    )
    return write_results(build_results(header=header, rows=rows), path)


# ---------------------------------------------------------------------------
# triage threshold sweep (planted dev seeds only)
# ---------------------------------------------------------------------------


class _MemoRuntime:
    """A runtime whose dispatches are memoised per (verb, args, switches), so a
    sweep that re-runs the same task at many thresholds pays each distinct call
    once. The calls are deterministic (the point of the harness)."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._memo: dict[tuple[str, str, Any], tuple[str, bool]] = {}

    def dispatch_with_status(
        self, verb: str, args: dict[str, Any], fold: bool = True
    ) -> tuple[str, bool]:
        key = (verb, json.dumps(args, sort_keys=True), bench_switches.current())
        if key not in self._memo:
            self._memo[key] = self._runtime.dispatch_with_status(verb, args, fold=fold)
        return self._memo[key]


SWEEP_THRESHOLDS = (0.0, 0.1, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6)
SWEEP_ARMS = ("all-on", "no-kwd")


def sweep_threshold(
    runtime: Any,
    catalog: Catalog,
    tasks: Sequence[BenchTask],
    thresholds: Sequence[float] = SWEEP_THRESHOLDS,
    arms: Sequence[str] = SWEEP_ARMS,
    *,
    budget: int = TOKEN_BUDGET,
    caller: str = "triage",
) -> list[dict[str, Any]]:
    """The ``caller`` (``triage`` or ``rowkw``) at each threshold on ``tasks`` (answerable ones
    only): per (arm, threshold) the share of tasks that reached the gold, the
    mean tokens to gold (a task that did not carries the budget) and the mean
    verbatim opens and calls."""
    memo = _MemoRuntime(runtime)
    answerable = [t for t in tasks if not t.abstain and t.has_gold]
    out: list[dict[str, Any]] = []
    for arm in arms:
        for th in thresholds:
            rows = [
                run_scripted(
                    memo, catalog, t, arm, budget=budget, caller=caller, threshold=th
                )
                for t in answerable
            ]
            n = len(rows)
            out.append(
                {
                    "arm": arm,
                    "threshold": th,
                    "n": n,
                    "found_rate": sum(1 for r in rows if r["found"]) / n if n else None,
                    "mean_tokens": _mean([r["tokens_to_first_gold"] for r in rows]),
                    "mean_opens": _mean([r["verbatim_opens"] for r in rows]),
                    "mean_calls": _mean([r["calls"] for r in rows]),
                }
            )
    return out


def pick_threshold(sweep: Sequence[Mapping[str, Any]]) -> float:
    """The threshold with the lowest mean tokens to gold averaged over the
    swept arms (a task that never reached the gold carries the budget, so
    losing recall is paid for); ties go to the lower threshold."""
    by_th: dict[float, list[float]] = {}
    for r in sweep:
        by_th.setdefault(float(r["threshold"]), []).append(float(r["mean_tokens"]))
    return min(by_th, key=lambda th: (statistics.fmean(by_th[th]), th))


def sweep_markdown(
    sweep: Sequence[Mapping[str, Any]],
    *,
    seeds: Sequence[int],
    caller: str = "triage",
) -> str:
    lines = [
        f"{caller} threshold sweep, planted dev seeds {', '.join(map(str, seeds))} "
        f"(not the reported seed {PLANTED_SEED}, not QASPER).",
        "",
        "| arm | threshold | n | found | mean tok→gold (miss = budget) | mean opens | mean calls |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sweep:
        lines.append(
            f"| {r['arm']} | {r['threshold']:g} | {r['n']} | {_fmt(r['found_rate'])} | "
            f"{_fmt(r['mean_tokens'], 0)} | {_fmt(r['mean_opens'], 2)} | "
            f"{_fmt(r['mean_calls'], 1)} |"
        )
    lines += [
        "",
        f"Picked threshold (lowest mean tok→gold over the arms): {pick_threshold(sweep):g}",
    ]
    return "\n".join(lines) + "\n"


def sweep_cli(
    *,
    set_name: str,
    seeds: Sequence[int],
    thresholds: Sequence[float],
    embedder_name: str,
    manifest_path: Path | None,
    tasks_path: Path | None,
    planted_n: int,
    budget: int,
    dsn: str,
    host_docs: int | None = None,
    host_paras: int | None = None,
    caller: str = "triage",
) -> str:
    """Everything behind ``scripts/bench sweep``: plant each dev seed's facts
    into the (throwaway) DB, fill their embeddings and keywords, run the
    ``caller`` (``triage`` or ``rowkw``) over those tasks at every threshold.
    ``triage`` is swept on ``all-on`` and ``no-kwd`` (its cheap rung is ``kwd``,
    else ``summary``); ``rowkw`` reads no rung, so on ``all-on`` alone.
    Returns markdown.

    Use a DB of its own (``precis_dev_sweep``): the planted documents of every
    seed in ``seeds`` stay in it and would otherwise sit among the reported
    run's distractors."""
    from precis.bench import corpus, layers, planted_ingest, qasper_tasks
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import make_embedder
    from precis.runtime import PrecisRuntime
    from precis.store import Store

    if caller not in ("triage", "rowkw"):
        raise BenchRunError(f"only triage and rowkw have a threshold, not {caller!r}")
    if PLANTED_SEED in seeds:
        raise BenchRunError(
            f"seed {PLANTED_SEED} is the reported run's seed; sweep on dev seeds "
            f"({', '.join(map(str, SWEEP_SEEDS))})"
        )
    manifest = corpus.load_manifest(manifest_path)
    records = manifest[set_name]
    t_path = tasks_path or qasper_tasks.default_tasks_path(set_name)
    qasper_rows = gold.load_tasks(t_path)
    store = Store.connect(dsn)
    try:
        embedder = make_embedder(embedder_name, dim=store.embedding_dim())
        embedder.warmup()
        runtime = PrecisRuntime(
            config=PrecisConfig(),
            hub=boot(store=store, embedder=embedder, boot_writes=False),
        )
        all_rows: list[dict[str, Any]] = []
        gold_text: dict[str, tuple[str, ...]] = {}
        with store.pool.connection() as conn:
            qasper_refs = _quick_ref_ids(conn, [r["qasper_id"] for r in records])
            for seed in seeds:
                rows, gt, _refs, _meta = _plant(
                    runtime,
                    conn,
                    qasper_refs,
                    qasper_rows,
                    seed=seed,
                    n=planted_n,
                    n_docs=host_docs or planted_ingest.HOST_DOCS,
                    per_doc=host_paras or planted_ingest.HOST_PARAS_PER_DOC,
                )
                all_rows += rows
                gold_text.update(gt)
        print("fill: embeddings + keywords for anything missing …", file=sys.stderr)
        layers.fill(store, embedder)
        tasks, _skipped = select_tasks(all_rows, gold_text)
        sweep = sweep_threshold(
            runtime,
            Catalog(store),
            tasks,
            thresholds,
            SWEEP_ARMS if caller == "triage" else ("all-on",),
            budget=budget,
            caller=caller,
        )
    finally:
        store.close()
    return sweep_markdown(sweep, seeds=list(seeds), caller=caller)
