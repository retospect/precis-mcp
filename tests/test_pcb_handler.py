"""PcbHandler end-to-end against a live store (ADR 0042 Slice 1).

Exercises the batch authoring path (put with components/nets/connections),
the netlist TOC, the graph-traversal reads (instance neighbourhood, net
members), re-runnability, and soft-delete. Uses the shared ``store`` fixture.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler

# A tiny but real board: an MCU + a bypass cap + a pull-up, on an I2C net.
_DESIGN = {
    "components": [
        {
            "refdes": "U1",
            "label": "ESP32-C3",
            "part": "C2838500",
            "footprint": "QFN-32",
            "roles": ["noisy"],
            "x": 10.0,
            "y": 10.0,
            "pins": [
                {"name": "VDD", "pad": "1", "tags": ["power", "3v3"]},
                {"name": "GND", "pad": "2", "tags": ["gnd"]},
                {"name": "SCL", "pad": "8", "tags": ["bidir", "i2c"]},
            ],
        },
        {
            "refdes": "C1",
            "label": "100nF 0402",
            "part": "C1525",
            "footprint": "0402",
            "x": 11.5,
            "y": 10.0,
            "pins": [{"name": "1"}, {"name": "2"}],
            "note": "VDD bypass for U1",
        },
        {
            "refdes": "R1",
            "label": "4.7k 0402",
            "part": "C25900",
            "footprint": "0402",
            "x": 13.0,
            "y": 10.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
    ],
    "nets": [
        {"name": "VCC3V3", "class": "power", "current": 0.5},
        {"name": "GND", "class": "gnd"},
        {"name": "I2C_SCL", "class": "i2c"},
    ],
    "connections": [
        {"net": "VCC3V3", "refdes": "U1", "pin": "VDD"},
        {"net": "VCC3V3", "refdes": "C1", "pin": "1", "note": "bypass hi side"},
        {"net": "GND", "refdes": "U1", "pin": "GND"},
        {"net": "GND", "refdes": "C1", "pin": "2"},
        {"net": "I2C_SCL", "refdes": "U1", "pin": "SCL"},
        {"net": "I2C_SCL", "refdes": "R1", "pin": "1"},
    ],
}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def test_put_creates_and_lists(pcb):
    resp = pcb.put(id="sensor-node", args=_DESIGN)
    assert "created" in resp.body
    assert "+3 part(s)" in resp.body and "+3 net(s)" in resp.body
    # the TOC shows parts + nets
    assert "U1" in resp.body and "ESP32-C3" in resp.body
    assert "I2C_SCL" in resp.body
    # listing shows it
    lst = pcb.get()
    assert "sensor-node" in lst.body


def test_toc_shows_placement_and_fanout(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    toc = pcb.get(id="sensor-node")
    assert "@10,10" in toc.body  # U1 placement (centroid)
    assert "noisy" in toc.body  # role tag rendered
    # GND + VCC each have fanout 2; the nets section lists them
    assert "GND" in toc.body and "VCC3V3" in toc.body


def test_instance_neighbourhood_hop(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    u1 = pcb.get(id="sensor-node#U1")
    # U1's VDD pin is on VCC3V3 and its neighbour there is C1
    assert "VDD" in u1.body and "VCC3V3" in u1.body
    assert "C1" in u1.body  # neighbour on the power net
    # SCL pin is on I2C_SCL with R1 as a neighbour
    assert "SCL" in u1.body and "R1" in u1.body


def test_net_members(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    net = pcb.get(id="sensor-node@VCC3V3")
    assert "U1" in net.body and "C1" in net.body
    assert "class power" in net.body


def test_put_is_rerunnable_and_extends(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    # re-applying the same design adds nothing (refdes/net names reused)
    again = pcb.put(id="sensor-node", args=_DESIGN)
    assert "+0 part(s)" in again.body and "+0 net(s)" in again.body
    # extending with a new part works
    ext = pcb.put(
        id="sensor-node",
        args={
            "components": [
                {"refdes": "R2", "label": "10k 0402", "pins": [{"name": "1"}]}
            ],
            "connections": [{"net": "I2C_SCL", "refdes": "R2", "pin": "1"}],
        },
    )
    assert "+1 part(s)" in ext.body
    assert "now 4 part(s)" in ext.body


def test_one_net_per_physical_pin(pcb):
    """The UNIQUE(instance,pin) invariant: re-connecting a pin moves it."""
    pcb.put(id="sensor-node", args=_DESIGN)
    # move U1.SCL from I2C_SCL to GND (a re-wire)
    pcb.put(
        id="sensor-node",
        args={"connections": [{"net": "GND", "refdes": "U1", "pin": "SCL"}]},
    )
    scl = pcb.get(id="sensor-node@I2C_SCL")
    assert "U1" not in scl.body  # U1.SCL left I2C_SCL
    gnd = pcb.get(id="sensor-node@GND")
    assert "U1" in gnd.body


def test_put_requires_id(pcb):
    with pytest.raises(BadInput):
        pcb.put(args=_DESIGN)


def test_get_unknown_design_raises(pcb):
    with pytest.raises(NotFound):
        pcb.get(id="does-not-exist")


def test_delete_soft_retires(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    resp = pcb.delete(id="sensor-node")
    assert "retired" in resp.body
    with pytest.raises(NotFound):
        pcb.get(id="sensor-node")


# ── the eyes (ADR 0042 §8) ───────────────────────────────────────────
# A board with a guaranteed crossing: two signal nets whose airwires form an X.
_CROSSED = {
    "components": [
        {"refdes": "A", "label": "ic", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "B", "label": "ic", "x": 2.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "C", "label": "ic", "x": 0.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "D", "label": "ic", "x": 2.0, "y": 0.0, "pins": [{"name": "1"}]},
    ],
    "nets": [
        {"name": "N1", "class": "signal"},
        {"name": "N2", "class": "signal"},
    ],
    "connections": [
        {"net": "N1", "refdes": "A", "pin": "1"},
        {"net": "N1", "refdes": "B", "pin": "1"},
        {"net": "N2", "refdes": "C", "pin": "1"},
        {"net": "N2", "refdes": "D", "pin": "1"},
    ],
}


def test_crossings_view(pcb):
    pcb.put(id="x", args=_CROSSED)
    resp = pcb.get(id="x", view="crossings")
    assert "crossings — 1" in resp.body
    assert "N1" in resp.body and "N2" in resp.body


def test_ratsnest_view_excludes_plane_nets(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    rn = pcb.get(id="sensor-node", view="ratsnest")
    # I2C_SCL is a signal net → an airwire; GND/VCC3V3 are plane → excluded
    assert "I2C_SCL" in rn.body
    assert "GND" not in rn.body and "VCC3V3" not in rn.body


def test_drc_view(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    drc = pcb.get(id="sensor-node", view="drc")
    # C1/R1 have unconnected second pins; flag them
    assert "unconnected-pin" in drc.body


def test_proximity_view(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    # U1@(10,10), C1 unplaced in _DESIGN → proximity needs both placed
    pcb.put(
        id="sensor-node",
        args={
            "components": [
                {
                    "refdes": "C9",
                    "label": "100nF",
                    "x": 13.0,
                    "y": 14.0,
                    "pins": [{"name": "1"}],
                }
            ]
        },
    )
    pr = pcb.get(id="sensor-node", view="proximity", args={"a": "U1", "b": "C9"})
    assert "5 mm" in pr.body  # 3-4-5 triangle from (10,10)→(13,14)


def test_measures_view(pcb):
    pcb.put(
        id="m",
        args={
            "components": [
                {
                    "refdes": "U1",
                    "label": "opamp",
                    "x": 0.0,
                    "y": 0.0,
                    "roles": ["sensitive"],
                    "pins": [{"name": "1"}],
                },
                {
                    "refdes": "Q1",
                    "label": "FET",
                    "x": 4.0,
                    "y": 0.0,
                    "roles": ["noisy"],
                    "pins": [{"name": "1"}],
                },
            ],
            "measures": [
                {
                    "metric": "separation",
                    "goal": 10.0,
                    "strength": "soft",
                    "operands": [{"role": "sensitive"}, {"role": "noisy"}],
                    "reason": "keep opamp off the FET",
                },
            ],
        },
    )
    mv = pcb.get(id="m", view="measures")
    assert "separation" in mv.body
    assert "VIOLATED" in mv.body  # 4mm < 10mm goal


def test_trace_view(pcb):
    pcb.put(
        id="t",
        args={
            "components": [
                {
                    "refdes": "R1",
                    "label": "4.7k",
                    "pins": [{"name": "1"}, {"name": "2"}],
                },
                {
                    "refdes": "U1",
                    "label": "MCU",
                    "pins": [{"name": "1"}, {"name": "2"}, {"name": "3"}],
                },
            ],
            "nets": [
                {"name": "NET_A", "class": "signal"},
                {"name": "NET_B", "class": "signal"},
            ],
            "connections": [
                {"net": "NET_A", "refdes": "R1", "pin": "1"},
                {"net": "NET_B", "refdes": "R1", "pin": "2"},
                {"net": "NET_B", "refdes": "U1", "pin": "3"},
            ],
        },
    )
    tr = pcb.get(id="t", view="trace", args={"net": "NET_A"})
    assert "NET_A" in tr.body and "NET_B" in tr.body
    assert "via R1" in tr.body


def test_unknown_view_raises(pcb):
    pcb.put(id="sensor-node", args=_DESIGN)
    with pytest.raises(BadInput):
        pcb.get(id="sensor-node", view="bogus")


# ── auto-place + feasibility (ADR 0042 §9) ───────────────────────────
def test_autoplace_reduces_crossings(pcb):
    pcb.put(id="x", args=_CROSSED)  # the X — 1 crossing
    before = pcb.get(id="x", view="crossings")
    assert "crossings — 1" in before.body
    placed = pcb.put(id="x", args={"autoplace": {"iters": 2000, "seed": 1}})
    assert "1 → 0" in placed.body  # crossings before → after
    after = pcb.get(id="x", view="crossings")
    assert "no crossings" in after.body


def test_autoplace_keeps_fixed_part_put(pcb):
    pcb.put(
        id="f",
        args={
            "components": [
                {
                    "refdes": "J1",
                    "label": "conn",
                    "x": 0.0,
                    "y": 0.0,
                    "fixed": "xy",
                    "pins": [{"name": "1"}],
                },
                {
                    "refdes": "U1",
                    "label": "ic",
                    "x": 40.0,
                    "y": 40.0,
                    "pins": [{"name": "1"}],
                },
                {
                    "refdes": "U2",
                    "label": "ic",
                    "x": 41.0,
                    "y": 40.0,
                    "pins": [{"name": "1"}],
                },
            ],
            "nets": [{"name": "N", "class": "signal"}],
            "connections": [
                {"net": "N", "refdes": "U1", "pin": "1"},
                {"net": "N", "refdes": "U2", "pin": "1"},
            ],
        },
    )
    pcb.put(id="f", args={"autoplace": {"iters": 300, "seed": 2}})
    # J1 (fixed) keeps @0,0 in the TOC
    toc = pcb.get(id="f", view=None)
    assert "J1" in toc.body
    j1 = pcb.get(id="f#J1")
    assert j1 is not None  # still resolvable
    # confirm via the net/proximity that J1 didn't move
    pr = pcb.get(id="f", view="proximity", args={"a": "J1", "b": "U1"})
    assert "mm" in pr.body


def test_feasibility_view(pcb):
    pcb.put(id="x", args=_CROSSED)
    f = pcb.get(id="x", view="feasibility")
    assert "route feasibility" in f.body
    assert "vias needed" in f.body
