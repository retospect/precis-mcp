"""pcb-stackup-regions slices 3 and 4, DB-free: the stackup keys and
presets, direction-aware maze costs, effective plane assignments (stackup
``plane_net``/``fill``), region pours, derived power regions, the bare
island drop and the ``pour_fragmented`` rule."""

from __future__ import annotations

from typing import Any

import pytest
from shapely.geometry import Point, Polygon

from precis.pcb import STACKUP_PRESETS, stackup_preset
from precis.pcb import ir as pcb_ir
from precis.pcb.connectivity import pour_components
from precis.pcb.drc import check_pour_fragmented
from precis.pcb.planes import (
    FILL_PRIORITY,
    PourSpec,
    ThermalSpec,
    derive_regions,
    plane_pours,
    region_polygon,
    resolve_assignments,
    thermal_spec,
)
from precis.pcb.session import content_hash
from tests.fixtures.pcb.stackups import LEGACY_V1_STACKUP

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


def test_default_stackup_pours_nothing() -> None:
    """Reto 2026-10-23 (me507871, superseding gr495395's "each layer
    should be filled"): no flood fill — a default board declares no
    ``plane_net``/``fill`` on any layer, so nothing pours unless a board
    authors one itself."""
    from precis.pcb import DEFAULT_STACKUP

    got, notes = resolve_assignments(DEFAULT_STACKUP, [], ["GND", "N1"])
    assert notes == []
    assert got == []


def test_a_missing_net_or_a_whole_board_row_skips_the_declaration() -> None:
    _, notes = resolve_assignments(_HYBRID, [], ["N1"])
    assert any("In1.Cu=GND" in n and "will not pour" in n for n in notes)
    rows = [{"layer": "In1.Cu", "net": "V5", "source": "authored"}]
    got, notes = resolve_assignments(_HYBRID, rows, ["GND", "V5"])
    assert ("In1.Cu", "GND") not in {(a.layer, a.net) for a in got}
    assert any("V5 (authored) already pours" in n for n in notes)


def test_a_legacy_stackup_keeps_its_plane_net_a_declaration() -> None:
    """The old default (no slice-3 keys), still on boards born before
    gr495395, pours nothing without a row."""
    got, notes = resolve_assignments(LEGACY_V1_STACKUP, [], ["GND"])
    assert got == []
    assert any("legacy" in n and "In1.Cu=GND" in n for n in notes)
    rows = [{"layer": "In1.Cu", "net": "GND", "source": "authored"}]
    got, notes = resolve_assignments(LEGACY_V1_STACKUP, rows, ["GND"])
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


# ── thermal reliefs (gr495399; pcb-board-profile-prototype last slice) ────

_THERMAL = ThermalSpec(spokes=4, spoke_width_mm=0.30, gap_mm=0.25)


def _gnd_pad(x: float, y: float, *, drill: float = 0.0) -> dict[str, Any]:
    pad: dict[str, Any] = {
        "layer": "F.Cu",
        "net": "GND",
        "shape": "circle",
        "x": x,
        "y": y,
        "w": 1.5,
        "refdes": "U1",
        "pin": "3",
        "synthesized": False,
    }
    if drill:
        pad["drill"] = drill
    return pad


def _pour_with_thermal(
    pad: dict[str, Any],
    copper: list[dict[str, Any]] | None = None,
    **kw: Any,
) -> list[dict[str, Any]]:
    return plane_pours(
        outline=_BOARD,
        layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        copper=list(copper or []),
        clearance_mm=0.2,
        edge_clearance_mm=0.5,
        specs=[PourSpec(0, "GND")],
        thermal=_THERMAL,
        pads=[pad],
        **kw,
    )


def _at(pad: dict[str, Any], dist_mm: float, deg: float) -> Point:
    import math

    a = math.radians(deg)
    return Point(
        float(pad["x"]) + dist_mm * math.cos(a),
        float(pad["y"]) + dist_mm * math.sin(a),
    )


