"""Support-passive groups — netlist detection, row layout, and the
frozen-parts guarantee (docs/backlog/pcb-placement-groups.md).

The detection cases run on a small hand netlist covering every shape the
pass recognises — a pull-up bank on a shared rail, a per-IC decoupling
pair, a flying (charge-pump) cap pair, and an LED + resistor pair —
plus the things it must NOT recognise (a resistor frozen by the author,
a parallel RC on the same two nets, a dangling passive). The layout
case asserts the row's actual geometry: members aligned on one line,
uniform rotation, rail pads sharing the row's +Y side. The frozen case
asserts pose-for-pose preservation through ``optimize`` itself.
"""

from __future__ import annotations

import itertools
import random

import pytest

from precis.pcb import landpattern, support_groups
from precis.pcb.cost import COURTYARD_MIN_SEPARATION_MM
from precis.pcb.ir import (
    COURTYARD_CLEARANCE_MM,
    courtyard_bound_radius_mm,
    from_graph,
    instance_courtyard_polygons,
)
from precis.pcb.optimize import (
    OptimizeConfig,
    optimize,
    seed_placement,
)


def _graph() -> dict:
    """The hand netlist: two ICs on a board, an I2C pull-up bank to one
    rail (plus an EN pull-up that shares only the rail — it joins the
    bank by the hub-overlap rule only if it merges with the SCL/SDA
    members' served sets, and a FROZEN R8 that must never move), a
    dedicated-bypass cap pair on U1, a flying charge-pump cap pair on
    U2, an LED + resistor pair, and a parallel R+C on BOOT that must
    never become a series element."""
    return {
        "board": {"stackup": [{"name": "F.Cu"}, {"name": "B.Cu"}]},
        "instances": [
            {"refdes": "U1", "x": 10.0, "y": 10.0, "rot": 0.0},
            {"refdes": "U2", "x": 40.0, "y": 10.0, "rot": 0.0},
            {"refdes": "R1"},
            {"refdes": "R2"},
            {"refdes": "R3"},
            {"refdes": "R8", "x": 5.0, "y": 5.0, "fixed": "both"},
            {"refdes": "C1"},
            {"refdes": "C2"},
            {"refdes": "C3"},
            {"refdes": "C4"},
            {"refdes": "D1"},
            {"refdes": "R9"},
            {"refdes": "R10"},
            {"refdes": "C5"},
        ],
        "nets": [
            {
                "name": "3V3",
                "members": [
                    {"refdes": "U1", "pin": "VCC"},
                    {"refdes": "U2", "pin": "VCC"},
                    {"refdes": "R1", "pin": "2"},
                    {"refdes": "R2", "pin": "2"},
                    {"refdes": "R3", "pin": "2"},
                    {"refdes": "R8", "pin": "2"},
                    {"refdes": "R9", "pin": "2"},
                    {"refdes": "C2", "pin": "1"},
                ],
            },
            {
                "name": "SCL",
                "members": [
                    {"refdes": "U1", "pin": "SCL"},
                    {"refdes": "U2", "pin": "SCL"},
                    {"refdes": "R1", "pin": "1"},
                ],
            },
            {
                "name": "SDA",
                "members": [
                    {"refdes": "U1", "pin": "SDA"},
                    {"refdes": "U2", "pin": "SDA"},
                    {"refdes": "R2", "pin": "1"},
                ],
            },
            {
                "name": "EN",
                "members": [
                    {"refdes": "U1", "pin": "EN"},
                    {"refdes": "R3", "pin": "1"},
                ],
            },
            {
                "name": "B8",
                "members": [
                    {"refdes": "U2", "pin": "B8"},
                    {"refdes": "R8", "pin": "1"},
                ],
            },
            # Dedicated bypass: each net is the cap plus exactly one U1 pin.
            {
                "name": "U1_P",
                "members": [
                    {"refdes": "U1", "pin": "P"},
                    {"refdes": "C1", "pin": "1"},
                ],
            },
            {
                "name": "U1_Q",
                "members": [
                    {"refdes": "U1", "pin": "Q"},
                    {"refdes": "C2", "pin": "2"},
                ],
            },
            # Flying (charge-pump) caps: both ends on the same IC.
            {
                "name": "U2_A",
                "members": [
                    {"refdes": "U2", "pin": "A"},
                    {"refdes": "C3", "pin": "1"},
                ],
            },
            {
                "name": "U2_B",
                "members": [
                    {"refdes": "U2", "pin": "B"},
                    {"refdes": "C3", "pin": "2"},
                ],
            },
            {
                "name": "U2_C",
                "members": [
                    {"refdes": "U2", "pin": "C"},
                    {"refdes": "C4", "pin": "1"},
                ],
            },
            {
                "name": "U2_D",
                "members": [
                    {"refdes": "U2", "pin": "D"},
                    {"refdes": "C4", "pin": "2"},
                ],
            },
            {
                "name": "GND",
                "members": [
                    {"refdes": "U1", "pin": "GND"},
                    {"refdes": "U2", "pin": "GND"},
                    {"refdes": "C1", "pin": "2"},
                    {"refdes": "D1", "pin": "K"},
                    {"refdes": "R10", "pin": "2"},
                    {"refdes": "C5", "pin": "2"},
                ],
            },
            # LED + series resistor: D1/R9 share only LEDL.
            {
                "name": "LEDL",
                "members": [
                    {"refdes": "D1", "pin": "A"},
                    {"refdes": "R9", "pin": "1"},
                ],
            },
            # Parallel RC on BOOT: R10+C5 share BOOT *and* GND — not a
            # series element, must not be grouped.
            {
                "name": "BOOT",
                "members": [
                    {"refdes": "U1", "pin": "BOOT"},
                    {"refdes": "R10", "pin": "1"},
                    {"refdes": "C5", "pin": "1"},
                ],
            },
        ],
    }


