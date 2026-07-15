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

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._link_target import LinkTarget, parse_link_target
from precis.handlers.memory import MemoryHandler
from precis.store import BlockInsert, Store
from precis.store.types import Relation
from precis.utils import handle_registry
from tests.conftest import id_of

# ── unit: parse_link_target ─────────────────────────────────────────


@pytest.fixture
def memory_handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _seed_paper(store: Store, slug: str = "wang2020state") -> int:
    ref = store.insert_ref(
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
    ref = store.insert_ref(kind="memory", slug=None, title=title)
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

    def test_unknown_kind_options_excludes_handler_less_kinds(
        self, store: Store
    ) -> None:
        """The MCP critic 2026-05-02 flagged that the ``options:``
        list on the unknown-kind error path enumerated every row in
        the ``kinds`` schema table, including file kinds (markdown,
        plaintext, tex, docx, book, rmk) whose env-gated handler
        wasn't registered. A 7B caller picking ``markdown:foo`` from
        the options got a contradictory ``unknown kind: markdown``
        error from ``get(kind='markdown')``. The fix lists only
        kinds that have at least one live ref — the realistic link
        targets in this build — with the schema-table fallback only
        when no refs exist anywhere.
        """
        # Seed a paper so it shows up; nothing in the file kinds
        # schema rows.
        _seed_paper(store)
        with pytest.raises(BadInput) as exc:
            parse_link_target("does-not-exist:42", store=store)
        opts = exc.value.options or []
        # ``paper`` should appear (we seeded a ref).
        assert "paper" in opts
        # ``markdown`` is in the kinds table but has no refs in this
        # build — must NOT appear.
        assert "markdown" not in opts, (
            "options must filter to kinds with at least one live ref"
        )

    def test_numeric_kind_rejects_slug_id(self, store: Store) -> None:
        with pytest.raises(BadInput, match="numeric - identifier must be an integer"):
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

    def test_link_exposes_chunk_id_endpoints(self, store: Store) -> None:
        """source-backfill 8a: ``links_for`` carries the raw chunk-id
        endpoints alongside the ord-based ``*_pos`` so a caller can map an
        edge into the reading-order tree (walk up to a visible ancestor),
        not just its ordinal. A ref-level edge leaves both ``None``."""

        def _chunk_id_at(ref_id: int, ord_: int) -> int:
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = %s",
                    (ref_id, ord_),
                ).fetchone()
            assert row is not None
            return int(row[0])

        paper = _seed_paper_with_blocks(store, n_blocks=10)
        store.add_link(
            src_ref_id=paper,
            src_pos=5,
            dst_ref_id=paper,
            dst_pos=7,
            relation="see-also",
        )
        (link,) = store.links_for(paper, direction="out")
        # the chunk-id endpoints resolve to the chunks at those ords …
        assert link.src_chunk_id == _chunk_id_at(paper, 5)
        assert link.dst_chunk_id == _chunk_id_at(paper, 7)
        # … and travel together with the ord projection, not instead of it.
        assert link.src_pos == 5 and link.dst_pos == 7

        # a ref-level edge (no pos on either side) has NULL chunk ids.
        m = _seed_memory(store)
        store.add_link(src_ref_id=m, dst_ref_id=paper, relation="related-to")
        (ref_level,) = store.links_for(m, direction="out")
        assert ref_level.src_chunk_id is None
        assert ref_level.dst_chunk_id is None


# ── merge_refs: the duplicate-paper resolver primitive ─────────────


