"""Independent geometry oracles for offset drills, slot axes and ring signs."""

from __future__ import annotations

import dataclasses
import math
from types import SimpleNamespace

import numpy as np
import pytest
from shapely.geometry import Point, box

from precis.pcb import (
    DEFAULT_STACKUP,
    drc,
    epro,
    epro_verify,
    epro_write,
    padplace,
    realize,
)
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import FootprintPad, from_graph
from precis.pcb.session import apply_real_pin_offsets
from precis.pcb.topoplace import realise
from precis.pcb.topoplace import wires as wires_mod
from precis.pcb.topoplace.mesh import build_mesh
from tests.test_pcb_topoplace_round11_copper import _joined
from tests.test_pcb_topoplace_walls import _board


def _world(x, y, inst_angle, bottom, *, pad_angle=0.0, origin=(0.0, 0.0)):
    # Independent R(instance) M R(pad), with clockwise angles in +Y-up mm.
    a = math.radians(pad_angle)
    x, y = x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a)
    x, y = x + origin[0], y + origin[1]
    x = -x if bottom else x
    a = math.radians(inst_angle)
    return 10.0 + x * math.cos(a) + y * math.sin(a), 10.0 - x * math.sin(
        a
    ) + y * math.cos(a)


@pytest.mark.parametrize("quarter", [0, 1, 2, 3])
@pytest.mark.parametrize("bottom", [False, True])
@pytest.mark.parametrize("slot_axis", [None, "x", "y"])
@pytest.mark.parametrize("extra", [False, True])
def test_router_uses_real_offset_hole_and_slot_tips(quarter, bottom, slot_axis, extra):
    origin = (0.0, 5.0) if extra else (0.0, 0.0)
    raw = dict(
        x=origin[0],
        y=origin[1],
        w=2.0,
        h=2.0,
        shape="rect",
        rot=37.0,
        drill=0.6,
        drill_offset={"x": 0.5, "y": 0.0},
    )
    if slot_axis:
        raw["slot"] = {
            "w": 1.2 if slot_axis == "x" else 0.6,
            "h": 1.2 if slot_axis == "y" else 0.6,
        }
    d = _board(
        [dict(x=10.0, y=10.0, net=0, w=2.0, h=2.0, side=2)],
        n_nets=1,
        extras=[dict(part=0, dx=0.0, dy=5.0, w=2.0, h=2.0, pin=0, side=2)]
        if extra
        else [],
    )
    ir = SimpleNamespace(
        net_name=("N0",),
        footprint_pads=(
            FootprintPad(
                instance=0, number="1", label="1", pin=0, primary=not extra, raw=raw
            ),
        ),
    )
    pose = np.asarray([[10.0, 10.0, quarter, bottom]], dtype=float)
    pads = realise._pad_bodies(d, ir, 4, pose)
    body = pads[1][-1][1]
    assert isinstance(body, realise._PadBody) and body.hole is not None
    expected_centre = _world(
        0.5, 0.0, quarter * 90, bottom, pad_angle=37.0, origin=origin
    )
    assert (body.hole.geom.centroid.x, body.hole.geom.centroid.y) == pytest.approx(
        expected_centre, abs=0.00005
    )
    sites = [(0.5, 0.0)]
    if slot_axis:
        ends = (
            [(0.2, 0.0), (0.8, 0.0)] if slot_axis == "x" else [(0.5, -0.3), (0.5, 0.3)]
        )
        expected = [
            _world(x, y, quarter * 90, bottom, pad_angle=37.0, origin=origin)
            for x, y in ends
        ]
        assert np.asarray(body.hole.geom.coords) == pytest.approx(
            np.asarray(expected), abs=0.00005
        )
        # A probe within the round cap beyond its end centre is still void.
        sites += [(0.95, 0.0)] if slot_axis == "x" else [(0.5, 0.45)]
    for x, y in sites:
        wx, wy = _world(x, y, quarter * 90, bottom, pad_angle=37.0, origin=origin)
        assert not _joined(
            box(wx - 0.025, wy - 0.025, wx + 0.025, wy + 0.025), pads=pads
        )


