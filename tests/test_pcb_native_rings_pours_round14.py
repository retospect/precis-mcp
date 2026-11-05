"""Native placed lands retain pour clearance and exact slot-cap ring verdicts."""

from __future__ import annotations

import math
from typing import Any

import pytest
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from precis.pcb import drc, realize
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import from_graph
from precis.pcb.session import apply_real_pin_offsets
from tests.test_pcb_geometry_round12 import _placed


def _point(x: float, y: float, bottom: bool) -> tuple[float, float]:
    """Independent authored-pad-to-board transform; no production geometry."""
    angle = math.radians(37.0)
    rx = x * math.cos(angle) + y * math.sin(angle)
    ry = -x * math.sin(angle) + y * math.cos(angle)
    return 10.0 + (-rx if bottom else rx), 10.0 + ry


@pytest.mark.parametrize("bottom", [False, True], ids=["top", "bottom"])
@pytest.mark.parametrize("shape", ["rect", "obround"])
def test_ir_pour_preserves_native_rotated_land_clearance(shape: str, bottom: bool):
    raw = dict(number="1", x=0.0, y=0.0, shape=shape, w=2.4, h=1.2, rot=37.0)
    graph = dict(
        instances=[
            dict(refdes="J1", x=10.0, y=10.0, layer="bottom" if bottom else "top"),
            dict(refdes="U2", x=17.0, y=17.0),
            dict(refdes="G1", x=2.0, y=2.0),
            dict(refdes="G2", x=2.0, y=17.0),
        ],
        nets=[
            dict(
                name="N",
                members=[dict(refdes="J1", pin="1"), dict(refdes="U2", pin="1")],
            ),
            dict(
                name="GND",
                members=[dict(refdes="G1", pin="1"), dict(refdes="G2", pin="1")],
            ),
        ],
    )
    ir = from_graph(
        graph,
        stackup=[dict(name="F.Cu", role="signal"), dict(name="B.Cu", role="signal")],
        outline=[[0.0, 0.0], [20.0, 0.0], [20.0, 20.0], [0.0, 20.0]],
    )
    footprints = {"J1": dict(pads=[raw], pin_map={"1": {"name": "1"}})}
    apply_real_pin_offsets(ir, footprints)
    side = "B.Cu" if bottom else "F.Cu"
    ir.net_plane_layers[list(ir.net_name).index("GND")] = 1 << int(bottom)
    pours, _ = realize._pour_planes(ir, [], [], [], [], 0.15, 0.2, footprints)
    copper = unary_union(
        [
            Polygon(row["polygon"], row.get("holes") or [])
            for row in pours
            if row["layer"] == side
        ]
    )
    assert not copper.is_empty
    pad = next(
        p
        for p in realize.pads_for_ir(ir, ["F.Cu", "B.Cu"], footprints)
        if p["refdes"] == "J1"
    )
    assert pad["shape"] == shape and pad["layer"] == side
    if shape == "rect":
        land = Polygon(
            [
                _point(x, y, bottom)
                for x, y in [(-1.2, -0.6), (1.2, -0.6), (1.2, 0.6), (-1.2, 0.6)]
            ]
        )
        gap = land.distance(copper)
        assert land.intersection(copper).area < 1e-12
    else:
        # An analytic stadium is its centre segment offset by 0.6 mm.
        # Measuring core-to-pour avoids a tessellated oracle at its caps.
        core = LineString([_point(-0.6, 0.0, bottom), _point(0.6, 0.0, bottom)])
        gap = core.distance(copper) - 0.6
    assert gap >= 0.15 - 1e-8, f"{shape}/{side}: native land-to-pour gap {gap:.9f} mm"


def _slot_pad(offset: float, bottom: bool, portrait: bool = False) -> dict[str, Any]:
    raw = dict(
        number="1",
        x=0.0,
        y=0.0,
        shape="obround",
        w=1.2 if portrait else 2.4,
        h=2.4 if portrait else 1.2,
        rot=37.0,
        drill=0.6,
        slot=dict(w=0.6 if portrait else 1.2, h=1.2 if portrait else 0.6),
        drill_offset=dict(x=0.0 if portrait else offset, y=offset if portrait else 0.0),
    )
    pad, _ = _placed(raw, 0.0, bottom, True)
    return pad


