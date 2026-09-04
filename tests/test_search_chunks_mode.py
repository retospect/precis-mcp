"""`Store.chunks.search_chunks(mode=…)` — the mode-dispatched entry point behind
the LLM-facing `search(mode=…)`. Verifies lexical-only, semantic-only,
and hybrid routing (incl. the no-embedder degrade)."""

from __future__ import annotations

from uuid import uuid4

from precis.embedder import MockEmbedder
from precis.store import ChunkInsert, Store
from precis.store.types import Tag


def _seed(store: Store, *, slug: str, blocks: list[str], embed: bool = True) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    e = MockEmbedder(dim=1024)
    rows = [
        ChunkInsert(ord=i, text=t, embedding=(e.embed_one(t) if embed else None))
        for i, t in enumerate(blocks)
    ]
    store.chunks.insert_chunks(ref.id, rows)
    return ref.id


_BLOCKS = [
    "Nitrate reduction on copper electrodes is fast.",
    "Carbon dioxide capture is an unrelated topic.",
    "Catalysts for nitrogen oxides reduction.",
]


def test_lexical_mode_needs_no_embedder(store: Store) -> None:
    _seed(store, slug="wang2020", blocks=_BLOCKS, embed=False)
    # query_vec=None + mode='lexical' → pure FTS, exact keyword match
    hits = store.chunks.search_chunks(q="nitrate copper", mode="lexical", kind="paper")
    assert hits and "nitrate" in hits[0][0].text.lower()


def test_semantic_mode_uses_vector(store: Store) -> None:
    _seed(store, slug="wang2021", blocks=_BLOCKS, embed=True)
    qv = MockEmbedder(dim=1024).embed_one("nitrate reduction copper")
    hits = store.chunks.search_chunks(
        q="nitrate", query_vec=qv, mode="semantic", kind="paper", max_distance=None
    )
    assert hits  # cosine ranking returned rows
    # scores are cosine distances (ascending) — non-negative
    assert all(score >= 0 for _b, _r, score in hits)


def test_semantic_mode_degrades_to_lexical_without_vector(store: Store) -> None:
    # embedder down → no query_vec → semantic can't run → lexical fallback
    _seed(store, slug="wang2022", blocks=_BLOCKS, embed=False)
    hits = store.chunks.search_chunks(
        q="carbon dioxide", query_vec=None, mode="semantic", kind="paper"
    )
    assert hits and "carbon dioxide" in hits[0][0].text.lower()


def test_hybrid_default_matches_fused(store: Store) -> None:
    rid = _seed(store, slug="wang2023", blocks=_BLOCKS, embed=True)
    qv = MockEmbedder(dim=1024).embed_one("nitrate reduction")
    via_dispatch = store.chunks.search_chunks(q="nitrate", query_vec=qv, kind="paper")
    via_fused = store.chunks.search_chunks_fused(
        q="nitrate", query_vec=qv, kind="paper"
    )
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
    hits = store.chunks.search_chunks(
        q=f"nitrate{tag} copper{tag}", mode="verbatim", kind="paper"
    )
    assert [h[1].id for h in hits] == [rid]
    assert "copper" in hits[0][0].text.lower()

    # AND semantics: a term absent from every keyword set → no hit (even though
    # `nitrate{tag}` alone appears on two chunks).
    assert (
        store.chunks.search_chunks(
            q=f"nitrate{tag} absent{tag}", mode="verbatim", kind="paper"
        )
        == []
    )
    # Empty query → nothing (an empty `@>` would otherwise match every row).
    assert store.chunks.search_chunks(q="   ", mode="verbatim", kind="paper") == []


