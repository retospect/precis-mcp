"""gr514251 — the unicycle-c1 rebuild script's repaired geometry.

``scripts/_gr514251_rebuild_unicycle_c1.py`` replays prod's se:unicycle-c1
as one ops list with three defects corrected. These tests build BOTH
variants — ``broken`` (prod verbatim) and fixed — into the test DB via
``SeHandler.put`` and assert the repairs geometrically on each block's
posed world-frame envelope:

  (a) the axle's envelope covers both crank envelopes' y-slabs
      (prod's stopped 5 mm short of each);
  (b) bearing_left/bearing_right are mirror y-spans, each inside its
      fork leg's slab;
  (c) pedal_left/pedal_right are 180° apart about the wheel axis
      (prod had both at x +0.170);
  (d) the rebuild adds no validate findings over prod's known four.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis_se import persist
from precis_se import validate as se_validate
from precis_se.handler import SeHandler
from tests._script import load_script

_SCRIPT = Path(__file__).parents[1] / "scripts" / "_gr514251_rebuild_unicycle_c1.py"
_MOD = load_script("gr514251_rebuild_unicycle_c1", _SCRIPT)

_SLUG = "unicycle-c1-test"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _build(handler: SeHandler, store: Store, *, broken: bool) -> Any:
    """put() the script's ops into the test DB; return the loaded tree.

    put's reply digest runs the full validate pass (an ~18 s envelope-
    overlap SDF sweep on this design, 30 s wall-clock budget) that no test
    reads — those that care call ``view="validate"`` themselves. Stubbing
    it for the put halves every test's runtime and keeps the budget from
    expiring under -n load.
    """
    with mock.patch.object(se_validate, "validate", return_value=[]):
        handler.put(id=_SLUG, text=json.dumps(_MOD.payload(broken=broken)))
    ref = store.get_ref(kind="se", id=_SLUG)
    assert ref is not None
    return persist.load_tree(store, ref.id)


def _yspan(tree: Any, name: str) -> tuple[float, float]:
    """A block's posed envelope y-extent, metres — the quantity every
    defect in gr514251 lives in. ``persist.load_tree`` leaves
    ``node.pose`` world-composed, so the span is the authored pose plus
    the cylinder's h along its rotated axis."""
    node = tree.blocks[name]
    lo, hi = _world_aabb(node)
    return float(lo[1]), float(hi[1])


def _world_aabb(node: Any) -> tuple[Any, Any]:
    from precis.cad import dsl as cad_dsl
    from precis.cad.bulk import expr_aabb
    from precis.cad.graph import Design as CadDesign
    from precis.cad.vec import as_vec3, pose

    spec = cad_dsl.parse(node.envelope)
    design = CadDesign()
    xform = pose(as_vec3([float(c) for c in node.pose]), as_vec3(node.rot))
    design.add_component(node.name, design.prim(node.name, cad_dsl.build(spec), xform))
    return expr_aabb(design, design.components[node.name])


def test_fixed_axle_reaches_into_both_cranks(handler: SeHandler, store: Store) -> None:
    tree = _build(handler, store, broken=False)
    ax_lo, ax_hi = _yspan(tree, "axle")
    cr_l_lo, cr_l_hi = _yspan(tree, "crank_left")
    cr_r_lo, cr_r_hi = _yspan(tree, "crank_right")
    # the axle's rod overlaps every millimetre of both crank slabs —
    # it no longer stops 5 mm short (prod: |y| <= 0.085 vs crank
    # |y| 0.090..0.110).
    assert ax_hi >= cr_l_hi, (ax_hi, cr_l_hi)
    assert ax_lo <= cr_r_lo, (ax_lo, cr_r_lo)


def test_fixed_bearings_mirror_each_other_and_sit_in_the_forks(
    handler: SeHandler, store: Store
) -> None:
    tree = _build(handler, store, broken=False)
    bl_lo, bl_hi = _yspan(tree, "bearing_left")
    br_lo, br_hi = _yspan(tree, "bearing_right")
    assert br_lo == pytest.approx(-bl_hi, abs=1e-9)
    assert br_hi == pytest.approx(-bl_lo, abs=1e-9)
    # each bearing is enclosed by its fork leg's slab
    fl_lo, fl_hi = _yspan(tree, "fork_left")
    fr_lo, fr_hi = _yspan(tree, "fork_right")
    assert fl_lo <= bl_lo and bl_hi <= fl_hi
    assert fr_lo <= br_lo and br_hi <= fr_hi


