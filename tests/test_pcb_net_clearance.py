"""Per-net clearance in the maze grid (slice 9 of
docs/backlog/pcb-lazy-netlist-and-checks.md).

The grid used to take ONE clearance, the max over every net's class, so a
single 0.4 mm HV class inflated every net to 0.4 mm. Now the grid keeps
the smallest as its base and a wider net carries its difference on both
its claims and its queries (``maze.py``, "Per-net clearance")."""

from __future__ import annotations

import math
from typing import Any

import pytest
from shapely.geometry import LineString, Point

from precis.pcb import DEFAULT_STACKUP, maze
from precis.pcb.ir import from_graph
from precis.pcb.maze import GridSpec, OccupancyGrid
from precis.pcb.realize import RealizeConfig, _route_context, realize

_BASE = 0.1
_HV = 0.4
_W = 0.1
_OBSTACLE_R = 0.2


def _detour_gap(*, obstacle_net: int, router_net: int) -> float:
    """Route ``router_net`` straight across a disk of ``obstacle_net``
    copper (net 7 is the HV class) and return the closest edge-to-edge
    gap the path keeps."""
    spec = GridSpec(0.0, 0.0, 0.05, 120, 120, 1)
    grid = OccupancyGrid(spec, clearance_mm=_BASE, net_clearance_mm={7: _HV})
    radius = grid.core_radius_mm(2 * _OBSTACLE_R)
    grid.stamp_disk((0,), 3.0, 3.0, radius, obstacle_net)
    path = grid.route(router_net, (0.5, 3.0), (5.5, 3.0), layers=[0], width_mm=_W)
    assert path is not None
    centre = min(
        math.hypot(x - 3.0, y - 3.0)
        for x, y, _lo, _hi in maze.path_samples(path, spec.pitch / 4)
    )
    return centre - _OBSTACLE_R - _W / 2


def test_a_default_net_passes_a_default_obstacle_at_fab_clearance():
    gap = _detour_gap(obstacle_net=8, router_net=2)
    assert _BASE - 1e-9 <= gap < _HV


@pytest.mark.parametrize(("obstacle_net", "router_net"), [(7, 2), (8, 7)])
def test_the_hv_net_keeps_its_clearance_from_either_side(obstacle_net, router_net):
    """HV copper claimed first with a default net routing past it, and
    the HV net routing past default copper: both keep the HV figure."""
    gap = _detour_gap(obstacle_net=obstacle_net, router_net=router_net)
    assert gap >= _HV - 1e-9


def _net(name: str, cls: str, a: str, b: str) -> dict[str, Any]:
    return {
        "name": name,
        "net_class": cls,
        "domain": "electrical",
        "members": [{"refdes": a, "pin": "1"}, {"refdes": b, "pin": "1"}],
    }


def _hv_graph() -> dict[str, Any]:
    """An HV net across the board and a default net crossing it."""
    return {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 5.0},
            {"refdes": "U1", "x": 10.0, "y": 5.0},
            {"refdes": "U2", "x": 5.0, "y": 0.0},
            {"refdes": "U3", "x": 5.0, "y": 10.0},
        ],
        "nets": [
            _net("HV", "hv", "U0", "U1"),
            _net("D", "signal", "U2", "U3"),
        ],
    }


def _line(seg: dict[str, Any]) -> LineString:
    return LineString([tuple(seg["start"]), tuple(seg["end"])])


def test_realize_keeps_hv_clearance_without_inflating_the_grid():
    """The two-net fixture: one 0.4 mm class, one default net. The grid
    base stays at the default net's clearance (the pitch is cut from it),
    and the HV tracks keep 0.4 mm to the default net's copper on every
    layer they share."""
    ir = from_graph(_hv_graph(), stackup=DEFAULT_STACKUP)
    config = RealizeConfig(router="maze", class_rules={"hv": {"clearance_mm": _HV}})
    ctx = _route_context(ir, list(range(len(ir.seg_net))), config, None, None)
    assert ctx is not None
    assert ctx.net_clearance[0] == pytest.approx(_HV)
    assert ctx.clearance == pytest.approx(ctx.net_clearance[1])
    assert ctx.clearance < _HV
    assert ctx.clearance_max == pytest.approx(_HV)

    result = realize(ir, config=config)
    assert not result.unrouted
    hv = [t for t in result.tracks if t.net_id == 0]
    assert hv
    gaps: list[float] = []
    for t in hv:
        hv_copper = [_line(s) for s in t.segments]
        for u in result.tracks:
            if u.net_id == 1 and u.layer == t.layer:
                gaps += [
                    a.distance(_line(s)) - (t.width_mm + u.width_mm) / 2
                    for a in hv_copper
                    for s in u.segments
                ]
        for v in result.vias:
            if v.net_id == 1 and v.layer_lo <= t.layer <= v.layer_hi:
                gaps += [
                    a.distance(Point(v.x, v.y)) - (t.width_mm + v.dia_mm) / 2
                    for a in hv_copper
                ]
    assert gaps, "the fixture must put HV and default copper on one layer"
    assert min(gaps) >= _HV - 1e-6
