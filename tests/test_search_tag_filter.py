"""Tests for the tags= filter on store search and handler search.

The filter lives in :mod:`precis.store._tag_filter` and is wired into:

  * ``Store.list_refs``
  * ``Store.count_refs``
  * ``Store.search_refs_lexical``
  * ``Store.search_chunks_lexical``
  * ``Store.search_chunks_semantic``
  * ``Store.search_chunks_fused``

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
from precis.store import ChunkInsert, Store, Tag
from precis.store._tag_filter import (
    _parse_tag_string,
    build_tag_filter,
    tag_filter_string,
)
from tests.conftest import chunk_handle

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
        # Standalone predicate — no leading connector; the caller joins
        # it into its WHERE clause with " AND ".
        assert not frag.startswith(" AND ")
        assert frag.lstrip().startswith("r.ref_id IN")
        # v2 references refs.ref_id (the v1 ``refs.id`` column was
        # renamed in migrations/0001_initial.sql).
        assert "r.ref_id IN" in frag
        # v2 unified tags model: ref_tags is the join table; tags
        # holds (namespace, value).
        assert "ref_tags" in frag
        assert "tags t" in frag
        # AND semantics: HAVING COUNT(DISTINCT want.slot) = N where N=1 —
        # the ``want`` VALUES leg stamps each expanded (ns, val) row with
        # the slot of the input tag that produced it, so a tag counts
        # once whichever of its expansion rows matched (bare-tag
        # OPEN+FLAG, lowercase-axis alias, … — see ``_parse_tag_string``).
        assert "HAVING COUNT(DISTINCT want.slot) = %s" in frag
        # Three params per expansion row (namespace, value, slot) + the count.
        assert params == ["STATUS", "open", 0, 1]

    def test_multi_tag_AND(self) -> None:
        frag, params = build_tag_filter(["STATUS:open", "PRIO:high"])
        # v2 wants (ns, val, slot) triples in the VALUES list: 3
        # placeholders per expansion row + the trailing count =
        # 7 placeholders for 2 single-row tags.
        assert frag.count("%s") == 7
        assert params == ["STATUS", "open", 0, "PRIO", "high", 1, 2]

    def test_block_level_uses_chunk_tags(self) -> None:
        """v2 block-level filter routes through ``chunks`` +
        ``chunk_tags`` instead of v1's ``pos IS NOT NULL`` predicate
        against the v1 ref_tags view."""
        frag, _ = build_tag_filter(["topic:co2"], block_level=True)
        assert "chunk_tags" in frag
        assert "chunks c" in frag
        # No pos sentinels in v2.
        assert "pos IS NOT NULL" not in frag
        assert "pos IS NULL" not in frag

    def test_alias_pluggable(self) -> None:
        frag, _ = build_tag_filter(["x"], ref_alias="ref")
        # v2 references ref.ref_id (column renamed).
        assert "ref.ref_id IN" in frag

    def test_negated_tag_emits_not_in(self) -> None:
        """gr568354: ``'!tag'`` excludes refs carrying the tag."""
        frag, params = build_tag_filter(["!STATUS:done"])
        assert "NOT IN" in frag
        assert "ref_tags" in frag
        # No HAVING — one matching tag row is enough to exclude.
        assert "HAVING" not in frag
        assert params == ["STATUS", "done"]

    def test_negated_plus_positive_ANDs(self) -> None:
        frag, params = build_tag_filter(["topic:x", "!PRIO:high"])
        assert " IN (" in frag
        assert " NOT IN (" in frag
        # Positive leg: 'topic:x' is an open tag → OPEN+FLAG rows of the
        # whole 'topic:x' value (slot 0), + count; negated leg: one
        # (PRIO, high).
        assert params == [
            "OPEN",
            "topic:x",
            0,
            "FLAG",
            "topic:x",
            0,
            1,
            "PRIO",
            "high",
        ]

    def test_negated_block_level_uses_chunk_tags(self) -> None:
        frag, _ = build_tag_filter(["!scratch"], block_level=True)
        assert "NOT IN" in frag
        assert "chunk_tags" in frag

    def test_negated_only_skips_positive_leg(self) -> None:
        frag, params = build_tag_filter(["!a", "!b"])
        # One NOT IN over the union of both negated tags' tuples.
        assert frag.count("NOT IN") == 1
        assert "HAVING" not in frag
        assert params == ["OPEN", "a", "FLAG", "a", "OPEN", "b", "FLAG", "b"]

    def test_exact_axis_escape_matches_stored_lowercase_namespace(self) -> None:
        """gr636493 review (td697200): a stored lowercase axis like
        ``tier`` can't ride the plain ``ns:value`` spelling — it would
        expand to the OPEN/FLAG pair and match nothing. ``=ns:value``
        forces the exact ``(namespace, value)`` pair instead."""
        assert _parse_tag_string("=tier:synthetic-insight") == [
            ("tier", "synthetic-insight")
        ]
        frag, params = build_tag_filter(["=tier:synthetic-insight"])
        assert params == ["tier", "synthetic-insight", 0, 1]
        # Negated exact-axis composes like any other exclusion.
        _frag, neg_params = build_tag_filter(["!=tier:dream"])
        assert neg_params == ["tier", "dream"]

    def test_tag_filter_string_round_trips_stored_pairs(self) -> None:
        """The canonical ``/drive?tag=`` spelling of a stored
        ``(namespace, value)`` row parses back to exactly that pair."""
        assert tag_filter_string("OPEN", "read-later") == "read-later"
        assert tag_filter_string("FLAG", "pinned") == "FLAG:pinned"
        assert tag_filter_string("STATUS", "open") == "STATUS:open"
        assert tag_filter_string("tier", "dream") == "=tier:dream"
        # An OPEN value that itself carries a colon still rides bare.
        assert tag_filter_string("OPEN", "topic:co2") == "topic:co2"

    @pytest.mark.parametrize(
        "value", ["!urgent", "=tier:dream", "STATUS:open", "OPEN:carbon"]
    )
    def test_open_syntax_values_keep_the_stored_pair(self, value: str) -> None:
        spelling = tag_filter_string("OPEN", value)
        assert _parse_tag_string(spelling) == [("OPEN", value)]
        _, params = build_tag_filter([spelling])
        assert params == ["OPEN", value, 0, 1]

    def test_lowercase_axis_alias_expands_to_both_spellings(self) -> None:
        """gr718845: ``studytype:conceptual`` — the facet spelling
        ``precis-paper-tag-axes`` documents — expands to the axis pass's
        stored ``STUDYTYPE`` row AND the literal OPEN/FLAG pair a manual
        ``tag(add=)`` writes."""
        assert _parse_tag_string("studytype:conceptual") == [
            ("STUDYTYPE", "conceptual"),
            ("OPEN", "studytype:conceptual"),
            ("FLAG", "studytype:conceptual"),
        ]
        # Mixed case aliases too; an unregistered lowercase prefix stays
        # an open-tag spelling (``topic:`` is documented open-tag syntax).
        assert _parse_tag_string("Studytype:review")[0] == ("STUDYTYPE", "review")
        assert _parse_tag_string("topic:x") == [
            ("OPEN", "topic:x"),
            ("FLAG", "topic:x"),
        ]

    @pytest.mark.parametrize(
        "axis",
        ["DOMAIN", "MATERIAL", "PROPERTY", "DIM", "SCALE", "STUDYTYPE", "TRANSPORT"],
    )
    def test_documented_paper_facets_alias(self, axis: str) -> None:
        spelling = f"{axis.lower()}:x"
        assert _parse_tag_string(spelling) == [
            (axis, "x"),
            ("OPEN", spelling),
            ("FLAG", spelling),
        ]

    @pytest.mark.parametrize(
        "axis",
        [
            "move",
            "taproot",
            "patent_example",
            "role",
            "role3",
            "open-question",
            "status",
            "sev",
            "junk",
        ],
    )
    def test_other_axes_keep_literal_lowercase_filters(self, axis: str) -> None:
        spelling = f"{axis}:x"
        assert _parse_tag_string(spelling) == [("OPEN", spelling), ("FLAG", spelling)]
        assert tag_filter_string("OPEN", spelling) == spelling

    def test_open_value_on_aliased_axis_keeps_exact_link(self) -> None:
        """td731629 review: a stored OPEN tag whose value spells like an
        aliased facet filter (``studytype:review``) must not ride bare —
        the plain spelling would pull in papers carrying only the closed
        ``STUDYTYPE:review`` row. ``=OPEN:`` preserves the exact pair."""
        assert tag_filter_string("OPEN", "studytype:review") == "=OPEN:studytype:review"
        assert _parse_tag_string("=OPEN:studytype:review") == [
            ("OPEN", "studytype:review")
        ]
        _, params = build_tag_filter(["=OPEN:studytype:review"])
        assert params == ["OPEN", "studytype:review", 0, 1]
        # OPEN values that don't resemble an aliased axis still ride bare.
        assert tag_filter_string("OPEN", "topic:co2") == "topic:co2"
        assert tag_filter_string("OPEN", "status:open") == "status:open"

    def test_lowercase_axis_alias_in_positive_and_negated_legs(self) -> None:
        frag, params = build_tag_filter(["studytype:conceptual"])
        assert params == [
            "STUDYTYPE",
            "conceptual",
            0,
            "OPEN",
            "studytype:conceptual",
            0,
            "FLAG",
            "studytype:conceptual",
            0,
            1,
        ]
        # The negated leg excludes every stored spelling.
        _frag, neg_params = build_tag_filter(["!studytype:conceptual"])
        assert neg_params == [
            "STUDYTYPE",
            "conceptual",
            "OPEN",
            "studytype:conceptual",
            "FLAG",
            "studytype:conceptual",
        ]


# ── store-level: ref-search filter ──────────────────────────────────


def _seed_two_memories(store: Store) -> tuple[int, int]:
    """Create two memory refs, tag the first with topic:co2-capture
    + PRIO:high, the second with topic:nox-reduction. Both share a
    common keyword ('precis') in their body for lexical search."""
    a = store.insert_ref(kind="memory", slug=None, title="precis on co2")
    b = store.insert_ref(kind="memory", slug=None, title="precis on nox")
    # A memory's body prose lives in a memory_body chunk (migration 0050),
    # and memory search matches that chunk — seed one per ref so the shared
    # 'precis' keyword is lexically searchable.
    store.chunks.insert_chunks(
        a.id,
        [ChunkInsert(ord=0, text="precis on co2", meta={"chunk_kind": "memory_body"})],
    )
    store.chunks.insert_chunks(
        b.id,
        [ChunkInsert(ord=0, text="precis on nox", meta={"chunk_kind": "memory_body"})],
    )
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

    def test_negated_tag_alone(self, store: Store) -> None:
        """gr568354: ``'!tag'`` excludes refs carrying the tag."""
        a, b = _seed_two_memories(store)
        refs = store.list_refs(kind="memory", tags=["!topic-nox-reduction"])
        assert {r.id for r in refs} == {a}
        refs = store.list_refs(kind="memory", tags=["!PRIO:high"])
        assert {r.id for r in refs} == {b}

    def test_negated_plus_positive(self, store: Store) -> None:
        """Positive AND still applies; the negation then removes
        carriers of the excluded tag (a carries both topic-co2 and
        PRIO:high, so it drops out)."""
        _seed_two_memories(store)
        refs = store.list_refs(kind="memory", tags=["topic-co2-capture", "!PRIO:high"])
        assert refs == []
        # The same positive tag without the negation still matches.
        refs = store.list_refs(kind="memory", tags=["topic-co2-capture"])
        assert len(refs) == 1

    def test_negated_tag_not_present_keeps_all(self, store: Store) -> None:
        a, b = _seed_two_memories(store)
        refs = store.list_refs(kind="memory", tags=["!topic-no-such"])
        assert {a, b} <= {r.id for r in refs}


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
    e = MockEmbedder(dim=1024)
    a = store.insert_ref(kind="paper", slug="paper-a", title="A study")
    b = store.insert_ref(kind="paper", slug="paper-b", title="B study")
    text = "photocatalysis under visible light improves selectivity"
    store.chunks.insert_chunks(
        a.id, [ChunkInsert(ord=0, text=text, embedding=e.embed_one(text))]
    )
    store.chunks.insert_chunks(
        b.id, [ChunkInsert(ord=0, text=text, embedding=e.embed_one(text))]
    )
    store.add_tag(a.id, Tag.open("topic-co2-capture"))
    store.add_tag(b.id, Tag.open("topic-nox-reduction"))
    return a.id, b.id


class TestSearchBlocksTagFilter:
    def test_lexical_unfiltered_returns_both(self, store: Store) -> None:
        _seed_two_papers_with_blocks(store)
        hits = store.chunks.search_chunks_lexical(q="photocatalysis", kind="paper")
        assert len(hits) == 2

    def test_lexical_filtered(self, store: Store) -> None:
        a, _ = _seed_two_papers_with_blocks(store)
        hits = store.chunks.search_chunks_lexical(
            q="photocatalysis", kind="paper", tags=["topic-co2-capture"]
        )
        assert len(hits) == 1
        assert hits[0][1].id == a

    def test_semantic_filtered(self, store: Store) -> None:
        a, _ = _seed_two_papers_with_blocks(store)
        e = MockEmbedder(dim=1024)
        hits = store.chunks.search_chunks_semantic(
            query_vec=e.embed_one("photocatalysis"),
            kind="paper",
            tags=["topic-co2-capture"],
        )
        assert len(hits) == 1
        assert hits[0][1].id == a

    def test_semantic_tags_and_across_every_listed_tag(self, store: Store) -> None:
        """``tags=`` narrows to refs carrying *every* tag (a recall scoped by
        SPACE: plus another tag must not widen to either one alone)."""
        a, b = _seed_two_papers_with_blocks(store)
        e = MockEmbedder(dim=1024)
        q = e.embed_one("photocatalysis")
        both = store.chunks.search_chunks_semantic(
            query_vec=q,
            kind="paper",
            tags=["topic-co2-capture", "topic-nox-reduction"],
        )
        assert both == []
        none = store.chunks.search_chunks_semantic(
            query_vec=q, kind="paper", tags=["topic-no-such"]
        )
        assert none == []
        one = store.chunks.search_chunks_semantic(
            query_vec=q, kind="paper", tags=["topic-nox-reduction"]
        )
        assert [h[1].id for h in one] == [b]

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
        hits = store.chunks.search_chunks_fused(
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
        assert chunk_handle(store, "paper-a") in out.body
        assert chunk_handle(store, "paper-b") not in out.body

    def test_invalid_tag_rejected_at_handler(self, store: Store) -> None:
        h = PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
        # Use an unknown closed prefix to exercise the handler-
        # boundary rejection. ``paper`` doesn't allow ``PRIO:`` (per
        # ``_KIND_ALLOWED_AXES``), so ``urgent`` no longer collides
        # with ``PRIO:urgent`` on this kind — it just becomes an
        # open tag. The contract being pinned is "tags=[bad] never
        # silently passes the handler boundary."
        with pytest.raises(BadInput):
            h.search(q="x", tags=["NOSUCHAXIS:value"])

    def test_invalid_status_value_rejected(self, store: Store) -> None:
        h = PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))
        # See above — paper doesn't allow STATUS, so the rejection is
        # axis-not-allowed rather than invalid-value. The contract
        # being tested is handler-boundary rejection, not the exact
        # error text.
        with pytest.raises(BadInput):
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

    def test_negated_status_axis_rejected_on_memory(self, store: Store) -> None:
        """``'!STATUS:…'`` gets the same per-kind axis enforcement as
        the positive spelling — a negation can't smuggle a disallowed
        axis past the boundary."""
        h = MemoryHandler(hub=Hub(store=store))
        with pytest.raises(BadInput, match="axis not allowed on kind 'memory'"):
            h.search(q="precis", tags=["!STATUS:open"])

    def test_invalid_status_value_rejected_on_kind_with_status(
        self, store: Store
    ) -> None:
        """The closed-vocab value check still fires for kinds that DO
        use STATUS — exercise it on todo, where STATUS is allowed."""
        from precis.handlers.todo import TodoHandler

        h = TodoHandler(hub=Hub(store=store))
        with pytest.raises(BadInput, match="invalid STATUS value"):
            h.search(q="x", tags=["STATUS:bogus"])  # 'bogus' not in vocab


class TestNegatedTagSearch:
    """gr568354: ``tags=['!<tag>']`` excludes refs carrying the tag —
    alone, beside a positive tag, and beside ``q=``."""

    def test_exclusion_alone(self, store: Store) -> None:
        _seed_two_memories(store)
        h = MemoryHandler(hub=Hub(store=store))
        # Queryless: the recency listing honours the negation —
        # a ("precis on co2") stays, b ("precis on nox", tagged
        # topic-nox-reduction) drops out.
        out = h.search(tags=["!topic-nox-reduction"])
        assert "precis on co2" in out.body
        assert "precis on nox" not in out.body

    def test_exclusion_plus_positive_tag(self, store: Store) -> None:
        _seed_two_memories(store)
        h = MemoryHandler(hub=Hub(store=store))
        # topic-co2-capture narrows to a; a does not carry
        # topic-nox-reduction, so the negation leaves it.
        out = h.search(tags=["topic-co2-capture", "!topic-nox-reduction"])
        assert "precis on co2" in out.body
        # A negation naming the positive tag itself empties the result.
        out = h.search(tags=["topic-co2-capture", "!topic-co2-capture"])
        assert "no memory entries" in out.body

    def test_exclusion_plus_q(self, store: Store) -> None:
        _seed_two_memories(store)
        h = MemoryHandler(hub=Hub(store=store))
        out = h.search(q="precis", tags=["!topic-co2-capture"])
        assert "precis on nox" in out.body
        assert "precis on co2" not in out.body

    def test_bare_bang_refused(self, store: Store) -> None:
        h = MemoryHandler(hub=Hub(store=store))
        with pytest.raises(BadInput, match="negates exactly one tag"):
            h.search(q="x", tags=["!"])

    def test_double_bang_refused(self, store: Store) -> None:
        h = MemoryHandler(hub=Hub(store=store))
        with pytest.raises(BadInput, match="negates exactly one tag"):
            h.search(q="x", tags=["!!topic-x"])


# ── perf hygiene: ref-level filter doesn't match block-tagged rows ──


class TestPosBoundary:
    def test_block_level_tag_does_not_match_ref_level_filter(
        self, store: Store
    ) -> None:
        """If we tag a *chunk* (pos=N), a ref-level filter must NOT
        find that ref. v2 enforces this via the table boundary itself:
        chunk-level tags live in ``chunk_tags``; the ref-level filter
        routes through ``ref_tags`` and therefore can't see them."""
        ref = store.insert_ref(kind="memory", slug=None, title="x")
        store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text="x")])
        # Chunk-level tag on pos=0 (writes ``chunk_tags`` only).
        store.add_tag(ref.id, Tag.open("scratch-note"), pos=0)
        # Ref-level filter (routes through ``ref_tags``) must NOT
        # find this ref.
        refs = store.list_refs(kind="memory", tags=["scratch-note"])
        assert refs == []
        # Block-level filter (routes through ``chunk_tags``) SHOULD
        # find it. v2 uses chunk_tags, not v1's pos sentinel.
        frag, params = build_tag_filter(["scratch-note"], block_level=True)
        assert "chunk_tags" in frag
        # Smoke-execute the fragment to make sure the SQL is valid.
        # ``frag`` is a standalone predicate; the caller joins it with
        # the rest of the WHERE clause via " AND ".
        with store.pool.connection() as conn:
            row = conn.execute(
                f"SELECT count(*) FROM refs r WHERE r.retired_at IS NULL AND {frag}",
                params,
            ).fetchone()
        assert row is not None
        assert row[0] >= 1


