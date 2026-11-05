"""G2e — earlier nets close their layers (topo-route-geometry-truth).

``build_wires`` routes the nets in order. After each net's tree it runs
``layer_dp`` on every new branch's final edges, records the layer of each
corridor triangle (``WirePath.layers``) and hands the next net a view of
what is used: a layer is closed on an edge whose slots are used up, an
occupied edge costs more, and the taut wire's own crossings with earlier wires steer
its layers (and, when no layer change resolves one, re-grow the tree with
those wires walled on their layer).
"""

from __future__ import annotations

import dataclasses

import numpy as np
from shapely.geometry import LineString, Point

from precis.pcb.topoplace import wires as wires_mod
from precis.pcb.topoplace.design import TopoZone
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.wires import (
    WirePath,
    _edge_layers,
    build_wires,
    layer_dp,
)
from tests.test_pcb_topoplace_walls import _board, _rect

_TRACE, _CLEAR = 0.15, 0.15


def _route(d, *, via_dia: float = 0.6, n_nets: int = 2):
    mesh = build_mesh(d, d.pose0)
    rule = (_TRACE, _CLEAR, via_dia)
    w = build_wires(mesh, d, 4, (rule,) * n_nets, pose=d.pose0)
    by_net = {b.net: b for b in w.branches}
    return mesh, w, by_net


def _crosses(a: WirePath, b: WirePath) -> bool:
    return LineString(a.points).crosses(LineString(b.points))


def _layer_at(mesh, b: WirePath, pt: Point) -> int:
    """The layer branch ``b`` is on at ``pt`` of its taut wire: the
    triangle between the corridor edges whose crossings bracket it."""
    line = LineString(b.points)
    s = 0.0
    marks = []
    for e, t in zip(b.edges, b.t):
        u, v = mesh.verts[mesh.edges[int(e)][0]], mesh.verts[mesh.edges[int(e)][1]]
        s = max(s, line.project(Point(*(u + t * (v - u)))))
        marks.append(s)
    return b.layers[int(np.searchsorted(marks, line.project(pt)))]


def _same_layer_crossings(mesh, a: WirePath, b: WirePath) -> int:
    """Crossings of two taut wires that sit on one layer there."""
    cut = LineString(a.points).intersection(LineString(b.points))
    return sum(
        _layer_at(mesh, a, g) == _layer_at(mesh, b, g)
        for g in getattr(cut, "geoms", [cut])
        if g.geom_type == "Point"
    )


def _cross_board(*, sides=(0, 0), zones: tuple[TopoZone, ...] = ()):
    """Net 0 runs left to right, net 1 bottom to top; they meet at (20, 20)
    in open space."""
    pins = [
        {"x": 10.0, "y": 20.0, "net": 0, "side": sides[0]},
        {"x": 30.0, "y": 20.0, "net": 0, "side": sides[0]},
        {"x": 20.0, "y": 10.0, "net": 1, "side": sides[1]},
        {"x": 20.0, "y": 30.0, "net": 1, "side": sides[1]},
    ]
    return _board(pins, n_nets=2, zones=zones)


def test_layer_dp_prices_a_layer_ahead_of_a_via() -> None:
    both = 0b1001
    open_e = [both] * 4
    fit = [True] * 4
    # nothing priced: a free corridor stays on the start layer
    assert layer_dp(open_e, fit, 0b0001, 0b0001, 4)[:2] == ([0], [])
    # edges 1 and 2 priced on layer 0: two vias take layer 3 under them
    pen = [[0.0] * 4, [1.0, 0, 0, 0], [1.0, 0, 0, 0], [0.0] * 4]
    runs, cuts, dead = layer_dp(open_e, fit, 0b0001, 0b0001, 4, pen)
    assert dead is None
    assert (runs, cuts) == ([0, 3, 0], [0, 2])
    assert _edge_layers(4, runs, cuts) == (0, 3, 3, 0, 0)
    # priced where no via fits: the price is paid, not a dead end
    runs, cuts, dead = layer_dp(open_e, [False] * 4, 0b0001, 0b0001, 4, pen)
    assert (runs, cuts, dead) == ([0], [], None)


def test_edge_layers_gives_every_triangle_its_run_layer() -> None:
    assert _edge_layers(5, [0], []) == (0, 0, 0, 0, 0, 0)
    # cuts [1, 3]: edges 0-1 on run 0, 2-3 on run 1, 4 on run 2; the
    # last triangle is on the final run
    assert _edge_layers(5, [0, 3, 1], [1, 3]) == (0, 0, 3, 3, 1, 1)


