"""Clearance survives reassignment, filtered net IDs and mesh pad reuse."""

from __future__ import annotations

import dataclasses
import math
from types import SimpleNamespace

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

from precis.pcb.clearance import FAB_QUANTUM_MM
from precis.pcb.ir import from_graph
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import design_from_ir, realise, wires
from precis.pcb.topoplace.copper_index import CopperIndex
from precis.pcb.topoplace.design import TopoZone
from precis.pcb.topoplace.mesh import build_mesh, world_pads
from precis.pcb.topoplace.wires import PlantedVia
from tests.fixtures.pcb.topo_example_a import example_a
from tests.test_pcb_topoplace_clearance_oracle import _frozen_design
from tests.test_pcb_topoplace_walls import _board


@pytest.mark.parametrize("obstacle", ["pad", "zone"])
def test_reassignment_planter_keeps_obstacle_rules(monkeypatch, obstacle) -> None:
    d, _ir, rules = _frozen_design()
    mesh = build_mesh(d, d.pose0)
    w = wires.build_wires(mesh, d, 2)
    init = wires._ViaPlanter.__init__
    checked = []

    def inspect(self, *args, **kwargs):
        init(self, *args, **kwargs)
        index = self.pad_ix if obstacle == "pad" else self.env_ix
        probe = Point(15.925, 0.0) if obstacle == "pad" else Point(10.525, 2.5)
        # 0.30 mm from the obstacle: incoming 0.15 cannot waive its 0.40.
        assert index.blocked(probe, 0.225, 0.15, net=1)
        checked.append(True)

    monkeypatch.setattr(wires._ViaPlanter, "__init__", inspect)
    realise.assign_layers(d, mesh, w, 2, net_rules=rules)
    assert checked


def test_reassignment_dynamic_vias_keep_obstacle_rules(monkeypatch) -> None:
    d = example_a()
    mesh = build_mesh(d, d.pose0)
    w = wires.build_wires(mesh, d, 2)
    first, second = w.branches[:2]
    first = dataclasses.replace(first, layers=(), vias=())
    second = dataclasses.replace(
        second,
        points=((20.0, 22.0), (21.0, 22.0)),
        vias=(PlantedVia(0, 0, 20.0, 20.0, 0.45, 0.2, 0.225),),
    )
    w = dataclasses.replace(w, branches=(first, second))
    # Force the legacy, unlayered branch through the cut legality check.
    monkeypatch.setattr(
        realise,
        "_corridor_masks",
        lambda *args: {first.net: np.zeros(len(mesh.edges), dtype=np.int32)},
    )
    rules = {
        name: NetRules(track_width_mm=0.15, clearance_mm=0.4) for name in d.net_name
    }
    checked = []

    def point(self, edge, radius, clearance, net, dyn, *, drill_mm):
        assert drill_mm == 0.3
        assert dyn.blocked(Point(20.75, 20.0), 0.225, 0.15, net=first.net)
        assert not dyn.blocked(Point(20.86, 20.0), 0.225, 0.15, net=first.net)
        checked.append(True)
        return None

    monkeypatch.setattr(wires._ViaPlanter, "point", point)
    realise.assign_layers(d, mesh, w, 2, net_rules=rules)
    assert checked


