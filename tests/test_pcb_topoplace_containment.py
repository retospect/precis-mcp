"""Board-outline containment on a CONCAVE outline (taz-leaf's leaf tip
and neck): the movers clamp part centres to the outline's bounding box,
which on a concave board admits positions off the board. The shove LP's
mesh guard holds a part that keeps a gap to the edge, but a courtyard
already TOUCHING the edge has ring vertices on the boundary with no
triangle beyond them — so the solved step used to carry it off the
board. These tests pin :mod:`precis.pcb.topoplace.contain`'s one
courtyard-in-polygon test across the shove (settle and route-stage
pushes), the anneal's commit gate and legalize's report.

Fixture: a 20×20 U — a notch x∈(6, 14), y∈(6, 20) cut from the top —
so the notch lies inside the bbox but off the board."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from shapely.geometry import Polygon

from precis.pcb.topoplace import placer
from precis.pcb.topoplace.contain import (
    board_polygon,
    on_board,
    outside_mm2,
    part_outside_mm2,
    stays_on_board,
)
from precis.pcb.topoplace.design import ObjectiveShares, TopoDesign
from precis.pcb.topoplace.legalize import legalize_pose
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.proxy import _ring_geom
from precis.pcb.topoplace.shove import ShovePush, shove_pose

_U = np.asarray(
    [[0, 0], [20, 0], [20, 20], [14, 20], [14, 6], [6, 6], [6, 20], [0, 20]],
    dtype=float,
)


def _sq(h: float) -> np.ndarray:
    """A closed square courtyard ring of half-size ``h``."""
    return np.asarray([[-h, -h], [h, -h], [h, h], [-h, h], [-h, -h]], dtype=float)


def _board(
    rings: list[np.ndarray],
    pose: list[list[float]],
    fixed: list[bool],
    *,
    nets: list[list[int]] | None = None,
    outline: np.ndarray = _U,
) -> TopoDesign:
    """One centred pin per part, courtyard ``rings[i]``, on the U."""
    n = len(rings)
    net_pins = nets or []
    pin_net = np.full(n, -1, dtype=np.int32)
    for k, pins in enumerate(net_pins):
        pin_net[pins] = k
    return TopoDesign(
        parts=tuple(f"P{i}" for i in range(n)),
        pin_xy=np.zeros((n, 2)),
        pin_part=np.arange(n, dtype=np.int64),
        pin_side=np.zeros(n, dtype=np.int8),
        pin_label=np.asarray(["1"] * n, dtype=object),
        pin_net=pin_net,
        net_pins=tuple(np.asarray(p, dtype=np.int64) for p in net_pins),
        flex=(),
        swap_groups=(),
        fixed=np.asarray(fixed, dtype=bool),
        freedoms=np.full(n, 3, dtype=np.uint8),
        outline=outline,
        pose0=np.asarray(pose, dtype=float),
        part_outline=tuple(rings),
        net_name=tuple(f"N{k}" for k in range(len(net_pins))),
        pin_wh=np.full((n, 2), 0.3),
    )


def _off(d: TopoDesign, pose: np.ndarray, part: int) -> float:
    """``part``'s courtyard area off the board at ``pose``."""
    return part_outside_mm2(d, board_polygon(d), pose[part], part)


def _all_on(d: TopoDesign, pose: np.ndarray) -> bool:
    """Every courtyard on the board — by the shared predicate AND by the
    mesh's own independent outline-conflict report."""
    board = board_polygon(d)
    if not all(on_board(board, _ring_geom(d, pose[i], i)) for i in range(len(d.parts))):
        return False
    return not build_mesh(d, pose).outline_conflicts


def test_shove_keeps_an_edge_touching_part_on_a_concave_board() -> None:
    """P1's courtyard touches the notch wall (x = 6); the fixed P0
    overlaps it from the left, so the pair's MTV pushes P1 straight into
    the notch — inside the bbox, off the board. Pre-fix the LP moved it
    0.4 mm off the board (no triangle stands beyond a boundary vertex);
    now the step is held at the wall and the overlap stays a residual
    for legalize."""
    d = _board([_sq(1.0), _sq(0.4)], [[4.6, 12, 0, 0], [5.6, 12, 0, 0]], [True, False])
    assert _all_on(d, d.pose0)
    res = shove_pose(d, d.pose0.copy())
    assert _all_on(d, res.pose), res.pose
    np.testing.assert_array_equal(res.pose[0], d.pose0[0])
    assert any((a, b) == (0, 1) for a, b, _ in res.remaining)
    assert not res.resolved


