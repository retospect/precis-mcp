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
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.quest.gaps import _handle, _live_servers
from precis.quest.logbook import append_entry

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, lo: int = 1, hi: int = 100) -> int:
    try:
        n = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(lo, min(hi, n))


#: Cap the queries honoured per tick (a weak proposer can't flood acquisition).
#: A day at the library beats weeks in the lab — lean hard into lit-search.
MAX_QUERIES = _env_int("PRECIS_QUEST_MAX_QUERIES", 10)
#: How many top hits to link per query.
MAX_LINK_PER_QUERY = 3


def _acquire_per_query() -> int:
    """How many S2 results per query the acquiring search will try to acquire
    (default 4, clamped 1..10) — a knob on acquisition volume without a
    redeploy."""
    return _env_int("PRECIS_QUEST_ACQUIRE_PER_QUERY", 4, lo=1, hi=10)


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
    queries: list[str],
    *,
    by: str = "agent",
    search_fn: SearchFn | None = None,
) -> SearchStep:
    """Run each query, link the top held papers as ``serves`` servers.

    Every query lands a logbook entry: a ``result`` when papers were linked
    (external progress → the cascade stall clock resets), or an ``observation``
    when nothing held matched (the un-held / acquisition-needed case, made
    visible rather than silent).
    """
    search = search_fn or _default_paper_search
    existing = {s.id for s in _live_servers(store, quest_id) if s.kind == "paper"}
    queries_run = 0
    linked_total = 0
    notes: list[str] = []

    for raw in queries[:MAX_QUERIES]:
        query = (raw or "").strip()
        if not query:
            continue
        queries_run += 1
        hits = search(store, query, list(existing))[:MAX_LINK_PER_QUERY]
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
    "SearchStep",
    "make_acquiring_search",
    "run_search_step",
]