def test_count_chunks_keywords_mirrors_search_chunks_keywords_predicate(
    store: Store,
) -> None:
    """gr311338: ``count_chunks_keywords`` is the honest "N of K"
    denominator for ``mode='verbatim'`` — a *different* universe than
    ``count_chunks_lexical``'s FTS count over the same query text."""
    tag = uuid4().hex[:8]
    rid = _seed(store, slug=f"ck{tag}", blocks=_BLOCKS, embed=False)
    with store.pool.connection() as conn:
        # Only ord=0 carries the KeyBERT keyword; ord=1/2 don't, even
        # though "nitrate"/"reduction" appear in their FTS-indexed text
        # too (ord=2: "Catalysts for nitrogen oxides reduction.").
        conn.execute(
            "UPDATE chunks SET keywords = %s WHERE ref_id = %s AND ord = 0",
            ([f"nitrate{tag}", f"reduction{tag}"], rid),
        )
        conn.commit()

    lexical_total = store.chunks.count_chunks_lexical(
        q=f"nitrate{tag} reduction{tag}", kind="paper"
    )
    verbatim_total = store.chunks.count_chunks_keywords(
        terms=[f"nitrate{tag}", f"reduction{tag}"], kind="paper"
    )
    # FTS matches nothing here (the tagged terms aren't real English
    # tokens the tsvector would stem to each other across chunks), so
    # the interesting assertion is the containment count itself — one
    # chunk, matching the actual keyword-tagged rows.
    assert verbatim_total == 1
    assert lexical_total != verbatim_total or lexical_total == 0

    # Same containment predicate as search_chunks_keywords: count == len(hits).
    hits = store.chunks.search_chunks_keywords(
        terms=[f"nitrate{tag}", f"reduction{tag}"], kind="paper"
    )
    assert len(hits) == verbatim_total


def test_count_chunks_keywords_empty_terms_is_zero(store: Store) -> None:
    # Mirrors search_chunks_keywords: an all-blank terms list must not
    # silently match every row via an empty ``@>``.
    assert store.chunks.count_chunks_keywords(terms=["  ", ""], kind="paper") == 0


def _fenced_verbatim_pair(store: Store, *, tag: str, fence_tag: Tag) -> tuple[int, int]:
    """Seed two refs sharing a unique keyword — one plain, one carrying
    ``fence_tag`` — and update both chunks' ``keywords`` to the tagged
    term. Returns ``(plain_ref_id, fenced_ref_id)``."""
    plain = _seed(store, slug=f"fp{tag}", blocks=_BLOCKS[:1], embed=False)
    fenced = _seed(store, slug=f"ff{tag}", blocks=_BLOCKS[:1], embed=False)
    store.add_tag(fenced, fence_tag)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET keywords = %s WHERE ref_id = ANY(%s) AND ord = 0",
            ([f"fenceterm{tag}"], [plain, fenced]),
        )
        conn.commit()
    return plain, fenced


def test_count_chunks_keywords_fences_speculative(store: Store) -> None:
    # gr311338 follow-up: a DREAM:speculative-tagged chunk that matches
    # the keywords must be excluded from BOTH the search results AND the
    # "N of K" count — a count that includes it while search omits it is
    # exactly the over-claim this fix eliminates.
    tag = uuid4().hex[:8]
    plain, fenced = _fenced_verbatim_pair(
        store, tag=tag, fence_tag=Tag.closed("DREAM", "speculative")
    )
    hits = store.chunks.search_chunks_keywords(terms=[f"fenceterm{tag}"], kind="paper")
    assert {r.id for _b, r, _s in hits} == {plain}
    total = store.chunks.count_chunks_keywords(terms=[f"fenceterm{tag}"], kind="paper")
    assert total == 1
    assert total == len(hits)
    assert fenced  # seeded, fenced out


def test_count_chunks_keywords_fences_wiki(store: Store) -> None:
    tag = uuid4().hex[:8]
    plain, fenced = _fenced_verbatim_pair(
        store, tag=tag, fence_tag=Tag.closed("ORIGIN", "wikipedia")
    )
    hits = store.chunks.search_chunks_keywords(terms=[f"fenceterm{tag}"], kind="paper")
    assert {r.id for _b, r, _s in hits} == {plain}
    total = store.chunks.count_chunks_keywords(terms=[f"fenceterm{tag}"], kind="paper")
    assert total == 1
    assert total == len(hits)
    assert fenced  # seeded, fenced out


