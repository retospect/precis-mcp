"""``search`` source-search wiring through the runtime dispatcher.

The store engine is pinned in ``test_search_across_kinds.py``; here we
exercise the dispatch interception: a ``sort=`` / ``since=`` / ``until=``
search routes to :meth:`PrecisRuntime._dispatch_source_search` (the
cross-kind chunk primitive) rather than the per-handler fan-out, resolves
the kind set, and renders. End to end through ``runtime.dispatch``.
"""

from __future__ import annotations

from precis.config import PrecisConfig
from precis.dispatch import boot
from precis.embedder import MockEmbedder
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store


def _seed(rt: PrecisRuntime, kind: str, slug: str, title: str, text: str) -> int:
    store = rt.hub.store
    assert store is not None
    ref = store.insert_ref(kind=kind, slug=slug, title=title)
    emb = rt.hub.embedder
    vec = emb.embed_one(text) if emb is not None else None
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=text, embedding=vec)])
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


def test_source_search_marks_non_production_embedder(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr249198: the sort=/since=/until= source-search primitive runs
    the semantic leg against the fixture's ``MockEmbedder`` — which
    never raises, so the existing degrade-to-lexical path never fires
    even though the vector is deterministic noise. The envelope must
    say so.
    """
    rt = runtime_with_store
    _seed(rt, "paper", "src-d", "Delta study", "quantum spin liquid frustration")
    out = rt.dispatch(
        "search",
        {"kind": "paper", "q": "quantum spin liquid", "sort": "recency"},
    )
    assert "non-production" in out
    assert "mock" in out


def test_source_search_no_marker_for_production_embedder(store: Store) -> None:
    """The converse: a backend that reports itself production must not
    carry the warning."""

    class _ProductionStubEmbedder(MockEmbedder):
        """Deterministic vectors (store-dim compatible) but flagged as
        a production backend — stands in for a real remote/bge-m3
        embedder without the network/torch dependency."""

        @property
        def backend(self) -> str:
            return "remote"

        @property
        def is_production(self) -> bool:
            return True

    rt = PrecisRuntime(
        config=PrecisConfig(),
        hub=boot(
            store=store, embedder=_ProductionStubEmbedder(dim=store.embedding_dim())
        ),
    )
    _seed(rt, "paper", "src-e", "Epsilon study", "quantum spin liquid frustration")
    out = rt.dispatch(
        "search",
        {"kind": "paper", "q": "quantum spin liquid", "sort": "recency"},
    )
    assert "non-production" not in out
