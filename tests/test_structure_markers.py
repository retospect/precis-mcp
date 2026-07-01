"""Cursors + measures + lineage links for the ``structure`` kind (ADR 0043 §6.8/§7).

Increment 1 of the viewer bundle: the write path (cursor/measure/unmark/
remove_measure ops), the versioned persistence (markers survive an edit and
re-evaluate), the ``view='markers'`` read, dangling-anchor handling, and the
``derived-from`` lineage link between two designs.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.structure import StructureHandler
from precis.structure import apply_ops, evaluate_measure
from precis.structure.cell import Cell
from precis.structure.ops import OpError
from precis.structure.scene import Scene

_PD = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
        ],
    }
)


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


def _pd_scene() -> Scene:
    scene = Scene(cell=Cell(np.eye(3) * 10.0, (True, True, False)))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
        ],
    )
    return scene


# ── pure-unit: ops + evaluation ──────────────────────────────────────────


def test_cursor_and_measure_ops_populate_and_evaluate():
    scene = _pd_scene()
    apply_ops(
        scene,
        [
            {
                "op": "cursor",
                "name": "active_site",
                "atoms": ["aPd1"],
                "reach": 3.0,
                "for": "the reactive Pd",
            },
            {
                "op": "measure",
                "kind": "distance",
                "atoms": ["aPd1", "aPd2"],
                "direction": "target",
                "goal": {"target": 2.5, "tol": 0.05},
                "strength": "soft",
                "for": "keep the dimer tight",
            },
        ],
    )
    assert len(scene.measures) == 2
    cursor = next(m for m in scene.measures if m.kind == "cursor")
    dist = next(m for m in scene.measures if m.kind == "distance")

    cval, cverdict = evaluate_measure(scene, cursor)
    assert cverdict is None
    assert [t["label"] for t in cval["touch"]] == [
        "aPd2"
    ]  # aPd2 at 2.6 Å is within reach

    dval, dverdict = evaluate_measure(scene, dist)
    assert dval["value"] == pytest.approx(2.6, abs=1e-3)
    assert dval["unit"] == "Å"
    # 2.6 is 0.1 off the 2.5 target (tol 0.05) → a fail, softened to a warning
    assert dverdict == "warn"


def test_measure_op_arity_and_kind_validation():
    scene = _pd_scene()
    with pytest.raises(OpError):
        apply_ops(scene, [{"op": "measure", "kind": "distance", "atoms": ["aPd1"]}])
    with pytest.raises(OpError):
        apply_ops(
            scene, [{"op": "measure", "kind": "bogus", "atoms": ["aPd1", "aPd2"]}]
        )
    with pytest.raises(OpError):
        apply_ops(scene, [{"op": "cursor", "atoms": ["aPd1"]}])  # no name


def test_cursor_replace_and_unmark():
    scene = _pd_scene()
    apply_ops(scene, [{"op": "cursor", "name": "site", "atoms": ["aPd1"]}])
    apply_ops(scene, [{"op": "cursor", "name": "site", "atoms": ["aPd2"]}])  # replace
    assert len(scene.measures) == 1 and scene.measures[0].operands == ["aPd2"]
    apply_ops(scene, [{"op": "unmark", "name": "site"}])
    assert scene.measures == []
    with pytest.raises(OpError):
        apply_ops(scene, [{"op": "unmark", "name": "nope"}])


def test_dangling_measure_after_vacancy():
    scene = _pd_scene()
    apply_ops(scene, [{"op": "measure", "kind": "distance", "atoms": ["aPd1", "aPd2"]}])
    apply_ops(scene, [{"op": "vacancy", "atom": "aPd2"}])
    value, verdict = evaluate_measure(scene, scene.measures[0])
    assert verdict == "dangling"
    assert "missing atoms" in value["error"]


# ── DB round-trip via the handler ────────────────────────────────────────


def test_markers_persist_and_reevaluate_across_edit(structure):
    structure.put(id="pd_marks", text=_PD)
    structure.edit(
        id="pd_marks",
        ops=[
            {
                "op": "cursor",
                "name": "active_site",
                "atoms": ["aPd1"],
                "reach": 3.0,
                "for": "reactive Pd",
            },
            {
                "op": "measure",
                "kind": "distance",
                "atoms": ["aPd1", "aPd2"],
                "direction": "target",
                "goal": {"target": 2.6, "tol": 0.05},
            },
        ],
    )
    # the markers view renders both, live-evaluated
    view = structure.get(id="pd_marks", view="markers")
    assert "active_site" in view.body and "distance" in view.body
    assert "ok" in view.body  # the distance sits on its 2.6 target

    # markers survive a geometry edit (add an atom) and re-hydrate
    structure.edit(
        id="pd_marks", ops=[{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}]
    )
    ref = resolve_live_slug_ref(structure.store, kind="structure", id="pd_marks")
    scene, _ = structure.store.structure_load(ref.id)
    kinds = sorted(m.kind for m in scene.measures)
    assert kinds == ["cursor", "distance"]
    cursor = next(m for m in scene.measures if m.kind == "cursor")
    assert (
        cursor.name == "active_site"
        and cursor.reach == 3.0
        and cursor.for_ == "reactive Pd"
    )


def test_remove_measure_op_persists(structure):
    structure.put(id="pd_rm", text=_PD)
    structure.edit(
        id="pd_rm",
        ops=[{"op": "measure", "kind": "distance", "atoms": ["aPd1", "aPd2"]}],
    )
    structure.edit(
        id="pd_rm",
        ops=[{"op": "remove_measure", "kind": "distance", "atoms": ["aPd1", "aPd2"]}],
    )
    ref = resolve_live_slug_ref(structure.store, kind="structure", id="pd_rm")
    scene, _ = structure.store.structure_load(ref.id)
    assert scene.measures == []


# ── lineage link ─────────────────────────────────────────────────────────


def test_derived_from_link_both_directions(structure):
    structure.put(id="pd_parent", text=_PD)
    structure.put(id="pd_child", text=_PD)
    ack = structure.link(
        id="pd_child", target="structure:pd_parent", rel="derived-from"
    )
    assert "link" in ack.body.lower()

    parent = resolve_live_slug_ref(structure.store, kind="structure", id="pd_parent")
    child = resolve_live_slug_ref(structure.store, kind="structure", id="pd_child")

    # child → parent (outgoing derived-from)
    out = structure.store.links_for(child.id, direction="out", relation="derived-from")
    assert [lnk.dst_ref_id for lnk in out] == [parent.id]
    # parent ← child (incoming derived-from = the "derived designs" view)
    inc = structure.store.links_for(parent.id, direction="in", relation="derived-from")
    assert [lnk.src_ref_id for lnk in inc] == [child.id]


def test_link_requires_target(structure):
    structure.put(id="pd_solo", text=_PD)
    with pytest.raises(BadInput):
        structure.link(id="pd_solo", rel="derived-from")


# ── derive (the Apply core) ──────────────────────────────────────────────


def test_derive_branches_new_slug_with_lineage(structure):
    structure.put(id="pd_base", text=_PD)
    # give the parent a marker so we can prove it carries over
    structure.edit(
        id="pd_base", ops=[{"op": "cursor", "name": "site", "atoms": ["aPd1"]}]
    )

    resp = structure.derive(
        id="pd_base",
        to="pd_base_o",
        ops=[{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}],
    )
    assert "derived" in resp.body and "aO1" in resp.body

    parent = resolve_live_slug_ref(structure.store, kind="structure", id="pd_base")
    child = resolve_live_slug_ref(structure.store, kind="structure", id="pd_base_o")

    # the derived design has the new atom + the carried-over cursor
    scene, _ = structure.store.structure_load(child.id)
    assert "aO1" in scene.atoms
    assert any(m.kind == "cursor" and m.name == "site" for m in scene.measures)

    # lineage recorded child → parent
    out = structure.store.links_for(child.id, direction="out", relation="derived-from")
    assert [lnk.dst_ref_id for lnk in out] == [parent.id]

    # the parent is untouched (still 2 atoms, no O)
    pscene, _ = structure.store.structure_load(parent.id)
    assert "aO1" not in pscene.atoms and len(pscene.atoms) == 2


def test_derive_rejects_existing_slug_and_relax(structure):
    structure.put(id="pd_src", text=_PD)
    structure.put(id="pd_taken", text=_PD)
    with pytest.raises(BadInput):
        structure.derive(id="pd_src", to="pd_taken", ops=[])  # slug exists
    with pytest.raises(BadInput):
        structure.derive(
            id="pd_src", to="pd_new", ops=[{"op": "relax", "fidelity": "clean"}]
        )
