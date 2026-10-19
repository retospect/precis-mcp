"""pcb-stackup-regions slices 3 and 4, DB-free: the stackup keys and
presets, direction-aware maze costs, effective plane assignments (stackup
``plane_net``/``fill``), region pours, derived power regions, the bare
island drop and the ``pour_fragmented`` rule."""

from __future__ import annotations

from typing import Any

import pytest
from shapely.geometry import Point, Polygon

from precis.pcb import DEFAULT_STACKUP, STACKUP_PRESETS, maze, stackup_preset
from precis.pcb import ir as pcb_ir
from precis.pcb.connectivity import pour_components
from precis.pcb.drc import check_pour_fragmented
from precis.pcb.planes import (
    FILL_PRIORITY,
    PourSpec,
    derive_regions,
    plane_pours,
    region_polygon,
    resolve_assignments,
)
from precis.pcb.session import content_hash

_BOARD = [[0.0, 0.0], [40.0, 0.0], [40.0, 30.0], [0.0, 30.0]]
_HYBRID = stackup_preset("4l-two-sided")


def _poly(pour: dict[str, Any]) -> Polygon:
    holes = [[(float(x), float(y)) for x, y in h] for h in pour.get("holes") or []]
    return Polygon([(float(x), float(y)) for x, y in pour["polygon"]], holes)


# ── slice 3: stackup keys and presets ─────────────────────────────────────


def test_every_preset_validates_and_the_hybrid_has_the_ruled_shape() -> None:
    for name in STACKUP_PRESETS:
        assert pcb_ir.validate_stackup(stackup_preset(name)) == stackup_preset(name)
    by_name = {e["name"]: e for e in _HYBRID}
    assert by_name["In1.Cu"]["plane_net"] == "GND"
    assert pcb_ir.layer_is_routable(by_name["In1.Cu"])
    assert 2.0 <= by_name["In1.Cu"]["route_cost"] <= 3.0
    assert by_name["In2.Cu"]["power_regions"] is True
    assert {by_name[n]["fill"] for n in ("F.Cu", "In2.Cu", "B.Cu")} == {"GND"}


def test_preset_ground_rename() -> None:
    layers = stackup_preset("4l-two-sided", ground="VSS")
    assert {e.get("plane_net") or e.get("fill") for e in layers} == {"VSS"}


@pytest.mark.parametrize(
    ("entry", "ok"),
    [
        ({"direction": "h"}, True),
        ({"direction": "any"}, True),
        ({"direction": 45}, True),
        ({"direction": "x"}, False),
        ({"direction": True}, False),
        ({"route_cost": 2.5}, True),
        ({"route_cost": 0.5}, False),
        ({"route_cost": "2"}, False),
        ({"fill": "GND"}, True),
        ({"fill": " "}, False),
        ({"fill": "GND", "plane_net": "GND"}, False),
        ({"power_regions": True}, True),
        ({"power_regions": "yes"}, False),
        ({"power_regions": True, "plane_net": "GND"}, False),
        ({"reference": "B.Cu"}, True),
        ({"reference": "In9.Cu"}, False),
        ({"reference": "F.Cu"}, False),
    ],
)
def test_new_stackup_keys_validate_or_refuse(entry: dict[str, Any], ok: bool) -> None:
    layers = [{"name": "F.Cu", "role": "signal", **entry}, {"name": "B.Cu"}]
    if ok:
        pcb_ir.validate_stackup(layers)
    else:
        with pytest.raises(ValueError, match=r"stackup\[0\]"):
            pcb_ir.validate_stackup(layers)


def test_direction_token_snaps_angles() -> None:
    assert pcb_ir.direction_token(0) == "h"
    assert pcb_ir.direction_token(90.0) == "v"
    assert pcb_ir.direction_token(135) == "d"
    assert pcb_ir.direction_token(178) == "h"
    assert pcb_ir.direction_token("any") is None


def test_reference_defaults_to_nearest_poured_layer() -> None:
    stack = _HYBRID
    assert pcb_ir.reference_layer(stack, 0) == "In1.Cu"
    assert pcb_ir.reference_layer(stack, 3) == "In2.Cu"
    explicit = [dict(e) for e in stack]
    explicit[0]["reference"] = "B.Cu"
    assert pcb_ir.reference_layer(explicit, 0) == "B.Cu"


