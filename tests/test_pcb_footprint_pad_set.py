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
    assert owners[(10.0, 7.0)] >= ir.n_nets + ir.n_pins
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


# ── inst_body_bbox: the footprint row's authored courtyard as body ──────


def _body_graph() -> dict[str, Any]:
    """PV1-class part (two far-apart lands under one body) plus a small
    0402-class part — the taz-leaf L1-inside-PV1 case, synthetic."""
    return {
        "instances": [
            {
                "refdes": "PV1",
                "part_lcsc": "C5358484",
                "x": 50.0,
                "y": 50.0,
                "fixed": "both",
            },
            {"refdes": "L1", "x": 50.0, "y": 52.0},
        ],
        "nets": [
            {
                "name": "IN",
                "members": [
                    {"refdes": "PV1", "pin": "1"},
                    {"refdes": "L1", "pin": "1"},
                ],
            },
            {
                "name": "OUT",
                "members": [
                    {"refdes": "PV1", "pin": "2"},
                    {"refdes": "L1", "pin": "2"},
                ],
            },
        ],
    }


def _wide_body_fp() -> dict[str, Any]:
    """Two 3x8mm lands at x = ±10.7 under a 24.4 x 8.1mm body claim."""
    return {
        "pads": [
            _pad("1", -10.7, 0.0, w=3.0, h=8.0),
            _pad("2", 10.7, 0.0, w=3.0, h=8.0),
        ],
        "pin_map": {},
        "courtyard": {"bbox": [-12.2, -4.064, 12.2, 4.064]},
    }


def _body_ir(footprints: dict[str, Any], **kw: Any):
    ir = from_graph(_body_graph(), stackup=DEFAULT_STACKUP)
    apply_real_pin_offsets(ir, footprints, **kw)
    return ir


def test_courtyard_covers_the_body_between_disjoint_pads() -> None:
    """Reto 2026-11-02: "2 objects can not occupy same space. No parts
    can be inside another part's courtyard." A catalog footprint's
    authored courtyard is the part's BODY claim: the ring covers the
    whole span, not two pad islands bridged by a corridor — the gap the
    placer parked L1 in."""
    from shapely.geometry import Point

    ir = _body_ir({"PV1": _wide_body_fp()}, footprint_sources={"PV1": "catalog"})
    assert ir.inst_body_bbox[0] == (-12.2, -4.064, 12.2, 4.064)
    assert ir.inst_body_bbox[1] is None, "L1 has no footprint row"
    court = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.4))
    assert court.covers(Point(0.0, 0.0)), "mid-body must be keep-out"
    # The zero-clearance body ring is the authored span, not two islands.
    body = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.0))
    assert body.covers(Point(0.0, 0.0))
    assert body.area == pytest.approx(24.4 * 8.128, rel=0.02)


def test_catalog_row_with_pad_sized_courtyard_still_fills_the_gap() -> None:
    """A catalog row's courtyard IS vendor data even when it equals the
    pad bbox exactly (the outline never widened it) — the vendor's
    footprint claims the whole extent either way."""
    from shapely.geometry import Point

    fp = _wide_body_fp()
    fp["courtyard"] = {"bbox": [-12.2, -4.0, 12.2, 4.0]}  # == pad extent
    ir = _body_ir({"PV1": fp}, footprint_sources={"PV1": "catalog"})
    body = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.0))
    assert body.covers(Point(0.0, 0.0))


def test_derived_local_courtyard_does_not_fill_the_gap() -> None:
    """A design-local row's PAD-DERIVED courtyard — the
    ``normalize_footprint`` fallback, exactly the pads' own extent —
    carries no body claim: the ring stays the pad union, the same
    equality check that keeps an authored comb's concave ring."""
    from shapely.geometry import Point

    fp = _wide_body_fp()
    fp["courtyard"] = {"bbox": [-12.2, -4.0, 12.2, 4.0]}  # == pad extent
    ir = _body_ir({"PV1": fp})  # no sources -> not a catalog row
    assert ir.inst_body_bbox[0] is None
    body = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.0))
    # The bridged corridor hugs the centre line; off it the gap is open.
    assert not body.covers(Point(0.0, 0.5))


