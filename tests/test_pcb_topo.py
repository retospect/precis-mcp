"""CDT region graph and physical portal capacities — the slice-5 probe of
docs/backlog/pcb-vector-router.md. Hand-built layouts with known gaps
first, then the comparison against the flow tile model on the QFP plaza
member and the EWOD dogfood-6 replay (slow): for each connection, does a
portal sequence exist, and where the tile model reads 0 lanes at a
terminal, does the CDT still find one?"""

from __future__ import annotations

import collections
import time
from collections import deque
from typing import Any

import pytest
from shapely.geometry import Point, box

from precis.pcb import globalplan, topo
from precis.pcb.ir import pin_point

W = 0.1  # track
S = 0.1  # clearance
OUTLINE = [[0.0, 0.0], [10.0, 0.0], [10.0, 6.0], [0.0, 6.0]]


def _ob(net: str, geom) -> topo.Obstacle:
    return topo.Obstacle(net, "pad", topo.outward(geom))


def _graph(obstacles) -> topo.RegionGraph:
    return topo.build_region_graph(
        obstacles, OUTLINE, "F.Cu", track_mm=W, clearance_mm=S
    )


def test_portal_capacity_counts_tracks_with_clearance_both_sides():
    assert topo.portal_capacity(S + 3 * (W + S), W, S) == 3  # exact fit
    assert topo.portal_capacity(S + 3 * (W + S) - 1e-3, W, S) == 2
    assert topo.portal_capacity(0.05, W, S) == 0
    with pytest.raises(ValueError, match="positive"):
        topo.portal_capacity(1.0, 0.0, 0.0)


def test_outward_contains_the_exact_shape_within_delta():
    exact = Point(0.0, 0.0).buffer(0.3, quad_segs=256)
    out = topo.outward(exact)
    assert out.buffer(1e-9).contains(exact)
    assert out.hausdorff_distance(exact) <= topo.DELTA_MM + 1e-9
    # Far fewer vertices than the 1024-gon it came from.
    assert len(out.exterior.coords) < 200


def test_gap_between_two_pads_is_a_portal_of_that_length():
    # S + 3 lanes: exactly 3 tracks on the exact shapes; the outward
    # drawing takes up to delta off the gap, so the probe reads 2.
    gap = S + 3 * (W + S)
    a = _ob("A", box(2.0, 2.0, 3.0, 3.0))
    b = _ob("B", box(3.0 + gap, 2.0, 4.0 + gap, 3.0))
    g = _graph([a, b])
    between = [p for p in g.portals if set(p.ends) == {0, 1}]
    assert between
    shortest = min(between, key=lambda p: p.length_mm)
    # The outward drawing narrows the gap by at most delta.
    assert gap - topo.DELTA_MM - 1e-9 <= shortest.length_mm <= gap
    assert shortest.capacity == topo.portal_capacity(shortest.length_mm, W, S) == 2
    assert all(p.capacity >= 0 for p in g.portals)
    assert sum(g.capacity_histogram().values()) == len(g.portals)


def _walled(slit_mm: float | None) -> tuple[topo.RegionGraph, int, int]:
    """Pad A left, pad B right, a full-height wall between them with an
    optional slit of ``slit_mm`` centred at y = 3."""
    obs = [_ob("A", box(1.0, 2.5, 2.0, 3.5)), _ob("B", box(8.0, 2.5, 9.0, 3.5))]
    if slit_mm is None:
        obs.append(_ob("W", box(4.9, -1.0, 5.1, 7.0)))
    else:
        obs.append(_ob("W", box(4.9, -1.0, 5.1, 3.0 - slit_mm / 2)))
        obs.append(_ob("W", box(4.9, 3.0 + slit_mm / 2, 5.1, 7.0)))
    return _graph(obs), 0, 1


def test_a_full_wall_has_no_portal_sequence():
    g, a, b = _walled(None)
    assert g.portal_path(a, b) is None


def test_a_slit_narrower_than_one_lane_blocks_and_a_wider_one_passes():
    narrow, a, b = _walled(0.25)  # < S + (W + S) = 0.3
    assert narrow.portal_path(a, b) is None
    wide, a, b = _walled(0.5)
    path = wide.portal_path(a, b)
    assert path is not None and path
    assert all(wide.portals[i].capacity >= 1 for i in path)
    # Asking for more lanes than the slit holds closes it again.
    assert wide.portal_path(a, b, min_capacity=2) is None


