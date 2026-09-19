"""Print groups with ``intent='manufacture'`` — print-in-place
(docs/backlog/structural-solution-space.md §Slice 4 bridge, round B2;
:mod:`precis_se.manufacture`).

Pinned here, the spec's acceptance sentence 2 and the B2 list: a
two-member group with a ``revolute`` connect fused at ``gap=g`` exports
ONE 3MF with exactly two objects separated by at least ``g`` everywhere
(measured on the exported meshes, independently of the report); a gap
below an overridden ``min_clearance`` is an ``in_place_clearance`` error
and a null floor says "uncalibrated" without erroring; a rigid pair is
one object whose seam gains material under ``blend``; a bought member is
a cavity with a pause height; a fastener across fused members is elided
(and the same tree as a ``model`` group still prints it); the ``screw``
mechanism demand is satisfied by fusion for ``manufacture`` only; a
horizontal revolute is an ``overhang`` finding naming the bore; a re-run
mints a sibling; the fused SIMP domain solves the group as one body.

Fixtures: a pin-in-knuckle hinge (both printed, the knuckle's bore cut
into its cad design through the store), an L of two rigid boxes, and the
seat clamp's bolt. Every design slug is unique per test (the shared
test-DB rule).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.cad.scene import NodeSpec, SceneSpec
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import manufacture as se_manufacture
from precis_se import manufacture_job, persist, simp_bridge
from precis_se import printgroup as se_printgroup
from precis_se.handler import SeHandler
from precis_se.ops import SeTree, apply_ops
from tests.test_se_print_intent import _3mf_objects, handler, register_se_simp
from tests.test_se_print_views import _mint_slug
from tests.test_se_simp_bridge import _job_params

__all__ = ["handler", "register_se_simp"]  # fixtures re-exported for pytest

_PITCH = 0.0005
_GAP = 0.001
_PIN_R = 0.004


def _load(h: SeHandler, slug: str) -> SeTree:
    ref = h.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(h.store, ref.id)


def _ref_meta(h: SeHandler, slug: str) -> dict[str, Any]:
    ref = h.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return dict(ref.meta or {})


def _hinge_ops(
    *, axis: tuple[float, float, float] | None = (1.0, 0.0, 0.0)
) -> list[dict[str, Any]]:
    """``hinge`` ⊃ ``knuckle`` (20×20×12 box, bore along x at z=6) +
    ``pin`` (r4 cylinder along x through the bore, 5 mm proud each side),
    revolute about x. The bore is cut into the knuckle's cad design after
    ``realize`` (:func:`_cut_bore`)."""
    joint: dict[str, Any] = {"class": "revolute"}
    if axis is not None:
        joint["axis"] = list(axis)
    return [
        {"op": "add_block", "name": "hinge"},
        {
            "op": "add_block",
            "name": "knuckle",
            "parent": "hinge",
            "envelope": "box:w0.02d0.02h0.012",
        },
        {"op": "set_mode", "block": "knuckle", "mode": "fdm/pla"},
        {
            "op": "add_block",
            "name": "pin",
            "parent": "hinge",
            "envelope": f"cyl:r{_PIN_R}h0.03",
            "pose": [-0.015, 0, 0.006],
            "rot": [0, math.pi / 2, 0],
        },
        {"op": "set_mode", "block": "pin", "mode": "fdm/pla"},
        {"op": "add_port", "block": "knuckle", "name": "bore"},
        {"op": "add_port", "block": "pin", "name": "axis"},
        {"op": "connect", "a": "pin.axis", "b": "knuckle.bore", "joint": joint},
        {
            "op": "set_mode",
            "block": "hinge",
            "mode": "fdm/pla",
            "intent": "manufacture",
        },
        {"op": "set_build_frame", "block": "hinge", "down": [0, 0, -1]},
    ]


def _cut_bore(h: SeHandler, slug: str, block: str = "knuckle") -> None:
    """Replace the knuckle's seed design with box minus an x-axis bore."""
    tree = _load(h, slug)
    cad_slug = tree.blocks[block].bound
    assert cad_slug
    ref = h.store.get_ref(kind="cad", id=cad_slug)
    assert ref is not None
    spec, _handles = h.store.cad_load(ref.id)
    bore = NodeSpec(
        name="bore",
        op="cut",
        config=f"cyl:r{_PIN_R}h0.04",
        component=spec.nodes[0].component,
        loc=(-0.02, 0.0, 0.006),
        rot=(0.0, math.pi / 2, 0.0),
    )
    h.store.cad_save(
        slug=cad_slug,
        title=str(ref.title or cad_slug),
        spec=SceneSpec(nodes=[*spec.nodes, bore], components=list(spec.components)),
        card_text="knuckle with bore",
    )


def _realized_hinge(h: SeHandler, slug: str, **ops_kw: Any) -> None:
    h.put(id=slug, text=json.dumps({"ops": _hinge_ops(**ops_kw)}))
    for block in ("knuckle", "pin"):
        h.edit(id=slug, ops=[{"op": "realize", "block": block, "mode": "fdm/pla"}])
    _cut_bore(h, slug)


def _mfg(block: str = "hinge", **kw: Any) -> dict[str, Any]:
    op: dict[str, Any] = {
        "op": "realize",
        "block": block,
        "strategy": "manufacture",
        "pitch": _PITCH,
    }
    op.update(kw)
    return op


def _last(h: SeHandler, slug: str) -> dict[str, Any]:
    return dict(_ref_meta(h, slug)["manufacture"]["last"])


def _radial(verts: np.ndarray, axis_yz: np.ndarray) -> np.ndarray:
    return np.linalg.norm(verts[:, 1:] - axis_yz[None, :], axis=1)


# ---------------------------------------------------------------------------
# acceptance: the revolute pair — two objects, ≥ gap everywhere
# ---------------------------------------------------------------------------


def test_revolute_pair_exports_two_objects_separated_by_the_gap(
    handler: SeHandler, tmp_path: Path
) -> None:
    _realized_hinge(handler, "mf-hinge")
    resp = handler.edit(id="mf-hinge", ops=[_mfg(gap=_GAP)])
    assert "se_manufacture — hinge fused" in resp.body
    assert "2 object(s): knuckle, pin" in resp.body

    tree = _load(handler, "mf-hinge")
    root = tree.blocks["hinge"]
    assert root.bound_kind == "cad" and root.bound == "mf-hinge-hinge-mfg"
    last = _last(handler, "mf-hinge")
    assert last["cad"] == "mf-hinge-hinge-mfg"
    assert last["fused_components"] == [["knuckle"], ["pin"]]
    assert last["eroded"] == ["knuckle", "pin"]
    assert [o["members"] for o in last["objects"]] == [["knuckle"], ["pin"]]
    (gap_row,) = last["gaps"]
    assert {gap_row["a"], gap_row["b"]} == {"pin", "knuckle"}
    assert gap_row["measured_m"] >= _GAP
    # uncalibrated floor: an info finding saying so, no error
    clearance = [f for f in last["findings"] if f["rule"] == "in_place_clearance"]
    assert [f["severity"] for f in clearance] == ["info"]
    assert "uncalibrated" in clearance[0]["detail"]
    assert "taken as declared" in clearance[0]["detail"]

    # the report: manufacture detail, frame pinned, gaps + objects rendered
    report = se_printgroup.report_for(tree, "hinge", cad_store_reader=handler.store)
    assert report is not None and report.intent == "manufacture"
    detail = report.manufacture
    assert detail is not None and detail.realized and not detail.stale
    assert [n for n, _v, _t in detail.objects] == ["knuckle", "pin"]
    assert report.pinned and report.chosen_down is not None
    body = handler.get(id="mf-hinge", view="print", args={"block": "hinge"}).body
    assert "fused components: knuckle; pin" in body
    assert (
        "DOF joints (each printed side carved back gap/2 from its partner): "
        "knuckle.bore—pin.axis" in body
    )
    assert "objects (2, one per connected component): knuckle, pin" in body
    assert re.search(r"gap knuckle–pin: measured [\d.]+ mm min separation", body)
    assert "elided fasteners: none" in body and "cavities: none" in body

    # the 3MF: exactly two objects, and the meshes prove the gap
    out = tmp_path / "hinge.3mf"
    resp = handler.get(
        id="mf-hinge",
        view="print",
        args={"block": "hinge", "fmt": "3mf", "path": str(out)},
    )
    assert "2 object(s): knuckle, pin" in resp.body
    assert "one per connected component" in resp.body
    objects = _3mf_objects(out)
    assert set(objects) == {"knuckle", "pin"}
    pin, knuckle = objects["pin"], objects["knuckle"]
    # frame pinned to identity: the pin's axis stays along x, and its
    # (y, z) centroid IS the axis (a cylinder is symmetric about it)
    axis_yz = pin[:, 1:].mean(axis=0)
    gap_mm = _GAP * 1000.0
    # the carve is seam-local: inside the knuckle the pin is thinner by
    # gap/2, while its proud ends keep the full radius
    kx_lo, kx_hi = knuckle[:, 0].min(), knuckle[:, 0].max()
    in_bore = (pin[:, 0] > kx_lo + 2.0) & (pin[:, 0] < kx_hi - 2.0)
    proud = (pin[:, 0] < kx_lo - 2.0) | (pin[:, 0] > kx_hi + 2.0)
    assert in_bore.any() and proud.any()
    assert np.all(_radial(pin[in_bore], axis_yz) <= _PIN_R * 1000.0 - gap_mm / 2 + 1e-6)
    assert _radial(pin[proud], axis_yz).max() == pytest.approx(_PIN_R * 1000.0, abs=0.3)
    inside_x = (knuckle[:, 0] > pin[:, 0].min() + 1.0) & (
        knuckle[:, 0] < pin[:, 0].max() - 1.0
    )
    bore = knuckle[inside_x]
    near = _radial(bore, axis_yz) < 7.0  # the bore's wall, not the box's faces
    assert near.any()
    assert np.all(_radial(bore[near], axis_yz) >= _PIN_R * 1000.0 + gap_mm / 2 - 1e-6)
    # and the brute-force vertex-to-vertex floor agrees with the report
    d = np.linalg.norm(pin[:, None, :] - knuckle[None, ::7, :], axis=2)
    assert d.min() >= gap_mm - 1e-6
    assert gap_row["measured_m"] * 1000.0 <= d.min() + 1e-6

    # fab collapses the group to one row naming the manufacture counts
    fab = handler.get(id="mf-hinge", view="fab").body
    assert len(re.findall(r"^hinge\t", fab, flags=re.M)) == 1
    assert "intent manufacture, 2 objects, 0 cavities, 0 elided" in fab
    assert not re.search(r"^(pin|knuckle)\t", fab, flags=re.M)