def _placed(raw, angle, bottom, ir_path):
    inst = dict(
        refdes="J1", x=10.0, y=10.0, rot=angle, layer="bottom" if bottom else "top"
    )
    if not ir_path:
        pads, _ = padplace.place_footprint_pads([raw], inst, layers=["F.Cu", "B.Cu"])
        return pads[0], inst
    graph = dict(
        instances=[inst, dict(refdes="U2", x=15.0, y=15.0)],
        nets=[
            dict(
                name="N",
                members=[dict(refdes="J1", pin="1"), dict(refdes="U2", pin="1")],
            )
        ],
    )
    footprints = {"J1": dict(pads=[raw], pin_map={"1": {"name": "1"}})}
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    apply_real_pin_offsets(ir, footprints)
    return next(
        p
        for p in realize.pads_for_ir(ir, ["F.Cu", "B.Cu"], footprints)
        if p["refdes"] == "J1"
    ), inst


@pytest.mark.parametrize("shape", ["circle", "polygon", "rect", "obround"])
@pytest.mark.parametrize("angle", [0.0, 23.7, 90.0, 270.0])
@pytest.mark.parametrize("bottom", [False, True])
@pytest.mark.parametrize("ir_path", [False, True])
def test_export_keeps_authored_slot_axis_on_every_land(shape, angle, bottom, ir_path):
    raw = dict(
        number="1",
        x=0.0,
        y=0.0,
        shape=shape,
        w=2.0,
        h=2.0,
        rot=37.0,
        drill=0.6,
        slot={"w": 1.2, "h": 0.6},
        drill_offset={"x": 0.25, "y": -0.125},
    )
    if shape == "polygon":
        raw["poly"] = [[-1, -0.7], [0.8, -0.7], [1, 0.0], [0.8, 0.7], [-1, 0.7]]
    pad, inst = _placed(raw, angle, bottom, ir_path)
    local = epro_write._local_pad(pad, inst)
    assert not local.slot_rounded
    assert local.slot == pytest.approx((1.2, 0.6), abs=0.0001)
    body = epro_write._pad_body(local, 0)
    assert body["hole"]["holeType"] == "SLOT"
    assert body["padAngle"] == pytest.approx(37.0)
    assert local.drill_offset == pytest.approx((0.25, -0.125), abs=0.0001)
    # Reconstruct the exported native slot without either placement helper.
    half = (body["hole"]["width"] - body["hole"]["height"]) * 0.0254 / 2
    got = [
        _world(
            body["padOffsetX"] * 0.0254 + sign * half,
            -body["padOffsetY"] * 0.0254,
            angle,
            bottom,
            pad_angle=body["padAngle"],
        )
        for sign in (-1, 1)
    ]
    want = [_world(x, -0.125, angle, bottom, pad_angle=37.0) for x in (-0.05, 0.55)]
    assert np.asarray(got) == pytest.approx(np.asarray(want), abs=0.0001)
    model = dict(
        layers=["F.Cu", "B.Cu"],
        outline=[[0, 0], [20, 0], [20, 20], [0, 20]],
        pads=[{**pad, "net": ""}],
        drills=padplace.drills_for_pads([pad]),
        copper=[],
        instances=[inst],
        silkscreen={"top": [], "bottom": []},
    )
    archive = epro_write.epro_files(model, slug="slot-axis")
    assert not archive.warnings
    blob = epro_write.zip_epro(archive.files)
    verdict = epro_verify.verify_export(model, blob, stats=archive.stats)
    assert not verdict.fails, verdict.render()
    design, _ = epro.build_design(epro.read_archive(blob))
    component = design.components[0]
    footprint = next(
        f for f in design.footprints if f["name"] == component["footprint"]
    )
    _, holes = padplace.place_footprint_pads(
        footprint["pads"], component, layers=["F.Cu", "B.Cu"]
    )
    assert len(holes) == 1
    assert np.asarray(holes[0]["slot"]) == pytest.approx(np.asarray(want), abs=0.0002)


