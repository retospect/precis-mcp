"""S2 acceptance and failure bookkeeping, on small synthetic cells."""

from __future__ import annotations

import dataclasses
import types
from collections import Counter

import pytest

from precis.pcb.topoplace import realise as rm
from precis.pcb.topoplace import wires as wm
from precis.pcb.topoplace.design import TopoZone
from precis.pcb.topoplace.masks import BoardMasks, LayerMasks
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.realise import (
    RoutePiece,
    RoutePlan,
    ViaSite,
    _plan_failed_nets,
    plan_route,
)
from tests.test_pcb_topoplace_walls import _NOVIA, _RULE, _board, _rect, _wall_pair


@pytest.fixture
def cell(monkeypatch):
    """Capture the ordinary loop's clean three-net cell before rescue."""
    d = _board(
        [
            {"x": x, "y": y, "net": k}
            for k, y in enumerate((10.0, 20.0, 30.0))
            for x in (6.0, 34.0)
        ],
        n_nets=3,
    )
    mesh = build_mesh(d, d.pose0)
    captured = []

    def capture(shove, st):
        captured.append((shove, st))
        return st

    with monkeypatch.context() as patch:
        patch.setattr(wm._Shove, "run", capture)
        wm.build_wires(mesh, d, 4, (_RULE,) * 3)
    return captured[0]


def _attempt(shove, st, k, *, comps=None):
    att = shove._grow(k, st.occupancy.rebuild([]), [], shove.budget)
    assert att is not None
    return dataclasses.replace(
        att, built=st.builts[k], comps=comps or att.comps, cross=[]
    )


def _pair_oracle(monkeypatch, a, b):
    """Model a cancelled bigon restored only by the physical witness.

    The real oracle's geometry is covered by the walls bigon fixtures; here
    control the pair to isolate state transitions and crossing ownership.
    """

    def crossings(occ, branch, *, witness=False):
        partner = b if branch.net == a else a if branch.net == b else None
        return [
            (0, 0, j, 0, 1)
            for j, (other, *_rest) in enumerate(occ.lines)
            if witness and other.net == partner
        ]

    monkeypatch.setattr(wm._Occupancy, "crossings", crossings)
    # This controlled oracle ignores physical distance. Its affected-net set
    # must use the same model; spatial culling is tested on real geometry below.
    monkeypatch.setattr(wm, "_affected_nets", lambda *_a: {a, b, 0, 1, 2})


def test_equal_count_relocation_with_better_secondary_score_is_refused():
    # Aggregate comparison alone accepts this one-for-one relocation.
    assert (1, 0, 0, 1.0) < (1, 2, 3, 20.0)
    assert not wm._accept_stats((1, 2, 3, 20.0), (1, 0, 0, 1.0), {0}, {1})


def test_witness_stats_count_a_dropped_via_collision_as_failure(cell):
    shove, st = cell
    att = dataclasses.replace(_attempt(shove, st, 1), collisions=[(1, 2, 3)])
    assert wm._wit_stats(st.occupancy, att)[0] == 1


def test_committed_via_collision_remains_a_state_failure(cell):
    _shove, st = cell
    st = dataclasses.replace(st, collisions=[(1, 2, 3)])
    assert st.fails(1)
    assert st.stats(1)[0] == 1
    assert st.metric()[0] == 1


@pytest.mark.parametrize("shaped", [False, True], ids=["metric", "guard"])
def test_shove_and_final_planner_receive_the_same_route_masks(
    cell, monkeypatch, shaped
):
    _shove, _st = cell
    masks = BoardMasks(
        layers=(LayerMasks((), (), 1.0, 1.0),) * 4,
        via_prevent=(),
    )
    seen = []

    class FinalPlanReached(Exception):
        pass

    def planner(*_args, masks=None, **_kwargs):
        seen.append(masks)
        if len(seen) == 2:
            raise FinalPlanReached
        return RoutePlan(pieces=(), vias=(), layers=())

    def run(shove, st):
        assert shove.masks is masks
        branches = [b.branch for bl in st.builts.values() for b in bl]
        shove._plan(st, branches, shaped=shaped)
        return st

    monkeypatch.setattr(rm, "plan_route", planner)
    monkeypatch.setattr(wm._Shove, "run", run)
    with pytest.raises(FinalPlanReached):
        rm._route_pass(_shove.d, types.SimpleNamespace(n_layers=4), masks=masks)
    assert len(seen) == 2
    assert all(actual is masks for actual in seen)


def test_swap_cannot_trade_a_failure_for_a_clean_partners_via_collision(
    cell, monkeypatch
):
    shove, st = cell
    st = dataclasses.replace(st, keys={0: (2, 0), 1: (1, 0), 2: (1, 0)})
    atts = {k: _attempt(shove, st, k) for k in (0, 1)}
    atts[1] = dataclasses.replace(atts[1], collisions=[(1, 2, 3)])
    monkeypatch.setattr(shove, "_grow", lambda k, *_a, **_kw: atts[k])
    kept = [(k, b) for k, bl in st.builts.items() if k != 0 for b in bl]
    assert shove._swap(st, 0, (1,), kept, shove.budget) is None


@pytest.mark.parametrize("guarded_net", [1, 2], ids=["partner", "untouched"])
def test_swap_refuses_a_new_final_guard_failure(cell, monkeypatch, guarded_net):
    shove, st = cell
    st = dataclasses.replace(st, keys={0: (2, 0), 1: (1, 0), 2: (1, 0)})
    atts = {k: _attempt(shove, st, k) for k in (0, 1)}
    proposed = dataclasses.replace(atts[1].built[0].branch)
    atts[1] = dataclasses.replace(
        atts[1], built=[wm._Built(proposed, atts[1].built[0].edges)]
    )
    monkeypatch.setattr(shove, "_grow", lambda k, *_a, **_kw: atts[k])
    # A controlled clearance oracle isolates guard preservation from spatial
    # culling; the real close-parallel geometry is checked below.
    monkeypatch.setattr(wm, "_affected_nets", lambda *_a: {0, 1, 2})

    def guarded(_state, branches, focus):
        return (
            {guarded_net: {b for b in branches if b.net == guarded_net}}
            if guarded_net in focus and any(b is proposed for b in branches)
            else {}
        )

    monkeypatch.setattr(shove, "_guarded", guarded, raising=False)
    kept = [(k, b) for k, bl in st.builts.items() if k != 0 for b in bl]
    assert shove._swap(st, 0, (1,), kept, shove.budget) is None


def test_direct_regrow_refuses_final_guard_failure(cell, monkeypatch):
    shove, st = cell
    st = dataclasses.replace(st, keys={**st.keys, 0: (2, 0)})
    att = _attempt(shove, st, 0)
    monkeypatch.setattr(shove, "_grow", lambda *_a, **_kw: att)
    monkeypatch.setattr(
        shove,
        "_guarded",
        lambda _s, branches, focus: (
            {0: {b for b in branches if b.net == 0}} if 0 in focus else {}
        ),
        raising=False,
    )
    assert not list(shove._tries(st, 0, shove.budget))


@pytest.mark.parametrize("swap", [False, True], ids=["direct-regrow", "swap"])
def test_guard_checks_the_committed_state_and_branch_order(cell, monkeypatch, swap):
    shove, st = cell
    st = dataclasses.replace(st, keys={**st.keys, 0: (2, 0)})
    atts = {k: _attempt(shove, st, k) for k in (0, 1)}
    monkeypatch.setattr(shove, "_grow", lambda k, *_a, **_kw: atts[k])
    observed = []

    def guarded(state, branches, focus):
        observed.append((state, tuple(branches)))
        return {}

    monkeypatch.setattr(shove, "_guarded", guarded)
    if swap:
        kept = [(k, b) for k, bl in st.builts.items() if k != 0 for b in bl]
        result = shove._swap(st, 0, (1,), kept, shove.budget)
    else:
        (result,) = shove._tries(st, 0, shove.budget)
    assert result is not None
    checked_state, checked_branches = observed[-1]
    assert checked_state is result
    assert checked_branches == tuple(
        b.branch for bl in result.builts.values() for b in bl
    )


