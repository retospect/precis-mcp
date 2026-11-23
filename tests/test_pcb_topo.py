"""CDT region graph and physical portal capacities — the slice-5 probe of
docs/backlog/pcb-vector-router.md. Hand-built layouts with known gaps
first, then the comparison against the flow tile model on the QFP plaza
member and the EWOD dogfood-6 replay (slow): for each connection, does a
portal sequence exist, and where the tile model reads 0 lanes at a
terminal, does the CDT still find one?"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import numpy as np
import pytest
from shapely.geometry import Point, box

from precis.pcb import rules, topo
from precis.pcb.capabilities import capability_for
from precis.store import Store

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


class _PlaceCtx:
    """Minimal ``DispatchContext`` for ``pcb_place._dispatch`` — mirrors
    ``tests/workers/test_pcb_place.py``'s fake: chunks land on
    ``summaries``, meta updates on ``meta``."""

    def __init__(self, store, *, params: dict[str, Any]) -> None:
        self.store = store
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="pcb_place test",
            meta={"executor": "job_inproc", "job_type": "pcb_place"},
        )
        self.ref_id = int(ref.id)
        self.title = "pcb_place test"
        self.meta: dict[str, Any] = {"params": params}
        self.failures: list[tuple[str, str | None]] = []
        self.summaries: list[tuple[str, str]] = []

    def record_failure(self, reason: str, *, failure_class: str | None = None) -> None:
        self.failures.append((reason, failure_class))

    def append_chunk(self, kind: str, text: str) -> None:
        self.summaries.append((kind, text))

    def set_status(self, value: str) -> None:
        pass

    def set_meta(self, **kw: Any) -> None:
        self.meta.update(kw)

    def is_cancel_requested(self) -> bool:
        return False


def _all_fixed_design() -> dict[str, Any]:
    """Four ``fixed='both'`` parts, two 2-pin nets — the jo635337
    all-locked shape in miniature: nothing the shove LP may move."""
    from tests._pcb_outline import closed_outline

    return {
        "components": [
            {
                "refdes": r,
                "label": "ic",
                "x": x,
                "y": y,
                "pins": [{"name": "1"}],
                "fixed": "both",
            }
            for r, x, y in (
                ("A", 50.0, 50.0),
                ("B", 52.0, 52.0),
                ("C", 50.0, 52.0),
                ("D", 52.0, 50.0),
            )
        ],
        "nets": [
            {"name": "N1", "class": "signal", "current": 0.001},
            {"name": "N2", "class": "signal", "current": 0.001},
        ],
        "connections": [
            {"net": "N1", "refdes": "A", "pin": "1"},
            {"net": "N1", "refdes": "B", "pin": "1"},
            {"net": "N2", "refdes": "C", "pin": "1"},
            {"net": "N2", "refdes": "D", "pin": "1"},
        ],
        "features": [closed_outline()],
    }


def _place_ctx(store: Any, pcb_ref_id: int) -> _PlaceCtx:
    return _PlaceCtx(
        store,
        params={
            "pcb_ref_id": pcb_ref_id,
            "placer": "topo",
            "route": True,
            "iters": 50,
            "seed": 1,
        },
    )


def test_route_records_shove_reason(store, monkeypatch) -> None:
    """gr635502 slice 2: the route stage's failure-driven shove used to
    break silently — a 0-round/0-mm shove on a still-failing board
    landed ``reason ''`` in the run record with no job line. An
    all-``fixed='both'`` board whose plan still fails must name the
    stop: the last ShoveResult lands on ``meta.topo_run.route.shove``
    and a job_event carries it."""
    from precis.dispatch import Hub
    from precis.handlers.pcb import PcbHandler
    from precis.pcb import realize as pcb_realize
    from precis.pcb.topoplace import realise
    from precis.workers.executors._context import DispatchContext
    from precis.workers.job_types import pcb_place

    PcbHandler(hub=Hub(store=store)).put(id="shove-reason", args=_all_fixed_design())
    ref = store.get_ref(kind="pcb", id="shove-reason")
    assert ref is not None

    def _failing(d, ir, **kw):
        # One no_via branch over net 0's two endpoint parts — enough to
        # mint a push row the all-fixed shove cannot serve.
        pins = d.net_pins[0]
        piece = realise.RoutePiece(
            net=0,
            branch=0,
            layer=0,
            points=((0.0, 0.0), (1.0, 0.0)),
            pin_a=int(pins[0]),
            pin_b=int(pins[-1]),
        )
        plan = realise.RoutePlan(pieces=(piece,), vias=(), layers=(0,), no_via=(0,))
        return plan, pcb_realize.RealizeResult((), (), ())

    monkeypatch.setattr(realise, "route_design", _failing)
    ctx = _place_ctx(store, int(ref.id))
    pcb_place._dispatch(cast(DispatchContext, ctx), pcb_place.SPEC)
    assert not ctx.failures
    shove = ctx.meta["topo_run"]["route"]["shove"]
    assert shove is not None
    assert shove["reason"] == "all-fixed"
    assert shove["rounds"] == 0 and shove["moved_mm"] == 0.0
    events = [t for k, t in ctx.summaries if k == "job_event"]
    assert any("shove stopped: all-fixed" in t for t in events)


def test_route_shove_runs_on_legalise_only_failures(store, monkeypatch) -> None:
    """gr635502, the "never runs" half: jo635337's five failing nets
    carried ONLY the ``legalise_reroute`` mark ("legalisation could not
    realise this net's spacing inside the move caps"), which
    ``_plan_failed_count`` did not count — ``best_failed`` read 0 and
    the shove loop broke before its first ``shove_pose`` call. The
    count now covers every failed-net family, so the same board must
    run the shove, record its ``all-fixed`` stop and emit the event."""
    from precis.dispatch import Hub
    from precis.handlers.pcb import PcbHandler
    from precis.pcb import realize as pcb_realize
    from precis.pcb.topoplace import realise
    from precis.workers.executors._context import DispatchContext
    from precis.workers.job_types import pcb_place

    PcbHandler(hub=Hub(store=store)).put(id="shove-legalise", args=_all_fixed_design())
    ref = store.get_ref(kind="pcb", id="shove-legalise")
    assert ref is not None

    def _failing(d, ir, **kw):
        # net 0 provisional geometry + the legalise mark only — the
        # exact failure shape that used to read as zero failures.
        pins = d.net_pins[0]
        piece = realise.RoutePiece(
            net=0,
            branch=0,
            layer=0,
            points=((0.0, 0.0), (1.0, 0.0)),
            pin_a=int(pins[0]),
            pin_b=int(pins[-1]),
        )
        plan = realise.RoutePlan(
            pieces=(piece,),
            vias=(),
            layers=(0,),
            legalise_reroute=(0,),
            withdrawn=(0,),
        )
        return plan, pcb_realize.RealizeResult((), (), ())

    monkeypatch.setattr(realise, "route_design", _failing)
    ctx = _place_ctx(store, int(ref.id))
    pcb_place._dispatch(cast(DispatchContext, ctx), pcb_place.SPEC)
    assert not ctx.failures
    # before the count fix the loop never ran: ``shove`` was None.
    shove = ctx.meta["topo_run"]["route"]["shove"]
    assert shove is not None
    assert shove["reason"] == "all-fixed"
    assert shove["rounds"] == 0 and shove["moved_mm"] == 0.0
    events = [t for k, t in ctx.summaries if k == "job_event"]
    assert any("shove stopped: all-fixed" in t for t in events)


def test_plan_failed_count_covers_every_failure_family() -> None:
    """The keep-best metric is a failed-NETS count across every family
    ``sketch_rows`` calls failed — a family it missed read 0 and gated
    the shove off (gr635502)."""
    from precis.workers.job_types import pcb_place

    def _plan(**kw: Any) -> SimpleNamespace:
        base: dict[str, Any] = dict(pieces=(), crossings=(), unreachable=(), no_via=())
        base.update(kw)
        return SimpleNamespace(**base)

    assert pcb_place._plan_failed_count(_plan()) == 0
    assert pcb_place._plan_failed_count(_plan(legalise_reroute=(0, 1))) == 2
    assert pcb_place._plan_failed_count(_plan(withdrawn=(2,))) == 1
    # (net, pin) pairs — two missing pads on one net are ONE failed net
    assert pcb_place._plan_failed_count(_plan(fanout_missing=((3, 0), (3, 5)))) == 1
    assert pcb_place._plan_failed_count(_plan(unreachable=(1,))) == 1
    assert (
        pcb_place._plan_failed_count(
            _plan(stranded=(SimpleNamespace(net=2, pin=0, owners=()),))
        )
        == 1
    )
    # crossings/no_via resolve through the branch→net map; an unmapped
    # branch still counts under its sentinel — a net marked twice counts once
    piece = SimpleNamespace(branch=0, net=4)
    plan = _plan(pieces=(piece,), crossings=((0, 9),), no_via=(0,))
    assert pcb_place._plan_failed_count(plan) == 2


def test_route_failure_pushes_cover_stranded_and_legalise() -> None:
    """The push half: a stranded component mints a walled-pin↔owner row
    and a legalise/withdrawn net's provisional piece mints the same
    endpoint-separation row a no_via branch does (gr635502)."""
    from precis.pcb.topoplace.wires import Unreachable
    from precis.workers.job_types import pcb_place

    td = SimpleNamespace(
        pin_part=np.asarray([0, 1, 2]),
        net_name=("N1", "N2"),
    )
    pose = np.asarray(
        [[0.0, 0.0, 0.0, 0.0], [10.0, 0.0, 0.0, 0.0], [5.0, 5.0, 0.0, 0.0]]
    )
    piece = SimpleNamespace(
        net=0, branch=0, layer=0, points=((0.0, 0.0), (1.0, 0.0)), pin_a=0, pin_b=1
    )
    plan = SimpleNamespace(
        pieces=(piece,),
        crossings=(),
        unreachable=(0,),
        no_via=(),
        guarded=(),
        severed=(),
        withdrawn=(0,),
        legalise_reroute=(0,),
        fanout_missing=(),
        stranded=(Unreachable(net=0, pin=2, owners=(1,)),),
    )
    pushes = pcb_place._route_failure_pushes(td, plan, pose, {}, 0.6)
    pairs = {(min(int(p.a), int(p.b)), max(int(p.a), int(p.b))) for p in pushes}
    # endpoint separation for the legalise-failed net's piece (parts 0–1)
    # and the stranded pin 2's part off its walling owner (parts 1–2)
    assert (0, 1) in pairs and (1, 2) in pairs


def _channel_wall_design() -> dict[str, Any]:
    """Three signal pairs crossing a TH-pad wall with one 0.6 mm
    channel (one slot per outer layer): demand 3 > 2, so the route
    stage fails at the FIXED placement — and every part is
    ``fixed='both'``, so the part shove can move nothing."""
    from tests._pcb_outline import closed_outline

    fp = {
        "name": "THW",
        "pads": [
            {
                "pin": "1",
                "shape": "rect",
                "x": 0.0,
                "y": 0.0,
                "w": 0.6,
                "h": 0.6,
                "drill": 0.3,
            }
        ],
    }
    comps: list[dict[str, Any]] = []
    nets: list[dict[str, Any]] = []
    conns: list[dict[str, Any]] = []
    for k, y in enumerate((8.25, 10.25, 12.25)):
        comps += [
            {
                "refdes": f"A{k}",
                "footprint": "THW",
                "pins": [{"name": "1"}],
                "x": 5.0,
                "y": y,
                "fixed": "both",
            },
            {
                "refdes": f"B{k}",
                "footprint": "THW",
                "pins": [{"name": "1"}],
                "x": 15.0,
                "y": y,
                "fixed": "both",
            },
        ]
        nets.append({"name": f"N{k}", "class": "signal", "current": 0.001})
        conns += [
            {"net": f"N{k}", "refdes": f"A{k}", "pin": "1"},
            {"net": f"N{k}", "refdes": f"B{k}", "pin": "1"},
        ]
    # wall: 0.4 mm pad gaps are closed (a lane needs ~0.6); the
    # shifted pad leaves exactly one 0.6 mm channel, and the row's
    # ends sit 0.42 mm inside the outline — clear of the 0.4 mm V-cut
    # edge rule but closed to routing (a >=0.5 mm residual opens a
    # lane), so the channel is the only way across x=10.
    ys = [0.72 + 1.0 * i for i in range(20)]
    ys[10] += 0.2
    for i, y in enumerate(ys):
        comps.append(
            {
                "refdes": f"W{i}",
                "footprint": "THW",
                "pins": [{"name": "1"}],
                "x": 10.0,
                "y": y,
                "fixed": "both",
            }
        )
        nets.append({"name": f"W{i}", "class": "signal", "current": 0.001})
        conns.append({"net": f"W{i}", "refdes": f"W{i}", "pin": "1"})
    return {
        "components": comps,
        "nets": nets,
        "connections": conns,
        "footprints": [fp],
        "features": [closed_outline(0.0, 0.0, 20.0, 20.44)],
    }


def test_route_stops_without_retry_at_fixed_placement(store: Store) -> None:
    """gr696400: an all-fixed, over-capacity board gets one route attempt."""
    from precis.dispatch import Hub
    from precis.handlers.pcb import PcbHandler
    from precis.pcb.topoplace import realise
    from precis.workers.executors._context import DispatchContext
    from precis.workers.job_types import pcb_place

    pcb = PcbHandler(hub=Hub(store=store))
    pcb.put(id="track-shove", args=_channel_wall_design())
    # 2 signal layers — the default 4l stackup gives the channel four
    # slots, enough for demand 3; on 2l it is cap 2, demand 3.
    pcb.put(
        id="track-shove",
        args={
            "op": "stackup",
            "layers": [
                {"name": "F.Cu", "role": "signal"},
                {"name": "B.Cu", "role": "signal"},
            ],
        },
    )
    ref = store.get_ref(kind="pcb", id="track-shove")
    assert ref is not None
    ctx = _place_ctx(store, int(ref.id))
    with patch.object(
        realise, "route_design", wraps=realise.route_design
    ) as route_design:
        pcb_place._dispatch(cast(DispatchContext, ctx), pcb_place.SPEC)
    route_design.assert_called_once()
    assert not ctx.failures
    route = ctx.meta["topo_run"]["route"]
    assert route["failed"] > 0, "channel demand 3 > 2 slots must fail nets"
    assert route["shove"] is not None and route["shove"]["reason"] == "all-fixed"
    assert route["track_shove"] is None
    assert route["shove_stops"] == ["all-fixed"]
    # the committed plan carries no illegal crossing; every stored route
    # row is either realized or a named honest failure, never a short
    assert route["crossings"] == 0
    events = [t for k, t in ctx.summaries if k == "job_event"]
    assert any("shove stopped:" in t for t in events)


def test_route_blocked_shove_names_its_stop(store: Store) -> None:
    """Record why placement recovery stopped on a saturated channel."""
    from precis.dispatch import Hub
    from precis.handlers.pcb import PcbHandler
    from precis.workers.executors._context import DispatchContext
    from precis.workers.job_types import pcb_place

    design = _channel_wall_design()
    # a fourth signal pair: demand 4 on the same 2-slot channel — no
    # order or seed can land a third net through it.
    design["components"] += [
        {
            "refdes": "A3",
            "footprint": "THW",
            "pins": [{"name": "1"}],
            "x": 5.0,
            "y": 6.25,
            "fixed": "both",
        },
        {
            "refdes": "B3",
            "footprint": "THW",
            "pins": [{"name": "1"}],
            "x": 15.0,
            "y": 6.25,
            "fixed": "both",
        },
    ]
    design["nets"].append({"name": "N3", "class": "signal", "current": 0.001})
    design["connections"] += [
        {"net": "N3", "refdes": "A3", "pin": "1"},
        {"net": "N3", "refdes": "B3", "pin": "1"},
    ]
    pcb = PcbHandler(hub=Hub(store=store))
    pcb.put(id="track-blocked", args=design)
    pcb.put(
        id="track-blocked",
        args={
            "op": "stackup",
            "layers": [
                {"name": "F.Cu", "role": "signal"},
                {"name": "B.Cu", "role": "signal"},
            ],
        },
    )
    ref = store.get_ref(kind="pcb", id="track-blocked")
    assert ref is not None
    ctx = _place_ctx(store, int(ref.id))
    pcb_place._dispatch(cast(DispatchContext, ctx), pcb_place.SPEC)
    assert not ctx.failures
    route = ctx.meta["topo_run"]["route"]
    assert route["failed"] > 0
    assert route["shove"] is not None and route["shove"]["reason"] == "all-fixed"
    assert route["shove_stops"], "a stopped recovery must say why"
    events = [t for k, t in ctx.summaries if k == "job_event"]
    assert any("shove stopped:" in t for t in events)


def _plugged_corridor_design() -> dict[str, Any]:
    """A keepout wall across x=10 leaves ONE gap (y in [10.4, 11.0]) —
    net N0's only corridor — and movable part M's 0.6 mm pad plugs it.
    The first route is stranded; the failure-driven shove pushes M
    clear and the re-route realises the net."""
    from tests._pcb_outline import closed_outline

    fp = {
        "name": "THW",
        "pads": [
            {
                "pin": "1",
                "shape": "rect",
                "x": 0.0,
                "y": 0.0,
                "w": 0.6,
                "h": 0.6,
                "drill": 0.2,
            }
        ],
    }
    return {
        "components": [
            {
                "refdes": "M",
                "footprint": "THW",
                "pins": [{"name": "1", "nc": True}],
                "x": 10.0,
                "y": 10.7,
            },
            {
                "refdes": "N0A",
                "footprint": "THW",
                "pins": [{"name": "1"}],
                "x": 5.0,
                "y": 10.7,
                "fixed": "both",
            },
            {
                "refdes": "N0B",
                "footprint": "THW",
                "pins": [{"name": "1"}],
                "x": 15.0,
                "y": 10.7,
                "fixed": "both",
            },
        ],
        "nets": [{"name": "N0", "class": "signal", "current": 0.001}],
        "connections": [
            {"net": "N0", "refdes": "N0A", "pin": "1"},
            {"net": "N0", "refdes": "N0B", "pin": "1"},
        ],
        "footprints": [fp],
        "features": [
            closed_outline(0.0, 0.0, 20.0, 20.44),
            {
                "ftype": "keepout",
                "geom": {
                    "poly": [
                        [9.8, 0.0],
                        [10.2, 0.0],
                        [10.2, 10.4],
                        [9.8, 10.4],
                    ]
                },
            },
            {
                "ftype": "keepout",
                "geom": {
                    "poly": [
                        [9.8, 11.0],
                        [10.2, 11.0],
                        [10.2, 20.44],
                        [9.8, 20.44],
                    ]
                },
            },
        ],
    }


def test_route_shove_recovers_a_blocked_corridor(store) -> None:
    """gr635502 review: the non-mocked successful recovery. A movable
    part's pad plugs the only corridor through a keepout wall, so the
    first route leaves N0 unreachable. The failure-driven shove must
    actually run — push rows off the plan's own failures, the part LP
    translating M clear — and the re-route at the shoved pose must land
    real copper: ``failed 0``, positive ``route_shove_mm`` and
    ``shove.moved_mm``, the pose committed (``shove_committed`` counts
    M), ``persisted`` route rows, and a ``view='drc'`` run with zero
    error findings (clearance AND connectivity)."""
    from precis.dispatch import Hub
    from precis.handlers.pcb import PcbHandler
    from precis.pcb import resolved as pcb_resolved
    from precis.pcb import session as pcb_session
    from precis.pcb import session_store as pcb_session_store
    from precis.workers.executors._context import DispatchContext
    from precis.workers.job_types import pcb_place

    pcb = PcbHandler(hub=Hub(store=store))
    pcb.put(id="shove-recover", args=_plugged_corridor_design())
    pcb.put(
        id="shove-recover",
        args={
            "op": "stackup",
            "layers": [
                {"name": "F.Cu", "role": "signal"},
                {"name": "B.Cu", "role": "signal"},
            ],
        },
    )
    ref = store.get_ref(kind="pcb", id="shove-recover")
    assert ref is not None
    ref_id = int(ref.id)

    # The same inputs _dispatch hands _topo_route, at the AUTHORED pose
    # — the test isolates the route stage; the placer is not under test
    # (it would re-seat the movable plug before route ever saw it).
    design = pcb_session_store.load_snapshot(store, ref_id)
    pcb_session.require_layout_ready(
        design.graph, design.features or [], board_id=ref_id
    )
    graph = pcb_resolved.apply(design.graph, None)
    features = design.features
    ir = pcb_session.build_ir(
        graph,
        outline=pcb_session.outline_from_features(features),
        mounting_holes=pcb_session.mounting_holes_from_features(features),
        footprints_by_lcsc=store.pcb_footprints_for(ref_id),
        local_footprints_by_name=store.pcb_local_footprints_for(ref_id),
    )
    board_id = (graph.get("board") or {}).get("board_id")
    capability = pcb_session.stackup_capability(
        ir.stackup, meta=(graph.get("board") or {}).get("meta")
    )
    footprints = pcb_session.footprints_by_refdes(
        ir,
        store.pcb_footprints_for(ref_id),
        local_footprints_by_name=store.pcb_local_footprints_for(ref_id),
        local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
    )
    ctx = _place_ctx(store, ref_id)
    _text, route = pcb_place._topo_route(
        cast(DispatchContext, ctx),
        ir,
        graph,
        footprints,
        features=features,
        fixed_copper=[],
        capability=capability,
        board_id=board_id,
        pcb_ref_id=ref_id,
    )
    assert not ctx.failures
    # the recovery ran, moved a real part, and landed the net
    assert route["failed"] == 0 and route["realized"] == 1
    shove = route["shove"]
    assert shove is not None
    assert shove["rounds"] >= 1 and shove["moved_mm"] > 0.0
    assert route["route_shove_rounds"] >= 1 and route["route_shove_mm"] > 0.0
    assert route["shove_committed"] >= 1, "M's pushed pose must persist"
    assert route["persisted"] is True and route["crossings"] == 0
    # the stored pose shows M off the gap it plugged
    loaded = store.pcb_load(ref_id)
    pose = {i["refdes"]: (i["x"], i["y"]) for i in loaded["instances"]}
    assert abs(pose["M"][0] - 10.0) + abs(pose["M"][1] - 10.7) > 0.3
    # zero error findings on the committed copper — no clearance or
    # connectivity violation was emitted to make the net fit
    drc_body = pcb.get(id="shove-recover", view="drc").body
    assert "\terror\t" not in drc_body
    assert "0 error(s)" in drc_body