def test_authored_local_courtyard_fills_the_gap() -> None:
    """A design-local row whose courtyard IS authored (it differs from
    the pad extent — a supercap's authored span) gets the same body
    coverage as a catalog row."""
    from shapely.geometry import Point

    ir = _body_ir({"PV1": _wide_body_fp()})  # differs from pad extent
    assert ir.inst_body_bbox[0] == (-12.2, -4.064, 12.2, 4.064)
    body = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.0))
    assert body.covers(Point(0.0, 0.0))


def test_body_bbox_join_failure_keeps_the_pin_fallback() -> None:
    """The pad set's own rule, mirrored: an instance whose pins do not
    resolve onto the footprint's labels contributes nothing — no pads,
    and no body hanging off pads the IR does not carry."""
    fp = _wide_body_fp()
    fp["pin_map"] = {"1": {"name": "X"}}  # pin "1" resolves no pad
    ir = _body_ir({"PV1": fp}, footprint_sources={"PV1": "catalog"})
    assert ir.inst_body_bbox[0] is None
    assert [p.instance for p in ir.footprint_pads] == []


def test_concave_pad_union_survives_a_derived_courtyard() -> None:
    """The authored-comb case: POLYGON pads plus a courtyard equal to
    their own extent — the ring keeps hugging the pad outlines, the
    mouth stays open. Had the pad bbox been applied as a body claim the
    ring would be the rectangle (the pre-gr524238 corner-hull again)."""
    from shapely.geometry import Point

    u = [
        (0.0, 0.0),
        (6.0, 0.0),
        (6.0, 6.0),
        (4.0, 6.0),
        (4.0, 2.0),
        (2.0, 2.0),
        (2.0, 6.0),
        (0.0, 6.0),
    ]
    fp = {
        "pads": [
            {"number": "1", "shape": "polygon", "x": 3.0, "y": 3.0, "poly": u},
            _pad("2", 9.0, 0.0),
        ],
        "pin_map": {},
        # == _pads_extent_bbox(pads): the pad-derived fallback
        "courtyard": {"bbox": [0.0, -0.2, 9.3, 6.0]},
    }
    ir = _body_ir({"PV1": fp})
    assert ir.inst_body_bbox[0] is None
    court = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.0))
    assert not court.covers(Point(3.0, 4.0)), "the U's mouth must stay open"
    assert court.area < court.convex_hull.area


def test_small_part_inside_the_body_is_a_courtyard_overlap() -> None:
    """DRC ``courtyard_overlap`` sees the body too: an 0402 parked in the
    gap between the wide part's lands — where the old islands-plus-
    corridor ring left it — reports an overlap. With the occupied
    unions and the bodied set threaded (the round-2 inputs), it grades
    as the ERROR Reto ruled — L1's lands sit inside PV1's modelled
    body, a proven collision, not a margin shortfall."""
    from precis.pcb.drc import check_courtyard_overlap
    from precis.pcb.ir import bodied_refdes, world_courtyard_polygons

    ir = _body_ir({"PV1": _wide_body_fp()}, footprint_sources={"PV1": "catalog"})
    assert bodied_refdes(ir) == frozenset({"PV1"})
    polys = world_courtyard_polygons(ir, clearance_mm=0.4)
    bodies = [
        (str(ir.instance_refdes[i]), p)
        for i, p in enumerate(world_courtyard_polygons(ir, clearance_mm=0.0))
    ]
    findings = check_courtyard_overlap(
        [("PV1", polys[0]), ("L1", polys[1])],
        bottom_by_refdes={},
        clearance_mm=0.4,
        bodies=bodies,
        bodied=bodied_refdes(ir),
    )
    assert findings, "L1 inside PV1's body must report courtyard_overlap"
    assert findings[0].rule == "courtyard_overlap"
    assert findings[0].severity == "error"