def test_swap_cannot_make_an_untouched_clean_net_fail(cell, monkeypatch):
    shove, st = cell
    atts = {k: _attempt(shove, st, k) for k in (0, 1)}
    # k recovers; failing p remains disconnected but gets a new bigon with q.
    atts[1] = dataclasses.replace(atts[1], comps=[[0], [1]])
    st = dataclasses.replace(st, keys={0: (2, 0), 1: (2, 0), 2: (1, 0)})
    original_p = st.builts[1][0].branch
    replacement_p = dataclasses.replace(original_p)
    atts[1] = dataclasses.replace(
        atts[1], built=[wm._Built(replacement_p, st.builts[1][0].edges)]
    )
    _pair_oracle(monkeypatch, 1, 2)
    oracle = wm._Occupancy.crossings

    def crossings(occ, branch, *, witness=False):
        if branch is original_p:
            return []
        hits = oracle(occ, branch, witness=witness)
        return [h for h in hits if occ.lines[h[2]][0] is not original_p]

    monkeypatch.setattr(wm._Occupancy, "crossings", crossings)
    monkeypatch.setattr(shove, "_grow", lambda k, *_a, **_kw: atts[k])
    kept = [(k, b) for k, bl in st.builts.items() if k != 0 for b in bl]
    assert shove._swap(st, 0, (1,), kept, shove.budget) is None


def test_apply_keeps_witness_failure_and_partners(cell, monkeypatch):
    shove, st = cell
    atts = {k: _attempt(shove, st, k) for k in (0, 1)}
    _pair_oracle(monkeypatch, 1, 2)
    branches = [b.branch for bl in st.builts.values() for b in bl]
    after = shove._apply(st, 0, (1,), atts, st.occupancy, branches)
    assert after.keys[1] == (1, 1), "plain-clean is still witness-failing"
    assert after.fails(1)
    assert after.partners[1] == [2]


def test_connected_probe_enumerates_its_witness_partners(cell, monkeypatch):
    shove, st = cell
    att = _attempt(shove, st, 0)
    st = dataclasses.replace(st, keys={**st.keys, 0: (1, 1)}, partners={})
    _pair_oracle(monkeypatch, 0, 1)
    monkeypatch.setattr(shove, "_grow", lambda *_a, **_kw: att)
    tried = []

    def swap(st, k, partners, *_a):
        tried.append(partners)
        return st

    monkeypatch.setattr(shove, "_swap", swap)
    assert list(shove._tries(st, 0, shove.budget))
    assert tried == [(1,)]


@pytest.mark.parametrize("key", [(1, 1), (2, 0)], ids=["crossing", "disconnected"])
def test_dropped_net_names_all_pins_and_remains_failed(cell, key):
    shove, st = cell
    st = dataclasses.replace(st, keys={**st.keys, 0: key}, partners={0: [1]})
    if key[0] > 1:
        pin = int(shove.d.net_pins[0][1])
        st = dataclasses.replace(
            st, unreachable=[wm.Unreachable(net=0, pin=pin, pins=(pin,), owners=())]
        )
    after = shove._strip(st, 0)
    (row,) = after.unreachable
    assert row.net == 0 and row.pins == tuple(shove.d.net_pins[0])
    assert row.pin == int(shove.d.net_pins[0][1 if key[0] > 1 else 0])
    assert row.blocked_by == (1,)
    assert row.layered == bool(key[1])
    w = wm.build_wires(shove.mesh, shove.d, 4, (_RULE,) * 3, shove=False)
    w = dataclasses.replace(
        w,
        branches=tuple(b for b in w.branches if b.net != 0),
        unreachable=tuple(after.unreachable),
    )
    plan = plan_route(shove.d, shove.mesh, w, 4)
    assert 0 in _plan_failed_nets(plan), "a withdrawn net must never look sketched"


def test_greedy_skips_a_net_fixed_as_an_earlier_partner(cell, monkeypatch):
    shove, st = cell
    st = dataclasses.replace(st, keys={0: (2, 0), 1: (2, 0), 2: (1, 0)})
    tried = []

    def tries(st, k, budget):
        tried.append(k)
        yield dataclasses.replace(st, keys={k: (1, 0) for k in st.keys})

    monkeypatch.setattr(shove, "_tries", tries)
    _, rounds = shove.run_greedy(st, order=[0, 1, 2])
    assert tried == [0]
    assert rounds == [1, 0]


def test_multistart_stops_after_an_empty_first_start(cell, monkeypatch):
    shove, st = cell
    st = dataclasses.replace(st, keys={**st.keys, 0: (2, 0)}, builts={})
    tried = []

    def tries(st, k, budget):
        tried.append(k)
        return iter(())

    monkeypatch.setattr(shove, "_tries", tries)
    _, rounds = shove.run_multi(st)
    assert tried == [0]
    assert rounds == [(0,)], "the start boundary is explicit"


def _line(st, net, points):
    """A synthetic committed single-layer branch for the real crossing oracle."""
    branch = dataclasses.replace(
        st.builts[net][0].branch,
        points=tuple(points),
        tris=(),
        edges=(),
        t=(),
        layers=(0,),
        vias=(),
    )
    return wm._Built(branch, ())


def _geometry(st, builts, **kwargs):
    occ = st.occupancy.rebuild((n, b) for n, bl in builts.items() for b in bl)
    return dataclasses.replace(st, builts=builts, occupancy=occ, **kwargs)


def test_shove_guard_sees_parallel_clearance_without_a_centerline_crossing(cell):
    shove, base = cell
    st = _geometry(
        base,
        {
            0: [_line(base, 0, [(6.0, 10.0), (34.0, 10.0)])],
            1: [_line(base, 1, [(6.0, 10.05), (34.0, 10.05)])],
            2: base.builts[2],
        },
    )
    assert not wm._witness_crossings(st.occupancy, st.builts[0])
    branches = [b.branch for bl in st.builts.values() for b in bl]
    assert shove._guarded(st, branches, {0, 1, 2}) == {
        n: {b.branch for b in st.builts[n]} for n in (0, 1)
    }
    assert shove._guarded(st, branches, {1}) == {1: {st.builts[1][0].branch}}
    assert shove._guarded(st, branches, {2}) == {}


def test_shove_guard_preserves_an_unrouted_foreign_nets_clearance(cell, monkeypatch):
    _shove, base = cell
    d = dataclasses.replace(
        _shove.d,
        zones=(
            TopoZone(
                "fixed_copper", _rect(18.0, 11.0, 4.0, 0.2), layers=(0,), net="FOREIGN"
            ),
        ),
    )
    captured = []

    def capture(shove, st):
        captured.append((shove, st))
        return st

    with monkeypatch.context() as patch:
        patch.setattr(wm._Shove, "run", capture)
        wm.build_wires(
            build_mesh(d, d.pose0),
            d,
            4,
            (_RULE,) * 3,
            net_clearance_by_name={"FOREIGN": 1.2},
        )
    ((shove, st),) = captured
    branch = _line(base, 0, [(6.0, 10.0), (34.0, 10.0)]).branch
    emitted = RoutePlan(
        pieces=(RoutePiece(net=0, branch=0, layer=0, points=branch.points),),
        vias=(),
        layers=(0,),
    )
    monkeypatch.setattr(shove, "_plan", lambda *_a, **_kw: emitted)
    assert shove._guarded(st, [branch], {0}) == {0: {branch}}
    # Its copper is 1 mm away: default clearance would incorrectly pass.
    shove.net_rules.pop("FOREIGN")
    assert shove._guarded(st, [branch], {0}) == {}


def test_shove_guard_supports_an_unnamed_design(cell):
    """Synthetic design pad ids still need a complete guard name table."""
    base, st = cell
    d = dataclasses.replace(base.d, net_name=(), ir_net_name=())
    shove = wm._Shove(
        base.ctx,
        base.mesh,
        d,
        base.n_layers,
        base.rules,
        base.pin_vids,
        base.corridor,
        base.order,
    )
    paths = [b.branch for bl in st.builts.values() for b in bl]
    assert shove._guarded(st, paths, set(st.builts)) == {}
    assert shove.ir.net_name == tuple(str(k) for k in range(len(d.net_pins)))


