"""Flow-capacity tile graphs (slice 1 of
docs/backlog/pcb-global-plan-router.md): per-tile-boundary capacity as
vertex-disjoint paths across the two-tile strip on the fine grid's own
free cells — the boundary-run model's interior-choke blind spot closed.
Hand-built grids with known max-flow first, then the EWOD replay bracket
(slow)."""

from __future__ import annotations

import numpy as np
import pytest

from precis.pcb import globalplan
from precis.pcb.maze import GridSpec, OccupancyGrid

TRACK = 0.1
CLEARANCE = 0.1
VIA_DIA = 0.45
LANE = TRACK + CLEARANCE

# 3 mm x 2 mm board, 0.05 mm cells, two layers -> 6 x 4 tiles of 0.5 mm.
NX, NY = 60, 40


def _grid() -> OccupancyGrid:
    return OccupancyGrid(GridSpec(0.0, 0.0, 0.05, NX, NY, 2), clearance_mm=0.0)


def _column(grid: OccupancyGrid, ix: int, net: int, layers=(0, 1)) -> None:
    """A full-height wall of foreign copper on fine-grid column ``ix``."""
    for iy in range(NY):
        grid.stamp_disk(layers, ix * 0.05, iy * 0.05, 0.03, net)


def _graph(grid: OccupancyGrid, capacity: str, **kw) -> globalplan.TileGraph:
    return globalplan.build_tile_graph(
        grid,
        layers=(0, 1),
        track_mm=TRACK,
        clearance_mm=CLEARANCE,
        via_dia_mm=VIA_DIA,
        capacity=capacity,
        **kw,
    )


def test_open_board_cap_is_face_width_in_lanes():
    graph = _graph(_grid(), "flow")
    # Every boundary face is 0.5 mm = 10 free cells = 2.5 lanes -> 2.
    assert (graph.cap_x == 2).all()
    assert (graph.cap_y == 2).all()
    assert graph.capacity == "flow"


def test_an_interior_wall_subtracts_lanes_the_runs_model_cannot_see():
    grid = _grid()
    # Wall on column 34, inside tile 3 (cells 30-39), full height. Its
    # keep-out fills columns 32-36: crossing 2|3 funnels through the
    # two-cell corridor left of the wall (one lane), while the open
    # 3|4 face still takes both lanes — tracks sitting in the pocket
    # columns 37-39 cross straight over.
    _column(grid, 34, net=9)
    flow = _graph(grid, "flow")
    runs = _graph(grid, "runs")
    assert (runs.cap_x == 2).all()
    expected = np.full((4, 5), 2, dtype=np.int32)
    expected[:, 2] = 1  # tiles 2|3: a two-cell entry corridor
    assert (flow.cap_x[0] == expected).all()
    assert (flow.cap_x[1] == expected).all()


def test_a_full_wall_is_zero_not_partial():
    grid = _grid()
    # A keep-out that touches BOTH boundary faces (columns 29-40
    # blocked): nothing crosses either side of tile 3 at any row.
    for ix in range(31, 39):
        _column(grid, ix, net=9)
    flow = _graph(grid, "flow")
    assert (flow.cap_x[0][:, 2] == 0).all()
    assert (flow.cap_x[0][:, 3] == 0).all()
    # Untouched boundaries stay at the open figure.
    assert (flow.cap_x[0][:, 0] == 2).all() and (flow.cap_x[0][:, 4] == 2).all()


def test_a_one_cell_slit_is_one_lane_not_a_fraction():
    grid = _grid()
    # Keep-out fills columns 31-38 of tile 3: the only way across 2|3 is
    # the single free column 30 — one lane, even though it is 0.05 mm of
    # face where a lane wants 0.2. The runs model's floor(0.05/0.2) = 0
    # would say the board is cut; it is not.
    _column(grid, 33, net=9)
    _column(grid, 36, net=9)
    flow = _graph(grid, "flow")
    assert flow.cap_x[0][0, 2] == 1
    # 3|4 keeps its open face (the col-39 pocket is free copper).
    assert flow.cap_x[0][0, 3] == 2


