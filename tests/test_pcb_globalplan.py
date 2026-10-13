"""Unit tests for the tile-capacity plan's strict model (slice 0b(a) of
docs/backlog/pcb-global-plan-router.md): participant copper blocks lanes,
a net's own copper is transparent only on the boundaries it narrows, and
a planned through-via is charged against its tile's lanes."""

from __future__ import annotations

from precis.pcb import globalplan
from precis.pcb.maze import GridSpec, OccupancyGrid

# 3 mm x 3 mm, 0.05 mm cells, two layers -> 6 x 6 tiles of 0.5 mm.
TRACK = 0.1
CLEARANCE = 0.1
VIA_DIA = 0.45


def _grid() -> OccupancyGrid:
    grid = OccupancyGrid(GridSpec(0.0, 0.0, 0.05, 60, 60, 2), clearance_mm=0.0)
    # Net 1's plaza via fills tile (2, 2) on both layers; net 2 owns a via
    # far away in tile (0, 5).
    grid.stamp_disk((0, 1), 1.25, 1.25, 0.3, 1)
    grid.stamp_disk((0, 1), 0.25, 2.75, 0.2, 2)
    return grid


def _graph(*, strict: bool) -> globalplan.TileGraph:
    return globalplan.build_tile_graph(
        _grid(),
        layers=(0, 1),
        track_mm=TRACK,
        clearance_mm=CLEARANCE,
        via_dia_mm=VIA_DIA,
        participant_nets={1, 2},
        strict=strict,
    )


def test_strict_graph_blocks_a_participant_via_for_everyone_else():
    open_graph = _graph(strict=False)
    strict_graph = _graph(strict=True)
    # Boundary between tiles (2, 2) and (3, 2) on layer 0.
    assert open_graph.cap_x[0, 2, 2] == 2
    assert strict_graph.cap_x[0, 2, 2] == 0
    # Net 1's own via is transparent to net 1 there; net 2 has no entry.
    assert strict_graph.exit_cap[("x", 0, 2, 2)] == {1: 2}
    assert open_graph.exit_cap == {}
    assert strict_graph.via_lanes == int((VIA_DIA + CLEARANCE) // (TRACK + CLEARANCE))
    assert open_graph.via_lanes == 0


def test_a_net_leaves_its_own_tile_and_others_detour_around_it():
    graph = _graph(strict=True)
    nets = [
        # Net 1 starts inside its own via's tile and ends three tiles right.
        globalplan.PlanNet(
            seg_id=10,
            net_id=1,
            name="N1",
            sources=(graph.node(0, 2, 2),),
            sink=graph.node(0, 5, 2),
        ),
        # Net 2 runs along row 2 from the left edge to the right edge.
        globalplan.PlanNet(
            seg_id=20,
            net_id=2,
            name="N2",
            sources=(graph.node(0, 0, 2),),
            sink=graph.node(0, 4, 2),
        ),
    ]
    plan = globalplan.plan_routes(graph, nets)
    assert plan.converged
    assert set(plan.paths) == {10, 20}
    n2_tiles = {graph.node_parts(n)[1:] for n in plan.paths[20].nodes}
    assert (2, 2) not in n2_tiles


def test_a_planned_via_is_charged_on_its_tile_lanes_but_not_its_own():
    graph = _graph(strict=True)
    # Arrive at tile (4, 4) on layer 0 from the left, via to layer 1.
    nodes = (graph.node(0, 3, 4), graph.node(0, 4, 4), graph.node(1, 4, 4))
    exs, eys, vts = globalplan._resources(graph, nodes)
    assert exs == [(0, 4, 3)] and vts == [4 * graph.ntx + 4]
    cxs, cys = globalplan._via_charge(graph, exs, eys, vts)
    # Four boundaries of an interior tile on two layers, minus the one
    # the path's own track crosses.
    assert len(cxs) + len(cys) == 2 * 4 - 1
    assert (0, 4, 3) not in cxs and (1, 4, 3) in cxs
    # The open model charges nothing.
    assert globalplan._via_charge(_graph(strict=False), exs, eys, vts) == ([], [])


def test_a_lone_via_fills_its_tile_without_conflicting_with_itself():
    graph = _graph(strict=True)
    # Layer-changing net in an empty corner: tile (4, 0) L0 -> via -> L1 (5, 0).
    net = globalplan.PlanNet(
        seg_id=30,
        net_id=3,
        name="N3",
        sources=(graph.node(0, 3, 0),),
        sink=graph.node(1, 5, 0),
    )
    plan = globalplan.plan_routes(graph, [net])
    assert plan.converged and plan.via_count == 1
    assert all(x.use <= x.cap for x in plan.saturated)