@pytest.mark.parametrize("shape", ["rect", "obround"])
@pytest.mark.parametrize("angle", [0.0, 23.7, 90.0])
@pytest.mark.parametrize("bottom", [False, True])
@pytest.mark.parametrize("offset", [(0.7483, 0.0146), (0.752, 0.0146), (0.0, 0.451)])
def test_ir_round_ring_uses_native_analytic_land(shape, angle, bottom, offset):
    raw = dict(
        number="1",
        x=0.0,
        y=0.0,
        w=2.4,
        h=1.2,
        shape=shape,
        rot=37.0,
        drill=0.6,
        drill_offset={"x": offset[0], "y": offset[1]},
    )
    pad, _ = _placed(raw, angle, bottom, True)
    assert pad["shape"] == shape
    # Compute in authored axes from the independently transformed real hole.
    world_angle = math.radians(angle - 37.0 if bottom else angle + 37.0)
    hx, hy = pad["drill_x"] - pad["x"], pad["drill_y"] - pad["y"]
    dx = hx * math.cos(world_angle) - hy * math.sin(world_angle)
    dy = hx * math.sin(world_angle) + hy * math.cos(world_angle)
    if shape == "obround":
        expected = 0.6 - math.hypot(max(abs(dx) - 0.6, 0.0), dy) - 0.3
    else:
        expected = min(1.2 - abs(dx), 0.6 - abs(dy)) - 0.3
    assert drc._round_pad_ring_mm(pad, 0.6) == pytest.approx(expected, abs=1e-10)
    cap = capability_for("4layer")
    floor = cap.jlc_min["annular_ring_mm"]
    assert floor is not None
    findings = drc.check_annular_ring({"pads": [pad]}, cap)
    assert bool([f for f in findings if f.severity == "error"]) == (expected < floor)


@pytest.mark.parametrize("shape", ["rect", "polygon", "obround", "circle"])
@pytest.mark.parametrize("angle", [0.0, 37.0, 90.0])
def test_detached_slot_has_signed_lateral_ring(shape, angle):
    raw = dict(
        number="1",
        x=0.0,
        y=0.0,
        w=2.0,
        h=2.0,
        shape=shape,
        rot=angle,
        drill=0.6,
        slot={"w": 1.2, "h": 0.6},
        drill_offset={"x": 0.0, "y": 1.6},
    )
    if shape == "polygon":
        # This land is already in footprint coordinates; rotate its vertices.
        a = math.radians(angle)
        raw["poly"] = [
            [x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a)]
            for x, y in [(-1, -1), (1, -1), (1, 1), (-1, 1)]
        ]
    pad, _ = _placed(raw, 0.0, False, False)
    assert drc._slot_lateral_ring_mm(pad, pad["slot"], 0.6) < 0.0
    assert any(
        f.severity == "error"
        for f in drc.check_annular_ring({"pads": [pad]}, capability_for("4layer"))
    )


@pytest.mark.parametrize("angle", [0.0, 37.0, 90.0])
def test_open_slot_ends_keep_a_valid_lateral_ring(angle):
    raw = dict(
        number="1",
        x=0.0,
        y=0.0,
        w=1.6,
        h=2.0,
        shape="rect",
        rot=angle,
        drill=0.6,
        slot={"w": 2.0, "h": 0.6},
    )
    pad, _ = _placed(raw, 0.0, False, False)
    assert drc._slot_lateral_ring_mm(pad, pad["slot"], 0.6) == pytest.approx(
        0.7, abs=0.0001
    )


@pytest.mark.parametrize("quarter", [0, 1, 2, 3])
@pytest.mark.parametrize("bottom", [False, True])
@pytest.mark.parametrize("extra", [False, True])
@pytest.mark.parametrize("consumer", ["site", "guard"])
def test_via_and_escape_siting_clear_the_actual_offset_slot_tip(
    quarter, bottom, extra, consumer
):
    origin = (0.0, 5.0) if extra else (0.0, 0.0)
    raw = dict(
        x=origin[0],
        y=origin[1],
        w=1.6,
        h=1.6,
        shape="rect",
        drill=0.6,
        drill_offset={"x": 0.5, "y": 0.0},
        slot={"w": 1.6, "h": 0.6},
    )
    d = _board(
        [dict(x=10.0, y=10.0, net=0, w=1.6, h=1.6, side=2)],
        n_nets=1,
        extras=[dict(part=0, dx=0.0, dy=5.0, w=1.6, h=1.6, pin=0, side=2)]
        if extra
        else [],
    )
    pose = np.asarray([[10.0, 10.0, quarter, bottom]], dtype=float)
    d = dataclasses.replace(
        d,
        pose0=pose,
        via_mask_web_mm=0.2,
        footprint_pads=(
            FootprintPad(
                instance=0, number="1", label="1", pin=0, primary=not extra, raw=raw
            ),
        ),
    )
    ir = SimpleNamespace(net_name=("N0",))
    mesh = build_mesh(d, pose)
    ctx = wires_mod._make_ctx(mesh, d, 4, pose)
    planter = wires_mod._ViaPlanter(ctx, d, pose)
    # Native slot end centre = 0.5 + (1.6 - 0.6)/2 = 1.0.
    # At 1.55: copper gap is 0.75 (clear); drill-centre gap is 0.55,
    # less than 0.3 + 0.1 + 0.2 fab wall, so a new drill cannot fit.
    x, y = _world(1.55, 0.0, quarter * 90, bottom, origin=origin)
    assert not planter.pad_ix.blocked(Point(x, y), 0.2, 0.2, net=1)
    if consumer == "site":
        assert planter.hole_ix.blocked(Point(x, y), 0.1, 0.0, net=0)
        return
    plan = realise.RoutePlan(
        pieces=(), vias=(realise.ViaSite(0, 0, 0, 0.5, x, y, 0.4, 0.2),), layers=(0,)
    )
    assert realise._guarded_branches(plan, d, ir, 4, pose, {}) == {0: {0}}


