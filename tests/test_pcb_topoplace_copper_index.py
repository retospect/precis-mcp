"""The route path's clearance oracle (topo-route-review D-a): swept
bodies, pairwise clearance, own-net exclusion, placed-via discs — and a
property test against a brute-force distance loop."""

from __future__ import annotations

import numpy as np
from shapely.geometry import LineString, Point, Polygon, box

from precis.pcb.topoplace.copper_index import ANY_NET, Body, CopperIndex


def test_pad_rect_blocks_the_barrel_keepout_only() -> None:
    """A 1x1 pad at the origin, barrel r 0.3, clearance 0.15: a centre
    0.44 mm off the pad edge is blocked, 0.46 mm is clear, inside is
    blocked."""
    idx = CopperIndex([Body(box(-0.5, -0.5, 0.5, 0.5))])
    got = idx.blocked_points(
        np.asarray([[0.94, 0.0], [0.96, 0.0], [0.1, 0.1]]), 0.3, 0.15
    )
    assert got.tolist() == [True, False, True]


def test_own_net_wire_never_blocks_its_net() -> None:
    wire = Body(LineString([(0, 0), (10, 0)]), r=0.1, net=3, clearance=0.2)
    pad = Body(box(20, -1, 21, 1))  # ANY_NET: blocks every query
    idx = CopperIndex([wire, pad])
    pt = np.asarray([[5.0, 0.4], [20.5, 1.25]])
    assert idx.blocked_points(pt, 0.2, 0.1, net=3).tolist() == [False, True]
    assert idx.blocked_points(pt, 0.2, 0.1, net=4).tolist() == [True, True]


def test_the_larger_clearance_wins() -> None:
    """The body's own clearance applies when it is the larger one."""
    wire = Body(LineString([(0, 0), (10, 0)]), r=0.1, net=1, clearance=0.4)
    idx = CopperIndex([wire])
    # 0.2 + 0.1 + max(0.1, 0.4) = 0.7
    pts = np.asarray([[5.0, 0.69], [5.0, 0.71]])
    assert idx.blocked_points(pts, 0.2, 0.1).tolist() == [True, False]


def test_placed_vias_join_as_discs() -> None:
    idx = CopperIndex([])
    assert not idx.blocked_points(np.asarray([[1.0, 0.0]]), 0.3, 0.15)[0]
    idx.add_disc(0.0, 0.0, 0.3)
    # 0.3 + 0.3 + 0.15 = 0.75
    got = idx.blocked_points(np.asarray([[0.74, 0.0], [0.76, 0.0]]), 0.3, 0.15)
    assert got.tolist() == [True, False]
    assert idx.blocked(LineString([(0.5, -1), (0.5, 1)]), 0.1, 0.15)
    assert not idx.blocked(LineString([(0.6, -1), (0.6, 1)]), 0.1, 0.15)


def test_segment_query_sees_a_pad_its_endpoints_clear() -> None:
    """A stub whose endpoints clear a pad but whose middle grazes it."""
    idx = CopperIndex([Body(box(-0.2, -0.2, 0.2, 0.2))])
    stub = LineString([(-2.0, 0.3), (2.0, 0.3)])
    assert idx.blocked(stub, 0.1, 0.15)
    assert not idx.blocked(LineString([(-2.0, 0.5), (2.0, 0.5)]), 0.1, 0.15)


def _brute(
    bodies: list[Body], xy: np.ndarray, r: float, c: float, net: int
) -> list[bool]:
    out = []
    for x, y in xy:
        p = Point(float(x), float(y))
        hit = False
        for b in bodies:
            if b.net != ANY_NET and b.net == net:
                continue
            if p.distance(b.geom) < r + b.r + max(c, b.clearance):
                hit = True
                break
        out.append(hit)
    return out


def test_index_matches_a_brute_force_distance_loop() -> None:
    """Random pads, wires and discs of mixed nets and clearances, random
    query points at two barrel sizes and three own nets: the index and a
    plain per-body distance loop disagree 0 times."""
    rng = np.random.default_rng(11)
    bodies: list[Body] = []
    for _ in range(40):
        x, y = rng.uniform(0, 20, 2)
        w, h = rng.uniform(0.2, 1.5, 2)
        bodies.append(Body(box(x, y, x + w, y + h)))
    for k in range(25):
        p = rng.uniform(0, 20, (3, 2))
        bodies.append(
            Body(
                LineString(p),
                r=float(rng.uniform(0.05, 0.3)),
                net=k % 5,
                clearance=float(rng.uniform(0.1, 0.3)),
            )
        )
    for k in range(10):
        x, y = rng.uniform(0, 20, 2)
        bodies.append(Body(Point(x, y), r=0.3, net=ANY_NET if k % 2 else k % 5))
    tri = Polygon([(2, 2), (6, 3), (3, 7)])
    bodies.append(Body(tri))
    idx = CopperIndex(bodies)
    xy = rng.uniform(-1, 21, (600, 2))
    for r, c in ((0.375, 0.15), (0.225, 0.1)):
        for net in (-2, 0, 3):
            got = idx.blocked_points(xy, r, c, net=net).tolist()
            assert got == _brute(bodies, xy, r, c, net), (r, c, net)