def _pv1_l1_overlap_inputs(ir):
    """The shared geometry for the provenance-grading tests: world
    courtyards at the placer clearance, zero-clearance occupied unions,
    and model pads for both parts (L1's lands sit inside PV1's modelled
    body span, clear of PV1's real lands)."""
    from precis.pcb.ir import world_courtyard_polygons

    polys = world_courtyard_polygons(ir, clearance_mm=0.4)
    bodies = [
        (str(ir.instance_refdes[i]), p)
        for i, p in enumerate(world_courtyard_polygons(ir, clearance_mm=0.0))
    ]
    pads = [
        {
            "refdes": "PV1",
            "layer": "F.Cu",
            "net": "IN",
            "shape": "rect",
            "x": 50.0 - 10.7,
            "y": 50.0,
            "w": 3.0,
            "h": 8.0,
        },
        {
            "refdes": "PV1",
            "layer": "F.Cu",
            "net": "OUT",
            "shape": "rect",
            "x": 50.0 + 10.7,
            "y": 50.0,
            "w": 3.0,
            "h": 8.0,
        },
        {
            "refdes": "L1",
            "layer": "F.Cu",
            "net": "IN",
            "shape": "rect",
            "x": 50.0,
            "y": 52.0,
            "w": 0.4,
            "h": 0.4,
        },
    ]
    return [("PV1", polys[0]), ("L1", polys[1])], bodies, pads


def test_graphic_hint_body_reserves_but_does_not_prove_a_collision() -> None:
    """gr565552: a catalog row whose courtyard is an INFERRED graphic hint
    (``courtyard.provenance "graphic_hint"`` — EasyEDA silk/TRACK outline
    extrema, e.g. X1 C18213028's asymmetric decoration box) keeps its
    keep-out reservation but is not a verified body claim. The placer's
    ring still covers the box; DRC grades an overlap reaching it as a
    margin shortfall (WARN), not a proven collision (ERROR)."""
    from shapely.geometry import Point

    from precis.pcb.drc import check_courtyard_overlap
    from precis.pcb.ir import (
        bodied_refdes,
        verified_bodied_refdes,
    )

    fp = _wide_body_fp()
    fp["courtyard"] = {
        "bbox": [-12.2, -4.064, 12.2, 4.064],
        "provenance": "graphic_hint",
    }
    ir = _body_ir({"PV1": fp}, footprint_sources={"PV1": "catalog"})
    # The reservation stands — the marker changes the claim, not the
    # geometry. A conservative reservation is not proof it is unsafe.
    assert ir.inst_body_bbox[0] == (-12.2, -4.064, 12.2, 4.064)
    assert ir.inst_body_provenance[0] == "graphic_hint"
    assert ir.inst_body_provenance[1] is None, "L1 has no footprint row"
    assert bodied_refdes(ir) == frozenset({"PV1"})
    assert verified_bodied_refdes(ir) == frozenset()
    body = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.0))
    assert body.covers(Point(0.0, 0.0)), "mid-body reservation must stand"
    courtyards, bodies, pads = _pv1_l1_overlap_inputs(ir)
    findings = check_courtyard_overlap(
        courtyards,
        bottom_by_refdes={},
        clearance_mm=0.4,
        pads=pads,
        bodies=bodies,
        bodied=verified_bodied_refdes(ir),
    )
    assert findings, "L1 inside the hinted span must still report"
    assert findings[0].severity == "warn", findings[0].detail


def test_unmarked_easyeda_cache_row_is_reclassified_as_hint_on_read() -> None:
    """Review td625107 finding 1: a cache row written before
    ``courtyard.provenance`` existed carries no marker, but the C-number
    cache is populated only from EasyEDA, whose producer derives the box
    from the pads plus silk-outline extrema — a pre-marker row IS that
    inferred envelope (gr565552's X1 C18213028 decoration box), not
    vendor-authored body data. On read the marker is re-derived with the
    producer's own rule: wider than the pad extent -> ``"graphic_hint"``
    — the keep-out reservation stands, but :func:`verified_bodied_refdes`
    excludes it and DRC grades an overlap reaching it a WARN, not a
    proven collision."""
    from precis.pcb.drc import check_courtyard_overlap
    from precis.pcb.ir import verified_bodied_refdes

    ir = _body_ir({"PV1": _wide_body_fp()}, footprint_sources={"PV1": "catalog"})
    assert ir.inst_body_provenance[0] == "graphic_hint"
    assert verified_bodied_refdes(ir) == frozenset()
    courtyards, bodies, pads = _pv1_l1_overlap_inputs(ir)
    findings = check_courtyard_overlap(
        courtyards,
        bottom_by_refdes={},
        clearance_mm=0.4,
        pads=pads,
        bodies=bodies,
        bodied=verified_bodied_refdes(ir),
    )
    assert findings
    assert findings[0].severity == "warn", findings[0].detail


