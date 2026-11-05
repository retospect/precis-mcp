"""Negotiated congestion with history in ``build_wires`` (td569540).

The PathFinder stage of the route-then-shove model: soft passes price
oversubscribed corridors, triangle occupancy and same-layer chord
crossings instead of forbidding them, history accumulates on resources
that stay oversubscribed, and the final pass restores hard legality
before ``_Shove`` runs. These tests pin the contract: a net the single
pass loses can be negotiated back, the pass count is bounded with an
early stop, the emit is deterministic, and no same-layer crossing
survives the final pass.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from precis.pcb.topoplace import wires as wm
from precis.pcb.topoplace.crossings import crossing_events
from precis.pcb.topoplace.mesh import build_mesh
from tests.test_pcb_topoplace_walls import _NOVIA, _RULE, _board, _wall_pair


def _fixture():
    """Two edge-flush horizontal walls (nets 0, 2) and a diagonal net 1
    between them — the ``_wall_pair`` shape the shove tests use. Under
    ``_NOVIA`` no via plants, so every wire is single-layer and a
    chord-blocked net has no layer escape: the single pass strands it,
    negotiated passes price the contested corridor until it fits."""
    pins = _wall_pair(
        [
            {"x": 2.0, "y": 10.0, "net": 1},
            {"x": 38.0, "y": 30.0, "net": 1},
        ]
    )
    d = _board(pins, n_nets=3)
    return d, build_mesh(d, d.pose0)


def test_negotiation_routes_the_net_the_single_pass_loses() -> None:
    d, mesh = _fixture()
    single = wm.build_wires(mesh, d, 4, (_NOVIA,) * 3, negotiate=1)
    assert {u.net for u in single.unreachable} == {1}

    negotiated = wm.build_wires(mesh, d, 4, (_NOVIA,) * 3)
    assert negotiated.unreachable == ()
    assert {b.net for b in negotiated.branches} == {0, 1, 2}
    # single-layer fixture (_NOVIA): every emitted crossing is a
    # same-layer one, and the final hard pass leaves none between nets
    events = crossing_events(mesh, negotiated.branches)
    crossed = {pair for pair, hits in events.items() if hits}
    assert all(
        negotiated.branches[a].net == negotiated.branches[b].net for a, b in crossed
    )


def test_negotiated_wires_are_deterministic() -> None:
    d, mesh = _fixture()
    rules = (_NOVIA,) * 3
    first = wm.build_wires(mesh, d, 4, rules)
    second = wm.build_wires(mesh, d, 4, rules)
    assert first.branches == second.branches
    assert first.unreachable == second.unreachable
    assert first.overflow == second.overflow


def test_a_clean_design_stops_after_the_first_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two nets on a big board share nothing: pass 1 ends with zero
    overuse and zero priced crossings, so the early stop fires and no
    later pass pays a grow."""
    calls = 0
    grow = wm._grow_net

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return grow(*args, **kwargs)

    monkeypatch.setattr(wm, "_grow_net", counting)
    d = _board(
        [
            {"x": 5.0, "y": 5.0, "net": 0},
            {"x": 15.0, "y": 5.0, "net": 0},
            {"x": 25.0, "y": 30.0, "net": 1},
            {"x": 35.0, "y": 30.0, "net": 1},
        ],
        n_nets=2,
    )
    mesh = build_mesh(d, d.pose0)
    w = wm.build_wires(mesh, d, 4, (_RULE,) * 2, negotiate=10)
    assert w.unreachable == ()
    assert calls == 2, "one grow per net — the first pass converged"


def test_the_final_pass_never_emits_a_same_layer_crossing() -> None:
    """Soft passes may adopt priced crossings; the emit comes from the
    best LEGAL ledger, whose last pass had chords hard again. On the
    single-layer fixture any surviving crossing would be same-layer."""
    d, mesh = _fixture()
    w = wm.build_wires(mesh, d, 4, (_NOVIA,) * 3, negotiate=6)
    assert w.unreachable == ()
    events = crossing_events(mesh, w.branches)
    bad = {
        pair
        for pair, hits in events.items()
        if hits and w.branches[pair[0]].net != w.branches[pair[1]].net
    }
    assert bad == set()


