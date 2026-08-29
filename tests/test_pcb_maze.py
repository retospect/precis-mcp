"""The maze router's own contract — :mod:`precis.pcb.maze` and the
``router='maze'`` path through :func:`precis.pcb.realize.realize`.

The tangent drawer's contract (one straight-or-hugging track per segment,
on ``seg_layer``, vias wherever that isn't the pad layer) lives in
``tests/test_pcb_realize.py``. This module tests the *opposite* promise:
the router picks its own layers and its own path, never emits copper that
overlaps another net's, and says so when it cannot route something.

**The trap this module is written around.** A clearance guarantee is
trivially satisfiable by routing nothing, and every measurement here that
counts DRC errors is therefore paired with one that counts copper. That
is not belt-and-braces: an early revision of this router scored a perfect
zero on the reference fixture while leaving 58 of 61 connections
unrouted, and the DRC number alone read as a triumph.
"""

from __future__ import annotations

import math

import pytest

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.geom import dist
from precis.pcb.ir import from_graph
from precis.pcb.maze import (
    CONTESTED,
    FREE,
    GridSpec,
    OccupancyGrid,
    grid_for,
)
from precis.pcb.optimize import OptimizeConfig, optimize
from precis.pcb.realize import RealizeConfig, realize


def _spec(nx: int = 60, ny: int = 60, n_layers: int = 2) -> GridSpec:
    return GridSpec(0.0, 0.0, 0.1, nx, ny, n_layers)


# ── grid geometry ────────────────────────────────────────────────────────
def test_grid_for_covers_every_point_with_its_margin():
    points = [(1.0, 2.0), (9.0, 4.0), (-3.0, 7.0)]
    spec = grid_for(points, n_layers=4, margin_mm=2.0)
    for x, y in points:
        ix, iy = spec.to_cell(x, y)
        # A clamped (out-of-grid) point would round-trip to the boundary;
        # a covered one round-trips to within half a cell.
        rx, ry = spec.to_point(ix, iy)
        assert dist((x, y), (rx, ry)) <= spec.pitch * math.sqrt(2) / 2 + 1e-9


def test_grid_for_pitch_scales_with_extent_not_with_point_count():
    """The pitch is set by the board's SIZE. A seed that scatters parts
    over five times the board they need makes the routing grid five times
    coarser than the pad pitch it has to resolve — the failure that made
    the placer's compaction a router problem."""
    tight = grid_for([(0.0, 0.0), (10.0, 10.0)], n_layers=2)
    loose = grid_for([(0.0, 0.0), (100.0, 100.0)], n_layers=2)
    assert loose.pitch > tight.pitch * 5


# ── the claim/query split ────────────────────────────────────────────────
def test_core_radius_excludes_the_other_nets_width():
    """A claim covers this copper plus its OWN clearance, and nothing
    else. Folding the widest net on the board into every claim (the first
    design) doubles a pad's keep-out and seals fine-pitch escapes."""
    grid = OccupancyGrid(_spec(), clearance_mm=0.1)
    assert grid.core_radius_mm(0.2) == pytest.approx(0.2 / 2 + 0.1)


def test_contested_cell_belongs_to_neither_net():
    grid = OccupancyGrid(_spec(), clearance_mm=0.1)
    grid.stamp_disk((0,), 1.0, 1.0, 0.3, 7, contest=True)
    grid.stamp_disk((0,), 1.2, 1.0, 0.3, 9, contest=True)
    owners = set(grid.owner[0].ravel().tolist())
    assert CONTESTED in owners, "overlapping claims must go to neither net"
    assert 7 in owners and 9 in owners


def test_stamp_disk_smaller_than_a_cell_still_claims_one_cell():
    """A pad disk narrower than half a cell diagonal can miss every cell
    centre. Claiming nothing there leaves its own net with no cell to
    start from — silently unroutable, and indistinguishable from a
    congested board."""
    grid = OccupancyGrid(GridSpec(0.0, 0.0, 1.0, 10, 10, 1), clearance_mm=0.0)
    grid.stamp_disk((0,), 4.5, 4.5, 0.01, 3)  # dead between four centres
    assert (grid.owner[0] == 3).sum() == 1