def test_unmarked_cache_row_matching_the_pad_extent_is_pad_extent() -> None:
    """The producer's other stamp: a pre-marker row whose box equals the
    pads' own extent is ``"pad_extent"`` on read — the reservation
    stands (the box still keeps the placer out) but it claims nothing
    beyond the pads, so it is not a verified body either."""
    from precis.pcb.ir import verified_bodied_refdes

    fp = _wide_body_fp()
    fp["courtyard"] = {"bbox": [-12.2, -4.0, 12.2, 4.0]}  # == pad extent
    ir = _body_ir({"PV1": fp}, footprint_sources={"PV1": "catalog"})
    assert ir.inst_body_bbox[0] == (-12.2, -4.0, 12.2, 4.0)
    assert ir.inst_body_provenance[0] == "pad_extent"
    assert verified_bodied_refdes(ir) == frozenset()


def test_unmarked_cache_row_with_unparseable_pads_stays_conservative() -> None:
    """The re-derivation needs the pads' extent to compare against; a row
    whose pads do not parse gives no signal, so the conservative unknown
    reading ``"catalog"`` survives and the body stays verified."""
    from precis.pcb.ir import footprint_body_bboxes, footprint_body_provenances

    ir = from_graph(_body_graph(), stackup=DEFAULT_STACKUP)
    bad = {
        "pads": [
            {"number": "1", "shape": "RECT", "x": 0.0, "y": 0.0, "w": "fat", "h": 8.0},
            {"number": "2", "shape": "RECT", "x": 1.0, "y": 0.0, "w": 3.0, "h": 8.0},
        ],
        "pin_map": {},
        "courtyard": {"bbox": [-12.2, -4.064, 12.2, 4.064]},
    }
    assert footprint_body_bboxes(ir, {"PV1": bad}, catalog_refdes={"PV1"})[0] == (
        -12.2,
        -4.064,
        12.2,
        4.064,
    )
    assert (
        footprint_body_provenances(ir, {"PV1": bad}, catalog_refdes={"PV1"})[0]
        == "catalog"
    )


def _authored_wide_body_fp() -> dict[str, Any]:
    """PV1's row as a VERIFIED claim — the designer-authored courtyard
    marker — the mixed-provenance grading tests' verified half."""
    fp = _wide_body_fp()
    fp["courtyard"] = {
        "bbox": [-12.2, -4.064, 12.2, 4.064],
        "provenance": "authored",
    }
    return fp


def _hinted_l1_fp() -> dict[str, Any]:
    """L1's row: a small two-pad part whose courtyard box is an INFERRED
    graphic-hint envelope (the producer's stamp) extending toward PV1 —
    never a verified body claim."""
    return {
        "pads": [_pad("1", 0.0, 0.0), _pad("2", 0.8, 0.0)],
        "pin_map": {},
        "courtyard": {"bbox": [-2.0, -0.7, 0.3, 0.5], "provenance": "graphic_hint"},
    }


def _mixed_pair_ir(l1_x: float):
    """The shared geometry for the mixed-provenance grading tests: PV1
    (verified authored body, world x 37.8–62.2, y 45.936–54.064 around
    its (50, 50) anchor) beside L1 at ``l1_x``, whose hint envelope
    reaches back toward PV1."""
    graph = _body_graph()
    graph["instances"][1]["x"] = l1_x
    graph["instances"][1]["y"] = 50.0
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    apply_real_pin_offsets(ir, {"PV1": _authored_wide_body_fp(), "L1": _hinted_l1_fp()})
    return ir