def test_thermal_relief_cuts_an_antipad_and_keeps_four_spokes() -> None:
    pad = _gnd_pad(20.0, 15.0)
    pours = _pour_with_thermal(pad)
    assert len(pours) == 1
    pour = pours[0]
    assert pour.get("holes")
    filled = _poly(pour)
    # Inside the gap ring (pad radius 0.75 + half the 0.25 gap): an AXIAL
    # direction is bare (the antipad), a 45-degree direction is a spoke.
    assert not filled.contains(_at(pad, 0.875, 0.0))
    assert filled.contains(_at(pad, 0.875, 45.0))
    for deg in (45.0, 135.0, 225.0, 315.0):
        assert filled.contains(_at(pad, 0.875, deg))
    # The realized rings ride ``fill`` so the EPRO POURed cache images the
    # relief; the antipad hole is one of its rings.
    rings = pour["fill"][0]["paths"]
    assert rings[0] == pour["polygon"] and len(rings) == 1 + len(pour["holes"])


def test_thermal_relief_through_hole_pad_relieves_on_an_inner_layer() -> None:
    pad = _gnd_pad(20.0, 15.0, drill=0.8)
    pours = _pour_with_thermal(pad)
    inner = [p for p in pours]
    assert len(inner) == 1
    filled = _poly(inner[0])
    assert not filled.contains(_at(pad, 0.875, 0.0))
    assert filled.contains(_at(pad, 0.875, 45.0))


def test_thermal_relief_off_is_byte_identical_and_solid_net_stays_solid() -> None:
    pad = _gnd_pad(20.0, 15.0)
    off = plane_pours(
        outline=_BOARD,
        layers=["F.Cu"],
        plane_nets={0: "GND"},
        copper=[],
        clearance_mm=0.2,
        edge_clearance_mm=0.5,
    )
    solid = _pour_with_thermal(pad, solid_nets={"GND"})
    for p in off + solid:
        assert "holes" not in p and "starved_thermal" not in p and "fill" not in p


def test_starved_thermal_marks_a_pad_whose_spokes_are_all_cut() -> None:
    """A pad boxed in by foreign copper keeps under 2 bridges — it stays
    solid and is named on the pour's ``starved_thermal`` list, which
    ``drc.check_starved_thermal`` then reports."""
    pad = _gnd_pad(20.0, 15.0)
    # Four foreign vias on the 45-degree axes, each swallowing its spoke's
    # bridge end.
    copper = [
        {
            "ctype": "via",
            "net": "SIG",
            "x": float(pad["x"]) + 1.35 * dx,
            "y": float(pad["y"]) + 1.35 * dy,
            "dia_mm": 1.6,
            "layers": ["F.Cu"],
        }
        for dx, dy in ((1, 1), (-1, 1), (1, -1), (-1, -1))
    ]
    pours = _pour_with_thermal(pad, copper)
    assert len(pours) == 1
    starved = pours[0].get("starved_thermal")
    assert starved and starved[0]["pad"] == "U1.3"
    assert starved[0]["spokes"] < 2
    # Solid: the pad is still merged — no antipad hole anywhere.
    assert _poly(pours[0]).contains(Point(20.0, 15.0))
    # And the DRC rule reads the marker.
    from precis.pcb.drc import check_starved_thermal

    found = check_starved_thermal({"copper": pours, "pads": [pad]})
    assert [(f.rule, f.severity) for f in found] == [("starved_thermal", "warn")]
    assert "U1.3" in found[0].detail


def test_thermal_spec_from_profile_rules() -> None:
    from precis.pcb.profiles import profile_rules

    assert thermal_spec(profile_rules("prod")) is None
    spec = thermal_spec(profile_rules("prototype"))
    assert spec == ThermalSpec(spokes=4, spoke_width_mm=0.30, gap_mm=0.25)
