"""Synthetic plane-fanout regressions: every emitted primitive is copper."""

from __future__ import annotations

import itertools
import math
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

from precis.pcb.clearance import outward_circle
from precis.pcb.gerber import DEFAULT_SILK_WIDTH_MM
from precis.pcb.realize import RealizedTrack, RealizedVia, RealizeResult
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import realise, wires
from precis.pcb.topoplace.design import SilkRoom, TopoDesign, TopoZone
from tests.test_pcb_topo_plane_fanout import _escape_case, _escape_mesh


def _case(
    monkeypatch: pytest.MonkeyPatch,
    positions: tuple[tuple[float, float], ...],
    *,
    directions: tuple[tuple[float, float], ...] | None = None,
    names: tuple[str, ...] = ("GND", "S"),
    zones: tuple[TopoZone, ...] = (),
) -> tuple[TopoDesign, Any]:
    """One tiny pad per net; first-choice sites lie 1.3 mm outward.

    Empty body hulls make silk irrelevant. Restrict the ladder, but leave
    all four directions so the placer may recover from a refused site.
    """
    monkeypatch.setattr(realise, "_FANOUT_RADII_MM", (1.3,))
    monkeypatch.setattr(realise, "_FANOUT_LADDER_CAP_MM", 0.0)
    monkeypatch.setattr(realise, "_FANOUT_DIRECTIONS", 4)
    count = len(positions)
    offsets = np.asarray(directions or ((0.1, 0.0),) * count)
    pose = np.zeros((count, 4))
    pose[:, :2] = np.asarray(positions) - offsets
    silk = replace(
        SilkRoom.fallback(),
        outline_reach_mm=1.3 - 0.225 - DEFAULT_SILK_WIDTH_MM / 2.0 - 0.05,
        via_to_silk_mm=0.0,
    )
    td = TopoDesign(
        parts=tuple(f"P{i}" for i in range(count)),
        pin_xy=offsets,
        pin_part=np.arange(count),
        pin_side=np.zeros(count, np.int8),
        pin_label=np.asarray(["1"] * count, object),
        pin_net=np.arange(count),
        net_pins=tuple(np.asarray([i]) for i in range(count)),
        flex=(),
        swap_groups=(),
        fixed=np.ones(count, bool),
        freedoms=np.zeros(count, np.uint8),
        outline=np.asarray([(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]),
        pose0=pose,
        pin_wh=np.full((count, 2), 0.1),
        net_name=names,
        zones=zones,
        silk=silk,
        n_layers=4,
    )
    return td, SimpleNamespace(
        net_name=names,
        n_nets=len(names),
        n_layers=4,
        stackup=[{"name": name} for name in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")],
        net_plane_layers=np.ones(len(names), np.int32),
    )


def _line(track: Any) -> LineString:
    return LineString(
        [tuple(segment["start"]) for segment in track.segments]
        + [tuple(track.segments[-1]["end"])]
    )


def test_fanout_stubs_keep_clearance_from_earlier_foreign_stubs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legal barrels cannot excuse crossing foreign-net 0.15 mm stubs.

    The original first choices are (10,10)->(11.3,10) and
    (10.65,10.6)->(10.65,9.3): barrel edge gap 0.505 mm, stub gap -0.15.
    """
    td, ir = _case(
        monkeypatch,
        ((10.0, 10.0), (10.65, 10.6)),
        directions=((0.1, 0.0), (0.0, -0.1)),
    )
    rules = {name: NetRules(0.15, 0.15, 0.45, 0.2) for name in td.net_name}
    tracks, vias, _fallback, deferred = realise._fanout_vias(
        td, ir, 4, td.pose0, rules, {0: 1, 1: 2}
    )
    assert tracks and tracks[0].net_id == 0
    assert len(tracks) + len(deferred) == 2
    assert len(vias) == len(tracks)
    for i, track in enumerate(tracks):
        for prior in tracks[:i]:
            if track.layer == prior.layer and track.net_id != prior.net_id:
                gap = (
                    _line(track).distance(_line(prior))
                    - track.width_mm / 2.0
                    - prior.width_mm / 2.0
                )
                assert gap >= 0.15 - 1e-9, f"foreign fanout stub gap {gap:.6f} mm"


@pytest.mark.parametrize("primitive", ["barrel", "stub"])
def test_fanout_keeps_the_frozen_net_clearance(
    monkeypatch: pytest.MonkeyPatch, primitive: str
) -> None:
    """GND's 0.15 mm rule cannot override frozen S copper's 0.40 mm.

    The east first choice puts its barrel 0.17 mm off the vertical zone,
    or its stub 0.245 mm off the short horizontal zone.
    """
    ring = (
        [(11.695, 9.8), (11.795, 9.8), (11.795, 10.2), (11.695, 10.2)]
        if primitive == "barrel"
        else [(10.5, 10.32), (10.7, 10.32), (10.7, 10.42), (10.5, 10.42)]
    )
    frozen = Polygon(ring)
    td, ir = _case(
        monkeypatch,
        ((10.0, 10.0),),
        zones=(TopoZone("fixed_copper", np.asarray(ring), (0,), "S"),),
    )
    rules = {
        "GND": NetRules(0.15, 0.15, 0.45, 0.2),
        "S": NetRules(0.15, 0.4, 0.45, 0.2),
    }
    tracks, vias, _fallback, deferred = realise._fanout_vias(
        td, ir, 4, td.pose0, rules, {0: 1}
    )
    assert len(tracks) + len(deferred) == 1
    assert tracks, "the unrestricted directions leave a legal fanout site"
    for via in vias:
        gap = frozen.distance(Point(via.x, via.y)) - via.dia_mm / 2.0
        assert gap >= 0.4 - 1e-9, f"barrel vs frozen S: gap {gap:.6f} mm"
    for track in tracks:
        gap = frozen.distance(_line(track)) - track.width_mm / 2.0
        assert gap >= 0.4 - 1e-9, f"stub vs frozen S: gap {gap:.6f} mm"


def test_fanout_barrels_keep_the_earlier_net_clearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prior 0.40 mm rule still applies when the next net asks 0.15 mm.

    The parallel first choices leave 0.27 mm between barrel edges;
    their stubs already have the required 0.40 mm clearance.
    """
    td, ir = _case(
        monkeypatch,
        ((10.0, 10.0), (10.0, 10.72)),
        names=("S", "GND"),
    )
    rules = {
        "S": NetRules(0.15, 0.4, 0.45, 0.2),
        "GND": NetRules(0.15, 0.15, 0.45, 0.2),
    }
    tracks, vias, _fallback, deferred = realise._fanout_vias(
        td, ir, 4, td.pose0, rules, {0: 1, 1: 2}
    )
    assert tracks and vias[0].net_id == 0
    assert len(vias) + len(deferred) == 2
    for i, via in enumerate(vias):
        for prior in vias[:i]:
            gap = (
                math.hypot(via.x - prior.x, via.y - prior.y)
                - via.dia_mm / 2.0
                - prior.dia_mm / 2.0
            )
            assert gap >= 0.4 - 1e-9, f"foreign fanout barrel gap {gap:.6f} mm"


@pytest.mark.parametrize("clearance", [0.15, 0.4])
def test_fanout_drills_keep_clearance_from_authored_same_net_barrels(
    monkeypatch: pytest.MonkeyPatch, clearance: float
) -> None:
    """Joining GND copper cannot exempt a new drill from an existing barrel.

    The first candidate leaves only 0.10 mm between 0.45 mm barrel
    edges, so even a non-overlap check would wrongly admit it.
    """
    authored_x, authored_y, authored_radius = 11.3, 10.55, 0.225
    ring = outward_circle(authored_x, authored_y, authored_radius)
    td, ir = _case(
        monkeypatch,
        ((10.0, 10.0),),
        zones=(TopoZone("fixed_via", np.asarray(ring.exterior.coords), (), "GND"),),
    )
    tracks, vias, _fallback, deferred = realise._fanout_vias(
        td,
        ir,
        4,
        td.pose0,
        {"GND": NetRules(0.15, clearance, 0.45, 0.2)},
        {0: 1},
    )
    assert deferred == []
    assert len(tracks) == len(vias) == 1
    gap = (
        math.hypot(vias[0].x - authored_x, vias[0].y - authored_y)
        - vias[0].dia_mm / 2.0
        - authored_radius
    )
    assert gap >= clearance - 1e-9, f"new drill vs authored GND barrel: {gap:.6f} mm"


def test_a_circle_pad_outward_facet_does_not_join_a_pour_piece(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sliver in clearance polygon excess still floats off real copper."""
    td, ir = _case(monkeypatch, ((10.0, 10.0),), names=("GND",))
    td = replace(
        td,
        pin_wh=np.asarray([[2.0, 2.0]]),
        pin_shape=np.asarray(["circle"], object),
    )
    outside = realise._pad_polys(td, ir, 4, td.pose0)[0][0][1]
    vx, vy = max(
        outside.exterior.coords,
        key=lambda p: math.hypot(p[0] - 10.0, p[1] - 10.0),
    )
    extent = math.hypot(vx - 10.0, vy - 10.0)
    excess = extent - 1.0
    assert excess > 1e-6
    radius = 1.0 + excess / 2.0
    sliver = Point(
        10.0 + radius * (vx - 10.0) / extent,
        10.0 + radius * (vy - 10.0) / extent,
    ).buffer(excess / 8.0)
    assert sliver.intersects(outside)
    assert sliver.distance(Point(10.0, 10.0)) > 1.0
    pieces = realise._joined_plane_pieces(
        td,
        ir,
        {0: 0},
        [],
        [],
        td.pose0,
        {},
        {},
        [],
        0.0,
        pours=[
            {
                "net": "GND",
                "layer": "F.Cu",
                "polygon": list(sliver.exterior.coords),
            }
        ],
    )
    assert pieces == {0: []}


def _track(net: int, layer: int, points: list[tuple[float, float]]) -> RealizedTrack:
    return RealizedTrack(
        seg_id=-1,
        net_id=net,
        layer=layer,
        segments=tuple(
            {"shape": "line", "start": list(a), "end": list(b)}
            for a, b in itertools.pairwise(points)
        ),
        length_mm=sum(math.dist(a, b) for a, b in itertools.pairwise(points)),
        blocked_by=None,
        width_mm=0.15,
    )


@pytest.mark.parametrize(
    ("foreign", "layer", "expected"),
    [(True, 0, {0}), (True, 3, set()), (False, 0, set())],
    ids=["foreign-same-layer", "foreign-other-layer", "same-net"],
)
def test_final_fanout_guard_checks_the_complete_signal_copper(
    foreign: bool, layer: int, expected: set[int]
) -> None:
    """Signal ordering cannot exempt a crossing, and own copper can join."""
    td, ir = _escape_case()
    gnd, signal = list(ir.net_name).index("GND"), list(ir.net_name).index("S")
    fanout = _track(gnd, 0, [(5.0, 10.0), (8.0, 10.0)])
    via = RealizedVia(-1, gnd, 8.0, 10.0, 0.45, 0.2, 0, 3, "a")
    crossed = _track(signal if foreign else gnd, layer, [(6.5, 9.0), (6.5, 11.0)])
    rules = {name: NetRules(0.15, 0.15, 0.45, 0.2) for name in td.net_name}
    bad = realise._guard_fanouts(
        td,
        ir,
        td.pose0,
        rules,
        RealizeResult((crossed,), (), ()),
        [fanout],
        [via],
    )
    assert bad == expected


def test_final_fanout_guard_checks_barrels_against_other_layer_larger_rules() -> None:
    """The F.Cu stub is clear; its through barrel crowds B.Cu signal S."""
    td, ir = _escape_case()
    gnd, signal = list(ir.net_name).index("GND"), list(ir.net_name).index("S")
    fanout = _track(gnd, 0, [(5.0, 10.0), (8.0, 10.0)])
    via = RealizedVia(-1, gnd, 8.0, 10.0, 0.45, 0.2, 0, 3, "a")
    # Edge gap .32 mm clears GND's .15 rule but violates S's .40 rule.
    signal_track = _track(signal, 3, [(8.62, 9.8), (8.62, 10.2)])
    rules = {
        "GND": NetRules(0.15, 0.15, 0.45, 0.2),
        "S": NetRules(0.15, 0.4, 0.45, 0.2),
    }
    assert realise._guard_fanouts(
        td,
        ir,
        td.pose0,
        rules,
        RealizeResult((signal_track,), (), ()),
        [fanout],
        [via],
    ) == {0}


@pytest.mark.parametrize("zone_layer", [0, 3], ids=["same-layer", "other-layer"])
def test_escape_whole_polyline_oracle_checks_authored_frozen_copper(
    monkeypatch: pytest.MonkeyPatch, zone_layer: int
) -> None:
    """A bad search proposal must pass a separate check of every segment.

    The proposed bent path crosses S's strip on its second segment;
    its endpoint and the direct pad-to-via chord both miss the strip.
    The same strip on B.Cu is clear of the barrel and may sit below it.
    """
    td, ir = _escape_case()
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    td = replace(
        td,
        zones=(
            TopoZone(
                "fixed_copper",
                np.asarray([(6.5, 11.9), (6.6, 11.9), (6.6, 12.1), (6.5, 12.1)]),
                (zone_layer,),
                "S",
            ),
        ),
    )
    mesh = _escape_mesh(td, [[5.0, 10.0], [8.0, 6.0], [8.0, 14.0], [11.0, 10.0]])
    proposed = [(5.0, 10.0), (6.0, 12.0), (8.0, 12.0)]
    verdicts: list[bool] = []

    def propose(*_args: Any, verify: Any, **_kwargs: Any) -> Any:
        accepted = bool(verify(proposed, (8.0, 12.0)))
        verdicts.append(accepted)
        return (proposed, (8.0, 12.0), False) if accepted else None

    monkeypatch.setattr(wires, "fanout_escape", propose)
    tracks, vias, missing, done, _need, reasons, _spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        {
            "GND": NetRules(0.15, 0.15, 0.45, 0.2),
            "S": NetRules(0.15, 0.4, 0.45, 0.2),
        },
        RealizeResult((), (), ()),
        int(ir.n_layers),
        mesh=mesh,
    )
    assert verdicts == [zone_layer != 0]
    if zone_layer == 0:
        assert tracks == vias == done == []
        assert missing == [(k, pin)]
        assert reasons[(k, pin)] == "no_site"
    else:
        assert missing == []
        assert done == [(k, pin)]
        assert len(vias) == len(tracks) == 1
        assert list(_line(tracks[0]).coords) == proposed


def test_route_design_defers_a_fanout_rejected_by_the_final_copper_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route boundary drops a shorting fanout pair before escape emission.

    Signal routing returns a legal S track crossing the proposed GND
    stub. The real final guard must remove that stub and its barrel,
    send its pad to the escape pass and retain the signal unchanged.
    """
    td, ir = _escape_case()
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    gnd, signal_id = list(ir.net_name).index("GND"), list(ir.net_name).index("S")
    fanout = _track(gnd, 0, [(5.0, 10.0), (8.0, 10.0)])
    via = RealizedVia(-1, gnd, 8.0, 10.0, 0.45, 0.2, 0, 3, "a")
    signal = _track(signal_id, 0, [(6.5, 9.0), (6.5, 11.0)])
    received: list[list[tuple[int, int]]] = []
    escape_copper: list[Any] = []

    def fanout_proposal(*_args: Any, **_kwargs: Any) -> Any:
        return [fanout], [via], [(k, pin)], []

    def route_signal(*_args: Any, **_kwargs: Any) -> Any:
        return realise.RoutePlan((), (), ()), RealizeResult((signal,), (), ())

    def missing_escape(
        _design: Any,
        _ir: Any,
        deferred: Any,
        _pose: Any,
        _rules: Any,
        copper: Any,
        *_args: Any,
        **_kwargs: Any,
    ) -> Any:
        received.append(list(deferred))
        escape_copper.append(copper)
        return [], [], list(deferred), [], {}, {kp: "no_site" for kp in deferred}, {}

    monkeypatch.setattr(realise, "_fanout_vias", fanout_proposal)
    monkeypatch.setattr(realise, "_route_signals", route_signal)
    monkeypatch.setattr(realise, "_emit_escapes", missing_escape)
    plan, result = realise.route_design(
        td,
        ir,
        net_rules={name: NetRules(0.15, 0.15, 0.45, 0.2) for name in td.net_name},
    )
    assert received == [[(k, pin)]]
    assert plan.fanout_missing == ((k, pin),)
    assert plan.fanout_missing_reason == {(k, pin): "no_site"}
    assert plan.fanout_fallback == ()
    assert escape_copper[0].tracks == result.tracks == (signal,)
    assert escape_copper[0].vias == result.vias == ()