def _aligned_slot_oracle(pad: dict[str, Any], bottom: bool, portrait: bool) -> float:
    """Closed-form cap cross sections at the actual rounded slot ends.

    These authored parallel slots end inside a cap, where the smallest lateral
    ring lies at an end-centre plane. Use the actual quantized slot axis to
    include its tiny angular error: intersect each such plane with the exact
    end circle and retain only the cap's outward half. This oracle neither
    tessellates nor reads any production land/rotation helper.
    """
    (ax, ay), (bx, by) = pad["slot"]
    span = math.hypot(bx - ax, by - ay)
    ux, uy = (bx - ax) / span, (by - ay) / span
    mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
    origin = _point(0.0, 0.0, bottom)
    values = []
    for sign in (-1.0, 1.0):
        cx, cy = _point(
            0.0 if portrait else sign * 0.6, sign * 0.6 if portrait else 0.0, bottom
        )
        ca = (cx - mx) * ux + (cy - my) * uy
        cv = -(cx - mx) * uy + (cy - my) * ux
        for a in (-span / 2.0, span / 2.0):
            radicand = 0.6**2 - (a - ca) ** 2
            if radicand < 0.0:
                continue
            for branch in (-1.0, 1.0):
                v = cv + branch * math.sqrt(radicand)
                x, y = mx + a * ux - v * uy, my + a * uy + v * ux
                if (x - cx) * (cx - origin[0]) + (y - cy) * (cy - origin[1]) >= 0.0:
                    values.append(abs(v) - 0.3)
    assert values
    return min(values)


@pytest.mark.parametrize("bottom", [False, True], ids=["top", "bottom"])
@pytest.mark.parametrize("portrait", [False, True], ids=["landscape", "portrait"])
@pytest.mark.parametrize(
    "offset,passes",
    [(0.75, False), (0.6964, True), (0.6973, False)],
    ids=["reported-cap", "near-floor-pass", "near-floor-fail"],
)
def test_slot_cap_ring_uses_native_stadium_boundary(
    offset: float, passes: bool, bottom: bool, portrait: bool
):
    pad = _slot_pad(offset, bottom, portrait)
    expected = _aligned_slot_oracle(pad, bottom, portrait)
    measured = drc._slot_lateral_ring_mm(pad, pad["slot"], 0.6)
    assert measured == pytest.approx(expected, abs=2e-8)
    assert (expected >= 0.15) is passes
    errors = [
        f
        for f in drc.check_annular_ring({"pads": [pad]}, capability_for("4layer"))
        if f.severity == "error"
    ]
    assert bool(errors) is not passes


@pytest.mark.parametrize("bottom", [False, True], ids=["top", "bottom"])
def test_slot_ring_retains_signed_outside_containment(bottom: bool):
    pad = _slot_pad(2.0, bottom)
    assert drc._slot_lateral_ring_mm(pad, pad["slot"], 0.6) == pytest.approx(-0.3)
    assert any(
        f.severity == "error"
        for f in drc.check_annular_ring({"pads": [pad]}, capability_for("4layer"))
    )


@pytest.mark.parametrize("bottom", [False, True], ids=["top", "bottom"])
def test_slot_ring_keeps_authored_open_ends(bottom: bool):
    raw = dict(
        number="1",
        x=0.0,
        y=0.0,
        shape="rect",
        w=1.1,
        h=1.6,
        rot=37.0,
        drill=0.6,
        slot=dict(w=0.6, h=1.6),
    )
    pad, _ = _placed(raw, 0.0, bottom, True)
    # The cutter reaches both short edges; only the lateral copper is judged.
    assert drc._slot_lateral_ring_mm(pad, pad["slot"], 0.6) == pytest.approx(
        0.25, abs=3e-5
    )
    assert not any(
        f.severity == "error"
        for f in drc.check_annular_ring({"pads": [pad]}, capability_for("4layer"))
    )


