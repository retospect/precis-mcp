"""Tests for the ``link=`` / ``unlink=`` / ``rel=`` CRUD on numeric refs.

Covers four layers:

  1. ``parse_link_target`` — canonical syntax parser.
  2. ``Store.add_link`` / ``remove_link`` / ``links_for`` — the
     row-level operations and their idempotency / self-loop /
     direction semantics.
  3. ``MemoryHandler.put`` integration — link/unlink/rel kwargs end
     to end, including create-time and update-time wiring.
  4. ``MemoryHandler.get(view='links')`` — the rendered view.

A separate registration test pins that the relations migration
seeded every slug the ``Relation`` literal type claims, so the
literal stays a real schema mirror rather than drifting into
fiction.
"""

from __future__ import annotations

from typing import get_args

import pytest

from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._link_target import LinkTarget, parse_link_target
from precis.handlers.memory import MemoryHandler
from precis.store import BlockInsert, Store
from precis.store.types import Relation

# ── unit: parse_link_target ─────────────────────────────────────────


@pytest.fixture
def memory_handler(store: Store) -> MemoryHandler:
    return MemoryHandler(store=store)


def _seed_paper(store: Store, slug: str = "wang2020state") -> int:
    cid = store.ensure_corpus("default")
    ref = store.insert_ref(
        corpus_id=cid,
        kind="paper",
        slug=slug,
        title=f"Test paper {slug}",
        provider="manual",
        meta={},
    )
    return ref.id


def _seed_paper_with_blocks(
    store: Store, slug: str = "wang2020state", n_blocks: int = 3
) -> int:
    ref_id = _seed_paper(store, slug=slug)
    store.insert_blocks(
        ref_id,
        [BlockInsert(pos=i, text=f"block {i}", slug=f"b{i}") for i in range(n_blocks)],
    )
    return ref_id


def _seed_memory(store: Store, title: str = "test memory") -> int:
    cid = store.ensure_corpus("default")
    ref = store.insert_ref(corpus_id=cid, kind="memory", slug=None, title=title)
    return ref.id


class TestParseLinkTarget:
    def test_slug_kind_ref_level(self, store: Store) -> None:
        ref_id = _seed_paper(store)
        target = parse_link_target("paper:wang2020state", store=store)
        assert target == LinkTarget(
            ref_id=ref_id, pos=None, kind="paper", raw="paper:wang2020state"
        )

    def test_numeric_kind_ref_level(self, store: Store) -> None:
        mem_id = _seed_memory(store)
        target = parse_link_target(f"memory:{mem_id}", store=store)
        assert target.ref_id == mem_id
        assert target.pos is None
        assert target.kind == "memory"

    def test_block_pos_selector(self, store: Store) -> None:
        ref_id = _seed_paper_with_blocks(store, n_blocks=5)
        target = parse_link_target("paper:wang2020state~3", store=store)
        assert target.ref_id == ref_id
        assert target.pos == 3

    def test_block_slug_selector(self, store: Store) -> None:
        ref_id = _seed_paper_with_blocks(store, n_blocks=3)
        target = parse_link_target("paper:wang2020state~b1", store=store)
        assert target.ref_id == ref_id
        assert target.pos == 1

    def test_missing_colon_rejected(self, store: Store) -> None:
        with pytest.raises(BadInput, match="missing required 'kind:' prefix"):
            parse_link_target("wang2020state", store=store)

    def test_empty_kind_rejected(self, store: Store) -> None:
        with pytest.raises(BadInput, match="empty kind"):
            parse_link_target(":wang2020state", store=store)

    def test_empty_identifier_rejected(self, store: Store) -> None:
        with pytest.raises(BadInput, match="empty identifier"):
            parse_link_target("paper:", store=store)

    def test_unknown_kind_rejected(self, store: Store) -> None:
        with pytest.raises(BadInput, match="unknown kind 'banana'"):
            parse_link_target("banana:something", store=store)

    def test_numeric_kind_rejects_slug_id(self, store: Store) -> None:
        with pytest.raises(BadInput, match="numeric — identifier must be an integer"):
            parse_link_target("memory:not-an-int", store=store)

    def test_missing_ref_raises_notfound(self, store: Store) -> None:
        with pytest.raises(NotFound, match="resolves to no live paper ref"):
            parse_link_target("paper:does-not-exist", store=store)

    def test_missing_block_pos_raises_notfound(self, store: Store) -> None:
        _seed_paper_with_blocks(store, n_blocks=2)
        with pytest.raises(NotFound, match=r"no block at pos=99"):
            parse_link_target("paper:wang2020state~99", store=store)

    def test_missing_block_slug_raises_notfound(self, store: Store) -> None:
        _seed_paper_with_blocks(store, n_blocks=2)
        with pytest.raises(NotFound, match="no block with slug='nope'"):
            parse_link_target("paper:wang2020state~nope", store=store)

    def test_negative_pos_rejected(self, store: Store) -> None:
        _seed_paper_with_blocks(store, n_blocks=2)
        with pytest.raises(BadInput, match="negative block pos"):
            parse_link_target("paper:wang2020state~-1", store=store)

    def test_empty_selector_after_tilde_rejected(self, store: Store) -> None:
        _seed_paper(store)
        with pytest.raises(BadInput, match="empty block selector"):
            parse_link_target("paper:wang2020state~", store=store)


