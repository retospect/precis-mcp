"""Pure tests for the supply/client role matcher."""

from __future__ import annotations

import itertools
import math
import random

import pytest

from precis.pcb.matching import Client, Role, Supply, solve


def _pos(table):
    return lambda r, p: table.get((r, p))


def test_more_clients_than_suppliers():
    sup = Supply("U1", "pad_driver", tuple((f"HVOUT{i}",) for i in range(64)))
    clients = [Client(f"J{i}", "1", "pad_driver") for i in range(70)]
    table = {("U1", f"HVOUT{i}"): (float(i), 0.0) for i in range(64)}
    table.update({(f"J{i}", "1"): (float(i), 1.0) for i in range(70)})
    r = solve(clients, [sup], {}, _pos(table))
    assert len(r.unsupplied) == 6
    assert len(r.assignment) == 64
    assert "6 pad_driver clients unsupplied" in r.findings


def _two_sinks():
    return [
        Supply("A", "sink", (("1",),), voltage_v=250.0),
        Supply("B", "sink", (("1",),), voltage_v=100.0),
    ]


def test_voltage_filters_suppliers():
    # B is nearer for both clients, but only A is admissible.
    table = {
        ("A", "1"): (10.0, 0.0),
        ("B", "1"): (0.0, 0.0),
        ("C1", "1"): (1.0, 0.0),
        ("C2", "1"): (2.0, 0.0),
    }
    clients = [Client("C1", "1", "sink", 250.0), Client("C2", "1", "sink", 250.0)]
    r = solve(clients, _two_sinks(), {"sink": Role("sink", fanout=2)}, _pos(table))
    assert set(r.assignment.values()) == {("A", "1")}
    assert not r.unsupplied


def test_mixed_voltage_refused():
    table = {("A", "1"): (0.0, 0.0), ("B", "1"): (1.0, 0.0), ("C1", "1"): (0.0, 1.0)}
    r = solve([Client("C1", "1", "sink")], _two_sinks(), {}, _pos(table))
    assert r.assignment == {}
    assert r.unsupplied == (("C1", "1"),)
    assert any(
        "differ in voltage" in f and f.startswith("1 sink clients") for f in r.findings
    )


def test_purity_and_prior():
    sup = Supply("U", "r", (("1",), ("2",), ("3",)))
    clients = [Client(f"C{i}", "1", "r") for i in range(3)]
    flat = lambda r, p: (0.0, 0.0)
    prior = {("C0", "1"): ("U", "3"), ("C1", "1"): ("U", "1"), ("C2", "1"): ("U", "2")}
    a = solve(clients, [sup], {}, flat, prior=prior)
    b = solve(clients, [sup], {}, flat, prior=prior)
    assert a == b
    assert a.assignment == prior


def test_optimal_vs_bruteforce():
    rng = random.Random(7)
    n = 6
    table = {}
    for i in range(n):
        table[("S", str(i))] = (rng.uniform(0, 50), rng.uniform(0, 50))
        table[(f"C{i}", "1")] = (rng.uniform(0, 50), rng.uniform(0, 50))
    sup = Supply("S", "r", tuple((str(i),) for i in range(n)))
    clients = [Client(f"C{i}", "1", "r") for i in range(n)]
    r = solve(clients, [sup], {}, _pos(table))
    best = min(
        sum(math.dist(table[(f"C{i}", "1")], table[("S", str(p[i]))]) for i in range(n))
        for p in itertools.permutations(range(n))
    )
    assert r.total_cost <= best + 1e-9
    assert not r.unsupplied


def test_multi_pin_block_raises():
    sup = Supply("U9", "r", (("1", "2"),))
    with pytest.raises(ValueError, match="U9"):
        solve([Client("C", "1", "r")], [sup], {}, lambda r, p: None)


def test_unbounded_fanout():
    sup = Supply("U", "gnd", (("1",),))
    clients = [Client(f"C{i}", "1", "gnd") for i in range(5)]
    r = solve(
        clients, [sup], {"gnd": Role("gnd", fanout=None)}, lambda r, p: (0.0, 0.0)
    )
    assert len(r.assignment) == 5
    assert set(r.assignment.values()) == {("U", "1")}
