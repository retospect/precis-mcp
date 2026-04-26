"""Block-level search tests: lexical / semantic / RRF-fused."""

from __future__ import annotations

from precis.embedder import MockEmbedder
from precis.store import BlockInsert, Store


def _seed_paper(
    store: Store,
    *,
    slug: str,
    title: str,
    blocks: list[str],
    embedder: MockEmbedder | None = None,
    embed: bool = True,
) -> int:
    """Insert a paper ref + N blocks; optionally with mock embeddings."""
    cid = store.ensure_corpus("default")
    ref = store.insert_ref(corpus_id=cid, kind="paper", slug=slug, title=title)
    e = embedder or MockEmbedder(dim=1024)
    rows = []
    for i, t in enumerate(blocks):
        emb = e.embed_one(t) if embed else None
        rows.append(BlockInsert(pos=i, text=t, embedding=emb))
    store.insert_blocks(ref.id, rows)
    return ref.id


# ---------------------------------------------------------------------------
# Lexical
# ---------------------------------------------------------------------------


class TestSearchBlocksLexical:
    def test_finds_matching_text(self, store: Store) -> None:
        _seed_paper(
            store,
            slug="wang2020state",
            title="Wang 2020",
            blocks=[
                "Nitrate reduction on copper electrodes is fast.",
                "Carbon dioxide capture is unrelated.",
                "Catalysts for nitrogen oxides reduction.",
            ],
        )
        hits = store.search_blocks_lexical(q="nitrate copper", kind="paper")
        assert len(hits) >= 1
        block, ref, rank = hits[0]
        assert "nitrate" in block.text.lower()
        assert ref.slug == "wang2020state"
        assert rank > 0

    def test_kind_filter(self, store: Store) -> None:
        _seed_paper(
            store,
            slug="paper1",
            title="P1",
            blocks=["nitrate reduction"],
            embed=False,
        )
        # Memory ref with same word — should be excluded by kind filter.
        cid = store.ensure_corpus("default")
        mem = store.insert_ref(corpus_id=cid, kind="memory", slug=None, title="M")
        store.insert_blocks(
            mem.id, [BlockInsert(pos=0, text="nitrate is in memory too")]
        )

        hits = store.search_blocks_lexical(q="nitrate", kind="paper")
        assert all(ref.kind == "paper" for _, ref, _ in hits)

    def test_scope_ref_id(self, store: Store) -> None:
        rid_a = _seed_paper(
            store,
            slug="paper-a",
            title="A",
            blocks=["nitrate reduction in catalysis"],
            embed=False,
        )
        _seed_paper(
            store,
            slug="paper-b",
            title="B",
            blocks=["nitrate cycling biology"],
            embed=False,
        )
        hits = store.search_blocks_lexical(
            q="nitrate", kind="paper", scope_ref_id=rid_a
        )
        assert len(hits) == 1
        assert hits[0][1].slug == "paper-a"

    def test_excludes_deleted_refs(self, store: Store) -> None:
        rid = _seed_paper(
            store,
            slug="dead",
            title="D",
            blocks=["unique topic xenophilus"],
            embed=False,
        )
        store.soft_delete_ref(rid)
        hits = store.search_blocks_lexical(q="xenophilus", kind="paper")
        assert hits == []

    def test_no_matches_returns_empty(self, store: Store) -> None:
        _seed_paper(
            store,
            slug="p",
            title="P",
            blocks=["alpha"],
            embed=False,
        )
        hits = store.search_blocks_lexical(q="zzqqxx", kind="paper")
        assert hits == []


# ---------------------------------------------------------------------------
# Semantic
# ---------------------------------------------------------------------------


