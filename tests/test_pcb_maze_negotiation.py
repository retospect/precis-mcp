"""Negotiated congestion — :class:`precis.pcb.maze.Negotiation` and its
commit through :meth:`precis.pcb.maze.OccupancyGrid.path_is_legal`.

The hard grid's guarantee (no overlapping copper) must survive this
feature untouched, so the commit side is tested as hard as the search:
a negotiated path is a proposal, and only a path the hard search could
itself have walked may be committed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from precis.pcb import DEFAULT_STACKUP
from precis.pcb import realize as realize_mod
from precis.pcb.geom import segments_cross
from precis.pcb.ir import from_graph
from precis.pcb.maze import (
    GridSpec,
    Negotiation,
    OccupancyGrid,
    RoutePath,
    path_samples,
)
from precis.pcb.optimize import OptimizeConfig, optimize
from precis.pcb.realize import RealizeConfig, realize


def _spec(nx: int = 80, ny: int = 80, n_layers: int = 1) -> GridSpec:
    return GridSpec(0.0, 0.0, 0.1, nx, ny, n_layers)


def _claims(grid: OccupancyGrid, path: RoutePath, width: float) -> list:
    r = grid.core_radius_mm(width)
    return [
        (x, y, lo, hi, r) for x, y, lo, hi in path_samples(path, grid.spec.pitch / 2)
    ]


def _wall_path(net: int, x: float, y0: float, y1: float) -> RoutePath:
    """A vertical run of copper, as a route result would describe it."""
    return RoutePath(net, ((x, y0, 0), (x, y1, 0)), y1 - y0)


def test_a_negotiated_claim_is_exactly_what_a_hard_stamp_claims():
    """Usage must count the cells the commit will own, or the negotiation
    settles on a split the hard grid then refuses."""
    spec = _spec(n_layers=2)
    hard = OccupancyGrid(spec, clearance_mm=0.05)
    path = hard.route(4, (1.0, 1.0), (6.0, 5.0), layers=[0, 1], width_mm=0.15)
    assert path is not None
    hard.stamp_path(path, 0.15)
    neg = Negotiation(spec)
    cells = neg.claim_cells(_claims(hard, path, 0.15))
    assert set(cells.tolist()) == set(
        np.flatnonzero(hard.owner.reshape(-1) == 4).tolist()
    )


def test_another_nets_copper_is_a_price_not_a_wall():
    """The wall that makes a hard route fail (test_pcb_maze's
    ``test_route_refuses_to_cross_another_nets_copper``) is crossable while
    negotiating — and the crossing is reported as a conflict."""
    spec = _spec()
    grid = OccupancyGrid(spec, clearance_mm=0.05)
    neg = Negotiation(spec)
    wall = _wall_path(1, 3.0, 0.0, 7.9)
    neg.set_net(1, [neg.claim_cells(_claims(grid, wall, 0.1))])
    path = grid.route(
        2, (1.0, 4.0), (5.0, 4.0), layers=[0], width_mm=0.1, negotiation=neg
    )
    assert path is not None
    assert neg.conflicts(2, path, width_mm=0.1, via_dia_mm=None).shape[0] > 0


def test_a_rising_price_moves_a_net_onto_its_detour():
    """A wall with one gap well off the straight line: cheap congestion
    goes straight through, expensive congestion goes round — and the
    detour is conflict-free."""
    spec = _spec()
    grid = OccupancyGrid(spec, clearance_mm=0.05)
    neg = Negotiation(spec)
    wall = [
        neg.claim_cells(_claims(grid, _wall_path(1, 3.0, 0.0, 5.4), 0.1)),
        neg.claim_cells(_claims(grid, _wall_path(1, 3.0, 6.6, 7.9), 0.1)),
    ]
    neg.set_net(1, wall)

    neg.pres_fac = 0.01
    cheap = grid.route(
        2, (1.0, 2.0), (5.0, 2.0), layers=[0], width_mm=0.1, negotiation=neg
    )
    assert cheap is not None
    assert neg.conflicts(2, cheap, width_mm=0.1, via_dia_mm=None).shape[0] > 0

    neg.pres_fac = 100.0
    dear = grid.route(
        2, (1.0, 2.0), (5.0, 2.0), layers=[0], width_mm=0.1, negotiation=neg
    )
    assert dear is not None
    assert neg.conflicts(2, dear, width_mm=0.1, via_dia_mm=None).shape[0] == 0
    assert max(y for _x, y, _l in dear.points) > 5.4


def test_history_alone_pushes_a_net_off_a_contested_corridor():
    """With no present congestion at all, accumulated history on the
    straight corridor is still a cost the search routes around."""
    spec = _spec()
    grid = OccupancyGrid(spec, clearance_mm=0.05)
    neg = Negotiation(spec, pres_fac=0.0, hist_fac=50.0)
    straight = grid.route(2, (1.0, 4.0), (6.0, 4.0), layers=[0], width_mm=0.1)
    assert straight is not None
    assert {round(y, 6) for _x, y, _l in straight.points} == {4.0}
    neg.hist[0, 35:46, 20:41] = 1.0
    moved = grid.route(
        2, (1.0, 4.0), (6.0, 4.0), layers=[0], width_mm=0.1, negotiation=neg
    )
    assert moved is not None
    assert any(abs(y - 4.0) > 0.5 for _x, y, _l in moved.points)


def test_ripping_a_net_returns_its_cells():
    spec = _spec()
    grid = OccupancyGrid(spec, clearance_mm=0.05)
    neg = Negotiation(spec)
    a = neg.claim_cells(_claims(grid, _wall_path(1, 3.0, 1.0, 6.0), 0.1))
    b = neg.claim_cells(_claims(grid, _wall_path(1, 3.0, 4.0, 7.0), 0.1))
    neg.set_net(1, [a, b])
    assert neg.usage.max() == 1, "one net's overlapping connections count once"
    neg.set_net(1, [a])
    assert int(neg.usage.sum()) == len(np.unique(a))
    neg.rip_net(1)
    assert int(neg.usage.sum()) == 0


def test_a_path_the_search_found_is_legal_until_someone_crosses_it():
    spec = _spec()
    grid = OccupancyGrid(spec, clearance_mm=0.05)
    path = grid.route(2, (1.0, 4.0), (6.0, 4.0), layers=[0], width_mm=0.1)
    assert path is not None
    kwargs: dict[str, Any] = {
        "width_mm": 0.1,
        "via_dia_mm": None,
        "start_exempt": True,
        "goal_exempt": True,
    }
    assert grid.path_is_legal(path, **kwargs)
    grid.stamp_path(_wall_path(1, 3.0, 0.0, 7.9), 0.1)
    assert not grid.path_is_legal(path, **kwargs)


def test_a_path_with_a_via_is_illegal_without_via_geometry():
    """``route`` never places a via it has no diameter for; the commit must
    not accept one either."""
    spec = _spec(n_layers=2)
    grid = OccupancyGrid(spec, clearance_mm=0.05)
    path = RoutePath(
        2, ((1.0, 1.0, 0), (2.0, 1.0, 0), (2.0, 1.0, 1), (3.0, 1.0, 1)), 2.0
    )
    assert not grid.path_is_legal(
        path, width_mm=0.1, via_dia_mm=None, start_exempt=True, goal_exempt=True
    )
    assert grid.path_is_legal(
        path, width_mm=0.1, via_dia_mm=0.4, start_exempt=True, goal_exempt=True
    )


def test_an_attach_point_is_only_where_the_copper_still_is():
    spec = _spec()
    grid = OccupancyGrid(spec, clearance_mm=0.05)
    trunk = RoutePath(3, ((1.0, 1.0, 0), (5.0, 1.0, 0)), 4.0)
    grid.register_attach(trunk)
    # The trunk's last sample in a cell is the point recorded there; its
    # far end is one for certain.
    assert grid.is_attach_point(3, 5.0, 1.0, 0)
    assert not grid.is_attach_point(3, 5.0, 1.0 + 1e-3, 0), "a point beside the trunk"
    assert not grid.is_attach_point(4, 5.0, 1.0, 0), "another net's copper"
    grid.forget_attach(3)
    assert not grid.is_attach_point(3, 5.0, 1.0, 0)


def _ladder_graph(n: int) -> dict:
    return {
        "instances": [{"refdes": f"L{i}"} for i in range(n)]
        + [{"refdes": f"R{i}"} for i in range(n)],
        "nets": [
            {
                "name": f"N{i}",
                "members": [
                    {"refdes": f"L{i}", "pin": "1"},
                    {"refdes": f"R{n - 1 - i}", "pin": "1"},
                ],
            }
            for i in range(n)
        ],
    }


def _assert_no_crossings(result) -> None:
    placed = [
        (t.net_id, t.layer, tuple(s["start"][:2]), tuple(s["end"][:2]))
        for t in result.tracks
        for s in t.segments
    ]
    for i, (na, la, a1, a2) in enumerate(placed):
        for nb, lb, b1, b2 in placed[i + 1 :]:
            if na != nb and la == lb:
                assert not segments_cross(a1, a2, b1, b2), f"nets {na}/{nb} cross"


def test_negotiation_is_off_by_default_and_never_runs_on_a_clean_board(monkeypatch):
    """Default off (review R2); and even when asked for, it runs only
    when the re-ordering passes left a net unrouted."""
    calls: list[int] = []
    real = realize_mod._negotiate

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(realize_mod, "_negotiate", spy)
    assert RealizeConfig().negotiate_iterations == 0
    ir = from_graph(_ladder_graph(8), stackup=DEFAULT_STACKUP)
    optimize(ir, OptimizeConfig(iters=300, seed=3))
    clean = realize(ir, config=RealizeConfig(router="maze", negotiate_iterations=10))
    assert not clean.unrouted
    assert not calls
    assert clean.negotiation is None


def test_a_converged_proposal_commits_verbatim_every_segment(monkeypatch):
    """Review R1: a conflict-free negotiation is hard-legal as it stands,
    so the commit must take EVERY proposal unchanged. A straighten before
    commit moved parents out from under their branches' attach points.

    The hard pass is made to report everything unrouted so the
    negotiate-and-commit path runs on a board that converges."""
    real_pass = realize_mod._route_pass
    real_negotiate = realize_mod._negotiate
    seen: dict[str, object] = {}
    accepted: list[int] = []

    def route_pass(*a, **k):
        preferred = k.get("preferred")
        if preferred:
            # The realizer passes its own list (for the report); read it.
            k.setdefault("accepted", accepted)
        out = real_pass(*a, **k)
        if not preferred:  # a hard pass: pretend it all failed
            return out[0], out[1], list(a[1]), out[3]
        if k["accepted"] is not accepted:
            accepted.extend(k["accepted"])
        seen["commit_unrouted"] = list(out[2])
        return out

    def negotiate(*a, **k):
        proposal, conflicted, report = real_negotiate(*a, **k)
        seen["proposal"] = proposal
        seen["conflicted"] = conflicted
        seen["report"] = report
        return proposal, conflicted, report

    monkeypatch.setattr(realize_mod, "_route_pass", route_pass)
    monkeypatch.setattr(realize_mod, "_negotiate", negotiate)
    ir = from_graph(_ladder_graph(6), stackup=DEFAULT_STACKUP)
    optimize(ir, OptimizeConfig(iters=300, seed=3))
    result = realize(
        ir, config=RealizeConfig(router="maze", route_passes=1, negotiate_iterations=10)
    )
    proposal = seen["proposal"]
    assert isinstance(proposal, dict) and proposal
    assert seen["conflicted"] == set(), "the fixture did not converge"
    assert sorted(accepted) == sorted(proposal), "a converged proposal was re-searched"
    assert seen["commit_unrouted"] == []
    assert not result.unrouted
    _assert_no_crossings(result)
    # The report says what happened, and the caller's copy carries the
    # commit's half (accepted/won) the loop itself cannot know.
    inner = seen["report"]
    assert isinstance(inner, realize_mod.NegotiationReport)
    assert inner.converged and inner.conflicted[-1] == 0
    assert inner.iterations_run == len(inner.conflicted) >= 1
    assert inner.proposals == len(proposal) and not inner.out_of_time
    assert inner.accepted == 0 and not inner.won
    outer = result.negotiation
    assert outer is not None
    assert (outer.iterations_run, outer.conflicted) == (
        inner.iterations_run,
        inner.conflicted,
    )
    assert outer.accepted == len(proposal) and outer.won
    assert "converged" in outer.line() and "result taken" in outer.line()
    assert outer.as_dict()["converged"] is True
    # Verbatim: every committed track run starts on its proposal's copper.
    starts = {
        (round(t.segments[0]["start"][0], 6), round(t.segments[0]["start"][1], 6))
        for t in result.tracks
        if t.segments
    }
    for prop in proposal.values():
        x, y, _layer = prop.copper.points[0]
        assert (round(x, 6), round(y, 6)) in starts


def test_an_exhausted_search_keeps_its_previous_proposal(monkeypatch):
    """Review R2: a late iteration that runs out of expansions must not
    drop a net it had already routed."""
    from precis.pcb import maze as maze_mod

    real_route = maze_mod.OccupancyGrid.route
    calls = {"n": 0}

    def route(self, net_id, start, goal, **kw):
        if kw.get("negotiation") is None:
            return real_route(self, net_id, start, goal, **kw)
        calls["n"] += 1
        if calls["n"] > 6:  # every search after the first iteration exhausts
            self.last_route_exhausted = True
            return None
        return real_route(self, net_id, start, goal, **kw)

    monkeypatch.setattr(maze_mod.OccupancyGrid, "route", route)
    real_conflicts = maze_mod.Negotiation.conflicts
    monkeypatch.setattr(  # force a second iteration
        maze_mod.Negotiation,
        "conflicts",
        lambda self, net_id, path, **kw: (
            np.array([0])
            if calls["n"] <= 6
            else real_conflicts(self, net_id, path, **kw)
        ),
    )
    seen: dict[str, object] = {}
    real_negotiate = realize_mod._negotiate

    def negotiate(*a, **k):
        out = real_negotiate(*a, **k)
        seen["proposal"] = out[0]
        return out

    monkeypatch.setattr(realize_mod, "_negotiate", negotiate)
    real_pass = realize_mod._route_pass

    def route_pass(*a, **k):
        out = real_pass(*a, **k)
        return out if k.get("preferred") else (out[0], out[1], list(a[1]), out[3])

    monkeypatch.setattr(realize_mod, "_route_pass", route_pass)
    ir = from_graph(_ladder_graph(6), stackup=DEFAULT_STACKUP)
    optimize(ir, OptimizeConfig(iters=300, seed=3))
    realize(
        ir, config=RealizeConfig(router="maze", route_passes=1, negotiate_iterations=3)
    )
    assert calls["n"] > 6, "no second iteration ran"
    assert len(seen["proposal"]) == 6  # type: ignore[arg-type]


def test_negotiation_stops_at_its_wall_clock_budget(monkeypatch):
    real_pass = realize_mod._route_pass

    def route_pass(*a, **k):
        out = real_pass(*a, **k)
        return out if k.get("preferred") else (out[0], out[1], list(a[1]), out[3])

    monkeypatch.setattr(realize_mod, "_route_pass", route_pass)
    seen: dict[str, object] = {}
    real_negotiate = realize_mod._negotiate

    def negotiate(*a, **k):
        out = real_negotiate(*a, **k)
        seen["proposal"] = out[0]
        return out

    monkeypatch.setattr(realize_mod, "_negotiate", negotiate)
    ir = from_graph(_ladder_graph(6), stackup=DEFAULT_STACKUP)
    optimize(ir, OptimizeConfig(iters=300, seed=3))
    result = realize(
        ir,
        config=RealizeConfig(
            router="maze",
            route_passes=1,
            negotiate_iterations=5,
            negotiate_budget_s=0.0,
        ),
    )
    assert seen["proposal"] == {}, "an iteration ran past a zero budget"
    report = result.negotiation
    assert report is not None and report.out_of_time
    assert report.iterations_run == 0 and report.conflicted == ()
    assert not report.converged and not report.won and report.accepted == 0
    assert "out of time" in report.line() and "no iteration completed" in report.line()


def test_the_budget_is_checked_per_net_not_per_iteration(monkeypatch):
    """Review N1: one iteration on a real board can run for minutes, so the
    deadline must stop it between nets. The clock runs out after the first
    net routes; nothing after it may."""
    real_pass = realize_mod._route_pass

    def route_pass(*a, **k):
        out = real_pass(*a, **k)
        return out if k.get("preferred") else (out[0], out[1], list(a[1]), out[3])

    monkeypatch.setattr(realize_mod, "_route_pass", route_pass)
    seen: dict[str, Any] = {}
    real_negotiate = realize_mod._negotiate

    def negotiate(*a, **k):
        # deadline = 0 + budget; first net check at 0; every later check late
        ticks = iter([0.0, 0.0])
        clock = SimpleNamespace(monotonic=lambda: next(ticks, 1e9))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(realize_mod, "time", clock)
            out = real_negotiate(*a, **k)
        seen["proposal"] = out[0]
        return out

    monkeypatch.setattr(realize_mod, "_negotiate", negotiate)
    ir = from_graph(_ladder_graph(6), stackup=DEFAULT_STACKUP)
    optimize(ir, OptimizeConfig(iters=300, seed=3))
    realize(
        ir,
        config=RealizeConfig(
            router="maze",
            route_passes=1,
            negotiate_iterations=5,
            negotiate_budget_s=1.0,
        ),
    )
    proposal = seen["proposal"]
    nets = {int(ir.seg_net[s]) for s in proposal}
    assert len(nets) == 1, f"routed {len(nets)} nets past the deadline"