def test_count_chunks_keywords_fences_refuted(store: Store) -> None:
    tag = uuid4().hex[:8]
    plain, fenced = _fenced_verbatim_pair(
        store, tag=tag, fence_tag=Tag.closed("STATUS", "refuted")
    )
    hits = store.chunks.search_chunks_keywords(terms=[f"fenceterm{tag}"], kind="paper")
    assert {r.id for _b, r, _s in hits} == {plain}
    total = store.chunks.count_chunks_keywords(terms=[f"fenceterm{tag}"], kind="paper")
    assert total == 1
    assert total == len(hits)
    assert fenced  # seeded, fenced out


def test_count_chunks_keywords_lifts_fence_on_explicit_tag(store: Store) -> None:
    # Listing the control tag in ``tags=`` both lifts the fence AND acts
    # as a real tag filter (``build_tag_filter`` — same as
    # test_speculative_fence.py's ``test_lexical_shows_speculative_on_
    # explicit_tag``), so only the tagged ref matches here — count and
    # search must still agree on that (smaller) universe.
    tag = uuid4().hex[:8]
    plain, fenced = _fenced_verbatim_pair(
        store, tag=tag, fence_tag=Tag.closed("DREAM", "speculative")
    )
    hits = store.chunks.search_chunks_keywords(
        terms=[f"fenceterm{tag}"], kind="paper", tags=["DREAM:speculative"]
    )
    assert {r.id for _b, r, _s in hits} == {fenced}
    total = store.chunks.count_chunks_keywords(
        terms=[f"fenceterm{tag}"], kind="paper", tags=["DREAM:speculative"]
    )
    assert total == 1
    assert total == len(hits)
    assert plain  # seeded, excluded by the tag filter (not by the fence)


def test_count_chunks_keywords_honours_year_range(store: Store) -> None:
    # Mirrors search_chunks_keywords' _year_range_clauses: a chunk whose
    # ref falls outside the after=/before= window must be excluded from
    # both the search results and the count.
    tag = uuid4().hex[:8]
    in_range = store.insert_ref(
        kind="paper", slug=f"yr-in-{tag}", title="in", year=2020
    )
    out_range = store.insert_ref(
        kind="paper", slug=f"yr-out-{tag}", title="out", year=2010
    )
    store.chunks.insert_chunks(
        in_range.id, [ChunkInsert(ord=0, text=_BLOCKS[0], embedding=None)]
    )
    store.chunks.insert_chunks(
        out_range.id, [ChunkInsert(ord=0, text=_BLOCKS[0], embedding=None)]
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET keywords = %s WHERE ref_id = ANY(%s) AND ord = 0",
            ([f"yearterm{tag}"], [in_range.id, out_range.id]),
        )
        conn.commit()

    hits = store.chunks.search_chunks_keywords(
        terms=[f"yearterm{tag}"], kind="paper", year_from=2015, year_to=2025
    )
    assert {r.id for _b, r, _s in hits} == {in_range.id}
    total = store.chunks.count_chunks_keywords(
        terms=[f"yearterm{tag}"], kind="paper", year_from=2015, year_to=2025
    )
    assert total == 1
    assert total == len(hits)


def test_lexical_mode_ignores_supplied_vector(store: Store) -> None:
    # Even with a vector present, mode='lexical' must run FTS only — the
    # ordering should match the pure lexical call.
    _seed(store, slug="wang2024", blocks=_BLOCKS, embed=True)
    qv = MockEmbedder(dim=1024).embed_one("anything")
    lex = store.chunks.search_chunks(
        q="nitrogen oxides", query_vec=qv, mode="lexical", kind="paper"
    )
    pure = store.chunks.search_chunks_lexical(q="nitrogen oxides", kind="paper")
    assert [h[0].id for h in lex] == [h[0].id for h in pure]