def test_strict_exit_cap_remeasures_with_the_owner_transparent():
    grid = _grid()
    # Net 1 owns a plaza-via-sized disk in tile (2, 2): its own lanes are
    # blocked for everyone else, transparent for itself.
    grid.stamp_disk((0, 1), 1.25, 1.25, 0.3, 1)
    graph = _graph(grid, "flow", strict=True, participant_nets={1, 2})
    # Boundary tiles 2|3 on layer 0: net 1's copper straddles it.
    assert graph.cap_x[0][2, 2] == 0
    assert graph.exit_cap[("x", 0, 2, 2)] == {1: 2}


def test_capacity_must_be_a_known_model():
    with pytest.raises(ValueError, match="capacity"):
        _graph(_grid(), "banana")


# ── the EWOD replay bracket (slow) ──────────────────────────────────────


@pytest.mark.slow
def test_ewod_flow_bracket(store, monkeypatch):
    """The slice-1 bracket on the dogfood-6 replay: the strict flow
    capacity must sit INSIDE the open/strict bracket measured in slice
    0b — never above the open model's boundary-run cap elementwise, and
    the plan it admits must not exceed what the fine grid can hold
    (the open model's 55/55 over-read) while staying looser than the
    strict-run model's 17-20 walled-off nets. Corridor hand-off numbers
    are printed for the decisions log."""
    import collections
    import json
    import time
    from dataclasses import replace
    from pathlib import Path
    from typing import Any

    from precis.pcb import realize
    from precis.workers.job_types.pcb_route import (
        _apply_pin_swap_warm_start,
        _resolve_pin_swap_groups,
    )
    from tests.test_pcb_escape_replay import _hydrate
    from tests.test_pcb_globalplan_ewod import (
        ESCAPE_CLASS,
        PLAN_LAYERS,
        _capture_pass_args,
        _corridor_phase,
        _escape_reqs,
        _open_in2,
        _plan_nets,
        _static_grid,
    )

    t0 = time.monotonic()
    ir, graph, _features, footprints, fixed, config = _hydrate(store)
    _open_in2(ir, config)
    groups = _resolve_pin_swap_groups(ir, graph, [])
    sink_instance = groups[0].instance
    layer_idx = {str(l.get("name")): i for i, l in enumerate(ir.stackup)}
    plan_layers = tuple(layer_idx[n] for n in PLAN_LAYERS)

    report: dict[str, Any] = {"tile_mm": globalplan.DEFAULT_TILE_MM}
    # The escape class on the DISTANCE warm-start assignment — the 51/55
    # baseline's own pin map, so the comparison is not confounded.
    _apply_pin_swap_warm_start(ir, groups, footprints, fixed, "distance")
    captured = _capture_pass_args(monkeypatch, ir, config, footprints, fixed)
    reqs = _escape_reqs(ir, captured, config)
    static = _static_grid(ir, captured, config, fixed)
    rules = next(iter(reqs.values())).rules
    kw = dict(
        layers=plan_layers,
        track_mm=rules.track_width_mm,
        clearance_mm=float(
            config.class_rules[ESCAPE_CLASS].get("clearance_mm")
            or captured["clearance"]
        ),
        via_dia_mm=rules.via_dia_mm or 0.45,
        participant_nets={r.net_id for r in reqs.values()},
    )
    t_start = time.monotonic()
    open_graph = globalplan.build_tile_graph(static, **kw)
    strict_graph = globalplan.build_tile_graph(static, strict=True, **kw)
    flow_graph = globalplan.build_tile_graph(static, strict=True, capacity="flow", **kw)
    flowopen_graph = globalplan.build_tile_graph(
        static, strict=False, capacity="flow", **kw
    )
    report["graphs_s"] = round(time.monotonic() - t_start, 2)
    nets = _plan_nets(ir, captured, reqs, flow_graph, sink_instance, plan_layers)
    prefs = realize._layer_preferences(ir, captured["signal_layers"])
    plans: dict[str, globalplan.PlanResult] = {}
    for name, g in (
        ("open", open_graph),
        ("strict", strict_graph),
        ("flow", flow_graph),
        ("flowopen", flowopen_graph),
    ):
        t_start = time.monotonic()
        plan = globalplan.plan_routes(g, nets, layer_prefs=prefs)
        plans[name] = plan
        report[name] = {
            "converged": plan.converged,
            "iterations": plan.iterations,
            "routed_in_plan": len(plan.paths),
            "over_capacity": sum(1 for s in plan.saturated if s.use > s.cap),
            "saturated_rows": sorted(
                collections.Counter(
                    (s.kind, s.layer, s.ty) for s in plan.saturated if s.use > s.cap
                ).items(),
                key=lambda kv: (-kv[1], kv[0]),
            )[:8],
            "plan_s": round(time.monotonic() - t_start, 2),
        }
    # The bracket itself: flow <= open elementwise (connectivity can only
    # subtract from a face-width measure), flow >= strict runs where the
    # strict mask is identical (the flow strip reads THROUGH, not just
    # across, so it can also be tighter OR looser — report, don't assert).
    report["flow_le_open"] = bool(
        (flow_graph.cap_x <= open_graph.cap_x).all()
        and (flow_graph.cap_y <= open_graph.cap_y).all()
    )
    report["flow_vs_strict_hist"] = {
        "tighter": int((flow_graph.cap_x < strict_graph.cap_x).sum())
        + int((flow_graph.cap_y < strict_graph.cap_y).sum()),
        "equal": int((flow_graph.cap_x == strict_graph.cap_x).sum())
        + int((flow_graph.cap_y == strict_graph.cap_y).sum()),
        "looser": int((flow_graph.cap_x > strict_graph.cap_x).sum())
        + int((flow_graph.cap_y > strict_graph.cap_y).sum()),
    }
    # Corridor hand-off on the flow plans, soft k=5 at dilate 1 — the
    # variant the opt-in route path runs.
    for name, g in (("flow", flow_graph), ("flowopen", flowopen_graph)):
        t_start = time.monotonic()
        corr_count, exhausted, unrouted_c, final_unrouted, _paths = _corridor_phase(
            ir, captured, config, fixed, g, plans[name], reqs, 1, 5.0
        )
        report[f"corridor_{name}_soft_1_k5"] = {
            "corridor_routed": corr_count,
            "exhausted": len(exhausted),
            "final_routed": 55 - len(final_unrouted),
            "final_unrouted": sorted(
                str(ir.net_name[int(ir.seg_net[s])]) for s in final_unrouted
            ),
            "corridor_s": round(time.monotonic() - t_start, 2),
        }
    # The opt-in route path itself (slice 1c): corridor_guided passes
    # then the plain fallback — the number the route job would report
    # with ``corridor=5`` set. Asserted against the plain 51/55 floor:
    # the opt-in may cost time, never nets.
    t_start = time.monotonic()
    rres = realize.realize(
        ir,
        config=replace(config, corridor_penalty=5.0),
        footprints=footprints,
        fixed_copper=fixed,
    )
    report["realize_corridor5"] = {
        "routed": 55 - len(rres.unrouted),
        "vias": len(rres.vias),
        "seconds": round(time.monotonic() - t_start, 1),
        "unrouted": sorted(str(ir.net_name[int(ir.seg_net[s])]) for s in rres.unrouted),
    }
    report["elapsed_s"] = round(time.monotonic() - t0, 1)
    out = Path(".scratch/flow-bracket-report.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print("FLOWPLAN_REPORT " + json.dumps(report, indent=1, default=str))

    assert report["flow_le_open"], "flow capacity may never exceed open"
    # The bracket, as measured (recorded in the decisions log):
    # participant-transparent flow reads >= what the fine grid holds
    # (55 planned vs the router's 51), strict flow reads the same 35 as
    # strict runs — both masks bracket the fine grid; the stall at 35 is
    # the tile abstraction's, not the measure's: flow is strictly-tighter
    # than strict-runs on only the 24 boundaries whose interiors choke.
    assert len(plans["flowopen"].paths) >= 51
    assert len(plans["flow"].paths) <= len(plans["flowopen"].paths)
    assert len(plans["flow"].paths) >= len(plans["strict"].paths)
    assert report["realize_corridor5"]["routed"] >= 51, (
        "corridor_penalty>0 lost ground the plain router holds (51/55) — "
        "the plain fallback in _realize_maze must always run after the "
        "guided passes"
    )