class TestSearchBlocksSemantic:
    def test_returns_blocks_sorted_by_distance(self, store: Store) -> None:
        e = MockEmbedder(dim=1024)
        _seed_paper(
            store,
            slug="p",
            title="P",
            blocks=[
                "alpha beta gamma",
                "delta epsilon zeta",
                "eta theta iota",
            ],
            embedder=e,
        )
        # Query with the first block's exact text — distance should be ~0
        # for that block.
        qv = e.embed_one("alpha beta gamma")
        hits = store.search_blocks_semantic(query_vec=qv, kind="paper")
        assert len(hits) == 3
        # Top hit must be the matching block (distance ~0).
        assert hits[0][0].text == "alpha beta gamma"
        assert hits[0][2] < 1e-5

    def test_excludes_blocks_without_embedding(self, store: Store) -> None:
        e = MockEmbedder(dim=1024)
        cid = store.ensure_corpus("default")
        ref = store.insert_ref(corpus_id=cid, kind="paper", slug="p", title="P")
        store.insert_blocks(
            ref.id,
            [
                BlockInsert(pos=0, text="has", embedding=e.embed_one("has")),
                BlockInsert(pos=1, text="missing"),
            ],
        )
        qv = e.embed_one("has")
        hits = store.search_blocks_semantic(query_vec=qv, kind="paper")
        assert {b.text for b, _, _ in hits} == {"has"}

    def test_scope_ref_id(self, store: Store) -> None:
        e = MockEmbedder(dim=1024)
        rid_a = _seed_paper(
            store,
            slug="a",
            title="A",
            blocks=["target text"],
            embedder=e,
        )
        _seed_paper(
            store,
            slug="b",
            title="B",
            blocks=["target text"],
            embedder=e,
        )
        qv = e.embed_one("target text")
        hits = store.search_blocks_semantic(
            query_vec=qv, kind="paper", scope_ref_id=rid_a
        )
        assert all(ref.id == rid_a for _, ref, _ in hits)


# ---------------------------------------------------------------------------
# Fused (RRF)
# ---------------------------------------------------------------------------


class TestSearchBlocksFused:
    def test_falls_back_to_lexical_when_no_vec(self, store: Store) -> None:
        _seed_paper(
            store,
            slug="p",
            title="P",
            blocks=["nitrate reduction"],
            embed=False,
        )
        hits = store.search_blocks_fused(q="nitrate", kind="paper")
        assert len(hits) == 1
        assert "nitrate" in hits[0][0].text

    def test_combines_lex_and_sem(self, store: Store) -> None:
        e = MockEmbedder(dim=1024)
        _seed_paper(
            store,
            slug="p",
            title="P",
            blocks=[
                "nitrate reduction copper",  # exact lex match
                "alpha beta gamma",  # exact semantic-target match
                "totally unrelated content",
            ],
            embedder=e,
        )
        qv = e.embed_one("alpha beta gamma")
        hits = store.search_blocks_fused(q="nitrate", query_vec=qv, kind="paper")
        # Both the lex-matching and the sem-matching block should
        # surface; unrelated text scores 0.
        texts = [b.text for b, _, _ in hits]
        assert "nitrate reduction copper" in texts
        assert "alpha beta gamma" in texts

    def test_score_descending(self, store: Store) -> None:
        e = MockEmbedder(dim=1024)
        _seed_paper(
            store,
            slug="p",
            title="P",
            blocks=["one two three", "four five six"],
            embedder=e,
        )
        qv = e.embed_one("one two three")
        hits = store.search_blocks_fused(q="one two", query_vec=qv, kind="paper")
        scores = [s for _, _, s in hits]
        assert scores == sorted(scores, reverse=True)

    def test_scope_ref_id_filters(self, store: Store) -> None:
        e = MockEmbedder(dim=1024)
        rid_a = _seed_paper(
            store,
            slug="a",
            title="A",
            blocks=["nitrate cycle"],
            embedder=e,
        )
        _seed_paper(
            store,
            slug="b",
            title="B",
            blocks=["nitrate cycle"],
            embedder=e,
        )
        qv = e.embed_one("nitrate cycle")
        hits = store.search_blocks_fused(
            q="nitrate", query_vec=qv, kind="paper", scope_ref_id=rid_a
        )
        assert all(ref.id == rid_a for _, ref, _ in hits)
