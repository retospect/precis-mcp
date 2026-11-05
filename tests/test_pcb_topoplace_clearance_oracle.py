"""Route obstacles contain true copper and retain each net's clearance rule."""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

from precis.pcb.ir import FixedVia, from_graph
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import realise, wires
from precis.pcb.topoplace.copper_index import CopperIndex
from precis.pcb.topoplace.design import TopoZone, _zones
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.realise import RoutePiece, RoutePlan, ViaSite
from precis.pcb.topoplace.wires import PlantedVia, WirePath
from tests.fixtures.pcb.topo_example_a import example_a
from tests.test_pcb_topoplace_realise import _guard_fixture, _sever_fixture


def _arc_row():
    return {
        "ctype": "track",
        "layer": "F.Cu",
        "net": "N0",
        "width_mm": 0.15,
        "segments": [
            {
                "shape": "arc",
                "start": [10.0, 0.0],
                "end": [0.0, 10.0],
                "center": [0.0, 0.0],
                "cw": False,
            }
        ],
    }


@pytest.mark.parametrize("kind", ["arc", "fixed-via"])
def test_authored_copper_rings_contain_the_true_copper(kind: str) -> None:
    """A 15-degree arc chord and a 12-gon facet both used to lose copper."""
    ir = from_graph({"instances": [], "nets": []})
    rows = []
    if kind == "arc":
        rows = [_arc_row()]
        theta = np.linspace(0.0, math.pi / 2.0, 2001)
        true_copper = LineString(
            np.column_stack([10 * np.cos(theta), 10 * np.sin(theta)])
        ).buffer(0.075, quad_segs=128)
    else:
        ir.fixed_vias = (FixedVia(x=3.0, y=2.0, dia_mm=0.45, net="N0"),)
        true_copper = Point(3.0, 2.0).buffer(0.225, quad_segs=1024)
    (zone,) = _zones(ir, (), rows, {"F.Cu": 0, "B.Cu": 1}, np.empty((0, 4)))
    polygon = Polygon(zone.ring)
    missing = true_copper.difference(polygon.buffer(1e-8)).area
    assert missing < 1e-8, f"{kind}: route obstacle omits {missing:g} mm2 of copper"


def test_guard_rejects_a_track_in_the_authored_arcs_missing_band() -> None:
    """The true edge gap is 0.06445 mm; old chords accepted the 0.15 rule."""
    d, ir = _guard_fixture()
    zone_ir = from_graph({"instances": [], "nets": []})
    d = dataclasses.replace(
        d,
        zones=_zones(zone_ir, (), [_arc_row()], {"F.Cu": 0}, d.pose0),
    )
    theta = math.radians(7.5)
    radial = np.asarray([math.cos(theta), math.sin(theta)])
    tangent = np.asarray([-math.sin(theta), math.cos(theta)])
    center = radial * (10.0 + 0.075 + 0.075 + 0.06445)
    points = tuple(tuple(p) for p in (center - 0.1 * tangent, center + 0.1 * tangent))
    plan = RoutePlan(
        pieces=(RoutePiece(net=1, branch=7, layer=0, points=points),),
        vias=(),
        layers=(0,),
    )
    assert realise._guarded_branches(plan, d, ir, 2, d.pose0, {}) == {1: {7}}


def _frozen_design(kind: str = "fixed_copper"):
    d, ir = _guard_fixture()
    ring = np.asarray([[9.0, 2.0], [10.0, 2.0], [10.0, 3.0], [9.0, 3.0]])
    d = dataclasses.replace(
        d, zones=(TopoZone(kind=kind, ring=ring, net="N0", layers=(0,)),)
    )
    rules = {
        "N0": NetRules(track_width_mm=0.15, clearance_mm=0.4),
        "N1": NetRules(track_width_mm=0.15, clearance_mm=0.15),
    }
    return d, ir, rules


@pytest.mark.parametrize("zone_kind", ["fixed_copper", "fixed_via"])
def test_frozen_body_retains_its_own_net_clearance(zone_kind: str) -> None:
    """The index must reject a 0.30 gap when the obstacle owes 0.40."""
    d, _ir, rules = _frozen_design(zone_kind)
    bodies = realise._zone_bodies(d, d.pose0, {"N0": 0, "N1": 1}, net_rules=rules)
    index = CopperIndex([body for body, _layers in bodies])
    assert index.blocked(Point(10.525, 2.5), 0.225, 0.15, net=1)
    assert not index.blocked(Point(10.525, 2.5), 0.225, 0.15, net=0)
    assert not index.blocked(Point(10.635, 2.5), 0.225, 0.15, net=1)