def test_an_h_layer_prices_vertical_steps_and_an_any_layer_does_not() -> None:
    assert maze._step_penalty("h", 1, 0) == 1.0
    assert maze._step_penalty("h", 0, 1) == maze.OFF_AXIS_PENALTY
    assert maze._step_penalty(None, 0, 1) == 1.0
    assert maze._step_penalty("h", 0, 1, 1.2) == 1.2
    assert maze._step_penalty("h", 1, 1, 1.1) == 1.1  # diagonal capped


def test_layer_preferences_read_the_stackup_direction() -> None:
    from precis.pcb.realize import _layer_preferences

    ir = pcb_ir.from_graph({"instances": [], "nets": []})
    ir.stackup = [
        {"name": "F.Cu", "role": "signal", "direction": "v"},
        {"name": "In1.Cu", "role": "signal", "direction": "any"},
        {"name": "In2.Cu", "role": "signal"},
        {"name": "B.Cu", "role": "signal", "direction": 0},
    ]
    prefs = _layer_preferences(ir, [0, 1, 2, 3])
    assert prefs[0] == "v"
    assert 1 not in prefs
    assert prefs[3] == "h"
    assert prefs[2] == "v"  # index-order fallback: In1 'h', In2 'v'


def test_layer_cost_reads_stackup_route_cost_and_the_run_overrides() -> None:
    from precis.pcb.realize import RealizeConfig, _layer_cost

    ir = pcb_ir.from_graph({"instances": [], "nets": []}, stackup=_HYBRID)
    assert _layer_cost(ir, RealizeConfig()) == {1: 2.5}
    over = RealizeConfig(layer_route_cost=(("In1.Cu", 3.0), ("B.Cu", 1.5)))
    assert _layer_cost(ir, over) == {1: 3.0, 3: 1.5}
    with pytest.raises(ValueError, match="off_axis_penalty"):
        RealizeConfig(off_axis_penalty=0.9)


# ── slice 3/4: effective plane assignments ────────────────────────────────


def test_stackup_plane_net_and_fill_resolve_without_rows() -> None:
    """gr495287: the default stackup's In1 GND pours with no pcb_planes row;
    gr495395: every layer's fill pours GND."""
    got, notes = resolve_assignments(_HYBRID, [], ["GND", "N1"])
    assert notes == []
    assert {(a.layer, a.net, a.source, a.priority) for a in got} == {
        ("In1.Cu", "GND", "stackup", 0),
        ("F.Cu", "GND", "fill", FILL_PRIORITY),
        ("In2.Cu", "GND", "fill", FILL_PRIORITY),
        ("B.Cu", "GND", "fill", FILL_PRIORITY),
    }
    assert all(a.locked for a in got)


def test_a_missing_net_or_a_whole_board_row_skips_the_declaration() -> None:
    _, notes = resolve_assignments(_HYBRID, [], ["N1"])
    assert any("In1.Cu=GND" in n and "will not pour" in n for n in notes)
    rows = [{"layer": "In1.Cu", "net": "V5", "source": "authored"}]
    got, notes = resolve_assignments(_HYBRID, rows, ["GND", "V5"])
    assert ("In1.Cu", "GND") not in {(a.layer, a.net) for a in got}
    assert any("V5 (authored) already pours" in n for n in notes)


def test_a_legacy_stackup_keeps_its_plane_net_a_declaration() -> None:
    """The old default (no slice-3 keys) pours nothing without a row — the
    switch to honouring it waits on Reto (nano floor table in the spec)."""
    got, notes = resolve_assignments(DEFAULT_STACKUP, [], ["GND"])
    assert got == []
    assert any("legacy" in n and "In1.Cu=GND" in n for n in notes)
    rows = [{"layer": "In1.Cu", "net": "GND", "source": "authored"}]
    got, notes = resolve_assignments(DEFAULT_STACKUP, rows, ["GND"])
    assert [(a.layer, a.net, a.source) for a in got] == [("In1.Cu", "GND", "authored")]
    assert notes == []


# ── slice 4: region pours ─────────────────────────────────────────────────


