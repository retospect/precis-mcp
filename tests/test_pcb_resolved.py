"""Pure tests for the resolved netlist snapshot (``precis.pcb.resolved``,
pcb-lazy-netlist-and-checks Slice 7): no DB, no job."""

from __future__ import annotations

from typing import Any

import numpy as np

from precis.pcb import DEFAULT_STACKUP
from precis.pcb import resolved as pcb_resolved
from precis.pcb.ir import NO_NET, from_graph
from precis.pcb.matching import Client, Supply


def _graph() -> dict[str, Any]:
    """S1/S2 are two same-role supply parts (S3 is a netless spare), E1/E2
    their clients; N_X has a net attribute set so rebind must keep it."""
    return {
        "instances": [
            {"refdes": "S1", "x": 0.0, "y": 0.0},
            {"refdes": "S2", "x": 20.0, "y": 0.0},
            {"refdes": "S3", "x": 40.0, "y": 0.0},
            {"refdes": "E1", "x": 0.0, "y": 10.0},
            {"refdes": "E2", "x": 20.0, "y": 10.0},
        ],
        "nets": [
            {
                "name": "A",
                "net_class": "signal",
                "domain": "electrical",
                "current": 0.5,
                "members": [
                    {"refdes": "S1", "pin": "1", "tags": ["t"], "meta": {"k": 1}},
                    {"refdes": "E2", "pin": "1"},
                ],
            },
            {
                "name": "B",
                "net_class": "power",
                "domain": "electrical",
                "members": [{"refdes": "S2", "pin": "1"}, {"refdes": "E1", "pin": "1"}],
            },
        ],
        "unconnected": [{"refdes": "S3", "pin": "1"}],
    }


def _members(graph: dict[str, Any]) -> dict[str, set[tuple[str, str]]]:
    return {
        n["name"]: {(m["refdes"], m["pin"]) for m in n["members"]}
        for n in graph["nets"]
    }


def _entry(refdes: str, pin: str, net: str | None, declared: str | None) -> dict:
    return {"refdes": refdes, "pin": pin, "net": net, "declared": declared}


def test_declared_pin_nets_includes_netless_pins() -> None:
    assert pcb_resolved.declared_pin_nets(_graph()) == {
        ("S1", "1"): "A",
        ("E2", "1"): "A",
        ("S2", "1"): "B",
        ("E1", "1"): "B",
        ("S3", "1"): None,
    }


def test_snapshot_holds_the_full_effective_set_incl_netless() -> None:
    """One entry per IR pin (here 5), netless ones with ``net: None``, each
    with its declared net, sorted by (refdes, pin)."""
    graph = _graph()
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    pins = pcb_resolved.snapshot_pins(ir, pcb_resolved.declared_pin_nets(graph))
    assert len(pins) == ir.n_pins == 5
    assert [(e["refdes"], e["pin"]) for e in pins] == sorted(
        (e["refdes"], e["pin"]) for e in pins
    )
    by_key = {(e["refdes"], e["pin"]): e for e in pins}
    assert by_key[("S3", "1")] == _entry("S3", "1", None, None)
    assert by_key[("S1", "1")] == _entry("S1", "1", "A", "A")
    assert not any(e.get("solver_created") for e in pins)


def test_snapshot_marks_a_netless_pin_that_now_carries_a_net_solver_created() -> None:
    graph = _graph()
    moved = pcb_resolved.rebind(graph, {("S1", "1"): None, ("S3", "1"): "A"})
    ir = from_graph(moved, stackup=DEFAULT_STACKUP)
    by_key = {
        (e["refdes"], e["pin"]): e
        for e in pcb_resolved.snapshot_pins(ir, pcb_resolved.declared_pin_nets(graph))
    }
    assert by_key[("S3", "1")] == {
        **_entry("S3", "1", "A", None),
        "solver_created": True,
    }
    # S1 moved OFF its declared net: a plain rebind, not solver-created.
    assert by_key[("S1", "1")] == _entry("S1", "1", None, "A")
    assert len(by_key) == ir.n_pins == 5


def test_rebind_moves_pins_and_keeps_net_attributes_and_order() -> None:
    graph = _graph()
    out = pcb_resolved.rebind(
        graph, {("S1", "1"): "B", ("S2", "1"): "A", ("S3", "1"): None}
    )
    assert [n["name"] for n in out["nets"]] == ["A", "B"]
    a, b = out["nets"]
    assert (a["net_class"], a["current"]) == ("signal", 0.5)
    assert (b["net_class"], b["domain"]) == ("power", "electrical")
    assert _members(out) == {
        "A": {("S2", "1"), ("E2", "1")},
        "B": {("S1", "1"), ("E1", "1")},
    }
    # member rows keep their own attributes when they move
    s1 = next(m for m in b["members"] if m["refdes"] == "S1")
    assert s1["tags"] == ["t"] and s1["meta"] == {"k": 1}
    assert [(m["refdes"], m["pin"]) for m in out["unconnected"]] == [("S3", "1")]
    # the input is not mutated
    assert _members(graph) == {
        "A": {("S1", "1"), ("E2", "1")},
        "B": {("S2", "1"), ("E1", "1")},
    }
    assert pcb_resolved.declared_pin_nets(graph)[("S1", "1")] == "A"


