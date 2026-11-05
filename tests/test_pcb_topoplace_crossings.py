"""G1 (topo-route-geometry-truth): ``realise._geometric_regions`` finds
crossings from the taut polylines' own geometry — proper intersections,
interleaved vertex touches and run swaps — never from the per-edge
order lists, and brackets each on the branches' edge-index spaces."""

from __future__ import annotations

import itertools

import numpy as np

from precis.pcb.topoplace.mesh import TopoMesh
from precis.pcb.topoplace.realise import _geometric_regions
from precis.pcb.topoplace.wires import WirePath, Wires

_N_COLS = 6


def _mesh() -> TopoMesh:
    """Vertical mesh edges at x = 0.5, 1.5, … spanning y in [-2, 5];
    a wire crossing column ``i`` at height ``y`` has ``t = (y+2)/7``."""
    verts = np.asarray([[i + 0.5, y] for i in range(_N_COLS) for y in (-2.0, 5.0)])
    edges = np.asarray([[2 * i, 2 * i + 1] for i in range(_N_COLS)], np.int32)
    return TopoMesh(
        verts=verts,
        tris=np.zeros((0, 3), np.int32),
        edges=edges,
        tri_edges=np.zeros((0, 3), np.int32),
        edge_tris=np.zeros((_N_COLS, 2), np.int32),
        constrained=np.zeros(_N_COLS, dtype=bool),
        pad_vertex={},
    )


def _wire(net: int, points, cols: tuple[int, ...] = ()) -> WirePath:
    """A branch along ``points``; ``cols`` are the mesh columns it
    crosses, marked where the polyline meets them."""
    ys = []
    for c in cols:
        for p, q in itertools.pairwise(points):
            if min(p[0], q[0]) <= c + 0.5 <= max(p[0], q[0]) and p[0] != q[0]:
                f = (c + 0.5 - p[0]) / (q[0] - p[0])
                ys.append(p[1] + f * (q[1] - p[1]))
                break
    return WirePath(
        net=net,
        pin_a=2 * net,
        pin_b=2 * net + 1,
        tris=(),
        edges=cols,
        t=tuple((y + 2.0) / 7.0 for y in ys),
        points=tuple((float(x), float(y)) for x, y in points),
    )


def _regions(
    *branches: WirePath, obstacles: tuple[tuple[float, float], ...] = ()
) -> dict[tuple[int, int], list[tuple[int, int, int, int]]]:
    return _geometric_regions(
        _mesh(),
        Wires(
            branches=branches,
            order=((),) * _N_COLS,
            capacity_mm=np.zeros(_N_COLS),
            net_layers=tuple(frozenset({0, 3}) for _ in branches),
            unreachable=(),
            gap_free_mm=np.full((_N_COLS, 4), -1.0),
            gap_cap=np.full((_N_COLS, 4), -1),
        ),
        np.asarray(obstacles, dtype=float).reshape(-1, 2),
    )


def test_two_straight_wires_crossing_in_open_mesh_are_one_crossing() -> None:
    a = _wire(0, [(0, 0), (4, 0)], (0, 1, 2, 3))
    b = _wire(1, [(2, -1.5), (2, 1.5)])  # vertical through (2, 0), no edges
    regs = _regions(a, b)
    assert list(regs) == [(0, 1)]
    assert len(regs[(0, 1)]) == 1


def test_crossing_between_edge_marks_brackets_the_piece_between_them() -> None:
    # a's marks sit at x = 0.5, 1.5, 2.5, 3.5; the crossing at x = 2.0 is
    # between marks 1 and 2 -> run index 2 -> bracket (1, 2]
    a = _wire(0, [(0, 0), (4, 0)], (0, 1, 2, 3))
    # b's marks at x = 1.5 (y = 1), 2.5 (y = -1): the crossing is between
    b = _wire(1, [(1.0, 1.0), (1.5, 1.0), (2.5, -1.0), (3.0, -1.0)], (1, 2))
    assert _regions(a, b) == {(0, 1): [(1, 2, 0, 1)]}


def test_wires_passing_opposite_sides_of_a_free_vertex_do_not_cross() -> None:
    a = _wire(0, [(0, 1), (2, 0.4), (4, 1)], (0, 1, 2, 3))
    b = _wire(1, [(0, -1), (2, -0.4), (4, -1)], (0, 1, 2, 3))
    assert _regions(a, b) == {}


def test_vertex_touch_with_interleaved_arms_is_one_crossing() -> None:
    a = _wire(0, [(0, 0), (2, 0), (4, 0)])
    b = _wire(1, [(1, -1), (2, 0), (3, 1)])  # through a's vertex, side to side
    assert list(_regions(a, b)) == [(0, 1)]
    # touching at a's interior of a SEGMENT instead of a vertex: same
    c = _wire(2, [(0, 0), (4, 0)])
    assert list(_regions(c, b)) == [(0, 1)]


def test_vertex_touch_on_one_side_is_not_a_crossing() -> None:
    a = _wire(0, [(0, 0), (2, 0), (4, 0)])
    b = _wire(1, [(1, 1), (2, 0), (3, 1)])  # a V resting on the line
    assert _regions(a, b) == {}
    # both bending at the shared vertex, arms nested not interleaved
    c = _wire(0, [(0, 1), (2, 0), (4, 1)])
    d = _wire(1, [(0, 2), (2, 0), (4, 2)])
    assert _regions(c, d) == {}


def test_shared_pad_endpoint_is_not_a_crossing() -> None:
    a = _wire(0, [(0, 0), (2, 2)])
    b = _wire(1, [(0, 0), (2, -2)])
    assert _regions(a, b) == {}
    # one wire's pad endpoint resting on the other's segment
    c = _wire(1, [(2, 0), (2, 2)])
    d = _wire(0, [(0, 0), (4, 0)])
    assert _regions(d, c) == {}


