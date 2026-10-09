"""Unit tests for ``precis.pcb.session`` — no DB. Focused on
:func:`~precis.pcb.session.footprints_by_refdes`, the join between
:meth:`~precis.store.Store.pcb_footprints_for`'s C-number-keyed cache and
:func:`~precis.pcb.realize.pad_geometry`'s refdes-keyed ``footprints`` arg
(the missing link the "pads must be precisely what the footprint says"
task closes: :attr:`~precis.pcb.ir.PcbIR.instance_part_lcsc` is the ONLY
thing on the IR side that knows an instance's C-number).
"""

from __future__ import annotations

import math
from typing import Any

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import from_graph
from precis.pcb.session import build_ir, footprints_by_refdes, pin_swap_diff

_FP = {"pads": [{"number": "1", "x": 0.0, "y": 0.0, "w": 1.0, "shape": "RECT"}]}


def _graph():
    return {
        "instances": [
            {"refdes": "U1", "part_lcsc": "C2838500"},
            {"refdes": "C1", "part_lcsc": "C1525"},
            {"refdes": "J1"},  # no catalog part at all
        ],
        "nets": [
            {"name": "N1", "members": [{"refdes": "U1", "pin": "1"}]},
            {"name": "N2", "members": [{"refdes": "C1", "pin": "1"}]},
            {"name": "N3", "members": [{"refdes": "J1", "pin": "1"}]},
        ],
    }


def test_footprints_by_refdes_remaps_cached_lcsc_rows_onto_refdes():
    ir = from_graph(_graph(), stackup=DEFAULT_STACKUP)
    footprints_by_lcsc = {"C2838500": _FP}
    out = footprints_by_refdes(ir, footprints_by_lcsc)
    assert out == {"U1": _FP}


def test_footprints_by_refdes_omits_instances_with_no_cache_hit():
    """C1 has a real ``part_lcsc`` but no cached row yet, and J1 has no
    linked catalog part at all -- both are simply absent from the result
    (never a KeyError, never an invented entry); the caller
    (:func:`precis.pcb.realize.pad_geometry`) already treats "absent" as
    "fall back to synthesized" for exactly this reason."""
    ir = from_graph(_graph(), stackup=DEFAULT_STACKUP)
    out = footprints_by_refdes(ir, {})
    assert out == {}


def test_footprints_by_refdes_is_a_pure_remap_not_a_size_computation():
    """The function only remaps the KEY (C-number -> refdes) -- the VALUE
    passes through byte-identical, since :func:`precis.pcb.realize.
    pad_geometry` (not this function) is what turns a cached row into
    real per-pin geometry."""
    ir = from_graph(_graph(), stackup=DEFAULT_STACKUP)
    footprints_by_lcsc: dict[str, dict[str, Any]] = {
        "C2838500": _FP,
        "C1525": {"pads": []},
    }
    out = footprints_by_refdes(ir, footprints_by_lcsc)
    assert out["U1"] is _FP
    # C1's cached row has no pads -- still remapped verbatim; whether an
    # empty `pads` list counts as "no real data" is `pad_geometry`'s own
    # call (its docstring: `fp and fp.get("pads")`), not this function's.
    assert out["C1"] == {"pads": []}