def test_regions_pour_first_and_the_fill_takes_the_rest() -> None:
    region = {"points": [[2, 2], [15, 2], [15, 12], [2, 12]]}
    pours = plane_pours(
        outline=_BOARD,
        layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        copper=[],
        clearance_mm=0.2,
        edge_clearance_mm=0.5,
        specs=[
            PourSpec(2, "GND", None, FILL_PRIORITY),
            PourSpec(2, "3V3", region, 5),
        ],
    )
    v33 = [_poly(p) for p in pours if p["net"] == "3V3"]
    gnd = [_poly(p) for p in pours if p["net"] == "GND"]
    assert len(v33) == 1 and gnd
    assert v33[0].within(Polygon(region["points"]).buffer(1e-6))
    for g in gnd:
        # Never overlapping, and at least the clearance apart.
        assert g.distance(v33[0]) >= 0.2 - 1e-6
    # The unclaimed rest is GND.
    assert sum(g.area for g in gnd) > 0.8 * (39 * 29 - v33[0].area)


def test_a_whole_board_plane_leaves_nothing_for_a_lower_spec() -> None:
    pours = plane_pours(
        outline=_BOARD,
        layers=["F.Cu"],
        copper=[{"ctype": "via", "net": "N2", "x": 5, "y": 5, "dia_mm": 0.6}],
        clearance_mm=0.2,
        edge_clearance_mm=0.5,
        specs=[PourSpec(0, "N1"), PourSpec(0, "N2")],
    )
    assert {p["net"] for p in pours} == {"N1"}


def test_plane_nets_and_specs_pour_byte_identically() -> None:
    copper = [{"ctype": "via", "net": "N2", "x": 5, "y": 5, "dia_mm": 0.6}]
    kw: dict[str, Any] = {
        "outline": _BOARD,
        "layers": ["F.Cu", "B.Cu"],
        "copper": copper,
        "clearance_mm": 0.2,
        "edge_clearance_mm": 0.5,
    }
    assert plane_pours(plane_nets={1: "N1"}, **kw) == plane_pours(
        specs=[PourSpec(1, "N1")], **kw
    )


def test_derive_regions_priority_by_pin_count_and_no_overlap() -> None:
    pins = {
        "3V3": [(5.0, 5.0), (8.0, 6.0), (6.0, 9.0)],
        "5V": [(30.0, 20.0), (33.0, 22.0)],
    }
    regions, notes = derive_regions(
        pins, outline=_BOARD, edge_clearance_mm=0.5, clearance_mm=0.2, reach_mm=2.0
    )
    assert notes == []
    assert [(r.net, r.priority) for r in regions] == [("3V3", 3), ("5V", 2)]
    a, b = (region_polygon(r.region) for r in regions)
    assert a is not None and b is not None
    assert a.distance(b) >= 0.2 - 1e-6
    for x, y in pins["3V3"]:
        # Facet slack only: two 64-gon circles at different angles.
        assert a.buffer(0.01).contains(Point(x, y).buffer(2.0))


def test_derive_regions_refuses_a_net_its_neighbour_crowds_out() -> None:
    pins = {
        "BIG": [(5.0, 5.0), (20.0, 5.0), (20.0, 20.0), (5.0, 20.0)],
        "SMALL": [(12.0, 12.0), (13.0, 13.0)],
    }
    regions, notes = derive_regions(
        pins, outline=_BOARD, edge_clearance_mm=0.5, clearance_mm=0.2, reach_mm=2.0
    )
    assert [r.net for r in regions] == ["BIG"]
    assert any(n.startswith("SMALL: no region") for n in notes)


def test_derive_regions_splits_a_straddled_boundary_at_the_bisector() -> None:
    # Two pins of different rails 10 mm apart: each reach disc (5.4 mm)
    # crosses the midline, so first come first served starves the later
    # net. The fair pass cuts at x = 20 less half the clearance each side.
    pins = {
        "3V3": [(5.0, 15.0), (15.0, 15.0)],
        "5V": [(25.0, 15.0), (35.0, 15.0)],
    }
    regions, notes = derive_regions(
        pins, outline=_BOARD, edge_clearance_mm=0.5, clearance_mm=0.2, reach_mm=5.4
    )
    assert [r.net for r in regions] == ["3V3", "5V"], notes
    a, b = (region_polygon(r.region) for r in regions)
    assert a is not None and b is not None
    assert a.bounds[2] <= 20.0 - 0.1 + 1e-6
    assert b.bounds[0] >= 20.0 + 0.1 - 1e-6
    assert a.distance(b) >= 0.2 - 1e-6