def test_reassignment_rethread_reuses_static_and_peer_via_rules(monkeypatch) -> None:
    d = example_a()
    d = dataclasses.replace(
        d,
        zones=(
            TopoZone(
                "fixed_copper",
                np.asarray([[8.0, 8.0], [9.0, 8.0], [9.0, 9.0], [8.0, 9.0]]),
                net="STATIC",
            ),
        ),
    )
    mesh = build_mesh(d, d.pose0)
    w = wires.build_wires(mesh, d, 2)
    branches = []
    for b in w.branches:
        branches.append(
            dataclasses.replace(
                b,
                points=tuple(
                    (
                        float(d.pose0[int(d.pin_part[p]), 0]),
                        float(d.pose0[int(d.pin_part[p]), 1]),
                    )
                    for p in (b.pin_a, b.pin_b)
                ),
                layers=(0,) * (len(b.edges) + 1),
                vias=(),
            )
        )
    # A peer's via is remote from its wire: only the barrel index can block it.
    branches[1] = dataclasses.replace(
        branches[1], vias=(PlantedVia(0, 0, 20.0, 20.0, 0.45, 0.2, 0.225),)
    )
    w = dataclasses.replace(w, branches=tuple(branches))
    rules = {
        name: NetRules(track_width_mm=0.15, clearance_mm=0.4)
        for name in (*d.net_name, "STATIC")
    }
    monkeypatch.setattr(wires._ViaPlanter, "point", lambda *args, **kwargs: None)
    monkeypatch.setattr(realise, "_runs_open", lambda *args: False)
    checked = []

    def regrow(*args, **kwargs):
        k = args[3]
        planter = kwargs.get("planter") or wires._ViaPlanter(args[0], d)
        assert planter.env_ix.blocked(Point(9.525, 8.5), 0.225, 0.15, net=k)
        if k != branches[1].net:
            view = kwargs.get("occ_view")
            dyn = view.dyn if view is not None else CopperIndex([])
            assert dyn.blocked(Point(20.75, 20.0), 0.225, 0.15, net=k)
            checked.append(True)
        return [], [], None

    monkeypatch.setattr(wires, "_net_tree", regrow)
    realise.assign_layers(d, mesh, w, 2, net_rules=rules)
    assert checked


def _filtered_design():
    groups = (("A_SINGLE", ["P0"]), ("GND", ["P1", "P2"]), ("S", ["P3", "P4"]))
    ir = from_graph(
        {
            "instances": [
                {"refdes": f"P{k}", "x": 5.0 + 5 * k, "y": 5.0} for k in range(5)
            ],
            "nets": [
                {
                    "name": name,
                    "net_class": "signal",
                    "members": [{"refdes": ref, "pin": "1"} for ref in refs],
                }
                for name, refs in groups
            ],
        }
    )
    ir.inst_x[:] = [5.0, 10.0, 15.0, 20.0, 25.0]
    ir.inst_y[:] = 5.0
    ir.pin_dx[:] = 0.0
    ir.pin_dy[:] = 0.0
    ir.pin_w[:] = 1.0
    ir.pin_h[:] = 1.0
    ir.outline = [(0.0, 0.0), (35.0, 0.0), (35.0, 15.0), (0.0, 15.0)]
    d = design_from_ir(ir, {}, route_planes=True)
    assert d.net_name == ("GND", "S")
    return d, ir


def test_filtered_pad_net_ids_preserve_identity_and_clearance() -> None:
    d, _ir = _filtered_design()
    mesh = build_mesh(d, d.pose0)
    ctx = wires._make_ctx(mesh, d, 2, d.pose0)
    planter = wires._ViaPlanter(
        ctx,
        d,
        net_clearance=(0.4, 0.15),
        net_clearance_by_name={"A_SINGLE": 0.5, "GND": 0.4, "S": 0.15},
    )
    assert planter.pad_ix.blocked(Point(11.025, 5.0), 0.225, 0.15, net=1)
    assert not planter.pad_ix.blocked(Point(11.025, 5.0), 0.225, 0.15, net=0)
    # The unrouted singleton is still a foreign pad with its own rule.
    assert planter.pad_ix.blocked(Point(6.025, 5.0), 0.225, 0.15, net=0)