def test_route_push_never_carries_a_part_off_the_board() -> None:
    """The route stage's failure-directed shove commits its pose with no
    legalize behind it. A push into the notch wall (and one into the
    notch floor) used to carry the part's pads off the board — the next
    round's mesh refused it ('pad(s) sit in no triangle') and the run
    still reported ``resolved``."""
    wall = _board(
        [_sq(0.4), _sq(0.4)], [[3.0, 12, 0, 0], [5.6, 12, 0, 0]], [True, False]
    )
    res = shove_pose(wall, wall.pose0.copy(), push=[ShovePush(0, 1, (1.0, 0.0), 1.0)])
    assert _all_on(wall, res.pose), res.pose
    assert res.mesh_errors == 0 and res.reason != "mesh"

    floor = _board(
        [_sq(0.4), _sq(0.4)], [[10.0, 3.0, 0, 0], [10.0, 5.6, 0, 0]], [True, False]
    )
    res = shove_pose(floor, floor.pose0.copy(), push=[ShovePush(0, 1, (0.0, 1.0), 1.5)])
    assert _all_on(floor, res.pose), res.pose
    assert res.mesh_errors == 0


def test_oblique_push_slides_along_the_wall_instead_of_leaving() -> None:
    """Containment does not kill a push the board has room for: P1
    touches the notch wall and is pushed diagonally (+x, +y). The LP may
    first spend the push on +x (off the board); the containment cut
    re-solves it onto +y, up the arm, so the push is still met."""
    d = _board([_sq(0.4), _sq(0.4)], [[3.0, 10, 0, 0], [5.6, 12, 0, 0]], [True, False])
    axis = np.asarray([1.0, 1.0]) / np.sqrt(2.0)
    res = shove_pose(
        d, d.pose0.copy(), push=[ShovePush(0, 1, (1.0, 1.0), 1.0)], rounds=3
    )
    assert _all_on(d, res.pose), res.pose
    gained = float(axis @ (res.pose[1, :2] - d.pose0[1, :2]))
    assert gained >= 1.0 - 1e-3, res.pose


def _pair_overlap(d: TopoDesign, pose: np.ndarray, a: int, b: int) -> float:
    ga, gb = _ring_geom(d, pose[a], a), _ring_geom(d, pose[b], b)
    return float(ga.intersection(gb).area)


def test_axis_push_slides_along_a_diagonal_wall() -> None:
    """An MTV push is axis-aligned, the leaf's edges are not: P1 touches
    a 45° wall and the fixed P0 pushes it straight up into it. Cut along
    the step's own direction the push would just stop (δy ≤ 0); cut
    along the wall's normal the LP slides P1 up-right along the wall,
    separating the pair on the board (the last hair converges round by
    round under the mesh guard — legalize's to finish)."""
    tri = np.asarray([[0, 0], [20, 0], [20, 20]], dtype=float)  # wall y = x
    d = _board(
        [_sq(0.75), _sq(0.5)],
        [[10.0, 8.0, 0, 0], [10.0, 9.0, 0, 0]],  # P1's corner (9.5, 9.5) on it
        [True, False],
        outline=tri,
    )
    assert _all_on(d, d.pose0)
    before = _pair_overlap(d, d.pose0, 0, 1)
    assert before > 0.2
    res = shove_pose(d, d.pose0.copy())
    assert _all_on(d, res.pose), res.pose
    assert res.pose[1, 0] > d.pose0[1, 0] + 0.2, res.pose  # slid along the wall
    assert _pair_overlap(d, res.pose, 0, 1) < 0.05 * before, res.pose


def test_rigid_group_is_held_on_the_board_as_one_unit() -> None:
    """A two-member group shares one LP variable: the fixed P0 pushes
    member P1 right, and the shared step would carry member P2 (touching
    the notch wall) off the board. The whole unit is held — both
    members stay on, the authored offset intact."""
    d = _board(
        [_sq(1.0), _sq(0.4), _sq(0.4)],
        [[2.6, 12, 0, 0], [3.6, 12, 0, 0], [5.6, 12, 0, 0]],
        [True, False, False],
    )
    group_of = np.asarray([-1, 0, 0], dtype=np.int32)
    group_off = np.zeros((3, 3))
    group_off[2] = (2.0, 0.0, 0.0)
    d = dataclasses.replace(
        d, group_of=group_of, group_members=((1, 2),), group_off=group_off
    )
    res = shove_pose(d, d.pose0.copy())
    assert _all_on(d, res.pose), res.pose
    np.testing.assert_allclose(res.pose[2, :2] - res.pose[1, :2], (2.0, 0.0))


