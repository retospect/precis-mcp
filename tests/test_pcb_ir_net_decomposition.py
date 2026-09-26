"""Segment decomposition picks its hub by board side (td450119).

The old decomposition made a net's VIA count depend on which member the
author happened to write first: ``J_INSTR -> U_TEMP -> R_BLEED`` needed two
side changes when the bottom-side member led the member list and one when it
did not. These tests pin the fix at the level that actually costs copper —
how many segments have endpoints on opposite board sides — rather than
pinning one particular hub, which would re-create the "fixture pins one
arbitrary tie-break" trap.

The decomposition stays a STAR. A spanning tree (option (a), strictly better
on via count) was built, measured, and reverted: it regressed the ESP32-C3
acceptance board to 2-4 connectivity DRC errors at every seed, because the
router relies on every segment of a net sharing the hub pad.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.pcb.ir import from_graph


def _cross_side_segments(ir: Any) -> int:
    return sum(
        1
        for s in range(ir.n_segments)
        if bool(ir.inst_bottom[ir.pin_instance[int(ir.seg_pin_a[s])]])
        != bool(ir.inst_bottom[ir.pin_instance[int(ir.seg_pin_b[s])]])
    )


def _straddling_graph(order: list[str], *, placed: bool = True) -> dict[str, Any]:
    """J_INSTR --- U_TEMP --- R_BLEED collinear, U_TEMP on the BACK.

    Deliberately asymmetric: U_TEMP sits geometrically BETWEEN the two
    top-side members, so a pure-distance tree threads it into the chain and
    pays two side changes, while the side-aware tree joins the two top
    members directly and pays one.
    """
    coords = {"J_INSTR": 0.0, "U_TEMP": 5.0, "R_BLEED": 10.0}
    instances: list[dict[str, Any]] = []
    for refdes, x in coords.items():
        inst: dict[str, Any] = {"refdes": refdes, "label": "r"}
        if placed:
            inst["x"] = x
            inst["y"] = 0.0
        if refdes == "U_TEMP":
            inst["layer"] = "bottom"
        instances.append(inst)
    return {
        "instances": instances,
        "nets": [
            {"name": "SENSE", "members": [{"refdes": r, "pin": "1"} for r in order]}
        ],
    }


_ORDERS = [
    ["U_TEMP", "J_INSTR", "R_BLEED"],
    ["J_INSTR", "U_TEMP", "R_BLEED"],
    ["R_BLEED", "U_TEMP", "J_INSTR"],
]


@pytest.mark.parametrize("order", _ORDERS)
def test_cross_side_net_needs_one_side_change_whatever_the_member_order(
    order: list[str],
) -> None:
    ir = from_graph(_straddling_graph(order))
    assert ir.n_segments == 2
    assert _cross_side_segments(ir) == 1


def test_cross_side_count_is_independent_of_member_order() -> None:
    """The VIA COUNT is the invariant, not the topology.

    Which top-side member ends up as hub is a tie the member order breaks,
    and pinning one answer would pin an arbitrary tie-break — the trap this
    package's fixtures have fallen into before. What must not vary is the
    number of segments that change board side, because that is what costs
    copper.
    """
    counts = {_cross_side_segments(from_graph(_straddling_graph(o))) for o in _ORDERS}
    assert len(counts) == 1, f"member order still changes the via count: {counts}"


def test_the_hub_sits_on_the_majority_side() -> None:
    """A star's cross-side count IS the minority size, so the hub choice is
    the whole fix. Two bottom members against one top member must root on
    the bottom, costing one crossing rather than two."""
    graph: dict[str, Any] = {
        "instances": [
            {"refdes": "T1", "label": "r", "x": 0.0, "y": 0.0},
            {"refdes": "B1", "label": "r", "x": 5.0, "y": 0.0, "layer": "bottom"},
            {"refdes": "B2", "label": "r", "x": 9.0, "y": 0.0, "layer": "bottom"},
        ],
        "nets": [
            {
                "name": "N",
                "members": [{"refdes": r, "pin": "1"} for r in ("T1", "B1", "B2")],
            }
        ],
    }
    ir = from_graph(graph)
    assert ir.n_segments == 2
    assert _cross_side_segments(ir) == 1
    hubs = {
        str(ir.instance_refdes[ir.pin_instance[int(ir.seg_pin_a[s])]])
        for s in range(ir.n_segments)
    }
    assert hubs == {"B1"}, f"hub landed off the majority side: {hubs}"


def test_every_net_is_still_a_star() -> None:
    """Load-bearing, not cosmetic: the maze router's multi-source start and
    realize's attach handling both rely on every segment of a net sharing
    one pad. A spanning tree was tried (td450119 option (a)) and regressed
    the ESP32-C3 acceptance board's connectivity DRC at every seed."""
    ir = from_graph(_straddling_graph(["J_INSTR", "U_TEMP", "R_BLEED"]))
    hubs = {int(ir.seg_pin_a[s]) for s in range(ir.n_segments)}
    assert len(hubs) == 1, f"net decomposed into a chain, not a star: {hubs}"


def test_unplaced_cross_side_net_still_avoids_the_second_side_change() -> None:
    """No geometry at all — the tree is then decided by board side alone."""
    ir = from_graph(_straddling_graph(["U_TEMP", "J_INSTR", "R_BLEED"], placed=False))
    assert ir.n_segments == 2
    assert _cross_side_segments(ir) == 1


def test_unplaced_single_side_net_keeps_the_original_star() -> None:
    """The pre-td450119 shape is preserved exactly where there is nothing to
    improve on: no positions and no side split means a star from the first
    member, byte-for-byte."""
    graph: dict[str, Any] = {
        "instances": [{"refdes": r, "label": "r"} for r in ("A1", "B1", "C1", "D1")],
        "nets": [
            {
                "name": "BUS",
                "members": [
                    {"refdes": r, "pin": "1"} for r in ("A1", "B1", "C1", "D1")
                ],
            }
        ],
    }
    ir = from_graph(graph)
    hubs = {
        str(ir.instance_refdes[ir.pin_instance[int(ir.seg_pin_a[s])]])
        for s in range(ir.n_segments)
    }
    assert ir.n_segments == 3
    assert hubs == {"A1"}


def test_a_net_is_a_tree_spanning_every_member_once() -> None:
    ir = from_graph(_straddling_graph(["J_INSTR", "U_TEMP", "R_BLEED"]))
    pins: set[int] = set()
    for s in range(ir.n_segments):
        pins.add(int(ir.seg_pin_a[s]))
        pins.add(int(ir.seg_pin_b[s]))
    assert len(pins) == 3
    assert ir.n_segments == len(pins) - 1


def test_a_net_listing_one_pin_twice_makes_no_self_segment() -> None:
    graph: dict[str, Any] = {
        "instances": [
            {"refdes": "A1", "label": "r", "x": 0.0, "y": 0.0},
            {"refdes": "B1", "label": "r", "x": 4.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "DUP",
                "members": [
                    {"refdes": "A1", "pin": "1"},
                    {"refdes": "A1", "pin": "1"},
                    {"refdes": "B1", "pin": "1"},
                ],
            }
        ],
    }
    ir = from_graph(graph)
    assert ir.n_segments == 1
    assert int(ir.seg_pin_a[0]) != int(ir.seg_pin_b[0])