def test_spoke_pad_ids_keep_filtered_net_rules(monkeypatch) -> None:
    d, _ir = _filtered_design()
    mesh = build_mesh(d, d.pose0)
    w = wires.build_wires(mesh, d, 2)
    captured = []

    def make_index(bodies):
        result = CopperIndex(bodies)
        captured.append(result)
        return result

    monkeypatch.setattr(realise, "CopperIndex", make_index)
    realise._spoke_offsets(
        d,
        mesh,
        w,
        2,
        net_rules={
            "GND": NetRules(track_width_mm=0.15, clearance_mm=0.4),
            "S": NetRules(track_width_mm=0.15, clearance_mm=0.15),
            "A_SINGLE": NetRules(track_width_mm=0.15, clearance_mm=0.5),
        },
    )
    assert len(captured) >= 2
    index = captured[1]  # the pad-only index used for free spoke intervals
    assert index.blocked(Point(11.025, 5.0), 0.225, 0.15, net=1)
    assert not index.blocked(Point(11.025, 5.0), 0.225, 0.15, net=0)
    assert index.blocked(Point(6.025, 5.0), 0.225, 0.15, net=0)


@pytest.mark.parametrize("grow", [0.0, 0.225])
def test_mesh_disc_clearance_body_contains_true_copper(grow) -> None:
    d = _board(
        [{"x": 10.0, "y": 10.0, "net": 0, "shape": "circle", "w": 2.0, "h": 2.0}],
        n_nets=1,
    )
    (pad,) = world_pads(d, d.pose0, grow_mm=grow)
    disk = Point(10.0, 10.0).buffer(1.0, quad_segs=1024)
    assert disk.difference(pad.poly.buffer(1e-9)).area < 1e-8
    theta = math.pi / 64.0
    p = Point(10.0 + 1.2238 * math.cos(theta), 10.0 + 1.2238 * math.sin(theta))
    # Exact gap 0.1488 mm for a 0.15-wide track.
    assert CopperIndex([realise.Body(pad.poly)]).blocked(p, 0.075, 0.15)


def test_unlisted_zone_rule_uses_the_same_default_in_every_index() -> None:
    d, _ir, _rules = _frozen_design()
    d = dataclasses.replace(d, zones=(dataclasses.replace(d.zones[0], net="STATIC"),))
    mesh = build_mesh(d, d.pose0)
    ctx = wires._make_ctx(mesh, d, 2)
    planter = wires._ViaPlanter(ctx, d)
    shaped = CopperIndex([b for b, _layers in realise._zone_bodies(d, d.pose0, {})])
    probe = Point(10.325, 2.5)  # barrel edge gap 0.10, below default 0.15
    assert shaped.blocked(probe, 0.225, 0.0, net=0)
    assert planter.env_ix.blocked(probe, 0.225, 0.0, net=0)
    assert not planter.env_ix.blocked(Point(10.385, 2.5), 0.225, 0.0, net=0)


@pytest.mark.parametrize(
    "kind", ["keepout", "mounting_hole", "fixed_copper", "fixed_via"]
)
def test_zone_kind_controls_ownership_and_clearance_in_both_indices(kind: str) -> None:
    d, _ir, rules = _frozen_design(kind)
    mesh = build_mesh(d, d.pose0)
    planter = wires._ViaPlanter(
        wires._make_ctx(mesh, d, 2),
        d,
        net_clearance=(0.4, 0.15),
        net_clearance_by_name={name: rule.clearance_mm for name, rule in rules.items()},
    )
    shaped = CopperIndex(
        [
            b
            for b, _layers in realise._zone_bodies(
                d, d.pose0, {"N0": 0, "N1": 1}, rules
            )
        ]
    )
    copper = kind in ("fixed_copper", "fixed_via")
    for index in (shaped, planter.env_ix):
        # Net names on geometric barriers grant neither exemption nor a rule.
        assert index.blocked(Point(9.5, 2.5), 0.225, 0.15, net=0) is not copper
        # Gap 0.30: only copper's 0.40 rule blocks a foreign 0.15 net.
        assert index.blocked(Point(10.525, 2.5), 0.225, 0.15, net=1) is copper