def test_guard_preservation_sees_an_untouched_via_near_a_changed_track(
    cell, monkeypatch
):
    shove, base = cell
    st = _geometry(
        base,
        {
            0: [_line(base, 0, [(10.0, 25.0), (30.0, 25.0)])],
            1: [_line(base, 1, [(20.0, 5.0), (20.0, 15.0)])],
            2: base.builts[2],
        },
    )
    after = _geometry(
        st, {**st.builts, 0: [_line(base, 0, [(10.0, 15.4), (30.0, 15.4)])]}
    )
    branches = [b.branch for bl in after.builts.values() for b in bl]

    # Control emission rather than the guard: the via ends net 1's downward
    # track. Its barrel clearance reaches the new track while the centerlines
    # remain farther apart than track-track clearance and never intersect.
    def emitted(_state, paths, *, shaped=False):
        return RoutePlan(
            pieces=tuple(
                RoutePiece(net=b.net, branch=i, layer=0, points=b.points)
                for i, b in enumerate(paths)
            ),
            vias=(ViaSite(net=1, branch=1, edge=0, t=0.5, x=20.0, y=15.0, dia_mm=0.6),),
            layers=(0,) * len(paths),
        )

    monkeypatch.setattr(shove, "_plan", emitted)
    assert not wm._witness_crossings(after.occupancy, after.builts[0])
    assert shove._guarded(st, branches, {0, 1, 2}) == {1: {st.builts[1][0].branch}}
    assert not shove._preserves_guard(st, after, {0})


def _guard_branch_cell(cell, monkeypatch, *, already_guarded):
    """An untouched net with one optional bad branch and one good via join."""
    shove, base = cell
    shove.d = dataclasses.replace(
        shove.d,
        zones=(TopoZone("keepout", _rect(11.0, 7.8, 2.0, 0.4), layers=(0,)),),
    )
    guarded = _line(base, 1, [(8.0, 8.0), (16.0, 8.0)])
    surviving = _line(base, 1, [(20.0, 5.0), (20.0, 15.0)])
    st = _geometry(
        base,
        {
            0: [_line(base, 0, [(10.0, 25.0), (30.0, 25.0)])],
            1: [*([guarded] if already_guarded else []), surviving],
            2: base.builts[2],
        },
        keys={**base.keys, 0: (2, 0)},
    )

    def emitted(_state, paths, *, shaped=False):
        via_branch = next(i for i, b in enumerate(paths) if b is surviving.branch)
        return RoutePlan(
            pieces=tuple(
                RoutePiece(net=b.net, branch=i, layer=0, points=b.points)
                for i, b in enumerate(paths)
            ),
            vias=(
                ViaSite(
                    net=1,
                    branch=via_branch,
                    edge=0,
                    t=0.5,
                    x=20.0,
                    y=15.0,
                    dia_mm=0.6,
                ),
            ),
            layers=(0,) * len(paths),
        )

    monkeypatch.setattr(shove, "_plan", emitted)
    return shove, st, emitted


def _raw_guard_marks(shove, st):
    paths = [b.branch for bl in st.builts.values() for b in bl]
    return rm._guarded_branches(
        shove._plan(st, paths, shaped=True),
        shove.d,
        shove.ir,
        shove.n_layers,
        shove.d.pose0,
        shove.net_rules,
    )


def _rethread_cell(monkeypatch, *, keepout=False, complete=False, n_layers=1, layer=0):
    """A partial three-pin tree which the real layer planner must expand."""
    d = _board(
        [
            {"x": 5.0, "y": 10.0, "net": 0},
            {"x": 35.0, "y": 10.0, "net": 0},
            {"x": 5.0, "y": 25.0, "net": 0},
            {"x": 20.0, "y": 5.0, "net": 1},
            {"x": 20.0, "y": 20.0, "net": 1},
        ],
        n_nets=2,
        n_layers=n_layers,
        zones=(TopoZone("keepout", _rect(19.8, 19.8, 0.4, 0.4), layers=(0,)),)
        if keepout
        else (TopoZone("keepout", _rect(0.0, 0.0, 40.0, 40.0), layers=(1 - layer,)),)
        if n_layers == 2
        else (),
    )
    if n_layers == 2:
        sides = d.pin_side.copy()
        sides[:] = 2
        d = dataclasses.replace(d, pin_side=sides)
    mesh = build_mesh(d, d.pose0)
    captured = []

    def capture(shove, st):
        captured.append((shove, st))
        return st

    with monkeypatch.context() as patch:
        patch.setattr(wm._Shove, "run", capture)
        wm.build_wires(mesh, d, n_layers, (_RULE,) * 2)
    shove, base = captured[0]

    def branch(net, pa, pb, points):
        tris, edges, params = wm._retrack(
            shove.ctx,
            mesh,
            int(mesh.pin_vertex[pa]),
            int(mesh.pin_vertex[pb]),
            list(points),
        )
        return wm._Built(
            wm.WirePath(
                net,
                pa,
                pb,
                tris,
                edges,
                params,
                tuple(points),
                (layer,) * (len(edges) + 1),
            ),
            edges,
        )

    tree = branch(0, 0, 1, [(5.0, 10.0), (35.0, 10.0)])
    crossing = branch(1, 3, 4, [(20.0, 5.0), (20.0, 20.0)])
    tree_builts = [tree]
    if complete:
        tree_builts.append(branch(0, 0, 2, [(5.0, 10.0), (5.0, 25.0)]))
    before = _geometry(base, {0: tree_builts, 1: []}, keys={0: (2, 0), 1: (2, 0)})
    after = _geometry(before, {0: tree_builts, 1: [crossing]})
    return shove, before, after, crossing


@pytest.mark.parametrize("layer", [0, 1])
@pytest.mark.parametrize(
    "encoding", ["implicit", "nominal", "resolved", "different-layer"]
)
@pytest.mark.parametrize("reason", ["clearance", "no-via", "crossing", "keepout"])
def test_rethread_retains_corridor_resolved_copper(
    monkeypatch, layer, encoding, reason
):
    """A real two-layer plan must preserve an unchanged legalized descendant."""
    shove, before, after, _ = _rethread_cell(
        monkeypatch, complete=True, n_layers=2, layer=layer
    )
    vertical = before.builts[0][1]
    branch = dataclasses.replace(
        vertical.branch,
        # An incomplete legacy tuple has no committed per-span assignment.
        layers=(1 - layer,) * (len(vertical.branch.edges) + 1)
        if encoding == "different-layer"
        else ()
        if encoding == "implicit"
        else (0,)
        if encoding == "nominal"
        else vertical.branch.layers,
    )
    tree = [before.builts[0][0], dataclasses.replace(vertical, branch=branch)]
    before = _geometry(before, {0: tree, 1: []})
    after = _geometry(after, {0: tree, 1: after.builts[1]})
    if reason == "keepout":
        shove.d = dataclasses.replace(
            shove.d,
            zones=(
                *shove.d.zones,
                TopoZone("keepout", _rect(4.8, 24.8, 0.4, 0.4), layers=(layer,)),
            ),
        )
    plans = [
        shove._plan(
            st, [b.branch for bs in st.builts.values() for b in bs], shaped=True
        )
        for st in (before, after)
    ]
    copper = [
        [p for p in plan.pieces if p.net == 0 and p.branch == 1] for plan in plans
    ]
    equivalent = encoding != "different-layer"
    assert copper[0] and copper[1]
    assert (copper[0] == copper[1]) is equivalent
    assert {p.layer for p in copper[0]} == {layer if equivalent else 1 - layer}
    assert {p.layer for p in copper[1]} == {layer}
    assert plans[0].branch_ids[1] == branch
    assert (plans[1].branch_ids[1] == branch) is equivalent
    assert plans[0].branch_lineage[1] < plans[1].branch_lineage[1]
    assert len(set(plans[1].branch_ids[:2])) == 2
    if reason == "keepout":
        assert _raw_guard_marks(shove, before) == {0: {1}}
        assert _raw_guard_marks(shove, after) == {0: {1}}
    else:
        plans = [
            dataclasses.replace(
                plan,
                no_via=(1,) if reason == "no-via" else (),
                crossings=((1, 1),) if reason == "crossing" else (),
            )
            for plan in plans
        ]
        monkeypatch.setattr(shove, "_plan", lambda st, *_a, **_kw: plans[st is after])
        monkeypatch.setattr(
            rm,
            "_guarded_branches",
            lambda *_a: {0: {1}} if reason == "clearance" else {},
        )
    assert shove._preserves_guard(before, after, {1}) is equivalent


