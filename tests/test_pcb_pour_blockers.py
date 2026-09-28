"""A pour must yield to copper it did not draw itself.

Two blockers :func:`precis.pcb.realize._pour_planes` did not feed
:func:`precis.pcb.planes.plane_pours`, both found the first time a GND
plane was authored on an INNER layer of the EWOD dogfood (2026-09-27,
once ``put(args={'op':'stackup'})`` made that expressible at all):
61 ``clearance`` errors, then 7, then none.

Same shape of defect as the router's own
(docs/backlog/pcb-escape-and-driver-chain.md item 2, gr451276): a fixture
the pass cannot see is one it draws straight through. The pour version is
worse than a track's, because a fill is a single sheet — one missing
antipad shorts a whole net to the plane.
"""

from __future__ import annotations

from typing import Any

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import from_graph, pin_point
from precis.pcb.planes import point_in_pour
from precis.pcb.realize import RealizeConfig, realize

_OUTLINE = [[0.0, 0.0], [40.0, 0.0], [40.0, 30.0], [0.0, 30.0]]


def _pour_on(result: Any, layer: str, net: str) -> dict[str, Any]:
    hits = [p for p in result.pours if p["layer"] == layer and p["net"] == net]
    assert hits, (
        f"no {net} pour on {layer} — pours: {[(p['layer'], p['net']) for p in result.pours]}"
    )
    return hits[0]


def test_a_drilled_pad_is_antipadded_on_every_layer_not_just_its_own():
    """``pads_for_ir`` reports ONE layer per pad — the side its outer
    copper flash lands on — and its own docstring names ``pad["drill"]``
    as the "spans every layer" signal DRC reads instead
    (``drc.py::clearance_pairs_indexed``). ``_pad_blockers`` did not read
    it, so an INNER plane poured solid copper straight through every
    through-hole barrel on the board.

    The pad here is on F.Cu and the plane is on In1.Cu, so a
    single-layer blocker misses it by construction. The fab drills this
    hole; the fill has to get out of its way."""
    graph = {
        "instances": [
            {"refdes": "J1", "x": 20.0, "y": 15.0},
            {"refdes": "G1", "x": 5.0, "y": 5.0},
            {"refdes": "G2", "x": 35.0, "y": 25.0},
        ],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "domain": "electrical",
                "members": [{"refdes": "G1", "pin": "1"}, {"refdes": "G2", "pin": "1"}],
            },
            {
                "name": "HV",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "J1", "pin": "HV"}],
            },
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=_OUTLINE)
    gnd = next(n for n in range(ir.n_nets) if str(ir.net_name[n]) == "GND")
    ir.promote_plane(gnd, 1)  # In1.Cu — an INNER layer, not J1's own
    footprints = {
        "J1": {
            "pads": [
                {
                    # `padplace.pad_label` resolves a raw pad to its pin off
                    # `number` (via `pin_map` when the footprint declares one),
                    # which is the ONE implementation gr451276 consolidated.
                    "number": "HV",
                    "shape": "circle",
                    "x": 0.0,
                    "y": 0.0,
                    "w": 1.6,
                    "drill": 1.0,
                },
            ]
        }
    }
    result = realize(
        ir,
        config=RealizeConfig(fab_caps=capability_for("4layer")),
        footprints=footprints,
    )
    pour = _pour_on(result, "In1.Cu", "GND")
    hv_pin = next(
        pid
        for pid in range(ir.n_pins)
        if str(ir.net_name[int(ir.pin_net[pid])]) == "HV"
    )
    point = pin_point(ir, hv_pin)
    assert point is not None
    assert not point_in_pour(pour, *point), (
        "J1's through-hole land is copper on EVERY layer — the In1.Cu GND "
        "fill must be cut around it, not poured through the barrel",
        point,
    )
    assert pour.get("holes"), "no antipad was cut at all"


def test_authored_fixed_copper_is_antipadded_out_of_a_pour():
    """``_pour_planes`` built its blocker list from ``to_gerber_model``
    (router-drawn copper) plus pads plus mounting holes — never the
    AUTHORED ``fixed_copper`` rows ``_claim_fixed_copper`` already
    respects on the router's grid. So a plane flooded over authored vias
    and tracks: on the EWOD dogfood, one ``pour[GND] <-> via[ARR1_RxCy]``
    error per plaza via, 61 of them.

    The via here spans F.Cu..B.Cu, so its barrel passes THROUGH the
    poured In1.Cu whether or not anything is drawn on that layer — the
    case a per-layer ``layer`` compare would miss and
    ``drc._via_layer_names`` gets right off ``span``."""
    graph = {
        "instances": [
            {"refdes": "G1", "x": 5.0, "y": 5.0},
            {"refdes": "G2", "x": 35.0, "y": 25.0},
            {"refdes": "U1", "x": 30.0, "y": 5.0},
        ],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "domain": "electrical",
                "members": [{"refdes": "G1", "pin": "1"}, {"refdes": "G2", "pin": "1"}],
            },
            {
                "name": "SIG",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U1", "pin": "1"}],
            },
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=_OUTLINE)
    gnd = next(n for n in range(ir.n_nets) if str(ir.net_name[n]) == "GND")
    ir.promote_plane(gnd, 1)  # In1.Cu
    fixed: list[dict[str, Any]] = [
        {
            "ctype": "via",
            "net": "SIG",
            "x": 20.0,
            "y": 15.0,
            "dia_mm": 0.6,
            "span": ["F.Cu", "B.Cu"],
        },
        {
            "ctype": "track",
            "net": "SIG",
            "layer": "In1.Cu",
            "width_mm": 0.3,
            "segments": [{"start": [10.0, 20.0], "end": [30.0, 20.0]}],
        },
    ]
    config = RealizeConfig(fab_caps=capability_for("4layer"))
    result = realize(ir, config=config, fixed_copper=fixed)
    pour = _pour_on(result, "In1.Cu", "GND")
    assert not point_in_pour(pour, 20.0, 15.0), (
        "the authored SIG via's barrel passes through In1.Cu — the GND "
        "fill must be cut around it"
    )
    assert not point_in_pour(pour, 20.0, 20.0), (
        "the authored SIG track sits ON In1.Cu — the GND fill must be cut around it"
    )


def test_a_plane_still_swallows_its_OWN_authored_copper():
    """The complement, so the fix above cannot be "antipad everything".
    Own-net copper is the connection, not an obstacle — ``plane_pours``
    skips a blocker whose ``net`` matches the pour's, and an authored row
    must go through that same gate rather than being carved out of the
    very plane it belongs to."""
    graph = {
        "instances": [
            {"refdes": "G1", "x": 5.0, "y": 5.0},
            {"refdes": "G2", "x": 35.0, "y": 25.0},
        ],
        "nets": [
            {
                "name": "GND",
                "net_class": "ground",
                "domain": "electrical",
                "members": [{"refdes": "G1", "pin": "1"}, {"refdes": "G2", "pin": "1"}],
            },
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=_OUTLINE)
    ir.promote_plane(0, 1)
    fixed: list[dict[str, Any]] = [
        {
            "ctype": "track",
            "net": "GND",
            "layer": "In1.Cu",
            "width_mm": 0.3,
            "segments": [{"start": [10.0, 20.0], "end": [30.0, 20.0]}],
        }
    ]
    result = realize(
        ir, config=RealizeConfig(fab_caps=capability_for("4layer")), fixed_copper=fixed
    )
    pour = _pour_on(result, "In1.Cu", "GND")
    assert point_in_pour(pour, 20.0, 20.0), (
        "a GND track on the GND plane is part of the plane, not a hole in it"
    )
