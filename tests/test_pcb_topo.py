"""CDT region graph and physical portal capacities — the slice-5 probe of
docs/backlog/pcb-vector-router.md. Hand-built layouts with known gaps
first, then the comparison against the flow tile model on the QFP plaza
member and the EWOD dogfood-6 replay (slow): for each connection, does a
portal sequence exist, and where the tile model reads 0 lanes at a
terminal, does the CDT still find one?"""

from __future__ import annotations

import collections
import math
import time
from collections import deque
from typing import Any

import pytest
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from precis.pcb import globalplan, rules, topo
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import pin_point

W = 0.1  # track
S = 0.1  # clearance
OUTLINE = [[0.0, 0.0], [10.0, 0.0], [10.0, 6.0], [0.0, 6.0]]


def _ctx(net_rules: dict[str, rules.NetRules] | None = None) -> rules.RuleContext:
    """A minimal rule context: the 2-layer fab row, the given per-net
    resolved rules, both copper layers outer (the IPC column split)."""
    return rules.RuleContext(
        fab_caps=capability_for("2layer"),
        net_rules=net_rules or {},
        outer_layers=frozenset({"F.Cu", "B.Cu"}),
        coated=True,
    )


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
    shorted: list[str] = []
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
            g = graphs[name]
            ends = [
                g.obstacle_at(net, *far),
                g.obstacle_at(net, *near),
            ]
            if any(e is not None and g.shorted_by(e) for e in ends):
                shorted.append(net)
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
        "cdt_miss_shorted": sorted(shorted),
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
    # gr496698: the one miss is not a routing cut. The plaza source at
    # (0, 9) is a drilled pad sitting on two unconnected QFP pads of the
    # top row (the fixture's plaza lattice lands on the pin row), so the
    # terminal is buried in foreign copper.
    assert result["cdt_misses"] == ["N31"]
    assert result["cdt_miss_shorted"] == ["N31"]


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


# ── net domain, required()-priced capacity and the funnel (slice B) ──────


def _net_rules(**overrides: Any) -> dict[str, rules.NetRules]:
    cap = capability_for("2layer")
    return {
        name: rules.resolve_net_rules(
            cls, layer_is_outer=True, fab_caps=cap, overrides=rules_ov or None
        )
        for name, (cls, rules_ov) in {
            "A": ("", {}),
            "B": ("", {}),
            "C": ("", {}),
            "D": ("", {}),
            **{k: (k, v) for k, v in overrides.items()},
        }.items()
    }


def test_net_domain_drops_own_copper_and_inflates_foreign_by_required():
    a = _ob("A", box(1.0, 2.0, 2.0, 3.0))
    b = _ob("B", box(4.0, 2.0, 5.0, 3.0))
    ctx = _ctx()
    dom = topo.net_region_graph([a, b], OUTLINE, "F.Cu", net="A", track_mm=W, ctx=ctx)
    assert [o.net for o in dom.obstacles] == ["B"]
    warn = rules.required(rules.Owner("A"), rules.Owner("B", "pad"), "F.Cu", ctx)
    grow = W / 2 + float(warn.warn_mm or warn.error_mm)
    # The inflation shrinks the A↔B gap by [grow − δ, grow + 2δ] (the
    # outward polys sit ≤ δ outside their bodies on both sides).
    d = b.poly.distance(a.poly) - dom.obstacles[0].poly.distance(a.poly)
    assert grow - 2 * topo.DELTA_MM - 1e-9 <= d <= grow + 3 * topo.DELTA_MM + 1e-9


def test_net_domain_triangulation_is_valid():
    """No free triangle crosses an inflated obstacle; every portal lies
    in free space (clearance respected by construction)."""
    obs = [
        _ob("A", box(1.0, 2.5, 2.0, 3.5)),
        _ob("B", box(4.0, 1.0, 5.0, 2.4)),
        _ob("C", Point(7.0, 4.0).buffer(0.8)),
        _ob("D", box(3.0, 4.2, 4.5, 5.4)),
    ]
    ctx = _ctx()
    dom = topo.net_region_graph(obs, OUTLINE, "F.Cu", net="A", track_mm=W, ctx=ctx)
    board = Polygon(OUTLINE)
    union = unary_union([o.poly for o in dom.obstacles])
    inner = union.buffer(-1e-6)  # strict interior (boundary contact is legal)
    for t in dom.triangles:
        tri = Polygon(t)
        assert board.covers(tri) or tri.difference(board).area < 1e-9
        assert tri.intersection(inner).area < 1e-9
    for p in dom.portals:
        seg = LineString(p.uv)
        assert seg.intersection(inner).length < 1e-6