def test_step_ok_and_intervals() -> None:
    """G2h's chord test on one triangle: a step keeping one side of the
    chord is legal, a step across it is not, a touch is no crossing."""
    d = _cross_board()
    mesh = build_mesh(d, d.pose0)
    ctx = wires_mod._make_ctx(mesh, d, 4, d.pose0)
    corridor = wires_mod._corridor(ctx, mesh, d, 4, [(_TRACE, _CLEAR)] * 2, d.pose0)
    occupancy = wires_mod._Occupancy(corridor, mesh, 4)
    occupancy.chords[(7, 0)] = [(0.5, 0.0, 0.0, 0.5)]
    same = occupancy.step_ok(7, 0, (1.0, 1.0), (0.6, 0.9))
    across = occupancy.step_ok(7, 0, (1.0, 0.0), (-0.5, -0.5))
    touch = occupancy.step_ok(7, 0, (0.5, 0.0), (1.0, 1.0))
    # review-2 P1: an endpoint on the chord's INTERIOR sits on the
    # earlier wire — a crossing, not a touch (its ends stay touches)
    on_seg = occupancy.step_ok(7, 0, (0.25, 0.25), (1.0, 1.0))
    assert (same, across, touch, on_seg) == (True, False, True, False)
    occupancy.splits[(3, 0)] = [0.3, 0.7]
    assert occupancy.intervals(3, 0) == [(0.0, 0.3), (0.3, 0.7), (0.7, 1.0)]
    assert occupancy.intervals(4, 0) == [(0.0, 1.0)]
    assert [occupancy.interval_at(3, 0, t) for t in (0.1, 0.5, 0.9)] == [0, 1, 2]


def test_the_planter_bounds_the_via_point_to_the_interval() -> None:
    """``_ViaPlanter.point(lo, hi)``: the returned t lies in [lo, hi]; a
    range excluding the first survivor's t returns another or None."""
    d = _cross_board()
    mesh = build_mesh(d, d.pose0)
    ctx = wires_mod._make_ctx(mesh, d, 4, d.pose0)
    corridor = wires_mod._corridor(ctx, mesh, d, 4, [(_TRACE, _CLEAR)] * 2, d.pose0)
    occupancy = wires_mod._Occupancy(corridor, mesh, 4)
    planter = occupancy.planter
    e = next(
        int(e)
        for e in range(len(mesh.edges))
        if not ctx.board[e] and planter.point(e, 0.3, _CLEAR, 0) is not None
    )
    full = planter.point(e, 0.3, _CLEAR, 0)
    assert full is not None and 0.0 <= full[2] <= 1.0
    got = planter.point(e, 0.3, _CLEAR, 0, None, 0.0, full[2])
    assert got is not None and got[2] <= full[2] + 1e-9
    left = planter.point(e, 0.3, _CLEAR, 0, None, full[2] + 0.05, 1.0)
    assert left is None or left[2] >= full[2] + 0.05 - 1e-9


def test_build_records_the_layers_the_dp_gave_each_branch() -> None:
    d = _cross_board()
    _mesh, w, by_net = _route(d)
    for b in w.branches:
        assert len(b.layers) == len(b.edges) + 1


def test_a_crossing_in_open_space_takes_vias_not_the_same_layer() -> None:
    d = _cross_board()
    mesh, w, by_net = _route(d)
    a, b = by_net[0], by_net[1]
    assert _crosses(a, b)  # the taut wires do cross
    assert set(a.layers) == {0}
    # net 1 leaves its top-side pads on the top layer and passes under
    # net 0 on another one, through two vias
    assert b.layers[0] == 0 and b.layers[-1] == 0
    assert set(b.layers) - {0}
    assert _same_layer_crossings(mesh, a, b) == 0


def test_with_no_via_to_take_it_goes_round() -> None:
    d = _cross_board()
    # a via wider than the board fits nowhere: net 1 cannot change layer
    mesh, w, by_net = _route(d, via_dia=50.0)
    a, b = by_net[0], by_net[1]
    assert not _crosses(a, b)
    assert set(b.layers) == {0}
    assert b.length_mm > 21.0  # round net 0's end, not straight through
    assert w.negotiated == ()  # the FIRST growth goes round


