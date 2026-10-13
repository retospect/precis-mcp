"""Global tile-capacity plan probe on the EWOD dogfood-6 replay.

Experiment (docs/backlog/pcb-global-plan-probe.md): does a
capacity-feasible 55/55 tile-level routing plan exist for the escape
class (In2.Cu + B.Cu), and does handing that plan to the maze router as
corridors beat the 51/55 of
:func:`~tests.test_pcb_escape_replay.test_connected_via_channel_assignment_replay`'s
``("distance", True)`` arm? Three plan variants: the stored pin
assignment, the ``distance`` warm-start assignment, and the assignment
folded into the negotiation itself (any free HVOUT pad, Hungarian-matched
each iteration). Numbers are printed for the results note; the asserts
pin what was measured.
"""

from __future__ import annotations

import collections
import copy
import inspect
import json
import time
from typing import Any

import numpy as np
import pytest

from precis.pcb import globalplan, maze, pinswap, realize
from precis.pcb.ir import pin_point
from precis.workers.job_types.pcb_route import (
    _apply_pin_swap_warm_start,
    _resolve_pin_swap_groups,
)
from tests.test_pcb_escape_replay import _hydrate

ESCAPE_CLASS = "ewod_ARR1_escape"
PLAN_LAYERS = ("In2.Cu", "B.Cu")

# Taken once, unpatched: the capture helper patches ``_route_pass`` per
# variant, so reading the signature late would bind the stub's own
# ``(*args, **kwargs)``.
_ROUTE_PASS_SIG = inspect.signature(realize._route_pass)


def _open_in2(ir, config) -> None:
    """Same re-authoring the reference test applies: In2.Cu signal, the
    escape class locked to [In2.Cu, B.Cu]."""
    inner = next(i for i, layer in enumerate(ir.stackup) if layer["name"] == "In2.Cu")
    ir.stackup[inner] = {"name": "In2.Cu", "role": "signal", "routable": True}
    config.class_rules[ESCAPE_CLASS]["layers"] = list(PLAN_LAYERS)


def _capture_pass_args(monkeypatch, ir, config, footprints, fixed):
    """Intercept ``realize._route_pass``'s arguments — the exact bundle a
    real routing attempt is built from (grid spec, pads, clearance, per-net
    rules, island terminals, net layer locks), before any segment is
    searched. Same probe technique as
    ``test_stored_no_path_labels_have_full_width_actual_terminal_paths``."""
    captured: dict[str, Any] = {}
    signature = _ROUTE_PASS_SIG

    class Captured(Exception):
        pass

    def capture(*args, **kwargs):
        captured.update(signature.bind(*args, **kwargs).arguments)
        raise Captured

    with monkeypatch.context() as patch:
        patch.setattr(realize, "_route_pass", capture)
        with pytest.raises(Captured):
            realize.realize(
                ir, config=config, footprints=footprints, fixed_copper=fixed
            )
    return captured


def _static_grid(ir, captured, config, fixed) -> maze.OccupancyGrid:
    """A fresh grid with every static claim, exactly what a route pass
    starts from."""
    spec = captured["spec"]
    grid = maze.OccupancyGrid(spec, clearance_mm=captured["clearance"])
    grid.set_body_mask(captured["grid"]._body_mask)
    realize._claim_static(
        ir,
        captured["plane_ids"],
        grid,
        config,
        captured["rules_by_net"],
        captured["pads"],
        captured["pad_geoms"],
        captured["ink_field"],
        fixed,
    )
    return grid


def _escape_reqs(ir, captured, config):
    """``{seg_id: _SegRequest}`` for the escape class, resolved exactly as
    a route pass resolves them."""
    reqs: dict[int, Any] = {}
    for seg in captured["order"]:
        net = int(ir.seg_net[seg])
        if str(ir.net_class[net]) != ESCAPE_CLASS:
            continue
        req = realize._seg_request(
            ir,
            seg,
            config,
            captured["rules_by_net"],
            captured["clearance"],
            captured["signal_layers"],
            captured["island_terminals"],
            captured["net_layers"],
        )
        if req is not None and not req.failed:
            reqs[seg] = req
    return reqs