def _occupancy_stub(n_e: int = 8, n_t: int = 8, n_l: int = 2):
    return SimpleNamespace(
        used=np.zeros((n_e, n_l)),
        cap=np.ones((n_e, n_l)),
        tri_used=np.zeros((n_t, n_l)),
        cross_used=np.zeros((n_t, n_l)),
    )


def _built(edges: tuple[int, ...], tris: tuple[int, ...], layers):
    return SimpleNamespace(
        branch=SimpleNamespace(edges=edges, tris=tris, layers=layers)
    )


def test_an_over_capacity_triangle_touches_its_occupants() -> None:
    """The touched set covers every axis the search prices (spec-owner
    ruling 2026-11-05): a net whose wires are otherwise clean but sit on
    a ``(tri, layer)`` past ``_NEG_TRI_CAP`` corridors is re-grown. The
    first build never tested ``tri_used``, so such a net kept its wires
    while the history cost under it rose."""
    occupancy = _occupancy_stub()
    builts = {
        0: [_built((3,), (2, 5), (0, 0))],
        1: [_built((6,), (1, 4), (0, 0))],
    }
    keys = {0: (1, 0), 1: (1, 0)}
    occupancy.tri_used[2, 0] = wm._NEG_TRI_CAP
    assert wm._neg_touch(occupancy, builts, keys, []) == set()
    occupancy.tri_used[2, 0] = wm._NEG_TRI_CAP + 1.0
    assert wm._neg_touch(occupancy, builts, keys, []) == {0}


def test_the_stall_escape_regrows_every_net_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity overuse flat two passes running (here a phantom triangle
    overuse pinned on net 0's triangle) and conflicts standing into the
    final pass each force one full re-grow — a clean net parked on a
    resource the touched nets need otherwise freezes the
    negotiation."""
    calls: list[tuple[frozenset[int], bool, bool]] = []
    grow_set_of = wm._neg_grow_set

    def spy(touch, partners, *, final, stall):
        calls.append((frozenset(touch), final, stall))
        return grow_set_of(touch, partners, final=final, stall=stall)

    monkeypatch.setattr(wm, "_neg_grow_set", spy)
    grown: list[list[int]] = [[]]
    grow = wm._grow_net

    def per_pass(*args, **kwargs):
        grown[-1].append(args[4])
        return grow(*args, **kwargs)

    monkeypatch.setattr(wm, "_grow_net", per_pass)
    rebuild = wm._Occupancy.rebuild

    def inflating(self, kept):
        new = rebuild(self, kept)
        for b, _ln, _g, lays in new.lines:
            if b.net == 0:
                new.tri_used[int(b.tris[0]), int(lays[0]) if lays else 0] = (
                    wm._NEG_TRI_CAP + 1.0
                )
                break
        return new

    monkeypatch.setattr(wm._Occupancy, "rebuild", inflating)

    def boundary(*args, **kwargs):
        grown.append([])
        return spy(*args, **kwargs)

    monkeypatch.setattr(wm, "_neg_grow_set", boundary)
    d = _board(
        [
            {"x": 5.0, "y": 5.0, "net": 0},
            {"x": 15.0, "y": 5.0, "net": 0},
            {"x": 25.0, "y": 30.0, "net": 1},
            {"x": 35.0, "y": 30.0, "net": 1},
        ],
        n_nets=2,
    )
    mesh = build_mesh(d, d.pose0)
    wm.build_wires(mesh, d, 4, (_RULE,) * 2, negotiate=5)
    stalls = [i for i, (_t, _f, s) in enumerate(calls) if s]
    assert stalls == [2], "two flat passes arm the escape; it fires once"
    assert set(grown[stalls[0] + 1]) == {0, 1}, "the escape pass re-grows every net"
    assert grown[1] == [0] and grown[2] == [0], (
        "selective passes grow only the touched net"
    )
    assert calls[-1][1], "conflicts into the final pass force a full re-grow too"


def test_negotiate_above_the_clamp_is_refused() -> None:
    """``p_fac`` ramps 1.4x a pass — float64 saturates near 2000 passes
    and every soft price goes inf long before. ``negotiate`` beyond
    ``_NEG_ITERS_MAX`` refuses instead of pretending to run."""
    d, mesh = _fixture()
    with pytest.raises(ValueError, match="negotiate"):
        wm.build_wires(mesh, d, 4, (_NOVIA,) * 3, negotiate=wm._NEG_ITERS_MAX + 1)