@pytest.mark.parametrize("layer", [0, 1])
def test_retention_key_reads_resolved_state_over_explicit_nominal_layers(layer):
    """Resolved layers are authoritative even when the annotation is explicit."""
    from tests.test_pcb_topoplace_realise import _span_fixture

    d, _mesh, wires = _span_fixture()
    b = dataclasses.replace(wires.branches[0], layers=(1 - layer,) * 5)
    final = dataclasses.replace(b, layers=(layer,) * 5)
    resolved: tuple[list[int], list[int]] = ([layer], [])
    key = rm._retained_branch_key(d, wires, b, 2, resolved=resolved)
    assert key == rm._retained_branch_key(d, wires, final, 2, resolved=resolved)
    assert key != rm._retained_branch_key(
        d, wires, final, 2, resolved=([1 - layer], [])
    )
    assert key != rm._retained_branch_key(
        d, wires, final, 2, resolved=([layer, 1 - layer], [1])
    )


@pytest.mark.parametrize("metadata", ["implicit-layers", "crossing-parameters"])
@pytest.mark.parametrize("reason", ["clearance", "no-via", "crossing"])
def test_rethread_retains_equivalent_branch_metadata(monkeypatch, metadata, reason):
    """Equivalent input encodings must not manufacture a new withdrawal."""
    shove, before, after, _ = _rethread_cell(monkeypatch, complete=True)
    vertical = before.builts[0][1]
    branch = dataclasses.replace(
        vertical.branch,
        **(
            {"layers": ()}
            if metadata == "implicit-layers"
            else {"t": tuple(t + 1e-12 for t in vertical.branch.t)}
        ),
    )
    tree = [before.builts[0][0], dataclasses.replace(vertical, branch=branch)]
    before = _geometry(before, {0: tree, 1: []})
    after = _geometry(after, {0: tree, 1: after.builts[1]})
    plans = [
        shove._plan(
            st, [b.branch for bs in st.builts.values() for b in bs], shaped=True
        )
        for st in (before, after)
    ]
    copper = [
        [p for p in plan.pieces if p.net == 0 and p.branch == 1] for plan in plans
    ]
    assert copper[0] and copper[0] == copper[1]
    assert plans[0].branch_lineage[1] < plans[1].branch_lineage[1]
    assert plans[0].branch_ids[1] == plans[1].branch_ids[1] == branch
    assert len(set(plans[1].branch_ids[:2])) == 2
    plans = [
        dataclasses.replace(
            plan,
            no_via=(1,) if reason == "no-via" else (),
            # Controlled attribution, not a claim of a physical self-crossing.
            crossings=((1, 1),) if reason == "crossing" else (),
        )
        for plan in plans
    ]
    monkeypatch.setattr(shove, "_plan", lambda st, *_a, **_kw: plans[st is after])
    monkeypatch.setattr(
        rm, "_guarded_branches", lambda *_a: {0: {1}} if reason == "clearance" else {}
    )
    assert shove._preserves_guard(before, after, {1})


def test_implicit_layer_rethread_retains_actual_keepout_offence(monkeypatch):
    """Real planner, shaping and guard keep the same copper at (5, 25)."""
    shove, before, after, _ = _rethread_cell(monkeypatch, complete=True)
    vertical = before.builts[0][1]
    vertical = dataclasses.replace(
        vertical, branch=dataclasses.replace(vertical.branch, layers=())
    )
    tree = [before.builts[0][0], vertical]
    before = _geometry(before, {0: tree, 1: []})
    after = _geometry(after, {0: tree, 1: after.builts[1]})
    shove.d = dataclasses.replace(
        shove.d,
        zones=(TopoZone("keepout", _rect(4.8, 24.8, 0.4, 0.4), layers=(0,)),),
    )
    assert _raw_guard_marks(shove, before) == {0: {1}}
    assert _raw_guard_marks(shove, after) == {0: {1}}
    assert shove._preserves_guard(before, after, {1})


@pytest.mark.parametrize(
    ("sides", "candidates", "layers"),
    [
        ((0, 0), (0, 3), (0, 0, 0, 0)),
        ((1, 1), (0, 3), (3, 3, 3, 3)),
        ((2, 2), (1, 2), (1, 1, 1, 1)),
        ((2, 2), (), (0, 0, 0, 0)),
        ((0, 1), (0, 3), (0, 0, 3, 3)),
    ],
)
def test_retention_key_uses_effective_layers(sides, candidates, layers):
    """Independent nominal choices, including forced opposite-side joins."""
    from tests.test_pcb_topoplace_realise import _span_fixture

    d, _, wires = _span_fixture()
    b = dataclasses.replace(
        wires.branches[0],
        edges=(0, 1, 2),
        tris=(0, 1, 2, 3),
        t=(0.25, 0.5, 0.75),
        layers=(),
    )
    sides_a = d.pin_side.copy()
    sides_a[b.pin_a], sides_a[b.pin_b] = sides
    d = dataclasses.replace(d, pin_side=sides_a)
    wires = dataclasses.replace(wires, net_layers=(frozenset(candidates),))
    explicit = dataclasses.replace(b, layers=layers)
    implicit_key = rm._retained_branch_key(d, wires, b, 4)
    assert implicit_key.layers == layers
    assert implicit_key == rm._retained_branch_key(d, wires, explicit, 4)


@pytest.mark.parametrize("change", ["layer", "transition", "points", "pin", "via"])
def test_retention_key_distinguishes_physical_changes(change):
    """Equivalent encoding cannot hide changed layers, topology or copper."""
    from tests.test_pcb_topoplace_realise import _span_fixture

    d, _, wires = _span_fixture()
    b = dataclasses.replace(
        wires.branches[0],
        edges=(0, 1, 2),
        tris=(0, 1, 2, 3),
        t=(0.25, 0.5, 0.75),
        layers=(0, 0, 0, 0),
    )
    changes = {
        "layer": dataclasses.replace(b, layers=(3, 3, 3, 3)),
        "transition": dataclasses.replace(b, layers=(0, 3, 3, 0)),
        "points": dataclasses.replace(b, points=(*b.points[:-1], (5.0, 0.000001))),
        "pin": dataclasses.replace(b, pin_b=2),
        "via": dataclasses.replace(
            b, vias=(wm.PlantedVia(1, 1, 1.0, 0.0, 0.5, 0.2, 0.25),)
        ),
    }
    changed = changes[change]
    assert rm._retained_branch_key(d, wires, b, 4) != rm._retained_branch_key(
        d, wires, changed, 4
    )


def test_retention_key_keeps_unplanted_join_sites_and_exact_vias():
    """A crossing parameter is physical at an unplanted transition only."""
    from tests.test_pcb_topoplace_realise import _span_fixture

    d, _, wires = _span_fixture()
    b = dataclasses.replace(
        wires.branches[0],
        edges=(0, 1, 2),
        tris=(0, 1, 2, 3),
        t=(0.25, 0.5, 0.75),
        layers=(0, 0, 3, 3),
    )
    changed = dataclasses.replace(b, t=(0.25, 0.500001, 0.75))
    assert rm._retained_branch_key(d, wires, b, 4) != rm._retained_branch_key(
        d, wires, changed, 4
    )
    via = wm.PlantedVia(1, 1, 1.0, 0.0, 0.5, 0.2, 0.25)
    planted = dataclasses.replace(b, vias=(via,))
    assert rm._retained_branch_key(d, wires, planted, 4) == rm._retained_branch_key(
        d, wires, dataclasses.replace(changed, vias=(via,)), 4
    )
    for changed_via in (
        dataclasses.replace(via, x=1.000001),
        dataclasses.replace(via, dia_mm=0.6),
        dataclasses.replace(via, drill_mm=0.3),
        dataclasses.replace(via, count=2),
    ):
        moved = dataclasses.replace(planted, vias=(changed_via,))
        assert rm._retained_branch_key(d, wires, planted, 4) != rm._retained_branch_key(
            d, wires, moved, 4
        )


