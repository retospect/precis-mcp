"""`Store.search_blocks(mode=…)` — the mode-dispatched entry point behind
the LLM-facing `search(mode=…)`. Verifies lexical-only, semantic-only,
and hybrid routing (incl. the no-embedder degrade)."""

from __future__ import annotations

from uuid import uuid4

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


_BLOCKS = [
    "Nitrate reduction on copper electrodes is fast.",
    "Carbon dioxide capture is an unrelated topic.",
    "Catalysts for nitrogen oxides reduction.",
]


def test_lexical_mode_needs_no_embedder(store: Store) -> None:
    _seed(store, slug="wang2020", blocks=_BLOCKS, embed=False)
    # query_vec=None + mode='lexical' → pure FTS, exact keyword match
    hits = store.search_blocks(q="nitrate copper", mode="lexical", kind="paper")
    assert hits and "nitrate" in hits[0][0].text.lower()


def test_semantic_mode_uses_vector(store: Store) -> None:
    _seed(store, slug="wang2021", blocks=_BLOCKS, embed=True)
    qv = MockEmbedder(dim=1024).embed_one("nitrate reduction copper")
    hits = store.search_blocks(
        q="nitrate", query_vec=qv, mode="semantic", kind="paper", max_distance=None
    )
    assert hits  # cosine ranking returned rows
    # scores are cosine distances (ascending) — non-negative
    assert all(score >= 0 for _b, _r, score in hits)


def test_semantic_mode_degrades_to_lexical_without_vector(store: Store) -> None:
    # embedder down → no query_vec → semantic can't run → lexical fallback
    _seed(store, slug="wang2022", blocks=_BLOCKS, embed=False)
    hits = store.search_blocks(
        q="carbon dioxide", query_vec=None, mode="semantic", kind="paper"
    )
    assert hits and "carbon dioxide" in hits[0][0].text.lower()


def test_hybrid_default_matches_fused(store: Store) -> None:
    rid = _seed(store, slug="wang2023", blocks=_BLOCKS, embed=True)
    qv = MockEmbedder(dim=1024).embed_one("nitrate reduction")
    via_dispatch = store.search_blocks(q="nitrate", query_vec=qv, kind="paper")
    via_fused = store.search_blocks_fused(q="nitrate", query_vec=qv, kind="paper")
    assert [h[0].id for h in via_dispatch] == [h[0].id for h in via_fused]
    assert rid  # seeded


def test_verbatim_mode_requires_all_keywords_present(store: Store) -> None:
    # Verbatim = chunks whose KeyBERT `keywords` array contains ALL query
    # words (`@>` containment, AND). Unique tag so the shared DB can't perturb.
    tag = uuid4().hex[:8]
    rid = _seed(store, slug=f"vb{tag}", blocks=_BLOCKS, embed=False)
    with store.pool.connection() as conn:
        # ord=0 carries both terms; ord=2 carries only one.
        conn.execute(
            "UPDATE chunks SET keywords = %s WHERE ref_id = %s AND ord = 0",
            ([f"nitrate{tag}", f"copper{tag}"], rid),
        )
        conn.execute(
            "UPDATE chunks SET keywords = %s WHERE ref_id = %s AND ord = 2",
            ([f"nitrate{tag}"], rid),
        )
        conn.commit()

    # Both terms present as keywords → exactly the ord=0 chunk.
    hits = store.search_blocks(
        q=f"nitrate{tag} copper{tag}", mode="verbatim", kind="paper"
    )
    assert [h[1].id for h in hits] == [rid]
    assert "copper" in hits[0][0].text.lower()

    # AND semantics: a term absent from every keyword set → no hit (even though
    # `nitrate{tag}` alone appears on two chunks).
    assert (
        store.search_blocks(
            q=f"nitrate{tag} absent{tag}", mode="verbatim", kind="paper"
        )
        == []
    )
    # Empty query → nothing (an empty `@>` would otherwise match every row).
    assert store.search_blocks(q="   ", mode="verbatim", kind="paper") == []


def test_lexical_mode_ignores_supplied_vector(store: Store) -> None:
    # Even with a vector present, mode='lexical' must run FTS only — the
    # ordering should match the pure lexical call.
    _seed(store, slug="wang2024", blocks=_BLOCKS, embed=True)
    qv = MockEmbedder(dim=1024).embed_one("anything")
    lex = store.search_blocks(
        q="nitrogen oxides", query_vec=qv, mode="lexical", kind="paper"
    )
    pure = store.search_blocks_lexical(q="nitrogen oxides", kind="paper")
    assert [h[0].id for h in lex] == [h[0].id for h in pure]