def test_the_later_wire_keeps_to_its_own_interval() -> None:
    """Interval portal (G2h): on every corridor edge both wires cross
    while net 0 is on layer 0, net 1's crossing parameter lands in an
    interval of ``splits[(e, 0)]`` the earlier wire's own ``t`` is not
    strictly inside — the split separates them, a vertex-touch stays a
    touch. No same-layer crossing survives, and a second build gives
    identical points. (The channel board at a wide gap — ``_cross_board``
    at via 50 routes net 1 around net 0's end on disjoint edges, so the
    shared set is empty there.)"""
    d = _channel_board(side=0, gap=2.5)
    mesh = build_mesh(d, d.pose0)
    rule = (_TRACE, _CLEAR, 50.0)
    n_nets = len(d.net_pins)
    ctx = wires_mod._make_ctx(mesh, d, 4, d.pose0)
    corridor = wires_mod._corridor(ctx, mesh, d, 4, [rule[:2]] * n_nets, d.pose0)
    occupancy = wires_mod._Occupancy(corridor, mesh, 4)
    occ = corridor.occ(np.zeros(len(mesh.edges)))
    pins = frozenset(int(v) for v in mesh.pin_vertex)
    grown0 = wires_mod._grow_net(
        ctx, mesh, d, 4, 0, rule, occ, pins, occupancy, occupancy.view(0)
    )
    for built0 in grown0.built:
        occupancy.add(0, built0)
    w = build_wires(mesh, d, 4, (rule,) * n_nets, pose=d.pose0)
    by_net = {b.net: b for b in w.branches}
    a, b = by_net[0], by_net[1]
    at = {int(e): i for i, e in enumerate(a.edges)}
    seen = 0
    for i, e in enumerate(b.edges):
        ia = at.get(int(e))
        if ia is None or (a.layers[ia] != 0 and a.layers[ia + 1] != 0):
            continue
        t_a = float(a.t[ia])
        iv = occupancy.interval_at(int(e), 0, float(b.t[i]))
        lo, hi = occupancy.intervals(int(e), 0)[iv]
        assert not lo < t_a < hi
        seen += 1
    assert seen
    assert _same_layer_crossings(mesh, a, b) == 0
    w2 = build_wires(mesh, d, 4, (rule,) * n_nets, pose=d.pose0)
    assert [p for br in w.branches for p in br.points] == [
        p for br in w2.branches for p in br.points
    ]


def _chord_blocked_named_keepout_design():
    """A construction-only ownership exemption, illegal under final clearance."""
    zones = (
        TopoZone(
            kind="keepout",
            ring=_rect(0.0, 0.0, 9.9, 40.0),
            layers=(),
            net="N0",
            name="west",
        ),
        TopoZone(
            kind="keepout",
            ring=_rect(30.1, 0.0, 9.9, 40.0),
            layers=(),
            net="N0",
            name="east",
        ),
    )
    return _cross_board(zones=zones)


def test_topological_negotiation_detours_the_earlier_net(monkeypatch) -> None:
    """Isolate the G2h construction step: no via fits and the named keepouts
    let only net 0's search leave the middle corridor. The probe crosses
    net 0, which re-grows around net 1's pin. Final clearance is deliberately
    outside this topology unit: keepouts have no real ownership exemption,
    so this artificial fixture cannot produce valid copper. The next test
    retains that real guard and verifies its refusal.
    """
    monkeypatch.setattr(wires_mod._Shove, "_preserves_guard", lambda *_a: True)
    d = _chord_blocked_named_keepout_design()
    mesh = build_mesh(d, d.pose0)
    w = build_wires(
        mesh, d, 4, ((_TRACE, _CLEAR, 50.0),) * 2, pose=d.pose0, shove=False
    )
    by_net = {b.net: b for b in w.branches}
    a, b = by_net[0], by_net[1]
    assert w.negotiated == ((1, 0, True),)
    assert not _crosses(a, b)
    assert a.length_mm > 21.0  # round net 1's pin, not straight through
    assert b.length_mm < 21.0


def test_named_keepouts_do_not_exempt_the_negotiated_copper(monkeypatch) -> None:
    """The topologically available detour is refused by the real keepout guard."""
    guarded = []
    oracle = wires_mod._Shove._guarded

    def record(shove, state, paths, focus):
        bad = oracle(shove, state, paths, focus)
        guarded.append(bad)
        return bad

    monkeypatch.setattr(wires_mod._Shove, "_guarded", record)
    _mesh, w, by_net = _route(_chord_blocked_named_keepout_design(), via_dia=50.0)
    assert w.negotiated == ((1, 0, False),)
    assert set(by_net) == {0}
    assert w.unreachable and {u.net for u in w.unreachable} == {1}
    assert any(0 in bad for bad in guarded), "the matching name cannot exempt a keepout"


