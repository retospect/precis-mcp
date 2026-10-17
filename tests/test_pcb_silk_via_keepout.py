"""gr495393 comment 1: vias stay off part-outline silk, enforced where vias
are placed (``RealizeConfig.silk_via_keepout``, off by default pending
Reto's call on its router-floor cost). No DB."""

from __future__ import annotations

import itertools
import math

from precis.pcb import DEFAULT_STACKUP, maze, realize, silk
from precis.pcb.ir import from_graph


def _seg_dist(p, a, b) -> float:
    ex, ey = b[0] - a[0], b[1] - a[1]
    seg2 = ex * ex + ey * ey
    t = (
        0.0
        if seg2 < 1e-12
        else max(0.0, min(1.0, ((p[0] - a[0]) * ex + (p[1] - a[1]) * ey) / seg2))
    )
    return math.hypot(p[0] - (a[0] + t * ex), p[1] - (a[1] + t * ey))


def _line_dist(p, line) -> float:
    return min(_seg_dist(p, a, b) for a, b in itertools.pairwise(line))


def test_a_keepout_line_refuses_vias_near_it_in_both_queries():
    """``via_clears_pads`` (fan-out / stitch sites) and the maze search's
    via mask (``_pad_keepout_mask``) read the same line, net-blind."""
    spec = maze.GridSpec(x0=0.0, y0=0.0, nx=101, ny=101, pitch=0.1, n_layers=2)
    grid = maze.OccupancyGrid(spec, clearance_mm=0.1)
    line = [(2.0, 5.0), (8.0, 5.0)]
    assert grid.via_clears_pads(5.0, 5.5, 0.3)
    grid.add_via_keepout_line(line, 0.625)
    # copper edge 0.625 off the line: centre at >= 0.925
    assert not grid.via_clears_pads(5.0, 5.9, 0.3)
    assert grid.via_clears_pads(5.0, 5.95, 0.3)
    assert not grid.via_clears_pads(8.8, 5.0, 0.3)  # past the end, within reach
    assert grid.via_clears_pads(9.0, 5.0, 0.3)
    mask = grid._pad_keepout_mask(0.3)
    for iy in range(spec.ny):
        for ix in range(0, spec.nx, 5):
            p = (ix * spec.pitch, iy * spec.pitch)
            assert bool(mask[iy, ix]) == (_line_dist(p, line) < 0.925), p


def test_with_the_keepout_on_no_via_lands_within_reach_of_outline_silk():
    """A top part wired to a bottom part needs a layer change; with the
    keep-out on, every via's copper stays the via-to-silk clearance plus
    two silk widths off every outline run ``build_silk`` will draw (both
    sides: the keep-out is layer-blind, like the pad keep-out)."""
    graph = {
        "instances": [
            {"refdes": "U1", "x": 0.0, "y": 0.0},
            {"refdes": "U2", "x": 6.0, "y": 0.0, "layer": "bottom"},
        ],
        "nets": [
            {
                "name": "N1",
                "members": [{"refdes": "U1", "pin": "1"}, {"refdes": "U2", "pin": "1"}],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    config = realize.RealizeConfig(router="maze", silk_via_keepout=True)
    result = realize.realize(ir, config=config)
    assert result.unrouted == (), result.unrouted_reasons
    keepouts = silk.outline_via_keepouts(ir, capability=config.fab_caps)
    assert keepouts
    stroke = silk.DEFAULT_SILK_WIDTH_MM
    assert keepouts[0][1] == stroke / 2.0 + 0.25 + 2 * stroke
    assert result.vias, "fixture places no vias, so proves nothing"
    for via in result.vias:
        for line, m in keepouts:
            assert _line_dist((via.x, via.y), line) >= via.dia_mm / 2.0 + m - 1e-6, via


def test_the_keepout_is_off_by_default():
    assert realize.RealizeConfig().silk_via_keepout is False