class TestStoreMergeRefs:
    def test_merge_migrates_links_to_survivor(self, store: Store) -> None:
        """Every link touching the victim re-points onto the survivor."""
        survivor = _seed_paper(store, slug="keepme2020")
        victim = _seed_paper(store, slug="dropme2020")
        other = _seed_memory(store)
        # An inbound edge to the victim that must survive the merge.
        store.add_link(src_ref_id=other, dst_ref_id=victim, relation="related-to")

        migrated = store.merge_refs(victim, survivor)
        assert migrated == 1
        # The edge now points at the survivor; the victim has none left.
        in_survivor = store.links_for(survivor, direction="in")
        assert len(in_survivor) == 1 and in_survivor[0].src_ref_id == other
        assert store.links_for(victim, direction="both") == []

    def test_merge_soft_deletes_the_victim(self, store: Store) -> None:
        survivor = _seed_paper(store, slug="keepme2021")
        victim = _seed_paper(store, slug="dropme2021")
        store.merge_refs(victim, survivor)
        live = store.fetch_refs_by_ids([survivor, victim], include_deleted=False)
        assert survivor in live
        assert victim not in live  # retired

    def test_merge_frees_the_victims_identifier(self, store: Store) -> None:
        """The victim's DOI must free up so it can be assigned to the
        survivor — a bare soft-delete would leave it claimed (the
        ``ref_identifiers`` uniqueness check ignores ``deleted_at``)."""
        survivor = _seed_paper(store, slug="keepme2022")
        victim = _seed_paper(store, slug="dropme2022")
        store.set_ref_identifier(victim, "doi", "10.1234/dup.2022")
        # Before the merge the DOI belongs to the victim.
        with pytest.raises(BadInput, match="already belongs to ref"):
            store.set_ref_identifier(survivor, "doi", "10.1234/dup.2022")

        store.merge_refs(victim, survivor)
        # After the merge it can be assigned to the survivor.
        store.set_ref_identifier(survivor, "doi", "10.1234/dup.2022")
        assert store.find_paper_ref_by_identifier("10.1234/dup.2022") == survivor

    def test_bare_delete_owner_does_not_wedge_identifier(self, store: Store) -> None:
        """A DOI orphaned by a *bare* soft-delete (the 🗑 Delete button,
        not ``merge_refs``) must not permanently block a live paper from
        claiming it. The deleted owner can't be loaded to merge against,
        so ``set_ref_identifier`` reclaims its orphaned row instead of
        raising the unresolvable duplicate-identifier conflict."""
        deleted = _seed_paper(store, slug="ghost1994")
        survivor = _seed_paper(store, slug="keepme1994")
        store.set_ref_identifier(deleted, "doi", "10.1126/science.7973651")
        # Bare soft-delete leaves the DOI row behind (cf. merge_refs).
        store.soft_delete_ref(deleted)

        # Reassigning to the live paper now succeeds (no dead-end conflict).
        assert store.set_ref_identifier(survivor, "doi", "10.1126/science.7973651")
        assert store.find_paper_ref_by_identifier("10.1126/science.7973651") == survivor

    def test_live_owner_still_blocks_identifier(self, store: Store) -> None:
        """A *live* owner of the value is still a real conflict — the
        soft-deleted-owner carve-out must not weaken the cross-ref
        uniqueness guard for two coexisting papers."""
        owner = _seed_paper(store, slug="alive2024a")
        other = _seed_paper(store, slug="alive2024b")
        store.set_ref_identifier(owner, "doi", "10.1234/live.2024")
        with pytest.raises(BadInput, match="already belongs to ref"):
            store.set_ref_identifier(other, "doi", "10.1234/live.2024")

    def test_merge_into_self_rejected(self, store: Store) -> None:
        a = _seed_paper(store, slug="self2023")
        with pytest.raises(BadInput, match="cannot merge a ref into itself"):
            store.merge_refs(a, a)


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


class TestPluginRelationVocabulary:
    """A *plugin* kind seeds its own relation via its migration; the
    handler-layer ``validate_relation`` must accept it — read from the
    live ``relations`` table — without it living in the ``Relation``
    literal (which stays the built-in typo-safety hint)."""

    def test_valid_relations_matches_table(self, store: Store) -> None:
        with store.pool.connection() as conn:
            rows = conn.execute("SELECT slug FROM relations").fetchall()
        assert store.valid_relations() == frozenset(r[0] for r in rows)

    def test_plugin_relation_accepted_after_seed(self, store: Store) -> None:
        from precis.handlers._link_tag_ops import validate_relation

        slug = "test-plugin-consumes"
        # Not in the literal and not yet seeded → rejected (and the
        # miss re-reads, so the cache reflects "absent" too).
        with pytest.raises(BadInput, match="unknown relation"):
            validate_relation(slug, store=store)
        try:
            with store.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO relations (slug, is_symmetric, description) "
                    "VALUES (%s, FALSE, %s) ON CONFLICT (slug) DO NOTHING",
                    (slug, "test-only plugin relation"),
                )
            # Seeded after the vocab was first cached → the refresh-on-
            # miss path picks it up rather than falsely rejecting.
            assert validate_relation(slug, store=store) == slug
        finally:
            with store.pool.connection() as conn:
                conn.execute("DELETE FROM relations WHERE slug = %s", (slug,))
            store.valid_relations(refresh=True)

    def test_store_free_validation_only_sees_builtins(self) -> None:
        from precis.handlers._link_tag_ops import validate_relation

        # No store handle → only the built-in literal is consulted.
        assert validate_relation("cites") == "cites"
        with pytest.raises(BadInput, match="unknown relation"):
            validate_relation("test-plugin-consumes")


# ── handler: link/unlink/rel on put ────────────────────────────────