def _mixed_pair_inputs(ir):
    from precis.pcb.ir import world_courtyard_polygons

    courtyards = [
        (str(ir.instance_refdes[i]), p)
        for i, p in enumerate(world_courtyard_polygons(ir, clearance_mm=0.4))
        if p
    ]
    bodies = [
        (str(ir.instance_refdes[i]), p)
        for i, p in enumerate(world_courtyard_polygons(ir, clearance_mm=0.0))
        if p
    ]
    return courtyards, bodies


def test_mixed_pair_hint_reservation_beside_verified_body_is_a_warn() -> None:
    """Review td625107 finding 2: body grading used to activate when
    EITHER part was verified and then treat both occupied unions as
    authoritative, so the inferred hint envelope of a part beside a
    verified one produced a spurious ERROR. The hint cannot prove a
    collision: here L1's pads sit clear of PV1 (both parts' copper
    clears) and only L1's hint reservation reaches PV1's courtyard
    margin — a shortfall (WARN) naming the inferred reservation, while
    the reservation itself still reports (the pair is not silent)."""
    from precis.pcb.drc import check_courtyard_overlap
    from precis.pcb.ir import verified_bodied_refdes

    ir = _mixed_pair_ir(63.2)
    assert verified_bodied_refdes(ir) == frozenset({"PV1"})
    pads = [
        {
            "refdes": "PV1",
            "layer": "F.Cu",
            "net": "IN",
            "shape": "rect",
            "x": 50.0 - 10.7,
            "y": 50.0,
            "w": 3.0,
            "h": 8.0,
        },
        {
            "refdes": "PV1",
            "layer": "F.Cu",
            "net": "OUT",
            "shape": "rect",
            "x": 50.0 + 10.7,
            "y": 50.0,
            "w": 3.0,
            "h": 8.0,
        },
        {
            "refdes": "L1",
            "layer": "F.Cu",
            "net": "IN",
            "shape": "rect",
            "x": 63.2,
            "y": 50.0,
            "w": 0.6,
            "h": 0.4,
        },
        {
            "refdes": "L1",
            "layer": "F.Cu",
            "net": "OUT",
            "shape": "rect",
            "x": 64.0,
            "y": 50.0,
            "w": 0.6,
            "h": 0.4,
        },
    ]
    courtyards, bodies = _mixed_pair_inputs(ir)
    findings = check_courtyard_overlap(
        courtyards,
        bottom_by_refdes={},
        clearance_mm=0.4,
        pads=pads,
        bodies=bodies,
        bodied=verified_bodied_refdes(ir),
    )
    assert findings, "the reservation overlap must still report"
    assert findings[0].severity == "warn", findings[0].detail
    assert "inferred (unverified) body reservation" in findings[0].detail


def test_mixed_pair_verified_body_against_real_lands_is_still_an_error() -> None:
    """The other half of the mixed pair: the hint status of L1's body
    does not shield a REAL proximity — L1's lands sit within the margin
    of PV1's verified body, so the overlap fills with geometry both
    sides stand behind and the pair stays a proven collision (ERROR)."""
    from precis.pcb.drc import check_courtyard_overlap
    from precis.pcb.ir import verified_bodied_refdes

    ir = _mixed_pair_ir(62.4)
    assert verified_bodied_refdes(ir) == frozenset({"PV1"})
    pads = [
        {
            "refdes": "PV1",
            "layer": "F.Cu",
            "net": "IN",
            "shape": "rect",
            "x": 50.0 - 10.7,
            "y": 50.0,
            "w": 3.0,
            "h": 8.0,
        },
        {
            "refdes": "PV1",
            "layer": "F.Cu",
            "net": "OUT",
            "shape": "rect",
            "x": 50.0 + 10.7,
            "y": 50.0,
            "w": 3.0,
            "h": 8.0,
        },
        {
            "refdes": "L1",
            "layer": "F.Cu",
            "net": "IN",
            "shape": "rect",
            "x": 62.4,
            "y": 50.0,
            "w": 0.6,
            "h": 0.4,
        },
        {
            "refdes": "L1",
            "layer": "F.Cu",
            "net": "OUT",
            "shape": "rect",
            "x": 63.2,
            "y": 50.0,
            "w": 0.6,
            "h": 0.4,
        },
    ]
    courtyards, bodies = _mixed_pair_inputs(ir)
    findings = check_courtyard_overlap(
        courtyards,
        bottom_by_refdes={},
        clearance_mm=0.4,
        pads=pads,
        bodies=bodies,
        bodied=verified_bodied_refdes(ir),
    )
    assert findings
    assert findings[0].severity == "error", findings[0].detail
    assert "PV1's occupied area sits inside L1's courtyard" in findings[0].detail