def test_board_obstacles_follow_layers_drills_and_via_spans():
    model: dict[str, Any] = {
        "layers": ["F.Cu", "In1.Cu", "B.Cu"],
        "pads": [
            {
                "layer": "F.Cu",
                "net": "A",
                "shape": "rect",
                "x": 1.0,
                "y": 1.0,
                "w": 0.5,
                "h": 1.0,
            },
            {
                "layer": "F.Cu",
                "net": "B",
                "shape": "circle",
                "x": 3.0,
                "y": 1.0,
                "w": 1.0,
                "drill": 0.5,
            },
        ],
        "copper": [
            {
                "ctype": "via",
                "net": "C",
                "x": 5.0,
                "y": 1.0,
                "dia_mm": 0.45,
                "span": ["F.Cu", "In1.Cu"],
            },
            {
                "ctype": "track",
                "net": "D",
                "layer": "B.Cu",
                "width_mm": 0.2,
                "segments": [{"shape": "line", "start": [0.0, 3.0], "end": [4.0, 3.0]}],
            },
        ],
    }
    nets = {
        lay: sorted(o.net for o in topo.board_obstacles(model, lay))
        for lay in model["layers"]
    }
    assert nets == {"F.Cu": ["A", "B", "C"], "In1.Cu": ["B", "C"], "B.Cu": ["B", "D"]}


def test_summarize_counts_reachable_connections():
    g, _a, _b = _walled(0.5)
    blocked, _, _ = _walled(None)
    conn = topo.Connection(
        "A", topo.Terminal(1.5, 3.0, ("F.Cu",)), topo.Terminal(1.5, 3.0, ("F.Cu",))
    )
    across = topo.Connection(
        "A", topo.Terminal(1.5, 3.0, ("F.Cu",)), topo.Terminal(8.5, 3.0, ("F.Cu",))
    )
    # Terminals resolve by net: B's pad is not net A, so `across` has no
    # destination obstacle and is unreachable on any graph.
    out = topo.summarize({"F.Cu": g}, [conn, across])
    assert out["connections"] == 2
    assert out["reachable"] == 1
    assert out["unreachable"] == ["A"]
    assert out["layers"]["F.Cu"]["portals"] == len(g.portals)
    assert topo.summarize({"F.Cu": blocked}, [conn])["reachable"] == 1


# ── the comparison against the flow tile model (slow) ──────────────────


def _cap(
    tg: globalplan.TileGraph, kind: str, li: int, ty: int, tx: int, net: int
) -> int:
    """A boundary's lanes as ``net`` sees them: the strict graph's
    ``exit_cap`` where its own copper narrows the boundary."""
    base = (tg.cap_x if kind == "x" else tg.cap_y)[li, ty, tx]
    return int(tg.exit_cap.get((kind, li, ty, tx), {}).get(net, base))


def _tile_steps(tg: globalplan.TileGraph, li: int, tx: int, ty: int, net: int):
    """``(neighbour tile, lanes)`` for each boundary of tile ``(tx, ty)``."""
    if tx + 1 < tg.ntx:
        yield (tx + 1, ty), _cap(tg, "x", li, ty, tx, net)
    if tx > 0:
        yield (tx - 1, ty), _cap(tg, "x", li, ty, tx - 1, net)
    if ty + 1 < tg.nty:
        yield (tx, ty + 1), _cap(tg, "y", li, ty, tx, net)
    if ty > 0:
        yield (tx, ty - 1), _cap(tg, "y", li, ty - 1, tx, net)


def _tile_reach(
    tg: globalplan.TileGraph,
    li: int,
    net: int,
    a: tuple[float, float],
    b: tuple[float, float],
) -> bool:
    """A tile path on layer ``li`` from ``a``'s tile to ``b``'s across
    boundaries ``net`` sees ≥ 1 lane on (single layer, as the CDT side)."""
    start, goal = tg.tile_of(*a), tg.tile_of(*b)
    seen = {start}
    queue = deque([start])
    while queue:
        tx, ty = queue.popleft()
        if (tx, ty) == goal:
            return True
        for nxt, lanes in _tile_steps(tg, li, tx, ty, net):
            if lanes > 0 and nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def _tile_walled(
    tg: globalplan.TileGraph, li: int, net: int, pt: tuple[float, float]
) -> bool:
    """Every boundary of ``pt``'s tile reads 0 lanes for ``net`` on ``li``."""
    return all(lanes == 0 for _t, lanes in _tile_steps(tg, li, *tg.tile_of(*pt), net))