def test_capacity_for_prices_each_end_with_required():
    cap = capability_for("2layer")
    net_rules = {
        "A": rules.resolve_net_rules("", layer_is_outer=True, fab_caps=cap),
        "HV": rules.resolve_net_rules(
            "hv", layer_is_outer=True, fab_caps=cap, overrides={"clearance_mm": 0.4}
        ),
    }
    ctx = _ctx(net_rules)
    own = _ob("A", box(2.0, 2.0, 3.0, 3.0))
    hv = _ob("HV", box(3.7, 2.0, 4.7, 3.0))
    g = _graph([own, hv])
    portal = min(
        (p for p in g.portals if set(p.ends) == {0, 1}),
        key=lambda p: p.length_mm,
    )
    req = rules.required(rules.Owner("A"), rules.Owner("HV", "pad"), "F.Cu", ctx)
    between = net_rules["A"].tiers_on("F.Cu", outer=True, coated=True).clearance_mm
    # The own-net end owes nothing (same-net merge); the HV end owes its
    # 0.4 mm tier; tracks among themselves owe A's own tier.
    assert (
        g.capacity_for(portal, "A", ctx)
        == topo.portal_capacity(
            portal.length_mm,
            W,
            S,
            end_a_mm=0.0,
            end_b_mm=float(req.warn_mm or 0.0),
            between_mm=between,
        )
        == topo.portal_capacity(
            portal.length_mm, W, S, end_a_mm=0.0, end_b_mm=0.4, between_mm=between
        )
    )
    # Pricing the portal for HV's own track (0.4 on both ends, HV's tier
    # between) leaves less room than for A's.
    assert g.capacity_for(portal, "HV", ctx) <= g.capacity_for(portal, "A", ctx)


def test_funnel_shortest_path_is_the_known_detour():
    """box(4,2,6,3.5) between (1,3)→(9,3): the top detour is the shorter
    (8.083 vs the bottom's 8.325)."""
    g = _graph([_ob("B", box(4.0, 2.0, 6.0, 3.5))])
    band = topo.funnel_path(g, (1.0, 3.0), (9.0, 3.0))
    assert band is not None
    opt = 2 * math.hypot(3.0, 0.5) + 2.0
    # Corners are the outward polygon's, ≤ δ past the box's own.
    assert opt <= band.length_mm <= opt + 4 * topo.DELTA_MM + 0.02
    assert band.length_mm < 2 * math.hypot(3.0, 1.0) + 2.0
    top = [p for p in band.points if p[1] > 3.4]
    assert len(top) == 2 and all(3.9 < p[0] < 6.1 for p in top)
    line = LineString(band.points)
    assert g.sleeve_polygon(band.sleeve).buffer(1e-9).covers(line)


def test_funnel_band_stays_inside_its_sleeve_over_a_bend():
    obs = [
        _ob("B", box(2.0, 0.0, 4.0, 2.4)),
        _ob("C", box(6.0, 3.6, 8.0, 6.0)),
        _ob("D", box(2.0, 4.0, 4.0, 5.5)),
    ]
    g = _graph(obs)
    band = topo.funnel_path(g, (1.0, 3.0), (9.0, 3.0))
    assert band is not None
    line = LineString(band.points)
    assert g.sleeve_polygon(band.sleeve).buffer(1e-9).covers(line)
    blocked = unary_union([o.poly for o in g.obstacles]).buffer(-1e-9)
    assert line.intersection(blocked).length < 1e-6


def test_side_swap_candidates_thread_the_other_side():
    g = _graph([_ob("B", box(4.0, 2.0, 6.0, 3.5))])
    base = topo.sleeve_between(g, (1.0, 3.0), (9.0, 3.0))
    assert base is not None
    base_band = topo.band_in_sleeve(g, base, (1.0, 3.0), (9.0, 3.0))
    assert max(p[1] for p in base_band.points) > 3.4  # the top sleeve
    cands = topo.side_swap_candidates(g, base)
    assert cands
    assert all(tuple(s[1]) != tuple(base[1]) for s in cands)
    bottoms = [
        topo.band_in_sleeve(g, s, (1.0, 3.0), (9.0, 3.0))
        for s in cands
        if any(
            p[1] < 2.0 for p in topo.band_in_sleeve(g, s, (1.0, 3.0), (9.0, 3.0)).points
        )
    ]
    assert bottoms and min(b.length_mm for b in bottoms) < 8.4


def test_funnel_reports_a_buried_terminal():
    obs = [_ob("B", box(0.5, 2.0, 2.5, 4.0))]
    dom = topo.net_region_graph(obs, OUTLINE, "F.Cu", net="A", track_mm=W, ctx=_ctx())
    assert dom.locate(1.0, 3.0) is None
    assert topo.funnel_path(dom, (1.0, 3.0), (9.0, 3.0)) is None


