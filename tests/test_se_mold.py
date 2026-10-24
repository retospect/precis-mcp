"""The ``mold`` op (spec ``docs/backlog/se-cylinder-wrap-mold.md`` slice
3): cut the mold halves from a molded part — one cad mold block per pull,
``Inter(sector_i, Diff(box, scale(part, 1+shrink)))``, refusing a layout
slice 2 finds undercut for.

Every design slug is unique per test (the shared test DB has no per-test
isolation and ``-n auto`` runs siblings concurrently)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from precis.cad.fold import expr_sdf
from precis.cad.graph import Design
from precis.cad.hmap import hmap_array
from precis.cad.mold import resolve_mold_layout
from precis.cad.primitives import CylRelief
from precis.cad.scene import NodeSpec, SceneSpec, build_design
from precis.cad.vec import vec3
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.mold import _refuse_undercut
from precis_se.ops import OpError, SeTree


@pytest.fixture
def handler(hub: Hub) -> SeHandler:
    return SeHandler(hub=hub)


def _load(handler: SeHandler, slug: str) -> SeTree:
    ref = handler.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(handler.store, int(ref.id))


def _cyl_part(store: Store, slug: str, *, config: str = "cyl:r0.003h0.012") -> None:
    """Store a one-body cylinder cad design the se block binds as its
    part."""
    spec = SceneSpec(
        nodes=[NodeSpec("body", "add", config, "part")], components=["part"]
    )
    store.cad_save(slug=slug, title=f"{slug} part", spec=spec, card_text=slug)


def _bind(block: str, cad_slug: str) -> list[dict]:
    return [
        {"op": "add_block", "name": block},
        {"op": "set_binding", "block": block, "kind": "cad", "design": cad_slug},
    ]


def _sdf(design: Design, p: tuple[float, float, float]) -> float:
    return min(
        expr_sdf(e, vec3(*p), design.instances) for e in design.components.values()
    )


class TestHappyPath:
    def test_halves_cuts_two_bound_sla_pieces(
        self, handler: SeHandler, store: Store
    ) -> None:
        _cyl_part(store, "mold-hp-part")
        handler.put(id="mold-hp", text=_put(_bind("pot", "mold-hp-part")))
        resp = handler.edit(
            id="mold-hp", ops=[{"op": "mold", "block": "pot", "layout": "halves"}]
        )
        assert "pot.mold.0" in resp.body and "pot.mold.1" in resp.body

        tree = _load(handler, "mold-hp")
        assert set(tree.blocks) >= {"pot", "pot.mold.0", "pot.mold.1"}
        # the molded part is stamped molding; each piece is an sla block
        # bound to its own cad cavity design.
        assert tree.blocks["pot"].mode == "molding"
        for j in (0, 1):
            piece = tree.blocks[f"pot.mold.{j}"]
            assert piece.bound_kind == "cad"
            assert piece.mode == "sla"
            assert piece.parent == "pot"
            assert piece.bound is not None
            assert store.get_ref(kind="cad", id=piece.bound) is not None

    def test_piece_geometry_is_box_minus_part_in_its_sector(
        self, handler: SeHandler, store: Store
    ) -> None:
        _cyl_part(store, "mold-geo-part")
        handler.put(id="mold-geo", text=_put(_bind("pot", "mold-geo-part")))
        handler.edit(
            id="mold-geo",
            ops=[{"op": "mold", "block": "pot", "layout": "halves", "margin": 0.005}],
        )
        tree = _load(handler, "mold-geo")
        d0 = _piece_design(store, tree, "pot.mold.0")
        d1 = _piece_design(store, tree, "pot.mold.1")
        # the part core is hollow in every piece (it is the cavity)
        assert _sdf(d0, (0.0, 0.0, 0.006)) > 0.0
        # piece 0 keeps +x, piece 1 keeps -x (parting plane through the axis)
        assert _sdf(d0, (0.006, 0.0, 0.006)) < 0.0
        assert _sdf(d0, (-0.006, 0.0, 0.006)) > 0.0
        assert _sdf(d1, (-0.006, 0.0, 0.006)) < 0.0

    def test_parting_surface_recorded_on_each_piece(
        self, handler: SeHandler, store: Store
    ) -> None:
        _cyl_part(store, "mold-part-part")
        handler.put(id="mold-part", text=_put(_bind("pot", "mold-part-part")))
        handler.edit(
            id="mold-part", ops=[{"op": "mold", "block": "pot", "layout": "halves"}]
        )
        tree = _load(handler, "mold-part")
        bound = tree.blocks["pot.mold.0"].bound
        assert bound is not None
        ref = store.get_ref(kind="cad", id=bound)
        assert ref is not None
        spec, _ = store.cad_load(ref.id)
        rec = spec.meta.get("mold")
        assert rec is not None
        assert rec["layout"] == "radial" and rec["k"] == 2 and rec["piece"] == 0
        assert len(rec["pull"]) == 3 and len(rec["parting_angles"]) == 2

    def test_shrink_enlarges_the_cavity(self, handler: SeHandler, store: Store) -> None:
        _cyl_part(store, "mold-shr-part")
        handler.put(id="mold-shr", text=_put(_bind("pot", "mold-shr-part")))
        handler.edit(
            id="mold-shr",
            ops=[
                {
                    "op": "mold",
                    "block": "pot",
                    "layout": "axial",
                    "shrink": 0.1,
                    "margin": 0.005,
                }
            ],
        )
        tree = _load(handler, "mold-shr")
        d = _piece_design(store, tree, "pot.mold.0")
        # the nominal part radius is 3 mm; at shrink 0.1 the cavity reaches
        # 3.3 mm, so a point at 3.15 mm (outside nominal, inside scaled) is
        # hollow.
        assert _sdf(d, (0.00315, 0.0, 0.006)) > 0.0
        # …while well outside the scaled cavity is solid wall.
        assert _sdf(d, (0.006, 0.0, 0.006)) < 0.0

    def test_axial_makes_one_sleeve(self, handler: SeHandler, store: Store) -> None:
        _cyl_part(store, "mold-ax-part")
        handler.put(id="mold-ax", text=_put(_bind("pot", "mold-ax-part")))
        handler.edit(
            id="mold-ax", ops=[{"op": "mold", "block": "pot", "layout": "axial"}]
        )
        tree = _load(handler, "mold-ax")
        assert "pot.mold.0" in tree.blocks
        assert "pot.mold.1" not in tree.blocks


class TestRefusals:
    def test_unbound_part_is_refused(self, handler: SeHandler) -> None:
        handler.put(id="mold-nb", text='{"ops": [{"op": "add_block", "name": "pot"}]}')
        with pytest.raises(BadInput, match="not bound to a cad design"):
            handler.edit(id="mold-nb", ops=[{"op": "mold", "block": "pot"}])

    def test_part_with_internal_cut_is_refused(
        self, handler: SeHandler, store: Store
    ) -> None:
        spec = SceneSpec(
            nodes=[
                NodeSpec("body", "add", "box:w0.02d0.02h0.02", "part"),
                NodeSpec("hole", "cut", "cyl:r0.004h0.03", "part"),
            ],
            components=["part"],
        )
        store.cad_save(slug="mold-cut-part", title="t", spec=spec, card_text="c")
        handler.put(id="mold-cut", text=_put(_bind("blk", "mold-cut-part")))
        with pytest.raises(BadInput, match="union of positive bodies"):
            handler.edit(id="mold-cut", ops=[{"op": "mold", "block": "blk"}])

    def test_undercut_layout_is_refused(self) -> None:
        """A full-height plateau wall hard against the k=2 join undercuts —
        the op refuses it rather than cut a mold that will not open."""
        m = np.zeros((256, 48))
        m[:, 10:38] = 1.0
        leaf = CylRelief(
            hmap=hmap_array(m),
            r=0.05,
            h=0.10,
            depth=0.002,
            k=2,
            alpha=math.radians(2.0),
            phi=0.0,
        )
        des = Design()
        des.prim("o1", leaf)
        with pytest.raises(OpError, match="undercuts"):
            _refuse_undercut(
                des, resolve_mold_layout("halves"), math.radians(2.0), "o1"
            )


def _piece_design(store: Store, tree: SeTree, block: str) -> Design:
    bound = tree.blocks[block].bound
    assert bound is not None
    ref = store.get_ref(kind="cad", id=bound)
    assert ref is not None
    spec, _ = store.cad_load(ref.id)
    return build_design(spec)


def _put(ops: list[dict]) -> str:
    import json

    return json.dumps({"description": "part", "ops": ops})