def test_the_chord_split_is_what_takes_the_net_round(monkeypatch) -> None:
    """Blinding construction's splits leaves a crossing for the layer planner.

    The builder must not claim its own search detoured the blinded net (G2h).
    """
    view0 = wires_mod._Occupancy.view

    def blind(self, k):
        return dataclasses.replace(view0(self, k), chords=None, splits=None)

    monkeypatch.setattr(wires_mod._Occupancy, "view", blind)
    d = _cross_board()
    mesh, w, by_net = _route(d, via_dia=50.0)
    # Blinding the construction view still leaves the straight crossing.
    # Real-plan selection keeps it because the layer planner can rethread
    # the pair; stripping would turn that recovery into an actual failure.
    assert w.shove_grows > 0, "the committed crossing sent net 1 to the shove"
    assert set(by_net) == {0, 1}
    assert _crosses(by_net[0], by_net[1])
    from precis.pcb.rules import NetRules
    from precis.pcb.topoplace.realise import _plan_failed_nets, plan_route

    plan = plan_route(
        d,
        mesh,
        w,
        4,
        net_rules={name: NetRules(_TRACE, _CLEAR, 50.0) for name in d.net_name},
    )
    assert not _plan_failed_nets(plan)


def test_a_views_closed_layers_bind_the_layer_pass_not_only_the_search() -> None:
    """Every layer but the last closed everywhere: the tree, the shortcut
    and the layer pass all read the reduced mask, so the wire sits on the
    last layer end to end (the unreduced layer pass put it on layer 0)."""
    d = _cross_board(sides=(2, 2))
    mesh = build_mesh(d, d.pose0)
    rule = (_TRACE, _CLEAR, 0.6)
    ctx = wires_mod._make_ctx(mesh, d, 4, d.pose0)
    corridor = wires_mod._corridor(ctx, mesh, d, 4, [rule[:2]] * 2, d.pose0)
    occupancy = wires_mod._Occupancy(corridor, mesh, 4)
    occ = corridor.occ(np.zeros(len(mesh.edges)))
    view = occupancy.view(0)
    shut = dataclasses.replace(view, closed=np.full(len(mesh.edges), 0b0111))
    pins = frozenset(int(v) for v in mesh.pin_vertex)
    grown = wires_mod._grow_net(ctx, mesh, d, 4, 0, rule, occ, pins, occupancy, shut)
    assert set(grown.built[0].branch.layers) == {3}
    free = wires_mod._grow_net(ctx, mesh, d, 4, 0, rule, occ, pins, occupancy, view)
    assert set(free.built[0].branch.layers) == {0}


def test_side_by_side_in_open_space_pay_no_via() -> None:
    pins = [
        {"x": 10.0, "y": 19.5, "net": 0},
        {"x": 30.0, "y": 19.5, "net": 0},
        {"x": 10.0, "y": 20.5, "net": 1},
        {"x": 30.0, "y": 20.5, "net": 1},
    ]
    d = _board(pins, n_nets=2)
    _mesh, w, by_net = _route(d)
    assert set(by_net[0].layers) == {0}
    assert set(by_net[1].layers) == {0}
    assert not _crosses(by_net[0], by_net[1])
    assert not w.negotiated


def _channel_board(*, side: int, gap: float = 0.5):
    """Nets 0 and 1 run left to right through one gap in a wall of two
    tall drilled pads of net 2 (the wall spans the board and every layer, so the gap is the
    only way across). ``side`` 2 gives the nets drilled
    pads (every layer), 0 top-only ones."""
    half = gap / 2.0
    hi = 20.0 - half  # the lower pad's top edge
    lo = 20.0 + half
    pins = [
        {"x": 10.0, "y": 19.0, "net": 0, "side": side},
        {"x": 30.0, "y": 19.0, "net": 0, "side": side},
        {"x": 10.0, "y": 21.0, "net": 1, "side": side},
        {"x": 30.0, "y": 21.0, "net": 1, "side": side},
        {"x": 20.0, "y": hi / 2.0, "net": 2, "w": 0.4, "h": hi, "side": 2},
        {
            "x": 20.0,
            "y": (lo + 40.0) / 2.0,
            "net": 2,
            "w": 0.4,
            "h": 40.0 - lo,
            "side": 2,
        },
    ]
    return _board(pins, n_nets=3)