def test_explicit_provenance_marker_wins_over_the_equality_check() -> None:
    """A design-local row whose courtyard EQUALS the pad extent is the
    pad-derived fallback — no body claim — unless the row carries an
    explicit marker: ``"authored"`` is a designer's claim (verified, gap
    filled), ``"graphic_hint"`` is an inferred envelope (reservation
    kept, not verified). The marker is stronger information than the
    equality heuristic."""
    from precis.pcb.ir import verified_bodied_refdes

    fp = _wide_body_fp()
    fp["courtyard"] = {"bbox": [-12.2, -4.0, 12.2, 4.0], "provenance": "authored"}
    ir = _body_ir({"PV1": fp})  # design-local, no sources
    assert ir.inst_body_bbox[0] == (-12.2, -4.0, 12.2, 4.0)
    assert ir.inst_body_provenance[0] == "authored"
    assert verified_bodied_refdes(ir) == frozenset({"PV1"})

    hinted = _wide_body_fp()
    hinted["courtyard"] = {
        "bbox": [-12.2, -4.0, 12.2, 4.0],
        "provenance": "graphic_hint",
    }
    ir2 = _body_ir({"PV1": hinted})
    assert ir2.inst_body_bbox[0] == (-12.2, -4.0, 12.2, 4.0)
    assert ir2.inst_body_provenance[0] == "graphic_hint"
    assert verified_bodied_refdes(ir2) == frozenset()


def test_a_rigid_group_pair_is_exempt_from_the_overlap_rule() -> None:
    """The authored exemption, end to end: two instances of one rigid
    ``inst_group`` assembly get no pair rule at all
    (:func:`precis.pcb.ir.rigid_refdes_pairs` — the placer's
    ``rigid_pairs`` ruling). Overlapping courtyards that would ERROR
    between strangers — a nested pair, the authored-adjacency case the
    ruling names — report nothing between group mates."""
    from precis.pcb.drc import check_courtyard_overlap
    from precis.pcb.ir import (
        bodied_refdes,
        rigid_refdes_pairs,
        world_courtyard_polygons,
    )

    graph = _body_graph()
    for k, inst in enumerate(graph["instances"]):
        inst["group"] = "assay"
        inst["group_offset"] = {"x": 0.0, "y": 2.0 * k, "rot": 0.0}
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    apply_real_pin_offsets(
        ir, {"PV1": _wide_body_fp()}, footprint_sources={"PV1": "catalog"}
    )
    assert rigid_refdes_pairs(ir) == frozenset({("L1", "PV1")})
    polys = world_courtyard_polygons(ir, clearance_mm=0.4)
    bodies = [
        (str(ir.instance_refdes[i]), p)
        for i, p in enumerate(world_courtyard_polygons(ir, clearance_mm=0.0))
    ]
    assert (
        check_courtyard_overlap(
            [("PV1", polys[0]), ("L1", polys[1])],
            bottom_by_refdes={},
            clearance_mm=0.4,
            bodies=bodies,
            bodied=bodied_refdes(ir),
            rigid_pairs=rigid_refdes_pairs(ir),
        )
        == []
    )