def _plan_nets(ir, captured, reqs, tgraph, sink_instance, layers, sinks=None):
    """PlanNet per escape segment: sources = the tile(s) of the pin's
    fixed through-via on every planned layer; sink = the pad's tile node
    on its goal layer (or the admissible pad-node set for the free-sink
    variant)."""
    nets: list[globalplan.PlanNet] = []
    layer_li = {layer: i for i, layer in enumerate(layers)}
    for seg in captured["order"]:
        req = reqs.get(seg)
        if req is None:
            continue
        net = int(ir.seg_net[seg])
        a, b = int(ir.seg_pin_a[seg]), int(ir.seg_pin_b[seg])
        sink_pin = a if int(ir.pin_instance[a]) == sink_instance else b
        far_pin = b if sink_pin == a else a
        term_a, term_b = captured["island_terminals"].get(seg, ((), ()))
        terms_far = term_a if far_pin == a else term_b
        via_xy = sorted({t.point for t, _l in terms_far if t.kind == "via"})
        assert len(via_xy) == 1, f"{ir.net_name[net]}: {len(via_xy)} via terminals"
        sources: list[int] = []
        for vx, vy in via_xy:
            tx, ty = tgraph.tile_of(vx, vy)
            for layer in layers:
                sources.append(tgraph.node(layer_li[layer], tx, ty))
        pad = pin_point(ir, sink_pin)
        assert pad is not None
        ptx, pty = tgraph.tile_of(*pad)
        sink = tgraph.node(layer_li[req.goal_layer], ptx, pty)
        nets.append(
            globalplan.PlanNet(
                seg_id=seg,
                net_id=net,
                name=str(ir.net_name[net]),
                sources=tuple(sorted(set(sources))),
                sink=None if sinks is not None else sink,
                sinks=tuple(sinks) if sinks is not None else (),
            )
        )
    return nets


def _apply_assignment(ir, groups, seg_to_pin: dict[int, int]) -> None:
    """Apply a seg->pin assignment to ``ir`` as pin swaps: each segment's
    net moves to its assigned pad's pin. Bijective over the group."""
    group = groups[0]
    pins = list(group.pins)
    pin_index = {p: i for i, p in enumerate(pins)}
    current: dict[int, int] = {}
    for seg_id in seg_to_pin:
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        pin = a if a in pin_index else b
        assert pin in pin_index
        current[seg_id] = pin
    targets = {current[s]: seg_to_pin[s] for s in seg_to_pin}
    perm = [pin_index[targets[p]] for p in pins]
    for cyc in pinswap._cycles(perm):
        if len(cyc) < 2:
            continue
        pivot = pins[cyc[0]]
        for i in cyc[1:]:
            ir.swap_pins(pivot, pins[i])