def _ir():
    graph = _graph()
    ir = from_graph(graph, stackup=graph["board"]["stackup"])
    # Give the served pins a deterministic package-edge position —
    # the edge bucketing (and therefore the row shapes) is then fixed,
    # instead of depending on the synthesized pad order for a "U2"
    # label. U1's P/Q/BOOT all on the top edge (y +2.7, x within the
    # split threshold); U2's A-D likewise.
    by_refdes = {str(ir.instance_refdes[i]): i for i in range(ir.n_instances)}
    labels = {
        (by_refdes["U1"], "P"): (-2.0, 2.7),
        (by_refdes["U1"], "Q"): (-0.5, 2.7),
        (by_refdes["U1"], "BOOT"): (1.0, 2.7),
        (by_refdes["U2"], "A"): (-2.0, 2.7),
        (by_refdes["U2"], "B"): (-0.8, 2.7),
        (by_refdes["U2"], "C"): (0.8, 2.7),
        (by_refdes["U2"], "D"): (2.0, 2.7),
    }
    for p in range(ir.n_pins):
        key = (int(ir.pin_instance[p]), str(ir.pin_label[p]))
        if key in labels:
            ir.set_pin_offset(p, *labels[key])
    seed_placement(ir, random.Random(0))
    return ir


def _by_refdes(groups, ir):
    out: dict[str, list[tuple[str, ...]]] = {}
    for g in groups:
        out.setdefault(g.kind, []).append(
            tuple(str(ir.instance_refdes[m]) for m in g.members)
        )
    return out


def test_detects_series_led_pair_decoupling_and_rail_bank() -> None:
    ir = _ir()
    groups = support_groups.detect_support_groups(ir)
    by_kind = _by_refdes(groups, ir)

    series = by_kind.get("series_pair", [])
    assert ("D1", "R9") in series or ("R9", "D1") in series, series
    # The parallel R+C on BOOT/GND is NOT a series element.
    assert not any({"R10", "C5"} == set(m) for m in series), series

    caps = by_kind.get("support_cap", [])
    # The flying charge-pump pair on U2 (A/B/C/D pins) becomes a row,
    # and U1's three bypass caps (C1 on P, C2 on Q, C5 bypassing BOOT
    # to GND) become one — the test stamps P/Q/BOOT onto the same
    # package edge (see ``_ir``).
    assert ("C3", "C4") in caps, caps
    assert ("C1", "C2", "C5") in caps, caps
    owners = {
        frozenset(str(ir.instance_refdes[m]) for m in g.members): g.anchor_inst
        for g in groups
        if g.kind == "support_cap"
    }
    assert str(ir.instance_refdes[owners[frozenset({"C1", "C2", "C5"})]]) == "U1"
    assert str(ir.instance_refdes[owners[frozenset({"C3", "C4"})]]) == "U2"

    banks = by_kind.get("rail_bank", [])
    members = {frozenset(m) for m in banks}
    # R1/R2/R3 hang off 3V3; R8 also does but it is FIXED — it can never
    # be a member, so no bank may contain it.
    assert not any("R8" in m for m in members)
    assert any({"R1", "R2", "R3"} <= set(m) for m in members), banks


