"""A region joins physical land copper, never a drilled-away envelope."""

from __future__ import annotations

import math
from typing import Any

import pytest
from shapely.geometry import LineString, Polygon, box

from precis.pcb import connectivity, drc, padplace
from precis.pcb.topoplace import realise


def _slot_model(offset: bool, contact: bool) -> dict[str, Any]:
    raw: dict[str, Any] = dict(
        number="1",
        x=0.0,
        y=0.0,
        w=1.6,
        h=1.6,
        shape="rect",
        drill=0.6,
        slot=dict(w=1.2 if offset else 1.8, h=0.6),
    )
    if offset:
        raw["drill_offset"] = dict(x=0.4, y=0.0)
    pads, drills = padplace.place_footprint_pads(
        [raw],
        dict(x=10.0, y=10.0, refdes="J1"),
        pin_to_net={"1": "N"},
        layers=["F.Cu", "B.Cu"],
    )
    land = next(p for p in pads if p["layer"] == "F.Cu")
    y = 10.4 if contact else 10.0
    other = dict(
        shape="circle",
        x=11.4,
        y=y,
        w=0.2,
        net="N",
        refdes="J2",
        pin="1",
        layer="F.Cu",
    )
    ring = [
        [10.75, y - 0.025],
        [11.5, y - 0.025],
        [11.5, y + 0.025],
        [10.75, y + 0.025],
    ]
    return dict(
        layers=["F.Cu", "B.Cu"],
        pads=[land, other],
        drills=drills,
        copper=[dict(ctype="pour", net="N", layer="F.Cu", polygon=ring)],
    )


@pytest.mark.parametrize("offset", [False, True], ids=["centered", "offset"])
@pytest.mark.parametrize("contact", [False, True], ids=["void", "land"])
@pytest.mark.parametrize("pending", [False, True], ids=["actual", "pending"])
def test_open_slot_region_requires_real_copper(
    offset: bool, contact: bool, pending: bool
) -> None:
    """Caught by Astra: open slot caps admitted a false electrical union."""
    model = _slot_model(offset, contact)
    land, _other = model["pads"]
    pour = model["copper"][0]
    # Independent boolean oracle: the narrow overlap is wholly drilled
    # away in the void cases; the raised control crosses actual land.
    physical = box(9.2, 9.2, 10.8, 10.8).difference(
        LineString(land["slot"]).buffer(0.3, quad_segs=256)
    )
    assert Polygon(pour["polygon"]).intersects(physical) is contact
    assert drc._slot_lateral_ring_mm(land, land["slot"], 0.6) == pytest.approx(0.5)
    if pending:
        model["pending_pours"] = [
            dict(net="N", layer="F.Cu", region=dict(points=pour["polygon"]))
        ]
        model["copper"] = []
    assert [(i.net, i.components) for i in connectivity.net_islands(model)] == (
        [] if contact else [("N", 2)]
    )
    if not pending:
        pair = frozenset({("J1", "1"), ("J2", "1")})
        assert (pair in connectivity.connected_pin_pairs(model)) is contact
        findings = drc.check_connectivity(model)
        assert [(f.rule, f.severity) for f in findings] == (
            [] if contact else [("connectivity", "error")]
        )


@pytest.mark.parametrize("offset", [False, True], ids=["centered", "offset"])
def test_open_slot_void_does_not_join_pour_components(offset: bool) -> None:
    model = _slot_model(offset, False)
    model["copper"].append(
        dict(
            ctype="pour",
            net="N",
            layer="F.Cu",
            polygon=[[9.1, 10.4], [9.3, 10.4], [9.3, 10.5], [9.1, 10.5]],
        )
    )
    assert connectivity.pour_components(model) == {("N", "F.Cu"): 2}


@pytest.mark.parametrize("pending", [False, True], ids=["actual", "pending"])
@pytest.mark.parametrize("gap", [-0.00002, 0.00002], ids=["overlap20nm", "gap20nm"])
def test_open_slot_preserves_tiny_native_cap_contact(pending: bool, gap: float) -> None:
    # The right stadium cap is the analytic circle (10.6,10), r=.6.
    # A 60-degree radial strip contacts its cap beyond the open slot wall.
    ux, uy = 0.5, math.sqrt(3.0) / 2.0

    def point(t: float, v: float) -> tuple[float, float]:
        return 10.6 + t * ux - v * uy, 10.0 + t * uy + v * ux

    land = dict(
        shape="obround",
        x=10.0,
        y=10.0,
        w=2.4,
        h=1.2,
        drill=0.6,
        slot=[[10.1, 10.0], [11.1, 10.0]],
        layer="F.Cu",
        net="N",
        refdes="J1",
        pin="1",
    )
    ring = [
        point(0.6 + gap, 0.0),
        point(1.6, 0.0),
        point(1.6, 0.02),
        point(0.6 + gap, 0.02),
    ]
    x, y = point(1.5, 0.01)
    probe = dict(
        shape="circle",
        x=x,
        y=y,
        w=0.1,
        layer="F.Cu",
        net="N",
        refdes="J2",
        pin="1",
    )
    pour = dict(ctype="pour", net="N", layer="F.Cu", polygon=ring)
    model: dict[str, Any] = dict(
        layers=["F.Cu", "B.Cu"], pads=[land, probe], copper=[pour]
    )
    expected = gap < 0.0
    body = realise._PadBody(
        LineString([(9.4, 10.0), (10.6, 10.0)]),
        0.6,
        0,
        0.0,
        realise.Body(LineString(land["slot"]), r=0.3),
    )
    assert (
        realise._pour_piece_joined(
            Polygon(ring),
            0,
            "F.Cu",
            "N",
            0,
            [],
            [],
            {0: [("N", body, 0)]},
            None,
            model["layers"],
        )
        is expected
    )
    if pending:
        model["pending_pours"] = [dict(net="N", layer="F.Cu", region=dict(points=ring))]
        model["copper"] = []
    else:
        pair = frozenset({("J1", "1"), ("J2", "1")})
        assert (pair in connectivity.connected_pin_pairs(model)) is expected
    assert (connectivity.net_islands(model) == []) is expected


@pytest.mark.parametrize("offset", [0.0, 0.4], ids=["centered", "offset"])
@pytest.mark.parametrize("shape", ["rect", "obround", "circle", "polygon"])
def test_round_bore_does_not_join_an_enclosed_pour(shape: str, offset: float) -> None:
    land = dict(
        shape=shape,
        x=10.0,
        y=10.0,
        w=2.4,
        h=1.6,
        drill=0.6,
        drill_x=10.0 + offset,
        drill_y=10.0,
        layer="F.Cu",
        net="N",
        refdes="J1",
        pin="1",
        poly=[[8.8, 9.2], [11.2, 9.2], [11.2, 10.8], [8.8, 10.8]],
    )
    x = 10.0 + offset
    rings = [
        [[x - 0.02, 9.98], [x + 0.02, 9.98], [x + 0.02, 10.02], [x - 0.02, 10.02]],
        [[9.8, 10.5], [10.2, 10.5], [10.2, 10.7], [9.8, 10.7]],
    ]
    model = dict(
        layers=["F.Cu", "B.Cu"],
        pads=[land],
        copper=[dict(ctype="pour", net="N", layer="F.Cu", polygon=r) for r in rings],
    )
    assert connectivity.pour_components(model) == {("N", "F.Cu"): 2}