def _corridor_phase(
    ir, captured, config, fixed, tgraph, plan, reqs, dilate, soft=None, budget=None
):
    """Route each planned segment inside its corridor on the real
    occupancy grid (most-constrained first), then give leftovers the
    ordinary failures-first re-ordering with the corridor results held as
    verbatim proposals. Returns ``(corridor_routed, exhausted, unrouted_c,
    final_unrouted, corridor_paths)``."""
    spec = captured["spec"]
    corridors: dict[int, np.ndarray] = {}
    clayers: dict[int, list[int]] = {}
    for seg_id, path in plan.paths.items():
        req = reqs[seg_id]
        tiles = globalplan.corridor_tiles(tgraph, path, dilate=dilate)
        corridors[seg_id] = globalplan.corridor_mask(
            tgraph,
            tiles,
            extra=(
                (req.start_layer, req.eff_start[0], req.eff_start[1]),
                (req.goal_layer, req.eff_goal[0], req.eff_goal[1]),
            ),
        )
        clayers[seg_id] = sorted(
            {tgraph.layers[tgraph.node_parts(n)[0]] for n in path.nodes}
        )
    order = globalplan.plan_order(
        tgraph, plan, [s for s in captured["order"] if s in plan.paths]
    )
    grid = _static_grid(ir, captured, config, fixed)
    _t, _v, unrouted_c, paths, exhausted = globalplan.corridor_route(
        grid,
        ir,
        reqs,
        order,
        corridors,
        clayers,
        config,
        spec,
        ink_field=captured["ink_field"],
        soft_penalty=soft,
        max_expansions=budget,
    )
    corridor_count = len(paths)
    leftover = list(unrouted_c) + list(plan.unrouted)
    # Leftover passes: fresh grid each pass, corridor commitments replayed
    # verbatim through the `preferred` seam (re-checked for legality),
    # then the previous pass's leftover failures searched first.
    best_unrouted: list[int] | None = None
    for _attempt in range(max(1, config.route_passes)):
        if not leftover:
            break
        g2 = maze.OccupancyGrid(spec, clearance_mm=captured["clearance"])
        g2.set_body_mask(captured["grid"]._body_mask)
        preferred = {s: realize._Proposal(p, p) for s, p in paths.items()}
        leftover_set = set(leftover)
        seq = [s for s in order if s in paths] + [
            s for s in captured["order"] if s in leftover_set
        ]
        _t2, _v2, unrouted2, _notes, _pf = realize._route_pass(
            ir,
            seq,
            captured["plane_ids"],
            g2,
            config,
            captured["rules_by_net"],
            captured["pads"],
            captured["clearance"],
            captured["signal_layers"],
            spec,
            captured["pad_geoms"],
            ink_field=captured["ink_field"],
            fixed_copper=fixed,
            island_terminals=captured["island_terminals"],
            net_layers=captured["net_layers"],
            preferred=preferred,
        )
        unrouted2 = [s for s in unrouted2 if s in leftover_set]
        if best_unrouted is None or len(unrouted2) < len(best_unrouted):
            best_unrouted = list(unrouted2)
        if not unrouted2:
            break
        failed = [s for s in seq if s in set(unrouted2)]
        leftover = failed + [s for s in leftover if s not in set(failed)]
    final_unrouted = best_unrouted if best_unrouted is not None else list(leftover)
    return corridor_count, exhausted, unrouted_c, final_unrouted, paths


def _pin_node(graph: globalplan.TileGraph, layer_index: int, ir, pin: int) -> int:
    """The tile-graph node under ``pin`` on ``layer_index``."""
    pt = pin_point(ir, pin)
    assert pt is not None
    return graph.node(layer_index, *graph.tile_of(pt[0], pt[1]))


#: Search budget for the priced-corridor runs: four times the router's own
#: (a priced search keeps the unpriced distance heuristic, so it opens more
#: cells per routed net).
BIG_BUDGET = 4 * maze.MAX_EXPANSIONS


