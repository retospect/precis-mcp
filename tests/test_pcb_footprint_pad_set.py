"""gr451276 — the IR's footprint-sourced PAD SET, separate from pin identity.

Reto's ruling (gripe comment 2): "a pad is a pad, it's part of the
footprint, that's its origin. It should be visible both places, whether
used or not: it takes up that space, no wire can route through it, even if
it is NC." Pins resolve by NAME (pin ids stay load-bearing for pin swaps,
net indexing and pin_to_net-by-name); pads are keyed by NUMBER, so two pads
sharing one name are two pads carrying one pin — where the name-keyed join
(``session._real_pin_offsets``, first-wins) could only ever keep the first.

The acceptance line from the gripe: for a part with a cached real
footprint, the emitted pad count equals the footprint's pad count,
regardless of how many nets reference it.
"""

from __future__ import annotations

from typing import Any

import pytest
from shapely.geometry import Polygon, box

from precis.pcb import DEFAULT_STACKUP, connectivity
from precis.pcb import realize as pcb_realize
from precis.pcb.ir import (
    NO_NET,
    NO_PIN,
    footprint_pad_set,
    from_graph,
    instance_courtyard_polygon,
    instance_land_rects,
)
from precis.pcb.realize import pads_for_ir
from precis.pcb.session import apply_real_pin_offsets

_LAYERS = [layer["name"] for layer in DEFAULT_STACKUP]


def _pad(number: str, x: float, y: float, **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "shape": "RECT",
        "x": x,
        "y": y,
        "w": 0.6,
        "h": 0.4,
        **extra,
    }


#: A 6-pad part shaped like the HV507 case the gripe names ("12 non-channel
#: NAMES across 13 pads"): two physically distinct GND leads share ONE pin
#: name, one lead is NC (never declared by any netlist), the rest are
#: ordinary one-pad pins.
_DUP_FP: dict[str, Any] = {
    "pads": [
        _pad("1", -2.0, -1.0),
        _pad("2", -2.0, 1.0),
        _pad("3", 2.0, 1.0),
        _pad("4", 2.0, -1.0),
        _pad("5", 0.0, -3.0),
        _pad("6", 0.0, 3.0),
    ],
    "pin_map": {
        "1": {"name": "GND"},
        "2": {"name": "A"},
        "3": {"name": "B"},
        "4": {"name": "GND"},
        "5": {"name": "NC1"},
        "6": {"name": "TP"},  # declared, but on no net
    },
}


def _graph() -> dict[str, Any]:
    return {
        "instances": [
            {"refdes": "U1", "part_lcsc": "C1", "x": 10.0, "y": 10.0},
            {"refdes": "J1", "x": 30.0, "y": 10.0},
        ],
        "nets": [
            {"name": "GND", "members": [{"refdes": "U1", "pin": "GND"}]},
            {
                "name": "SIG",
                "members": [{"refdes": "U1", "pin": "A"}, {"refdes": "J1", "pin": "1"}],
            },
            {"name": "SIG2", "members": [{"refdes": "U1", "pin": "B"}]},
        ],
        "unconnected": [{"refdes": "U1", "pin": "TP"}],
    }


def _ir(footprints: dict[str, Any] | None = None):
    ir = from_graph(_graph(), stackup=DEFAULT_STACKUP)
    if footprints:
        apply_real_pin_offsets(ir, footprints)
    return ir


def test_pad_set_is_keyed_by_number_and_pinned_by_name() -> None:
    ir = _ir()
    pads = footprint_pad_set(ir, {"U1": _DUP_FP})
    assert [p.number for p in pads] == ["1", "2", "3", "4", "5", "6"], (
        "every footprint pad, once, in footprint order"
    )
    by_label = {
        str(ir.pin_label[p]): p for p in range(ir.n_pins) if ir.pin_instance[p] == 0
    }
    gnd = [p for p in pads if p.label == "GND"]
    assert [p.pin for p in gnd] == [by_label["GND"]] * 2, (
        "two pads sharing a name are two pads carrying ONE pin"
    )
    assert [p.primary for p in gnd] == [True, False], (
        "exactly one of them is the pad ir.pin_dx/pin_w describe"
    )
    nc = next(p for p in pads if p.number == "5")
    assert nc.pin == NO_PIN and not nc.primary, "NC: net-less copper, no pin"
    tp = next(p for p in pads if p.number == "6")
    assert tp.pin == by_label["TP"] and int(ir.pin_net[tp.pin]) == NO_NET, (
        "a declared-but-unwired pin still owns its pad"
    )
    assert all(p.instance == 0 for p in pads), "J1 has no real footprint: no pads"


def test_pad_set_is_empty_for_an_instance_whose_pin_names_miss_the_footprint() -> None:
    """A pin the footprint does not name falls back to a synthesized pad at
    a synthesized offset; emitting the real lands beside it would put the
    same copper at two coordinates on two 'nets'. The whole instance keeps
    the old behaviour, and ``session.pin_name_mismatches`` reports it."""
    fp = {
        "pads": _DUP_FP["pads"],
        "pin_map": {**_DUP_FP["pin_map"], "2": {"name": "X"}},
    }
    assert footprint_pad_set(_ir(), {"U1": fp}) == ()