def test_gap_below_an_overridden_floor_is_an_error_finding(
    handler: SeHandler, tmp_path: Path
) -> None:
    _realized_hinge(handler, "mf-floor")
    handler.edit(
        id="mf-floor",
        ops=[
            {
                "op": "set_process_override",
                "block": "hinge",
                "field": "min_clearance",
                "value": 2.0,
            }
        ],
    )
    resp = handler.edit(id="mf-floor", ops=[_mfg(gap=_GAP)])
    assert "ERROR finding(s): in_place_clearance" in resp.body
    last = _last(handler, "mf-floor")
    assert last["gap_floor_mm"] == 2.0 and last["gap_source"] == "given"
    (f,) = [f for f in last["findings"] if f["rule"] == "in_place_clearance"]
    assert (
        f["severity"] == "error" and "below the min_clearance floor 2 mm" in f["detail"]
    )
    body = handler.get(id="mf-floor", view="print", args={"block": "hinge"}).body
    assert "in_place_clearance" in body and "floor 2 mm" in body
    out = tmp_path / "floor.3mf"
    resp = handler.get(
        id="mf-floor",
        view="print",
        args={"block": "hinge", "fmt": "3mf", "path": str(out)},
    )
    assert "exported WITH error-severity finding(s)" in resp.body

    # the house floor is also the DEFAULT gap when none is given
    resp = handler.edit(id="mf-floor", ops=[_mfg()])
    assert "gap 0.002 m (house)" in resp.body
    assert _last(handler, "mf-floor")["gap_source"] == "house"


def test_gap_is_required_while_the_floor_is_null(handler: SeHandler) -> None:
    _realized_hinge(handler, "mf-nogap")
    with pytest.raises(BadInput, match="needs gap=") as exc:
        handler.edit(id="mf-nogap", ops=[_mfg()])
    assert "min_clearance" in str(exc.value) and "uncalibrated" in str(exc.value)
    # nothing bound
    assert _load(handler, "mf-nogap").blocks["hinge"].bound_kind is None


# ---------------------------------------------------------------------------
# rigid pair: one object, blended seam
# ---------------------------------------------------------------------------


def _ell_ops(*, intent: str | None = "manufacture") -> list[dict[str, Any]]:
    """``ell`` ⊃ ``foot`` (20×10×10 box) + ``post`` (10×10×20 box beside it,
    touching on x = 10 mm), rigid."""
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "ell"},
        {
            "op": "add_block",
            "name": "foot",
            "parent": "ell",
            "envelope": "box:w0.02d0.01h0.01",
        },
        {"op": "set_mode", "block": "foot", "mode": "fdm/pla"},
        {
            "op": "add_block",
            "name": "post",
            "parent": "ell",
            "envelope": "box:w0.01d0.01h0.02",
            "pose": [0.015, 0, 0],
        },
        {"op": "set_mode", "block": "post", "mode": "fdm/pla"},
        {"op": "add_port", "block": "foot", "name": "end"},
        {"op": "add_port", "block": "post", "name": "side"},
        {
            "op": "connect",
            "a": "foot.end",
            "b": "post.side",
            "joint": {"class": "rigid"},
        },
    ]
    mode_op: dict[str, Any] = {"op": "set_mode", "block": "ell", "mode": "fdm/pla"}
    if intent is not None:
        mode_op["intent"] = intent
    ops.append(mode_op)
    ops.append({"op": "set_build_frame", "block": "ell", "down": [0, 0, -1]})
    return ops


def _realized_ell(h: SeHandler, slug: str) -> None:
    h.put(id=slug, text=json.dumps({"ops": _ell_ops()}))
    for block in ("foot", "post"):
        h.edit(id=slug, ops=[{"op": "realize", "block": block, "mode": "fdm/pla"}])


def test_rigid_pair_is_one_object_and_blend_adds_seam_material(
    handler: SeHandler, tmp_path: Path
) -> None:
    _realized_ell(handler, "mf-ell")
    handler.edit(id="mf-ell", ops=[_mfg("ell")])  # no DOF joint: no gap needed
    plain = _last(handler, "mf-ell")
    assert plain["fused_components"] == [["foot", "post"]]
    assert plain["gap_m"] is None and plain["eroded"] == []
    assert [o["members"] for o in plain["objects"]] == [["foot", "post"]]
    out = tmp_path / "ell.3mf"
    handler.get(
        id="mf-ell", view="print", args={"block": "ell", "fmt": "3mf", "path": str(out)}
    )
    assert set(_3mf_objects(out)) == {"foot+post"}

    handler.edit(id="mf-ell", ops=[_mfg("ell", blend=0.003)])
    blended = _last(handler, "mf-ell")
    assert blended["blend_m"] == 0.003
    # a smooth-min only ever ADDS material, inside the concave seam
    assert blended["volume_m3"] > plain["volume_m3"]
    assert blended["volume_m3"] < plain["volume_m3"] * 1.05
    # re-run minted a sibling and left the first design in place
    assert blended["cad"] == "mf-ell-ell-mfg-2"
    assert blended["previous_cad"] == "mf-ell-ell-mfg"
    assert handler.store.get_ref(kind="cad", id="mf-ell-ell-mfg") is not None
    runs = _ref_meta(handler, "mf-ell")["manufacture"]["runs"]
    assert [r["cad"] for r in runs] == ["mf-ell-ell-mfg", "mf-ell-ell-mfg-2"]
    assert _load(handler, "mf-ell").blocks["ell"].bound == "mf-ell-ell-mfg-2"


# ---------------------------------------------------------------------------
# a bought member: cavity + pause height; a fastener across fused members: elided
# ---------------------------------------------------------------------------