def test_a_pattern_group_is_not_a_rigid_pair() -> None:
    """Round-3 blocker, case 1: a ``pattern``/``pattern_instance``
    tile is a post-seed stamp, NOT a mover unit — ``design_from_ir``'s
    ``group_of`` build skips it, so ``rigid_refdes_pairs`` must skip it
    too or DRC would hide a collision the placer moves independently.
    Both demotions share :func:`precis.pcb.ir.rigid_instance_groups`:
    the pattern members get no exemption (the nested pair reports) and
    the placer agrees — ``group_members`` is empty either way."""
    from precis.pcb.drc import check_courtyard_overlap
    from precis.pcb.ir import (
        bodied_refdes,
        rigid_refdes_pairs,
        world_courtyard_polygons,
    )
    from precis.pcb.topoplace.design import design_from_ir

    graph = _body_graph()
    for inst in graph["instances"]:
        inst["pattern"] = "tile"
        inst["pattern_instance"] = 0
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    apply_real_pin_offsets(
        ir, {"PV1": _wide_body_fp()}, footprint_sources={"PV1": "catalog"}
    )
    assert rigid_refdes_pairs(ir) == frozenset()
    td = design_from_ir(ir, {"PV1": _wide_body_fp()})
    assert td.group_members == ()
    polys = world_courtyard_polygons(ir, clearance_mm=0.4)
    bodies = [
        (str(ir.instance_refdes[i]), p)
        for i, p in enumerate(world_courtyard_polygons(ir, clearance_mm=0.0))
    ]
    findings = check_courtyard_overlap(
        [("PV1", polys[0]), ("L1", polys[1])],
        bottom_by_refdes={},
        clearance_mm=0.4,
        bodies=bodies,
        bodied=bodied_refdes(ir),
        rigid_pairs=rigid_refdes_pairs(ir),
    )
    assert findings, "a pattern pair's nest must report, not exempt"
    assert findings[0].rule == "courtyard_overlap"


def test_a_shapeless_group_is_not_a_rigid_pair() -> None:
    """Round-3 blocker, case 2: one member with NEITHER a
    ``group_offset`` NOR a placed pose demotes the whole group — the
    placer cannot recover its rigid shape, so DRC cannot exempt it.
    J1/J2 pose-overlap inside group 'hdr' while J3 is poseless: the
    pair reports (``rigid_refdes_pairs`` empty) and
    ``design_from_ir`` demotes the same group."""
    from precis.pcb.drc import check_courtyard_overlap
    from precis.pcb.ir import rigid_refdes_pairs, world_courtyard_polygons
    from precis.pcb.topoplace.design import design_from_ir

    ir = from_graph(
        {
            "instances": [
                {
                    "refdes": "J1",
                    "pins": [{"name": "1"}],
                    "x": 50.0,
                    "y": 50.0,
                    "group": "hdr",
                },
                {
                    "refdes": "J2",
                    "pins": [{"name": "1"}],
                    "x": 50.4,
                    "y": 50.0,
                    "group": "hdr",
                },
                {"refdes": "J3", "pins": [{"name": "1"}], "group": "hdr"},
            ],
            "nets": [],
        },
        stackup=DEFAULT_STACKUP,
    )
    assert rigid_refdes_pairs(ir) == frozenset()
    td = design_from_ir(ir, {})
    assert td.group_members == ()
    polys = world_courtyard_polygons(ir, clearance_mm=0.4, fallback_half_extent_mm=1.0)
    assert polys[0] and polys[1], "the two posed members have courtyards"
    findings = check_courtyard_overlap(
        [("J1", polys[0]), ("J2", polys[1])],
        bottom_by_refdes={},
        clearance_mm=0.4,
        rigid_pairs=rigid_refdes_pairs(ir),
    )
    assert findings, "the demoted pair's overlap must report"
    assert findings[0].rule == "courtyard_overlap"


@pytest.mark.parametrize("rot", [0.0, 90.0, 180.0, 270.0])
def test_pad_count_is_invariant_under_instance_rotation(rot: float) -> None:
    graph = _graph()
    graph["instances"][0]["rot"] = rot
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    footprints = {"U1": _DUP_FP}
    apply_real_pin_offsets(ir, footprints)
    pads = [p for p in pads_for_ir(ir, _LAYERS, footprints) if p["refdes"] == "U1"]
    assert len(pads) == len(_DUP_FP["pads"])
