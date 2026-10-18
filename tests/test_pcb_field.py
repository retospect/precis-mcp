"""Gradient checks for the unified place-and-route energy (slice 0: T1, T4)."""

from __future__ import annotations

import numpy as np
import pytest

from precis.pcb import field as F


def _board(seed: int, n: int = 5, overlap: bool = False) -> F.FieldBoard:
    rng = np.random.default_rng(seed)
    parts = []
    for i in range(n):
        w, h = rng.uniform(1.0, 2.5, 2)
        court = np.array([[-w, -h], [w, -h], [w, h], [-w, h]])
        pads = rng.uniform(-0.8, 0.8, (3, 2))
        parts.append(
            F.Part(pads=pads, court=court, side=int(i == n - 1 and not overlap))
        )
    nets = tuple(
        tuple(
            (int(i), int(rng.integers(3))) for i in rng.choice(n, size=3, replace=False)
        )
        for _ in range(4)
    )
    span = 3.0 if overlap else 6.0
    return F.FieldBoard(
        parts=tuple(parts),
        nets=nets,
        x0=rng.uniform(0, span, n),
        y0=rng.uniform(0, span, n),
        th0=rng.uniform(-3, 3, n),
        vias0=rng.uniform(0, 5, (2, 2)),
    )


def _term(name: str, lay: F.Layout) -> F.Term:
    if name == "T1":
        return F.ConnectivityPull(lay, gamma=0.7)
    return F.CourtyardClearance(lay, beta=6.0)


@pytest.mark.parametrize("name", ["T1", "T4"])
@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_gradient_checks(name: str, seed: int) -> None:
    lay = F.Layout(_board(seed, overlap=(name == "T4" and seed % 2 == 0)))
    term = _term(name, lay)
    z = lay.pack()
    if name == "T4":
        assert term.value_grad(z)[0] > 0.0  # the board actually exercises the term
    res = F.check_gradient(term.value_grad, z, rng=np.random.default_rng(seed), h=1e-5)
    assert res.dot_rel_err < 1e-4, res
    assert all(1.8 < o < 2.3 for o in res.taylor_orders), res
    assert res.fd_max_rel_err < 1e-3, res


@pytest.mark.parametrize("name", ["T1", "T4"])
def test_rotation_gradient_nonzero(name: str) -> None:
    lay = F.Layout(_board(7, overlap=True))
    _, g = _term(name, lay).value_grad(lay.pack())
    start = 2 * int(lay.free_xy.sum())
    rot = g[start : start + int(lay.free_rot.sum())]
    assert np.abs(rot).max() > 1e-6


def test_fixed_variables_have_no_slot() -> None:
    b = _board(3)
    p0 = F.Part(b.parts[0].pads, b.parts[0].court, fixed_xy=True, fixed_rot=True)
    lay = F.Layout(F.FieldBoard((p0, *b.parts[1:]), b.nets, b.x0, b.y0, b.th0, b.vias0))
    assert lay.size == 2 * 4 + 4 + 4
    z = lay.pack()
    x, y, th, vias = lay.unpack(z)
    assert (x[0], y[0], th[0]) == (b.x0[0], b.y0[0], b.th0[0])
    assert vias.shape == (2, 2)
    energy = F.Energy(lay, [(1.0, _term("T1", lay)), (0.5, _term("T4", lay))])
    assert energy.value_grad(z)[1].shape == z.shape


def test_t4_ignores_opposite_sides() -> None:
    sq = np.array([[-1.0, -1], [1, -1], [1, 1], [-1, 1]])
    zeros = np.zeros(2)
    for sides, expect_zero in (((0, 1), True), ((0, 0), False)):
        parts = tuple(F.Part(pads=np.zeros((1, 2)), court=sq, side=s) for s in sides)
        lay = F.Layout(F.FieldBoard(parts, (), np.array([0.0, 0.5]), zeros, zeros))
        v, _ = F.CourtyardClearance(lay).value_grad(lay.pack())
        assert (v == 0.0) == expect_zero