def test_rethread_preserves_the_identity_of_a_later_guarded_branch(monkeypatch):
    """Round 10: rethread expansion used final index 2 on a two-wire input."""
    shove, _before, after, crossing = _rethread_cell(monkeypatch, keepout=True)
    paths = [b.branch for bl in after.builts.values() for b in bl]
    plan = shove._plan(after, paths, shaped=True)
    assert len(paths) == 2
    assert {(p.net, p.branch) for p in plan.pieces} == {(0, 0), (0, 1), (1, 2)}
    assert _raw_guard_marks(shove, after) == {1: {2}}
    assert shove._guarded(after, paths, {0, 1}) == {1: {crossing.branch}}


@pytest.mark.parametrize("reason", ["clearance", "no-via", "crossing"])
def test_guard_rejects_a_second_failed_descendant_with_the_same_ancestry(
    monkeypatch, reason
):
    """Real rethread outputs; control only the terminal withdrawal verdicts."""
    shove, _, before, _ = _rethread_cell(monkeypatch, complete=True)
    changed = dataclasses.replace(
        before.builts[1][0],
        branch=dataclasses.replace(
            before.builts[1][0].branch,
            points=((20.0, 5.0), (20.01, 12.5), (20.0, 20.0)),
        ),
    )
    after = _geometry(before, {0: before.builts[0], 1: [changed]})
    plans = {}
    for st in (before, after):
        paths = [b.branch for bl in st.builts.values() for b in bl]
        plans[id(st)] = shove._plan(st, paths, shaped=True)
    p0, p1 = plans[id(before)], plans[id(after)]
    assert {p.branch for p in p0.pieces if p.net == 0} == {0, 1}
    assert p0.branch_lineage[:2] == p1.branch_lineage[:2]
    assert p0.branch_lineage[0] == p0.branch_lineage[1]

    active = False

    def planned(st, *_a, **_kw):
        nonlocal active
        active = st is after
        marks = (0, 1) if st is after else (0,)
        return dataclasses.replace(
            plans[id(st)],
            no_via=marks if reason == "no-via" else (),
            # A self-pair isolates attribution without inventing physical
            # crossings. Both descendants belong to the untouched N0.
            crossings=(((0, 1),) if active else ((0, 0),))
            if reason == "crossing"
            else (),
        )

    monkeypatch.setattr(shove, "_plan", planned)
    monkeypatch.setattr(
        rm,
        "_guarded_branches",
        lambda *_a: {0: {0, 1} if active else {0}} if reason == "clearance" else {},
    )
    assert not shove._preserves_guard(before, after, {1})


def test_identical_retained_descendant_offence_survives_expanded_ancestry(monkeypatch):
    """An unchanged vertical branch must not inherit its sibling's offence."""
    shove, before, after, _ = _rethread_cell(monkeypatch, complete=True)
    plans = [
        shove._plan(
            st, [b.branch for bl in st.builts.values() for b in bl], shaped=True
        )
        for st in (before, after)
    ]
    old, new = (
        [p for p in plan.pieces if p.net == 0 and p.branch == 1] for plan in plans
    )
    assert old and old == new
    assert plans[0].branch_lineage[1] < plans[1].branch_lineage[1]
    monkeypatch.setattr(shove, "_plan", lambda st, *_a, **_kw: plans[st is after])
    monkeypatch.setattr(rm, "_guarded_branches", lambda *_a: {0: {1}})
    assert shove._preserves_guard(before, after, {1})


@pytest.mark.parametrize("complete", [False, True])
def test_actual_keepout_offence_survives_rethread_and_index_shift(
    monkeypatch, complete
):
    """No offence mocks: the real guard marks N1 before/after N0 rethreads."""
    shove, _, after, crossing = _rethread_cell(
        monkeypatch, keepout=True, complete=complete
    )
    before = _geometry(after, {0: [], 1: [crossing]})
    assert _raw_guard_marks(shove, before) == {1: {0}}
    assert _raw_guard_marks(shove, after) == {1: {2}}
    assert shove._preserves_guard(before, after, {0})
    paths = [b.branch for bl in after.builts.values() for b in bl]
    plan = shove._plan(after, paths, shaped=True)
    assert len(plan.branch_ids) == len(plan.layers) == 3
    assert plan.branch_ids[2] == crossing.branch


@pytest.mark.parametrize("metadata", ["explicit", "implicit", "crossing-parameters"])
def test_transitive_rethread_retains_descendant_identity_separately_from_ancestry(
    monkeypatch, metadata
):
    """A second planner call preserves a retained child's earlier flip identity."""
    shove, _, st, _ = _rethread_cell(monkeypatch, complete=True)
    paths = [b.branch for bl in st.builts.values() for b in bl]
    if metadata != "explicit":
        retained = dataclasses.replace(
            st.builts[0][1],
            branch=dataclasses.replace(
                paths[1],
                **(
                    {"layers": ()}
                    if metadata == "implicit"
                    else {"t": tuple(t + 1e-12 for t in paths[1].t)}
                ),
            ),
        )
        st = _geometry(st, {0: [st.builts[0][0], retained], 1: st.builts[1]})
        paths[1] = retained.branch
    parent = dataclasses.replace(paths[0], points=((1.0, 1.0), (2.0, 2.0)))
    flipped = dataclasses.replace(paths[1], layers=())
    seeds = (
        frozenset((paths[0], parent)),
        frozenset((paths[1], parent)),
        frozenset((paths[2],)),
    )
    identities = (parent, flipped, paths[2])
    original = rm.assign_layers

    def assigned(d, mesh, wires, n_layers, **kw):
        return original(
            d,
            mesh,
            dataclasses.replace(wires, branch_lineage=seeds, branch_ids=identities),
            n_layers,
            **kw,
        )

    monkeypatch.setattr(rm, "assign_layers", assigned)
    plan = shove._plan(st, paths, shaped=True)
    ancestors = frozenset((paths[0], paths[1], parent))
    assert plan.branch_lineage[:2] == (ancestors, ancestors)
    assert len(set(plan.branch_ids[:2])) == 2
    assert plan.branch_ids[0] != parent  # Newly created replacement branch.
    assert plan.branch_ids[1:] == identities[1:]  # Retained child and other net.


def test_swap_cannot_guard_a_rethreaded_branch_of_an_untouched_net(monkeypatch):
    """A swap's planner-created branch still belongs to its untouched parent."""
    shove, before, after, crossing = _rethread_cell(monkeypatch)
    real_plan = shove._plan
    plans = []

    def planned(state, paths, *, shaped=False):
        result = real_plan(state, paths, shaped=shaped)
        plans.append(result)
        return result

    # The real planner expands N0 after the N1 swap. Control the clearance
    # verdict to isolate attribution of an offence on its second child.
    monkeypatch.setattr(
        rm,
        "_guarded_branches",
        lambda plan, *_a: {0: {1}} if len(plan.layers) == 3 else {},
    )
    monkeypatch.setattr(shove, "_plan", planned)
    paths = [b.branch for bl in after.builts.values() for b in bl]
    descendants = shove._plan(after, paths, shaped=True).branch_ids
    assert descendants[1] != before.builts[0][0].branch
    assert shove._guarded(after, paths, {0}) == {0: {descendants[1]}}
    att = dataclasses.replace(_attempt(shove, before, 1), built=[crossing])
    monkeypatch.setattr(shove, "_grow", lambda *_a, **_kw: att)
    # This test exercises the final planner guard after the earlier topology
    # gate has admitted the candidate; those gates have separate regressions.
    monkeypatch.setattr(wm, "_wit_stats", lambda *_a: (0, 0, 0, 1.0))
    monkeypatch.setattr(wm, "_preserves_other_nets", lambda *_a: True)
    kept = [(0, b) for b in before.builts[0]]
    assert shove._swap(before, 1, (), kept, shove.budget) is None
    assert any(len(plan.layers) == 3 for plan in plans)


