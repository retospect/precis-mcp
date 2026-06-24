"""Card-inclusive paper search.

A *title* / meta query surfaces a paper via its embedded
``card_combined`` (``ord=-1``) even when no body block matches — the
card is opted into both the lexical and semantic legs through the
``card_kinds`` param. A real body hit still wins per paper, so a paper
that matches on both a body block and its card is not double-listed
(``_dedup_card_hits``).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store import BlockInsert, Store
from precis.utils import handle_registry


def _seed(
    store: Store,
    *,
    slug: str,
    body: list[str],
    card_text: str,
    embedder: MockEmbedder | None = None,
    embed_card: bool = False,
) -> tuple[int, list[int], int]:
    """Insert a paper + body blocks + a ``card_combined`` card.

    Returns ``(ref_id, body_block_ids, card_chunk_id)``.
    """
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    e = embedder or MockEmbedder(dim=1024)
    blocks = store.insert_blocks(
        ref.id,
        [
            BlockInsert(pos=i, text=t, embedding=e.embed_one(t))
            for i, t in enumerate(body)
        ],
    )
    cid = store.upsert_card_combined(ref.id, card_text)
    if embed_card:
        store.update_block_embedding(cid, e.embed_one(card_text))
    return ref.id, [b.id for b in blocks], cid


# ── store-level: the card_kinds opt-in ────────────────────────────


def test_lexical_card_kinds_opts_card_in(store: Store) -> None:
    """The title word lives only in the card; default search misses it,
    ``card_kinds=('card_combined',)`` surfaces it."""
    _seed(
        store,
        slug="attn",
        body=["neural sequence to sequence modeling"],
        card_text="Attention Is All You Need Transformer",
    )
    # Default (cards excluded) — the body has no 'transformer'.
    assert store.search_blocks_lexical(q="transformer", kind="paper") == []
    # Opted in — the card surfaces.
    hits = store.search_blocks_lexical(
        q="transformer", kind="paper", card_kinds=("card_combined",)
    )
    assert len(hits) == 1
    block, ref, rank = hits[0]
    assert ref.slug == "attn"
    assert block.chunk_kind == "card_combined"
    assert rank > 0


def test_semantic_card_kinds_opts_card_in(store: Store) -> None:
    e = MockEmbedder(dim=1024)
    _seed(
        store,
        slug="attn",
        body=["completely unrelated body paragraph"],
        card_text="attention transformer architecture",
        embedder=e,
        embed_card=True,
    )
    qv = e.embed_one("attention transformer architecture")
    # Default excludes the card.
    base = store.search_blocks_semantic(query_vec=qv, kind="paper", max_distance=None)
    assert all(b.chunk_kind != "card_combined" for b, _r, _s in base)
    # Opted in finds the card as the nearest neighbour.
    hits = store.search_blocks_semantic(
        query_vec=qv,
        kind="paper",
        max_distance=None,
        card_kinds=("card_combined",),
    )
    assert any(
        b.chunk_kind == "card_combined" and r.slug == "attn" for b, r, _s in hits
    )


def test_count_blocks_lexical_card_kinds(store: Store) -> None:
    """The ``N of K`` count tracks the same card opt-in the search uses."""
    _seed(
        store,
        slug="attn",
        body=["neural sequence modeling"],
        card_text="Attention Is All You Need Transformer",
    )
    assert store.count_blocks_lexical(q="transformer", kind="paper") == 0
    assert (
        store.count_blocks_lexical(
            q="transformer", kind="paper", card_kinds=("card_combined",)
        )
        == 1
    )


def test_bad_card_kind_rejected(store: Store) -> None:
    """A non-``card_`` kind is rejected (literal interpolation guard)."""
    with pytest.raises(BadInput, match="card_kind"):
        store.search_blocks_lexical(q="x", kind="paper", card_kinds=("paragraph",))


# ── handler-level: end-to-end through PaperHandler.search ──────────


def _handler(store: Store) -> PaperHandler:
    # No embedder → lexical degrade; the card opt-in applies on that leg.
    return PaperHandler(hub=Hub(store=store))


def test_handler_title_only_paper_surfaces_via_card(store: Store) -> None:
    """A paper whose body never repeats its title is still found by a
    title query, via the card."""
    _ref_id, _body_ids, cid = _seed(
        store,
        slug="attn",
        body=["we present a model for sequence transduction tasks"],
        card_text="Attention Is All You Need; Vaswani; Shazeer",
    )
    out = _handler(store).search(q="attention all you need")
    card_handle = handle_registry.try_format("paper", cid, chunk=True)
    assert card_handle is not None
    assert card_handle in out.body  # surfaced via the card


def test_handler_body_hit_dedups_card(store: Store) -> None:
    """When both a body block and the card match, only the body block is
    listed — the card is the introducer, not a duplicate hit."""
    _ref_id, body_ids, cid = _seed(
        store,
        slug="attn",
        body=["attention mechanism details and analysis"],
        card_text="attention mechanism overview card",
    )
    out = _handler(store).search(q="attention mechanism")
    body_handle = handle_registry.try_format("paper", body_ids[0], chunk=True)
    card_handle = handle_registry.try_format("paper", cid, chunk=True)
    assert body_handle is not None and card_handle is not None
    assert body_handle in out.body  # the quotable body block wins
    assert card_handle not in out.body  # the card is deduped away
