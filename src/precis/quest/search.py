"""Quest lit-search — a tick can go *ground* itself in the literature.

The missing half of the research loop. A reasoning-only quest with no paper
servers (the ``no-literature`` / ``thin-support`` gaps) had only one lever: mint
another hypothesis — so it recycled the same open question forever (the spin the
allocator then rewarded as "activity"). This gives a tick a real grounding
action: it emits ``searches`` (queries), and each becomes a corpus lookup whose
top hits are linked ``serves``→quest. The next tick sees those papers as
servers + context, and — crucially — acquiring a paper is *external progress*
(cascade resets the stall clock), so grounding earns compute where re-reasoning
does not.

The search is an **injectable seam** (``search_fn``) exactly like
``dispatch_relax`` in :mod:`precis.quest.compute`: the default
(``_default_paper_search``) is a safe, embedder-free lexical lookup over held
papers (no network, no acquisition). Acquisition is now **built**:
:func:`make_acquiring_search` layers a Semantic Scholar free-text search on top
— any hit with a DOI is queued via ``PaperHandler.acquire`` (idempotent stub
mint + ``fetch_oa`` pickup), so a query that misses the held corpus doesn't just
log an "acquisition needed" observation, it actually requests the paper. Tests
and other callers may still pass a narrower search (e.g. lexical-only, or a
semantic reranker) through the same ``search_fn`` seam.

**HyDE for the corpus leg (dossier-hygiene design).** A tick's ``searches``
entry may carry an optional ``hypothetical`` alongside its keyword ``query``
(:class:`SearchQuery`, :func:`_parse_search_entry`) — a one-two sentence
passage phrased the way it might appear verbatim in the abstract of the paper
the model wishes existed. The model's question-phrased ``query`` alone kept
missing the held corpus (7 misses across 3 prod ticks: retrieval matches
DOCUMENTS, not questions). When present, :func:`_hyde_corpus_hits` routes the
corpus leg through :mod:`precis.handlers._paper_search`'s broad-retrieval
fusion (the same ``queries=``/``answers=`` facility ``search(kind='paper',
…)`` exposes) instead of :func:`_default_paper_search`'s plain lexical
lookup — run by :func:`run_search_step` itself, independent of ``search_fn``,
so the ``search_fn`` seam (the Semantic Scholar + acquire leg, still driven by
the plain keyword ``query``) keeps its exact 3-argument shape unchanged.
Degrades to a fused-LEXICAL-only result when no embedder is wired (never
raises) — same contract as the broad ``search()`` verb with no embedder
configured.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.quest.gaps import _handle, _live_servers
from precis.quest.logbook import append_entry
from precis.quest.tagging import quest_tag_value
from precis.store.types import Tag
from precis.utils.env import env_int

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


#: Cap the queries honoured per tick (a weak proposer can't flood acquisition).
#: A day at the library beats weeks in the lab — lean hard into lit-search.
MAX_QUERIES = env_int("PRECIS_QUEST_MAX_QUERIES", 10, lo=1, hi=100)
#: How many top hits to link per query.
MAX_LINK_PER_QUERY = 3


def _acquire_per_query() -> int:
    """How many S2 results per query the acquiring search will try to acquire
    (default 4, clamped 1..10) — a knob on acquisition volume without a
    redeploy."""
    return env_int("PRECIS_QUEST_ACQUIRE_PER_QUERY", 4, lo=1, hi=10)


#: (store, query, exclude_ref_ids) -> ranked paper ref_ids (best first).
SearchFn = Callable[["Store", str, list[int]], list[int]]

#: Parses ``id=N`` out of the ``PaperHandler.acquire`` ack (mirrors
#: ``_good_search._ID_IN_ACK``).
_ID_IN_ACK = re.compile(r"\bid=(\d+)\b")


@dataclass(frozen=True)
class SearchStep:
    queries_run: int
    papers_linked: int
    notes: list[str] = field(default_factory=list)


def _default_paper_search(
    store: Store, query: str, exclude_ref_ids: list[int]
) -> list[int]:
    """Safe corpus-only default: lexical paper-title lookup, no network."""
    ex = set(exclude_ref_ids)
    rows = store.search_refs_lexical(q=query, kind="paper", limit=10)
    return [r.id for (r, _rank) in rows if r.id not in ex]


@dataclass(frozen=True)
class SearchQuery:
    """One parsed ``searches`` payload entry (dossier-hygiene design).

    ``query`` is the short keyword phrasing — unchanged role, still what
    drives the Semantic Scholar + acquire leg (:func:`make_acquiring_search`)
    and the logbook entry's own text. ``hypothetical`` is optional: a HyDE
    passage that, when present, routes the CORPUS leg through
    :func:`_hyde_corpus_hits` instead of :func:`_default_paper_search`'s
    plain lexical lookup — see the module docstring.
    """

    query: str
    hypothetical: str | None = None


def _parse_search_entry(raw: Any) -> SearchQuery | None:
    """Parse one ``payload["searches"]`` entry — either the legacy plain
    string, or ``{"query": "...", "hypothetical": "..."}``. ``None`` when
    the entry has no usable ``query`` (a blank string, an empty/malformed
    dict, or anything else) — the caller's cue to skip it, mirroring the
    old blank-string skip."""
    if isinstance(raw, dict):
        query = str(raw.get("query") or "").strip()
        hypothetical = str(raw.get("hypothetical") or "").strip() or None
    else:
        query = str(raw or "").strip()
        hypothetical = None
    return SearchQuery(query=query, hypothetical=hypothetical) if query else None


def _hyde_corpus_hits(
    store: Store,
    embedder: Any | None,
    quest_id: int,
    query: str,
    hypothetical: str,
    exclude_ref_ids: list[int],
    *,
    limit: int = 10,
) -> list[int]:
    """The HyDE-fused corpus leg: ``query`` + ``hypothetical`` run through
    :class:`precis.handlers._paper_search.FusedBlockSearch` (``queries=
    [query], answers=[hypothetical]``) — the same broad-retrieval fusion the
    ``search(kind='paper', queries=…, answers=…)`` verb exposes — in place of
    :func:`_default_paper_search`'s plain lexical lookup. Ranked paper
    ref_ids, best first, deduped, ``exclude_ref_ids`` dropped.

    Degrades to ``[]`` on any failure (an embedder-less store, a store stub
    missing a method this pulls in, a flaky embed call) — one search entry's
    HyDE leg must never sink the whole lit-search step; the caller still has
    ``search_fn``'s ordinary corpus+acquire leg to fall back on.
    """
    from precis.handlers._paper_search import FusedBlockSearch

    try:
        result = FusedBlockSearch(store=store, embedder=embedder, kind="paper").run(
            q=query,
            scope=None,
            tags=None,
            page_size=limit,
            page=1,
            exclude=None,
            mode=None,
            after=None,
            before=None,
            queries=[query],
            answers=[hypothetical],
            per_paper=None,
        )
    except Exception:
        log.debug(
            "quest %s: HyDE corpus search failed for %r",
            quest_id,
            query[:80],
            exc_info=True,
        )
        return []
    ex = set(exclude_ref_ids)
    seen: set[int] = set()
    out: list[int] = []
    for _block, ref, _score in result.hits:
        if ref.id in ex or ref.id in seen:
            continue
        seen.add(ref.id)
        out.append(ref.id)
    return out


def make_acquiring_search(quest_id: int, hub: Any) -> SearchFn:
    """Build a ``search_fn`` that acquires, not just looks up.

    Layers Semantic Scholar over :func:`_default_paper_search`: held-corpus
    lexical hits come first (free, instant), then each of the top S2 results
    for the query — anything carrying a DOI — is queued through
    ``PaperHandler.acquire`` (idempotent stub mint + link ``serves``→quest;
    ``fetch_oa`` ingests the PDF later, out of band). A bad DOI or a flaky S2 /
    fetch round-trip is swallowed per-candidate — one dud result must never
    sink the whole lit-search step.
    """

    def _search(store: Store, query: str, exclude_ref_ids: list[int]) -> list[int]:
        from precis.handlers.paper import PaperHandler
        from precis.ingest.semantic_scholar import search_s2_papers

        held = _default_paper_search(store, query, exclude_ref_ids)

        acquired: list[int] = []
        try:
            candidates = search_s2_papers(query, limit=_acquire_per_query())
        except Exception:
            log.debug("quest %s: S2 search failed for %r", quest_id, query[:80])
            candidates = []

        handler = PaperHandler(hub=hub)
        for paper in candidates:
            doi = paper.get("doi")
            if not doi:
                continue
            try:
                resp = handler.acquire(
                    identifier=f"doi:{doi}",
                    context_ref_id=quest_id,
                    reason=f"quest lit-search: {query[:120]}",
                    verify=True,
                )
            except Exception:
                log.debug(
                    "quest %s: acquire failed for doi=%s (query=%r)",
                    quest_id,
                    doi,
                    query[:80],
                )
                continue
            m = _ID_IN_ACK.search(resp.body or "")
            if m is not None:
                acquired.append(int(m.group(1)))

        ex = set(exclude_ref_ids)
        ordered = held + acquired
        seen: set[int] = set()
        out: list[int] = []
        for rid in ordered:
            if rid in ex or rid in seen:
                continue
            seen.add(rid)
            out.append(rid)
        return out

    return _search


def run_search_step(
    store: Store,
    quest_id: int,
    searches: list[Any],
    *,
    by: str = "agent",
    search_fn: SearchFn | None = None,
    embedder: Any | None = None,
) -> SearchStep:
    """Run each search, link the top held papers as ``serves`` servers.

    ``searches`` entries are parsed by :func:`_parse_search_entry` — either
    the legacy plain query string, or ``{"query": ..., "hypothetical":
    ...}`` (HyDE, dossier-hygiene design). An entry with a ``hypothetical``
    runs its corpus leg through :func:`_hyde_corpus_hits` (fused
    ``queries=``/``answers=`` retrieval — the model's ``query`` phrasing
    alone kept missing the held corpus) IN ADDITION to ``search_fn`` (still
    driven by the plain ``query`` — the Semantic Scholar + acquire leg is
    unchanged); the two hit lists are merged (HyDE first) and deduped before
    the per-query link cap. ``embedder`` (optional) powers the HyDE leg's
    semantic reformulation — see :func:`_hyde_corpus_hits`.

    Every search lands a logbook entry: a ``result`` when papers were linked
    (external progress → the cascade stall clock resets), or an ``observation``
    when nothing held matched (the un-held / acquisition-needed case, made
    visible rather than silent). The entry's own text always quotes the
    short ``query`` (not the ``hypothetical`` passage).

    Every freshly-linked paper also picks up the ``quest:<public_id>`` OPEN
    tag (see :mod:`precis.quest.tagging`) — the same tag the Drive-scoped
    hub links point at, so a paper this step links is immediately visible
    there without waiting on a backfill.
    """
    search = search_fn or _default_paper_search
    quest_tag = Tag.open(quest_tag_value(quest_id, store))
    existing = {s.id for s in _live_servers(store, quest_id) if s.kind == "paper"}
    queries_run = 0
    linked_total = 0
    notes: list[str] = []

    for raw in searches[:MAX_QUERIES]:
        entry = _parse_search_entry(raw)
        if entry is None:
            continue
        query = entry.query
        queries_run += 1
        merged: list[int] = []
        if entry.hypothetical:
            merged.extend(
                _hyde_corpus_hits(
                    store,
                    embedder,
                    quest_id,
                    query,
                    entry.hypothetical,
                    list(existing),
                )
            )
        seen = set(merged)
        for rid in search(store, query, list(existing)):
            if rid in seen:
                continue
            seen.add(rid)
            merged.append(rid)
        hits = merged[:MAX_LINK_PER_QUERY]
        linked: list[int] = []
        for rid in hits:
            if rid in existing:
                continue
            store.add_link(
                src_ref_id=rid,
                dst_ref_id=quest_id,
                relation="serves",
                set_by="agent",
            )
            store.add_tag(rid, quest_tag, set_by="system")
            existing.add(rid)
            linked.append(rid)
        linked_total += len(linked)
        if linked:
            handles = ", ".join(_handle("paper", rid) for rid in linked)
            append_entry(
                store,
                quest_id,
                text=f'lit-search: "{query[:80]}" → linked {len(linked)} paper(s): {handles}',
                entry_type="result",
                by=by,
            )
            notes.append(f"{query[:40]}: +{len(linked)}")
        else:
            append_entry(
                store,
                quest_id,
                text=(
                    f'lit-search: "{query[:80]}" → no held paper matched '
                    "(acquisition needed)"
                ),
                entry_type="observation",
                by=by,
            )
            notes.append(f"{query[:40]}: 0")

    return SearchStep(queries_run=queries_run, papers_linked=linked_total, notes=notes)


__all__ = [
    "MAX_LINK_PER_QUERY",
    "MAX_QUERIES",
    "SearchFn",
    "SearchQuery",
    "SearchStep",
    "make_acquiring_search",
    "run_search_step",
]