# ── store: add_link / remove_link / links_for ──────────────────────


class TestStoreLinkCRUD:
    def test_add_link_basic(self, store: Store) -> None:
        a = _seed_memory(store)
        b = _seed_memory(store)
        link = store.add_link(src_ref_id=a, dst_ref_id=b, relation="related-to")
        assert link.src_ref_id == a
        assert link.dst_ref_id == b
        assert link.src_pos is None
        assert link.dst_pos is None
        assert link.relation == "related-to"

    def test_add_link_idempotent(self, store: Store) -> None:
        a = _seed_memory(store)
        b = _seed_memory(store)
        l1 = store.add_link(src_ref_id=a, dst_ref_id=b)
        l2 = store.add_link(src_ref_id=a, dst_ref_id=b)
        # Same row id on conflict — the ON CONFLICT DO UPDATE
        # SET set_by = links.set_by RETURNING form yields the
        # existing row.
        assert l1.id == l2.id

    def test_add_link_different_relations_create_two_rows(self, store: Store) -> None:
        a = _seed_memory(store)
        b = _seed_memory(store)
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="cites")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="contradicts")
        out = store.links_for(a, direction="out")
        assert {l.relation for l in out} == {"cites", "contradicts"}

    def test_self_loop_at_ref_level_rejected(self, store: Store) -> None:
        a = _seed_memory(store)
        with pytest.raises(BadInput, match="cannot link a ref to itself"):
            store.add_link(src_ref_id=a, dst_ref_id=a)

    def test_self_loop_different_pos_allowed(self, store: Store) -> None:
        # A memory with two blocks linking block~5 → block~7 is fine.
        # Memories don't typically have blocks but the schema allows
        # it; use a paper for the seeding.
        paper = _seed_paper_with_blocks(store, n_blocks=10)
        link = store.add_link(
            src_ref_id=paper,
            src_pos=5,
            dst_ref_id=paper,
            dst_pos=7,
            relation="see-also",
        )
        assert link.src_pos == 5
        assert link.dst_pos == 7

    def test_remove_link_specific_relation(self, store: Store) -> None:
        a = _seed_memory(store)
        b = _seed_memory(store)
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="cites")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="contradicts")

        deleted = store.remove_link(src_ref_id=a, dst_ref_id=b, relation="cites")
        assert deleted == 1
        out = store.links_for(a, direction="out")
        assert [l.relation for l in out] == ["contradicts"]

    def test_remove_link_any_relation(self, store: Store) -> None:
        a = _seed_memory(store)
        b = _seed_memory(store)
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="cites")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="contradicts")

        # relation=None removes both.
        deleted = store.remove_link(src_ref_id=a, dst_ref_id=b)
        assert deleted == 2
        assert store.links_for(a, direction="out") == []

    def test_remove_link_missing_is_no_op(self, store: Store) -> None:
        a = _seed_memory(store)
        b = _seed_memory(store)
        # No link exists; remove returns 0, not an error.
        assert store.remove_link(src_ref_id=a, dst_ref_id=b) == 0

    def test_links_for_directions(self, store: Store) -> None:
        a = _seed_memory(store)
        b = _seed_memory(store)
        c = _seed_memory(store)
        store.add_link(src_ref_id=a, dst_ref_id=b)  # a → b
        store.add_link(src_ref_id=c, dst_ref_id=a)  # c → a (inbound to a)

        out = store.links_for(a, direction="out")
        in_ = store.links_for(a, direction="in")
        both = store.links_for(a, direction="both")

        assert len(out) == 1 and out[0].dst_ref_id == b
        assert len(in_) == 1 and in_[0].src_ref_id == c
        assert len(both) == 2

    def test_links_for_relation_filter(self, store: Store) -> None:
        a = _seed_memory(store)
        b = _seed_memory(store)
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="cites")
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="contradicts")

        cites_only = store.links_for(a, relation="cites")
        assert len(cites_only) == 1
        assert cites_only[0].relation == "cites"


