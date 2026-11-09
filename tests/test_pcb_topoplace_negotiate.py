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
import random
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

    def sabotage(
        ctx, mesh_, d_, n_layers, k, rule, occ, pin_vids, occupancy, view, hv_weight=0.0
    ):
        # len(calls) == pass index: _neg_grow_set ran at the top of the
        # pass for it >= 1; the final pass closes every layer so both
        # grows honestly strand — the wrecked state the guard must not
        # emit
        if len(calls) == n_final - 1:
            view = dataclasses.replace(
                view, closed=np.full_like(view.closed, (1 << 4) - 1)
            )
        return grow(
            ctx,
            mesh_,
            d_,
            n_layers,
            k,
            rule,
            occ,
            pin_vids,
            occupancy,
            view,
            hv_weight,
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


def test_the_emit_never_ranks_worse_than_the_single_hard_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass 0 IS the old hard pass in the original route order, so the
    best-state ranking can only improve on it (td598636). Here every
    later pass is sabotaged into stranding all three nets — the emit
    must still be the single-pass result, not the wreck."""
    d, mesh = _fixture()
    single = wm.build_wires(mesh, d, 4, (_NOVIA,) * 3, negotiate=1)
    calls = {"n": 0}
    grow_set = wm._neg_grow_set
    grow = wm._grow_net

    def boundary(touch, partners, *, final, stall):
        calls["n"] += 1
        return grow_set(touch, partners, final=final, stall=stall)

    def wrecked(
        ctx_,
        mesh_,
        d_,
        n_layers_,
        k,
        rule,
        occ,
        pin_vids,
        occupancy,
        view,
        hv_weight=0.0,
    ):
        # calls["n"] > 0 from pass 1 on: every pass after the first
        # closes every layer so each grow honestly strands
        if calls["n"]:
            view = dataclasses.replace(
                view, closed=np.full_like(view.closed, (1 << 4) - 1)
            )
        return grow(
            ctx_,
            mesh_,
            d_,
            n_layers_,
            k,
            rule,
            occ,
            pin_vids,
            occupancy,
            view,
            hv_weight,
        )

    monkeypatch.setattr(wm, "_neg_grow_set", boundary)
    monkeypatch.setattr(wm, "_grow_net", wrecked)
    w = wm.build_wires(mesh, d, 4, (_NOVIA,) * 3, shove=False)
    assert {b.net for b in w.branches} == {b.net for b in single.branches}
    assert {u.net for u in w.unreachable} == {u.net for u in single.unreachable}


def test_a_stranded_net_is_no_convergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The early stop requires no unrouted net (td598636): a pass that
    strands a net with every resource free is no convergence — the
    touched set must pull the net back. Here pass 0's grow of net 1 is
    sabotaged into a wall; the repair pass must still run."""
    calls: list[frozenset[int]] = []
    grow_set = wm._neg_grow_set
    grow = wm._grow_net

    def boundary(touch, partners, *, final, stall):
        calls.append(frozenset(touch))
        return grow_set(touch, partners, final=final, stall=stall)

    def walled(
        ctx_,
        mesh_,
        d_,
        n_layers_,
        k,
        rule,
        occ,
        pin_vids,
        occupancy,
        view,
        hv_weight=0.0,
    ):
        if not calls and k == 1:  # pass 0, net 1 only
            view = dataclasses.replace(
                view, closed=np.full_like(view.closed, (1 << 4) - 1)
            )
        return grow(
            ctx_,
            mesh_,
            d_,
            n_layers_,
            k,
            rule,
            occ,
            pin_vids,
            occupancy,
            view,
            hv_weight,
        )

    monkeypatch.setattr(wm, "_neg_grow_set", boundary)
    monkeypatch.setattr(wm, "_grow_net", walled)
    d = _two_net_board()
    mesh = build_mesh(d, d.pose0)
    w = wm.build_wires(mesh, d, 4, (_RULE,) * 2, negotiate=6, shove=False)
    assert calls, "a stranded pass is no convergence — the next pass ran"
    assert w.unreachable == ()
    assert {b.net for b in w.branches} == {0, 1}


def test_an_exempt_shared_edge_is_no_conflict() -> None:
    """``_neg_touch`` and ``_neg_over_e`` exempt the edges ``view``
    never closes as a channel (td598636): a sliver bounding fewer than
    two pads is shared for free, and a wire on an edge only its own
    pads bound owes nothing — so their overuse neither touches the net
    nor counts toward ``over_total``."""
    occupancy = _occupancy_stub()
    corridor = SimpleNamespace(
        edge_pins=[frozenset({42}) for _ in range(8)],  # foreign pins everywhere
        edge_pads={3: np.asarray([0])},  # one pad — a sliver, not a channel
        own_pins=[frozenset({7}), frozenset()],
    )
    occupancy.corridor = corridor
    builts = {
        0: [_built((3,), (2,), (0, 0))],
        1: [_built((6,), (1,), (0, 0))],
    }
    keys = {0: (1, 0), 1: (1, 0)}
    occupancy.used[3, 0] = 2.0  # over the stub's cap of 1.0
    assert wm._neg_touch(occupancy, builts, keys, []) == set()
    assert wm._neg_over_e(occupancy)[3, 0] == 0.0
    # a true two-pad channel at the same overuse still conflicts
    corridor.edge_pads[3] = np.asarray([0, 1])
    assert wm._neg_touch(occupancy, builts, keys, []) == {0}
    assert wm._neg_over_e(occupancy)[3, 0] == 1.0
    # an edge only the net's own pads bound is exempt for that net
    corridor.edge_pins[3] = frozenset({7})
    assert wm._neg_touch(occupancy, builts, keys, []) == set()
    # the ledger still counts foreign demand on it — the exemption is
    # per net, never a licence to overfill the channel for others
    assert wm._neg_over_e(occupancy)[3, 0] == 1.0


def test_a_shove_that_leaves_the_ledger_worse_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The never-worse-than-pass-0 bound covers the shove (td598636):
    a shove whose ledger ranks worse than pass 0's is rolled back
    wholesale — pass 0's state emits and no shove is recorded."""
    d = _two_net_board()
    mesh = build_mesh(d, d.pose0)
    rules = (_RULE,) * 2
    base = wm.build_wires(mesh, d, 4, rules, negotiate=1, shove=False)

    def lose(self, st):
        # pretend the shove stranded net 1: its copper gone and its key
        # counting it unrouted — a ledger pass 0 strictly outranks
        return dataclasses.replace(
            st,
            builts={k: v for k, v in st.builts.items() if k != 1},
            keys={**st.keys, 1: (2, 0)},
        )

    monkeypatch.setattr(wm._Shove, "run", lose)
    for n in (1, 5):
        w = wm.build_wires(mesh, d, 4, rules, negotiate=n)
        assert w.branches == base.branches
        assert w.unreachable == base.unreachable == ()
        assert w.shove_grows == 0 and w.shove_rounds == ()


def test_a_shove_that_cuts_stranded_at_a_length_cost_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard ranks the shove's own metric — (failing nets,
    crossings, vias, length) through ``st.metric()`` on both sides
    (td598636): a shove that drops a crossing off a still-failing net
    at a length cost is an improvement the single pass's behaviour
    keeps. The emit-shape tuple it replaced priced overuse and length
    only, so the same ledger read strictly worse and was rolled back."""
    d = _two_net_board()
    mesh = build_mesh(d, d.pose0)
    rules = (_RULE,) * 2
    base = wm.build_wires(mesh, d, 4, rules, negotiate=1, shove=False)

    witness = wm._witness_crossings

    def phantom(occupancy, built):
        # pass 0's net 1 attempt reads one flagged crossing: its key
        # (1, 1) fails it, so the pass-0 metric is (1, 1, ·, L)
        if built and built[0].branch.net == 1:
            return [(0, 0.0, 0, 0.0, 0.0)]
        return witness(occupancy, built)

    monkeypatch.setattr(wm, "_witness_crossings", phantom)
    emitted: list[tuple] = []

    def win(self, st):
        # net 1 still fails (stranded rather than crossing — (2, 0))
        # and its copper ran longer: (1, 0, ·, L+Δ) strictly beats
        # pass 0's (1, 1, ·, L), so the shove must be kept
        st = dataclasses.replace(
            st,
            builts={**st.builts, 1: [*st.builts[1], st.builts[0][0]]},
            keys={**st.keys, 1: (2, 0)},
        )
        emitted.append(tuple(b.branch for bl in st.builts.values() for b in bl))
        return st

    monkeypatch.setattr(wm._Shove, "run", win)
    w = wm.build_wires(mesh, d, 4, rules, negotiate=1)
    assert w.branches == emitted[0], "the improved shove emits, tie-or-better"
    assert w.unreachable == ()

    def lose(self, st):
        # a strictly worse ledger — more failing crossings than pass 0
        # — is still rolled back wholesale
        return dataclasses.replace(st, keys={**st.keys, 1: (3, 2)})

    monkeypatch.setattr(wm._Shove, "run", lose)
    w = wm.build_wires(mesh, d, 4, rules, negotiate=1)
    assert w.branches == base.branches
    assert w.shove_grows == 0 and w.shove_rounds == ()


def test_a_negotiated_win_at_a_length_cost_survives_a_noop_shove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard compares the shoved state's ``st.metric()`` against
    the shove's INPUT — the emit candidate — never pass 0 directly
    (td598636): a negotiated pass admitted on an overuse gain while
    reading strictly worse on the shove's metric (which has no
    overuse term) must survive a no-op shove — comparing to pass 0
    fired the rollback and silently discarded the negotiation's
    trade, the whole point of td569540."""
    d, mesh = _fixture()
    rules = (_NOVIA,) * 3
    base = wm.build_wires(mesh, d, 4, rules, negotiate=1, shove=False)
    state = {"pass": 0}
    grow_set = wm._neg_grow_set
    rebuild = wm._Occupancy.rebuild
    witness = wm._witness_crossings

    def boundary(touch, partners, *, final, stall):
        state["pass"] += 1  # _neg_grow_set runs at the top of pass it>=1
        return grow_set(touch, partners, final=final, stall=stall)

    def inflating(self, kept):
        # pin pass 0's overuse high so a negotiated pass admits on the
        # overuse axis with the same unrouted count — the trade the
        # negotiation exists for
        new = rebuild(self, kept)
        if state["pass"] == 0:
            for b, _ln, _g, lays in new.lines:
                if b.net == 0:
                    new.tri_used[int(b.tris[0]), int(lays[0]) if lays else 0] += 100.0
                    break
        return new

    def phantom(occupancy, built):
        # passes after 0 flag one crossing on net 1's key: the emit
        # keeps pass 0's unrouted count and wins the tuple on overuse,
        # while the shove's own metric reads it strictly worse on the
        # crossing element
        if built and built[0].branch.net == 1 and state["pass"]:
            return [(0, 0.0, 0, 0.0, 0.0)] * 5
        return witness(occupancy, built)

    emitted: list[tuple] = []

    def noop(self, st):
        emitted.append(tuple(b.branch for bl in st.builts.values() for b in bl))
        return st

    monkeypatch.setattr(wm, "_neg_grow_set", boundary)
    monkeypatch.setattr(wm._Occupancy, "rebuild", inflating)
    monkeypatch.setattr(wm, "_witness_crossings", phantom)
    monkeypatch.setattr(wm._Shove, "run", noop)
    w = wm.build_wires(mesh, d, 4, rules, negotiate=5)
    assert emitted[0] != base.branches, "the emit is a negotiated pass"
    assert w.branches == emitted[0], (
        "a no-op shove ties its input — the negotiated emit must stand"
    )
    assert w.unreachable == ()


