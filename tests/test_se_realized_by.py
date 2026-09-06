"""se's `component` bindings project onto ``realized-by`` links
(reconciliation decided 2026-09-06 — ``docs/backlog/`` and
``persist.sync_realized_by``'s docstring).

The point of the reconciliation is a single question having a single
answer: *what does this artifact resolve to, and who calls for this
component?* — across both the cad track (``part`` lines) and se
(``set_binding``). So the load-bearing test here is the **inverse**
lookup from the component, not the forward one.

``se_blocks.bound_kind``/``bound_design`` stays authoritative; the link is
a derived projection rebuilt on every save. These tests pin both halves:
that it is rebuilt, and that rebuilding never eats a row it does not own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import precis
import precis_se
from precis.dispatch import Hub
from precis.store import Store
from precis_se.handler import SeHandler

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"
_CORE_MIGRATIONS = Path(precis.__file__).parent / "migrations"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))
        # 0156 seeds the relation itself; the shared test template may
        # predate it, and an unknown relation slug refuses at add_link.
        realizes = _CORE_MIGRATIONS / "0156_realizes_relation.sql"
        if realizes.exists():
            body = realizes.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))
    return SeHandler(hub=hub)


def _component(store: Store, slug: str) -> int:
    ref, _ = store.component_entity_upsert(
        slug=slug, title=f"{slug} (test)", meta_patch={}
    )
    return int(ref.id)


def _design(*bindings: tuple[str, str, str]) -> str:
    """``(block, kind, slug)`` triples → one design's ops."""
    ops: list[dict[str, Any]] = []
    for block, kind, slug in bindings:
        ops.append({"op": "add_block", "name": block})
        ops.append({"op": "set_binding", "block": block, "kind": kind, "design": slug})
    return json.dumps({"description": "bindings", "ops": ops})


def _realized(store: Store, ref_id: int) -> set[int]:
    return {
        int(lk.dst_ref_id)
        for lk in store.links_for(ref_id, direction="out", relation="realized-by")
    }


class TestProjection:
    def test_a_component_binding_becomes_a_realized_by_link(
        self, handler: SeHandler, store: Store
    ) -> None:
        comp = _component(store, "iso-4762-m6x30")
        handler.put(id="rb-one", text=_design(("bolt", "component", "iso-4762-m6x30")))
        ref = store.get_ref(kind="se", id="rb-one")
        assert ref is not None
        assert _realized(store, ref.id) == {comp}

    def test_the_component_can_be_asked_who_calls_for_it(
        self, handler: SeHandler, store: Store
    ) -> None:
        """The whole reason for the projection: one query from the
        component side reaches every design that wants it, whichever
        track authored the binding."""
        comp = _component(store, "iso-4032-m6")
        handler.put(id="rb-a", text=_design(("nut", "component", "iso-4032-m6")))
        handler.put(id="rb-b", text=_design(("nut", "component", "iso-4032-m6")))
        callers = {
            lk.src_ref_id
            for lk in store.links_for(comp, direction="in", relation="realized-by")
        }
        a = store.get_ref(kind="se", id="rb-a")
        b = store.get_ref(kind="se", id="rb-b")
        assert a is not None and b is not None
        assert {a.id, b.id} <= callers

    def test_two_blocks_binding_one_component_make_one_link(
        self, handler: SeHandler, store: Store
    ) -> None:
        """A frame with twenty identical bolts resolves to one component,
        not twenty edges — the link says *what*, quantities are the BOM's
        job."""
        comp = _component(store, "iso-7089-m6")
        handler.put(
            id="rb-dup",
            text=_design(
                ("washer_a", "component", "iso-7089-m6"),
                ("washer_b", "component", "iso-7089-m6"),
            ),
        )
        ref = store.get_ref(kind="se", id="rb-dup")
        assert ref is not None
        assert _realized(store, ref.id) == {comp}