# ── pin_swap_diff must not let NO_NET (-1) wrap-index into net_name ─────
# (found while wiring `part_lcsc` through this same module, and reported
# by a sibling agent as the identical sentinel-collision defect it hit in
# `realize.pads_for_ir` and `maze.FREE`: `NO_NET == -1` is also a valid
# Python/numpy index, so `ir.net_name[NO_NET]` doesn't raise -- it
# silently wraps to the LAST real net in the array.)
def _no_net_swap_graph():
    """U1 has three degree-0 pins (every net here has exactly one member,
    so `from_graph` creates the pin but no segment/dart for it) -- the
    equal-rotation-CSR-degree precondition `PcbIR.swap_pins` enforces is
    satisfied trivially, which is what lets this fixture swap a REAL net
    onto NO_NET without needing any placed geometry at all. `NET_LAST` is
    a SECOND, distinct net so a wrap-around bug reads back a wrong-but-
    real net name instead of accidentally matching the correct one -- with
    only one net in the graph the bug and the fix would look identical."""
    return {
        "instances": [{"refdes": "U1"}],
        "nets": [
            {"name": "NET_A", "members": [{"refdes": "U1", "pin": "A"}]},
            {"name": "NET_LAST", "members": [{"refdes": "U1", "pin": "C"}]},
        ],
        "unconnected": [{"refdes": "U1", "pin": "B"}],
    }


def test_pin_swap_diff_reports_empty_net_not_a_wrapped_last_net_name():
    ir = from_graph(_no_net_swap_graph(), stackup=DEFAULT_STACKUP)
    pin_a = next(p for p in range(ir.n_pins) if str(ir.pin_label[p]) == "A")
    pin_b = next(p for p in range(ir.n_pins) if str(ir.pin_label[p]) == "B")
    baseline = ir.pin_net.copy()
    ir.swap_pins(pin_a, pin_b)  # NET_A's pin now sits at NO_NET, and vice versa

    diff = pin_swap_diff(ir, baseline)
    by_pin = {d["pin"]: d for d in diff}
    assert by_pin["A"]["net"] == "", (
        "pin A moved OFF its net onto NO_NET -- must read back as an empty "
        "net, never a real (wrong) net name"
    )
    assert by_pin["B"]["net"] == "NET_A"


# ── real per-pin POSITIONS (gripe 338983) ────────────────────────────────
# The position counterpart of the size join above: `pad_geometry` took pad
# SIZE from a real footprint while every consumer still read the pin's
# POSITION off `landpattern.offsets_for`'s synthesized guess.


_WIDE_FP = {
    # Two pads 20mm apart — far enough that no plausible landpattern
    # synthesis for a 2-pin part could accidentally land on them, so a
    # test asserting the real coordinates cannot pass by coincidence
    # (the trivial-symmetry-group rule: y differs too, so a swapped or
    # mirrored read is visible).
    "pads": [
        {"number": "1", "x": -10.0, "y": 2.5, "w": 1.0, "shape": "RECT"},
        {"number": "2", "x": 10.0, "y": -2.5, "w": 1.0, "shape": "RECT"},
    ],
    "pin_map": {"1": {"name": "A"}, "2": {"name": "B"}},
}


def _two_pin_graph():
    return {
        "instances": [{"refdes": "U1", "part_lcsc": "C1", "x": 5.0, "y": 5.0}],
        "nets": [
            {"name": "N1", "members": [{"refdes": "U1", "pin": "A"}]},
            {"name": "N2", "members": [{"refdes": "U1", "pin": "B"}]},
        ],
    }


def test_apply_real_pin_offsets_replaces_the_synthesized_guess():
    from precis.pcb.session import apply_real_pin_offsets

    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    assert bool(ir.pin_offsets_synthesized.all())

    changed = apply_real_pin_offsets(ir, {"U1": _WIDE_FP})

    assert changed == 2
    by_label = {str(ir.pin_label[p]): p for p in range(ir.n_pins)}
    assert (ir.pin_dx[by_label["A"]], ir.pin_dy[by_label["A"]]) == (-10.0, 2.5)
    assert (ir.pin_dx[by_label["B"]], ir.pin_dy[by_label["B"]]) == (10.0, -2.5)
    assert not bool(ir.pin_offsets_synthesized.any())