def _gap_layer(mesh, b: WirePath) -> int:
    """The layer the branch is on where it crosses the wall's line."""
    cut = LineString(b.points).intersection(LineString([(20.0, 0.0), (20.0, 40.0)]))
    return _layer_at(mesh, b, cut)


def test_a_full_channel_on_one_layer_sends_the_second_net_to_another() -> None:
    d = _channel_board(side=2)
    mesh, w, by_net = _route(d, n_nets=3)
    first, second = by_net[0], by_net[1]
    # one slot per layer in the gap: never both on one
    assert _gap_layer(mesh, first) != _gap_layer(mesh, second)
    assert not w.negotiated


def _two_gap_channel_board(
    *, side: int, gap: float = 0.5, zones: tuple[TopoZone, ...] = ()
):
    """``_channel_board`` with a second same-width gap at y = 30 — the
    wall is three drilled pads of net 2 leaving slots at y = 20 and
    y = 30."""
    half = gap / 2.0
    pins = [
        {"x": 10.0, "y": 19.0, "net": 0, "side": side},
        {"x": 30.0, "y": 19.0, "net": 0, "side": side},
        {"x": 10.0, "y": 21.0, "net": 1, "side": side},
        {"x": 30.0, "y": 21.0, "net": 1, "side": side},
        {
            "x": 20.0,
            "y": (20.0 - half) / 2.0,
            "net": 2,
            "w": 0.4,
            "h": 20.0 - half,
            "side": 2,
        },
        {"x": 20.0, "y": 25.0, "net": 2, "w": 0.4, "h": 10.0 - gap, "side": 2},
        {
            "x": 20.0,
            "y": (30.0 + half + 40.0) / 2.0,
            "net": 2,
            "w": 0.4,
            "h": 40.0 - 30.0 - half,
            "side": 2,
        },
    ]
    return _board(pins, n_nets=3, zones=zones)


def test_a_closure_blocked_net_gets_one_earlier_net_to_yield() -> None:
    """Negotiation kept, closure-blocked (G2h): the wall gives two slots,
    but net 0's fixed copper covers the upper gap's whole side
    region, so once net 0 fills the lower gap net 1 has no way — the
    probe fails too, the cut names net 0, and net 0 is re-grown through
    the gap its own copper leaves it."""
    zones = (
        TopoZone(
            kind="fixed_copper",
            ring=_rect(0.0, 24.0, 40.0, 16.0),
            layers=(),
            net="N0",
            name="north",
        ),
    )
    d = _two_gap_channel_board(side=0, zones=zones)
    mesh, w, by_net = _route(d, via_dia=50.0, n_nets=3)
    assert w.negotiated == ((1, 0, True),)
    wall = LineString([(20.0, 0.0), (20.0, 40.0)])
    y0 = LineString(by_net[0].points).intersection(wall).y
    y1 = LineString(by_net[1].points).intersection(wall).y
    assert y0 > 25.0  # net 0 yielded to the upper gap
    assert 19.0 < y1 < 22.0  # net 1 keeps the lower gap
    assert w.overflow == ()


def test_a_closure_blocked_net_is_named_after_one_negotiation() -> None:
    """Top-only pads and no via anywhere: the one slot of the gap on the
    top layer is net 0's, net 1 has no other way across (G2h closure
    variant). The negotiation asks net 0 to yield; it cannot — the cut
    is its only way too — so net 1 is named, blocked by net 0."""
    d = _channel_board(side=0)
    _mesh, w, by_net = _route(d, via_dia=50.0, n_nets=3)
    assert w.negotiated == ((1, 0, False),)
    assert 1 not in by_net
    assert w.unreachable[0].blocked_by == (0,)
    # the refusal is a capacity fact: net 1's unrouted demand still
    # lands on the cut (G2h review-2 A)
    assert w.overflow != ()


def test_the_assign_pass_keeps_the_layers_the_search_pass_gave() -> None:
    from precis.pcb.topoplace.realise import _layers_segment

    d = _cross_board()
    _mesh, w, by_net = _route(d)
    b = by_net[1]
    runs, cuts = _layers_segment(d, w, b, 4)
    assert _edge_layers(len(b.edges), runs, cuts)[:-1] == b.layers[:-1]