def _clamp_ops(
    screw_slug: str,
    *,
    intent: str = "manufacture",
    bolt_rot: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> list[dict[str, Any]]:
    """``assy`` ⊃ ``clamp`` (30×20×12 box at z = 8..20) + ``bolt`` (csk M4×12,
    head top at z = 8, thread +z, optionally rotated) — the seat clamp
    without its rail."""
    return [
        {"op": "add_block", "name": "assy"},
        {
            "op": "add_block",
            "name": "clamp",
            "parent": "assy",
            "envelope": "box:w0.03d0.02h0.012",
            "pose": [0, 0, 0.008],
        },
        {"op": "set_mode", "block": "clamp", "mode": "fdm/asa"},
        {
            "op": "add_block",
            "name": "bolt",
            "parent": "assy",
            "pose": [0, 0, 0.008],
            "rot": list(bolt_rot),
        },
        {"op": "set_mode", "block": "bolt", "mode": "purchase"},
        {
            "op": "set_binding",
            "block": "bolt",
            "kind": "component",
            "design": screw_slug,
        },
        {"op": "add_port", "block": "bolt", "name": "thread"},
        {"op": "add_port", "block": "clamp", "name": "boss"},
        {
            "op": "connect",
            "a": "bolt.thread",
            "b": "clamp.boss",
            "joint": {
                "class": "rigid",
                "mechanism": "screw",
                "params": {"thread_strategy": "nut-trap"},
            },
        },
        {"op": "set_mode", "block": "assy", "mode": "fdm/asa", "intent": intent},
        {"op": "set_build_frame", "block": "assy", "down": [0, 0, -1]},
    ]


def test_bought_member_becomes_a_cavity_with_a_pause_height(
    handler: SeHandler, hub: Hub, tmp_path: Path
) -> None:
    screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
    handler.put(id="mf-cav", text=json.dumps({"ops": _clamp_ops(screw_slug)}))
    handler.edit(
        id="mf-cav", ops=[{"op": "realize", "block": "clamp", "mode": "fdm/asa"}]
    )
    with pytest.raises(BadInput, match="needs fit="):
        handler.edit(id="mf-cav", ops=[_mfg("assy")])
    fit = 0.0005
    resp = handler.edit(id="mf-cav", ops=[_mfg("assy", fit=fit)])
    assert "1 cavity(ies)" in resp.body and "0 fastener(s) elided" in resp.body
    last = _last(handler, "mf-cav")
    (cav,) = last["cavities"]
    assert cav["block"] == "bolt" and cav["fit_m"] == fit
    assert "catalog part 'csk:m4x12'" in cav["source"]
    assert last["elided"] == []

    # the stored fused field, in the root's frame (= world here): a point
    # inside the bolt's shank is void, a point `fit` off its surface is
    # void, a point well inside the clamp beyond the fit is solid
    tree = _load(handler, "mf-cav")
    stored = se_manufacture._stored_root(handler.store, tree.blocks["assy"], "assy")
    assert stored is not None
    _slug, _summary, fld = stored
    shank_r = 0.002
    z_mid = 0.008 + 0.006  # halfway down the thread, inside the clamp
    assert fld.distance_local_np(np.array([[0.0, 0.0, z_mid]]))[0] > 0.0
    assert fld.distance_local_np(np.array([[shank_r + 0.6 * fit, 0.0, z_mid]]))[0] > 0.0
    assert (
        fld.distance_local_np(np.array([[shank_r + fit + 2 * _PITCH, 0.0, z_mid]]))[0]
        < 0.0
    )
    assert fld.distance_local_np(np.array([[0.012, 0.0, z_mid]]))[0] < 0.0

    body = handler.get(id="mf-cav", view="print", args={"block": "assy"}).body
    m = re.search(
        r"cavity 'bolt' \((.*)\): top layer at ([\d.]+) mm above the bed", body
    )
    assert m is not None, body
    assert "csk:m4x12" in m.group(1)
    # head top at z = 8 mm, 12 mm of thread up: top at 20 mm + fit, over a
    # bed at the clamp's bottom (z = 8 mm)
    assert float(m.group(2)) == pytest.approx((0.020 + fit - 0.008) * 1000.0, abs=0.05)
    assert "mid-print pause here" in body and "bambuuzle" in body
    fab = handler.get(id="mf-cav", view="fab").body
    assert "intent manufacture, 1 objects, 1 cavities, 0 elided" in fab
    out = tmp_path / "cav.3mf"
    resp = handler.get(
        id="mf-cav",
        view="print",
        args={"block": "assy", "fmt": "3mf", "path": str(out)},
    )
    assert set(_3mf_objects(out)) == {"clamp"}
    assert "cavity 'bolt'" in resp.body


def _plates_ops(screw_slug: str, *, intent: str) -> list[dict[str, Any]]:
    """``stack`` ⊃ ``lower`` (30×20×4 at z 0..4) + ``upper`` (same at z 4..8),
    rigid; ``bolt`` (csk M4×12) head top at z = 0 driving +z through both;
    plus a bare ``screw``-mechanism connect between the plates with no
    fastener block behind it."""
    return [
        {"op": "add_block", "name": "stack"},
        {
            "op": "add_block",
            "name": "lower",
            "parent": "stack",
            "envelope": "box:w0.03d0.02h0.004",
        },
        {"op": "set_mode", "block": "lower", "mode": "fdm/asa"},
        {
            "op": "add_block",
            "name": "upper",
            "parent": "stack",
            "envelope": "box:w0.03d0.02h0.004",
            "pose": [0, 0, 0.004],
        },
        {"op": "set_mode", "block": "upper", "mode": "fdm/asa"},
        {"op": "add_block", "name": "bolt", "parent": "stack", "pose": [0.008, 0, 0]},
        {"op": "set_mode", "block": "bolt", "mode": "purchase"},
        {
            "op": "set_binding",
            "block": "bolt",
            "kind": "component",
            "design": screw_slug,
        },
        {"op": "add_port", "block": "bolt", "name": "thread"},
        {"op": "add_port", "block": "upper", "name": "boss"},
        {"op": "add_port", "block": "upper", "name": "face"},
        {"op": "add_port", "block": "lower", "name": "face"},
        {"op": "add_port", "block": "lower", "name": "boss2"},
        {"op": "add_port", "block": "upper", "name": "boss2"},
        {
            "op": "connect",
            "a": "bolt.thread",
            "b": "upper.boss",
            "joint": {
                "class": "rigid",
                "mechanism": "screw",
                "params": {"thread_strategy": "nut-trap"},
            },
        },
        {
            "op": "connect",
            "a": "upper.face",
            "b": "lower.face",
            "joint": {"class": "rigid"},
        },
        {
            "op": "connect",
            "a": "upper.boss2",
            "b": "lower.boss2",
            "joint": {"class": "rigid", "mechanism": "screw"},
        },
        {"op": "set_mode", "block": "stack", "mode": "fdm/asa", "intent": intent},
        {"op": "set_build_frame", "block": "stack", "down": [0, 0, -1]},
    ]


def test_fastener_across_fused_members_is_elided_but_model_still_prints_it(
    handler: SeHandler, hub: Hub, tmp_path: Path
) -> None:
    screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
    handler.put(
        id="mf-elide",
        text=json.dumps({"ops": _plates_ops(screw_slug, intent="manufacture")}),
    )
    for block in ("lower", "upper"):
        handler.edit(
            id="mf-elide", ops=[{"op": "realize", "block": block, "mode": "fdm/asa"}]
        )
    resp = handler.edit(id="mf-elide", ops=[_mfg("stack", fit=0.0005)])
    assert "1 fastener(s) elided" in resp.body and "0 cavity(ies)" in resp.body
    last = _last(handler, "mf-elide")
    (el,) = last["elided"]
    assert el["block"] == "bolt" and el["stack"] == ["lower", "upper"]
    assert last["cavities"] == []
    (f,) = [f for f in last["findings"] if f["rule"] == "fastener_elided"]
    assert f["detail"].startswith("joint fused, bolt not needed")
    assert [o["members"] for o in last["objects"]] == [["lower", "upper"]]

    tree = _load(handler, "mf-elide")
    report = se_printgroup.report_for(tree, "stack", cad_store_reader=handler.store)
    assert report is not None and report.manufacture is not None
    assert report.manufacture.elided == ["bolt"]
    # the bare screw-mechanism connect between the fused plates is NOT an
    # abstract joint here: fusion satisfies the demand
    assert not any(f.rule == "abstract_joint" for f in report.findings)
    body = handler.get(id="mf-elide", view="print", args={"block": "stack"}).body
    assert "elided fasteners: bolt" in body
    out = tmp_path / "elide.3mf"
    handler.get(
        id="mf-elide",
        view="print",
        args={"block": "stack", "fmt": "3mf", "path": str(out)},
    )
    assert set(_3mf_objects(out)) == {"lower+upper"}
    fab = handler.get(id="mf-elide", view="fab").body
    assert "intent manufacture, 1 objects, 0 cavities, 1 elided" in fab

    # the SAME tree as a model group: the bolt is a stand-in object and the
    # bare screw connect is abstract again
    handler.put(
        id="mf-elide-model",
        text=json.dumps({"ops": _plates_ops(screw_slug, intent="model")}),
    )
    for block in ("lower", "upper"):
        handler.edit(
            id="mf-elide-model",
            ops=[{"op": "realize", "block": block, "mode": "fdm/asa"}],
        )
    model = se_printgroup.report_for(
        _load(handler, "mf-elide-model"), "stack", cad_store_reader=handler.store
    )
    assert model is not None and model.manufacture is None
    assert [m.block for m in model.exportable] == ["bolt", "lower", "upper"]
    assert any(f.rule == "abstract_joint" for f in model.findings)
    out2 = tmp_path / "elide-model.3mf"
    handler.get(
        id="mf-elide-model",
        view="print",
        args={"block": "stack", "fmt": "3mf", "path": str(out2)},
    )
    assert set(_3mf_objects(out2)) == {"bolt", "lower", "upper"}


# ---------------------------------------------------------------------------
# teardrop rule, unrealized report, request/job shape
# ---------------------------------------------------------------------------


def test_horizontal_revolute_is_an_overhang_finding_naming_the_bore(
    handler: SeHandler,
) -> None:
    _realized_hinge(handler, "mf-bore")
    handler.edit(id="mf-bore", ops=[_mfg(gap=_GAP)])
    tree = _load(handler, "mf-bore")
    report = se_printgroup.report_for(tree, "hinge", cad_store_reader=handler.store)
    assert report is not None
    bore = [
        f
        for f in report.findings
        if f.rule == "overhang" and f.subject == "knuckle.bore—pin.axis"
    ]
    assert len(bore) == 1 and bore[0].severity == "warn"
    assert "'pin'" in bore[0].detail and "'knuckle'" in bore[0].detail
    assert (
        "axis [1, 0, 0]" in bore[0].detail
        and "0° from the build plate" in bore[0].detail
    )
    assert "teardrop" in bore[0].detail
    # axis vertical in the frame: the rule is silent
    handler.edit(
        id="mf-bore",
        ops=[{"op": "set_build_frame", "block": "hinge", "down": [-1, 0, 0]}],
    )
    tree = _load(handler, "mf-bore")
    report = se_printgroup.report_for(tree, "hinge", cad_store_reader=handler.store)
    assert report is not None
    assert not any(
        f.rule == "overhang" and f.subject == "knuckle.bore—pin.axis"
        for f in report.findings
    )
    # no axis declared: the rule says it could not run, never silence
    _realized_hinge(handler, "mf-noaxis", axis=None)
    handler.edit(id="mf-noaxis", ops=[_mfg(gap=_GAP)])
    report = se_printgroup.report_for(
        _load(handler, "mf-noaxis"), "hinge", cad_store_reader=handler.store
    )
    assert report is not None
    (f,) = [
        f
        for f in report.findings
        if f.rule == "overhang" and f.subject == "knuckle.bore—pin.axis"
    ]
    assert f.severity == "info" and "declares no axis" in f.detail


def test_unrealized_manufacture_group_reports_its_plan_and_refuses_export(
    handler: SeHandler,
) -> None:
    _realized_hinge(handler, "mf-plan")
    tree = _load(handler, "mf-plan")
    report = se_printgroup.report_for(tree, "hinge", cad_store_reader=handler.store)
    assert report is not None and report.manufacture is not None
    assert not report.manufacture.realized
    assert any(f.rule == "manufacture_unrealized" for f in report.findings)
    body = handler.get(id="mf-plan", view="print", args={"block": "hinge"}).body
    assert "fused realization: none yet" in body
    assert "Next: realize(block='hinge', strategy='manufacture'" in body
    with pytest.raises(BadInput, match="no fused realization"):
        handler.get(id="mf-plan", view="print", args={"block": "hinge", "fmt": "3mf"})
    fab = handler.get(id="mf-plan", view="fab").body
    assert "intent manufacture, not fused yet" in fab
    summary = handler.get(id="mf-plan", view="print").body
    assert "## hinge — print group, intent manufacture" in summary
    assert "## pin" not in summary


def test_stale_fuse_is_flagged_after_a_member_moves(handler: SeHandler) -> None:
    _realized_hinge(handler, "mf-stale")
    handler.edit(id="mf-stale", ops=[_mfg(gap=_GAP)])
    handler.edit(
        id="mf-stale",
        ops=[{"op": "set_pose", "block": "pin", "pose": [-0.015, 0, 0.007]}],
    )
    report = se_printgroup.report_for(
        _load(handler, "mf-stale"), "hinge", cad_store_reader=handler.store
    )
    assert report is not None and report.manufacture is not None
    assert report.manufacture.stale
    assert any(f.rule == "manufacture_stale" for f in report.findings)


def test_refusals_and_the_request_shape(handler: SeHandler) -> None:
    _realized_hinge(handler, "mf-refuse")
    with pytest.raises(
        BadInput, match="not a print group root with intent 'manufacture'"
    ):
        handler.edit(id="mf-refuse", ops=[_mfg("pin", gap=_GAP)])
    with pytest.raises(BadInput, match="disagrees with the group root's mode"):
        handler.edit(id="mf-refuse", ops=[_mfg(gap=_GAP, mode="fdm/asa")])
    with pytest.raises(BadInput, match="blend=.*below the pitch"):
        handler.edit(id="mf-refuse", ops=[_mfg(gap=_GAP, blend=_PITCH / 4)])
    with pytest.raises(BadInput, match="above the .* budget"):
        handler.edit(id="mf-refuse", ops=[_mfg(gap=_GAP, pitch=0.00001)])
    # the request round-trips through the job's params schema
    tree = _load(handler, "mf-refuse")
    echo, req = se_manufacture.prepare_manufacture(
        tree, _mfg(gap=_GAP, blend=0.002), cad_store_reader=handler.store
    )
    assert req.sync and "fusing inline" in echo
    params = req.to_params()
    assert set(params) <= set(manufacture_job.PARAMS_SCHEMA["properties"])
    assert se_manufacture.ManufactureRequest.from_params(
        params
    ) == se_manufacture.ManufactureRequest(**{**req.__dict__, "sync": False})
    assert manufacture_job.SPEC.name == "se_manufacture"
    assert manufacture_job.SPEC.compatible_executors == frozenset({"job_inproc"})
    # a finer pitch pushes the grid over the inline cap: the op enqueues
    _e, big = se_manufacture.prepare_manufacture(
        tree, _mfg(gap=_GAP, pitch=0.0001), cad_store_reader=handler.store
    )
    assert not big.sync


def test_classify_joints_is_pure_over_the_tree() -> None:
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "a", "envelope": "box:w0.01d0.01h0.01"},
            {"op": "add_block", "name": "b", "envelope": "box:w0.01d0.01h0.01"},
            {"op": "add_block", "name": "c", "envelope": "box:w0.01d0.01h0.01"},
            {"op": "add_block", "name": "d", "envelope": "box:w0.01d0.01h0.01"},
            {"op": "add_port", "block": "a", "name": "p"},
            {"op": "add_port", "block": "b", "name": "p"},
            {"op": "add_port", "block": "b", "name": "q"},
            {"op": "add_port", "block": "c", "name": "p"},
            {"op": "add_port", "block": "c", "name": "q"},
            {"op": "add_port", "block": "d", "name": "p"},
            {"op": "connect", "a": "a.p", "b": "b.p", "joint": {"class": "rigid"}},
            {
                "op": "connect",
                "a": "b.q",
                "b": "c.p",
                "joint": {"class": "prismatic", "axis": [0, 0, 1]},
            },
            {"op": "connect", "a": "c.q", "b": "d.p"},  # undeclared: fused, said so
        ],
    )
    plan = se_manufacture.classify_joints(tree, ["a", "b", "c", "d"], [])
    assert plan.components() == [["a", "b"], ["c", "d"]]
    assert plan.eroded == {"b", "c"}
    assert [e.subject for e in plan.dof_edges] == ["b.q—c.p"]
    assert plan.fused("a", "b") and plan.fused("c", "d") and not plan.fused("b", "c")
    assert [f.rule for f in plan.findings] == ["joint_undeclared"]