def test_apply_real_pin_offsets_keeps_the_bound_where_there_is_no_footprint():
    """Same "absent means fall back, never invent" contract
    :func:`~precis.pcb.realize.pad_geometry` already honours for size — a
    part with no cached/authored footprint keeps its landpattern offsets
    AND keeps saying so (``pin_offsets_synthesized``)."""
    from precis.pcb.session import apply_real_pin_offsets

    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    before = (ir.pin_dx.copy(), ir.pin_dy.copy())

    assert apply_real_pin_offsets(ir, {}) == 0
    assert apply_real_pin_offsets(ir, {"U2": _WIDE_FP}) == 0

    assert (ir.pin_dx == before[0]).all() and (ir.pin_dy == before[1]).all()
    assert bool(ir.pin_offsets_synthesized.all())


def test_apply_real_pin_offsets_takes_the_first_pad_of_a_multi_pad_pin():
    """One pin, several pads — an EWOD electrode emits its crenellated
    BODY, then a neck stub, then a drilled via, all on one pin name
    (:mod:`precis.pcb.generators`). Position must come from the SAME pad
    that supplies the outline downstream (``realize._real_pad_sizes``'s
    own first-wins rule), or a real polygon gets anchored at another
    pad's centre — worse than the guess this replaces."""
    from precis.pcb.session import apply_real_pin_offsets

    fp = {
        "pads": [
            {"number": "1", "x": -10.0, "y": 2.5, "w": 2.0, "shape": "RECT"},
            {"number": "1", "x": -8.4, "y": 2.5, "w": 0.2, "shape": "RECT"},
            {"number": "1", "x": -8.0, "y": 2.5, "w": 0.45, "shape": "ELLIPSE"},
            {"number": "2", "x": 10.0, "y": -2.5, "w": 1.0, "shape": "RECT"},
        ],
        "pin_map": {"1": {"name": "A"}, "2": {"name": "B"}},
    }
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    apply_real_pin_offsets(ir, {"U1": fp})

    pin_a = next(p for p in range(ir.n_pins) if str(ir.pin_label[p]) == "A")
    assert (ir.pin_dx[pin_a], ir.pin_dy[pin_a]) == (-10.0, 2.5)


def test_apply_real_pin_offsets_carries_the_real_pad_size_and_outline():
    """gr460567: position was real while size stayed synthesized, so the
    courtyard hull wrapped tiny pads at real centres and cut through the
    real pads. Size, a pad's own rotation and a polygon ring now come from
    the same first pad as the centre, and the courtyard covers the pads."""
    from shapely.geometry import Polygon, box

    from precis.pcb.ir import instance_courtyard_polygon
    from precis.pcb.session import apply_real_pin_offsets

    fp = {
        "pads": [
            {"number": "1", "x": -10.0, "y": 2.5, "w": 4.0, "h": 1.0, "rot": 90},
            {
                "number": "2",
                "x": 10.0,
                "y": -2.5,
                "w": 3.0,
                "shape": "POLYGON",
                "poly": [[8.5, -4.0], [11.5, -4.0], [11.5, -1.0], [8.5, -1.0]],
            },
        ],
        "pin_map": {"1": {"name": "A"}, "2": {"name": "B"}},
    }
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    apply_real_pin_offsets(ir, {"U1": fp})

    by_label = {str(ir.pin_label[p]): p for p in range(ir.n_pins)}
    a, b = by_label["A"], by_label["B"]
    assert (ir.pin_w[a], ir.pin_h[a]) == (1.0, 4.0)  # rot 90 swaps w/h
    assert ir.pin_poly[b] == [(-1.5, -1.5), (1.5, -1.5), (1.5, 1.5), (-1.5, 1.5)]
    assert (ir.pin_w[b], ir.pin_h[b]) == (3.0, 3.0)
    assert not bool(ir.pin_pad_synthesized[a]) and not bool(ir.pin_pad_synthesized[b])

    court = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.1))
    assert court.contains(box(-10.5, 0.5, -9.5, 4.5))
    assert court.contains(box(8.5, -4.0, 11.5, -1.0))


