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


def test_multi_pin_block_needs_tuple_client():
    # A block is carried whole: a single-pin client cannot split it, so
    # it is unsupplied rather than bound to one member.
    sup = Supply("U9", "r", (("1", "2"),))
    r = solve([Client("C", "1", "r")], [sup], {}, lambda r, p: None)
    assert r.assignment == {}
    assert r.unsupplied == (("C", "1"),)
    assert "1 r clients unsupplied" in r.findings


def test_tuple_client_binds_block_in_order():
    # Non-swappable: member k of the client tuple binds member k of the
    # block (declared order, polarity lock).
    sup = Supply("U9", "usb", (("DP", "DM"), ("EP", "EM")))
    clients = [
        Client("J1", "A1", "usb", members=("A1", "A2")),
        Client("J2", "B1", "usb", members=("B1", "B2")),
    ]
    table = {
        ("U9", "DP"): (0.0, 0.0),
        ("U9", "DM"): (1.0, 0.0),
        ("U9", "EP"): (20.0, 0.0),
        ("U9", "EM"): (21.0, 0.0),
        ("J1", "A1"): (0.0, 1.0),
        ("J1", "A2"): (1.0, 1.0),
        ("J2", "B1"): (20.0, 1.0),
        ("J2", "B2"): (21.0, 1.0),
    }
    r = solve(clients, [sup], {}, _pos(table))
    assert r.assignment == {
        ("J1", "A1"): ("U9", "DP"),
        ("J1", "A2"): ("U9", "DM"),
        ("J2", "B1"): ("U9", "EP"),
        ("J2", "B2"): ("U9", "EM"),
    }
    assert not r.unsupplied


def test_tuple_client_swappable_permutes():
    # within_block_swappable: the block serves the pair either way, so
    # the client lands reversed when that is cheaper.
    sup = Supply("U9", "usb", (("DP", "DM"),), within_block_swappable=True)
    clients = [Client("J1", "A1", "usb", members=("A1", "A2"))]
    table = {
        ("U9", "DP"): (0.0, 0.0),
        ("U9", "DM"): (10.0, 0.0),
        ("J1", "A1"): (10.0, 1.0),  # nearer DM
        ("J1", "A2"): (0.0, 1.0),  # nearer DP
    }
    r = solve(clients, [sup], {}, _pos(table))
    assert r.assignment == {("J1", "A1"): ("U9", "DM"), ("J1", "A2"): ("U9", "DP")}


def test_tuple_client_unsupplied_lists_members():
    # Only a size-1 block exists for a size-2 client: both member pins
    # are listed unsupplied, counted once as a client.
    sup = Supply("U9", "usb", (("DP",),))
    clients = [Client("J1", "A1", "usb", members=("A1", "A2"))]
    r = solve(clients, [sup], {}, lambda r, p: None)
    assert r.unsupplied == (("J1", "A1"), ("J1", "A2"))
    assert "1 usb clients unsupplied" in r.findings


def test_tuple_client_capacity_counts_blocks():
    sup = Supply("U9", "usb", (("DP", "DM"),))
    clients = [
        Client("J1", "A1", "usb", members=("A1", "A2")),
        Client("J2", "B1", "usb", members=("B1", "B2")),
    ]
    r = solve(clients, [sup], {}, lambda r, p: None)
    assert set(r.unsupplied) == {("J2", "B1"), ("J2", "B2")}
    assert len(r.assignment) == 2


def test_client_members_must_be_headed_by_pin():
    sup = Supply("U9", "r", (("1", "2"),))
    with pytest.raises(ValueError, match="J1/A2"):
        solve(
            [Client("J1", "A2", "r", members=("A1", "A2"))],
            [sup],
            {},
            lambda r, p: None,
        )


def test_tuple_prior_steers_swappable():
    # Identical distances; the prior picks the swapped pairing.
    sup = Supply("U9", "usb", (("DP", "DM"),), within_block_swappable=True)
    clients = [Client("J1", "A1", "usb", members=("A1", "A2"))]
    flat = lambda r, p: (0.0, 0.0)
    prior = {("J1", "A1"): ("U9", "DM"), ("J1", "A2"): ("U9", "DP")}
    r = solve(clients, [sup], {}, flat, prior=prior)
    assert r.assignment == prior


def test_unbounded_fanout():
    sup = Supply("U", "gnd", (("1",),))
    clients = [Client(f"C{i}", "1", "gnd") for i in range(5)]
    r = solve(
        clients, [sup], {"gnd": Role("gnd", fanout=None)}, lambda r, p: (0.0, 0.0)
    )
    assert len(r.assignment) == 5
    assert set(r.assignment.values()) == {("U", "1")}


def test_channel_map_binds_by_net():
    from precis.pcb.matching import ChannelRow, channel_map

    sup = Supply("U", "r", (("A",), ("B",)))
    clients = [Client("C2", "1", "r"), Client("C1", "1", "r"), Client("C3", "1", "r")]
    nets = {
        ("U", "A"): "N1",
        ("U", "B"): "N2",
        ("C1", "1"): "N1",
        ("C2", "1"): "N2",
        ("C3", "1"): None,
    }
    assert channel_map(clients, [sup], nets) == [
        ChannelRow("r", ("C1", "1"), "N1", ("U", "A")),
        ChannelRow("r", ("C2", "1"), "N2", ("U", "B")),
        ChannelRow("r", ("C3", "1"), None, None),
    ]


def test_channel_map_role_must_match():
    from precis.pcb.matching import channel_map

    sup = Supply("U", "other", (("A",),))
    nets = {("U", "A"): "N", ("C", "1"): "N"}
    rows = channel_map([Client("C", "1", "r")], [sup], nets)
    assert rows[0].supplier is None and rows[0].net == "N"


def test_channel_map_binds_two_suppliers_on_one_net():
    # Paralleled channels: two same-role terminals on the client's net
    # bind (deterministically, first sorted) instead of reading unbound.
    from precis.pcb.matching import ChannelRow, channel_map

    sup = Supply("U", "r", (("A",), ("B",)))
    nets = {("U", "A"): "N", ("U", "B"): "N", ("C", "1"): "N"}
    assert channel_map([Client("C", "1", "r")], [sup], nets) == [
        ChannelRow("r", ("C", "1"), "N", ("U", "A"))
    ]


def test_channel_map_reads_multi_pin_block_members():
    # Every member pin of a block is a terminal; a tuple client emits
    # one row per member.
    from precis.pcb.matching import ChannelRow, channel_map

    sup = Supply("U", "usb", (("DP", "DM"),))
    clients = [Client("J1", "A1", "usb", members=("A1", "A2"))]
    nets = {
        ("U", "DP"): "N_P",
        ("U", "DM"): "N_M",
        ("J1", "A1"): "N_P",
        ("J1", "A2"): "N_M",
    }
    assert channel_map(clients, [sup], nets) == [
        ChannelRow("usb", ("J1", "A1"), "N_P", ("U", "DP")),
        ChannelRow("usb", ("J1", "A2"), "N_M", ("U", "DM")),
    ]
