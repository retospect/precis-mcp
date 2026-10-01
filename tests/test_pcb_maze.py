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

import numpy as np
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


def test_a_rule_pitch_cap_refines_a_large_board_and_leaves_a_small_one():
    """Span / 400 on a 200 mm board is a 0.5 mm cell, which rounds the gap
    between fine-pitch pads shut. The design-rule cap refines the large
    board and never touches a board already finer than it."""
    large = grid_for([(0.0, 0.0), (200.0, 70.0)], n_layers=4, max_pitch_mm=0.15)
    small = grid_for([(0.0, 0.0), (40.0, 30.0)], n_layers=4, max_pitch_mm=0.15)
    assert large.pitch == pytest.approx(0.15)
    assert small.pitch == grid_for([(0.0, 0.0), (40.0, 30.0)], n_layers=4).pitch


def test_the_rule_pitch_cap_is_bounded_by_the_cell_limit():
    spec = grid_for(
        [(0.0, 0.0), (1000.0, 10.0)],
        n_layers=4,
        max_pitch_mm=0.05,
        max_cells_per_axis=2000,
    )
    assert spec.nx <= 2000 + 1
    assert spec.pitch == pytest.approx(1004.0 / 2000)


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


# ── budget exhaustion vs no path ─────────────────────────────────────────
def test_a_route_that_runs_out_of_budget_says_so():
    """Both outcomes return ``None``; only one of them proves there is no
    path. Conflating them reported a far-but-open connection as walled in."""
    grid = OccupancyGrid(_spec(n_layers=1), clearance_mm=0.05)
    path = grid.route(
        2, (0.5, 0.5), (5.5, 5.5), layers=[0], width_mm=0.1, max_expansions=5
    )
    assert path is None
    assert grid.last_route_exhausted is True


def test_a_walled_in_route_is_not_called_exhausted():
    grid = OccupancyGrid(_spec(n_layers=1), clearance_mm=0.05)
    for k in range(80):
        grid.stamp_disk((0,), 3.0, k * 0.1, 0.12, 1)
    assert grid.route(2, (1.0, 4.0), (5.0, 4.0), layers=[0], width_mm=0.1) is None
    assert grid.last_route_exhausted is False


def test_a_success_clears_the_exhausted_flag():
    grid = OccupancyGrid(_spec(n_layers=1), clearance_mm=0.05)
    grid.route(2, (0.5, 0.5), (5.5, 5.5), layers=[0], width_mm=0.1, max_expansions=5)
    assert grid.route(2, (0.5, 0.5), (1.5, 0.5), layers=[0], width_mm=0.1)
    assert grid.last_route_exhausted is False


def test_a_detour_outside_the_search_window_still_routes(monkeypatch):
    """The first search is boxed to the endpoints; a wall spanning the
    whole box forces a path around it, which only the full-grid retry can
    find."""
    import precis.pcb.maze as maze_mod

    monkeypatch.setattr(maze_mod, "WINDOW_MARGIN_MM", 0.5)
    monkeypatch.setattr(maze_mod, "WINDOW_SPAN_FRACTION", 0.0)
    grid = OccupancyGrid(_spec(nx=120, ny=120, n_layers=1), clearance_mm=0.05)
    for k in range(70):  # y = 0 .. 6.9 mm: a wall the box cannot get round
        grid.stamp_disk((0,), 5.0, k * 0.1, 0.12, 1)
    path = grid.route(2, (3.0, 3.0), (7.0, 3.0), layers=[0], width_mm=0.1)
    assert path is not None
    assert max(p[1] for p in path.points) > 3.0 + 0.5 + 1.0


def test_a_claim_just_outside_the_window_still_blocks_inside_it(monkeypatch):
    """The other-net mask is dilated from a read GROWN past the window, so
    copper whose clearance reaches in is not invisible to the search."""
    import precis.pcb.maze as maze_mod

    monkeypatch.setattr(maze_mod, "WINDOW_MARGIN_MM", 0.3)
    monkeypatch.setattr(maze_mod, "WINDOW_SPAN_FRACTION", 0.0)
    grid = OccupancyGrid(_spec(nx=80, ny=80, n_layers=1), clearance_mm=0.05)
    win = grid.search_window([(1.0, 2.5), (5.0, 2.5)])
    y_edge = grid.spec.y0 + win[1] * grid.spec.pitch  # first row past the box
    assert y_edge == pytest.approx(2.9)
    # A short run of copper on that first outside row, over the middle of
    # the connection. At width 0.6 its dilation reaches down through
    # y = 2.5, so the straight line is illegal and the path must dip.
    for k in range(11):
        grid.stamp_disk((0,), 2.5 + k * 0.1, y_edge + 0.05, 0.12, 1)
    path = grid.route(2, (1.0, 2.5), (5.0, 2.5), layers=[0], width_mm=0.6)
    assert path is not None
    assert min(p[1] for p in path.points) < 2.45, "routed through unseen copper"