# ---------------------------------------------------------------------------
# reviewer round: seam-local gap, dof_bridged, elided holes, grid pause
# ---------------------------------------------------------------------------


def _based_hinge_ops(*, bridge: bool) -> list[dict[str, Any]]:
    """The hinge plus a ``base`` plate (20×20×4 at z = -4..0) rigidly under
    the knuckle, touching it on z = 0; ``bridge=True`` also connects the
    pin rigidly to the base, closing a rigid path around the revolute."""
    ops = _hinge_ops()
    ops[1:1] = [
        {
            "op": "add_block",
            "name": "base",
            "parent": "hinge",
            "envelope": "box:w0.02d0.02h0.004",
            "pose": [0, 0, -0.004],
        },
        {"op": "set_mode", "block": "base", "mode": "fdm/pla"},
    ]
    ops += [
        {"op": "add_port", "block": "base", "name": "top"},
        {"op": "add_port", "block": "knuckle", "name": "bottom"},
        {
            "op": "connect",
            "a": "base.top",
            "b": "knuckle.bottom",
            "joint": {"class": "rigid"},
        },
    ]
    if bridge:
        ops += [
            {"op": "add_port", "block": "base", "name": "pin_seat"},
            {"op": "add_port", "block": "pin", "name": "end"},
            {
                "op": "connect",
                "a": "base.pin_seat",
                "b": "pin.end",
                "joint": {"class": "rigid"},
            },
        ]
    return ops