def _compare(
    model: dict[str, Any],
    outline: list[list[float]],
    tg: globalplan.TileGraph,
    layer_names: dict[int, str],
    pairs: list[tuple[str, int, tuple[float, float], tuple[float, float], int]],
    *,
    track_mm: float,
    clearance_mm: float,
) -> dict[str, Any]:
    """Both models on the same connections, each on its goal layer only.
    ``pairs`` are ``(net name, net id, far end, near end, stackup layer
    index)``."""
    graphs: dict[str, topo.RegionGraph] = {}
    t0 = time.monotonic()
    for li in tg.layers:
        name = layer_names[li]
        graphs[name] = topo.build_region_graph(
            topo.board_obstacles(model, name),
            outline,
            name,
            track_mm=track_mm,
            clearance_mm=clearance_mm,
        )
    cdt_s = time.monotonic() - t0
    rows: collections.Counter[str] = collections.Counter()
    misses: list[str] = []
    for net, net_id, far, near, layer in pairs:
        name = layer_names[layer]
        conn = topo.Connection(
            net, topo.Terminal(*far, (name,)), topo.Terminal(*near, (name,))
        )
        cdt = topo.connection_reach(conn, {name: graphs[name]})[name] is not None
        li = tg.layers.index(layer)
        tile = _tile_reach(tg, li, net_id, far, near)
        walled = _tile_walled(tg, li, net_id, far) or _tile_walled(tg, li, net_id, near)
        if not cdt:
            misses.append(net)
        rows["connections"] += 1
        rows["cdt"] += cdt
        rows["tile"] += tile
        rows["tile_walled"] += walled
        rows["cdt_where_tile_walled"] += cdt and walled
        rows["cdt_only"] += cdt and not tile
        rows["tile_only"] += tile and not cdt
    summary = topo.summarize(graphs, [])
    return {
        "counts": dict(rows),
        "layers": summary["layers"],
        "cdt_misses": sorted(misses),
        "cdt_s": round(cdt_s, 2),
    }


def _table(board: str, result: dict[str, Any]) -> str:
    c = result["counts"]
    lines = [
        f"{board}: connections {c['connections']}, CDT portal sequence {c['cdt']}, "
        f"flow tile path {c['tile']}, terminal tile walled (0 lanes) {c['tile_walled']}, "
        f"CDT finds one where tile walled {c['cdt_where_tile_walled']}, "
        f"CDT only {c['cdt_only']}, tile only {c['tile_only']}; "
        f"CDT misses {result['cdt_misses']}; region graphs built in {result['cdt_s']} s"
    ]
    for lay, s in result["layers"].items():
        lines.append(
            f"  {lay}: {s['triangles']} triangles, {s['portals']} portals, "
            f"{s['zero_cap']} at 0, capacity histogram {s['cap_hist']}"
        )
    return "\n".join(lines)


@pytest.mark.slow
def test_qfp_plaza_cdt_against_flow_tiles(monkeypatch):
    from dataclasses import replace

    from precis.pcb import session
    from tests.fixtures.pcb import qfp_fanout as qf
    from tests.test_pcb_globalplan_ewod import _capture_pass_args, _static_grid

    spec = replace(qf.FanoutSpec(pins_per_side=20, n_nets=55, sources="plaza"))
    built = qf.build(spec)
    qf.warm_start(built, "distance")
    ir = built.ir
    captured = _capture_pass_args(monkeypatch, ir, built.config, built.footprints, [])
    static = _static_grid(ir, captured, built.config, [])
    names = {i: str(layer["name"]) for i, layer in enumerate(ir.stackup)}
    plan = tuple(i for i, n in names.items() if n in spec.layers)
    tg = globalplan.build_tile_graph(
        static,
        layers=plan,
        track_mm=spec.track_mm,
        clearance_mm=spec.clearance_mm,
        via_dia_mm=spec.via_dia_mm,
        participant_nets=set(range(len(ir.net_name))),
        strict=True,
        capacity="flow",
    )
    bottom = next(i for i, n in names.items() if n == "B.Cu")
    pairs = []
    for seg in range(len(ir.seg_net)):
        a, b = int(ir.seg_pin_a[seg]), int(ir.seg_pin_b[seg])
        pa, pb = pin_point(ir, a), pin_point(ir, b)
        assert pa is not None and pb is not None
        net_id = int(ir.seg_net[seg])
        pairs.append((str(ir.net_name[net_id]), net_id, pa, pb, bottom))
    model = session._copper_model(ir, [], footprints=built.footprints)
    result = _compare(
        model,
        qf.outline(spec),
        tg,
        names,
        pairs,
        track_mm=spec.track_mm,
        clearance_mm=spec.clearance_mm,
    )
    print("\n" + _table("qfp-plaza", result))
    counts = result["counts"]
    assert counts["connections"] == 55
    assert counts == QFP_PLAZA_COUNTS


