"""Fine-pitch escape stubs — gr489923.

taz15's CN4 has 0.3 x 1.55 mm pads at 0.5 mm pitch; a 0.3 mm Default-class
track (0.1524 mm clearance) centred on a pad clears its neighbours by
0.2 mm, 0.048 mm of slack against a ~0.1 mm routing cell, so on the grid
every pad reads as walled in. :meth:`~precis.pcb.maze.OccupancyGrid.
escape_stub` proves the straight exit along the pad's axis by exact
geometry and hands the search the first passable cell past the pad.
"""

from __future__ import annotations

from precis.pcb.maze import (
    OccupancyGrid,
    PadShape,
    RoutePath,
    _pt_seg_dist,
    _seg_shape_dist,
    grid_for,
)
from precis.pcb.realize import _stamp_pads

CLEARANCE = 0.1524
TRACK = 0.3
PAD_W, PAD_H, PITCH = 0.3, 1.55, 0.5


_Pad = tuple[tuple[float, float], int, PadShape, tuple[int, ...]]


def _cn4_row(n: int = 7) -> tuple[OccupancyGrid, list[_Pad]]:
    """One row of CN4-like pads at y=0, one net each, on F.Cu (layer 0)."""
    xs = [(i - n // 2) * PITCH for i in range(n)]
    pads: list[_Pad] = [
        ((x, 0.0), i, PadShape("rect", x, 0.0, PAD_W, PAD_H), (0,))
        for i, x in enumerate(xs)
    ]
    spec = grid_for(
        [(x, 0.0) for x in xs] + [(0.0, 5.0), (0.0, -5.0)],
        n_layers=2,
        max_pitch_mm=CLEARANCE * 2.0 / 3.0,
    )
    grid = OccupancyGrid(spec, clearance_mm=CLEARANCE)
    _stamp_pads(grid, pads)
    return grid, pads


def _axis_clearance(
    stub: tuple[tuple[float, float], ...], pads: list[_Pad], net: int
) -> float:
    """Smallest copper gap from the stub's axis run to a foreign pad."""
    a, b = stub[0], stub[1]
    return min(
        _seg_shape_dist(a, b, shape) - TRACK / 2.0
        for _p, n, shape, _l in pads
        if n != net
    )


def test_a_cn4_pad_is_walled_in_on_the_grid():
    grid, _pads = _cn4_row()
    mid = (0.0, 0.0)
    assert grid.route(3, mid, (0.0, 4.0), layers=[0], width_mm=TRACK) is None


def test_the_escape_stub_runs_along_the_pad_and_clears_by_the_exact_rule():
    grid, pads = _cn4_row()
    stub = grid.escape_stub(3, (0.0, 0.0), 0, TRACK)
    assert stub is not None
    assert stub[0] == (0.0, 0.0)
    assert stub[1][0] == 0.0  # the axis run stays on the pad's own axis
    assert abs(stub[1][1]) > PAD_H / 2.0  # and leaves the pad
    assert abs(stub[-1][0] - stub[1][0]) <= grid.spec.pitch / 2.0 + 1e-9
    assert _axis_clearance(stub, pads, 3) >= CLEARANCE - 1e-9
    path = grid.route(
        3,
        (0.0, 0.0),
        (0.0, 4.0),
        layers=[0],
        width_mm=TRACK,
        extra_start_terminals=((stub[-1], 0),),
    )
    assert path is not None
    assert path.points[0][:2] == stub[-1]


def test_a_neighbour_escapes_past_a_routed_stub():
    grid, pads = _cn4_row()
    stub3 = grid.escape_stub(3, (0.0, 0.0), 0, TRACK)
    assert stub3 is not None
    path = grid.route(
        3,
        (0.0, 0.0),
        (0.0, 4.0),
        layers=[0],
        width_mm=TRACK,
        extra_start_terminals=((stub3[-1], 0),),
    )
    assert path is not None
    joined = RoutePath(
        3,
        tuple((x, y, 0) for x, y in stub3[:-1]) + path.points,
        path.length_mm,
    )
    grid.stamp_path(joined, TRACK)
    stub2 = grid.escape_stub(2, (-PITCH, 0.0), 0, TRACK)
    assert stub2 is not None
    gap = min(
        _pt_seg_dist(px, py, stub2[0], stub2[1])
        for px, py, _l in joined.points
        if abs(py) <= abs(stub2[1][1]) + 1e-9 and abs(px) < 0.3
    )
    assert gap - TRACK >= CLEARANCE - 1e-9


def test_grid_only_copper_beside_the_axis_blocks_that_direction():
    grid, _pads = _cn4_row()
    # Foreign copper the grid knows only as cells (fixed copper, a via),
    # right beside the upward exit.
    grid.stamp_disk([0], 0.2, 1.2, 0.1 + CLEARANCE, 99)
    stub = grid.escape_stub(3, (0.0, 0.0), 0, TRACK)
    assert stub is not None
    assert stub[1][1] < 0.0  # it left downward instead
    grid.stamp_disk([0], 0.2, -1.2, 0.1 + CLEARANCE, 99)
    assert grid.escape_stub(3, (0.0, 0.0), 0, TRACK) is None


def test_a_circle_pad_gets_no_stub():
    spec = grid_for([(0.0, 0.0), (2.0, 2.0)], n_layers=1, max_pitch_mm=0.1)
    grid = OccupancyGrid(spec, clearance_mm=CLEARANCE)
    grid.stamp_pad_shape([0], PadShape("circle", 0.0, 0.0, 0.3, 0.3), 1)
    assert grid.escape_stub(1, (0.0, 0.0), 0, TRACK) is None
