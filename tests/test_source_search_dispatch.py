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
from precis.utils import handle_registry


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


def test_recency_render_shows_total_and_per_kind_breakdown(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr503440: the recency-ordered source search reports the "N of K"
    total and per-kind breakdown — the over-fetched pool made a bare
    "# N matches" hide how much further the query reaches."""
    rt = runtime_with_store
    _seed(
        rt, "paper", "tot-a", "Alpha study", "spintronic magnon transport in insulators"
    )
    _seed(rt, "web", "tot-b", "Beta note", "spintronic magnon transport review")

    out = rt.dispatch(
        "search",
        {
            "kind": "paper,web",
            "q": "spintronic magnon transport",
            "sort": "recency",
        },
    )
    assert "2 total · paper: 1, web: 1" in out


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


def test_since_composes_with_exclude(runtime_with_store: PrecisRuntime) -> None:
    """gr608576: a creation-date bound no longer refuses ``exclude=`` —
    the slug resolves into the same ref-id channel ``uncited=`` uses."""
    rt = runtime_with_store
    _seed(rt, "paper", "excl-keep", "Keep study", "spintronic magnon transport alpha")
    _seed(rt, "paper", "excl-drop", "Drop study", "spintronic magnon transport beta")

    out = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "spintronic magnon transport",
            "since": "2020-01-01",
            "exclude": ["excl-drop"],
        },
    )
    assert "Keep study" in out
    assert "Drop study" not in out


def test_since_composes_with_broad_legs(runtime_with_store: PrecisRuntime) -> None:
    """gr608576: ``queries=``/``answers=`` fuse extra legs on the
    source-search path instead of being refused."""
    rt = runtime_with_store
    _seed(rt, "paper", "broad-a", "Broad Alpha", "spintronic magnon transport alpha")

    out = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "spintronic magnon",
            "since": "2020-01-01",
            "queries": ["magnon transport study"],
            "answers": ["a passage about magnon transport"],
        },
    )
    assert "Broad Alpha" in out


def test_since_knobs_work_nested_in_args(runtime_with_store: PrecisRuntime) -> None:
    """gr608576: the knobs callers learned as ``args={…}`` on paper
    search hoist onto the source-search path rather than refusing."""
    rt = runtime_with_store
    _seed(rt, "paper", "nest-keep", "Nested Keep", "spintronic magnon transport nest")
    _seed(rt, "paper", "nest-drop", "Nested Drop", "spintronic magnon transport nest")

    out = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "spintronic magnon transport",
            "since": "2020-01-01",
            "__extras__": {
                "exclude": ["nest-drop"],
                "queries": ["magnon transport nested"],
                "view": "index",
            },
        },
    )
    assert "Nested Keep" in out
    assert "Nested Drop" not in out


def test_source_search_pages(runtime_with_store: PrecisRuntime) -> None:
    """gr608576: ``page=`` walks the fused per-ref list and the response
    advertises the working ``args={'page': N+1}`` continuation
    (gr608577 — the spelling that survives the MCP boundary)."""
    rt = runtime_with_store
    for i in range(3):
        _seed(rt, "paper", f"pg-{i}", f"Page study {i}", "spintronic magnon paging")

    out = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "spintronic magnon paging",
            "since": "2020-01-01",
            "page_size": 2,
        },
    )
    assert "Page study" in out
    assert "args={'page': 2}" in out

    page2 = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "spintronic magnon paging",
            "since": "2020-01-01",
            "page_size": 2,
            "page": 2,
        },
    )
    assert "Page study" in page2


def test_source_search_pages_past_pool_dominating_ref(
    runtime_with_store: PrecisRuntime,
) -> None:
    """td609888: the fused pool caps *chunks* before refs collapse — one
    ref with 80+ matching chunks must not hide the other refs or
    suppress the ``page=N+1`` continuation hint."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    dom = store.insert_ref(kind="paper", slug="pg-dom", title="Dominant study")
    store.chunks.insert_chunks(
        dom.id,
        [
            ChunkInsert(ord=i, text="magnon caloritronics " * 6, embedding=None)
            for i in range(85)
        ],
    )
    _seed(rt, "paper", "pg-tail-a", "Tail alpha", "magnon caloritronics tail alpha")
    _seed(rt, "paper", "pg-tail-b", "Tail beta", "magnon caloritronics tail beta")

    args = {
        "kind": "paper",
        "q": "magnon caloritronics",
        "since": "2020-01-01",
        "mode": "lexical",
        "page_size": 2,
    }
    out = rt.dispatch("search", dict(args))
    assert "args={'page': 2}" in out
    page2 = rt.dispatch("search", {**args, "page": 2})
    seen = out + page2
    for title in ("Dominant study", "Tail alpha", "Tail beta"):
        assert title in seen
    # Three refs over two rows a page: the probe must not over-promise.
    assert "'page': 3" not in page2


def test_source_search_pin_counts_against_page(
    runtime_with_store: PrecisRuntime,
) -> None:
    """td609888: the pinned identity card is a page row, not a freebie —
    the fused row it displaces on page 1 must still be reachable on a
    later page (the page-2 offset accounts for the pinned row)."""
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    pin_ref = store.insert_ref(kind="paper", slug="pin-target", title="Pinned study")
    handle = handle_registry.format_handle("paper", pin_ref.id)
    for slug in ("pin-a", "pin-b", "pin-c"):
        _seed(rt, "paper", slug, f"Study {slug}", f"cites {handle} on magnon pinning")

    args = {
        "kind": "paper",
        "q": handle,
        "since": "2020-01-01",
        "mode": "lexical",
        "page_size": 2,
    }
    out = rt.dispatch("search", dict(args))
    # Page 1 leads with the pinned card plus one fetched row (top_k=2).
    assert "Pinned study" in out
    assert "args={'page': 2}" in out
    page2 = rt.dispatch("search", {**args, "page": 2})
    seen = out + page2
    # All three fetched refs surface across the two pages — the row the
    # pin displaced on page 1 is not skipped by the page-2 offset.
    for slug in ("pin-a", "pin-b", "pin-c"):
        assert f"Study {slug}" in seen


def test_source_search_bad_page_refuses(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    body, is_error = rt.dispatch_with_status(
        "search",
        {"kind": "paper", "q": "anything", "since": "2020-01-01", "page": 0},
    )
    assert is_error
    assert "page=" in body


def test_source_search_exclude_dead_handle_warns(
    runtime_with_store: PrecisRuntime,
) -> None:
    """A named handle that resolves nowhere is reported, not silently
    skipped (same ``⚠ exclude=`` contract as the paper path)."""
    rt = runtime_with_store
    _seed(rt, "paper", "dead-a", "Dead study", "spintronic magnon deadhandle")

    out = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "spintronic magnon deadhandle",
            "since": "2020-01-01",
            "exclude": ["pa999999"],
        },
    )
    assert "Dead study" in out
    assert "⚠ exclude=" in out


def test_source_search_still_refuses_unknown_view(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``view='index'``/``'ids'`` are no-op aliases; any other view the
    path can't render is still refused."""
    rt = runtime_with_store
    _seed(rt, "paper", "view-a", "View study", "spintronic magnon viewcheck")

    out = rt.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "spintronic magnon viewcheck",
            "since": "2020-01-01",
            "view": "index",
        },
    )
    assert "View study" in out

    body, is_error = rt.dispatch_with_status(
        "search",
        {
            "kind": "paper",
            "q": "spintronic magnon viewcheck",
            "since": "2020-01-01",
            "view": "bogus",
        },
    )
    assert is_error
    assert "not applied" in body


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