def test_a_pin_with_several_same_numbered_pads_is_covered_whole():
    """gr460567 residual: SMD-1_BD8.7-D6.2 numbers its four split tabs all
    ``1``. Only the first sets the pin's position/size, but the courtyard
    and the land rects must cover every tab, or the courtyard cuts through
    the part's own copper."""
    from shapely.geometry import Polygon, box

    from precis.pcb.ir import instance_courtyard_polygon, instance_land_rects
    from precis.pcb.session import apply_real_pin_offsets

    tabs = [(-4.0, -4.0), (4.0, -4.0), (4.0, 4.0), (-4.0, 4.0)]
    fp = {
        "pads": [{"number": "1", "x": x, "y": y, "w": 1.5, "h": 1.0} for x, y in tabs]
        + [{"number": "2", "x": 0.0, "y": 0.0, "w": 1.0}],
        "pin_map": {"1": {"name": "A"}, "2": {"name": "B"}},
    }
    ir = from_graph(_two_pin_graph(), stackup=DEFAULT_STACKUP)
    apply_real_pin_offsets(ir, {"U1": fp})

    pin_a = next(p for p in range(ir.n_pins) if str(ir.pin_label[p]) == "A")
    assert (ir.pin_dx[pin_a], ir.pin_dy[pin_a]) == (-4.0, -4.0)
    # Every tab is a pad of the IR's pad set, keyed by number, carrying
    # pin A; only the first is the one `pin_dx`/`pin_w` describe.
    tabs_in_set = [pad for pad in ir.footprint_pads if pad.pin == pin_a]
    assert [(p.raw["x"], p.raw["y"]) for p in tabs_in_set] == tabs
    assert [p.primary for p in tabs_in_set] == [True, False, False, False]
    assert len(ir.footprint_pads) == 5

    court = Polygon(instance_courtyard_polygon(ir, 0, clearance_mm=0.1))
    for x, y in tabs:
        assert court.contains(box(x - 0.75, y - 0.5, x + 0.75, y + 0.5))
    assert len(instance_land_rects(ir)[0]) == 5


def test_build_ir_wires_real_pin_offsets_from_both_footprint_sources():
    """``build_ir`` is where the rule lives (one call site) — a caller
    passing either cache gets real positions on the IR every consumer
    reads, and a caller passing neither gets exactly today's synthesized
    behaviour."""
    graph = {
        "instances": [
            {"refdes": "U1", "part_lcsc": "C1", "x": 0.0, "y": 0.0},
            {"refdes": "E1", "footprint": "electrode-pair", "x": 0.0, "y": 0.0},
        ],
        "nets": [
            {"name": "N1", "members": [{"refdes": "U1", "pin": "A"}]},
            {"name": "N2", "members": [{"refdes": "E1", "pin": "A"}]},
        ],
    }
    ir = build_ir(
        graph,
        footprints_by_lcsc={"C1": _WIDE_FP},
        local_footprints_by_name={"electrode-pair": _WIDE_FP},
    )
    by_key = {
        (str(ir.instance_refdes[int(ir.pin_instance[p])]), str(ir.pin_label[p])): p
        for p in range(ir.n_pins)
    }
    assert (ir.pin_dx[by_key[("U1", "A")]], ir.pin_dy[by_key[("U1", "A")]]) == (
        -10.0,
        2.5,
    )
    assert (ir.pin_dx[by_key[("E1", "A")]], ir.pin_dy[by_key[("E1", "A")]]) == (
        -10.0,
        2.5,
    )
    assert bool(build_ir(graph).pin_offsets_synthesized.all())


# ── mounting-hole hydration (round-3 review item 4) ──────────────────────