def test_content_hash_sees_region_and_priority() -> None:
    graph: dict[str, Any] = {"instances": [], "nets": []}
    row = {"layer": "In2.Cu", "net": "3V3", "source": "authored", "region": None}
    base = content_hash(graph, {}, session_state={"planes": [row]})
    legacy = content_hash(
        graph,
        {},
        session_state={"planes": [{**row, "region_hint": None, "priority": 0}]},
    )
    assert base == legacy  # a pre-region row keeps its digest
    region = {**row, "region": {"points": [[0, 0], [1, 0], [1, 1]]}}
    assert content_hash(graph, {}, session_state={"planes": [region]}) != base
    prio = {**row, "priority": 3}
    assert content_hash(graph, {}, session_state={"planes": [prio]}) != base


# ── islands: drop only what stitching could not reach ─────────────────────


def _two_pin_ir() -> pcb_ir.PcbIR:
    graph = {
        "instances": [
            {"refdes": "R1", "label": "10k 0603", "x": 5.0, "y": 5.0},
        ],
        "nets": [
            {
                "name": "GND",
                "members": [{"refdes": "R1", "pin": "1"}, {"refdes": "R1", "pin": "2"}],
            }
        ],
    }
    return pcb_ir.from_graph(
        graph, outline=[(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
    )


def test_a_bare_island_is_dropped_and_reported_a_padded_one_kept() -> None:
    from precis.pcb.realize import _drop_bare_islands, pads_for_ir

    ir = _two_pin_ir()
    layers = [str(e["name"]) for e in ir.stackup]
    pads = pads_for_ir(ir, layers)
    held = {
        "ctype": "pour",
        "layer": "F.Cu",
        "net": "GND",
        "polygon": [[2, 2], [9, 2], [9, 9], [2, 9]],
    }
    bare = {
        "ctype": "pour",
        "layer": "F.Cu",
        "net": "GND",
        "polygon": [[30, 20], [35, 20], [35, 25], [30, 25]],
    }
    kept, dropped = _drop_bare_islands(ir, [], [], [held, bare], pads)
    assert kept == [held]
    assert [(d.net, d.layer, round(d.area_mm2)) for d in dropped] == [
        ("GND", "F.Cu", 25)
    ]
    assert dropped[0].centroid == pytest.approx((32.5, 22.5))
    # Authored fixed copper anchors a piece too.
    fixed = [{"ctype": "via", "net": "GND", "x": 32.0, "y": 22.0, "dia_mm": 0.6}]
    kept, dropped = _drop_bare_islands(ir, [], [], [held, bare], pads, fixed)
    assert kept == [held, bare] and dropped == []


def test_pour_fragmented_counts_electrical_pieces_per_layer() -> None:
    a = {
        "ctype": "pour",
        "layer": "F.Cu",
        "net": "GND",
        "polygon": [[0, 0], [5, 0], [5, 5], [0, 5]],
    }
    b = {
        "ctype": "pour",
        "layer": "F.Cu",
        "net": "GND",
        "polygon": [[10, 0], [15, 0], [15, 5], [10, 5]],
    }
    via_a = {
        "ctype": "via",
        "net": "GND",
        "x": 2.0,
        "y": 2.0,
        "dia_mm": 0.6,
        "layers": ["F.Cu", "B.Cu"],
    }
    via_b = {
        "ctype": "via",
        "net": "GND",
        "x": 12.0,
        "y": 2.0,
        "dia_mm": 0.6,
        "layers": ["F.Cu", "B.Cu"],
    }
    joined = {
        "ctype": "track",
        "layer": "B.Cu",
        "net": "GND",
        "width_mm": 0.25,
        "segments": [{"shape": "line", "start": [2, 2], "end": [12, 2]}],
    }
    split = {"layers": ["F.Cu", "B.Cu"], "copper": [a, b, via_a, via_b], "pads": []}
    assert pour_components(split) == {("GND", "F.Cu"): 2}
    found = check_pour_fragmented(split)
    assert [(f.rule, f.severity) for f in found] == [("pour_fragmented", "warn")]
    whole = {**split, "copper": [a, b, via_a, via_b, joined]}
    assert pour_components(whole) == {("GND", "F.Cu"): 1}
    assert check_pour_fragmented(whole) == []
