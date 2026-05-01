"""Tests for the tags= filter on store search and handler search.

The filter lives in :mod:`precis.store._tag_filter` and is wired into:

  * ``Store.list_refs``
  * ``Store.count_refs``
  * ``Store.search_refs_lexical``
  * ``Store.search_blocks_lexical``
  * ``Store.search_blocks_semantic``
  * ``Store.search_blocks_fused``

Plus runtime validation via ``Tag.normalize_filter`` in:

  * ``PaperHandler.search``
  * ``NumericRefHandler.search`` (memory + every other numeric kind)

The MCP critic flagged the absence of this filter; the docs called
it out as "not yet implemented". This file pins the wiring so we
can drop that disclaimer in the same commit.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.handlers.paper import PaperHandler
from precis.store import BlockInsert, Store, Tag
from precis.store._tag_filter import build_tag_filter

# ── unit: build_tag_filter ──────────────────────────────────────────


class TestBuildTagFilter:
    def test_none_is_no_op(self) -> None:
        frag, params = build_tag_filter(None)
        assert frag == ""
        assert params == []

    def test_empty_is_no_op(self) -> None:
        frag, params = build_tag_filter([])
        assert frag == ""
        assert params == []

    def test_single_tag(self) -> None:
        frag, params = build_tag_filter(["STATUS:open"])
        assert frag.startswith(" AND ")
        assert "r.id IN" in frag
        assert "ref_tags" in frag
        assert "pos IS NULL" in frag
        # AND semantics: HAVING COUNT(DISTINCT tag) = N where N = 1.
        assert "HAVING COUNT(DISTINCT tag) = %s" in frag
        assert params == ["STATUS:open", 1]

    def test_multi_tag_AND(self) -> None:
        frag, params = build_tag_filter(["STATUS:open", "PRIO:high"])
        # Two placeholders + the count param.
        assert frag.count("%s") == 3
        assert params == ["STATUS:open", "PRIO:high", 2]

    def test_block_level_uses_pos_not_null(self) -> None:
        frag, _ = build_tag_filter(["topic:co2"], block_level=True)
        assert "pos IS NOT NULL" in frag
        assert "pos IS NULL" not in frag

    def test_alias_pluggable(self) -> None:
        frag, _ = build_tag_filter(["x"], ref_alias="ref")
        assert "ref.id IN" in frag


# ── store-level: ref-search filter ──────────────────────────────────


def _seed_two_memories(store: Store) -> tuple[int, int]:
    """Create two memory refs, tag the first with topic:co2-capture
    + PRIO:high, the second with topic:nox-reduction. Both share a
    common keyword ('precis') in their title for lexical search."""
    cid = store.ensure_corpus("default")
    a = store.insert_ref(corpus_id=cid, kind="memory", slug=None, title="precis on co2")
    b = store.insert_ref(corpus_id=cid, kind="memory", slug=None, title="precis on nox")
    store.add_tag(a.id, Tag.open("topic-co2-capture"))
    store.add_tag(a.id, Tag.closed("PRIO", "high"))
    store.add_tag(b.id, Tag.open("topic-nox-reduction"))
    return a.id, b.id


class TestListRefsTagFilter:
    def test_unfiltered_returns_both(self, store: Store) -> None:
        a, b = _seed_two_memories(store)
        refs = store.list_refs(kind="memory")
        ids = {r.id for r in refs}
        assert {a, b} <= ids

    def test_single_tag_narrows(self, store: Store) -> None:
        a, b = _seed_two_memories(store)
        refs = store.list_refs(kind="memory", tags=["topic-co2-capture"])
        ids = {r.id for r in refs}
        assert ids == {a}

    def test_AND_two_tags_must_both_match(self, store: Store) -> None:
        a, b = _seed_two_memories(store)
        refs = store.list_refs(kind="memory", tags=["topic-co2-capture", "PRIO:high"])
        assert {r.id for r in refs} == {a}

        # b carries only the topic, not PRIO:high → filtered out.
        refs2 = store.list_refs(
            kind="memory", tags=["topic-nox-reduction", "PRIO:high"]
        )
        assert refs2 == []

    def test_no_match_returns_empty(self, store: Store) -> None:
        _seed_two_memories(store)
        refs = store.list_refs(kind="memory", tags=["topic-nonexistent"])
        assert refs == []


class TestCountRefsTagFilter:
    def test_count_matches_list(self, store: Store) -> None:
        _seed_two_memories(store)
        n = store.count_refs(kind="memory", tags=["PRIO:high"])
        assert n == 1

    def test_unfiltered_count(self, store: Store) -> None:
        _seed_two_memories(store)
        assert store.count_refs(kind="memory") == 2


class TestSearchRefsLexicalTagFilter:
    def test_filter_narrows_lexical(self, store: Store) -> None:
        a, b = _seed_two_memories(store)
        # Both titles contain "precis" — without filter, both match.
        unfiltered = store.search_refs_lexical(q="precis", kind="memory")
        assert len(unfiltered) == 2
        # With the filter, only the high-prio ref remains.
        filtered = store.search_refs_lexical(
            q="precis", kind="memory", tags=["PRIO:high"]
        )
        assert len(filtered) == 1
        assert filtered[0][0].id == a


# ── store-level: block search filter ────────────────────────────────


def _seed_two_papers_with_blocks(
    store: Store,
) -> tuple[int, int]:
    """Two papers, each with a block containing 'photocatalysis'.
    Paper a tagged with topic:co2-capture, paper b with topic:nox."""
    cid = store.ensure_corpus("default")
    e = MockEmbedder(dim=1024)
    a = store.insert_ref(corpus_id=cid, kind="paper", slug="paper-a", title="A study")
    b = store.insert_ref(corpus_id=cid, kind="paper", slug="paper-b", title="B study")
    text = "photocatalysis under visible light improves selectivity"
    store.insert_blocks(
        a.id, [BlockInsert(pos=0, text=text, embedding=e.embed_one(text))]
    )
    store.insert_blocks(
        b.id, [BlockInsert(pos=0, text=text, embedding=e.embed_one(text))]
    )
    store.add_tag(a.id, Tag.open("topic-co2-capture"))
    store.add_tag(b.id, Tag.open("topic-nox-reduction"))
    return a.id, b.id


class TestSearchBlocksTagFilter:
    def test_lexical_unfiltered_returns_both(self, store: Store) -> None:
        _seed_two_papers_with_blocks(store)
        hits = store.search_blocks_lexical(q="photocatalysis", kind="paper")
        assert len(hits) == 2

    def test_lexical_filtered(self, store: Store) -> None:
        a, _ = _seed_two_papers_with_blocks(store)
        hits = store.search_blocks_lexical(
            q="photocatalysis", kind="paper", tags=["topic-co2-capture"]
        )
        assert len(hits) == 1
        assert hits[0][1].id == a

    def test_semantic_filtered(self, store: Store) -> None:
        a, _ = _seed_two_papers_with_blocks(store)
        e = MockEmbedder(dim=1024)
        hits = store.search_blocks_semantic(
            query_vec=e.embed_one("photocatalysis"),
            kind="paper",
            tags=["topic-co2-capture"],
        )
        assert len(hits) == 1
        assert hits[0][1].id == a

    def test_fused_filtered_in_BOTH_CTEs(self, store: Store) -> None:
        """Critical correctness pin.

        If the tag filter only applied to the lexical CTE, the
        semantic CTE would surface paper-b's block via embedding
        proximity, and RRF would fuse it back into the result —
        defeating the filter. The runtime applies the filter to
        both CTEs; this test pins that invariant.
        """
        a, b = _seed_two_papers_with_blocks(store)
        e = MockEmbedder(dim=1024)
        hits = store.search_blocks_fused(
            q="photocatalysis",
            query_vec=e.embed_one("photocatalysis"),
            kind="paper",
            tags=["topic-co2-capture"],
        )
        assert len(hits) == 1, (
            f"fused search returned {len(hits)} hits with filter — "
            "the tag filter likely missed one of the two CTEs"
        )
        assert hits[0][1].id == a


# ── handler-level: validation at the agent boundary ─────────────────


class TestPaperHandlerSearchTags:
    def test_valid_tag_passes_through(self, store: Store) -> None:
        a, _ = _seed_two_papers_with_blocks(store)
        h = PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
        out = h.search(q="photocatalysis", tags=["topic-co2-capture"])
        assert "paper-a" in out.body
        assert "paper-b" not in out.body

    def test_invalid_tag_rejected_at_handler(self, store: Store) -> None:
        h = PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
        # ``urgent`` collides with ``PRIO:urgent`` — same rejection
        # shape as put(tags=['urgent']).
        with pytest.raises(BadInput, match="bare flag 'urgent'"):
            h.search(q="x", tags=["urgent"])

    def test_invalid_status_value_rejected(self, store: Store) -> None:
        h = PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
        with pytest.raises(BadInput, match="invalid STATUS value"):
            h.search(q="x", tags=["STATUS:bogus"])


class TestMemorySearchTags:
    def test_filter_narrows_memory_search(self, store: Store) -> None:
        # Memory disallows ``PRIO:`` under per-kind axis enforcement.
        # The narrowing contract is the same with an open tag — what
        # we care about is "filter at handler level passes through to
        # the store and reduces hits."
        a, _ = _seed_two_memories(store)
        h = MemoryHandler(hub=Hub(store=store))
        out = h.search(q="precis", tags=["topic-co2-capture"])
        assert f"id={a}" in out.body or str(a) in out.body
        # Check the count line reflects the narrowed result.
        assert "1 memor" in out.body  # "1 memory match(es)"

    def test_empty_result_mentions_filter(self, store: Store) -> None:
        _seed_two_memories(store)
        h = MemoryHandler(hub=Hub(store=store))
        out = h.search(q="precis", tags=["topic-no-such-thing"])
        assert "no memory entries match" in out.body
        assert "topic-no-such-thing" in out.body

    def test_status_axis_rejected_on_memory_search(self, store: Store) -> None:
        """Per-kind axis enforcement also fires on the search path —
        STATUS: filters against memory raise at the handler boundary
        rather than silently returning zero hits."""
        h = MemoryHandler(hub=Hub(store=store))
        with pytest.raises(BadInput, match="axis not allowed on kind 'memory'"):
            h.search(q="precis", tags=["STATUS:open"])

    def test_invalid_status_value_rejected_on_kind_with_status(
        self, store: Store
    ) -> None:
        """The closed-vocab value check still fires for kinds that DO
        use STATUS — exercise it on todo, where STATUS is allowed."""
        from precis.handlers.todo import TodoHandler

        h = TodoHandler(hub=Hub(store=store))
        with pytest.raises(BadInput, match="invalid STATUS value"):
            h.search(q="x", tags=["STATUS:active"])  # 'active' not in vocab


# ── perf hygiene: ref-level filter doesn't match block-tagged rows ──


class TestPosBoundary:
    def test_block_level_tag_does_not_match_ref_level_filter(
        self, store: Store
    ) -> None:
        """If we tag a *block* (pos=N), a ref-level filter must NOT
        find that ref. Otherwise the helper's pos-IS-NULL gate is
        broken and block-level annotations leak into ref-level
        listings."""
        cid = store.ensure_corpus("default")
        ref = store.insert_ref(corpus_id=cid, kind="memory", slug=None, title="x")
        store.insert_blocks(ref.id, [BlockInsert(pos=0, text="x")])
        # Block-level tag on pos=0.
        store.add_tag(ref.id, Tag.open("scratch"), pos=0)
        # Ref-level filter should NOT find this ref.
        refs = store.list_refs(kind="memory", tags=["scratch"])
        assert refs == []
        # But block-level filter SHOULD find it (via the helper's
        # block_level=True path — note the store doesn't yet expose
        # this kwarg through any agent surface, which is fine).
        frag, params = build_tag_filter(["scratch"], block_level=True)
        assert "pos IS NOT NULL" in frag
        # Smoke-execute the fragment to make sure the SQL is valid.
        with store.pool.connection() as conn:
            row = conn.execute(
                f"SELECT count(*) FROM refs r WHERE r.deleted_at IS NULL{frag}",
                params,
            ).fetchone()
        assert row is not None
        assert row[0] >= 1