# ── relations vocabulary: literal vs schema parity ─────────────────


class TestRelationsVocabularyMatchesSchema:
    def test_every_literal_relation_exists_in_schema(self, store: Store) -> None:
        """The ``Relation`` typing literal must be a strict subset of
        the seeded ``relations`` table — otherwise a put would
        type-check but FK-violate at INSERT time."""
        with store.pool.connection() as conn:
            rows = conn.execute("SELECT slug FROM relations").fetchall()
        seeded = {r[0] for r in rows}
        for slug in get_args(Relation):
            assert slug in seeded, (
                f"Relation literal {slug!r} not found in schema; "
                "either add a migration or trim the literal"
            )


# ── handler: link/unlink/rel on put ────────────────────────────────


class TestMemoryHandlerLink:
    def test_link_on_create(self, memory_handler: MemoryHandler, store: Store) -> None:
        target = _seed_paper(store)
        out = memory_handler.put(text="see this paper", link="paper:wang2020state")
        # Extract memory id from "created memory id=N"
        new_id = int(out.body.split("=")[-1].strip().split()[0])
        links = store.links_for(new_id, direction="out")
        assert len(links) == 1
        assert links[0].dst_ref_id == target
        assert links[0].relation == "related-to"

    def test_link_with_explicit_rel(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        _seed_paper(store)
        out = memory_handler.put(
            text="rebuts", link="paper:wang2020state", rel="contradicts"
        )
        new_id = int(out.body.split("=")[-1].strip().split()[0])
        links = store.links_for(new_id, direction="out")
        assert links[0].relation == "contradicts"

    def test_link_to_block(self, memory_handler: MemoryHandler, store: Store) -> None:
        target = _seed_paper_with_blocks(store, n_blocks=5)
        out = memory_handler.put(
            text="cites block 3", link="paper:wang2020state~3", rel="cites"
        )
        new_id = int(out.body.split("=")[-1].strip().split()[0])
        links = store.links_for(new_id, direction="out")
        assert links[0].dst_ref_id == target
        assert links[0].dst_pos == 3

    def test_link_on_update(self, memory_handler: MemoryHandler, store: Store) -> None:
        _seed_paper(store)
        m = memory_handler.put(text="just a memory")
        new_id = int(m.body.split("=")[-1].strip().split()[0])

        memory_handler.put(id=new_id, link="paper:wang2020state", rel="cites")

        links = store.links_for(new_id, direction="out")
        assert len(links) == 1
        assert links[0].relation == "cites"

    def test_unlink_specific_relation(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        _seed_paper(store)
        m = memory_handler.put(text="m", link="paper:wang2020state", rel="cites")
        new_id = int(m.body.split("=")[-1].strip().split()[0])
        memory_handler.put(id=new_id, link="paper:wang2020state", rel="contradicts")
        assert len(store.links_for(new_id, direction="out")) == 2

        memory_handler.put(id=new_id, unlink="paper:wang2020state", rel="cites")
        remaining = store.links_for(new_id, direction="out")
        assert len(remaining) == 1
        assert remaining[0].relation == "contradicts"

    def test_unlink_without_rel_removes_all(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        _seed_paper(store)
        m = memory_handler.put(text="m", link="paper:wang2020state", rel="cites")
        new_id = int(m.body.split("=")[-1].strip().split()[0])
        memory_handler.put(id=new_id, link="paper:wang2020state", rel="contradicts")

        memory_handler.put(id=new_id, unlink="paper:wang2020state")
        assert store.links_for(new_id, direction="out") == []

    def test_link_and_unlink_mutually_exclusive(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        _seed_paper(store)
        m = memory_handler.put(text="m")
        new_id = int(m.body.split("=")[-1].strip().split()[0])
        with pytest.raises(BadInput, match="mutually exclusive"):
            memory_handler.put(
                id=new_id,
                link="paper:wang2020state",
                unlink="paper:wang2020state",
            )

    def test_rel_without_link_or_unlink_rejected(
        self, memory_handler: MemoryHandler
    ) -> None:
        m = memory_handler.put(text="m")
        new_id = int(m.body.split("=")[-1].strip().split()[0])
        with pytest.raises(BadInput, match="rel= requires link= or unlink="):
            memory_handler.put(id=new_id, rel="cites")

    def test_unlink_on_create_rejected(self, memory_handler: MemoryHandler) -> None:
        with pytest.raises(BadInput, match="unlink= is not supported on create"):
            memory_handler.put(text="m", unlink="paper:wang2020state")

    def test_unknown_relation_rejected(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        _seed_paper(store)
        with pytest.raises(BadInput, match="unknown relation"):
            memory_handler.put(text="m", link="paper:wang2020state", rel="references")

    def test_bad_link_target_rejected_before_create(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        """A bad link= must reject the whole put, not leave a half-
        created memory in the corpus."""
        before = store.count_refs(kind="memory")
        with pytest.raises(NotFound):
            memory_handler.put(text="m", link="paper:does-not-exist")
        after = store.count_refs(kind="memory")
        assert after == before, (
            "memory was created despite bad link target — should have "
            "rejected before insert"
        )


# ── handler: view='links' ──────────────────────────────────────────


class TestMemoryHandlerLinksView:
    def test_no_links(self, memory_handler: MemoryHandler, store: Store) -> None:
        m = memory_handler.put(text="alone")
        new_id = int(m.body.split("=")[-1].strip().split()[0])
        out = memory_handler.get(id=new_id, view="links")
        assert "(no links)" in out.body
        # Hint pointing at how to add one.
        assert "link='kind:identifier'" in out.body

    def test_outbound_rendering(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        _seed_paper(store, slug="wang2020state")
        m = memory_handler.put(text="m", link="paper:wang2020state", rel="cites")
        new_id = int(m.body.split("=")[-1].strip().split()[0])
        out = memory_handler.get(id=new_id, view="links")
        assert "outbound" in out.body
        assert "→ paper:wang2020state" in out.body
        assert "(cites)" in out.body

    def test_inbound_rendering(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        # mem_a links to mem_b; viewing mem_b's links shows mem_a inbound.
        a_resp = memory_handler.put(text="alpha")
        b_resp = memory_handler.put(text="beta")
        a_id = int(a_resp.body.split("=")[-1].strip().split()[0])
        b_id = int(b_resp.body.split("=")[-1].strip().split()[0])
        memory_handler.put(id=a_id, link=f"memory:{b_id}", rel="cites")

        out = memory_handler.get(id=b_id, view="links")
        assert "inbound" in out.body
        assert f"← memory:{a_id}" in out.body
        assert "(cites)" in out.body

    def test_block_pos_in_rendering(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        _seed_paper_with_blocks(store, n_blocks=4)
        m = memory_handler.put(
            text="cites a block",
            link="paper:wang2020state~2",
            rel="cites",
        )
        new_id = int(m.body.split("=")[-1].strip().split()[0])
        out = memory_handler.get(id=new_id, view="links")
        assert "→ paper:wang2020state~2" in out.body

    def test_unknown_view_rejected(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        m = memory_handler.put(text="m")
        new_id = int(m.body.split("=")[-1].strip().split()[0])
        with pytest.raises(Unsupported, match="unknown view 'banana'"):
            memory_handler.get(id=new_id, view="banana")

    def test_deleted_target_shown_with_marker(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        # Link to a memory, then soft-delete that memory.
        a = memory_handler.put(text="alpha")
        b = memory_handler.put(text="beta")
        a_id = int(a.body.split("=")[-1].strip().split()[0])
        b_id = int(b.body.split("=")[-1].strip().split()[0])
        memory_handler.put(id=a_id, link=f"memory:{b_id}")
        store.soft_delete_ref(b_id)

        out = memory_handler.get(id=a_id, view="links")
        assert "(deleted)" in out.body