@pytest.mark.parametrize(
    "kind", ["keepout", "mounting_hole", "fixed_copper", "fixed_via"]
)
def test_named_zone_kind_controls_final_guard_exemption(kind: str) -> None:
    """A barrier's net label must not authorize copper through its interior."""
    d, ir, rules = _frozen_design(kind)
    piece = realise.RoutePiece(
        net=0, branch=0, layer=0, points=((8.0, 2.5), (11.0, 2.5))
    )
    plan = realise.RoutePlan(pieces=(piece,), vias=(), layers=(0,))
    expected = {} if kind in ("fixed_copper", "fixed_via") else {0: {0}}
    assert realise._guarded_branches(plan, d, ir, 2, d.pose0, rules) == expected


@pytest.mark.parametrize("kind", ["keepout", "mounting_hole"])
def test_named_barrier_route_detours_instead_of_claiming_ownership(kind: str) -> None:
    """The corridor must route around same-named geometric barriers."""
    barrier = TopoZone(
        kind,
        np.asarray([[18.0, 15.0], [22.0, 15.0], [22.0, 25.0], [18.0, 25.0]]),
        net="N0",
    )
    d = _board(
        [{"x": 10.0, "y": 20.0, "net": 0}, {"x": 30.0, "y": 20.0, "net": 0}],
        n_nets=1,
        zones=(barrier,),
        n_layers=2,
    )
    ir = SimpleNamespace(net_name=np.asarray(["N0"], dtype=object), n_layers=2)
    plan, result = realise._route_pass(
        d, ir, net_rules={"N0": NetRules(track_width_mm=0.15, clearance_mm=0.15)}
    )
    assert result.tracks, "the open board has room for a detour"
    assert not plan.guarded and not plan.severed
    for piece in plan.pieces:
        assert LineString(piece.points).distance(Polygon(barrier.ring)) >= 0.075


def test_emission_preserves_the_bend_validated_after_micro_coalescing() -> None:
    """A second coalesce consumes the export reserve after the final guard."""
    d, ir, _rules = _frozen_design()
    zone = TopoZone(
        "fixed_copper",
        np.asarray([[5.4, 5.2252], [5.6, 5.2252], [5.6, 6.2252], [5.4, 6.2252]])
        + np.array([0.0, FAB_QUANTUM_MM]),
        net="STATIC",
        layers=(0,),
    )
    d = dataclasses.replace(
        d,
        zones=(zone,),
        outline=np.asarray([[-5.0, -5.0], [25.0, -5.0], [25.0, 10.0], [-5.0, 10.0]]),
    )
    rules = {
        name: NetRules(track_width_mm=0.15, clearance_mm=0.15)
        for name in (*d.net_name, "STATIC")
    }
    piece = realise.RoutePiece(
        net=0,
        branch=0,
        layer=0,
        points=((5.0, 5.0), (6.0, 5.0), (6.0011, 5.0), (6.0005, 5.0005)),
    )
    plan = realise.RoutePlan(pieces=(piece,), vias=(), layers=(0,))
    shaped = realise._emit_shapes(plan, d, ir, 2, d.pose0, rules)
    assert not realise._guarded_branches(shaped, d, ir, 2, d.pose0, rules)
    validated = shaped.pieces[0].points
    assert validated == ((5.0, 5.0), (6.0, 5.0), (6.0005, 5.0005))
    assert LineString(validated).distance(Polygon(zone.ring)) - 0.075 == pytest.approx(
        0.1502 + FAB_QUANTUM_MM
    )
    shortcut = dataclasses.replace(
        shaped,
        pieces=(dataclasses.replace(piece, points=(validated[0], validated[-1])),),
    )
    assert realise._guarded_branches(shortcut, d, ir, 2, d.pose0, rules) == {0: {0}}
    (track,) = realise.realize_result(shaped, d, ir, 2, net_rules=rules).tracks
    emitted = (tuple(track.segments[0]["start"]),) + tuple(
        tuple(segment["end"]) for segment in track.segments
    )
    gap = LineString(emitted).distance(Polygon(zone.ring)) - track.width_mm / 2.0
    assert gap >= 0.15 + FAB_QUANTUM_MM, (
        f"emitted gap {gap:.7f} mm loses the export reserve"
    )
    assert emitted == validated
    assert track.length_mm == pytest.approx(LineString(validated).length)
    rechecked = dataclasses.replace(
        shaped, pieces=(dataclasses.replace(piece, points=emitted),)
    )
    assert not realise._guarded_branches(rechecked, d, ir, 2, d.pose0, rules)