def test_funnel_on_the_net_domain_respects_pairwise_clearance():
    """On net A's domain the band keeps `required()` from foreign copper:
    the HV pad's 0.4 tier pushes it off B's corridor where 0.152 fits."""
    cap = capability_for("2layer")
    net_rules = {
        "A": rules.resolve_net_rules("", layer_is_outer=True, fab_caps=cap),
        "HV": rules.resolve_net_rules(
            "hv", layer_is_outer=True, fab_caps=cap, overrides={"clearance_mm": 0.4}
        ),
    }
    ctx = _ctx(net_rules)
    # Slit between two HV pads: 0.5 mm. A's band (w=0.1) needs
    # 0.05 + 0.4 + 0.4 + 0.05 = 0.9 mm — the domain has no corridor;
    # at s=0.15 it would need only 0.4 mm.
    obs = [
        _ob("HV", box(4.0, 0.0, 6.0, 3.0)),
        _ob("HV", box(4.0, 3.5, 6.0, 6.0)),
    ]
    dom = topo.net_region_graph(obs, OUTLINE, "F.Cu", net="A", track_mm=W, ctx=ctx)
    assert topo.funnel_path(dom, (1.0, 3.25), (9.0, 3.25)) is None
    # With ordinary clearance the same slit admits the band.
    ctx2 = _ctx(_net_rules())
    dom3 = topo.net_region_graph(obs, OUTLINE, "F.Cu", net="A", track_mm=W, ctx=ctx2)
    band = topo.funnel_path(dom3, (1.0, 3.25), (9.0, 3.25))
    assert band is not None
    line = LineString(band.points)
    assert (
        unary_union([o.poly for o in obs]).buffer(-1e-9).intersection(line).length
        < 1e-6
    )


# ── performance on real boards (slow) ────────────────────────────────────


def _perf_board(
    model: dict[str, Any],
    outline: list[list[float]],
    graphs: dict[str, list[topo.Obstacle]],
    conns: list[tuple[str, str, tuple[float, ...], tuple[float, ...], float]],
    ctx: rules.RuleContext,
    label: str,
) -> dict[str, Any]:
    """Time net-domain builds and funnel paths over ``conns``
    ``(net, layer, a, b, track_mm)``; prints the table line."""
    t0 = time.monotonic()
    built: dict[tuple[str, str], topo.RegionGraph] = {}
    for net, lay, _a, _b, w in conns:
        key = (net, lay)
        if key not in built:
            built[key] = topo.net_region_graph(
                graphs[lay], outline, lay, net=net, track_mm=w, ctx=ctx
            )
    domain_s = time.monotonic() - t0
    t0 = time.monotonic()
    bands = misses = 0
    for net, lay, a, b, w in conns:
        if topo.funnel_path(built[(net, lay)], (a[0], a[1]), (b[0], b[1])) is not None:
            bands += 1
        else:
            misses += 1
    funnel_s = time.monotonic() - t0
    sizes = {key: len(g.triangles) for key, g in built.items()}
    print(
        f"\n{label}: {len(conns)} conns, {len(built)} net domains in "
        f"{domain_s:.2f}s, funnel {bands} ok {misses} miss in {funnel_s:.2f}s; "
        f"median tris/domain {sorted(sizes.values())[len(sizes) // 2]}"
    )
    return {"domains": len(built), "bands": bands, "misses": misses}