class TestRebuild:
    def test_removing_the_binding_prunes_the_link(
        self, handler: SeHandler, store: Store
    ) -> None:
        _component(store, "iso-4762-m6x30")
        handler.put(id="rb-drop", text=_design(("bolt", "component", "iso-4762-m6x30")))
        ref = store.get_ref(kind="se", id="rb-drop")
        assert ref is not None
        assert _realized(store, ref.id)
        handler.edit(id="rb-drop", ops=[{"op": "remove_block", "block": "bolt"}])
        assert _realized(store, ref.id) == set()

    def test_an_edit_that_adds_a_binding_adds_the_link(
        self, handler: SeHandler, store: Store
    ) -> None:
        comp = _component(store, "iso-4032-m8")
        handler.put(
            id="rb-add", text=json.dumps({"ops": [{"op": "add_block", "name": "nut"}]})
        )
        ref = store.get_ref(kind="se", id="rb-add")
        assert ref is not None
        assert _realized(store, ref.id) == set()
        handler.edit(
            id="rb-add",
            ops=[
                {
                    "op": "set_binding",
                    "block": "nut",
                    "kind": "component",
                    "design": "iso-4032-m8",
                }
            ],
        )
        assert _realized(store, ref.id) == {comp}


class TestPrunesOnlyItsOwn:
    def test_a_hand_authored_link_survives_a_rebuild(
        self, handler: SeHandler, store: Store
    ) -> None:
        """The courtesy cad's sync extends and se must too: a candidate
        realization someone recorded by hand is not this sync's row to
        delete. Many candidates per design are legal by design (0156)."""
        alt = _component(store, "alt-sourcing")
        _component(store, "iso-4762-m6x30")
        handler.put(id="rb-keep", text=_design(("bolt", "component", "iso-4762-m6x30")))
        ref = store.get_ref(kind="se", id="rb-keep")
        assert ref is not None
        store.add_link(
            src_ref_id=ref.id,
            dst_ref_id=alt,
            relation="realized-by",
            meta={"note": "second source, hand-authored"},
        )
        handler.edit(id="rb-keep", ops=[{"op": "remove_block", "block": "bolt"}])
        # the managed row is gone, the hand-authored one is not
        assert _realized(store, ref.id) == {alt}


class TestScopeAndTotality:
    def test_a_part_binding_is_not_linked(
        self, handler: SeHandler, store: Store
    ) -> None:
        """`realized-by` targets a procurable `component`; an LCSC
        C-number is a `part`. Out of the relation's scope on purpose —
        recorded, not papered over."""
        handler.put(id="rb-part", text=_design(("chip", "part", "C12345")))
        ref = store.get_ref(kind="se", id="rb-part")
        assert ref is not None
        assert _realized(store, ref.id) == set()

    def test_a_dangling_component_slug_saves_without_a_link(
        self, handler: SeHandler, store: Store
    ) -> None:
        """A binding may legitimately precede the component row. The
        design saves; the projection is simply incomplete until it
        exists."""
        resp = handler.put(id="rb-ghost", text=_design(("bolt", "component", "nope")))
        assert "created" in resp.body
        ref = store.get_ref(kind="se", id="rb-ghost")
        assert ref is not None
        assert _realized(store, ref.id) == set()

    def test_a_later_save_picks_up_a_component_that_has_since_appeared(
        self, handler: SeHandler, store: Store
    ) -> None:
        handler.put(id="rb-late", text=_design(("bolt", "component", "late-part")))
        ref = store.get_ref(kind="se", id="rb-late")
        assert ref is not None
        assert _realized(store, ref.id) == set()
        comp = _component(store, "late-part")
        handler.edit(id="rb-late", ops=[{"op": "add_block", "name": "spacer"}])
        assert _realized(store, ref.id) == {comp}

    def test_a_store_without_the_link_surface_is_a_no_op(self) -> None:
        """Plugin tests run against fakes; the sync must skip rather than
        demand a surface, exactly as ``attach_catalog`` does."""
        from precis_se import persist
        from precis_se.ops import SeBlock, SeTree

        tree = SeTree()
        tree.blocks["bolt"] = SeBlock(
            name="bolt", bound_kind="component", bound="whatever"
        )
        persist.sync_realized_by(object(), 1, tree)  # must not raise
