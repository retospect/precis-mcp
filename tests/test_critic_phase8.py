"""Regressions for the third MCP critic pass (phase-8 follow-up).

Covers the deferred items from phase 7 that landed this session:

  * Per-kind axis enforcement.  ``Tag.parse_strict(kind=…)`` rejects
    closed-prefix tags on kinds that don't list the prefix in
    ``_KIND_ALLOWED_AXES``. The MCP critic flagged ``STATUS:open``
    on a memory as a smell — memories have no workflow state.
  * Auto-mirror inverse relations *via read-side rewrite*. ``cites``
    from A to B is stored as a single row; ``links_for(B,
    relation='cited-by')`` rewrites internally to also match
    ``relation='cites'`` rows where B is dst. One-row-per-edge,
    no drift, but the "who cites me?" filter just works.
  * ``total_hits`` header in search responses. Each handler emits
    ``# N of K …`` so an agent that asks for ``top_k=10`` and gets
    exactly 10 hits knows whether there are more.
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.store import BlockInsert, Store, Tag

# ── per-kind axis enforcement ──────────────────────────────────────


class TestPerKindAxisEnforcement:
    def test_status_axis_rejected_on_memory(self) -> None:
        with pytest.raises(BadInput, match="axis not allowed on kind 'memory'"):
            Tag.parse_strict("STATUS:open", kind="memory")

    def test_prio_axis_rejected_on_memory(self) -> None:
        """Memory has an empty closed-axis allowlist — no closed
        prefix at all is permitted, including ``PRIO:``."""
        with pytest.raises(BadInput, match="axis not allowed on kind 'memory'"):
            Tag.parse_strict("PRIO:high", kind="memory")

    def test_status_axis_allowed_on_todo(self) -> None:
        t = Tag.parse_strict("STATUS:open", kind="todo")
        assert t.namespace == "closed"
        assert t.prefix == "STATUS"
        assert t.value == "open"

    def test_status_axis_rejected_on_paper(self) -> None:
        """Papers have no workflow state — STATUS: is not allowed."""
        with pytest.raises(BadInput, match="axis not allowed on kind 'paper'"):
            Tag.parse_strict("STATUS:done", kind="paper")

    def test_src_axis_allowed_on_paper(self) -> None:
        """Papers DO use SRC: (primary vs secondary literature)."""
        t = Tag.parse_strict("SRC:primary", kind="paper")
        assert t.value == "primary"

    def test_kind_none_keeps_global_vocabulary(self) -> None:
        """Callers that don't know their kind at validation time
        (filter queries, migrations) can pass kind=None and get
        the unrestricted global behaviour."""
        # STATUS:open is globally registered, so without kind=
        # restriction it parses fine.
        t = Tag.parse_strict("STATUS:open", kind=None)
        assert t.namespace == "closed"

    def test_open_tags_unaffected_by_kind(self) -> None:
        """Open tags don't go through the axis check — they have
        no closed prefix to vet."""
        t = Tag.parse_strict("topic-noxrr", kind="memory")
        assert t.namespace == "open"
        assert t.value == "topic-noxrr"

    def test_invalid_status_value_still_rejected_globally(self) -> None:
        """The closed-vocab value check still fires when kind=None."""
        with pytest.raises(BadInput, match="invalid STATUS value"):
            Tag.parse_strict("STATUS:bogus", kind=None)

    def test_invalid_status_value_rejected_with_kind(self) -> None:
        """With kind=, the axis check fires first if the kind
        disallows the axis. Otherwise the value check fires."""
        with pytest.raises(BadInput, match="invalid STATUS value"):
            Tag.parse_strict("STATUS:bogus", kind="todo")

    def test_axis_check_on_handler_put(self, store: Store) -> None:
        """End-to-end: handler.put rejects STATUS: on memory at
        the agent boundary."""
        h = MemoryHandler(store=store)
        with pytest.raises(BadInput, match="axis not allowed on kind 'memory'"):
            h.put(text="m", tags=["STATUS:open"])

    def test_axis_check_on_handler_search(self, store: Store) -> None:
        """End-to-end: handler.search also rejects STATUS: on
        memory in the tags= filter (so agents don't get silent
        zero-hits responses)."""
        h = MemoryHandler(store=store)
        with pytest.raises(BadInput, match="axis not allowed on kind 'memory'"):
            h.search(q="x", tags=["STATUS:open"])


# ── auto-mirror inverse relations (read-side rewrite) ─────────────


class TestInverseRelationRewrite:
    def _seed_citation(self, store: Store) -> tuple[int, int]:
        """Insert a citation edge A→B with relation='cites'.
        Returns (a_id, b_id)."""
        cid = store.ensure_corpus("default")
        a = store.insert_ref(corpus_id=cid, kind="paper", slug="paper-a", title="A")
        b = store.insert_ref(corpus_id=cid, kind="paper", slug="paper-b", title="B")
        store.add_link(src_ref_id=a.id, dst_ref_id=b.id, relation="cites")
        return a.id, b.id

    def test_one_row_stored(self, store: Store) -> None:
        """Single-row contract: ``add_link(rel='cites')`` inserts
        exactly one row, not a mirrored pair."""
        a_id, b_id = self._seed_citation(store)
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM links WHERE src_ref_id IN (%s, %s)",
                (a_id, b_id),
            ).fetchone()
        assert n is not None
        assert n[0] == 1

    def test_outbound_cites_from_a(self, store: Store) -> None:
        a_id, b_id = self._seed_citation(store)
        out = store.links_for(a_id, relation="cites", direction="out")
        assert len(out) == 1
        assert out[0].src_ref_id == a_id
        assert out[0].dst_ref_id == b_id
        assert out[0].relation == "cites"

    def test_outbound_cited_by_from_b_finds_inverse(self, store: Store) -> None:
        """The motivating case: ``links_for(B, relation='cited-by',
        direction='out')`` finds the ``cites`` edge where B is dst.
        Stored row's relation is still 'cites' — caller handles
        the labelling."""
        a_id, b_id = self._seed_citation(store)
        out = store.links_for(b_id, relation="cited-by", direction="out")
        assert len(out) == 1
        # The row was stored as 'cites' but matches the 'cited-by'
        # filter via the inverse rewrite.
        assert out[0].relation == "cites"
        assert out[0].src_ref_id == a_id
        assert out[0].dst_ref_id == b_id

    def test_inbound_cites_to_b(self, store: Store) -> None:
        """Direction='in' with relation='cites' on B: literal-
        relation match (B is dst, the row's relation is cites)."""
        a_id, b_id = self._seed_citation(store)
        out = store.links_for(b_id, relation="cites", direction="in")
        assert len(out) == 1
        assert out[0].src_ref_id == a_id

    def test_no_relation_filter_returns_both_sides(self, store: Store) -> None:
        """``relation=None`` keeps the original direction='both'
        behaviour: one row per edge, regardless of which side
        you query from."""
        a_id, b_id = self._seed_citation(store)
        out = store.links_for(b_id, direction="both")
        assert len(out) == 1

    def test_remove_link_removes_single_row(self, store: Store) -> None:
        """``remove_link`` only deletes the named row — there is
        no shadow row to clean up."""
        a_id, b_id = self._seed_citation(store)
        n = store.remove_link(src_ref_id=a_id, dst_ref_id=b_id, relation="cites")
        assert n == 1
        # Inverse-direction query now returns nothing.
        assert store.links_for(b_id, relation="cited-by", direction="out") == []

    def test_symmetric_relation_no_rewrite(self, store: Store) -> None:
        """``related-to`` is symmetric — not in _INVERSE_RELATIONS,
        so links_for behaves traditionally."""
        cid = store.ensure_corpus("default")
        a = store.insert_ref(corpus_id=cid, kind="memory", slug=None, title="A")
        b = store.insert_ref(corpus_id=cid, kind="memory", slug=None, title="B")
        store.add_link(src_ref_id=a.id, dst_ref_id=b.id, relation="related-to")
        # One row stored.
        out = store.links_for(b.id, direction="both")
        assert len(out) == 1

    def test_see_also_no_rewrite(self, store: Store) -> None:
        """``see-also`` has no inverse — direction='out' from the
        target side returns nothing (as expected, since see-also
        is one-way for context)."""
        cid = store.ensure_corpus("default")
        a = store.insert_ref(corpus_id=cid, kind="memory", slug=None, title="A")
        b = store.insert_ref(corpus_id=cid, kind="memory", slug=None, title="B")
        store.add_link(src_ref_id=a.id, dst_ref_id=b.id, relation="see-also")
        out = store.links_for(b.id, relation="see-also", direction="out")
        assert out == []


# ── total_hits header in search responses ─────────────────────────


class TestTotalHitsHeader:
    def test_header_format_when_capped(self) -> None:
        """The helper renders 'N of K' when total > n_returned."""
        from precis.utils.search_header import format_search_headline

        line = format_search_headline(
            n_returned=10, total=42, noun="paper match", query="x"
        )
        assert line == "# 10 of 42 paper matches for 'x'"

    def test_header_format_when_not_capped(self) -> None:
        """When the agent saw everything, the redundant 'N of N'
        is suppressed."""
        from precis.utils.search_header import format_search_headline

        line = format_search_headline(
            n_returned=3, total=3, noun="paper match", query="x"
        )
        assert line == "# 3 paper matches for 'x'"

    def test_header_format_when_no_total(self) -> None:
        """``total=None`` (semantic-only search) drops the 'of K'."""
        from precis.utils.search_header import format_search_headline

        line = format_search_headline(
            n_returned=5, total=None, noun="block hit", query="x"
        )
        assert line == "# 5 block hits for 'x'"

    def test_header_format_singular(self) -> None:
        """Single result drops the plural 's'."""
        from precis.utils.search_header import format_search_headline

        line = format_search_headline(
            n_returned=1, total=1, noun="memory match", query="x"
        )
        assert line == "# 1 memory match for 'x'"

    def test_count_refs_lexical_matches_search(self, store: Store) -> None:
        """The companion count method must produce the same number
        as len(search_refs_lexical(limit=∞))."""
        cid = store.ensure_corpus("default")
        for i in range(15):
            store.insert_ref(
                corpus_id=cid,
                kind="memory",
                slug=None,
                title=f"precis fact {i}",
            )
        total = store.count_refs_lexical(q="precis", kind="memory")
        # Cross-check: search at high limit returns all of them.
        all_hits = store.search_refs_lexical(q="precis", kind="memory", limit=100)
        assert total == len(all_hits) == 15

    def test_count_blocks_lexical_matches_search(self, store: Store) -> None:
        """Same shape for blocks — counts must agree with searches."""
        cid = store.ensure_corpus("default")
        ref = store.insert_ref(corpus_id=cid, kind="paper", slug="p", title="P")
        store.insert_blocks(
            ref.id,
            [
                BlockInsert(pos=i, text=f"photocatalysis sample text {i}")
                for i in range(8)
            ],
        )
        total = store.count_blocks_lexical(q="photocatalysis", kind="paper")
        all_hits = store.search_blocks_lexical(
            q="photocatalysis", kind="paper", limit=100
        )
        assert total == len(all_hits) == 8

    def test_search_response_renders_total_when_capped(self, store: Store) -> None:
        """End-to-end: handler.search renders the header with 'of K'
        when top_k truncates the result set."""
        cid = store.ensure_corpus("default")
        for i in range(5):
            store.insert_ref(
                corpus_id=cid,
                kind="memory",
                slug=None,
                title=f"precis fact {i}",
            )
        h = MemoryHandler(store=store)
        out = h.search(q="precis", top_k=2)
        # 2 hits returned, 5 total. Header should reflect both.
        assert "2 of 5" in out.body

    def test_search_response_no_total_when_uncapped(self, store: Store) -> None:
        """When the agent already saw everything, no 'of K'."""
        cid = store.ensure_corpus("default")
        for i in range(2):
            store.insert_ref(
                corpus_id=cid,
                kind="memory",
                slug=None,
                title=f"precis fact {i}",
            )
        h = MemoryHandler(store=store)
        out = h.search(q="precis", top_k=10)
        # All 2 returned. No "of N" trailer.
        assert "of 2" not in out.body
        assert "2 memory match" in out.body