def test_fixed_pedals_are_180_degrees_apart(handler: SeHandler, store: Store) -> None:
    tree = _build(handler, store, broken=False)
    pl = tree.blocks["pedal_left"]
    pr = tree.blocks["pedal_right"]
    # a half-turn about the axle (the y axis at z=Z0): x and y negate.
    assert pr.pose[0] == pytest.approx(-pl.pose[0])
    assert pr.pose[1] == pytest.approx(-pl.pose[1])
    assert pl.pose[0] == pytest.approx(_MOD.CRANK_L)


def test_broken_variant_reproduces_prod_defects(
    handler: SeHandler, store: Store
) -> None:
    """The before-picture: broken replay carries the three defects the
    gripe names, so the fix's effect is attributable to the script, not
    the harness."""
    tree = _build(handler, store, broken=True)
    ax_lo, ax_hi = _yspan(tree, "axle")
    _, cr_l_hi = _yspan(tree, "crank_left")
    _, cr_r_lo = _yspan(tree, "crank_right")
    assert ax_hi < cr_l_hi and ax_lo > cr_r_lo  # stops short both sides
    br_lo, _ = _yspan(tree, "bearing_right")
    _, bl_hi = _yspan(tree, "bearing_left")
    assert br_lo == pytest.approx(-bl_hi - _MOD.BRG_W)  # asymmetric
    pr = tree.blocks["pedal_right"]
    pl = tree.blocks["pedal_left"]
    assert pr.pose[0] == pl.pose[0]  # both at +CRANK_L


def test_fixed_validate_warns_exactly_prods_known_findings(
    handler: SeHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unbounded overlap check: on a loaded CI runner the 30 s budget ran
    # out and added an overlap_budget_exceeded warning to the count (r104).
    monkeypatch.setattr(
        se_validate, "validate", functools.partial(se_validate.validate, budget_s=None)
    )
    _build(handler, store, broken=False)
    body = handler.get(id=_SLUG, view="validate").body
    assert "0 error(s), 4 warning(s)" in body
    # prod's two standing findings survive verbatim; nothing new is added
    assert "undeclared_interpenetration" in body  # axle—fork_left/right
    assert "crank_bolt" not in body


def test_fixed_clearance_engages_axle_and_bearing_symmetrically(
    handler: SeHandler, store: Store
) -> None:
    _build(handler, store, broken=False)
    body = handler.get(id=_SLUG, view="clearance").body
    rows = {(r["a"], r["b"]): r for r in _clearance_rows(body)}
    # (a) press joints now interpenetrate — the axle reaches each crank
    assert rows[("axle", "crank_left")]["gap (mm)"] < 0
    assert rows[("axle", "crank_right")]["gap (mm)"] < 0
    # (b) bearing/fork seats match left↔right
    assert rows[("bearing_right", "fork_right")]["gap (mm)"] == pytest.approx(
        rows[("bearing_left", "fork_left")]["gap (mm)"]
    )
    # (c) pedal↔crank gaps match left↔right
    assert rows[("crank_right", "pedal_right")]["gap (mm)"] == pytest.approx(
        rows[("crank_left", "pedal_left")]["gap (mm)"]
    )


def _clearance_rows(body: str) -> list[dict[str, Any]]:
    """Parse the clearance digest's tab-separated rows (the header line
    is the one wrapped in braces)."""
    rows = []
    for line in body.splitlines():
        fields = line.strip("{}").split("\t")
        if len(fields) < 7 or fields[0] == "id":
            continue
        rows.append(
            {
                "id": fields[0],
                "a": fields[1],
                "b": fields[2],
                "joint": fields[3],
                "gap (mm)": float(fields[4]),
                "via": fields[5],
                "tag": fields[6],
            }
        )
    return rows
