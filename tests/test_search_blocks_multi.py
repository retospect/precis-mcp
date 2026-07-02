"""`Store.search_blocks_multi` — multi-leg reciprocal-rank fusion behind
the broad-retrieval `search(queries=, answers=, per_paper=)` path. Checks
that several lexical + semantic legs fuse into one ordered list, that the
per-paper cap spreads results, that it degrades like the single path
when the embedder is unavailable, that the leg fan-out has a hard
ceiling, and that lexical rank ties break deterministically."""

from __future__ import annotations

import pytest

from precis.embedder import MockEmbedder
from precis.store import BlockInsert, Store


def _seed(store: Store, *, slug: str, blocks: list[str], embed: bool = True) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    e = MockEmbedder(dim=1024)
    rows = [
        BlockInsert(pos=i, text=t, embedding=(e.embed_one(t) if embed else None))
        for i, t in enumerate(blocks)
    ]
    store.insert_blocks(ref.id, rows)
    return ref.id


_A = [
    "Single-atom copper boosts nitrate to ammonia selectivity.",
    "Hydrogen evolution competes with nitrate reduction.",
]
_B = [
    "Isolated Cu sites raise faradaic efficiency for ammonia.",
    "An unrelated note about carbon capture membranes.",
]


def _vec(text: str) -> list[float]:
    return MockEmbedder(dim=1024).embed_one(text)


def test_multi_fuses_lexical_and_semantic_legs(store: Store) -> None:
    _seed(store, slug="amA", blocks=_A)
    _seed(store, slug="amB", blocks=_B)
    hits = store.search_blocks_multi(
        q_texts=["nitrate ammonia", "copper selectivity"],
        query_vecs=[_vec("single-atom copper ammonia"), _vec("faradaic efficiency")],
        kind="paper",
        limit=10,
        max_distance=None,
    )
    assert hits  # fused list is non-empty
    # Both papers are reachable through the fused legs.
    slugs = {ref.slug for _b, ref, _s in hits}
    assert "amA" in slugs and "amB" in slugs
    # Fused RRF scores are returned best-first.
    scores = [s for _b, _r, s in hits]
    assert scores == sorted(scores, reverse=True)
    # No chunk appears twice in the fused output.
    cids = [b.id for b, _r, _s in hits]
    assert len(cids) == len(set(cids))


def test_multi_per_paper_cap_spreads(store: Store) -> None:
    _seed(store, slug="capA", blocks=_A)
    _seed(store, slug="capB", blocks=_B)
    hits = store.search_blocks_multi(
        q_texts=["nitrate ammonia copper carbon hydrogen faradaic"],
        query_vecs=[_vec("nitrate ammonia copper")],
        kind="paper",
        limit=10,
        max_distance=None,
        per_paper=1,
    )
    # At most one hit per ref under per_paper=1.
    ref_ids = [ref.id for _b, ref, _s in hits]
    assert ref_ids and len(ref_ids) == len(set(ref_ids))


def test_multi_lexical_mode_ignores_vectors(store: Store) -> None:
    # mode='lexical' must run only the text legs even with vectors present.
    _seed(store, slug="lexA", blocks=_A)
    hits = store.search_blocks_multi(
        q_texts=["nitrate ammonia"],
        query_vecs=[_vec("anything")],
        mode="lexical",
        kind="paper",
        limit=10,
    )
    assert hits and any("nitrate" in b.text.lower() for b, _r, _s in hits)


def test_multi_leg_hard_cap_raises(store: Store) -> None:
    # The MCP surface + paper handler cap queries=/answers= at 8 each;
    # the store enforces a defensive hard ceiling (32 total legs) so a
    # direct (agentic-tier) caller can't fire unbounded SQL fan-out.
    with pytest.raises(ValueError, match="hard cap"):
        store.search_blocks_multi(
            q_texts=[f"query variant {i}" for i in range(33)],
            query_vecs=[],
            kind="paper",
        )
    with pytest.raises(ValueError, match="hard cap"):
        store.search_blocks_multi(
            q_texts=["ok"],
            query_vecs=[_vec(f"t{i}") for i in range(32)],
            kind="paper",
        )


def test_lexical_tie_determinism_chunk_id_order(store: Store) -> None:
    # Equal-rank lexical rows (identical text → identical ts_rank_cd)
    # must return in stable chunk_id order, not whatever the executor
    # felt like — under fusion, unstable ties flipped fused scores and
    # per_paper winners between page 1 and page 2.
    same = "Deterministic tiebreak sentinel phrase for lexical ranking."
    _seed(store, slug="tieA", blocks=[same, same], embed=False)
    _seed(store, slug="tieB", blocks=[same, same], embed=False)
    hits = store.search_blocks_lexical(
        q="deterministic tiebreak sentinel", kind="paper", limit=10
    )
    assert len(hits) == 4
    ranks = [s for _b, _r, s in hits]
    assert len(set(ranks)) == 1  # genuinely tied on ts_rank_cd
    cids = [b.id for b, _r, _s in hits]
    assert cids == sorted(cids)  # tiebreak: ascending chunk_id


def test_multi_semantic_only_degrades_without_vecs(store: Store) -> None:
    # semantic mode but no usable vectors → lexical legs answer (degrade),
    # mirroring the single-path embedder-down fallback.
    _seed(store, slug="degA", blocks=_A, embed=False)
    hits = store.search_blocks_multi(
        q_texts=["hydrogen evolution"],
        query_vecs=[],
        mode="semantic",
        kind="paper",
        limit=10,
    )
    assert hits and any("hydrogen" in b.text.lower() for b, _r, _s in hits)