@pytest.mark.parametrize("bottom", [False, True], ids=["top", "bottom"])
@pytest.mark.parametrize(
    "offset", [0.7, 0.812, 0.813], ids=["cap", "near-floor-pass", "near-floor-fail"]
)
def test_circle_land_slot_reads_the_circle_arc(offset: float, bottom: bool):
    # Deliberately retain exact board coordinates so the elementary circular
    # cross section is an independent oracle at a rotated slot-end plane.
    slot = [_point(offset + sign * 0.3, 0.0, bottom) for sign in (-1.0, 1.0)]
    pad = dict(
        shape="circle",
        x=10.0,
        y=10.0,
        w=2.4,
        drill=0.6,
        slot=slot,
        layer="B.Cu" if bottom else "F.Cu",
    )
    expected = math.sqrt(1.2**2 - (offset + 0.3) ** 2) - 0.3
    assert drc._slot_lateral_ring_mm(pad, slot, 0.6) == pytest.approx(
        expected, abs=2e-12
    )
    errors = [
        f
        for f in drc.check_annular_ring({"pads": [pad]}, capability_for("4layer"))
        if f.severity == "error"
    ]
    assert bool(errors) is (expected < 0.15)


@pytest.mark.parametrize("bottom", [False, True], ids=["top", "bottom"])
@pytest.mark.parametrize(
    "offset", [0.9, 0.9155, 0.9159], ids=["cap", "near-floor-pass", "near-floor-fail"]
)
def test_perpendicular_slot_cap_ring_has_an_independent_cross_section(
    offset: float, bottom: bool
):
    # Slot and stadium major axes differ by 90 degrees. Its end-centre planes
    # cut only circular caps; the straight walls lie outside the slot span.
    slot = [_point(offset, sign * 0.2, bottom) for sign in (-1.0, 1.0)]
    pad = dict(
        shape="obround",
        x=10.0,
        y=10.0,
        w=2.4,
        h=1.2,
        oblique_world_rot=-37.0 if bottom else 37.0,
        drill=0.2,
        slot=slot,
        layer="B.Cu" if bottom else "F.Cu",
    )
    expected = 0.6 + math.sqrt(0.6**2 - 0.2**2) - offset - 0.1
    assert drc._slot_lateral_ring_mm(pad, slot, 0.2) == pytest.approx(
        expected, abs=2e-12
    )
    errors = [
        f
        for f in drc.check_annular_ring({"pads": [pad]}, capability_for("4layer"))
        if f.severity == "error"
    ]
    assert bool(errors) is (expected < 0.15)


@pytest.mark.parametrize("bottom", [False, True], ids=["top", "bottom"])
@pytest.mark.parametrize("shape", ["rect", "obround"])
@pytest.mark.parametrize("local_x", [1.25, 1.55], ids=["overlap", "clear-control"])
def test_naive_clearance_measures_rotated_native_land(
    shape: str, bottom: bool, local_x: float
):
    side = "B.Cu" if bottom else "F.Cu"
    x, y = _point(local_x, 0.0, bottom)
    model = dict(
        layers=["F.Cu", "B.Cu"],
        copper=[],
        pads=[
            dict(
                shape=shape,
                x=10.0,
                y=10.0,
                w=2.4,
                h=1.2,
                oblique_world_rot=-37.0 if bottom else 37.0,
                net="A",
                layer=side,
            ),
            dict(shape="circle", x=x, y=y, w=0.2, net="B", layer=side),
        ],
    )
    # On either native land's long axis the boundary is exactly 1.2 mm from
    # its centre. The foreign 0.1 mm radius disk therefore overlaps by 0.05
    # or clears by 0.25 mm, independent of either production distance engine.
    expected_gap = max(0.0, local_x - 1.2 - 0.1)
    required = 0.05
    expected_pairs = {(0, 1)} if expected_gap < required else set()
    naive = drc.clearance_violations_naive(model, required_mm=required)
    indexed = drc.clearance_pairs_indexed(model, required_mm=required)
    assert {(a, b) for a, b, _ in naive} == expected_pairs
    assert {(a, b) for a, b, _, _ in indexed} == expected_pairs
    for _, _, gap in naive:
        assert gap == pytest.approx(expected_gap, abs=2e-12)
    for _, _, gap, layer in indexed:
        assert gap == pytest.approx(expected_gap, abs=2e-12)
        assert layer == side