@pytest.mark.slow
def test_ewod_replay_net_domain_funnel_perf(store, monkeypatch):
    from precis.pcb import gerber, session
    from precis.workers.job_types.pcb_route import (
        _apply_pin_swap_warm_start,
        _graph_net_rules,
        _graph_net_voltages,
        _resolve_pin_swap_groups,
    )
    from tests.test_pcb_escape_replay import _hydrate
    from tests.test_pcb_globalplan_ewod import (
        PLAN_LAYERS,
        _capture_pass_args,
        _escape_reqs,
        _open_in2,
    )

    ir, graph, features, footprints, fixed, config = _hydrate(store)
    _open_in2(ir, config)
    groups = _resolve_pin_swap_groups(ir, graph, [])
    _apply_pin_swap_warm_start(ir, groups, footprints, fixed, "distance")
    captured = _capture_pass_args(monkeypatch, ir, config, footprints, fixed)
    reqs = _escape_reqs(ir, captured, config)
    names = {i: str(layer["name"]) for i, layer in enumerate(ir.stackup)}
    model = session._copper_model(
        ir,
        [{**row, "net": row.get("net") or ""} for row in fixed],
        footprints=footprints,
    )
    outline = session.outline_from_features(features)
    assert outline is not None
    ctx = rules.RuleContext(
        fab_caps=config.fab_caps,
        net_rules=_graph_net_rules(graph, config.fab_caps),
        outer_layers=frozenset({names[0], names[len(names) - 1]}),
        net_voltages=_graph_net_voltages(graph),
        coated=gerber.declares_parylene_coat(model),
    )
    obstacles = {
        names[li]: topo.board_obstacles(model, names[li])
        for li in {int(r.goal_layer) for r in reqs.values()}
        if names[li] in PLAN_LAYERS
    }
    sink_instance = groups[0].instance
    conns = []
    for seg, req in reqs.items():
        a, b = int(ir.seg_pin_a[seg]), int(ir.seg_pin_b[seg])
        sink_pin = a if int(ir.pin_instance[a]) == sink_instance else b
        far_pin = b if sink_pin == a else a
        term_a, term_b = captured["island_terminals"].get(seg, ((), ()))
        terms_far = term_a if far_pin == a else term_b
        vias = sorted({t.point for t, _l in terms_far if t.kind == "via"})
        pad = pin_point(ir, sink_pin)
        lay = names[int(req.goal_layer)]
        if lay not in obstacles or not vias or pad is None:
            continue
        conns.append(
            (
                str(ir.net_name[req.net_id]),
                lay,
                tuple(map(float, vias[0])),
                tuple(map(float, pad)),
                float(req.rules.track_width_mm),
            )
        )
    result = _perf_board(model, outline, obstacles, conns, ctx, "ewod-replay")
    # Every escape connection's far via is free copper of its own net, so
    # the band domain should hold a sleeve for essentially all of them.
    assert result["bands"] >= len(conns) - 2


@pytest.mark.slow
def test_nano_net_domain_funnel_perf(store):
    from precis.dispatch import Hub
    from precis.handlers.pcb import PcbHandler
    from precis.pcb import resolved, session
    from precis.pcb.drc import process_for_stackup
    from precis.workers.job_types.pcb_route import (
        _graph_net_rules,
        _graph_net_voltages,
    )
    from tests.fixtures.pcb import nano_usb_pd_testboard as fx
    from tests.test_pcb_nano_usb_pd_testboard import _drain_one_job

    for lcsc, fp in fx.CATALOG_FOOTPRINTS.items():
        store.part_footprint_put(lcsc, fp)
    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id="nano-topo", args=dict(fx.DESIGN)).body
    ref = store.get_ref(kind="pcb", id="nano-topo")
    assert "enqueued" in pcb.put(id="nano-topo", args={"op": "place", "seed": 1}).body
    _drain_one_job(store, ref.id, unrouted_ok=False)
    graph = store.pcb_graph(ref.id)
    features = store.pcb_features_list(ref.id)
    local = store.pcb_local_footprints_for(ref.id)
    cached = store.pcb_footprints_for(ref.id)
    fixed = store.pcb_fixed_copper_list(graph["board"]["board_id"])
    ir = session.build_ir(
        resolved.apply(graph, store.pcb_netlist_snapshot_latest(ref.id)),
        outline=session.outline_from_features(features),
        mounting_holes=session.mounting_holes_from_features(features),
        footprints_by_lcsc=cached,
        local_footprints_by_name=local,
        fixed_copper=fixed,
    )
    footprints = session.footprints_by_refdes(
        ir,
        cached,
        local_footprints_by_name=local,
        local_names_by_refdes=session.local_footprint_names_by_refdes(graph),
    )
    model = session._copper_model(ir, list(fixed), footprints=footprints)
    outline = session.outline_from_features(features)
    assert outline is not None
    names = {i: str(layer["name"]) for i, layer in enumerate(ir.stackup)}
    cap = capability_for(process_for_stackup(ir.stackup))
    ctx = rules.RuleContext(
        fab_caps=cap,
        net_rules=_graph_net_rules(graph, cap),
        outer_layers=frozenset({names[0], names[len(names) - 1]}),
        net_voltages=_graph_net_voltages(graph),
    )
    net_rules = ctx.net_rules
    conns = []
    for seg in range(len(ir.seg_net)):
        pa, pb = (
            pin_point(ir, int(ir.seg_pin_a[seg])),
            pin_point(ir, int(ir.seg_pin_b[seg])),
        )
        if pa is None or pb is None:
            continue
        net = str(ir.net_name[int(ir.seg_net[seg])])
        w = float(net_rules[net].track_width_mm) if net in net_rules else 0.2
        conns.append((net, names[0], tuple(map(float, pa)), tuple(map(float, pb)), w))
    obstacles = {names[0]: topo.board_obstacles(model, names[0])}
    result = _perf_board(model, outline, obstacles, conns, ctx, "nano F.Cu")
    assert result["domains"] > 0
