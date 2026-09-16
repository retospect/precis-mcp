"""gr340057: ``NumericRefHandler.search_hits(mode=...)`` — the cross-kind
fan-out path used to let ``mode=`` fall into ``**_kw`` and vanish, so a
``search(mode='semantic')`` still ran the normal hybrid stream for every
kind built on this base (job, todo, memory, gripe, ...). Pins that ``mode``
is actually threaded into both branches (ref-level ``fused_ref_hits`` and
the body-chunk ``_best_body_hits`` → ``store.chunks.search_chunks``), and
that the body-chunk branch produces a genuinely restricted result set under
``mode='semantic'`` — mirroring ``PaperHandler.search_hits``.
"""

from __future__ import annotations

from unittest.mock import patch

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers import _numeric_ref
from precis.handlers.job import JobHandler
from precis.handlers.memory import MemoryHandler
from precis.store import ChunkInsert, Store

# ── ref-level branch (job/todo/etc.): mode must reach fused_ref_hits ───


def test_ref_level_search_hits_forwards_explicit_mode(hub: Hub) -> None:
    """Before the fix, ``mode`` fell into ``**_kw`` and ``fused_ref_hits``
    always saw ``mode=None`` regardless of the caller's request."""
    jobs = JobHandler(hub=hub)
    with patch.object(
        _numeric_ref, "fused_ref_hits", wraps=_numeric_ref.fused_ref_hits
    ) as spy:
        jobs.search_hits(q="anything", mode="semantic")
    assert spy.call_args.kwargs.get("mode") == "semantic"


def test_ref_level_search_hits_default_mode_is_none(hub: Hub) -> None:
    jobs = JobHandler(hub=hub)
    with patch.object(
        _numeric_ref, "fused_ref_hits", wraps=_numeric_ref.fused_ref_hits
    ) as spy:
        jobs.search_hits(q="anything")
    assert spy.call_args.kwargs.get("mode") is None


# ── body-chunk branch (memory/gripe): mode must reach search_chunks ────


def test_body_chunk_search_hits_forwards_mode_to_search_chunks(hub: Hub) -> None:
    memory = MemoryHandler(hub=hub)
    with patch.object(
        hub.store.chunks, "search_chunks", wraps=hub.store.chunks.search_chunks
    ) as spy:
        memory.search_hits(q="anything", mode="lexical")
    assert spy.call_args.kwargs.get("mode") == "lexical"


def test_body_chunk_semantic_mode_restricts_to_close_matches(store: Store) -> None:
    """The distinguishing behavioral test: ``mode='semantic'`` on a
    body-chunk NumericRefHandler kind must produce a *different*
    (restricted) result set than the default hybrid stream — the same
    ``SEMANTIC_DISTANCE_FLOOR`` cut ``PaperHandler`` applies, not the
    unrestricted hybrid RRF fusion this used to always run."""
    e = MockEmbedder(dim=store.embedding_dim())
    hub = Hub(store=store, embedder=e)
    memory = MemoryHandler(hub=hub)

    marker = "gr340057uniq"
    query = f"{marker} exact phrase carried into the semantic leg"

    close = store.insert_ref(kind="memory", slug=None, title="close", meta={})
    store.chunks.insert_chunks(
        close.id, [ChunkInsert(ord=0, text=query, embedding=e.embed_one(query))]
    )
    far_text = f"{marker} — a wholly unrelated sentence about gardening topics"
    far = store.insert_ref(kind="memory", slug=None, title="far", meta={})
    store.chunks.insert_chunks(
        far.id, [ChunkInsert(ord=0, text=far_text, embedding=e.embed_one(far_text))]
    )

    # Default hybrid: both refs match via the lexical leg on the shared
    # marker term (the semantic leg, embedding the bare marker, is close
    # to neither and doesn't add filtering).
    hybrid_ids = {h.ref_id for h in memory.search_hits(q=marker, page_size=10)}
    assert {close.id, far.id} <= hybrid_ids

    # mode='semantic' on the full query phrase: only the chunk whose text
    # IS the query (distance 0) survives the SEMANTIC_DISTANCE_FLOOR cut;
    # the lexical-only match is excluded outright, not just re-ranked.
    semantic_ids = {
        h.ref_id for h in memory.search_hits(q=query, mode="semantic", page_size=10)
    }
    assert close.id in semantic_ids
    assert far.id not in semantic_ids


def test_body_chunk_lexical_mode_skips_embedding(store: Store) -> None:
    """``mode='lexical'`` must still answer via pure FTS — no embedder
    round-trip needed (mirrors ``query_vec_for``'s lexical/verbatim skip)."""
    hub_no_embedder = Hub(store=store)
    memory = MemoryHandler(hub=hub_no_embedder)

    marker = "gr340057lex"
    ref = store.insert_ref(kind="memory", slug=None, title="lex-only", meta={})
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=f"{marker} appears in plain text", embedding=None)],
    )

    hits = memory.search_hits(q=marker, mode="lexical", page_size=10)
    assert {h.ref_id for h in hits} == {ref.id}