def test_mounting_holes_from_features_parses_drill_ring_and_plating():
    from precis.pcb.session import mounting_holes_from_features

    holes = mounting_holes_from_features(
        [
            {"ftype": "outline", "geom": {"path": [[0, 0], [10, 0], [10, 10]]}},
            {"ftype": "mounting_hole", "x": 4.0, "y": 4.0, "geom": {"diameter": 4.3}},
            {
                "ftype": "mounting_hole",
                "x": 6.0,
                "y": 6.0,
                "geom": {
                    "diameter": 5.6,
                    "ring_dia_mm": 8.0,
                    "plated": True,
                    "head_dia_mm": 9.0,
                },
            },
            # malformed / degenerate rows must be skipped, never crash
            {"ftype": "mounting_hole", "x": 1.0, "y": 1.0, "geom": {}},
            {"ftype": "mounting_hole", "x": "oops", "y": 1.0, "geom": {"diameter": 3}},
        ]
    )
    assert [
        (h.x, h.y, h.drill_mm, h.ring_dia_mm, h.head_dia_mm, h.plated) for h in holes
    ] == [
        (4.0, 4.0, 4.3, 0.0, 0.0, False),
        # absent geom.head_dia_mm on the first hole defaults to 0.0; an
        # authored value round-trips onto the second (M4 screw head, say)
        (6.0, 6.0, 5.6, 8.0, 9.0, True),
    ]


def test_build_ir_carries_mounting_holes_onto_the_ir():
    from precis.pcb.session import build_ir, mounting_holes_from_features

    holes = mounting_holes_from_features(
        [{"ftype": "mounting_hole", "x": 2.0, "y": 3.0, "geom": {"diameter": 3.2}}]
    )
    ir = build_ir(_graph(), mounting_holes=holes)
    assert len(ir.mounting_holes) == 1
    assert ir.mounting_holes[0].drill_mm == 3.2
    # the default stays the degrade-cleanly empty tuple, mirroring outline
    assert build_ir(_graph()).mounting_holes == ()


# ── rigid "super footprint" groups / patterns (build_ir's own pass-through
# of PcbIR.inst_group and friends, see precis.pcb.ir._parse_instance_groups)


def test_build_ir_parses_an_authored_group_and_its_offset():
    graph = {
        "instances": [
            {
                "refdes": "J1",
                "group": "nano_hdr",
                "group_offset": {"x": 0.0, "y": 0.0, "rot": 0.0},
            },
            {
                "refdes": "J2",
                "group": "nano_hdr",
                "group_offset": {"x": 15.24, "y": 0.0, "rot": 0.0},
            },
            {"refdes": "U1"},  # ungrouped
        ],
        "nets": [],
    }
    ir = build_ir(graph)
    assert ir.n_groups == 1
    gid = int(ir.inst_group[0])
    assert gid >= 0
    assert int(ir.inst_group[1]) == gid
    assert int(ir.inst_group[2]) == -1  # U1 names no group at all
    assert ir.group_pattern[gid] is None  # an authored group, not a pattern
    assert int(ir.group_pattern_index[gid]) == -1
    assert float(ir.inst_group_offset_dx[0]) == 0.0
    assert float(ir.inst_group_offset_dx[1]) == 15.24
    assert float(ir.inst_group_offset_dy[1]) == 0.0


def test_build_ir_parses_pattern_instances_into_one_group_each():
    graph = {
        "instances": [
            {"refdes": "A0", "pattern": "channel", "pattern_instance": 0},
            {"refdes": "B0", "pattern": "channel", "pattern_instance": 0},
            {"refdes": "A1", "pattern": "channel", "pattern_instance": 1},
            {"refdes": "B1", "pattern": "channel", "pattern_instance": 1},
        ],
        "nets": [],
    }
    ir = build_ir(graph)
    assert ir.n_groups == 2
    g0, g0b = int(ir.inst_group[0]), int(ir.inst_group[1])
    g1, g1b = int(ir.inst_group[2]), int(ir.inst_group[3])
    assert g0 == g0b and g1 == g1b and g0 != g1
    assert ir.group_pattern[g0] == "channel"
    assert ir.group_pattern[g1] == "channel"
    assert int(ir.group_pattern_index[g0]) == 0
    assert int(ir.group_pattern_index[g1]) == 1
    # a pattern member carries NO authored offset -- that's a post-seed
    # tiling-stamp result (precis.pcb.optimize.seed_placement), never
    # authored data.
    assert math.isnan(float(ir.inst_group_offset_dx[0]))
    assert math.isnan(float(ir.inst_group_offset_dy[0]))
    assert math.isnan(float(ir.inst_group_offset_rot[0]))


