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

import dataclasses
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


def _two_net_board():
    return _board(
        [
            {"x": 5.0, "y": 5.0, "net": 0},
            {"x": 15.0, "y": 5.0, "net": 0},
            {"x": 25.0, "y": 30.0, "net": 1},
            {"x": 35.0, "y": 30.0, "net": 1},
        ],
        n_nets=2,
    )


def _stall_rig(monkeypatch: pytest.MonkeyPatch, inflate):
    """Spy on the pass machinery while ``inflate`` drives the overuse
    signal: returns ``(calls, grown)`` where ``calls[i]`` is pass
    ``i + 1``'s ``_neg_grow_set`` arguments and ``grown[i]`` the nets
    pass ``i`` grew. ``inflate(new, pass_idx)`` runs inside
    ``_Occupancy.rebuild`` — set ``new.tri_used`` there to pin the
    overuse total the stall detector sees."""
    calls: list[tuple[frozenset[int], bool, bool]] = []
    grown: list[list[int]] = [[]]
    state = {"pass": 0}
    grow_set_of = wm._neg_grow_set
    grow = wm._grow_net
    rebuild = wm._Occupancy.rebuild

    def boundary(touch, partners, *, final, stall):
        state["pass"] += 1  # called at the top of pass it -> pass index it
        grown.append([])
        calls.append((frozenset(touch), final, stall))
        return grow_set_of(touch, partners, final=final, stall=stall)

    def per_pass(*args, **kwargs):
        grown[-1].append(args[4])
        return grow(*args, **kwargs)

    def inflating(self, kept):
        new = rebuild(self, kept)
        for b, _ln, _g, lays in new.lines:
            if b.net == 0:
                inflate(new, state["pass"], int(b.tris[0]), int(lays[0]) if lays else 0)
                break
        return new

    monkeypatch.setattr(wm, "_neg_grow_set", boundary)
    monkeypatch.setattr(wm, "_grow_net", per_pass)
    monkeypatch.setattr(wm._Occupancy, "rebuild", inflating)
    return calls, grown


def test_the_stall_escape_regrows_every_net_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overuse pinned flat (a phantom triangle overuse on net 0's
    triangle) never sets a new minimum, so the windowed detector arms
    the escape ``_NEG_STALL_K`` passes after the minimum — a clean net
    parked on a resource the touched nets need otherwise freezes the
    negotiation. Conflicts standing into the final pass force one full
    re-grow too."""

    def flat(new, _pass_idx, tri, lay):
        new.tri_used[tri, lay] = wm._NEG_TRI_CAP + 1.0

    calls, grown = _stall_rig(monkeypatch, flat)
    d = _two_net_board()
    mesh = build_mesh(d, d.pose0)
    wm.build_wires(mesh, d, 4, (_RULE,) * 2, negotiate=wm._NEG_STALL_K + 3)
    stalls = [i for i, (_t, _f, s) in enumerate(calls) if s]
    assert stalls == [wm._NEG_STALL_K], (
        "K flat passes after the minimum arm the escape; it fires once"
    )
    assert set(grown[stalls[0] + 1]) == {0, 1}, "the escape pass re-grows every net"
    assert all(grown[i] == [0] for i in range(1, stalls[0] + 1)), (
        "selective passes grow only the touched net"
    )
    assert calls[-1][1], "conflicts into the final pass force a full re-grow too"


def test_a_period_two_overuse_oscillation_still_arms_the_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """local-61/shuf2 oscillates e~161<->3 — every other pass falls, so
    a consecutive-non-fall detector never fires while the negotiation
    goes nowhere (spec-owner ruling 2026-11-06). The windowed detector
    does: revisiting 3 is not a NEW minimum, so ``_NEG_STALL_K`` passes
    without improvement arm the escape."""

    def oscillating(new, pass_idx, tri, lay):
        new.tri_used[tri, lay] = wm._NEG_TRI_CAP + (200.0 if pass_idx % 2 == 0 else 3.0)

    calls, grown = _stall_rig(monkeypatch, oscillating)
    d = _two_net_board()
    mesh = build_mesh(d, d.pose0)
    wm.build_wires(mesh, d, 4, (_RULE,) * 2, negotiate=wm._NEG_STALL_K + 4)
    stalls = [i for i, (_t, _f, s) in enumerate(calls) if s]
    # pass 1 sets the minimum (3); passes 2..5 revisit it without
    # beating it, so pass 6 (calls[5]) escapes
    assert stalls == [wm._NEG_STALL_K + 1], (
        "an oscillation that never beats its minimum is a stall"
    )
    assert set(grown[stalls[0] + 1]) == {0, 1}, "the escape pass re-grows every net"


def test_a_bad_escape_returns_the_state_that_armed_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-regression guard (spec-owner ruling 2026-11-06): the emit
    comes from the best corridor-legal pass ranked ``(unrouted nets,
    overuse, length)`` — never the last. Here the final pass's full
    re-grow is sabotaged so BOTH nets strand; the earlier passes lost
    only net 1's clean copper (a flagged crossing), so the best one
    must come back. On ``1dca30576`` a crossing made a soft pass
    ineligible outright, and the emit was the wrecked last pass."""
    calls, _grown = _stall_rig(monkeypatch, lambda new, _p, tri, lay: None)
    d = _two_net_board()
    mesh = build_mesh(d, d.pose0)
    n_final = 6
    grow = wm._grow_net

    def sabotage(*args, **kwargs):
        view = args[-1]
        # len(calls) == pass index: _neg_grow_set ran at the top of the
        # pass for it >= 1; the final pass closes every layer so both
        # grows honestly strand — the wrecked state the guard must not
        # emit
        if len(calls) == n_final - 1:
            view = dataclasses.replace(
                view, closed=np.full_like(view.closed, (1 << 4) - 1)
            )
        ctx, mesh, d, n_layers, k, rule, occ, pin_vids, occupancy = args[:9]
        return grow(
            ctx, mesh, d, n_layers, k, rule, occ, pin_vids, occupancy, view, **kwargs
        )

    witness = wm._witness_crossings

    def phantom(occupancy, built):
        # a crossing the pass flags but the corridor never overflows:
        # the state stays emit-legal while still counting one unrouted
        # net under the new ranking — and stays ineligible on
        # 1dca30576, which refused any soft pass with a crossing
        if built and built[0].branch.net == 1 and len(occupancy.lines):
            return [(0, 0.0, 0, 0.0, 0.0)]
        return witness(occupancy, built)

    monkeypatch.setattr(wm, "_grow_net", sabotage)
    monkeypatch.setattr(wm, "_witness_crossings", phantom)
    w = wm.build_wires(mesh, d, 4, (_RULE,) * 2, negotiate=n_final, shove=False)
    assert {u.net for u in w.unreachable} == set(), (
        "the guard returns the routed pre-escape state, not the stranded last pass"
    )
    assert 1 in {b.net for b in w.branches}


def test_negotiate_above_the_clamp_is_refused() -> None:
    """``p_fac`` ramps 1.4x a pass — float64 saturates near 2000 passes
    and every soft price goes inf long before. ``negotiate`` beyond
    ``_NEG_ITERS_MAX`` refuses instead of pretending to run."""
    d, mesh = _fixture()
    with pytest.raises(ValueError, match="negotiate"):
        wm.build_wires(mesh, d, 4, (_NOVIA,) * 3, negotiate=wm._NEG_ITERS_MAX + 1)