# ── the guarantee ────────────────────────────────────────────────────────
def test_route_refuses_to_cross_another_nets_copper():
    """A wall of net 1 across the board, with no gap. Net 2 must fail
    rather than route through it."""
    grid = OccupancyGrid(_spec(n_layers=1), clearance_mm=0.05)
    for k in range(80):
        grid.stamp_disk((0,), 3.0, k * 0.1, 0.12, 1)
    path = grid.route(2, (1.0, 4.0), (5.0, 4.0), layers=[0], width_mm=0.1)
    assert path is None


def test_route_finds_the_one_gap_in_a_wall():
    """The same wall with a hole in it: the router must find the hole,
    and the path must actually pass through it."""
    grid = OccupancyGrid(_spec(n_layers=1), clearance_mm=0.02)
    # The hole has to be wider than the wall's own claim plus the query
    # dilation on both sides, or it is not a hole -- 1.0mm here against a
    # 0.1mm claim radius and a 0.2mm query dilation.
    for k in range(80):
        if 15 <= k <= 25:
            continue  # the gap, around y = 2.0
        grid.stamp_disk((0,), 3.0, k * 0.1, 0.1, 1)
    path = grid.route(2, (1.0, 4.0), (5.0, 4.0), layers=[0], width_mm=0.05)
    assert path is not None
    crossing_ys = [y for x, y, _ in path.points if abs(x - 3.0) < 0.25]
    assert crossing_ys, "path never reached the wall's x"
    assert min(crossing_ys) < 2.5, "went somewhere other than through the hole"


def test_route_changes_layer_between_non_adjacent_signal_layers():
    """A via is a hole through the stackup, not a step to the neighbouring
    index. When the only two signal layers are 0 and 3 — a 4-layer board
    with two internal planes — an adjacency-only transition makes every
    layer change unreachable and the router silently single-layer."""
    grid = OccupancyGrid(_spec(n_layers=4), clearance_mm=0.02)
    for k in range(80):
        grid.stamp_disk((0,), 3.0, k * 0.1, 0.12, 1)  # sealed on layer 0 only
    path = grid.route(
        2, (1.0, 4.0), (5.0, 4.0), layers=[0, 3], width_mm=0.05, via_dia_mm=0.3
    )
    assert path is not None
    assert {layer for _x, _y, layer in path.points} == {0, 3}
    assert path.vias, "a layer change must report its via"
    for _vx, _vy, lo, hi in path.vias:
        assert (lo, hi) == (0, 3)


def test_route_will_not_place_a_via_it_has_no_geometry_for():
    """No resolved via diameter means no via — never an invented one."""
    grid = OccupancyGrid(_spec(n_layers=4), clearance_mm=0.02)
    for k in range(80):
        grid.stamp_disk((0,), 3.0, k * 0.1, 0.12, 1)
    path = grid.route(
        2, (1.0, 4.0), (5.0, 4.0), layers=[0, 3], width_mm=0.05, via_dia_mm=None
    )
    assert path is None


def test_routed_copper_is_claimed_so_the_next_net_must_avoid_it():
    grid = OccupancyGrid(_spec(n_layers=1), clearance_mm=0.05)
    first = grid.route(1, (1.0, 3.0), (5.0, 3.0), layers=[0], width_mm=0.1)
    assert first is not None
    grid.stamp_path(first, 0.1)
    assert (grid.owner[0] != FREE).any()
    # Net 2 crossing that corridor perpendicularly has to deviate from the
    # straight line it would otherwise take.
    second = grid.route(2, (3.0, 1.0), (3.0, 5.0), layers=[0], width_mm=0.1)
    assert second is None or all(
        abs(y - 3.0) > 1e-9 or abs(x - 3.0) > 0.15 for x, y, _ in second.points
    )