@pytest.mark.slow
def test_ewod_replay_cdt_against_flow_tiles(store, monkeypatch):
    from precis.pcb import session
    from precis.workers.job_types.pcb_route import (
        _apply_pin_swap_warm_start,
        _resolve_pin_swap_groups,
    )
    from tests.test_pcb_escape_replay import _hydrate
    from tests.test_pcb_globalplan_ewod import (
        ESCAPE_CLASS,
        PLAN_LAYERS,
        _capture_pass_args,
        _escape_reqs,
        _open_in2,
        _static_grid,
    )

    ir, graph, features, footprints, fixed, config = _hydrate(store)
    _open_in2(ir, config)
    groups = _resolve_pin_swap_groups(ir, graph, [])
    _apply_pin_swap_warm_start(ir, groups, footprints, fixed, "distance")
    captured = _capture_pass_args(monkeypatch, ir, config, footprints, fixed)
    reqs = _escape_reqs(ir, captured, config)
    static = _static_grid(ir, captured, config, fixed)
    names = {i: str(layer["name"]) for i, layer in enumerate(ir.stackup)}
    plan = tuple(i for i, n in names.items() if n in PLAN_LAYERS)
    req0 = next(iter(reqs.values()))
    track = float(req0.rules.track_width_mm)
    clearance = float(
        config.class_rules[ESCAPE_CLASS].get("clearance_mm") or captured["clearance"]
    )
    tg = globalplan.build_tile_graph(
        static,
        layers=plan,
        track_mm=track,
        clearance_mm=clearance,
        via_dia_mm=req0.rules.via_dia_mm or 0.45,
        participant_nets={r.net_id for r in reqs.values()},
        strict=True,
        capacity="flow",
    )
    sink_instance = groups[0].instance
    pairs = []
    for seg, req in reqs.items():
        a, b = int(ir.seg_pin_a[seg]), int(ir.seg_pin_b[seg])
        sink_pin = a if int(ir.pin_instance[a]) == sink_instance else b
        far_pin = b if sink_pin == a else a
        term_a, term_b = captured["island_terminals"].get(seg, ((), ()))
        terms_far = term_a if far_pin == a else term_b
        vias = sorted({t.point for t, _l in terms_far if t.kind == "via"})
        pad = pin_point(ir, sink_pin)
        assert len(vias) == 1 and pad is not None
        pairs.append(
            (
                str(ir.net_name[req.net_id]),
                req.net_id,
                vias[0],
                pad,
                int(req.goal_layer),
            )
        )
    model = session._copper_model(
        ir,
        [{**row, "net": row.get("net") or ""} for row in fixed],
        footprints=footprints,
    )
    outline = session.outline_from_features(features)
    assert outline is not None
    result = _compare(
        model,
        outline,
        tg,
        names,
        pairs,
        track_mm=track,
        clearance_mm=clearance,
    )
    print("\n" + _table("ewod-replay", result))
    counts = result["counts"]
    assert counts["connections"] == 55
    assert counts == EWOD_COUNTS


#: Measured 2026-10-18 (docs/backlog/pcb-vector-router.md slice 5). The
#: probe is deterministic; a change here is a change in the model.
QFP_PLAZA_COUNTS = {
    "connections": 55,
    "cdt": 54,
    "tile": 25,
    "tile_walled": 27,
    "cdt_where_tile_walled": 26,
    "cdt_only": 29,
    "tile_only": 0,
}
EWOD_COUNTS = {
    "connections": 55,
    "cdt": 55,
    "tile": 34,
    "tile_walled": 14,
    "cdt_where_tile_walled": 14,
    "cdt_only": 21,
    "tile_only": 0,
}