def test_new_rethread_child_cannot_reuse_its_parents_existing_offence(monkeypatch):
    """A new child is not a retained offence merely because its parent failed."""
    shove, before, after, _crossing = _rethread_cell(monkeypatch)
    monkeypatch.setattr(
        rm,
        "_guarded_branches",
        lambda plan, *_a: {0: {1 if len(plan.layers) == 3 else 0}},
    )
    assert (
        len(
            shove._plan(
                before, [b.branch for bl in before.builts.values() for b in bl]
            ).layers
        )
        == 1
    )
    assert not shove._preserves_guard(before, after, {1})


def test_rethread_children_inherit_the_entire_replaced_tree(monkeypatch):
    """A replacement topology has no one-to-one edge mapping to its old tree."""
    shove, _before, after, crossing = _rethread_cell(monkeypatch, complete=True)
    paths = [b.branch for bl in after.builts.values() for b in bl]
    plan = shove._plan(after, paths, shaped=True)
    originals = frozenset(b.branch for b in after.builts[0])
    assert len(originals) == 2
    assert len(plan.layers) == 3
    assert plan.branch_lineage == (
        originals,
        originals,
        frozenset((crossing.branch,)),
    )
    monkeypatch.setattr(rm, "_guarded_branches", lambda *_a: {0: {1}})
    assert len(set(plan.branch_ids[:2])) == 2
    assert shove._guarded(after, paths, {0}) == {0: {plan.branch_ids[1]}}


@pytest.mark.parametrize("reason", ["no-via", "crossing"])
def test_rethread_shifted_branch_withdrawals_retain_lineage(monkeypatch, reason):
    """The non-clearance withdrawal paths use the final planner's index space."""
    shove, _before, after, crossing = _rethread_cell(monkeypatch)
    paths = [b.branch for bl in after.builts.values() for b in bl]
    plan = shove._plan(after, paths, shaped=True)
    assert len(plan.layers) == 3
    failed = dataclasses.replace(
        plan,
        no_via=(2,) if reason == "no-via" else (),
        crossings=((1, 2),) if reason == "crossing" else (),
        pieces=(),
        vias=(),
    )
    monkeypatch.setattr(shove, "_plan", lambda *_a, **_kw: failed)
    assert shove._guarded(after, paths, {1}) == {1: {crossing.branch}}


def test_layer_flip_keeps_the_original_branch_lineage():
    """Layer/via edits retain their input identity without a tree replacement."""
    from tests.test_pcb_topoplace_realise import _span_fixture

    d, mesh, wires = _span_fixture()
    planned, runs, cuts, crossings = rm.assign_layers(d, mesh, wires, 4)
    assert crossings == ()
    assert runs[1] == [0, 3, 0]
    assert cuts[1]
    assert planned.branch_lineage == tuple(frozenset((b,)) for b in wires.branches)
    assert planned.branch_ids == wires.branches


def test_merge_pass_keeps_lineage_aligned_with_shifted_branch_indices(cell):
    """Reroute passes shift lineage along with their layer and piece records."""
    from precis.pcb.realize import RealizeResult

    shove, st = cell
    a, b, c = (st.builts[n][0].branch for n in range(3))
    base = RoutePlan(
        pieces=(RoutePiece(net=0, branch=0, layer=0, points=a.points),),
        vias=(),
        layers=(0,),
        branch_lineage=(frozenset((a,)),),
        branch_ids=(a,),
    )
    extra = RoutePlan(
        pieces=(RoutePiece(net=1, branch=0, layer=1, points=b.points),),
        vias=(),
        layers=(1, 2),
        no_via=(1,),
        branch_lineage=(frozenset((b,)), frozenset((c,))),
        branch_ids=(b, c),
    )
    empty = RealizeResult((), (), ())
    merged, _result = rm._merge_pass(base, empty, extra, empty, {1}, shove.d, shove.ir)
    assert merged.layers == (0, 1, 2)
    assert merged.branch_lineage == base.branch_lineage + extra.branch_lineage
    assert merged.branch_ids == base.branch_ids + extra.branch_ids
    assert merged.pieces[-1].branch == 1
    assert merged.no_via == (2,)
    assert merged.branch_lineage[merged.pieces[-1].branch] == frozenset((b,))
    assert merged.branch_lineage[merged.no_via[0]] == frozenset((c,))
    assert merged.branch_ids[merged.no_via[0]] == c
    unknown, _result = rm._merge_pass(
        dataclasses.replace(base, branch_ids=()),
        empty,
        extra,
        empty,
        {1},
        shove.d,
        shove.ir,
    )
    assert unknown.branch_ids == (None, b, c)


def test_swap_cannot_guard_another_branch_of_an_already_guarded_net(cell, monkeypatch):
    """Round 9: net-only guards let a swap withdraw a surviving via branch."""
    shove, st, _emitted = _guard_branch_cell(cell, monkeypatch, already_guarded=True)
    proposed = _line(st, 0, [(10.0, 15.4), (30.0, 15.4)])
    att = dataclasses.replace(_attempt(shove, st, 0), built=[proposed])
    after = _geometry(st, {**st.builts, 0: [proposed]})
    assert _raw_guard_marks(shove, st) == {1: {1}}
    assert _raw_guard_marks(shove, after) == {1: {1, 2}}
    assert not wm._witness_crossings(after.occupancy, after.builts[0])
    monkeypatch.setattr(shove, "_grow", lambda *_a, **_kw: att)
    kept = [(n, b) for n, bl in st.builts.items() if n != 0 for b in bl]
    assert shove._swap(st, 0, (), kept, shove.budget) is None


@pytest.mark.parametrize(
    "already_guarded", [False, True], ids=["clean-net", "good-branch"]
)
def test_strip_cannot_guard_a_surviving_branch(cell, monkeypatch, already_guarded):
    """A withdrawal must recheck order-sensitive shaping of untouched copper."""
    shove, st, emitted = _guard_branch_cell(
        cell, monkeypatch, already_guarded=already_guarded
    )

    def shaped(state, paths, *, shaped=False):
        plan = emitted(state, paths, shaped=shaped)
        if any(b.net == 0 for b in paths):
            return plan
        return dataclasses.replace(
            plan,
            pieces=tuple(
                dataclasses.replace(pc, points=((10.0, 15.4), (30.0, 15.4)))
                if pc.net == 2
                else pc
                for pc in plan.pieces
            ),
        )

    monkeypatch.setattr(shove, "_plan", shaped)
    assert _raw_guard_marks(shove, st) == ({1: {1}} if already_guarded else {})
    stripped = _geometry(st, {**st.builts, 0: []})
    assert _raw_guard_marks(shove, stripped) == (
        {1: {0, 1}} if already_guarded else {1: {0}}
    )
    result = shove._strip(st, 0)
    assert result is st, "unsafe withdrawal must retain the original copper"


def test_safe_strip_keeps_existing_guard_branch_despite_shifted_indices(
    cell, monkeypatch
):
    """A bad branch retains its identity when an earlier net disappears."""
    shove, st, _emitted = _guard_branch_cell(cell, monkeypatch, already_guarded=True)
    assert _raw_guard_marks(shove, st) == {1: {1}}
    after = shove._strip(st, 0)
    assert after is not st
    assert not after.builts[0]
    assert after.builts[1] == st.builts[1]
    assert _raw_guard_marks(shove, after) == {1: {0}}