def test_seam_local_gap_keeps_the_rigid_seam_intact(
    handler: SeHandler, tmp_path: Path
) -> None:
    handler.put(id="mf-seam", text=json.dumps({"ops": _based_hinge_ops(bridge=False)}))
    for block in ("base", "knuckle", "pin"):
        handler.edit(
            id="mf-seam", ops=[{"op": "realize", "block": block, "mode": "fdm/pla"}]
        )
    _cut_bore(handler, "mf-seam")
    handler.edit(id="mf-seam", ops=[_mfg(gap=_GAP)])
    last = _last(handler, "mf-seam")
    assert last["fused_components"] == [["base", "knuckle"], ["pin"]]
    assert [o["members"] for o in last["objects"]] == [["base", "knuckle"], ["pin"]]
    (gap_row,) = last["gaps"]
    assert gap_row["measured_m"] >= _GAP
    assert not any(f["rule"] == "dof_bridged" for f in last["findings"])
    # a probe ON the base–knuckle seam (z = 0), away from the bore, is inside
    # the fused solid: the knuckle was carved back from the pin only, not
    # shrunk everywhere (the seam itself reads exactly 0 now that the base
    # is analytic — on the surface of both, inside by the <= convention)
    tree = _load(handler, "mf-seam")
    stored = se_manufacture._stored_root(handler.store, tree.blocks["hinge"], "hinge")
    assert stored is not None
    fld = stored[2]
    for x in (0.007, -0.007):
        assert fld.distance_local_np(np.array([[x, 0.007, 0.0]]))[0] <= 1e-9
        assert fld.distance_local_np(np.array([[x, 0.007, 0.0005]]))[0] < 0.0
        assert fld.distance_local_np(np.array([[x, 0.007, -0.0005]]))[0] < 0.0
    # and the bore wall is still gapped: a point just off the pin's surface
    # (radially, at the bore's mid-length) is void
    probe = np.array([[0.0, 0.0, 0.006 + _PIN_R + 0.0003]])
    assert fld.distance_local_np(probe)[0] > 0.0
    out = tmp_path / "seam.3mf"
    handler.get(
        id="mf-seam",
        view="print",
        args={"block": "hinge", "fmt": "3mf", "path": str(out)},
    )
    assert set(_3mf_objects(out)) == {"base+knuckle", "pin"}


def test_bridged_dof_is_an_error_and_the_export_is_refused(
    handler: SeHandler,
) -> None:
    handler.put(id="mf-bridge", text=json.dumps({"ops": _based_hinge_ops(bridge=True)}))
    for block in ("base", "knuckle", "pin"):
        handler.edit(
            id="mf-bridge", ops=[{"op": "realize", "block": block, "mode": "fdm/pla"}]
        )
    _cut_bore(handler, "mf-bridge")
    tree = _load(handler, "mf-bridge")
    geo = se_manufacture.group_geometry(tree, "hinge", cad_store_reader=handler.store)
    assert [e.subject for e in geo.plan.bridged] == ["knuckle.bore—pin.axis"]
    (f,) = [f for f in geo.plan.findings if f.rule == "dof_bridged"]
    assert f.severity == "error"
    assert "bridged by a rigid path through base" in f.detail
    assert "not print-in-place" in f.detail
    handler.edit(id="mf-bridge", ops=[_mfg(gap=_GAP)])
    body = handler.get(id="mf-bridge", view="print", args={"block": "hinge"}).body
    assert "dof_bridged" in body and "BRIDGED by a rigid path" in body
    with pytest.raises(BadInput, match="not print-in-place.*dof_bridged"):
        handler.get(id="mf-bridge", view="print", args={"block": "hinge", "fmt": "3mf"})


def test_elided_fastener_leaves_no_hole_in_the_fused_solid(
    handler: SeHandler, hub: Hub
) -> None:
    from precis.cad.relate import component_sdf_np
    from precis_se.printsolid import printed_solid

    screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
    handler.put(
        id="mf-nohole",
        text=json.dumps({"ops": _plates_ops(screw_slug, intent="manufacture")}),
    )
    for block in ("lower", "upper"):
        handler.edit(
            id="mf-nohole", ops=[{"op": "realize", "block": block, "mode": "fdm/asa"}]
        )
    handler.edit(id="mf-nohole", ops=[_mfg("stack", fit=0.0005)])
    tree = _load(handler, "mf-nohole")
    assert _last(handler, "mf-nohole")["elided"][0]["block"] == "bolt"
    stored = se_manufacture._stored_root(handler.store, tree.blocks["stack"], "stack")
    assert stored is not None
    fld = stored[2]
    # on the screw axis (x = 8 mm), inside each plate: solid in the fuse
    for z in (0.002, 0.006):
        assert fld.distance_local_np(np.array([[0.008, 0.0, z]]))[0] < 0.0
    # ...and void in the same member's ordinary (model / DRC) print, which
    # still carries the clearance hole
    lower = printed_solid(tree, "lower", cad_store_reader=handler.store)
    assert lower is not None and lower.features
    d = lower.design
    assert component_sdf_np(d, d.whole(), np.array([[0.008, 0.0, 0.002]]))[0] > 0.0
    # the exclusion is exactly the elided set: passing it drops the hole
    plain = printed_solid(
        tree, "lower", cad_store_reader=handler.store, exclude=frozenset({"bolt"})
    )
    assert plain is not None and not plain.features
    dp = plain.design
    assert component_sdf_np(dp, dp.whole(), np.array([[0.008, 0.0, 0.002]]))[0] < 0.0