def test_the_pad_keepout_mask_sees_a_pad_added_after_it_was_cached():
    """The mask is cached across route calls; a stale one would let a via
    land on a pad stamped after the first route."""
    grid = OccupancyGrid(_spec(n_layers=1), clearance_mm=0.05)
    grid.stamp_pad((0,), 1.0, 1.0, 0.2, 3)
    first = grid._pad_keepout_mask(0.2).copy()
    assert grid._pad_keepout_mask(0.2) is grid._pad_keepout_mask(0.2)
    grid.stamp_pad((0,), 4.0, 4.0, 0.2, 3)
    second = grid._pad_keepout_mask(0.2)
    ix, iy = grid.spec.to_cell(4.0, 4.0)
    assert not first[iy, ix]
    assert second[iy, ix]


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


def _walled_two_layer_grid() -> tuple[OccupancyGrid, GridSpec]:
    """A two-layer grid with net 1 sealing layer 0 across the FULL y range
    at x=3.0 (no gap), forcing any other net's route from x=1.0 to x=5.0 to
    dip onto layer 1 and back -- the same wall
    :func:`test_route_changes_layer_between_non_adjacent_signal_layers`
    uses, at two layers instead of four so the round trip is a single pair
    of vias, both landing on the straight line through ``y=4.0`` because
    nothing else on the board prefers any other y."""
    spec = _spec(n_layers=2)
    grid = OccupancyGrid(spec, clearance_mm=0.02)
    for k in range(80):
        grid.stamp_disk((0,), 3.0, k * 0.1, 0.12, 1)
    return grid, spec


def test_via_body_cost_pushes_the_via_out_of_a_masked_body_strip():
    """With no surcharge the cheapest crossing drops its via at (2.5, 4.0)
    -- dead on the straight line between start and goal, confirmed by
    :func:`_walled_two_layer_grid`'s own geometry. Marking that cell (and
    its immediate neighbourhood) as a component body and pricing a via
    there must move the via off it: a few tenths of a millimetre of lateral
    detour is cheaper than the surcharge, so the search should take that
    trade rather than pay to drop the via under the part."""
    grid, spec = _walled_two_layer_grid()
    mask = np.zeros((spec.ny, spec.nx), dtype=bool)
    cx, cy = spec.to_cell(2.5, 4.0)
    mask[cy - 3 : cy + 4, cx - 2 : cx + 3] = True
    grid.set_body_mask(mask)

    def masked(path):
        return [
            bool(mask[spec.to_cell(vx, vy)[1], spec.to_cell(vx, vy)[0]])
            for vx, vy, _lo, _hi in path.vias
        ]

    cheap = grid.route(
        2, (1.0, 4.0), (5.0, 4.0), layers=[0, 1], width_mm=0.05, via_dia_mm=0.3
    )
    assert cheap is not None and cheap.vias
    assert any(masked(cheap)), (
        "the unpriced baseline must actually exercise the body cell"
    )

    priced = grid.route(
        2,
        (1.0, 4.0),
        (5.0, 4.0),
        layers=[0, 1],
        width_mm=0.05,
        via_dia_mm=0.3,
        via_body_cost_mm=5.0,
    )
    assert priced is not None and priced.vias
    assert not any(masked(priced)), "a priced via must not land under the body"


def test_via_body_cost_mm_defaults_to_zero_and_unset_mask_is_a_no_op():
    """Every caller predating this feature never calls
    :meth:`OccupancyGrid.set_body_mask` and never passes
    ``via_body_cost_mm`` -- both must be exact no-ops: an unset mask (or
    one that is set but all-False) plus the ``via_body_cost_mm=0.0``
    default must reproduce the identical route byte-for-byte."""
    grid_unset, _spec_unset = _walled_two_layer_grid()
    path_unset = grid_unset.route(
        2, (1.0, 4.0), (5.0, 4.0), layers=[0, 1], width_mm=0.05, via_dia_mm=0.3
    )

    grid_masked, spec = _walled_two_layer_grid()
    grid_masked.set_body_mask(np.zeros((spec.ny, spec.nx), dtype=bool))
    path_masked = grid_masked.route(
        2,
        (1.0, 4.0),
        (5.0, 4.0),
        layers=[0, 1],
        width_mm=0.05,
        via_dia_mm=0.3,
        via_body_cost_mm=0.0,
    )

    assert path_unset is not None and path_masked is not None
    assert path_unset.points == path_masked.points
    assert path_unset.vias == path_masked.vias
    assert path_unset.length_mm == pytest.approx(path_masked.length_mm)