def test_stub_side_crossings_bracket_the_nearest_piece() -> None:
    # a's only marks are x = 1.5 and 2.5; crossings left of 1.5 and
    # right of 2.5 sit on its pad stubs
    a = _wire(0, [(0, 0), (4, 0)], (1, 2))
    before = _wire(1, [(0.5, -1), (0.5, 1)])
    after = _wire(2, [(3.5, -1), (3.5, 1)])
    regs = _regions(a, before, after)
    # (-1, 0]: the stub before the first mark; (0, 1]: the last piece
    assert regs[(0, 1)] == [(-1, 0, -1, 0)]
    assert regs[(0, 2)] == [(0, 1, -1, 0)]


def test_straight_branch_with_one_edge_brackets_validly() -> None:
    a = _wire(0, [(0, 0), (4, 0)], (1,))
    b = _wire(1, [(3.0, -1), (3.0, 1)])
    c = _wire(2, [(0.5, -1), (0.5, 1)])
    regs = _regions(a, b, c)
    # one edge: before the mark and after it both map to index 0
    assert regs[(0, 1)] == [(-1, 0, -1, 0)]
    assert regs[(0, 2)] == [(-1, 0, -1, 0)]


def test_same_net_pairs_are_kept() -> None:
    a = _wire(0, [(0, 0), (4, 0)])
    b = WirePath(
        net=0,
        pin_a=5,
        pin_b=6,
        tris=(),
        edges=(),
        t=(),
        points=((2.0, -1.0), (2.0, 1.0)),
    )
    assert list(_regions(a, b)) == [(0, 1)]


def test_collinear_run_where_the_wires_swap_sides_is_a_crossing() -> None:
    # both ride (1,0)->(3,0); a arrives from above and leaves below, b
    # the reverse — they trade places along the shared run
    a = _wire(0, [(0, 1), (1, 0), (2, 0), (3, 0), (4, -1)])
    b = _wire(1, [(0, -1), (1, 0), (2, 0), (3, 0), (4, 1)])
    assert list(_regions(a, b)) == [(0, 1)]


def test_collinear_run_parting_on_the_side_they_came_from_is_a_fan() -> None:
    a = _wire(0, [(0, 1), (1, 0), (3, 0), (4, 1)])
    b = _wire(1, [(0, 2), (1, 0), (3, 0), (4, 2)])
    assert _regions(a, b) == {}


def test_collinear_run_ending_at_a_pad_is_not_a_crossing() -> None:
    a = _wire(0, [(0, 0), (4, 0)])
    b = _wire(1, [(1, 0), (3, 0)])  # sits along a, both ends are pads
    assert _regions(a, b) == {}


def test_same_side_run_whose_inner_outer_order_swaps_is_a_crossing() -> None:
    # both leave to the right of the run (0,0)->(1,0); b comes in as the
    # outer wire and leaves as the inner one
    a = _wire(0, [(-1, -1), (0, 0), (1, 0), (2, -2)])
    b = _wire(1, [(0, -1), (0, 0), (1, 0), (3, -1)])
    assert list(_regions(a, b)) == [(0, 1)]
    # the same topology with the run collapsed to one vertex agrees
    a1 = _wire(0, [(-1, -1), (0, 0), (2, -2)])
    b1 = _wire(1, [(0, -1), (0, 0), (3, -1)])
    assert list(_regions(a1, b1)) == [(0, 1)]
    # and so does the nested pair, with the wires' roles exchanged
    na = _wire(0, [(0, -1), (0, 0), (1, 0), (2, -2)])
    nb = _wire(1, [(-1, -1), (0, 0), (1, 0), (3, -1)])
    assert _regions(na, nb) == {}
    na1 = _wire(0, [(0, -1), (0, 0), (2, -2)])
    nb1 = _wire(1, [(-1, -1), (0, 0), (3, -1)])
    assert _regions(na1, nb1) == {}


def test_wire_running_straight_through_a_runs_end_orders_by_the_others_side() -> None:
    a = _wire(0, [(0, 0), (4, 0)])
    b = _wire(1, [(1, -1), (2, 0), (3, 0), (4, 1)])  # below a, then above a
    assert list(_regions(a, b)) == [(0, 1)]
    c = _wire(1, [(1, -1), (2, 0), (3, 0), (4, -1)])  # stays below a
    assert _regions(a, c) == {}


def test_empty_bigon_is_no_crossing() -> None:
    a = _wire(0, [(0, 0), (4, 0)])
    b = _wire(1, [(1, -1), (2, 1), (3, -1)])  # over a and back, nothing inside
    assert _regions(a, b) == {}


def test_bigon_with_an_obstacle_inside_keeps_both_crossings() -> None:
    a = _wire(0, [(0, 0), (4, 0)])
    b = _wire(1, [(1, -1), (2, 1), (3, -1)])
    regs = _regions(a, b, obstacles=((2.0, 0.3),))
    assert len(regs[(0, 1)]) == 2
    # an obstacle at the tip or on an arc does not block the cancellation
    assert _regions(a, b, obstacles=((2.0, 1.0), (1.5, 0.0))) == {}


def test_three_crossings_with_one_empty_bigon_leave_one() -> None:
    a = _wire(0, [(0, 0), (6, 0)])
    b = _wire(1, [(0.5, -1), (1, 1), (2, -1), (4, -1), (5, 1), (5, 2)])
    assert len(_regions(a, b)[(0, 1)]) == 1