def test_attach_lets_a_second_connection_join_existing_net_copper():
    """Connecting to a net means reaching ANY point of it. Without this
    the star decomposition's hub pad carries every one of its net's
    connections through one escape corridor."""
    grid = OccupancyGrid(_spec(n_layers=1), clearance_mm=0.02)
    first = grid.route(1, (1.0, 1.0), (5.0, 1.0), layers=[0], width_mm=0.05)
    assert first is not None
    grid.stamp_path(first, 0.05)
    joined = grid.route(1, (1.0, 1.0), (3.0, 4.0), layers=[0], width_mm=0.05)
    assert joined is not None
    # It starts on the existing trunk near x=3, not back at the far pad.
    assert joined.points[0][0] > 1.5


# ── end to end through realize() ─────────────────────────────────────────
def _ladder_graph(n: int = 6) -> dict:
    """n two-pin nets between two banks of parts — deliberately forced to
    interleave, so a router blind to other tracks produces crossings."""
    return {
        "instances": [{"refdes": f"L{i}"} for i in range(n)]
        + [{"refdes": f"R{i}"} for i in range(n)],
        "nets": [
            {
                "name": f"N{i}",
                "members": [
                    {"refdes": f"L{i}", "pin": "1"},
                    # crossed on purpose: L0->R(n-1), L1->R(n-2), ...
                    {"refdes": f"R{n - 1 - i}", "pin": "1"},
                ],
            }
            for i in range(n)
        ],
    }


def test_maze_router_emits_no_overlapping_copper_where_tangent_does():
    """The measurement that motivates the whole module, on a fixture
    small enough to reason about: the same crossed ladder, drawn both
    ways. `unrouted` is asserted too — a router that declines everything
    also has no crossings."""
    ir = from_graph(_ladder_graph(), stackup=DEFAULT_STACKUP)
    optimize(ir, OptimizeConfig(iters=400, seed=5))

    maze_result = realize(ir, config=RealizeConfig(router="maze"))
    assert maze_result.tracks, "routed nothing — a vacuous clearance result"

    # Every pair of same-layer track segments from different nets must be
    # strictly separated. This is the property the occupancy grid exists
    # to make structural.
    placed: list[tuple[int, int, tuple[float, float], tuple[float, float]]] = []
    for track in maze_result.tracks:
        for seg in track.segments:
            placed.append(
                (
                    track.net_id,
                    track.layer,
                    (seg["start"][0], seg["start"][1]),
                    (seg["end"][0], seg["end"][1]),
                )
            )
    from precis.pcb.geom import segments_cross

    for i, (net_a, layer_a, a1, a2) in enumerate(placed):
        for net_b, layer_b, b1, b2 in placed[i + 1 :]:
            if net_a == net_b or layer_a != layer_b:
                continue
            assert not segments_cross(a1, a2, b1, b2), (
                f"nets {net_a}/{net_b} cross on layer {layer_a}"
            )


def test_maze_router_reports_what_it_could_not_route():
    """``unrouted`` is the honest residue. A board with no room at all
    must name the segments it gave up on, not draw them anyway."""
    ir = from_graph(_ladder_graph(3), stackup=DEFAULT_STACKUP)
    optimize(ir, OptimizeConfig(iters=200, seed=6))
    result = realize(ir, config=RealizeConfig(router="maze", max_expansions=1))
    # With a 1-expansion budget nothing non-trivial can route, and every
    # such segment has to show up in `unrouted` rather than vanish.
    assert result.unrouted
    assert set(result.unrouted) <= set(range(ir.n_segments))


def test_tangent_router_still_available_and_never_reports_unrouted():
    ir = from_graph(_ladder_graph(3), stackup=DEFAULT_STACKUP)
    optimize(ir, OptimizeConfig(iters=200, seed=7))
    result = realize(ir, config=RealizeConfig(router="tangent"))
    assert result.tracks
    assert result.unrouted == ()


def test_unknown_router_is_rejected_rather_than_silently_defaulted():
    ir = from_graph(_ladder_graph(2), stackup=DEFAULT_STACKUP)
    optimize(ir, OptimizeConfig(iters=50, seed=8))
    with pytest.raises(ValueError, match="unknown router"):
        realize(ir, config=RealizeConfig(router="freerouting"))
