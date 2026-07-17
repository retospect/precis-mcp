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
``dispatch_relax`` in :mod:`precis.quest.compute`: the default is a safe,
embedder-free lexical lookup over held papers (no network, no acquisition), and
tests / a richer caller can pass a semantic or acquiring search. Fetching
papers the corpus does *not* hold yet (an OA acquisition serving the quest) is
the deliberate follow-on — this rung grounds against what we already have and
makes the un-held case *visible* (an ``observation`` noting acquisition is
needed) rather than silently missing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from precis.quest.gaps import _handle, _live_servers
from precis.quest.logbook import append_entry

if TYPE_CHECKING:
    from precis.store import Store

#: Cap the queries honoured per tick (a weak proposer can't flood acquisition).
MAX_QUERIES = 3
#: How many top hits to link per query.
MAX_LINK_PER_QUERY = 3

#: (store, query, exclude_ref_ids) -> ranked paper ref_ids (best first).
SearchFn = Callable[["Store", str, list[int]], list[int]]


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
    "run_search_step",
]
