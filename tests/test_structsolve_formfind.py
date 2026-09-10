"""precis.structsolve.formfind — the pure force-density solver
(docs/backlog/structural-solution-space.md slice 2). Unit-agnostic,
store-free: everything here is arrays in, arrays out."""

from __future__ import annotations

import math

import numpy as np
import pytest

from precis.structsolve import FormFindError, form_find

_ANCHORED = np.array([[True] * 3, [True] * 3, [False] * 3])


def _two_anchor_problem() -> tuple[np.ndarray, np.ndarray]:
    coords = np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [99.0, -7.0, 3.0]])
    members = np.array([[0, 2], [1, 2]])
    return coords, members


def test_equal_densities_put_the_free_node_at_the_midpoint() -> None:
    coords, members = _two_anchor_problem()
    res = form_find(coords, members, np.array([1.0, 1.0]), _ANCHORED)
    assert np.allclose(res.coords[2], [1.0, 0.0, 0.0])
    # the free node's junk input coordinates never mattered
    assert res.residual < 1e-12
    assert np.allclose(res.lengths, [1.0, 1.0])
    assert np.allclose(res.forces, [1.0, 1.0])  # t = q · L, tension-positive
    assert list(res.solved) == [False, False, True]


def test_prescribed_coordinates_return_verbatim() -> None:
    coords, members = _two_anchor_problem()
    res = form_find(coords, members, np.array([1.0, 1.0]), _ANCHORED)
    assert np.array_equal(res.coords[:2], coords[:2])


def test_load_sags_the_free_node_by_p_over_sum_q() -> None:
    coords, members = _two_anchor_problem()
    loads = np.zeros((3, 3))
    loads[2, 2] = -3.0
    res = form_find(coords, members, np.array([1.0, 2.0]), _ANCHORED, loads=loads)
    # per-axis balance: Σq·(anchor − x) + p = 0 → z = −3/(1+2)
    assert math.isclose(res.coords[2][2], -1.0)
    assert res.residual < 1e-12


def test_free_node_lands_at_the_density_weighted_anchor_average() -> None:
    coords = np.array(
        [[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [0.0, 4.0, 0.0], [1.0, 1.0, 1.0]]
    )
    members = np.array([[0, 3], [1, 3], [2, 3]])
    q = np.array([6.0, 1.0, 1.0])
    fixed = np.zeros((4, 3), dtype=bool)
    fixed[:3] = True
    res = form_find(coords, members, q, fixed)
    assert np.allclose(res.coords[3], (q[:, None] * coords[:3]).sum(axis=0) / q.sum())


def test_per_axis_prescription_rides_a_rail() -> None:
    coords, members = _two_anchor_problem()
    fixed = _ANCHORED.copy()
    fixed[2] = [False, False, True]  # z prescribed at 3.0, x/y solved
    res = form_find(coords, members, np.array([1.0, 1.0]), fixed)
    assert np.allclose(res.coords[2], [1.0, 0.0, 3.0])
    assert list(res.solved) == [False, False, True]


def test_mixed_sign_densities_solve_and_balance() -> None:
    # An anchored triangle of cables above, one strut below pulling out:
    # solvability with q < 0 members is the tensegrity-relevant branch.
    coords = np.array(
        [
            [1.0, 0.0, 0.0],
            [-0.5, math.sqrt(3) / 2, 0.0],
            [-0.5, -math.sqrt(3) / 2, 0.0],
            [0.0, 0.0, 0.0],
        ]
    )
    members = np.array([[0, 3], [1, 3], [2, 3]])
    q = np.array([1.0, 1.0, -0.5])
    fixed = np.zeros((4, 3), dtype=bool)
    fixed[:3] = True
    res = form_find(coords, members, q, fixed)
    # independent equilibrium recheck at the free node from the geometry
    balance = np.zeros(3)
    for k in range(3):
        balance += q[k] * (res.coords[k] - res.coords[3])
    assert np.allclose(balance, 0.0, atol=1e-12)
    assert res.forces[2] < 0.0  # the strut ends up in compression


# ── refusal paths ────────────────────────────────────────────────────────


def test_no_anchors_is_singular_with_a_named_cause() -> None:
    coords, members = _two_anchor_problem()
    with pytest.raises(FormFindError, match="singular along axis x"):
        form_find(coords, members, np.array([1.0, 1.0]), np.zeros((3, 3), dtype=bool))


def test_free_group_disconnected_from_every_anchor_is_singular() -> None:
    coords = np.zeros((4, 3))
    coords[1] = [2.0, 0.0, 0.0]
    members = np.array([[0, 1], [2, 3]])  # 2—3 floats free
    fixed = np.zeros((4, 3), dtype=bool)
    fixed[0] = fixed[1] = True
    with pytest.raises(FormFindError, match="disconnected"):
        form_find(coords, members, np.array([1.0, 1.0]), fixed)


def test_cancelling_densities_are_singular_not_garbage() -> None:
    # q = +1 and −1 on the only two members of the free node: the
    # Laplacian diagonal cancels to zero — refuse, never lstsq a shape.
    coords, members = _two_anchor_problem()
    with pytest.raises(FormFindError, match="singular"):
        form_find(coords, members, np.array([1.0, -1.0]), _ANCHORED)


@pytest.mark.parametrize(
    "q",
    [np.array([0.0, 1.0]), np.array([np.nan, 1.0]), np.array([np.inf, 1.0])],
)
def test_zero_or_nonfinite_densities_reject(q: np.ndarray) -> None:
    coords, members = _two_anchor_problem()
    with pytest.raises(FormFindError, match="finite nonzero"):
        form_find(coords, members, q, _ANCHORED)


def test_self_loop_rejects() -> None:
    coords, _ = _two_anchor_problem()
    with pytest.raises(FormFindError, match="self-loop"):
        form_find(coords, np.array([[2, 2]]), np.array([1.0]), _ANCHORED)


def test_out_of_range_member_index_rejects() -> None:
    coords, _ = _two_anchor_problem()
    with pytest.raises(FormFindError, match=r"\[0, 3\)"):
        form_find(coords, np.array([[0, 5]]), np.array([1.0]), _ANCHORED)


def test_shape_mismatches_reject() -> None:
    coords, members = _two_anchor_problem()
    with pytest.raises(FormFindError, match="one force density per member"):
        form_find(coords, members, np.array([1.0]), _ANCHORED)
    with pytest.raises(FormFindError, match="fixed must be"):
        form_find(coords, members, np.array([1.0, 1.0]), np.array([[True] * 3] * 2))
    with pytest.raises(FormFindError, match="loads must be"):
        form_find(
            coords, members, np.array([1.0, 1.0]), _ANCHORED, loads=np.zeros((2, 3))
        )
    with pytest.raises(FormFindError, match="nothing to solve"):
        form_find(coords, np.empty((0, 2), dtype=int), np.empty(0), _ANCHORED)


def test_nonfinite_prescribed_coordinate_rejects() -> None:
    coords, members = _two_anchor_problem()
    coords[0, 0] = np.nan
    with pytest.raises(FormFindError, match="prescribed coordinates must be finite"):
        form_find(coords, members, np.array([1.0, 1.0]), _ANCHORED)