def test_a_hard_view_builds_no_exempt_array() -> None:
    """``view(k)`` with ``neg=None`` needs none of the negotiated
    passes' prices, so it never builds the every-edge exempt array
    (td598636): the hard closure clears the exempt bits inline, only
    where the closure actually set — the same view the single pass
    computed. A channel only the net's own pads bound or a one-pad
    sliver stays open; a foreign channel over capacity still closes."""
    occupancy = _occupancy_stub()
    gets: list[int] = []

    class Pads(dict):
        def get(self, e, default=None):
            gets.append(e)
            return super().get(e, default)

    corridor = SimpleNamespace(
        weight=[1.0, 1.0],
        rules_used=[(0.4, 0.3), (0.4, 0.3)],
        edge_pins=[
            frozenset({9}),
            frozenset({9}),
            frozenset({9}),
            frozenset({7}),
            frozenset({9}),
            frozenset({9}),
            frozenset({9}),
            frozenset({9}),
        ],
        edge_pads=Pads({1: (0, 1), 3: (0, 1), 5: (0,)}),
        own_pins=[frozenset({7}), frozenset()],
    )
    occupancy.corridor = corridor
    occupancy.n_layers = 2
    occupancy.via_close = np.full(8, np.inf)
    occupancy.via_clear = np.full(8, np.inf)
    occupancy.chords = {}
    occupancy.splits = {}
    occupancy.dyn_index = lambda *a, **k: None
    for e in (1, 3, 5):
        occupancy.used[e, 0] = 0.5  # over the stub's cap of 1.0
    view = wm._Occupancy.view(occupancy, 0)
    assert gets == [1, 5], (
        "a hard view visits only the edges the closure set, not every edge"
    )
    assert int(view.closed[1]) == 1, "the foreign over-full channel closes"
    assert int(view.closed[3]) == 0, "the own-pads channel stays open"
    assert int(view.closed[5]) == 0, "the one-pad sliver stays open"