def test_pause_height_follows_a_rotated_cavity_to_within_a_pitch(
    handler: SeHandler, hub: Hub
) -> None:
    from precis.cad.export import _component_meshes, _scaled_for_export
    from precis.cad.vec import as_vec3
    from precis.cad.vec import pose as cad_pose

    screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
    rot = (math.radians(30.0), 0.0, 0.0)  # 30° about x: the thread leans -y
    handler.put(
        id="mf-tilt", text=json.dumps({"ops": _clamp_ops(screw_slug, bolt_rot=rot)})
    )
    handler.edit(
        id="mf-tilt", ops=[{"op": "realize", "block": "clamp", "mode": "fdm/asa"}]
    )
    fit = 0.0005
    handler.edit(id="mf-tilt", ops=[_mfg("assy", fit=fit)])
    tree = _load(handler, "mf-tilt")
    report = se_printgroup.report_for(tree, "assy", cad_store_reader=handler.store)
    assert report is not None and report.manufacture is not None
    ((block, pause, _source),) = report.manufacture.pauses
    assert block == "bolt"
    # the analytic answer: the rotated stand-in's true top along +z (down
    # is pinned to -z), plus the fit, over the clamp's bottom at z = 8 mm
    bolt = next(m for m in report.members if m.block == "bolt")
    assert bolt.stand_in is not None
    (_n, verts_mm, _t), *rest = _component_meshes(
        _scaled_for_export(bolt.stand_in.spec)
    )
    assert not rest
    xf = cad_pose(as_vec3([0.0, 0.0, 0.008]), as_vec3(list(rot)))
    world = (verts_mm / 1000.0) @ xf.R.T + xf.t
    expected = float(world[:, 2].max()) + fit - 0.008
    assert abs(pause - expected) <= _PITCH, (pause, expected)
    # and the old AABB reading would have been wrong by more than that
    corners = np.array(
        [
            [x, y, z]
            for x in (-0.004, 0.004)
            for y in (-0.004, 0.004)
            for z in (0.0, 0.0125)
        ]
    )
    aabb_top = float((corners @ xf.R.T + xf.t)[:, 2].max()) + fit - 0.008
    assert aabb_top - expected > _PITCH


# ---------------------------------------------------------------------------
# the fused SIMP domain: one solve over the group
# ---------------------------------------------------------------------------


def test_simp_on_a_manufacture_root_solves_the_fused_group(
    handler: SeHandler, store: Store, register_se_simp: Any
) -> None:
    ops = _ell_ops() + [
        {"op": "set_load", "block": "ell", "force": [0.0, 0.0, -10.0], "fixed": True},
    ]
    handler.put(id="mf-simp", text=json.dumps({"ops": ops}))
    for block in ("foot", "post"):
        handler.edit(
            id="mf-simp", ops=[{"op": "realize", "block": block, "mode": "fdm/pla"}]
        )
    resp = handler.edit(
        id="mf-simp",
        ops=[
            {
                "op": "realize",
                "block": "ell",
                "mode": "fdm/pla",
                "strategy": "simp",
                "pitch": 0.002,
                "volfrac": 0.5,
                "load_at": "z+",
                "fixed_at": "x-",
                "max_iter": 10,
            }
        ],
    )
    assert "fused manufacture group: keep-in = the members' envelopes" in resp.body
    ref = store.get_ref(kind="se", id="mf-simp")
    assert ref is not None
    params = next(p for p in _job_params(store, ref.id) if p["block"] == "ell")
    outcome = simp_bridge.run_simp(
        store, ref.id, simp_bridge.SimpRequest.from_params(params)
    )
    assert "union of 2 member envelope(s)" in outcome.summary["domain"]
    # the L's box is 30 x 10 x 20 mm at 2 mm: 15 x 5 x 10 elements, of which
    # the L itself (foot 20x10x10 + post 10x10x20, touching at x = 10) is
    # active — the empty corner above the foot is not
    assert outcome.summary["grid"] == [15, 5, 10]
    assert outcome.summary["active_elements"] == 250 + 250
    tree = _load(handler, "mf-simp")
    assert tree.blocks["ell"].bound_kind == "cad"
    assert tree.blocks["ell"].bound == outcome.cad_slug

    # a DOF joint inside the group: one solve is one body — refused
    _realized_hinge(handler, "mf-simp-dof")
    handler.edit(
        id="mf-simp-dof",
        ops=[
            {
                "op": "set_load",
                "block": "hinge",
                "force": [0.0, 0.0, -10.0],
                "fixed": True,
            }
        ],
    )
    with pytest.raises(BadInput, match="one SIMP solve is one body"):
        handler.edit(
            id="mf-simp-dof",
            ops=[
                {
                    "op": "realize",
                    "block": "hinge",
                    "mode": "fdm/pla",
                    "strategy": "simp",
                    "pitch": 0.002,
                    "volfrac": 0.5,
                    "load_at": "z+",
                    "fixed_at": "x-",
                }
            ],
        )


# ---------------------------------------------------------------------------
# the mixed root (2026-09-19): analytic members, per-member field leaves,
# analytic vs field-backed cavities
# ---------------------------------------------------------------------------


def _root_spec(h: SeHandler, cad_slug: str) -> SceneSpec:
    ref = h.store.get_ref(kind="cad", id=cad_slug)
    assert ref is not None
    spec, _handles = h.store.cad_load(ref.id)
    return spec


def test_analytic_member_keeps_its_compensated_hole_in_root_and_mesh(
    handler: SeHandler, hub: Hub, tmp_path: Path
) -> None:
    """A rigid printed member with a compensated fastener hole enters the
    root as its own node tree — the hole node itself, no ``field:`` leaf —
    and the exported mesh's hole wall sits on the compensated analytic
    radius to well under a quarter pitch (the vertices interpolate the
    exact SDF; nothing was re-sampled)."""
    from precis.cad.dsl import parse
    from precis_se import fasten as se_fasten
    from precis_se.printsolid import printed_solid

    screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
    handler.put(id="mf-analytic", text=json.dumps({"ops": _clamp_ops(screw_slug)}))
    handler.edit(
        id="mf-analytic", ops=[{"op": "realize", "block": "clamp", "mode": "fdm/asa"}]
    )
    tree = _load(handler, "mf-analytic")
    holes = [h for h in se_fasten.features_for(tree, "clamp") if "clearance" in h.kind]
    assert holes, [h.kind for h in se_fasten.features_for(tree, "clamp")]
    hole = holes[0]
    assert hole.source and "compensation" in hole.source
    solid = printed_solid(tree, "clamp", cad_store_reader=handler.store)
    assert solid is not None and solid.features
    hole_node = next(
        n
        for n in solid.spec.nodes
        if n.op == "cut"
        and parse(n.config).alias == "cyl"
        and parse(n.config).params["r"] == pytest.approx(hole.diameter_m / 2)
    )
    r_hole = hole.diameter_m / 2

    resp = handler.edit(id="mf-analytic", ops=[_mfg("assy", fit=0.0)])
    assert "clamp analytic" in resp.body and "bolt analytic" in resp.body
    last = _last(handler, "mf-analytic")
    forms = {p["block"]: p["form"] for p in last["parts"]}
    assert forms == {"clamp": "analytic", "bolt": "analytic"}
    assert last["field_cells"] == 0
    assert last["cells"] == last["export"]["cells"]
    spec = _root_spec(handler, last["cad"])
    names = {n.name: n for n in spec.nodes}
    assert f"clamp.{hole_node.name}" in names
    placed = names[f"clamp.{hole_node.name}"]
    assert placed.op == "cut" and placed.config == hole_node.config
    assert not any(n.config.startswith("field:") for n in spec.nodes)
    # the cavity at fit=0: the csk stand-in's own nodes as cuts
    bolt_nodes = [n for n in spec.nodes if n.name.startswith("bolt.")]
    assert {n.name for n in bolt_nodes} == {"bolt.shank", "bolt.head"}
    assert all(n.op == "cut" for n in bolt_nodes)
    assert spec.meta["se_manufacture"]["root"] == "assy"

    body = handler.get(id="mf-analytic", view="print", args={"block": "assy"}).body
    assert "- clamp: analytic" in body and "- cavity bolt: analytic" in body
    assert "re-sampled" not in body

    out = tmp_path / "analytic.3mf"
    handler.get(
        id="mf-analytic",
        view="print",
        args={"block": "assy", "fmt": "3mf", "path": str(out)},
    )
    (clamp,) = _3mf_objects(out).values()
    # the file is laid on the bed (down = -z pinned: no rotation, one z
    # offset putting the clamp's bottom, world z = 8 mm, at z = 0)
    assert clamp[:, 0].min() == pytest.approx(-15.0, abs=0.01)
    dz = clamp[:, 2].min() - 8.0
    # the hole wall in the middle of the hole's own run, about its axis
    z0 = hole.origin[2] * 1000.0 + dz
    z1 = z0 + hole.depth_m * hole.axis[2] * 1000.0
    lo_z, hi_z = sorted((z0, z1))
    band = (clamp[:, 2] > lo_z + 0.4 * (hi_z - lo_z)) & (
        clamp[:, 2] < lo_z + 0.6 * (hi_z - lo_z)
    )
    axis_xy = np.array([hole.origin[0], hole.origin[1]]) * 1000.0
    radial = np.linalg.norm(clamp[band][:, :2] - axis_xy[None, :], axis=1)
    ring = radial[radial < r_hole * 1000.0 + 1.0]
    assert len(ring) > 20
    assert np.abs(ring - r_hole * 1000.0).max() < _PITCH * 1000.0 / 4