def test_stamp_pad_records_pad_and_still_claims_copper_like_stamp_disk():
    grid = OccupancyGrid(_spec(n_layers=1), clearance_mm=0.05)
    grid.stamp_pad((0,), 2.0, 2.0, 0.2, 3)
    assert grid.pads == ((2.0, 2.0, 0.2),)
    assert (grid.owner[0] == 3).any(), "stamp_pad must still claim copper"


def test_via_clears_pads_is_net_blind():
    """The one deliberately net-BLIND query on this grid: same-net copper
    is legal everywhere else (that is how a trace joins a pad), but a via
    is a drilled hole, not a trace, and must clear a pad regardless of
    whose net claimed it."""
    grid = OccupancyGrid(_spec(n_layers=1), clearance_mm=0.05)
    # `via_clears_pads` takes no net_id at all -- the rule is about the
    # PAD, not about who owns the copper crowding it, and the pad here is
    # claimed on the SAME net the via query stands in for.
    grid.stamp_pad((0,), 2.0, 2.0, 0.2, net_id=7)
    assert grid.via_clears_pads(2.0, 2.0, 0.15) is False  # squarely on the pad
    # Comfortably clear: pad edge (0.2) + via edge (0.15) + clearance
    # (0.05) = 0.4mm apart is exactly the boundary; go well past it.
    assert grid.via_clears_pads(3.0, 2.0, 0.15) is True


def test_route_never_places_a_via_within_pad_keepout_even_on_the_vias_own_net():
    """A thin wall on layer 0 forces the search to change layers right
    where a same-net pad sits — measured (without this fix) to land a via
    exactly on that pad's coordinate. Prove the negative property that
    matters: whatever vias the route DOES emit, none of them violate
    :meth:`OccupancyGrid.via_clears_pads` against the pad."""
    grid = OccupancyGrid(_spec(n_layers=4), clearance_mm=0.02)
    for k in range(80):
        grid.stamp_disk((0,), 3.0, k * 0.1, 0.12, 1)  # sealed on layer 0 only
    # Baseline (no pad): the search's own natural via site for this
    # geometry -- confirms the test isn't vacuous before adding the pad.
    baseline = grid.route(
        2, (1.0, 4.0), (5.0, 4.0), layers=[0, 3], width_mm=0.05, via_dia_mm=0.3
    )
    assert baseline is not None and baseline.vias
    natural_site = baseline.vias[0][:2]

    grid2 = OccupancyGrid(_spec(n_layers=4), clearance_mm=0.02)
    for k in range(80):
        grid2.stamp_disk((0,), 3.0, k * 0.1, 0.12, 1)
    # A pad on the SAME net right where the via naturally wants to go.
    grid2.stamp_pad((0,), natural_site[0], natural_site[1], 0.15, net_id=2)
    path = grid2.route(
        2, (1.0, 4.0), (5.0, 4.0), layers=[0, 3], width_mm=0.05, via_dia_mm=0.3
    )
    assert path is not None and path.vias, "a detour must still exist on this board"
    for vx, vy, _lo, _hi in path.vias:
        assert grid2.via_clears_pads(vx, vy, 0.15), (
            f"via at ({vx}, {vy}) violates the pad keep-out"
        )


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