class TestMemoryHandlerLink:
    def test_link_on_create(self, memory_handler: MemoryHandler, store: Store) -> None:
        target = _seed_paper(store)
        out = memory_handler.put(text="see this paper", link="paper:wang2020state")
        # Extract memory id from "created memory id=N"
        new_id = id_of(out.body)
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
        new_id = id_of(out.body)
        links = store.links_for(new_id, direction="out")
        assert links[0].relation == "contradicts"

    def test_link_to_block(self, memory_handler: MemoryHandler, store: Store) -> None:
        target = _seed_paper_with_blocks(store, n_blocks=5)
        out = memory_handler.put(
            text="cites block 3", link="paper:wang2020state~3", rel="cites"
        )
        new_id = id_of(out.body)
        links = store.links_for(new_id, direction="out")
        assert links[0].dst_ref_id == target
        assert links[0].dst_pos == 3

    def test_link_on_update(self, memory_handler: MemoryHandler, store: Store) -> None:
        _seed_paper(store)
        m = memory_handler.put(text="just a memory")
        new_id = id_of(m.body)

        memory_handler.link(id=new_id, target="paper:wang2020state", rel="cites")

        links = store.links_for(new_id, direction="out")
        assert len(links) == 1
        assert links[0].relation == "cites"

    def test_unlink_specific_relation(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        _seed_paper(store)
        m = memory_handler.put(text="m", link="paper:wang2020state", rel="cites")
        new_id = id_of(m.body)
        memory_handler.link(id=new_id, target="paper:wang2020state", rel="contradicts")
        assert len(store.links_for(new_id, direction="out")) == 2

        memory_handler.link(
            id=new_id, target="paper:wang2020state", mode="remove", rel="cites"
        )
        remaining = store.links_for(new_id, direction="out")
        assert len(remaining) == 1
        assert remaining[0].relation == "contradicts"

    def test_unlink_without_rel_removes_all(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        _seed_paper(store)
        m = memory_handler.put(text="m", link="paper:wang2020state", rel="cites")
        new_id = id_of(m.body)
        memory_handler.link(id=new_id, target="paper:wang2020state", rel="contradicts")

        memory_handler.link(id=new_id, target="paper:wang2020state", mode="remove")
        assert store.links_for(new_id, direction="out") == []

    def test_put_on_existing_id_rejected(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        """After the seven-verb cutover, put is creation-only on
        memory. Passing ``id=`` (with anything) raises BadInput
        pointing at tag/link/delete.
        """
        _seed_paper(store)
        m = memory_handler.put(text="m")
        new_id = id_of(m.body)
        with pytest.raises(BadInput, match="put on existing memory"):
            memory_handler.put(
                id=new_id,
                link="paper:wang2020state",
            )

    def test_rel_without_link_on_create_rejected(
        self, memory_handler: MemoryHandler
    ) -> None:
        """On create, ``rel=`` only makes sense paired with ``link=``.
        A bare ``rel=`` is a misuse and rejects rather than silently
        no-opping."""
        with pytest.raises(BadInput, match="rel= requires link= on create"):
            memory_handler.put(text="m", rel="cites")

    def test_unlink_kwarg_rejected_on_put(self, memory_handler: MemoryHandler) -> None:
        """``unlink=`` is gone from the put surface entirely — the error
        points at the link verb's ``mode='remove'`` form."""
        with pytest.raises(BadInput, match="unlink= is not accepted on put"):
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
        """MCP critic MINOR-C (round 2, deep pass): the no-links
        recovery hint teaches ``link(...)`` (the canonical add-link
        verb after seven-verb cutover), not the stale
        ``put(link=, rel=)`` form which no longer works on most
        kinds."""
        m = memory_handler.put(text="alone")
        new_id = id_of(m.body)
        out = memory_handler.get(id=new_id, view="links")
        assert "(no links)" in out.body
        # Hint points at the ``link()`` verb.
        assert "link(kind='memory'" in out.body
        assert "target='kind:identifier'" in out.body
        # Stale ``put(link=)`` form must not appear.
        assert "put(kind='memory'" not in out.body
        assert "link='kind:identifier'" not in out.body

    def test_outbound_rendering(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        _seed_paper(store, slug="wang2020state")
        m = memory_handler.put(text="m", link="paper:wang2020state", rel="cites")
        new_id = id_of(m.body)
        out = memory_handler.get(id=new_id, view="links")
        assert "outbound" in out.body
        # ADR 0036: a ref-level link renders the target's record handle.
        paper_ref = store.get_ref(kind="paper", id="wang2020state")
        assert paper_ref is not None
        assert f"→ {handle_registry.format_handle('paper', paper_ref.id)}" in out.body
        assert "(cites)" in out.body

    def test_inbound_rendering(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        # mem_a links to mem_b; viewing mem_b's links shows mem_a inbound.
        a_resp = memory_handler.put(text="alpha")
        b_resp = memory_handler.put(text="beta")
        a_id = id_of(a_resp.body)
        b_id = id_of(b_resp.body)
        memory_handler.link(id=a_id, target=f"memory:{b_id}", rel="cites")

        out = memory_handler.get(id=b_id, view="links")
        assert "inbound" in out.body
        assert f"← {handle_registry.format_handle('memory', a_id)}" in out.body
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
        new_id = id_of(m.body)
        out = memory_handler.get(id=new_id, view="links")
        assert "→ paper:wang2020state~2" in out.body

    def test_unknown_view_rejected(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        m = memory_handler.put(text="m")
        new_id = id_of(m.body)
        with pytest.raises(Unsupported, match="unknown view 'banana'"):
            memory_handler.get(id=new_id, view="banana")

    def test_deleted_target_shown_with_marker(
        self, memory_handler: MemoryHandler, store: Store
    ) -> None:
        # Link to a memory, then soft-delete that memory.
        a = memory_handler.put(text="alpha")
        b = memory_handler.put(text="beta")
        a_id = id_of(a.body)
        b_id = id_of(b.body)
        memory_handler.link(id=a_id, target=f"memory:{b_id}")
        store.soft_delete_ref(b_id)

        out = memory_handler.get(id=a_id, view="links")
        assert "(deleted)" in out.body
