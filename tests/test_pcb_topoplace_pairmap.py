"""Slice 1a: the pairwise part map — strength (clique weight) and
facing-row inversions (R14), per spec §1a's own tests."""

from __future__ import annotations

import numpy as np

from precis.pcb.topoplace import TopoDesign, pair_map


def _design(
    pin_xy: list[list[float]],
    pin_part: list[int],
    net_pins: list[list[int]],
    pose0: list[list[float]],
    n_parts: int,
) -> TopoDesign:
    return TopoDesign(
        parts=tuple(f"U{i}" for i in range(n_parts)),
        pin_xy=np.asarray(pin_xy, dtype=np.float64),
        pin_part=np.asarray(pin_part, dtype=np.int64),
        pin_side=np.zeros(len(pin_xy), dtype=np.int8),
        pin_label=np.asarray([str(i) for i in range(len(pin_xy))], dtype=object),
        pin_net=np.full(len(pin_xy), -1, dtype=np.int32),
        net_pins=tuple(np.asarray(n, dtype=np.int64) for n in net_pins),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(n_parts, dtype=bool),
        freedoms=np.full(n_parts, 3, dtype=np.uint8),
        outline=np.asarray([[-5, -5], [15, -5], [15, 15], [-5, 15]]),
        pose0=np.asarray(pose0, dtype=np.float64),
    )


def test_facing_rows_reversed_six_inversions() -> None:
    """Two 4-pin parts facing, nets in reversed order: 6 inversions at
    (0, 0), 0 at the rotation pair that reverses one row, ``best``
    names a zero-scoring pair."""
    # A's pins march up the facing edge y=0..3 for nets 0..3; B's march
    # down y=3..0 — the two orders are exact reversals.
    d = _design(
        pin_xy=[
            [-1.0, 0.0],
            [-1.0, 1.0],
            [-1.0, 2.0],
            [-1.0, 3.0],
            [1.0, 3.0],
            [1.0, 2.0],
            [1.0, 1.0],
            [1.0, 0.0],
        ],
        pin_part=[0, 0, 0, 0, 1, 1, 1, 1],
        net_pins=[[0, 4], [1, 5], [2, 6], [3, 7]],
        pose0=[[0.0, 0.0, 0.0, 0.0], [10.0, 0.0, 0.0, 0.0]],
        n_parts=2,
    )
    stat = pair_map(d).pairs[(0, 1)]
    assert stat.crossings.shape == (4, 4, 2)
    assert int(stat.crossings[0, 0, 0]) == 6
    # reversing either row (a 180° turn on one side) aligns the orders.
    assert int(stat.crossings[0, 2, 0]) == 0
    assert int(stat.crossings[2, 0, 0]) == 0
    assert stat.best_crossings == 0
    ra, rb = stat.best
    assert int(stat.crossings[ra, rb, :].min()) == 0


def test_clique_weight_three_part_net() -> None:
    """A net on 3 parts adds 1/(k-1) = 0.5 to each of its 3 pairs."""
    d = _design(
        pin_xy=[[0.0, 0.0]] * 3,
        pin_part=[0, 1, 2],
        net_pins=[[0, 1, 2]],
        pose0=[[0.0, 0.0, 0.0, 0.0], [5.0, 0.0, 0.0, 0.0], [2.5, 4.0, 0.0, 0.0]],
        n_parts=3,
    )
    pm = pair_map(d)
    assert set(pm.pairs) == {(0, 1), (0, 2), (1, 2)}
    for stat in pm.pairs.values():
        assert stat.strength == 0.5


def test_pairs_without_shared_nets_are_not_stored() -> None:
    d = _design(
        pin_xy=[[0.0, 0.0]] * 4,
        pin_part=[0, 1, 2, 3],
        net_pins=[[0, 1]],
        pose0=[
            [0.0, 0.0, 0.0, 0.0],
            [5.0, 0.0, 0.0, 0.0],
            [0.0, 5.0, 0.0, 0.0],
            [5.0, 5.0, 0.0, 0.0],
        ],
        n_parts=4,
    )
    pm = pair_map(d)
    assert set(pm.pairs) == {(0, 1)}


def test_seed_rotations_and_clusters() -> None:
    """The reversed-rows pair seeds the correcting rotation, and the
    strongest pair lands in one cluster."""
    d = _design(
        pin_xy=[
            [-1.0, 0.0],
            [-1.0, 1.0],
            [-1.0, 2.0],
            [-1.0, 3.0],
            [1.0, 3.0],
            [1.0, 2.0],
            [1.0, 1.0],
            [1.0, 0.0],
        ],
        pin_part=[0, 0, 0, 0, 1, 1, 1, 1],
        net_pins=[[0, 4], [1, 5], [2, 6], [3, 7]],
        pose0=[[0.0, 0.0, 0.0, 0.0], [10.0, 0.0, 0.0, 0.0]],
        n_parts=2,
    )
    pm = pair_map(d)
    rots = pm.seed_rotations()
    stat = pm.pairs[(0, 1)]
    assert int(stat.crossings[int(rots[0]), int(rots[1]), :].min()) == 0
    assert pm.clusters(1) == [[0, 1]]