def test_discard_plus_regrow_leaves_the_rebuild_ledger() -> None:
    """``_Occupancy.discard`` is ``rebuild``'s incremental twin
    (td598636): ripping a net out and re-growing it under the
    post-discard view must leave every field — slots, tri and chord
    crossing counts, splits and their refcounts, via reaches, chords
    and their primary/mirror marks — equal to replaying the kept nets
    plus the new wires on a fresh ledger. The mirrored chord record is
    the same physical segment: it starts no crossing pair of its own
    (``_chord_prim``), and a plain recount scored it.

    The grow order is the sanity board's route order under a
    fixed-seed shuffle (the invariant file's TAZ boards are absent
    locally). The ledger also carries one fabricated crossing pair:
    a legal grow can never commit crossing chords (chord legality
    refuses them) so ``cross_used`` only goes nonzero on negotiated
    passes or on a real board's committed crossings — the pair stands
    in for both."""
    from precis.pcb import topoplace as topo
    from tests.test_pcb_topoplace_invariant import _board as _ledger_board

    design, ir, fps, _spots = _ledger_board("sanity")
    d = topo.design_from_ir(
        ir,
        fps,
        net_classes=design.graph.get("net_classes"),
        features=design.features,
        fixed_copper=design.fixed_copper,
        route_planes=True,
        capability=design.fab_caps,
        hole_margin_mm=0.0,
    )
    n_layers = int(ir.n_layers)
    pose = np.asarray(d.pose0, dtype=float)
    mesh = build_mesh(d, pose)
    ctx = wm._make_ctx(mesh, d, n_layers, pose)
    rules = ((0.15, 0.15, 0.6, 0.3),) * len(d.net_pins)
    corridor = wm._corridor(ctx, mesh, d, n_layers, [r[:2] for r in rules], pose)
    occupancy = wm._Occupancy(
        corridor,
        mesh,
        n_layers,
        pose,
        net_clearance=tuple(r[1] for r in rules),
    )
    used = np.zeros(len(mesh.edges))
    pin_vids = frozenset(int(v) for v in mesh.pin_vertex)
    order = list(wm._net_order(mesh, d, n_layers))
    random.Random(0).shuffle(order)
    order = order[:4]
    pairs: list[tuple[int, wm._Built]] = []
    for k in order:
        att = wm._grow_net(
            ctx,
            mesh,
            d,
            n_layers,
            k,
            rules[k],
            corridor.occ(used),
            pin_vids,
            occupancy,
            occupancy.view(k),
        )
        for b in att.built:
            occupancy.add(k, b)
        used += corridor.used_of(b.branch for b in att.built)
        pairs += [(k, b) for b in att.built]

    # a committed crossing pair on (tri 340, layer 0): two one-
    # triangle chords whose pin-to-pin segments cross, folded in for
    # nets the grow order never touched — the kept side both the
    # discard recount and the rebuild replay must agree on
    def _fab(k: int, pin_a: int, pin_b: int):
        va = mesh.verts[int(mesh.pin_vertex[pin_a])]
        vb = mesh.verts[int(mesh.pin_vertex[pin_b])]
        return SimpleNamespace(
            branch=SimpleNamespace(
                net=k,
                points=[
                    (float(va[0]), float(va[1])),
                    (float(vb[0]), float(vb[1])),
                ],
                vias=[],
                tris=[340],
                edges=[],
                t=[],
                layers=(0,),
                pin_a=pin_a,
                pin_b=pin_b,
            )
        )

    for fk, pa_, pb_ in ((0, 11, 32), (1, 36, 39)):
        fb = _fab(fk, pa_, pb_)
        occupancy.add(fk, fb)
        pairs.append((fk, fb))
    # rip net 18 out and re-grow it under the view the kept nets
    # leave — the shuffled order's re-grow is not a replay
    k = 18
    old_chords = set(occupancy._contrib_chords.get(k, ()))
    old = [b for j, b in pairs if j == k]
    kept = [(j, b) for j, b in pairs if j != k]
    occupancy.discard(k)
    used -= corridor.used_of(b.branch for b in old)
    att2 = wm._grow_net(
        ctx,
        mesh,
        d,
        n_layers,
        k,
        rules[k],
        corridor.occ(used),
        pin_vids,
        occupancy,
        occupancy.view(k),
    )
    for b in att2.built:
        occupancy.add(k, b)
    # the re-grow is real (different corridors, not a replay), the
    # ledger carries a mirrored chord record, and the crossing count
    # the recount maintains is non-empty — otherwise the field
    # comparison below proves nothing about either mechanism
    assert set(occupancy._contrib_chords.get(k, ())) != old_chords, (
        "the regrown net landed on different chords"
    )
    assert occupancy.cross_used.any(), "the fabricated crossing pair stands"
    assert any(
        not prim for prims in occupancy._chord_prim.values() for prim in prims
    ), "a mirrored chord record stands"
    # the comparison replay runs in the POST-DISCARD order (k last);
    # a canonical route-order rebuild may legitimately differ in
    # ``cross_used`` for mirrored chords — reviewer finding 3, filed
    # separately, not fixed here
    fresh = occupancy.rebuild([*kept, *((k, b) for b in att2.built)])

    def fields(o: wm._Occupancy) -> dict:
        return {
            "chords": o.chords,
            "prim": o._chord_prim,
            "splits": o.splits,
            "split_rc": o._split_rc,
            "lines": [(b, ln.wkb, g) for b, ln, g, _lays in o.lines],
            "wire_bodies": [
                (w.geom.wkb, w.r, w.net, w.clearance) for w in o.wire_bodies
            ],
            "vias": o.vias,
            "via_net": o._via_net,
            "contrib_c": o._contrib_chords,
            "contrib_s": o._contrib_splits,
            "added": o._added,
        }

    assert fields(occupancy) == fields(fresh)
    for name in ("used", "tri_used", "cross_used", "via_close", "via_clear"):
        np.testing.assert_allclose(
            getattr(occupancy, name), getattr(fresh, name), atol=1e-9
        )
    for name in ("_via_vclose", "_via_vclear"):
        for a_v, b_v in zip(
            getattr(occupancy, name), getattr(fresh, name), strict=True
        ):
            np.testing.assert_allclose(a_v, b_v, atol=1e-9)