def test_owner_route_reaches_authored_via_inside_grown_zone() -> None:
    # A copper corridor between full-height keepouts forces N0 across
    # its authored via; the foreign-net control must remain blocked.
    via = TopoZone(
        "fixed_via",
        np.asarray([[19.7, 19.7], [20.3, 19.7], [20.3, 20.3], [19.7, 20.3]]),
        net="N0",
    )
    barriers = tuple(
        TopoZone("keepout", np.asarray(ring))
        for ring in (
            [[0, 0], [40, 0], [40, 19.6], [0, 19.6]],
            [[0, 20.4], [40, 20.4], [40, 40], [0, 40]],
        )
    )
    d = _board(
        [{"x": 10.0, "y": 20.0, "net": 0}, {"x": 30.0, "y": 20.0, "net": 0}],
        n_nets=1,
        zones=(via, *barriers),
        n_layers=2,
    )
    mesh = build_mesh(d, d.pose0, trace_mm=0.15, clearance_mm=0.15)
    w = wires.build_wires(mesh, d, 2, ((0.15, 0.15),))
    assert not w.unreachable
    plan = realise.plan_route(d, mesh, w, 2)
    assert not plan.no_via and not plan.stranded
    assert any(
        LineString(p.points).buffer(0.075).intersects(Polygon(via.ring))
        for p in plan.pieces
    )
    foreign = dataclasses.replace(
        d, zones=(dataclasses.replace(via, net="OTHER"), *barriers)
    )
    other_mesh = build_mesh(foreign, foreign.pose0, trace_mm=0.15, clearance_mm=0.15)
    other = wires.build_wires(other_mesh, foreign, 2, ((0.15, 0.15),))
    assert other.unreachable


@pytest.mark.parametrize("subdivide", [False, True])
def test_collinear_spoke_vertices_do_not_pin_a_clearance_violation(subdivide) -> None:
    start, pivot, end = (5.0, 5.0), (6.0, 5.0), (7.0, 9.0)
    inserted = (
        tuple(
            (pivot[0] + t * (end[0] - pivot[0]), pivot[1] + t * (end[1] - pivot[1]))
            for t in (0.125, 0.2, 0.25, 0.3)
        )
        if subdivide
        else ()
    )
    path = (start, pivot, *inserted, end)
    # Both ends of the peer are fixed inside the original trace's gap:
    # repairing the peer cannot rescue a trace pinned by redundant points.
    peer = ((6.0, 5.375), (6.0, 6.375))
    assert LineString(path).hausdorff_distance(LineString((start, pivot, end))) < 1e-9
    d = _board(
        [
            {"x": x, "y": y, "net": net, "w": 0.0, "h": 0.0}
            for net, ends in ((0, (start, end)), (1, (peer[0], peer[-1])))
            for x, y in ends
        ],
        n_nets=2,
    )
    ir = SimpleNamespace(net_name=np.asarray(d.net_name))
    plan = realise.RoutePlan(
        pieces=(
            realise.RoutePiece(net=0, branch=0, layer=0, points=path),
            realise.RoutePiece(net=1, branch=1, layer=0, points=peer),
        ),
        vias=(),
        layers=(0, 0),
    )
    shaped = realise._emit_shapes(plan, d, ir, 2, d.pose0, {})
    assert (
        LineString(shaped.pieces[0].points).distance(
            LineString(shaped.pieces[1].points)
        )
        >= 0.3 - 1e-6
    )
    assert realise._guarded_nets(shaped, d, ir, 2, d.pose0, {}) == set()