def test_routed_copper_only_ever_lands_on_a_signal_layer():
    """The router may not put a trace on a PLANE layer, and nothing was
    checking.

    A via's barrel is copper on every layer it passes through, so
    ``stamp_path`` registers attach cells on all of them — correct, and
    connectivity depends on it. But ``route`` then used those cells as
    multi-source starts without filtering them back down to the layers it
    was actually given, so a net that already owned a through via could
    begin a later connection *inside the barrel* on an inner layer and run
    a trace along it. On the reference board that put three traces and
    three vias on In1.Cu, which shorts to the plane the moment one is
    poured.

    It was found by rendering the board and noticing the wrong colour.
    Every existing test asked whether copper overlapped, was reachable, or
    was connected; none asked *which layer it was on*.
    """
    ir = from_graph(_ladder_graph(6), stackup=DEFAULT_STACKUP)
    optimize(ir, OptimizeConfig(iters=800, seed=3))
    result = realize(ir, config=RealizeConfig(router="maze"))
    signal = {i for i, layer in enumerate(ir.stackup) if layer.get("role") == "signal"}
    assert signal, "fixture must have signal layers for this to mean anything"
    assert result.tracks, "a router that drew nothing satisfies this vacuously"

    stray = [t for t in result.tracks if t.layer not in signal and not t.is_dogbone]
    assert not stray, [(t.seg_id, t.layer, str(ir.net_name[t.net_id])) for t in stray]
    # A via may SPAN a plane layer (that is what a through via does); its
    # two ENDS are where copper is drawn, and those must be signal layers.
    stray_vias = [
        v for v in result.vias if v.layer_lo not in signal or v.layer_hi not in signal
    ]
    assert not stray_vias, [(v.seg_id, v.layer_lo, v.layer_hi) for v in stray_vias]


def test_unknown_router_is_rejected_rather_than_silently_defaulted():
    ir = from_graph(_ladder_graph(2), stackup=DEFAULT_STACKUP)
    optimize(ir, OptimizeConfig(iters=50, seed=8))
    with pytest.raises(ValueError, match="unknown router"):
        realize(ir, config=RealizeConfig(router="freerouting"))


def test_the_compiled_chord_check_agrees_with_disk_is_free_sample_by_sample() -> None:
    """`chord_is_free` replaced a Python `all(disk_is_free(...))` loop in
    path straightening; it must give the same answer on every chord,
    including chords that leave the grid and the off-by-rounding edges."""
    rng = np.random.default_rng(7)
    spec = GridSpec(-1.0, -2.0, 0.1, 120, 90, 2)
    grid = OccupancyGrid(spec, clearance_mm=0.1)
    for net in range(1, 6):
        for _ in range(8):
            grid.stamp_disk(
                (int(rng.integers(0, 2)),),
                float(rng.uniform(-1.0, 11.0)),
                float(rng.uniform(-2.0, 7.0)),
                float(rng.uniform(0.05, 0.6)),
                net,
            )
    agree = disagree = 0
    for _ in range(400):
        layer = int(rng.integers(-1, 3))
        a = (float(rng.uniform(-2.0, 12.0)), float(rng.uniform(-3.0, 8.0)))
        b = (float(rng.uniform(-2.0, 12.0)), float(rng.uniform(-3.0, 8.0)))
        n = int(rng.integers(1, 40))
        radius = float(rng.uniform(0.0, 0.5))
        net = int(rng.integers(1, 6))
        expected = all(
            grid.disk_is_free(
                (layer,),
                a[0] + (b[0] - a[0]) * k / n,
                a[1] + (b[1] - a[1]) * k / n,
                radius,
                net,
            )
            for k in range(n + 1)
        )
        got = grid.chord_is_free(layer, a, b, n, radius, net)
        if got == expected:
            agree += 1
        else:
            disagree += 1
    assert disagree == 0, f"{disagree} of {agree + disagree} chords disagree"


@pytest.mark.parametrize(("ny", "nx"), [(40, 50), (1, 30), (3, 3), (40, 1)])
def test_the_keep_out_is_a_euclidean_disk_including_at_the_grid_edge(
    ny: int, nx: int
) -> None:
    """The router's keep-out was a dilation documented as Chebyshev that
    built an L1 diamond, which reaches only r/sqrt(2) along a diagonal, so
    a wide track's clearance fell short there. `_disk_hits_foreign` must
    agree with a brute-force disk at every cell, on grids thinner than the
    radius too."""
    from precis.pcb.maze import _disk_half_chords, _disk_hits_foreign

    rng = np.random.default_rng(ny * 100 + nx)
    owner = np.full((2, ny, nx), FREE, dtype=np.int32)
    owner[rng.random((2, ny, nx)) < 0.05] = 7
    owner[rng.random((2, ny, nx)) < 0.05] = 3  # this net's own copper
    yy, xx = np.mgrid[0:ny, 0:nx]
    for r in range(8):
        half = _disk_half_chords(r)
        for layer in range(2):
            foreign = owner[layer] == 7
            for y in range(ny):
                for x in range(nx):
                    want = bool(
                        (foreign & ((yy - y) ** 2 + (xx - x) ** 2 <= r * r)).any()
                    )
                    got = _disk_hits_foreign(owner, layer, y, x, r, half, 3)
                    assert got == want, (r, layer, y, x)