def test_overhanging_part_never_overhangs_more() -> None:
    """A part already partly off the board (an edge connector, a
    jittered restart) may come in but not go further out: the pair's
    MTV pushes the overhanging P1 deeper into the notch."""
    d = _board([_sq(1.0), _sq(0.4)], [[4.8, 12, 0, 0], [5.8, 12, 0, 0]], [True, False])
    before = _off(d, d.pose0, 1)
    assert before > 0.0
    res = shove_pose(d, d.pose0.copy())
    assert _off(d, res.pose, 1) <= before + 1e-9, res.pose


def test_settle_shove_swallows_no_off_board_mesh_error() -> None:
    """The taz-leaf summary symptom ('N pad(s) sit in no triangle —
    outside the board outline'): a deep overlap's MTV pushed a part that
    touches the notch wall far enough for its pad to leave the board,
    and the next shove round's mesh refused it. The settle's shoves now
    stay on the board, so no mesh error is swallowed, and the settled
    pose keeps every courtyard on the board."""
    d = _board([_sq(1.2), _sq(0.4)], [[4.7, 12, 0, 0], [5.6, 12, 0, 0]], [True, False])
    pose, shoved, legal = placer._settle_pose(d, d.pose0.copy())
    assert shoved.mesh_errors == 0, shoved.mesh_error
    assert _all_on(d, pose), pose
    assert not any(b == -1 for _a, b, _area in legal.remaining)


def test_shove_containment_is_deterministic() -> None:
    d = _board([_sq(1.2), _sq(0.4)], [[4.7, 12, 0, 0], [5.6, 12, 0, 0]], [True, False])
    a = shove_pose(d, d.pose0.copy(), push=[ShovePush(0, 1, (1.0, 1.0), 1.0)])
    b = shove_pose(d, d.pose0.copy(), push=[ShovePush(0, 1, (1.0, 1.0), 1.0)])
    np.testing.assert_array_equal(a.pose, b.pose)
    assert a.moved_mm == b.moved_mm


def test_anneal_keeps_on_board_parts_off_the_notch() -> None:
    """The anneal clamps translates to the outline's BBOX, and with no
    overlap share its legality debt is identically 0 — nothing held a
    courtyard to the polygon, so the ratsnest pull across the notch
    parked parts in it. Every elite is a raw anneal pose: none may have
    a courtyard off the board when the seed pose had none."""
    d = _board(
        [_sq(0.6)] * 4,
        [[2.0, 16, 0, 0], [4.0, 10, 0, 0], [18.0, 16, 0, 0], [16.0, 10, 0, 0]],
        [False] * 4,
        nets=[[0, 2], [1, 3]],
    )
    assert _all_on(d, d.pose0)
    res = placer.topo_place(d, objective=ObjectiveShares(), effort=1500, seed=3)
    for e in res.elites:
        assert _all_on(d, e.pose), e.pose
    assert _all_on(d, res.best_pose), res.best_pose


def test_keeps_on_board_gate_is_the_shared_rule() -> None:
    """The anneal gate refuses a staged row that carries an on-board
    courtyard into the notch and accepts one that stays on the board."""
    d = _board([_sq(0.4)], [[4.0, 12, 0, 0]], [False])
    board = board_polygon(d)
    off_now = np.zeros(1)
    into_notch = d.pose0[0].copy()
    into_notch[0] = 8.0
    along_arm = d.pose0[0].copy()
    along_arm[1] = 15.0
    assert not placer._keeps_on_board(d, board, {0: into_notch}, off_now, {})
    assert placer._keeps_on_board(d, board, {0: along_arm}, off_now, {})


def test_contain_predicate_matches_legalize_report() -> None:
    """One notion everywhere: on-board is the courtyard covered by the
    outline polygon (boundary contact allowed), graded as the area off
    it — the same number legalize reports as an ``(i, -1, area)``
    residual for a part it cannot bring back."""
    board = Polygon(_U)
    touching = Polygon(_sq(0.4)[:-1] + (5.6, 12.0))
    poking = Polygon(_sq(0.4)[:-1] + (5.8, 12.0))
    assert on_board(board, touching) and outside_mm2(board, touching) == 0.0
    assert not on_board(board, poking)
    assert outside_mm2(board, poking) == pytest.approx(0.2 * 0.8, abs=1e-9)
    assert stays_on_board(0.0, 0.0) and not stays_on_board(0.0, 1e-6)
    assert stays_on_board(0.16, 0.16) and not stays_on_board(0.16, 0.2)

    # a fixed overhanging part legalize cannot move: reported off the board
    d = _board([_sq(0.4)], [[5.8, 12, 0, 0]], [True])
    res = legalize_pose(d, d.pose0.copy())
    off = [area for a, b, area in res.remaining if (a, b) == (0, -1)]
    assert off and off[0] == pytest.approx(0.16, abs=1e-9)
    ring = _ring_geom(d, d.pose0[0], 0)
    assert off[0] == outside_mm2(board_polygon(d), ring)