def test_rebind_none_unbinds_and_unknown_net_or_pin_is_ignored() -> None:
    out = pcb_resolved.rebind(
        _graph(),
        {("S1", "1"): None, ("E1", "1"): "NOPE", ("ZZ", "9"): "A"},
    )
    declared = pcb_resolved.declared_pin_nets(out)
    assert declared[("S1", "1")] is None
    assert declared[("E1", "1")] == "B"  # unknown net name: pin keeps its net
    assert ("ZZ", "9") not in declared


def test_rebind_pin_order_is_the_authored_creation_order_and_is_sticky() -> None:
    graph = _graph()
    once = pcb_resolved.rebind(graph, {("S1", "1"): "B", ("S2", "1"): "A"})
    # authored order: net A's sorted members, net B's, then unconnected
    assert once["pin_order"] == [
        ["E2", "1"],
        ["S1", "1"],
        ["E1", "1"],
        ["S2", "1"],
        ["S3", "1"],
    ]
    twice = pcb_resolved.rebind(once, {("S1", "1"): "A", ("S2", "1"): "B"})
    assert twice["pin_order"] == once["pin_order"]  # not recomputed off the moved graph


def _multi_pin_graph() -> dict[str, Any]:
    """U0 with four pins on four nets; every other pin on its own part, so
    U0's synthesized pad layout is the one thing a pin-creation-order bug
    would move."""
    pins = ["p1", "p2", "p3", "p4"]
    return {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            *[{"refdes": f"E{i}", "x": 10.0 * i, "y": 10.0} for i in range(4)],
        ],
        "nets": [
            {
                "name": f"N{i}",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": f"E{i}", "pin": "1"},
                    {"refdes": "U0", "pin": pins[3 - i]},
                ],
            }
            for i in range(4)
        ],
    }


def _offsets(ir: Any) -> dict[tuple[str, str], tuple[float, float]]:
    return {
        (str(ir.instance_refdes[int(ir.pin_instance[p])]), str(ir.pin_label[p])): (
            float(ir.pin_dx[p]),
            float(ir.pin_dy[p]),
        )
        for p in range(ir.n_pins)
    }


def test_rebound_ir_keeps_every_pins_synthesized_offset() -> None:
    graph = _multi_pin_graph()
    authored = from_graph(graph, stackup=DEFAULT_STACKUP)
    # permute all four U0 pins' nets
    swap = {
        ("U0", "p4"): "N1",
        ("U0", "p3"): "N0",
        ("U0", "p2"): "N3",
        ("U0", "p1"): "N2",
    }
    rebound = pcb_resolved.rebind(graph, swap)
    assert pcb_resolved.declared_pin_nets(rebound) != pcb_resolved.declared_pin_nets(
        graph
    )
    ir = from_graph(rebound, stackup=DEFAULT_STACKUP)
    assert _offsets(ir) == _offsets(authored)
    assert (
        len(set(_offsets(authored).values())) > 1
    )  # offsets are distinct: not vacuous
    # and the nets DID move
    p1 = next(
        p
        for p in range(ir.n_pins)
        if str(ir.pin_label[p]) == "p1"
        and str(ir.instance_refdes[int(ir.pin_instance[p])]) == "U0"
    )
    assert str(ir.net_name[int(ir.pin_net[p1])]) == "N2"
    # Without pin_order the creation order (and so the layout) follows the
    # moved nets - what pin_order exists to prevent.
    stripped = {k: v for k, v in rebound.items() if k != "pin_order"}
    assert _offsets(from_graph(stripped, stackup=DEFAULT_STACKUP)) != _offsets(authored)


def test_apply_without_a_snapshot_is_the_graph() -> None:
    graph = _graph()
    assert pcb_resolved.apply(graph, None) == graph
    assert pcb_resolved.apply(graph, {"pins": []}) == graph


def test_apply_honours_an_entry_only_while_the_declared_net_is_unchanged() -> None:
    graph = _graph()
    snapshot = {
        "pins": [
            _entry("S1", "1", "B", "A"),
            _entry("S2", "1", "A", "B"),
            _entry("S3", "1", "A", None),
        ]
    }
    out = pcb_resolved.apply(graph, snapshot)
    assert _members(out) == {
        "A": {("S2", "1"), ("E2", "1"), ("S3", "1")},
        "B": {("S1", "1"), ("E1", "1")},
    }
    # The author unbinds S1.1 (declared A -> None): its entry (declared A)
    # is stale and loses to the edit.
    edited = pcb_resolved.rebind(graph, {("S1", "1"): None})
    stale = {"pins": [_entry("S1", "1", "B", "A")]}
    assert (
        pcb_resolved.declared_pin_nets(pcb_resolved.apply(edited, stale))[("S1", "1")]
        is None
    )
    # a pin the author gave a net after the snapshot (declared None -> B)
    edited = pcb_resolved.rebind(graph, {("S3", "1"): "B"})
    assert (
        pcb_resolved.declared_pin_nets(pcb_resolved.apply(edited, snapshot))[
            ("S3", "1")
        ]
        == "B"
    )