@pytest.mark.parametrize("reason", ["no-via", "crossing"])
def test_guard_preserves_a_branch_dropped_before_clearance(cell, monkeypatch, reason):
    """Earlier plan withdrawals cannot hide a newly lost surviving branch."""
    shove, st, emitted = _guard_branch_cell(cell, monkeypatch, already_guarded=True)
    after = dataclasses.replace(st)

    def plan(state, paths, *, shaped=False):
        result = emitted(state, paths, shaped=shaped)
        if state is st:
            return result
        return dataclasses.replace(
            result,
            no_via=(2,) if reason == "no-via" else (),
            crossings=((2, 3),) if reason == "crossing" else (),
            pieces=tuple(pc for pc in result.pieces if pc.branch != 2),
            vias=(),
        )

    monkeypatch.setattr(shove, "_plan", plan)
    paths = [b.branch for bl in st.builts.values() for b in bl]
    assert shove._guarded(after, paths, {1}) == {1: {b.branch for b in st.builts[1]}}
    assert not shove._preserves_guard(st, after, {0})


@pytest.mark.parametrize("allow", [False, True], ids=["guard-refuses", "guard-allows"])
@pytest.mark.parametrize(
    "disconnected", [False, True], ids=["connected-partner", "disconnected-partner"]
)
def test_order_loop_retry_checks_guard_before_replacing_a_partner(
    cell, monkeypatch, allow, disconnected
):
    """A prefix retry needs a connected partner and guard preservation."""
    shove, st = cell
    originals = {n: _attempt(shove, st, n) for n in range(3)}
    if disconnected:
        originals[0] = dataclasses.replace(originals[0], comps=[[0], [1]])
    first = dataclasses.replace(originals[2], built=[], comps=[[0], [1]])
    probe = dataclasses.replace(
        originals[2], built=[wm._Built(dataclasses.replace(st.builts[2][0].branch), ())]
    )
    retries = {
        n: dataclasses.replace(
            originals[n],
            built=[wm._Built(dataclasses.replace(st.builts[n][0].branch), ())],
        )
        for n in (0, 2)
    }
    grows = {
        0: iter((originals[0], originals[0], retries[0]))
        if disconnected
        else iter((originals[0], retries[0])),
        1: iter((originals[1],)),
        2: iter((first, probe, retries[2])),
    }

    grown = []

    def grow(_ctx, _mesh, _d, _layers, n, *_args):
        grown.append(n)
        return next(grows[n])

    def crossings(_occ, built):
        return [(0, 0, 0, 0, 1)] if built is probe.built else []

    checked = []

    def preserves(_shove, before, after, changed):
        checked.append(changed)
        assert changed == {0, 2}
        assert before.builts[0] is originals[0].built
        assert before.builts[2] is first.built
        assert after.builts[0] is retries[0].built
        assert after.builts[2] is retries[2].built
        assert after.builts[1] is before.builts[1] is originals[1].built
        return allow

    monkeypatch.setattr(wm, "_net_order", lambda *_a: [0, 1, 2])
    monkeypatch.setattr(wm, "_grow_net", grow)
    monkeypatch.setattr(wm, "_witness_crossings", crossings)
    monkeypatch.setattr(wm._Shove, "_preserves_guard", preserves)
    result = wm.build_wires(
        shove.mesh, shove.d, 4, (_RULE,) * 3, shove=False, negotiate=1
    )
    if disconnected:
        assert grown.count(2) == 2, "a disconnected partner cannot trigger retry_k"
        assert checked == [], (
            "refuse before guard planning when the partner is disconnected"
        )
    else:
        assert checked == [{0, 2}], "the order loop must consult guard preservation"
    accepted = allow and not disconnected
    assert result.negotiated == ((2, 0, accepted),)
    assert any(b.net == 2 for b in result.branches) is accepted


@pytest.mark.parametrize("closure", [False, True], ids=["probe", "closure"])
def test_order_loop_partner_ties_keep_earlier_ledger_owner(cell, monkeypatch, closure):
    """Equal blocker counts retain encounter order, including a higher net id."""
    shove, st = cell
    originals = {n: _attempt(shove, st, n) for n in range(3)}
    first = dataclasses.replace(originals[2], built=[], comps=[[0], [1]])
    probe = dataclasses.replace(
        originals[2],
        built=[wm._Built(dataclasses.replace(st.builts[2][0].branch), ())],
        comps=[[0], [1]] if closure else originals[2].comps,
    )
    retries = {
        n: dataclasses.replace(
            originals[n],
            built=[wm._Built(dataclasses.replace(st.builts[n][0].branch), ())],
        )
        for n in range(3)
    }
    grows = {
        0: iter((originals[0], retries[0])),
        1: iter((originals[1], retries[1])),
        2: iter((first, probe, retries[2])),
    }

    def grow(_ctx, _mesh, _d, _layers, n, *_args):
        return next(grows[n])

    def crossings(occ, built):
        if built is not probe.built:
            return []
        assert [row[0].net for row in occ.lines] == [1, 0]
        return [(0, 0, 0, 0, 1), (0, 0, 1, 0, 1)]

    monkeypatch.setattr(wm, "_net_order", lambda *_a: [1, 0, 2])
    monkeypatch.setattr(wm, "_grow_net", grow)
    monkeypatch.setattr(wm, "_witness_crossings", crossings)
    monkeypatch.setattr(wm._Occupancy, "nets_on", lambda *_a: Counter({1: 1, 0: 1}))
    monkeypatch.setattr(wm._Shove, "_preserves_guard", lambda *_a: True)
    result = wm.build_wires(
        shove.mesh, shove.d, 4, (_RULE,) * 3, shove=False, negotiate=1
    )
    assert result.negotiated == ((2, 1, True),)


def test_legal_same_net_contact_cannot_mask_a_new_foreign_crossing(cell, monkeypatch):
    shove, base = cell
    q = [
        _line(base, 2, [(5.0, 20.0), (30.0, 20.0)]),
        _line(base, 2, [(20.0, 15.0), (20.0, 25.0)]),
        _line(base, 2, [(5.0, 20.0), (20.0, 15.0)]),
    ]
    st = _geometry(
        base,
        {
            0: [_line(base, 0, [(5.0, 5.0), (30.0, 5.0)])],
            1: [_line(base, 1, [(5.0, 35.0), (30.0, 35.0)])],
            2: q,
        },
        keys={0: (2, 0), 1: (2, 0), 2: (1, 0)},
    )
    # The underlying oracle sees q's legal self-contact; the foreign oracle
    # must discard it before preservation decides whether q was failing.
    assert st.occupancy.crossings(q[0].branch, witness=True)
    assert not wm._witness_crossings(st.occupancy, q)
    atts = {n: _attempt(shove, st, n) for n in (0, 1)}
    atts[1] = dataclasses.replace(
        atts[1], comps=[[0], [1]], built=[_line(base, 1, [(15.0, 17.0), (15.0, 23.0)])]
    )
    monkeypatch.setattr(shove, "_grow", lambda n, *_a, **_kw: atts[n])
    kept = [(n, b) for n, bl in st.builts.items() if n != 0 for b in bl]
    assert shove._swap(st, 0, (1,), kept, shove.budget) is None


def test_already_crossed_net_cannot_gain_another_foreign_crossing(cell):
    _shove, base = cell
    st = _geometry(
        base,
        {
            0: [_line(base, 0, [(5.0, 5.0), (30.0, 5.0)])],
            1: [_line(base, 1, [(10.0, 15.0), (10.0, 25.0)])],
            2: [_line(base, 2, [(5.0, 20.0), (30.0, 20.0)])],
        },
    )
    after = _geometry(
        st, {**st.builts, 0: [_line(base, 0, [(20.0, 15.0), (20.0, 25.0)])]}
    )
    assert len(wm._witness_crossings(st.occupancy, st.builts[2])) == 1
    assert len(wm._witness_crossings(after.occupancy, st.builts[2])) == 2
    assert not wm._preserves_other_nets(st.occupancy, after.occupancy, st.builts, {0})