@pytest.mark.parametrize("kind", ["track", "via"])
@pytest.mark.parametrize("zone_kind", ["fixed_copper", "fixed_via"])
def test_guard_prices_frozen_copper_at_the_larger_net_rule(
    kind: str, zone_kind: str
) -> None:
    d, ir, rules = _frozen_design(zone_kind)
    piece = RoutePiece(net=1, branch=9, layer=0, points=((10.375, 2.2), (10.375, 2.8)))
    via = ViaSite(net=1, branch=9, edge=0, t=0.5, x=10.525, y=2.5, dia_mm=0.45)
    plan = RoutePlan(
        pieces=(piece,) if kind == "track" else (),
        vias=(via,) if kind == "via" else (),
        layers=(0,),
    )
    assert realise._guarded_branches(plan, d, ir, 2, d.pose0, rules) == {1: {9}}


def _occupancy():
    d = example_a()
    mesh = build_mesh(d, d.pose0)
    ctx = wires._make_ctx(mesh, d, 2, d.pose0)
    rules = tuple((0.15, 0.4 if k == 0 else 0.15) for k in range(len(d.net_pins)))
    corridor = wires._corridor(ctx, mesh, d, 2, rules, d.pose0)
    occupancy = wires._Occupancy(
        corridor, mesh, 2, d.pose0, net_clearance=tuple(c for _w, c in rules)
    )
    return d, mesh, ctx, occupancy


def _add_via(occupancy, x: float, y: float):
    branch = WirePath(
        net=0,
        pin_a=0,
        pin_b=1,
        tris=(),
        edges=(),
        t=(),
        points=(),
        vias=(
            PlantedVia(i=0, k=0, x=x, y=y, dia_mm=0.45, drill_mm=0.2, r_site_mm=0.225),
        ),
    )
    built = wires._Built(branch=branch, edges=())
    occupancy.add(0, built)
    return built


def test_stored_vias_retain_their_net_rule_in_the_replant_index() -> None:
    _d, _mesh, _ctx, occupancy = _occupancy()
    built = _add_via(occupancy, 10.0, 3.0)
    index = occupancy.dyn_index()
    assert index.blocked(Point(10.75, 3.0), 0.225, 0.15, net=1)
    assert index.blocked(Point(10.75, 3.0), 0.225, 0.15, net=0)
    assert not index.blocked(Point(10.86, 3.0), 0.225, 0.15, net=1)
    rebuilt = occupancy.rebuild([(0, built)])
    assert rebuilt.dyn_index().blocked(Point(10.75, 3.0), 0.225, 0.15, net=1)


def test_stored_vias_close_edges_at_the_larger_net_rule() -> None:
    _d, _mesh, ctx, occupancy = _occupancy()
    u, v = np.asarray(ctx.seg_u[0]), np.asarray(ctx.seg_v[0])
    direction = (v - u) / np.linalg.norm(v - u)
    normal = np.asarray([-direction[1], direction[0]])
    x, y = (u + v) / 2.0 + 0.6 * normal
    _add_via(occupancy, float(x), float(y))
    assert int(occupancy.view(1).closed[0]) == 3
    _d, _mesh, _ctx, clear = _occupancy()
    x, y = (u + v) / 2.0 + 0.71 * normal
    _add_via(clear, float(x), float(y))
    assert int(clear.view(1).closed[0]) == 0


def test_via_planters_frozen_index_keeps_the_obstacles_rule() -> None:
    d, _ir, rules = _frozen_design()
    mesh = build_mesh(d, d.pose0)
    ctx = wires._make_ctx(mesh, d, 2, d.pose0)
    planter = wires._ViaPlanter(ctx, d, d.pose0, net_clearance=(0.4, 0.15))
    assert planter.env_ix.blocked(
        Point(10.525, 2.5), 0.225, rules["N1"].clearance_mm, net=1
    )
    assert not planter.env_ix.blocked(Point(10.525, 2.5), 0.225, 0.15, net=0)


def test_unrouted_frozen_net_keeps_its_name_keyed_clearance() -> None:
    """A singleton or plane net's copper has a rule without a routed net index."""
    d, _ir, _rules = _frozen_design()
    d = dataclasses.replace(d, zones=(dataclasses.replace(d.zones[0], net="STATIC"),))
    assert "STATIC" not in d.net_name
    mesh = build_mesh(d, d.pose0)
    ctx = wires._make_ctx(mesh, d, 2, d.pose0)
    planter = wires._ViaPlanter(
        ctx,
        d,
        d.pose0,
        net_clearance=(0.15, 0.15),
        net_clearance_by_name={"STATIC": 0.4},
    )
    assert planter.env_ix.blocked(Point(10.525, 2.5), 0.225, 0.15, net=1)
    assert planter.env_ix.blocked(Point(10.525, 2.5), 0.225, 0.15, net=0)
    assert not planter.env_ix.blocked(Point(10.635, 2.5), 0.225, 0.15, net=1)
    corridor = wires._corridor(ctx, mesh, d, 2, ((0.15, 0.15),), d.pose0)
    occupancy = wires._Occupancy(
        corridor,
        mesh,
        2,
        d.pose0,
        net_clearance=(0.15, 0.15),
        net_clearance_by_name={"STATIC": 0.4},
    )
    assert occupancy.planter.env_ix.blocked(Point(10.525, 2.5), 0.225, 0.15, net=1)
    assert occupancy.rebuild([]).planter.env_ix.blocked(
        Point(10.525, 2.5), 0.225, 0.15, net=1
    )


