"""Review regressions: legal collective moves and export-safe placement."""

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from precis.pcb.clearance import FAB_QUANTUM_MM
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import realise
from precis.pcb.topoplace.design import TopoDesign, TopoZone
from precis.pcb.topoplace.legalize import legalize_pose
from precis.pcb.topoplace.mesh import TopoMesh
from precis.pcb.topoplace.realise import RoutePiece, RoutePlan
from precis.pcb.topoplace.shove import shove_pose
from precis.pcb.topoplace.wires import WirePath, Wires
from tests.test_pcb_topo_fanout_clearance import _case
from tests.test_pcb_topoplace_legalize import _design


def _empty_design(count: int) -> TopoDesign:
    return TopoDesign(
        parts=(),
        pin_xy=np.empty((0, 2)),
        pin_part=np.asarray([], dtype=np.int64),
        pin_side=np.asarray([], dtype=np.int8),
        pin_label=np.asarray([], dtype=object),
        pin_net=np.asarray([], dtype=np.int32),
        net_pins=(np.asarray([], dtype=np.int64),) * count,
        net_name=tuple(f"N{i}" for i in range(count)),
        flex=(),
        swap_groups=(),
        fixed=np.asarray([], dtype=bool),
        freedoms=np.asarray([], dtype=np.uint8),
        outline=np.empty((0, 2)),
        pose0=np.empty((0, 4)),
    )


def test_fanout_broadphase_includes_missing_nets_default(monkeypatch) -> None:
    d, ir = _case(monkeypatch, ((5.0, 5.0),), names=("N0",))
    monkeypatch.setattr(realise, "_FANOUT_DIRECTIONS", 1)
    monkeypatch.setattr(realise, "_FANOUT_RADII_MM", (1.5,))
    monkeypatch.setattr(
        realise,
        "_pad_polys",
        lambda *_: {
            0: [
                ("N0", box(4.975, 4.975, 5.025, 5.025), 0),
                ("N1", box(5.975, 5.175, 6.025, 5.225), 1),
            ],
        },
    )
    tracks, vias, _, deferred = realise._fanout_vias(
        d, ir, 4, d.pose0, {"N0": NetRules(0.15, 0.01, 0.45, 0.2)}, {0: 1}
    )
    assert not tracks and not vias
    assert deferred == [(0, 0)]


def test_collective_bend_fan_keeps_all_four_branches(monkeypatch) -> None:
    d = _empty_design(4)
    mesh = TopoMesh(
        verts=np.asarray([[120.0, 20.0], [120.0, 0.0]]),
        tris=np.empty((0, 3), dtype=np.int32),
        edges=np.asarray([[0, 1]], dtype=np.int32),
        tri_edges=np.empty((0, 3), dtype=np.int32),
        edge_tris=np.asarray([[-1, -1]], dtype=np.int32),
        constrained=np.asarray([False]),
        pad_vertex={},
    )
    branches = tuple(
        WirePath(
            net=i,
            pin_a=-1,
            pin_b=-1,
            tris=(),
            edges=(),
            t=(),
            points=((20.0 - i, 120.0), (120.0, 20.0), (220.0 + i, 120.0)),
        )
        for i in range(4)
    )
    wires = Wires(
        branches=branches,
        order=((0, 1, 2, 3),),
        capacity_mm=np.asarray([10.0]),
        net_layers=(frozenset({0}),) * 4,
        unreachable=(),
        gap_free_mm=np.empty((0, 2)),
        gap_cap=np.empty((0, 2), dtype=np.int64),
    )
    monkeypatch.setattr(realise, "obstacle_vertices", lambda *_: frozenset())
    rules = {name: NetRules(0.15, 0.15) for name in d.net_name}
    paths = realise._spoke_offsets(d, mesh, wires, 2, net_rules=rules)
    plan = RoutePlan(
        pieces=tuple(RoutePiece(i, i, 0, p) for i, p in enumerate(paths)),
        vias=(),
        layers=(),
    )
    ir = SimpleNamespace(net_name=d.net_name)
    shaped = realise._emit_shapes(plan, d, ir, 2, d.pose0, rules)
    assert realise._guarded_branches(shaped, d, ir, 2, d.pose0, rules) == {}
    for pc in shaped.pieces:
        assert pc.points[0] == branches[pc.branch].points[0]
        assert pc.points[-1] == branches[pc.branch].points[-1]
    for i, pc in enumerate(shaped.pieces):
        for peer in shaped.pieces[i + 1 :]:
            assert (
                LineString(pc.points).distance(LineString(peer.points))
                >= 0.3 + FAB_QUANTUM_MM - 1e-9
            )