def test_apply_refreshes_untouched_net_whose_crossing_disappeared(cell):
    shove, base = cell
    st = _geometry(
        base,
        {
            0: base.builts[0],
            1: [_line(base, 1, [(15.0, 15.0), (15.0, 25.0)])],
            2: [_line(base, 2, [(5.0, 20.0), (30.0, 20.0)])],
        },
        keys={0: (2, 0), 1: (1, 1), 2: (1, 1)},
        partners={1: [2], 2: [1]},
    )
    atts = {n: _attempt(shove, st, n) for n in (0, 1)}
    atts[1] = dataclasses.replace(
        atts[1], built=[_line(base, 1, [(5.0, 35.0), (30.0, 35.0)])]
    )
    after = _geometry(st, {**st.builts, **{n: a.built for n, a in atts.items()}})
    result = shove._apply(
        st,
        0,
        (1,),
        atts,
        after.occupancy,
        [b.branch for bl in after.builts.values() for b in bl],
    )
    assert result.keys[2] == (1, 0)
    assert not result.fails(2)
    assert 2 not in result.partners
    assert st.keys[2] == (1, 1), "snapshots must remain immutable"


def test_multistart_picks_the_real_plan_failure_set_over_stale_keys(cell, monkeypatch):
    shove, clean = cell
    # The physically clean state has a stale saved crossing. The alternative
    # has a better saved metric but leaves k disconnected in the actual plan.
    clean = dataclasses.replace(clean, keys={**clean.keys, 2: (1, 1)})
    failed = dataclasses.replace(
        clean,
        keys={0: (2, 0), 1: (1, 0), 2: (1, 0)},
        unreachable=[wm.Unreachable(net=0, pin=0, pins=(0, 1), owners=())],
    )
    assert failed.metric() < clean.metric()
    starts = iter([clean, failed, failed, failed])
    monkeypatch.setattr(shove, "run_greedy", lambda *_a: (next(starts), [1]))
    best, _rounds = shove.run_multi(failed)
    assert best is clean


@pytest.mark.parametrize(
    ("before_failed", "after_failed", "shorter", "accept"),
    [
        ({0, 2}, {1}, False, False),
        ({0, 2}, {2}, False, True),
        ({0, 2}, {0, 2}, True, True),
        ({0, 2}, {0, 2}, False, False),
    ],
    ids=["incomparable-fewer", "strict-subset", "equal-shorter", "equal-tie"],
)
def test_multistart_compares_actual_failure_sets_before_secondary_costs(
    cell, monkeypatch, before_failed, after_failed, shorter, accept
):
    shove, base = cell

    def failed_state(failed, detour=False):
        builts = dict(base.builts)
        if detour:
            builts[0] = [_line(base, 0, [(6.0, 10.0), (20.0, 8.0), (34.0, 10.0)])]
        return _geometry(
            base,
            builts,
            # Saved keys deliberately agree: only the actual plan's stranded
            # certificates distinguish these starts' failed-net identities.
            keys={n: (2, 0) for n in range(3)},
            unreachable=[
                wm.Unreachable(net=n, pin=2 * n, pins=(2 * n, 2 * n + 1), owners=())
                for n in sorted(failed)
            ],
        )

    before = failed_state(before_failed, detour=shorter)
    after = failed_state(after_failed)
    monkeypatch.setattr(shove, "run_greedy", lambda *_a: (after, [1]))
    best, _rounds = shove.run_multi(before)
    assert best is (after if accept else before)


def test_beam_keeps_earlier_improvement_when_next_round_has_no_accept(
    cell, monkeypatch
):
    shove, st = cell
    st = dataclasses.replace(st, keys={0: (2, 0), 1: (2, 0), 2: (1, 0)})
    improved = dataclasses.replace(st, keys={**st.keys, 0: (1, 0)})
    monkeypatch.setattr(wm, "_SHOVE_BEAM", 1)

    def tries(state, k, _budget):
        if state is st and k == 0:
            yield improved

    monkeypatch.setattr(shove, "_tries", tries)
    best, rounds = shove.run_beam(st)
    assert rounds == [1, 0]
    assert best is improved


def test_multistart_keeps_shuffles_when_first_share_misses_a_failed_net(
    cell, monkeypatch
):
    shove, base = cell
    st = dataclasses.replace(base, keys={n: (2, 0) for n in range(3)})
    for n in range(3):
        st = shove._strip(st, n)
    tried = []

    def tries(st, n, budget):
        tried.append(n)
        shove.grows = min(budget, shove.grows + (1 if n == 2 else 11))
        if n == 2:
            yield dataclasses.replace(
                st,
                builts={**st.builts, 2: base.builts[2]},
                keys={**st.keys, 2: (1, 0)},
                unreachable=[u for u in st.unreachable if u.net != 2],
            )

    monkeypatch.setattr(shove, "_tries", tries)
    best, rounds = shove.run_multi(st)
    assert rounds[0] == (0,)
    assert len(rounds) == 4, "the untried recoverable net needs a shuffled start"
    assert 2 in tried and not best.fails(2)
    assert shove.grows <= shove.budget


def test_multistart_keeps_shuffles_after_zero_accept_stripping(cell, monkeypatch):
    shove, st = cell
    st = dataclasses.replace(st, keys={**st.keys, 0: (2, 0)})
    monkeypatch.setattr(shove, "_tries", lambda *_a: iter(()))
    _best, rounds = shove.run_multi(st)
    assert len(rounds) == 4, "stripping changes occupancy even without acceptance"


def test_preservation_spatial_cull_matches_exhaustive_and_skips_remote_nets(
    cell, monkeypatch
):
    _shove, base = cell
    st = _geometry(base, {0: base.builts[0], 1: base.builts[1], 2: base.builts[2]})
    # Cross only net 1 at y=20; net 2 at y=30 cannot change status.
    # The crossing is on a different layer, so exhaustive checking succeeds
    # and cannot accidentally hide a remote check by short-circuiting.
    moved = _line(base, 0, [(20.0, 15.0), (20.0, 25.0)])
    moved = wm._Built(dataclasses.replace(moved.branch, layers=(1,)), ())
    after = _geometry(st, {**st.builts, 0: [moved]})
    assert wm._affected_nets(st.occupancy, after.occupancy, {0}) == {0, 1}
    checked: list[int] = []
    oracle = wm._foreign_crossing_set

    def record(occ, bl):
        checked.extend(b.branch.net for b in bl)
        return oracle(occ, bl)

    monkeypatch.setattr(wm, "_foreign_crossing_set", record)
    got = wm._preserves_other_nets(st.occupancy, after.occupancy, st.builts, {0})
    assert checked and set(checked) == {1}
    expected = all(
        oracle(after.occupancy, bl) <= oracle(st.occupancy, bl)
        for n, bl in st.builts.items()
        if n != 0
    )
    assert got == expected
    assert got


def test_crossing_spatial_index_is_invalidated_by_add(cell):
    _shove, st = cell
    occ = st.occupancy.rebuild([])
    b = st.builts[0][0]
    line = wm.LineString(b.branch.points)
    assert occ.nearby(line) == []
    occ.add(0, b)
    assert occ.nearby(line) == [0]


def test_small_shove_enabled_builds_have_identical_copper_and_order():
    d = _board(
        _wall_pair([{"x": 2.0, "y": 10.0, "net": 1}, {"x": 38.0, "y": 30.0, "net": 1}]),
        n_nets=3,
    )
    mesh = build_mesh(d, d.pose0)
    a, b = (wm.build_wires(mesh, d, 4, (_NOVIA,) * 3, negotiate=1) for _ in range(2))
    # The old adopted swap guarded nets 0 and 1. Exercise the shove even when
    # the shared final guard safely refuses every candidate in this fixture.
    assert a.shove_grows > 0 and a.shove_rounds and a.shove_rounds[0]
    assert a.branches == b.branches
    assert a.order == b.order and a.order_pos == b.order_pos
    assert a.unreachable == b.unreachable
    assert a.shove_rounds == b.shove_rounds and a.shove_grows == b.shove_grows


def test_first_attempt_witness_failures_enter_the_shove(cell, monkeypatch):
    shove, _st = cell
    _pair_oracle(monkeypatch, 0, 1)
    cells = []

    def capture(shove, st):
        cells.append(st)
        return st

    monkeypatch.setattr(wm._Shove, "run", capture)
    wm.build_wires(shove.mesh, shove.d, 4, (_RULE,) * 3)
    (st,) = cells
    assert st.keys[1] == (1, 1), "the first-attempt 2D bigon must not look clean"
    assert st.partners[1] == [0]