def test_pads_for_ir_emits_every_footprint_pad_exactly_once() -> None:
    """The gripe's acceptance line, at unit scale: 6 footprint pads, 3 nets
    naming 4 of them (one through two pads), one NC — 6 emitted."""
    footprints = {"U1": _DUP_FP}
    pads = [
        p
        for p in pads_for_ir(_ir(footprints), _LAYERS, footprints)
        if p["refdes"] == "U1"
    ]
    assert len(pads) == len(_DUP_FP["pads"])
    assert sorted(p["pin"] for p in pads) == ["A", "B", "GND", "GND", "NC1", "TP"]
    by_xy = {(p["x"], p["y"]): p for p in pads}
    assert len(by_xy) == 6, "six distinct lands, no pad emitted twice"
    # The second GND lead keeps its pin's net -- it is GND's own copper,
    # not a foreign obstacle to GND; the NC land has none.
    assert by_xy[(12.0, 9.0)]["net"] == "GND"
    assert by_xy[(8.0, 9.0)]["net"] == "GND"
    assert by_xy[(10.0, 7.0)]["net"] == "" and by_xy[(10.0, 7.0)]["pin"] == "NC1"
    assert by_xy[(10.0, 13.0)]["net"] == "" and by_xy[(10.0, 13.0)]["pin"] == "TP"
    assert all(p["synthesized"] is False for p in pads)


def test_router_claims_carry_the_pin_net_for_a_pinned_pad() -> None:
    """The second GND lead is claimed FOR GND (the same owner its primary
    pad stamps), an unwired pin's second pad under that pin's own NC
    sentinel, and an NC land in the synthetic band -- so GND may land on
    either of its leads and nothing may cross any of the three."""
    fp = {
        "pads": [*_DUP_FP["pads"], _pad("7", 0.0, 4.0)],
        "pin_map": {**_DUP_FP["pin_map"], "7": {"name": "TP"}},
    }
    footprints = {"U1": fp}
    ir = _ir(footprints)
    claims = pcb_realize._footprint_pad_claims(
        ir, _LAYERS, pcb_realize._signal_layers(ir), footprints, len(DEFAULT_STACKUP)
    )
    owners = {(round(p[0], 3), round(p[1], 3)): owner for p, owner, _s, _l in claims}
    gnd_net = next(n for n in range(ir.n_nets) if str(ir.net_name[n]) == "GND")
    tp_pin = next(p for p in range(ir.n_pins) if str(ir.pin_label[p]) == "TP")
    assert owners[(12.0, 9.0)] == gnd_net
    assert owners[(10.0, 14.0)] == ir.n_nets + tp_pin
    assert (
        owners[(10.0, 7.0)]
        >= ir.n_nets + ir.n_pins + pcb_realize._UNCLAIMED_PAD_NET_OFFSET
    )
    assert len(claims) == 3, "only the pads the per-pin loop does not stamp"


def test_connectivity_treats_the_pads_of_one_pin_as_one_node() -> None:
    """Two GND leads 4mm apart with copper on only one of them: the part
    bonds them internally, so GND is whole -- not an island per lead."""
    footprints = {"U1": _DUP_FP}
    ir = _ir(footprints)
    pads = pads_for_ir(ir, _LAYERS, footprints)
    track = {
        "ctype": "track",
        "net": "GND",
        "layer": "F.Cu",
        "width_mm": 0.2,
        "points": [[8.0, 9.0], [8.0, 20.0]],
    }
    islands = connectivity.net_islands(
        {"layers": _LAYERS, "copper": [track], "pads": pads}
    )
    assert "GND" not in {i.net for i in islands}, islands


def test_connectivity_still_reports_two_different_pins_apart() -> None:
    """The union is by PIN identity, never by net: A and J1.1 share SIG and
    are still two terminals the board must join."""
    footprints = {"U1": _DUP_FP}
    ir = _ir(footprints)
    pads = pads_for_ir(ir, _LAYERS, footprints)
    islands = connectivity.net_islands({"layers": _LAYERS, "copper": [], "pads": pads})
    assert [i.net for i in islands] == ["SIG"]


def test_courtyard_and_land_rects_cover_the_whole_pad_set() -> None:
    """The hull is over every footprint pad -- the NC lead and the second
    GND lead included -- not the pins the netlist names."""
    ir = _ir({"U1": _DUP_FP})
    court = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.1))
    for pad in _DUP_FP["pads"]:
        assert court.contains(
            box(pad["x"] - 0.3, pad["y"] - 0.2, pad["x"] + 0.3, pad["y"] + 0.2)
        ), f"pad {pad['number']} outside its own part's courtyard"
    assert len(instance_land_rects(ir)[0]) == len(_DUP_FP["pads"])
    assert len(instance_land_rects(ir)[1]) == 1, "J1: no footprint, one pin, one rect"


def test_courtyard_without_a_footprint_falls_back_to_the_pins() -> None:
    ir = _ir()
    assert ir.footprint_pads == ()
    assert len(instance_courtyard_polygon(ir, 0, clearance_mm=0.1)) > 0
    assert len(instance_land_rects(ir)[0]) == 4


@pytest.mark.parametrize("rot", [0.0, 90.0, 180.0, 270.0])
def test_pad_count_is_invariant_under_instance_rotation(rot: float) -> None:
    graph = _graph()
    graph["instances"][0]["rot"] = rot
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    footprints = {"U1": _DUP_FP}
    apply_real_pin_offsets(ir, footprints)
    pads = [p for p in pads_for_ir(ir, _LAYERS, footprints) if p["refdes"] == "U1"]
    assert len(pads) == len(_DUP_FP["pads"])
