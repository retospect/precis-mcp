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
                "geom": {"diameter": 5.6, "ring_dia_mm": 8.0, "plated": True},
            },
            # malformed / degenerate rows must be skipped, never crash
            {"ftype": "mounting_hole", "x": 1.0, "y": 1.0, "geom": {}},
            {"ftype": "mounting_hole", "x": "oops", "y": 1.0, "geom": {"diameter": 3}},
        ]
    )
    assert [(h.x, h.y, h.drill_mm, h.ring_dia_mm, h.plated) for h in holes] == [
        (4.0, 4.0, 4.3, 0.0, False),
        (6.0, 6.0, 5.6, 8.0, True),
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