def test_eroded_member_is_one_field_leaf_sized_to_its_own_box(
    handler: SeHandler,
) -> None:
    _realized_hinge(handler, "mf-leaf")
    handler.edit(id="mf-leaf", ops=[_mfg(gap=_GAP)])
    last = _last(handler, "mf-leaf")
    parts = {p["block"]: p for p in last["parts"]}
    assert parts["knuckle"]["form"] == "field (gap)"
    assert parts["pin"]["form"] == "field (gap)"
    spec = _root_spec(handler, last["cad"])
    for m in ("knuckle", "pin"):
        (node,) = [n for n in spec.nodes if n.name.startswith(f"{m}.")]
        assert node.name == f"{m}.eroded" and node.op == "add"
        assert node.config == f"field:{parts[m]['sha']}"
        assert node.component == m
    # each leaf's grid is its member's box plus the carve radius + margin,
    # on the group pitch — not the group's box
    export = last["export"]
    group_span = (np.array(export["shape"]) - 1) * _PITCH
    margin = 0.5 * _GAP + 0.5 * _PITCH + 2 * _PITCH
    spans: dict[str, np.ndarray] = {}
    for m, expect in (("pin", (0.03, 0.008, 0.008)), ("knuckle", (0.02, 0.02, 0.012))):
        header, fld = handler.store.get_field(parts[m]["sha"])
        assert header["provenance"] == {
            "source": "se_manufacture",
            "root": "hinge",
            "block": m,
            "form": "field (gap)",
        }
        assert fld.pitch == _PITCH
        span = (np.array(fld.shape) - 1) * fld.pitch
        spans[m] = span
        assert np.all(span >= np.array(expect) + 2 * margin - 1e-9)
        assert np.all(span <= np.array(expect) + 2 * margin + 2 * _PITCH + 1e-9)
        # the leaf's origin sits on the export lattice
        k = (np.asarray(fld.origin) - np.asarray(export["origin"])) / _PITCH
        assert np.allclose(k, np.rint(k), atol=1e-6)
    assert spans["pin"][1] < group_span[1] - 0.005  # the pin's y-extent, not the box's
    assert last["cells"] == export["cells"] + last["field_cells"]
    assert last["field_cells"] == parts["pin"]["cells"] + parts["knuckle"]["cells"]
    body = handler.get(id="mf-leaf", view="print", args={"block": "hinge"}).body
    assert "- knuckle: field (gap)" in body and "- pin: field (gap)" in body


def test_cavity_is_field_backed_with_fit_and_analytic_without(
    handler: SeHandler, hub: Hub
) -> None:
    screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
    handler.put(id="mf-cavform", text=json.dumps({"ops": _clamp_ops(screw_slug)}))
    handler.edit(
        id="mf-cavform", ops=[{"op": "realize", "block": "clamp", "mode": "fdm/asa"}]
    )
    fit = 0.0005
    resp = handler.edit(id="mf-cavform", ops=[_mfg("assy", fit=fit)])
    assert "bolt field (cavity fit)" in resp.body
    last = _last(handler, "mf-cavform")
    (cav,) = last["cavities"]
    assert cav["form"] == "field (cavity fit)" and cav["sha"]
    assert cav["components"] == ["clamp.part"]
    spec = _root_spec(handler, last["cad"])
    (node,) = [n for n in spec.nodes if n.name.startswith("bolt.")]
    assert node.name == "bolt.cavity" and node.op == "cut"
    assert node.config == f"field:{cav['sha']}" and node.component == "clamp.part"
    header, fld = handler.store.get_field(cav["sha"])
    assert header["provenance"]["form"] == "field (cavity fit)"
    # the leaf covers the stand-in plus the fit, not the clamp
    span = (np.array(fld.shape) - 1) * fld.pitch
    assert span[0] < 0.02  # the clamp is 30 mm wide
    body = handler.get(id="mf-cavform", view="print", args={"block": "assy"}).body
    assert "- cavity bolt: field (cavity fit)" in body
    assert "- clamp: analytic" in body

    # fit == 0: the same cavity is the analytic stand-in, cut node by node;
    # the re-run mints the -mfg-2 sibling
    resp = handler.edit(id="mf-cavform", ops=[_mfg("assy", fit=0.0)])
    assert "bolt analytic" in resp.body
    again = _last(handler, "mf-cavform")
    assert again["cad"] == "mf-cavform-assy-mfg-2"
    assert again["previous_cad"] == "mf-cavform-assy-mfg"
    (cav2,) = again["cavities"]
    assert cav2["form"] == "analytic" and cav2["sha"] is None
    spec2 = _root_spec(handler, again["cad"])
    bolt_nodes = [n for n in spec2.nodes if n.name.startswith("bolt.")]
    assert [n.op for n in bolt_nodes] == ["cut", "cut"]
    assert not any(n.config.startswith("field:") for n in spec2.nodes)
    assert _load(handler, "mf-cavform").blocks["assy"].bound == "mf-cavform-assy-mfg-2"


# ---------------------------------------------------------------------------
# reviewer round: per-object exact folds — the gap regime, the generic cad
# export of the -mfg design, blend_chain, the cavity analytic rule
# ---------------------------------------------------------------------------


def _min_dist(a: np.ndarray, b: np.ndarray) -> float:
    best = math.inf
    for start in range(0, len(a), 512):
        d = np.linalg.norm(a[start : start + 512, None, :] - b[None, :, :], axis=2)
        best = min(best, float(d.min()))
    return best


@pytest.mark.parametrize("gap_pitches", [2.0, 1.5])
def test_gap_regime_survives_the_3mf_path(
    handler: SeHandler, tmp_path: Path, gap_pitches: float
) -> None:
    """A DOF pair at gap = 2 and 1.5 pitches through the real
    ``view='print', fmt='3mf'`` path. Each object is the exact fold of its
    own parts, so the bore wall and the pin surface are the two eroded
    leaves' own zero sets: the pin's in-bore radius lies in the band the
    carve guarantees, ``[R − gap/2 − pitch, R − gap/2]`` (the carve is
    ``gap/2 + pitch/2`` off the partner's binarised surface, which sits
    within ``pitch/2`` of the true one), the bore's in
    ``[R + gap/2, R + gap/2 + pitch]``, and the vertex-to-vertex floor is
    at least ``gap − pitch/2`` (the radial bands give ``>= gap`` for the
    leaf zero sets themselves; the half pitch is the trilinear slack
    between a leaf's zero set and the marched vertices, observed far
    smaller)."""
    slug = f"mf-regime-{int(gap_pitches * 10)}"
    gap = gap_pitches * _PITCH
    _realized_hinge(handler, slug)
    resp = handler.edit(id=slug, ops=[_mfg(gap=gap)])
    assert "2 object(s): knuckle, pin" in resp.body
    (gap_row,) = _last(handler, slug)["gaps"]
    assert gap_row["measured_m"] >= gap - 1e-9
    out = tmp_path / f"{slug}.3mf"
    handler.get(
        id=slug, view="print", args={"block": "hinge", "fmt": "3mf", "path": str(out)}
    )
    objects = _3mf_objects(out)
    assert set(objects) == {"knuckle", "pin"}
    pin, knuckle = objects["pin"], objects["knuckle"]
    p_mm, gap_mm, r_mm = _PITCH * 1000.0, gap * 1000.0, _PIN_R * 1000.0
    axis_yz = pin[:, 1:].mean(axis=0)
    kx_lo, kx_hi = knuckle[:, 0].min(), knuckle[:, 0].max()
    in_bore = (pin[:, 0] > kx_lo + 2.0) & (pin[:, 0] < kx_hi - 2.0)
    pin_r = _radial(pin[in_bore], axis_yz)
    assert in_bore.sum() > 100
    assert pin_r.max() <= r_mm - gap_mm / 2 + 1e-6
    assert pin_r.min() >= r_mm - gap_mm / 2 - p_mm - 1e-6
    inside_x = (knuckle[:, 0] > kx_lo + 1.0) & (knuckle[:, 0] < kx_hi - 1.0)
    near = knuckle[inside_x]
    near = near[_radial(near, axis_yz) < 5.5]  # the bore's wall, not the faces
    bore_r = _radial(near, axis_yz)
    wall = bore_r < r_mm + gap_mm / 2 + 1.5 * p_mm  # the carved wall itself
    assert wall.sum() > 100
    assert bore_r.min() >= r_mm + gap_mm / 2 - 1e-6
    assert bore_r[wall].max() <= r_mm + gap_mm / 2 + p_mm + 1e-6
    # the floor between the two exported meshes, every pin vertex against
    # every knuckle vertex anywhere near the bore
    floor = _min_dist(pin, near)
    assert floor >= gap_mm - p_mm / 2 - 1e-6, (floor, gap_mm)
    assert gap_row["measured_m"] * 1000.0 <= floor + 1e-6


