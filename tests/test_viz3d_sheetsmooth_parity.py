"""Vectorised ``smooth_sheet`` vs the pre-kernel per-atom loop (gr462703).

``_reference_smooth_sheet`` is the previous implementation copied verbatim
(the repo's "pre-kernel code verbatim" parity pattern)."""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from precis.viz3d.sheetsmooth import _adjacency, smooth_sheet
from precis_se.atomic.generators.smooth_drum import build_smooth_drum
from precis_se.atomic.generators.sp2 import build_fullerene


def _reference_smooth_sheet(
    coords: NDArray[np.floating],
    bonds: list[tuple[int, int]],
    *,
    iters: int = 20,
    lam: float = 0.5,
    mu: float = -0.53,
) -> NDArray[np.float64]:
    xyz = np.asarray(coords, dtype=np.float64)
    n = len(xyz)
    adj = _adjacency(n, bonds)

    def _pass(x: NDArray[np.float64], factor: float) -> NDArray[np.float64]:
        out = x.copy()
        for i, nbrs in enumerate(adj):
            if not nbrs:
                continue
            mean = x[nbrs].mean(axis=0)
            out[i] = x[i] + factor * (mean - x[i])
        return out

    x = xyz.copy()
    for _ in range(iters):
        x = _pass(x, lam)
        x = _pass(x, mu)
    return x


def _inputs(name: str) -> tuple[NDArray[np.float64], list[tuple[int, int]]]:
    if name == "c60":
        g = build_fullerene({"atoms": 60})
    else:
        g = build_smooth_drum(
            {
                "neck": 8,
                "wall": 40,
                "stalk_length_A": 6,
                "wall_height_A": 12,
                "sheet_radius_A": 20,
            }
        )
    return (
        np.asarray(g.coords, dtype=np.float64),
        [(i, j) for i, j, _o, _k in g.bonds],
    )


@pytest.mark.parametrize("name", ["c60", "drum"])
def test_vectorised_smooth_matches_reference(name: str) -> None:
    coords, bonds = _inputs(name)
    new = smooth_sheet(coords, bonds)
    old = _reference_smooth_sheet(coords, bonds)
    assert new.shape == old.shape
    # atol, not equality: a vectorised neighbour mean sums in a different
    # order from one ``ndarray.mean`` per atom, so the last bits differ.
    assert np.allclose(new, old, rtol=0, atol=1e-9)
    # Not vacuous: smoothing actually moved atoms.
    assert float(np.abs(new - coords).max()) > 1e-3


def test_isolated_atom_and_duplicate_bonds_match_reference() -> None:
    rng = np.random.default_rng(0)
    coords = rng.normal(size=(5, 3))
    bonds = [(0, 1), (1, 0), (1, 2), (2, 3), (0, 1)]  # atom 4 isolated, dups
    assert np.allclose(
        smooth_sheet(coords, bonds),
        _reference_smooth_sheet(coords, bonds),
        rtol=0,
        atol=1e-9,
    )
    assert np.array_equal(smooth_sheet(coords, [])[4], coords[4])