def test_build_ir_ignores_malformed_group_and_pattern_entries():
    graph = {
        "instances": [
            {"refdes": "A", "group": ""},  # empty name -- ungrouped
            {"refdes": "B", "group": 123},  # wrong type -- ungrouped
            # a named group with a garbage group_offset -- degrades to
            # (0, 0, 0) rather than poisoning the group with NaN
            {"refdes": "C", "group": "g1", "group_offset": "not-a-dict"},
            {"refdes": "D", "pattern": "p", "pattern_instance": "oops"},  # no slot
            {"refdes": "E", "pattern": "", "pattern_instance": 0},  # empty name
            # both keys on one instance -- "pattern" wins, "group" ignored
            {
                "refdes": "F",
                "pattern": "p2",
                "pattern_instance": 0,
                "group": "g2",
                "group_offset": {"x": 1.0, "y": 2.0, "rot": 3.0},
            },
        ],
        "nets": [],
    }
    ir = build_ir(graph)  # must not raise on any of the above
    assert int(ir.inst_group[0]) == -1
    assert int(ir.inst_group[1]) == -1
    gid_c = int(ir.inst_group[2])
    assert gid_c >= 0
    assert ir.group_pattern[gid_c] is None
    assert float(ir.inst_group_offset_dx[2]) == 0.0
    assert float(ir.inst_group_offset_dy[2]) == 0.0
    assert float(ir.inst_group_offset_rot[2]) == 0.0
    assert int(ir.inst_group[3]) == -1
    assert int(ir.inst_group[4]) == -1
    gid_f = int(ir.inst_group[5])
    assert gid_f >= 0
    assert ir.group_pattern[gid_f] == "p2"
    # "group"/"group_offset" were ignored for F -- a pattern member's
    # offset arrays stay NaN, never F's authored (1, 2, 3).
    assert math.isnan(float(ir.inst_group_offset_dx[5]))


# --- pre-route DRC gate (docs/backlog/pcb-guided-place-route.md; gr451052) ---
# The bug these close: drc.run_geometric_drc had exactly ONE caller (the
# view='drc' path), so neither pcb_place nor pcb_route ran any geometric
# check and an already-illegal placement (authored escape vias inside a
# driver IC's own solder lands) was routed silently. session.placement_drc_*
# is the single shared builder both jobs now call BEFORE routing, so the two
# call sites cannot drift on HOW the pre-route check is framed.


def _two_pin_two_net_graph():
    # U1/1 on net NA, U2/1 on net NB, ten mm apart — pads present, nothing
    # routed. build_ir gives U1 a pad at the origin, which the fixed via
    # below is deliberately drilled straight into.
    return {
        "board": {"board_id": 1, "stackup": DEFAULT_STACKUP},
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0, "rot": 0.0},
            {"refdes": "U2", "x": 10.0, "y": 0.0, "rot": 0.0},
        ],
        "nets": [
            {"name": "NA", "members": [{"refdes": "U1", "pin": "1"}]},
            {"name": "NB", "members": [{"refdes": "U2", "pin": "1"}]},
        ],
    }