@pytest.mark.parametrize(
    "x_offset,drill_mm,blocked",
    [(1.55, 0.3, True), (1.7, 0.3, False), (1.7, 0.5, True)],
)
def test_physical_drill_spacing_uses_drill_radius_and_its_own_cache_key(
    monkeypatch, x_offset, drill_mm, blocked
):
    raw = dict(
        x=0.0,
        y=0.0,
        w=1.6,
        h=1.6,
        shape="rect",
        drill=0.6,
        drill_offset={"x": 0.5, "y": 0.0},
        slot={"w": 1.6, "h": 0.6},
    )
    d = _board([dict(x=10.0, y=10.0, net=0, w=1.6, h=1.6, side=2)], n_nets=1)
    d = dataclasses.replace(
        d,
        footprint_pads=(
            FootprintPad(
                instance=0, number="1", label="1", pin=0, primary=True, raw=raw
            ),
        ),
    )
    mesh = build_mesh(d, d.pose0)
    ctx = wires_mod._make_ctx(mesh, d, 4, d.pose0)
    planter = wires_mod._ViaPlanter(ctx, d)
    point = np.asarray([[10.0 + x_offset, 10.0]])
    # Isolate the physical-drill verdict from candidate sampling/electrical
    # clearance: both candidate sizes have already cleared the copper pass.
    monkeypatch.setattr(planter, "static_survivors", lambda *_: point)
    e = next(i for i, board in enumerate(ctx.board) if not board)
    expected_wall = x_offset - 1.0 - 0.3 - drill_mm / 2
    assert (expected_wall < 0.2) == blocked
    # First cache a smaller drill; the real drill must be re-evaluated.
    assert planter.point(e, 0.3, 0.9, 0, drill_mm=0.05) is not None
    got = planter.point(e, 0.3, 0.9, 0, drill_mm=drill_mm)
    assert (got is None) == blocked


@pytest.mark.parametrize("shape", ["rect", "obround", "circle", "polygon"])
def test_pour_extending_beyond_open_hole_does_not_join_only_through_void(shape):
    raw = dict(
        x=0.0,
        y=0.0,
        w=1.6,
        h=1.6,
        shape=shape,
        drill=0.6,
        drill_offset={"x": 0.5, "y": 0.0},
        slot={"w": 1.6, "h": 0.6},
    )
    if shape == "obround":
        raw["w"] = 2.0
    if shape == "polygon":
        raw["poly"] = [[-0.8, -0.8], [0.8, -0.8], [0.8, 0.8], [-0.8, 0.8]]
    d = _board([dict(x=10.0, y=10.0, net=0, w=1.6, h=1.6, side=2)], n_nets=1)
    d = dataclasses.replace(
        d,
        footprint_pads=(
            FootprintPad(
                instance=0, number="1", label="1", pin=0, primary=True, raw=raw
            ),
        ),
    )
    pads = realise._pad_bodies(d, SimpleNamespace(net_name=("N0",)), 4, d.pose0)
    # All possible overlap with these lands has x in [.7,1], |y|<=.025;
    # every such point is inside the slot (axis x in [0,1], radius .3).
    assert not _joined(box(10.7, 9.975, 11.5, 10.025), pads=pads)
    # Cross the actual side wall: this piece has real copper contact.
    assert _joined(box(10.2, 10.25, 11.5, 10.5), pads=pads)
