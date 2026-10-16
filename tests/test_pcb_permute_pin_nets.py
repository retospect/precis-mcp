"""Cross-instance net moves through a resolved netlist snapshot
(``pcb.resolved.apply``), and ``swap_pins`` staying same-instance."""

from __future__ import annotations

import numpy as np
import pytest

from precis.pcb import DEFAULT_STACKUP, resolved
from precis.pcb.ir import from_graph


def _ir():
    return from_graph(_graph(), stackup=DEFAULT_STACKUP)


def _graph():
    members = [("S0", "H1"), ("S0", "H2"), ("S1", "H1"), ("S1", "H2")]
    return {
        "instances": [
            {"refdes": "S0", "x": 0.0, "y": 0.0},
            {"refdes": "S1", "x": 20.0, "y": 0.0},
            *[{"refdes": f"E{i}", "x": 5.0 * i, "y": 10.0} for i in range(4)],
        ],
        "nets": [
            {
                "name": f"N{i}",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": f"E{i}", "pin": "1"},
                    {"refdes": members[i][0], "pin": members[i][1]},
                ],
            }
            for i in range(4)
        ],
    }


def _pin(ir, refdes, label):
    for p in range(ir.n_pins):
        if (
            str(ir.instance_refdes[int(ir.pin_instance[p])]) == refdes
            and str(ir.pin_label[p]) == label
        ):
            return p
    raise KeyError((refdes, label))


def _net(ir, name):
    return [str(n) for n in ir.net_name].index(name)


def _check_consistent(ir):
    for s in range(ir.n_segments):
        for pin in (int(ir.seg_pin_a[s]), int(ir.seg_pin_b[s])):
            assert int(ir.pin_net[pin]) == int(ir.seg_net[s])
    expect: dict[int, list[int]] = {}
    for s in range(ir.n_segments):
        for pin in (ir.seg_pin_a[s], ir.seg_pin_b[s]):
            expect.setdefault(int(ir.pin_instance[pin]), []).append(s)
    got = {k: sorted(v) for k, v in ir._segs_of_instance.items() if v}
    assert got == {k: sorted(v) for k, v in expect.items()}


def test_swap_pins_still_refuses_cross_instance():
    ir = _ir()
    with pytest.raises(ValueError):
        ir.swap_pins(_pin(ir, "S0", "H1"), _pin(ir, "S1", "H1"))


def _applied(entries):
    """A fresh IR built from the graph with a netlist snapshot laid over
    it (the replacement for replaying pin-swap override rows)."""
    graph = _graph()
    declared = resolved.declared_pin_nets(graph)
    snapshot = {
        "pins": [
            {"refdes": r, "pin": p, "net": net, "declared": declared[(r, p)]}
            for (r, p), net in entries.items()
        ]
    }
    return from_graph(resolved.apply(graph, snapshot), stackup=DEFAULT_STACKUP)


def test_snapshot_replays_cross_instance_permutation():
    ref = _ir()
    fresh = _applied({("S0", "H1"): "N2", ("S1", "H1"): "N0"})
    # pin ids are the authored ones; only the two moved pins differ
    assert int(fresh.pin_net[_pin(fresh, "S0", "H1")]) == _net(fresh, "N2")
    assert int(fresh.pin_net[_pin(fresh, "S1", "H1")]) == _net(fresh, "N0")
    moved = {_pin(ref, "S0", "H1"), _pin(ref, "S1", "H1")}
    keep = [p for p in range(ref.n_pins) if p not in moved]
    assert np.array_equal(fresh.pin_net[keep], ref.pin_net[keep])
    _check_consistent(fresh)


def test_snapshot_same_instance_swap_still_works():
    fresh = _applied({("S0", "H1"): "N1", ("S0", "H2"): "N0"})
    assert int(fresh.pin_net[_pin(fresh, "S0", "H1")]) == _net(fresh, "N1")
    assert int(fresh.pin_net[_pin(fresh, "S0", "H2")]) == _net(fresh, "N0")
    _check_consistent(fresh)