def test_placement_drc_flags_fixed_via_drilled_into_foreign_pad():
    from precis.pcb.capabilities import capability_for
    from precis.pcb.session import placement_drc_findings, placement_drc_report

    ir = build_ir(_two_pin_two_net_graph())
    # An authored escape via on NB sitting exactly on U1/1 (net NA) — the
    # gr451052 shape: a via inside a foreign net's solder land, geometry that
    # exists BEFORE a single trace is routed.
    via = {
        "ctype": "via",
        "net": "NB",
        "x": 0.0,
        "y": 0.0,
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        "layers": ["F.Cu"],
        "fixed": True,
    }
    findings = placement_drc_findings(
        ir, capability=capability_for("4layer"), fixed_copper=[via]
    )
    rules = {f.rule for f in findings if f.severity == "error"}
    assert "via_pad_keepout" in rules  # the named gr451052 rule

    n_errors, block = placement_drc_report(findings)
    assert n_errors >= 1
    assert block is not None
    # A NAMED account (rule + where + margin), not just a bit flipped.
    assert "via_pad_keepout" in block
    assert "unmanufacturable as placed" in block
    assert "before a single trace is routed" in block


def test_placement_drc_exempts_same_net_authored_via():
    # check_via_pad_keepout's same-net authored-via exemption: an escape via
    # on the SAME net as the pad it lands on is legal (that is what an escape
    # via IS) — the gate must not cry wolf over a board's own via stitching.
    from precis.pcb.capabilities import capability_for
    from precis.pcb.session import placement_drc_findings

    ir = build_ir(_two_pin_two_net_graph())
    via_same_net = {
        "ctype": "via",
        "net": "NA",
        "x": 0.0,
        "y": 0.0,
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        "layers": ["F.Cu"],
        "fixed": True,
    }
    findings = placement_drc_findings(
        ir, capability=capability_for("4layer"), fixed_copper=[via_same_net]
    )
    keepout = [f for f in findings if f.rule == "via_pad_keepout"]
    assert keepout == []


def test_placement_drc_report_is_silent_when_clean():
    # A clean placement subset returns (0, None) — deliberately NO reassuring
    # "clean" line, because this is a SUBSET check that never looked at trace
    # width / connectivity, so "clean" would overclaim (handlers/pcb.py's
    # pads-only DRC note guards the same way).
    from precis.pcb.session import placement_drc_report

    n_errors, block = placement_drc_report([])
    assert n_errors == 0
    assert block is None


def test_run_placement_drc_is_a_subset_of_run_geometric_drc():
    # The subset contract: run_placement_drc fires only rules that exist
    # before routing. On an unrouted 2-pin net, run_geometric_drc raises
    # 'connectivity' (a router-output rule) while run_placement_drc — which
    # never looks at connectivity/unrouted/trace_width/annular_ring — stays
    # silent, so acting on the early gate can only ever AGREE with the later
    # full run, never fire something it would not.
    from precis.pcb import drc, realize
    from precis.pcb.capabilities import capability_for

    graph = {
        "board": {"board_id": 1, "stackup": DEFAULT_STACKUP},
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0, "rot": 0.0},
            {"refdes": "U2", "x": 10.0, "y": 0.0, "rot": 0.0},
        ],
        "nets": [
            {
                "name": "NET1",
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "U2", "pin": "1"},
                ],
            },
        ],
    }
    ir = build_ir(graph)
    layers = [str(layer.get("name")) for layer in ir.stackup]
    model = {
        "layers": layers,
        "pads": realize.pads_for_ir(ir, layers, None),
        "copper": [],
    }
    cap = capability_for("4layer")
    geometric_rules = {f.rule for f in drc.run_geometric_drc(model, capability=cap)}
    placement_rules = {f.rule for f in drc.run_placement_drc(model, capability=cap)}
    # placement is a strict subset here — the router-only rule is dropped.
    assert placement_rules <= geometric_rules
    assert "connectivity" in geometric_rules
    assert "connectivity" not in placement_rules
    # None of the router-output rules may ever ride the placement subset.
    router_only = {
        "connectivity",
        "unrouted",
        "trace_width",
        "annular_ring",
        "silk",
    }
    assert not (placement_rules & router_only)