@pytest.mark.slow
def test_ewod_global_tile_plan_probe(store, monkeypatch):
    t0 = time.monotonic()
    ir, graph, _features, footprints, fixed, config = _hydrate(store)
    _open_in2(ir, config)
    groups = _resolve_pin_swap_groups(ir, graph, [])
    assert len(groups) == 1
    sink_instance = groups[0].instance
    layer_idx = {str(layer.get("name")): i for i, layer in enumerate(ir.stackup)}
    plan_layers = tuple(layer_idx[n] for n in PLAN_LAYERS)

    report: dict[str, Any] = {"tile_mm": globalplan.DEFAULT_TILE_MM}
    variants: dict[str, Any] = {}
    plans: dict[str, globalplan.PlanResult] = {}
    bundles: dict[str, tuple] = {}
    tile_graph = None
    for variant in ("stored", "distance", "negotiated"):
        ir_v = copy.deepcopy(ir)
        if variant == "distance":
            _apply_pin_swap_warm_start(ir_v, groups, footprints, fixed, "distance")
        captured = _capture_pass_args(monkeypatch, ir_v, config, footprints, fixed)
        reqs = _escape_reqs(ir_v, captured, config)
        if tile_graph is None:
            static = _static_grid(ir_v, captured, config, fixed)
            t_start = time.monotonic()
            tile_graph = globalplan.build_tile_graph(
                static,
                layers=plan_layers,
                track_mm=next(iter(reqs.values())).rules.track_width_mm,
                clearance_mm=float(
                    config.class_rules[ESCAPE_CLASS].get("clearance_mm")
                    or captured["clearance"]
                ),
                via_dia_mm=next(iter(reqs.values())).rules.via_dia_mm or 0.45,
                participant_nets={req.net_id for req in reqs.values()},
            )
            report["tile_graph_s"] = round(time.monotonic() - t_start, 2)
            report["capacity_map"] = list(tile_graph.row_report)
            report["edge_cap_hist"] = {
                "x": np.bincount(tile_graph.cap_x.reshape(-1)).tolist(),
                "y": np.bincount(tile_graph.cap_y.reshape(-1)).tolist(),
                "via": np.bincount(tile_graph.via_cap.reshape(-1)).tolist(),
            }
        sinks = None
        assert tile_graph is not None
        if variant == "negotiated":
            goal_li = tile_graph.layers.index(next(iter(reqs.values())).goal_layer)
            sinks = [
                _pin_node(tile_graph, goal_li, ir_v, pin) for pin in groups[0].pins
            ]
        nets = _plan_nets(
            ir_v,
            captured,
            reqs,
            tile_graph,
            sink_instance,
            plan_layers,
            sinks=sinks,
        )
        assert len(nets) == 55
        prefs = realize._layer_preferences(ir_v, captured["signal_layers"])
        t_start = time.monotonic()
        plan = globalplan.plan_routes(
            tile_graph,
            nets,
            layer_prefs=prefs,
            free_sinks=variant == "negotiated",
        )
        plan_s = time.monotonic() - t_start
        det = globalplan.plan_routes(
            tile_graph,
            nets,
            layer_prefs=prefs,
            free_sinks=variant == "negotiated",
        )
        assert {s: p.nodes for s, p in det.paths.items()} == {
            s: p.nodes for s, p in plan.paths.items()
        }, f"{variant}: plan is not deterministic"
        assert det.assignment == plan.assignment
        variants[variant] = {
            "converged": plan.converged,
            "iterations": plan.iterations,
            "conflicted_trail": list(plan.conflicted_per_iter),
            "routed_in_plan": len(plan.paths),
            "total_tile_len_mm": round(plan.total_length_mm, 1),
            "plan_vias": plan.via_count,
            "plan_s": round(plan_s, 2),
            "over_capacity": [
                (s.kind, s.layer, s.tx, s.ty, s.use, s.cap)
                for s in plan.saturated
                if s.use > s.cap
            ],
            # Used-to-the-limit resources only (cap > 0, see
            # globalplan.plan_routes); grouped by tile row so the note can
            # name WHICH cut saturates.
            "at_capacity": sum(1 for s in plan.saturated if s.use == s.cap),
            "saturated_rows": sorted(
                collections.Counter(
                    (s.kind, s.layer, s.ty) for s in plan.saturated
                ).items(),
                key=lambda kv: (-kv[1], kv[0]),
            )[:12],
        }
        plans[variant] = plan
        bundles[variant] = (ir_v, captured, reqs, nets, prefs)

    # Corridor phase on the best converged plan (fewest vias, then shortest
    # total tile length). None converged is itself the negative result.
    converged = [v for v in variants if variants[v]["converged"]]
    best = None
    if converged:
        best = min(
            converged,
            key=lambda v: (plans[v].via_count, plans[v].total_length_mm),
        )
        plan = plans[best]
        ir_v, captured, reqs, _nets, _prefs = bundles[best]
        if best == "negotiated" and plan.assignment:
            assert tile_graph is not None
            goal_li = tile_graph.layers.index(next(iter(reqs.values())).goal_layer)
            node_to_pin = {
                _pin_node(tile_graph, goal_li, ir, pin): pin for pin in groups[0].pins
            }
            ir_v = copy.deepcopy(ir)
            _apply_assignment(
                ir_v,
                groups,
                {s: node_to_pin[n] for s, n in plan.assignment.items()},
            )
            captured = _capture_pass_args(monkeypatch, ir_v, config, footprints, fixed)
            reqs = _escape_reqs(ir_v, captured, config)
        report["corridor"] = {"variant": best}
        # Hard corridors (mask=) at d=1/2, then slice 0b: the corridor as
        # a cost (off-corridor step x (1 + k)) at d=1 for two strengths.
        for dilate, soft in ((1, None), (2, None), (1, 2.0), (1, 5.0), (1, 10.0)):
            t_start = time.monotonic()
            corr_count, exhausted, unrouted_c, final_unrouted, _paths = _corridor_phase(
                ir_v, captured, config, fixed, tile_graph, plan, reqs, dilate, soft
            )
            key = f"dilate_{dilate}" if soft is None else f"soft_{dilate}_k{soft:g}"
            report["corridor"][key] = {
                "corridor_routed": corr_count,
                "corridor_failed": sorted(
                    str(ir_v.net_name[int(ir_v.seg_net[s])]) for s in unrouted_c
                ),
                "exhausted": len(exhausted),
                "final_routed": 55 - len(final_unrouted),
                "final_unrouted": sorted(
                    str(ir_v.net_name[int(ir_v.seg_net[s])]) for s in final_unrouted
                ),
                "corridor_s": round(time.monotonic() - t_start, 2),
            }
        # The 51/55 baseline uses the DISTANCE assignment; the corridor phase
        # above used the fewest-via plan. Same mechanism on the distance plan
        # so the comparison is not confounded by the pin assignment.
        if "distance" in converged and best != "distance":
            d_plan = plans["distance"]
            d_ir, d_captured, d_reqs, _nets, _prefs = bundles["distance"]
            t_start = time.monotonic()
            corr_count, exhausted, unrouted_c, final_unrouted, _paths = _corridor_phase(
                d_ir, d_captured, config, fixed, tile_graph, d_plan, d_reqs, 1, 5.0
            )
            report["corridor"]["distance_soft_1_k5"] = {
                "corridor_routed": corr_count,
                "exhausted": len(exhausted),
                "final_routed": 55 - len(final_unrouted),
                "final_unrouted": sorted(
                    str(d_ir.net_name[int(d_ir.seg_net[s])]) for s in final_unrouted
                ),
                "corridor_s": round(time.monotonic() - t_start, 2),
            }
        # Slice 0b(b) follow-up: the same soft corridor with a 4x search
        # budget, on the open-model plan — separates "budget-bound" from
        # "corridor wrong" before the strict model changes the corridors.
        t_start = time.monotonic()
        corr_count, exhausted, unrouted_c, final_unrouted, _paths = _corridor_phase(
            ir_v, captured, config, fixed, tile_graph, plan, reqs, 1, 5.0, BIG_BUDGET
        )
        report["corridor"]["soft_1_k5_budget4x"] = {
            "corridor_routed": corr_count,
            "exhausted": len(exhausted),
            "final_routed": 55 - len(final_unrouted),
            "final_unrouted": sorted(
                str(ir_v.net_name[int(ir_v.seg_net[s])]) for s in final_unrouted
            ),
            "corridor_s": round(time.monotonic() - t_start, 2),
        }

    # Slice 0b(a): strict capacities (participant copper blocks lanes, a
    # net's own copper transparent only where it narrows a boundary;
    # planned through-vias charged on their tile's lanes), same three
    # assignments, then the soft corridor on the fixed-sink strict plans.
    # Each fixed-sink assignment gets its own strict graph: pad ownership
    # (who may pass a pad's copper) follows the assignment. The negotiated
    # (free-sink) plan reuses the stored graph — its candidate pads are
    # owned by the stored nets, which biases it against swaps.
    def strict_graph_for(variant: str) -> globalplan.TileGraph:
        v_ir, v_captured, v_reqs, _n, _p = bundles[variant]
        rules = next(iter(v_reqs.values())).rules
        return globalplan.build_tile_graph(
            _static_grid(v_ir, v_captured, config, fixed),
            layers=plan_layers,
            track_mm=rules.track_width_mm,
            clearance_mm=float(
                config.class_rules[ESCAPE_CLASS].get("clearance_mm")
                or v_captured["clearance"]
            ),
            via_dia_mm=rules.via_dia_mm or 0.45,
            participant_nets={req.net_id for req in v_reqs.values()},
            strict=True,
        )

    t_start = time.monotonic()
    strict_graphs = {"stored": strict_graph_for("stored")}
    strict_graphs["distance"] = strict_graph_for("distance")
    strict_graphs["negotiated"] = strict_graphs["stored"]
    s_graph = strict_graphs["stored"]
    strict_report: dict[str, Any] = {
        "tile_graph_s": round((time.monotonic() - t_start) / 2, 2),
        "via_lanes": s_graph.via_lanes,
        "exit_edges": len(s_graph.exit_cap),
        "edge_cap_hist": {
            "x": np.bincount(s_graph.cap_x.reshape(-1)).tolist(),
            "y": np.bincount(s_graph.cap_y.reshape(-1)).tolist(),
        },
        "variants": {},
    }
    strict_plans: dict[str, globalplan.PlanResult] = {}
    for variant in ("stored", "distance", "negotiated"):
        _ir_s, _cap_s, _reqs_s, s_nets, s_prefs = bundles[variant]
        t_start = time.monotonic()
        s_plan = globalplan.plan_routes(
            strict_graphs[variant],
            s_nets,
            layer_prefs=s_prefs,
            free_sinks=variant == "negotiated",
        )
        strict_plans[variant] = s_plan
        strict_report["variants"][variant] = {
            "converged": s_plan.converged,
            "iterations": s_plan.iterations,
            "conflicted_tail": list(s_plan.conflicted_per_iter[-5:]),
            "routed_in_plan": len(s_plan.paths),
            "plan_vias": s_plan.via_count,
            "total_tile_len_mm": round(s_plan.total_length_mm, 1),
            "over_capacity": sum(1 for x in s_plan.saturated if x.use > x.cap),
            "saturated_rows": sorted(
                collections.Counter(
                    (x.kind, x.layer, x.ty) for x in s_plan.saturated if x.use > x.cap
                ).items(),
                key=lambda kv: (-kv[1], kv[0]),
            )[:8],
            "plan_s": round(time.monotonic() - t_start, 2),
        }
    # Fixed-sink variants only: their bundles already carry the pin
    # assignment the plan was made for. Default budget (4x was refuted on
    # the open model above).
    for variant in ("stored", "distance"):
        s_plan = strict_plans[variant]
        if not s_plan.paths:
            continue
        s_ir, s_captured, s_reqs, _n, _p = bundles[variant]
        t_start = time.monotonic()
        corr_count, exhausted, unrouted_c, final_unrouted, _paths = _corridor_phase(
            s_ir,
            s_captured,
            config,
            fixed,
            strict_graphs[variant],
            s_plan,
            s_reqs,
            1,
            5.0,
        )
        key = f"{variant}_soft_1_k5"
        strict_report[key] = {
            "corridor_routed": corr_count,
            "exhausted": len(exhausted),
            "final_routed": 55 - len(final_unrouted),
            "final_unrouted": sorted(
                str(s_ir.net_name[int(s_ir.seg_net[s])]) for s in final_unrouted
            ),
            "corridor_s": round(time.monotonic() - t_start, 2),
        }
    report["strict"] = strict_report
    report["elapsed_s"] = round(time.monotonic() - t0, 1)
    report["variants"] = variants
    print("GLOBALPLAN_REPORT " + json.dumps(report, indent=1, default=str))

    # Invariants, not pinned measurements: the measured numbers live in
    # docs/backlog/pcb-global-plan-probe.md and are printed above. The last
    # assertion fires when the hand-off finally beats the 51/55 replay
    # baseline — that is the signal to rewrite the note, not a regression.
    assert [variants[v]["converged"] for v in ("stored", "distance", "negotiated")] == [
        True,
        True,
        True,
    ]
    assert all(v["over_capacity"] == [] for v in variants.values())
    for d in ("dilate_1", "dilate_2"):
        c = report["corridor"][d]
        assert 0 <= c["corridor_routed"] <= c["final_routed"] <= 55
    assert report["corridor"]["dilate_2"]["final_routed"] <= 51, (
        "corridor-guided routing now beats the 51/55 baseline: update "
        "docs/backlog/pcb-global-plan-probe.md"
    )