def test_apply_drops_every_entry_on_a_net_a_stale_entry_touches() -> None:
    """A swap pair never half-applies and a spare never keeps a net its
    client has left: one stale entry drops all entries on its nets."""
    graph = _graph()
    snapshot = {
        "pins": [
            _entry("S1", "1", "B", "A"),
            _entry("S2", "1", "A", "B"),
            _entry("E2", "1", "A", "A"),
        ]
    }
    # the author moves S1.1 off A: S1's entry is stale, S2's (net A) must
    # not apply alone, which would leave A with two suppliers
    edited = pcb_resolved.rebind(graph, {("S1", "1"): None})
    out = pcb_resolved.apply(edited, snapshot)
    assert pcb_resolved.declared_pin_nets(out) == pcb_resolved.declared_pin_nets(edited)
    # spare S3 took A; the client E2 then moves to B: S3 drops off A
    spare = {"pins": [_entry("S3", "1", "A", None), _entry("E2", "1", "A", "A")]}
    edited = pcb_resolved.rebind(graph, {("E2", "1"): "B"})
    out = pcb_resolved.apply(edited, spare)
    assert pcb_resolved.declared_pin_nets(out)[("S3", "1")] is None


def test_rebind_marks_a_released_pin_unused() -> None:
    out = pcb_resolved.rebind(_graph(), {("S1", "1"): None})
    row = next(u for u in out["unconnected"] if (u["refdes"], u["pin"]) == ("S1", "1"))
    assert row["meta"] == {"k": 1, "unused": True}
    s3 = next(u for u in out["unconnected"] if u["refdes"] == "S3")
    assert "unused" not in (s3.get("meta") or {})


def test_apply_ignores_pins_the_graph_no_longer_has_and_keeps_unlisted_pins() -> None:
    graph = _graph()
    out = pcb_resolved.apply(graph, {"pins": [_entry("GONE", "1", "A", "A")]})
    assert pcb_resolved.declared_pin_nets(out) == pcb_resolved.declared_pin_nets(graph)
    out = pcb_resolved.apply(graph, {"pins": [_entry("S1", "1", None, "A")]})
    assert pcb_resolved.declared_pin_nets(out)[("E2", "1")] == "A"


def test_apply_then_snapshot_is_a_fixed_point() -> None:
    """Laying a snapshot over its own graph and re-snapshotting the IR gives
    the same pins: the snapshot describes exactly the netlist it rebuilds."""
    graph = _graph()
    moved = pcb_resolved.rebind(graph, {("S1", "1"): None, ("S3", "1"): "A"})
    ir = from_graph(moved, stackup=DEFAULT_STACKUP)
    declared = pcb_resolved.declared_pin_nets(graph)
    pins = pcb_resolved.snapshot_pins(ir, declared)
    again = from_graph(
        pcb_resolved.apply(graph, {"pins": pins}), stackup=DEFAULT_STACKUP
    )
    assert pcb_resolved.snapshot_pins(again, declared) == pins
    assert np.array_equal(
        [int(n) == NO_NET for n in ir.pin_net],
        [int(n) == NO_NET for n in again.pin_net],
    )


def test_prior_assignment_reads_the_binding_off_the_effective_nets() -> None:
    clients = [Client("E1", "1", "r"), Client("E2", "1", "r")]
    supplies = [Supply("S1", "r", (("1",),)), Supply("S2", "r", (("1",),))]
    pin_nets = {
        ("S1", "1"): "A",
        ("E2", "1"): "A",
        ("S2", "1"): "B",
        ("E1", "1"): "B",
    }
    assert pcb_resolved.prior_assignment(clients, supplies, pin_nets) == {
        ("E1", "1"): ("S2", "1"),
        ("E2", "1"): ("S1", "1"),
    }
    # a client whose net carries no supplier terminal has no prior
    assert pcb_resolved.prior_assignment(
        clients, supplies, {**pin_nets, ("S2", "1"): None}
    ) == {("E2", "1"): ("S1", "1")}


def test_encode_prior_is_sorted_json_rows() -> None:
    prior = {("E2", "1"): ("S1", "1"), ("E1", "1"): ("S2", "1")}
    assert pcb_resolved.encode_prior(prior) == [
        ["E1", "1", "S2", "1"],
        ["E2", "1", "S1", "1"],
    ]
    assert pcb_resolved.encode_prior({}) == []
