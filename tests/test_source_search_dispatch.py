"""``search`` source-search wiring through the runtime dispatcher.

The store engine is pinned in ``test_search_across_kinds.py``; here we
exercise the dispatch interception: a ``sort=`` / ``since=`` / ``until=``
search routes to :meth:`PrecisRuntime._dispatch_source_search` (the
cross-kind chunk primitive) rather than the per-handler fan-out, resolves
the kind set, and renders. End to end through ``runtime.dispatch``.
"""

from __future__ import annotations

from precis.runtime import PrecisRuntime
from precis.store import BlockInsert


def _seed(rt: PrecisRuntime, kind: str, slug: str, title: str, text: str) -> int:
    store = rt.hub.store
    assert store is not None
    ref = store.insert_ref(kind=kind, slug=slug, title=title)
    emb = rt.hub.embedder
    vec = emb.embed_one(text) if emb is not None else None
    store.insert_blocks(ref.id, [BlockInsert(pos=0, text=text, embedding=vec)])
    return ref.id


def test_sort_recency_routes_and_returns_hits(
    runtime_with_store: PrecisRuntime,
) -> None:
    rt = runtime_with_store
    _seed(
        rt, "paper", "src-a", "Alpha study", "spintronic magnon transport in insulators"
    )
    _seed(rt, "web", "src-b", "Beta note", "spintronic magnon transport review")

    out = rt.dispatch(
        "search",
        {"kind": "paper,web", "q": "spintronic magnon transport", "sort": "recency"},
    )
    # Both kinds' refs surface through the single cross-kind primitive.
    assert "Alpha study" in out
    assert "Beta note" in out


def test_since_far_future_yields_empty(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    _seed(rt, "paper", "src-c", "Gamma", "topological insulator surface states")

    out = rt.dispatch(
        "search",
        {"kind": "paper", "q": "topological insulator", "since": "2999-01-01"},
    )
    assert "no matches" in out.lower()


def test_bad_since_surfaces_error(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    body, is_error = rt.dispatch_with_status(
        "search",
        {"kind": "paper", "q": "anything", "since": "not-a-date"},
    )
    assert is_error
    assert "since=" in body