def test_frozen_and_parallel_parts_never_grouped() -> None:
    ir = _ir()
    groups = support_groups.detect_support_groups(ir)
    members = {m for g in groups for m in g.members}
    refdes_of = {m: str(ir.instance_refdes[m]) for m in members}
    # R8 is fixed='both'. R10 is the pull-down half of a parallel RC on
    # BOOT (it shares BOOT AND GND with C5) — no series element, and as
    # the only resistor on GND no rail bank claims it either.
    assert "R8" not in refdes_of.values()
    assert "R10" not in refdes_of.values()


def test_row_layout_aligns_members_and_shares_a_rail_line() -> None:
    ir = _ir()
    groups = support_groups.detect_support_groups(ir)
    radii = courtyard_bound_radius_mm(
        instance_courtyard_polygons(
            ir,
            clearance_mm=COURTYARD_CLEARANCE_MM,
            fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
        )
    )
    bank = next(g for g in groups if g.kind == "rail_bank")
    offsets, ax, ay, rots = support_groups.layout_group(ir, bank, radii)

    # Uniform pitch: the sorted dx gaps are all equal.
    dxs = sorted(dx for dx, _dy, _r in offsets)
    for a, b in itertools.pairwise(dxs):
        assert b - a == pytest.approx(dxs[1] - dxs[0], abs=1e-9)

    # All shared (rail) pads sit on the row's +Y side in the group
    # frame — one line, so a single trace serves the row.
    shared_ys = []
    for m, sp, (dx, _dy, drot) in zip(bank.members, bank.shared_pin, offsets):
        pad_x, pad_y = landpattern.rotate_offset(
            float(ir.pin_dx[sp]),
            float(ir.pin_dy[sp]),
            drot,
            mirrored=bool(ir.inst_bottom[m]),
        )
        assert pad_y > 0 and abs(pad_y) >= abs(pad_x), (m, pad_x, pad_y)
        shared_ys.append(pad_y)
    for y in shared_ys[1:]:
        assert y == pytest.approx(shared_ys[0], abs=1e-6)

    # Identical pads get identical member rotations — a uniform row.
    assert len({(r[2]) % 360 for r in offsets}) == 1


def test_optimize_groups_keeps_frozen_poses_exact() -> None:
    g = _graph()
    ir = from_graph(g, stackup=g["board"]["stackup"])
    before = {
        i: (
            float(ir.inst_x[i]),
            float(ir.inst_y[i]),
            float(ir.inst_rot[i]),
        )
        for i in range(ir.n_instances)
        if bool(ir.inst_fixed_xy[i]) or bool(ir.inst_fixed_rot[i])
    }
    assert before, "the graph's fixed instance must exist for this test"
    res = optimize(ir, OptimizeConfig(iters=300, seed=1, support_groups=True))
    after = {
        i: (float(ir.inst_x[i]), float(ir.inst_y[i]), float(ir.inst_rot[i]))
        for i in before
    }
    assert before == after, (before, after)
    # And detection itself found the groups (R8 stayed out of them).
    assert res.support_groups


def test_groups_param_defaults_off_and_is_opt_in() -> None:
    g = _graph()
    ir = from_graph(g, stackup=g["board"]["stackup"])
    res = optimize(ir, OptimizeConfig(iters=100, seed=1))
    assert res.support_groups == ()

    ir2 = from_graph(g, stackup=g["board"]["stackup"])
    res2 = optimize(ir2, OptimizeConfig(iters=100, seed=1, support_groups=True))
    assert res2.support_groups != ()
