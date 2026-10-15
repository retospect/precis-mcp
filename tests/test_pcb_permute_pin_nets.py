"""PcbIR.permute_pin_nets: cross-instance net moves + their persistence replay."""

from __future__ import annotations

import numpy as np
import pytest

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import from_graph
from precis.pcb.session import apply_pin_swap_overrides


def _ir():
    members = [("S0", "H1"), ("S0", "H2"), ("S1", "H1"), ("S1", "H2")]
    graph = {
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
    return from_graph(graph, stackup=DEFAULT_STACKUP)


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


def _target(ir):
    return {
        _pin(ir, "S0", "H1"): _net(ir, "N2"),
        _pin(ir, "S1", "H1"): _net(ir, "N0"),
    }


def test_permute_moves_net_across_instances():
    ir = _ir()
    _check_consistent(ir)
    ir.dirty_l2[:] = False
    tgt = _target(ir)
    ir.permute_pin_nets(tgt)
    for p, n in tgt.items():
        assert int(ir.pin_net[p]) == n
    _check_consistent(ir)
    assert ir.dirty_l2.any()


def test_permute_rejects_non_permutation_without_mutating():
    ir = _ir()
    before = (
        ir.pin_net.copy(),
        ir.seg_pin_a.copy(),
        ir.seg_pin_b.copy(),
        {k: list(v) for k, v in ir._segs_of_instance.items()},
    )
    bad = {_pin(ir, "S0", "H1"): _net(ir, "N3"), _pin(ir, "S1", "H1"): _net(ir, "N0")}
    with pytest.raises(ValueError):
        ir.permute_pin_nets(bad)
    assert np.array_equal(ir.pin_net, before[0])
    assert np.array_equal(ir.seg_pin_a, before[1])
    assert np.array_equal(ir.seg_pin_b, before[2])
    assert ir._segs_of_instance == before[3]


def test_swap_pins_still_refuses_cross_instance():
    ir = _ir()
    with pytest.raises(ValueError):
        ir.swap_pins(_pin(ir, "S0", "H1"), _pin(ir, "S1", "H1"))


def test_overrides_replay_cross_instance_permutation():
    ref = _ir()
    ref.permute_pin_nets(_target(ref))
    rows = [
        {"refdes": "S0", "pin": "H1", "net": "N2"},
        {"refdes": "S1", "pin": "H1", "net": "N0"},
    ]
    fresh = _ir()
    apply_pin_swap_overrides(fresh, rows)
    assert np.array_equal(fresh.pin_net, ref.pin_net)
    _check_consistent(fresh)


def test_overrides_same_instance_swap_still_works():
    fresh = _ir()
    apply_pin_swap_overrides(
        fresh,
        [
            {"refdes": "S0", "pin": "H1", "net": "N1"},
            {"refdes": "S0", "pin": "H2", "net": "N0"},
        ],
    )
    assert int(fresh.pin_net[_pin(fresh, "S0", "H1")]) == _net(fresh, "N1")
    assert int(fresh.pin_net[_pin(fresh, "S0", "H2")]) == _net(fresh, "N0")
    _check_consistent(fresh)
