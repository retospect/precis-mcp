"""gr524239 (topo-route-review C): a plane net drops to its plane by via.

A board that pours GND on In1 routes no GND wire: every SMD GND pad gets
a fanout via and a stub on its own layer, In1 is barred to every routed
net, and the plane is poured over the finished copper (an interim, Reto
2026-11-01). Driven through ``pcb.local``'s route stage, DB-free — the
half the ``placer='topo'`` job shares.

The board: four two-pad parts. GND sits on one pad of each, spread round
the board (R1 west, R2 east); signal S runs R3 → R4 south to north, across
the line a GND wire from R1 to R2 would take on F.Cu. (Two signals that
cross each other do not route here yet — the router's open crossing
resolution, with or without the plane.)
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from precis.pcb import drc as pcb_drc
from precis.pcb import local
from precis.pcb import realize as pcb_realize
from precis.pcb import session as pcb_session
from tests._pcb_outline import closed_outline

_P2: dict[str, Any] = {
    "name": "P2",
    "pads": [
        {"number": "1", "shape": "rect", "x": -0.5, "y": 0.0, "w": 0.5, "h": 0.5},
        {"number": "2", "shape": "rect", "x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5},
    ],
    "pin_map": {"1": "1", "2": "2"},
}


def _part(refdes: str, x: float, y: float) -> dict[str, Any]:
    return {
        "refdes": refdes,
        "label": "r",
        "footprint": "P2",
        "x": x,
        "y": y,
        "pins": [{"name": "1"}, {"name": "2"}],
    }


def _batch(*, plane: bool) -> dict[str, Any]:
    features: list[dict[str, Any]] = [closed_outline(0.0, 0.0, 30.0, 20.0)]
    if plane:
        features.append({"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"})
    wiring = {
        "GND": (("R1", "1"), ("R2", "2"), ("R3", "1"), ("R4", "2")),
        "S": (("R3", "2"), ("R4", "1")),
    }
    return {
        "footprints": [_P2],
        "components": [
            _part("R1", 8.0, 10.0),
            _part("R2", 22.0, 10.0),
            _part("R3", 15.0, 4.0),
            _part("R4", 15.0, 16.0),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
            {"name": "S", "current": 0.001},
        ],
        "connections": [
            {"net": net, "refdes": r, "pin": p}
            for net, members in wiring.items()
            for r, p in members
        ],
        "features": features,
    }


@pytest.fixture(scope="module")
def routed() -> tuple[Any, Any, Any, Any]:
    design = local._design(_batch(plane=True), None)
    ir, fps, _specs = local._prepare(design)
    return design, ir, fps, local._route_stage(design, ir, fps)


def test_each_gnd_pad_drops_to_the_plane_by_its_own_via(routed) -> None:
    design, ir, fps, stage = routed
    gnd = list(ir.net_name).index("GND")
    assert stage.plan.fanout_missing == ()
    assert [layer for _net, layer in stage.plan.planes] == [1]
    pads = [
        p
        for p in pcb_realize.pads_for_ir(ir, design.layer_names, fps)
        if p["net"] == "GND"
    ]
    vias = [v for v in stage.result.vias if v.net_id == gnd]
    assert len(pads) == 4
    assert len(vias) >= 4
    for pad in pads:
        near = min(math.hypot(v.x - pad["x"], v.y - pad["y"]) for v in vias)
        assert near < 1.5, pad


def test_signals_stay_off_the_plane_layer_and_the_plane_pours(routed) -> None:
    _design, ir, _fps, stage = routed
    gnd = list(ir.net_name).index("GND")
    assert all(t.layer != 1 for t in stage.result.tracks)
    # GND is never wired: its copper is the stubs on the pads' own layer
    assert all(t.layer == 0 for t in stage.result.tracks if t.net_id == gnd)
    assert any(p["layer"] == "In1.Cu" and p["net"] == "GND" for p in stage.result.pours)
    assert stage.rows["S"]["status"] == "realized"


def test_the_poured_board_is_drc_clean(routed) -> None:
    design, ir, fps, stage = routed
    model = pcb_session._copper_model(
        ir,
        pcb_realize.result_copper_rows(
            stage.result, ir, design.layer_names, derived=True
        ),
        footprints=fps,
    )
    errors = [
        (f.rule, f.where)
        for f in pcb_drc.run_geometric_drc(
            model,
            capability=design.fab_caps,
            outline=[list(p) for p in (design.outline or [])],
            keepouts=pcb_session.keepouts_from_features(design.features),
            net_rules=pcb_session.clearance_net_rules(design.graph, design.fab_caps),
        )
        if f.severity == "error"
    ]
    assert errors == []


def test_without_a_plane_gnd_routes_as_a_net() -> None:
    """The split only fires on a poured net: the same board without the
    plane feature wires GND like any other net and pours nothing."""
    design = local._design(_batch(plane=False), None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    assert stage.plan.planes == ()
    assert stage.result.pours == ()