def test_guard_checks_via_to_via_clearance_with_connecting_tracks() -> None:
    """Tracks point away: neither via-track nor track-track gates catch this pair."""
    d, ir = _guard_fixture()
    rules = {
        "N0": NetRules(track_width_mm=0.15, clearance_mm=0.4),
        "N1": NetRules(track_width_mm=0.15, clearance_mm=0.15),
    }
    plan = RoutePlan(
        pieces=(
            RoutePiece(net=0, branch=0, layer=0, points=((9.0, 3.0), (10.0, 3.0))),
            RoutePiece(net=1, branch=1, layer=0, points=((10.75, 3.0), (11.75, 3.0))),
        ),
        vias=(
            ViaSite(net=0, branch=0, edge=0, t=0.5, x=10.0, y=3.0, dia_mm=0.45),
            ViaSite(net=1, branch=1, edge=1, t=0.5, x=10.75, y=3.0, dia_mm=0.45),
        ),
        layers=(0, 0),
    )
    guarded = realise._guarded_branches(plan, d, ir, 2, d.pose0, rules)
    assert guarded, "0.30 mm barrel gap violates the first net's 0.40 mm rule"
    clear = dataclasses.replace(
        plan,
        pieces=(
            plan.pieces[0],
            dataclasses.replace(plan.pieces[1], points=((10.86, 3.0), (11.86, 3.0))),
        ),
        vias=(plan.vias[0], dataclasses.replace(plan.vias[1], x=10.86)),
    )
    assert not realise._guarded_branches(clear, d, ir, 2, d.pose0, rules)


def test_round_pad_obstacles_contain_the_true_disk() -> None:
    d, ir = _guard_fixture()
    d = dataclasses.replace(
        d, pin_wh=np.asarray([[2.0, 2.0]]), pin_shape=np.asarray(["circle"])
    )
    ((_name, polygon, _pin),) = realise._pad_polys(d, ir, 2, d.pose0)[0]
    missing = (
        Point(15.0, 0.0)
        .buffer(1.0, quad_segs=1024)
        .difference(polygon.buffer(1e-9))
        .area
    )
    assert missing < 1e-8, f"round-pad obstacle omits {missing:g} mm2 of copper"


def test_guard_rejects_track_in_round_pads_missing_facet_band() -> None:
    """The true gap is 0.1488 mm; the old 64-gon facet read it as >0.15."""
    d, ir = _guard_fixture()
    d = dataclasses.replace(
        d, pin_wh=np.asarray([[2.0, 2.0]]), pin_shape=np.asarray(["circle"])
    )
    theta = math.pi / 64.0
    radial = np.asarray([math.cos(theta), math.sin(theta)])
    tangent = np.asarray([-math.sin(theta), math.cos(theta)])
    center = np.asarray([15.0, 0.0]) + 1.2238 * radial
    points = tuple(tuple(p) for p in (center - 0.2 * tangent, center + 0.2 * tangent))
    plan = RoutePlan(
        pieces=(RoutePiece(net=1, branch=3, layer=0, points=points),),
        vias=(),
        layers=(0,),
    )
    assert realise._guarded_branches(plan, d, ir, 2, d.pose0, {}) == {1: {3}}


def test_conservative_round_pad_envelope_does_not_invent_a_copper_join() -> None:
    """The obstacle's outward sag cannot bridge a real 1.2 micrometre gap."""
    d, ir = _sever_fixture()
    d = dataclasses.replace(
        d,
        pin_wh=np.asarray([[1.0, 1.0], [1.0, 1.0]]),
        pin_shape=np.asarray(["circle", "circle"], dtype=object),
    )
    radial = np.asarray([math.cos(math.radians(7.5)), math.sin(math.radians(7.5))])
    pad = np.asarray([15.0, 0.0])
    tip = pad + 0.5762 * radial
    outer = tip + 2.0 * radial
    points = ((0.0, 0.0), (0.0, 3.0), tuple(outer), tuple(tip))
    true_gap = LineString(points).distance(Point(15.0, 0.0)) - 0.5 - 0.075
    assert true_gap == pytest.approx(0.0012, abs=1e-9)
    plan = RoutePlan(
        pieces=(RoutePiece(net=0, branch=0, layer=0, points=points),),
        vias=(),
        layers=(0,),
    )
    severed = realise._severed_nets(plan, d, ir, 2, d.pose0, {})
    assert set(severed) == {0}
    assert severed[0][0] == frozenset({0})
    touching_tip = tuple(pad + 0.575 * radial)
    touching = dataclasses.replace(
        plan,
        pieces=(
            dataclasses.replace(plan.pieces[0], points=(*points[:-1], touching_tip)),
        ),
    )
    assert realise._severed_nets(touching, d, ir, 2, d.pose0, {}) == {}