def test_generic_cad_export_of_the_mfg_design_matches_view_print(
    handler: SeHandler, tmp_path: Path
) -> None:
    """The object split is a property of the design (``meta.export_objects``
    + the lattice): cad's own 3MF export of the ``-mfg`` design writes the
    same objects, with the same vertices, as ``view='print'``; STL of a
    two-object design is refused, naming 3MF."""
    from precis.cad.export import EXPORT_LATTICE_KEY, EXPORT_OBJECTS_KEY, ExportError
    from precis.cad.export import export_mesh as cad_export_mesh

    _realized_hinge(handler, "mf-generic")
    handler.edit(id="mf-generic", ops=[_mfg(gap=_GAP)])
    last = _last(handler, "mf-generic")
    spec = _root_spec(handler, last["cad"])
    assert spec.meta[EXPORT_OBJECTS_KEY] == {"knuckle": ["knuckle"], "pin": ["pin"]}
    assert spec.meta[EXPORT_LATTICE_KEY]["pitch"] == _PITCH
    assert spec.meta[EXPORT_LATTICE_KEY]["origin"] == last["export"]["origin"]
    generic = tmp_path / "generic.3mf"
    cad_export_mesh(spec, generic)  # pitch: the design's own lattice pitch
    g_objects = _3mf_objects(generic)
    printed = tmp_path / "print.3mf"
    handler.get(
        id="mf-generic",
        view="print",
        args={"block": "hinge", "fmt": "3mf", "path": str(printed)},
    )
    p_objects = _3mf_objects(printed)
    assert set(g_objects) == set(p_objects) == {"knuckle", "pin"}
    # view='print' lays the group on the bed (one shared z offset, root
    # pose identity, down pinned -z): the same vertex sets, shifted
    shift = np.vstack(list(p_objects.values())).min(axis=0) - np.vstack(
        list(g_objects.values())
    ).min(axis=0)
    assert shift[0] == pytest.approx(0.0, abs=1e-6)
    assert shift[1] == pytest.approx(0.0, abs=1e-6)
    for name in ("knuckle", "pin"):
        g, p = g_objects[name], p_objects[name] - shift
        assert g.shape == p.shape, (name, g.shape, p.shape)
        assert _min_dist(g[::13], p) < 1e-6 and _min_dist(p[::13], g) < 1e-6
        assert np.allclose(np.sort(g, axis=0), np.sort(p, axis=0), atol=1e-6)
    with pytest.raises(ExportError, match="3MF"):
        cad_export_mesh(spec, tmp_path / "generic.stl")


def _tri_ops() -> list[dict[str, Any]]:
    """The L plus a ``cap`` (10×10×5) rigidly on top of the post."""
    ops = _ell_ops()
    ops[-2:-2] = [
        {
            "op": "add_block",
            "name": "cap",
            "parent": "ell",
            "envelope": "box:w0.01d0.01h0.005",
            "pose": [0.015, 0, 0.02],
        },
        {"op": "set_mode", "block": "cap", "mode": "fdm/pla"},
        {"op": "add_port", "block": "post", "name": "top"},
        {"op": "add_port", "block": "cap", "name": "bottom"},
        {
            "op": "connect",
            "a": "post.top",
            "b": "cap.bottom",
            "joint": {"class": "rigid"},
        },
    ]
    return ops


def test_rigid_three_member_root_exports_one_object_from_cad_too(
    handler: SeHandler, tmp_path: Path
) -> None:
    from precis.cad.export import EXPORT_OBJECTS_KEY
    from precis.cad.export import export_mesh as cad_export_mesh

    handler.put(id="mf-tri", text=json.dumps({"ops": _tri_ops()}))
    for block in ("foot", "post", "cap"):
        handler.edit(
            id="mf-tri", ops=[{"op": "realize", "block": block, "mode": "fdm/pla"}]
        )
    handler.edit(id="mf-tri", ops=[_mfg("ell")])
    last = _last(handler, "mf-tri")
    assert last["fused_components"] == [["cap", "foot", "post"]]
    (obj,) = last["objects"]
    assert obj["members"] == ["cap", "foot", "post"]
    assert obj["components"] == ["cap.part", "foot.part", "post.part"]
    spec = _root_spec(handler, last["cad"])
    # three cad components (blend 0 keeps every member its own) …
    assert spec.components == ["cap.part", "foot.part", "post.part"]
    assert spec.meta[EXPORT_OBJECTS_KEY] == {
        "cap+foot+post": ["cap.part", "foot.part", "post.part"]
    }
    # … and ONE printed object, from cad's generic export as from se's
    out = tmp_path / "tri.3mf"
    cad_export_mesh(spec, out)
    assert set(_3mf_objects(out)) == {"cap+foot+post"}
    se_out = tmp_path / "tri-se.3mf"
    handler.get(
        id="mf-tri",
        view="print",
        args={"block": "ell", "fmt": "3mf", "path": str(se_out)},
    )
    assert set(_3mf_objects(se_out)) == {"cap+foot+post"}
    # STL of a one-object design is that object
    cad_export_mesh(spec, tmp_path / "tri.stl")
    assert (tmp_path / "tri.stl").stat().st_size > 84


def test_blend_chain_finding_only_when_a_later_member_cuts(
    handler: SeHandler,
) -> None:
    # two plain boxes: chained, nothing leaks, no finding
    _realized_ell(handler, "mf-chain-plain")
    handler.edit(id="mf-chain-plain", ops=[_mfg("ell", blend=0.003)])
    last = _last(handler, "mf-chain-plain")
    assert last["components"] == ["foot+post"]
    assert not any(f["rule"] == "blend_chain" for f in last["findings"])
    # the post carries a bore: its cut reaches the foot along the chain
    _realized_ell(handler, "mf-chain-cut")
    _cut_bore(handler, "mf-chain-cut", block="post")
    handler.edit(id="mf-chain-cut", ops=[_mfg("ell", blend=0.003)])
    last = _last(handler, "mf-chain-cut")
    (f,) = [f for f in last["findings"] if f["rule"] == "blend_chain"]
    assert f["subject"] == "foot+post" and f["severity"] == "info"
    assert "cut/intersect nodes of post" in f["detail"]
    spec = _root_spec(handler, last["cad"])
    names = [n.name for n in spec.nodes]
    assert names == ["foot.body", "post.body", "post.bore"]
    assert spec.nodes[1].blend == 0.003 and spec.nodes[2].op == "cut"


def test_cavity_analytic_rule_requires_plain_add_nodes() -> None:
    plain = [
        NodeSpec(
            name="b.shank", op="add", config="cyl:r0.002h0.012", component="b.body"
        ),
        NodeSpec(
            name="b.head", op="add", config="cone:r0.004h0.002", component="b.body"
        ),
    ]
    assert se_manufacture._cavity_is_analytic(plain, 0.0)
    assert not se_manufacture._cavity_is_analytic(plain, 0.0005)
    blended = [plain[0], replace(plain[1], blend=0.001)]
    assert not se_manufacture._cavity_is_analytic(blended, 0.0)
    bored = [plain[0], replace(plain[1], op="cut")]
    assert not se_manufacture._cavity_is_analytic(bored, 0.0)
