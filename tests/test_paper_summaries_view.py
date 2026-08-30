"""``view='summaries'`` — the flat, per-chunk summary list for a paper.

The agent-surface twin of the web reader's Semantic/Keyword rapid-nav
list: both read ``Store.chunk_llm_summaries_for_ref``. One row per body
chunk, carrying the ``llm-v1`` summary (``chunk_summaries``) and the
KeyBERT keyword string. This drives a real store + ``PaperHandler``
end-to-end.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.paper import PaperHandler
from precis.store import ChunkInsert, Store
from precis.utils import handle_registry


def _seed_paper(store: Store, *, slug: str, n: int) -> int:
    """Insert a paper with ``n`` body chunks, keywords on each, and an
    ``llm-v1`` summary on the first chunk only (the trickle-coverage
    case). Returns the paper ``ref_id``."""
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    e = MockEmbedder(dim=1024)
    blocks = store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=i, text=f"chunk {i} body text", embedding=e.embed_one("x"))
            for i in range(n)
        ],
    )
    with store.pool.connection() as conn:
        for i, b in enumerate(blocks):
            conn.execute(
                "UPDATE chunks SET keywords = %s WHERE chunk_id = %s",
                (["alpha", "beta"], b.id),
            )
        # Only the first chunk gets an llm-v1 summary — the rest fall back
        # to keywords, exercising both columns.
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_id, summarizer, text, status) "
            "VALUES (%s, 'llm-v1', %s, 'ok')",
            (blocks[0].id, "The opening summary."),
        )
    return ref.id


def test_summaries_view_lists_every_chunk_with_summary_and_keywords(
    store: Store,
) -> None:
    hub = Hub(store=store, embedder=MockEmbedder(dim=1024))
    handler = PaperHandler(hub=hub)
    ref_id = _seed_paper(store, slug="nanobuds07", n=5)
    pa = handle_registry.format_handle("paper", ref_id)

    out = handler.get(id=pa, view="summaries").body
    # Headline reports the coverage (1 of 5 chunks has a summary).
    assert out.startswith(f"# {pa} summaries")
    assert "1 with an llm summary" in out
    # Every chunk is a row, addressed by its ~ord handle.
    for i in range(5):
        assert f"{pa}~{i}" in out
    # The summary shows on chunk 0; keywords fill the rest.
    assert "The opening summary." in out
    assert "alpha" in out


def test_summaries_view_in_supported_views(store: Store) -> None:
    """The view is advertised, so an ``Unsupported`` on a typo lists it."""
    hub = Hub(store=store, embedder=MockEmbedder(dim=1024))
    handler = PaperHandler(hub=hub)
    ref_id = _seed_paper(store, slug="listed07", n=3)
    pa = handle_registry.format_handle("paper", ref_id)
    # Path form resolves the same as the kwarg.
    kwarg = handler.get(id=pa, view="summaries").body
    path = handler.get(id=f"{pa}/summaries").body
    assert kwarg == path


def test_chunk_llm_summaries_for_ref_shape_and_scope(store: Store) -> None:
    """The store helper the web /chunks + /search endpoints read."""
    ref_id = _seed_paper(store, slug="scoped07", n=6)
    summaries = store.chunks.chunk_llm_summaries_for_ref(ref_id)
    assert [g["ord"] for g in summaries] == [0, 1, 2, 3, 4, 5]
    assert summaries[0]["summary"] == "The opening summary."
    assert summaries[1]["summary"] == ""  # no summary → empty, keyword fallback
    assert summaries[0]["keywords"] == "alpha, beta"
    # Scope narrows to an ord range inclusively.
    scoped = store.chunks.chunk_llm_summaries_for_ref(ref_id, pos_range=(2, 4))
    assert [g["ord"] for g in scoped] == [2, 3, 4]

    # And the targeted summary batch used by the search path.
    summ = store.chunks.chunk_summaries_for(ref_id, [0, 1, 2])
    assert summ == {0: "The opening summary."}