class TestBareFlagDedup:
    """gr492995: a bare reserved flag restating a closed value in the same
    filter is dropped; alone it still raises."""

    def test_bare_flag_beside_closed_form_is_dropped(self) -> None:
        assert Tag.normalize_filter(["DREAM:speculative", "speculative"]) == [
            "DREAM:speculative"
        ]

    def test_bare_flag_alone_still_raises(self) -> None:
        with pytest.raises(BadInput):
            Tag.normalize_filter(["speculative"])


class TestNegatedFilterNormalization:
    """gr568354: ``normalize_filter`` canonicalises ``'!<tag>'`` to
    ``'!<canonical>'`` and refuses a bare/empty negation."""

    def test_negated_closed_tag(self) -> None:
        assert Tag.normalize_filter(["!STATUS:open"]) == ["!STATUS:open"]

    def test_negated_open_tag(self) -> None:
        assert Tag.normalize_filter(["!topic-x"]) == ["!topic-x"]

    def test_negated_mixed_with_positive(self) -> None:
        assert Tag.normalize_filter(["STATUS:open", "!PRIO:high"]) == [
            "STATUS:open",
            "!PRIO:high",
        ]

    def test_bare_bang_raises(self) -> None:
        with pytest.raises(BadInput, match="negates exactly one tag"):
            Tag.normalize_filter(["!"])

    def test_double_bang_raises(self) -> None:
        with pytest.raises(BadInput, match="negates exactly one tag"):
            Tag.normalize_filter(["!!topic-x"])

    def test_negated_invalid_value_still_validated(self) -> None:
        with pytest.raises(BadInput):
            Tag.normalize_filter(["!STATUS:bogus"])
