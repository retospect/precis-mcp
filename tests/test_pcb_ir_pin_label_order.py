"""Synthesized land-pattern slots are handed out by PIN LABEL, never by
pin-creation order (Reto 2026-10-31: "a pad1 is a pad1 everywhere, and
it's the same pad. Otherwise nothing works").

Measured on taz-leaf-local-61 (board 69): R12 and R13 are the same
label-only passive ("R 1M 0402 1%", rot 0, top). R12's pin "2" (net S2_A)
was created before its pin "1" (S2_DRV) because S2_A is listed first in
the netlist, so ``from_graph`` zipped pin "2" onto the −x slot and pin
"1" onto +x; R13's pin "1", created first, took −x. Same part, mirrored
pad numbering, decided by which net happened to come first — and every
consumer downstream (router, DRC, fab, viewer) faithfully agreed with it,
which is what made the stored S2_DRV copper look like a pose desync.

These tests pin the invariant per pin LABEL across the three readers a
pad position reaches: ``ir.pin_point`` (the IR), ``pads_for_ir`` (DRC/
fab/viewer) and the topoplace design's ``pin_xy`` (placer + router).
"""

from __future__ import annotations

import pytest

from precis.pcb import ir as ir_mod
from precis.pcb import realize as pcb_realize
from precis.pcb.landpattern import PASSIVE_PITCH_MM
from precis.pcb.topoplace import design_from_ir

_HALF = PASSIVE_PITCH_MM / 2.0


def _graph(*, r12_pin2_first: bool) -> dict:
    """Two label-only 0402s, R12 at x=10 and R13 at x=20. With
    ``r12_pin2_first`` the netlist lists R12.2 before R12.1 (the taz
    order); R13 is always listed pin 1 first."""
    nets_a = {
        "name": "S2_A",
        "members": [{"refdes": "R12", "pin": "2"}, {"refdes": "R13", "pin": "2"}],
    }
    nets_b = {
        "name": "S2_DRV",
        "members": [{"refdes": "R12", "pin": "1"}, {"refdes": "R13", "pin": "1"}],
    }
    return {
        "instances": [
            {
                "refdes": "R12",
                "label": "R 1M 0402 1%",
                "layer": "top",
                "x": 10.0,
                "y": 0.0,
                "rot": 0.0,
            },
            {
                "refdes": "R13",
                "label": "R 1M 0402 1%",
                "layer": "top",
                "x": 20.0,
                "y": 0.0,
                "rot": 0.0,
            },
        ],
        "nets": [nets_a, nets_b] if r12_pin2_first else [nets_b, nets_a],
    }


def _pid(ir: ir_mod.PcbIR, refdes: str, pin: str) -> int:
    for pid in range(ir.n_pins):
        if (
            str(ir.instance_refdes[int(ir.pin_instance[pid])]) == refdes
            and str(ir.pin_label[pid]) == pin
        ):
            return pid
    raise AssertionError(f"{refdes}.{pin} not in IR")


@pytest.mark.parametrize("r12_pin2_first", [True, False])
def test_pin_1_takes_the_minus_x_slot_whatever_the_netlist_order(r12_pin2_first):
    ir = ir_mod.from_graph(_graph(r12_pin2_first=r12_pin2_first))
    # the creation order really differs between the two parametrizations
    assert (_pid(ir, "R12", "2") < _pid(ir, "R12", "1")) is r12_pin2_first
    for refdes, cx in (("R12", 10.0), ("R13", 20.0)):
        p1, p2 = _pid(ir, refdes, "1"), _pid(ir, refdes, "2")
        assert float(ir.pin_dx[p1]) == pytest.approx(-_HALF)
        assert float(ir.pin_dx[p2]) == pytest.approx(+_HALF)
        assert ir_mod.pin_point(ir, p1) == pytest.approx((cx - _HALF, 0.0))
        assert ir_mod.pin_point(ir, p2) == pytest.approx((cx + _HALF, 0.0))


def test_pin_1_agrees_across_ir_pads_for_ir_and_topo_design():
    ir = ir_mod.from_graph(_graph(r12_pin2_first=True))
    layers = [str(layer.get("name")) for layer in ir.stackup]
    pads = {
        (str(p["refdes"]), str(p["pin"])): (float(p["x"]), float(p["y"]))
        for p in pcb_realize.pads_for_ir(ir, layers, None)
    }
    td = design_from_ir(ir, {})
    for refdes, cx in (("R12", 10.0), ("R13", 20.0)):
        for pin, dx in (("1", -_HALF), ("2", +_HALF)):
            pid = _pid(ir, refdes, pin)
            world = (cx + dx, 0.0)
            assert ir_mod.pin_point(ir, pid) == pytest.approx(world)
            assert pads[(refdes, pin)] == pytest.approx(world)
            assert tuple(td.pin_xy[pid]) == pytest.approx((dx, 0.0))
            assert str(td.pin_label[pid]) == pin


def test_pin_label_sort_key_is_natural_order():
    # numbers by value, connector rows A1..A12 stay rows, text after digits
    labels = ["10", "2", "EP", "1", "A12", "B1", "A4", "A1", "SHELL"]
    assert sorted(labels, key=ir_mod._pin_label_sort_key) == [
        "1",
        "2",
        "10",
        "A1",
        "A4",
        "A12",
        "B1",
        "EP",
        "SHELL",
    ]