def test_midpoint_repair_remains_an_alternative() -> None:
    d = replace(
        _empty_design(2),
        outline=np.asarray(
            [[0, 1], [10.9, 1], [10.9, 1.769775], [12, 1.769775], [12, 3.5], [0, 3.5]]
        ),
    )
    plan = RoutePlan(
        pieces=(
            RoutePiece(0, 0, 0, ((1.0, 2.0), (11.0, 2.0))),
            RoutePiece(1, 1, 0, ((10.974, 2.3002), (10.974, 2.4002))),
        ),
        vias=(),
        layers=(),
    )
    ir = SimpleNamespace(net_name=d.net_name)
    rules = {name: NetRules(0.15, 0.15) for name in d.net_name}
    shaped = realise._emit_shapes(plan, d, ir, 2, d.pose0, rules)
    assert realise._guarded_branches(shaped, d, ir, 2, d.pose0, rules) == {}
    assert len(shaped.pieces[0].points) > 2


@pytest.mark.parametrize("solver", [legalize_pose, shove_pose])
def test_placement_reserves_export_quantum_at_hole(solver) -> None:
    from precis.pcb.optimize import _hole_polygon

    x, y, radius = 5.8006, 5.8006, 0.424999873978
    ring = np.asarray(
        [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5], [-0.5, -0.5]]
    )
    d = _design(
        parts=("U1",),
        pin_part=[],
        pin_net=[],
        part_outline=(ring,),
        zones=(
            TopoZone(
                kind="mounting_hole",
                ring=np.asarray(_hole_polygon(x, y, radius)),
                circle=(x, y, radius),
            ),
        ),
        pose0=[[5.000079, 5.000079, 0, 0]],
    )
    d = replace(d, part_body=(ring,))
    result = solver(d, d.pose0)
    assert result.resolved
    body = Polygon(ring + result.pose[0, :2])
    assert body.distance(Point(x, y)) >= radius + FAB_QUANTUM_MM - 1e-9
    # The fab model rounds the pad centre to four decimal places.
    exported = Polygon(ring + np.round(result.pose[0, :2], 4))
    assert exported.distance(Point(x, y)) >= radius


@pytest.mark.parametrize("solver", [legalize_pose, shove_pose])
@pytest.mark.parametrize("barrier", ["keepout", "pair", "outline"])
@pytest.mark.parametrize("gap_mm,accepted", [(0.0001, False), (0.0005, True)])
def test_placement_audit_reserves_quantum_at_other_boundaries(
    solver, barrier, gap_mm, accepted
) -> None:
    """A final solver audit must report a reserve shortfall without moving it."""
    ring = np.asarray(
        [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5], [-0.5, -0.5]]
    )
    d = _design(
        parts=("U1",),
        pin_part=[],
        pin_net=[],
        part_outline=(ring,),
        pose0=[[5, 5, 0, 0]],
    )
    if barrier == "keepout":
        d = replace(
            d,
            zones=(
                TopoZone(
                    kind="keepout",
                    ring=np.asarray(box(5.5 + gap_mm, 4, 7, 6).exterior.coords),
                ),
            ),
        )
    elif barrier == "outline":
        d = replace(
            d,
            outline=np.asarray(
                [[4.5 - gap_mm, 0], [20, 0], [20, 20], [4.5 - gap_mm, 20]]
            ),
        )
    else:
        d = _design(
            parts=("U1", "U2"),
            pin_part=[],
            pin_net=[],
            part_outline=(ring, ring),
            pose0=[[5, 5, 0, 0], [6 + d.silk.pair_req_mm + gap_mm, 5, 0, 0]],
        )
    audited = solver(d, d.pose0, rounds=0)
    np.testing.assert_array_equal(audited.pose, d.pose0)
    assert audited.resolved is accepted
