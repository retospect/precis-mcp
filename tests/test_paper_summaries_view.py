"""``view='summaries'`` — the flat, per-chunk gloss list for a paper.

The agent-surface twin of the web reader's Semantic/Keyword rapid-nav
list: both read ``Store.chunk_glosses_for_ref``. One row per body chunk,
carrying the ``llm-v1`` gloss (``chunk_summaries``) and the KeyBERT
keyword string. This drives a real store + ``PaperHandler`` end-to-end.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.paper import PaperHandler
from precis.store import BlockInsert, Store
from precis.utils import handle_registry


def _seed_paper(store: Store, *, slug: str, n: int) -> int:
    """Insert a paper with ``n`` body chunks, keywords on each, and an
    ``llm-v1`` summary on the first chunk only (the trickle-coverage
    case). Returns the paper ``ref_id``."""
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    e = MockEmbedder(dim=1024)
    blocks = store.insert_blocks(
        ref.id,
        [
            BlockInsert(pos=i, text=f"chunk {i} body text", embedding=e.embed_one("x"))
            for i in range(n)
        ],
    )
    with store.pool.connection() as conn:
        for i, b in enumerate(blocks):
            conn.execute(
                "UPDATE chunks SET keywords = %s WHERE chunk_id = %s",
                (["alpha", "beta"], b.id),
            )
        # Only the first chunk gets an llm-v1 gloss — the rest fall back
        # to keywords, exercising both columns.
        conn.execute(
            "INSERT INTO chunk_summaries (chunk_id, summarizer, text, status) "
            "VALUES (%s, 'llm-v1', %s, 'ok')",
            (blocks[0].id, "The opening gloss."),
        )
    return ref.id


def test_summaries_view_lists_every_chunk_with_gloss_and_keywords(
    store: Store,
) -> None:
    hub = Hub(store=store, embedder=MockEmbedder(dim=1024))
    handler = PaperHandler(hub=hub)
    ref_id = _seed_paper(store, slug="nanobuds07", n=5)
    pa = handle_registry.format_handle("paper", ref_id)

    out = handler.get(id=pa, view="summaries").body
    # Headline reports the coverage (1 of 5 chunks has a gloss).
    assert out.startswith(f"# {pa} summaries")
    assert "1 with an llm gloss" in out
    # Every chunk is a row, addressed by its ~ord handle.
    for i in range(5):
        assert f"{pa}~{i}" in out
    # The gloss shows on chunk 0; keywords fill the rest.
    assert "The opening gloss." in out
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


def test_chunk_glosses_for_ref_shape_and_scope(store: Store) -> None:
    """The store helper the web /chunks + /search endpoints read."""
    ref_id = _seed_paper(store, slug="scoped07", n=6)
    glosses = store.chunk_glosses_for_ref(ref_id)
    assert [g["ord"] for g in glosses] == [0, 1, 2, 3, 4, 5]
    assert glosses[0]["summary"] == "The opening gloss."
    assert glosses[1]["summary"] == ""  # no gloss → empty, keyword fallback
    assert glosses[0]["keywords"] == "alpha, beta"
    # Scope narrows to an ord range inclusively.
    scoped = store.chunk_glosses_for_ref(ref_id, pos_range=(2, 4))
    assert [g["ord"] for g in scoped] == [2, 3, 4]

    # And the targeted summary batch used by the search path.
    summ = store.chunk_summaries_for(ref_id, [0, 1, 2])
    assert summ == {0: "The opening gloss."}
