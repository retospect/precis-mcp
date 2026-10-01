"""Parity of the numba ``stick_relax_pinned`` kernel against the numpy loop.

``_ref_*`` below is the pre-kernel implementation copied verbatim.  The
kernel mirrors its floating-point operation order, so the bar here is
tight (coords <= 1e-9, max_force likewise, NaN positions identical).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hexfold.build import build
from hexfold.stick import _angle_springs, stick_relax_pinned

_EXAMPLES = Path(__file__).resolve().parent.parent / "hexfold" / "examples"

_ITERS = 3000
_DT = 0.05
_K_BOND = 1.0
_K_ANGLE = 0.5
_K_REP = 0.1
_REP_CUT = 1.3
_REFRESH = 20
_REP_MARGIN = 2.0


def _ref_spring_forces(pos, ii, jj, rest, k):
    d = pos[jj] - pos[ii]
    r = np.linalg.norm(d, axis=1)
    r = np.where(r == 0.0, 1e-9, r)
    f = k * (r - rest)[:, None] * d / r[:, None]
    out = np.zeros_like(pos)
    np.add.at(out, ii, f)
    np.add.at(out, jj, -f)
    return out


def _ref_rep_pairs(pos, bonded, lim):
    d = pos[None, :, :] - pos[:, None, :]
    r = np.linalg.norm(d, axis=2)
    mask = (r < lim) & (r > 0.0) & ~bonded
    ii, jj = np.nonzero(np.triu(mask))
    return np.stack([ii, jj], axis=1)


def _ref_relax(pos, bonds, brest, springs, sigma, *, iters=_ITERS, movable=None):
    pos = pos.copy()
    n = len(pos)
    mv = movable if movable is not None else np.ones(n, dtype=np.float64)
    bonded = np.zeros((n, n), dtype=bool)
    bonded[bonds[:, 0], bonds[:, 1]] = True
    bonded[bonds[:, 1], bonds[:, 0]] = True
    si = springs[:, 0].astype(np.int64)
    sj = springs[:, 1].astype(np.int64)
    bonded[si, sj] = True
    bonded[sj, si] = True
    srest = springs[:, 2]

    pairs = _ref_rep_pairs(pos, bonded, _REP_MARGIN * _REP_CUT * sigma)
    f = np.zeros_like(pos)
    for it in range(iters):
        if it % _REFRESH == 0:
            pairs = _ref_rep_pairs(pos, bonded, _REP_MARGIN * _REP_CUT * sigma)
        f = _ref_spring_forces(pos, bonds[:, 0], bonds[:, 1], brest, _K_BOND)
        f += _ref_spring_forces(pos, si, sj, srest, _K_ANGLE)
        if len(pairs):
            pi, pj = pairs[:, 0], pairs[:, 1]
            d = pos[pj] - pos[pi]
            r = np.linalg.norm(d, axis=1)
            near = r < _REP_CUT * sigma
            pi, pj, d = pi[near], pj[near], d[near]
            r = r[near]
            r = np.where(r == 0.0, 1e-9, r)
            rf = -_K_REP * (_REP_CUT * sigma - r)[:, None] * d / r[:, None]
            np.add.at(f, pi, rf)
            np.add.at(f, pj, -rf)
        f *= mv[:, None]
        pos += _DT * f
    max_force = float(np.linalg.norm(f, axis=1).max()) if len(f) else 0.0
    return pos, max_force


def _same(ref, got, tol=1e-9):
    (rp, rm), (gp, gm) = ref, got
    assert rp.shape == gp.shape
    np.testing.assert_array_equal(np.isnan(rp), np.isnan(gp))
    ok = ~np.isnan(rp)
    assert np.max(np.abs(rp[ok] - gp[ok]), initial=0.0) <= tol
    if np.isnan(rm):
        assert np.isnan(gm)
    else:
        assert abs(rm - gm) <= tol * max(1.0, abs(rm))


def _random_case(seed: int, n: int):
    rng = np.random.default_rng(seed)
    pos = rng.normal(scale=2.0, size=(n, 3))
    chain = np.stack([np.arange(n - 1), np.arange(1, n)], axis=1).astype(np.int64)
    extra = rng.integers(0, n, size=(n // 2, 2)).astype(np.int64)
    extra = extra[extra[:, 0] != extra[:, 1]]
    bonds = np.concatenate([chain, extra])
    brest = rng.uniform(1.2, 1.6, size=len(bonds))
    k = n
    springs = np.stack(
        [rng.integers(0, n, k), rng.integers(0, n, k), rng.uniform(2.0, 2.6, k)],
        axis=1,
    )
    return pos, bonds, brest, springs, 1.42


@pytest.mark.parametrize("seed,n", [(0, 12), (1, 40), (2, 90)])
def test_random_parity_with_mask(seed: int, n: int) -> None:
    pos, bonds, brest, springs, sig = _random_case(seed, n)
    mv = (np.random.default_rng(seed + 7).random(n) > 0.4).astype(np.float64)
    ref = _ref_relax(pos, bonds, brest, springs, sig, iters=400, movable=mv)
    got = stick_relax_pinned(pos, bonds, brest, springs, sig, iters=400, movable=mv)
    _same(ref, got)
    # pinned atoms exactly in place
    assert np.array_equal(got[0][mv == 0.0], pos[mv == 0.0])


@pytest.mark.parametrize(
    "example", ["pillar.hx", "nanobud_87.hx", "tube55.hx", "sheet_sw.hx", "cone5.hx"]
)
def test_real_build_parity(example: str) -> None:
    net = build((_EXAMPLES / example).read_text(encoding="utf-8"), strict=False)
    assert net.seed3 is not None
    pos0 = np.array(net.seed3, dtype=np.float64)
    sig = net.lattice.sigma_A
    bonds = np.array([(i, j) for i, j, _ in net.bonds], dtype=np.int64)
    lat_el = set(net.lattice.elements)
    brest = np.array(
        [
            sig
            if net.atoms[i].element in lat_el and net.atoms[j].element in lat_el
            else net.lattice.sigma_CH_A
            for i, j, _ in net.bonds
        ],
        dtype=np.float64,
    )
    springs = np.array(_angle_springs(net), dtype=np.float64).reshape(-1, 3)
    ref = _ref_relax(pos0, bonds, brest, springs, sig, iters=300)
    got = stick_relax_pinned(pos0, bonds, brest, springs, sig, iters=300)
    _same(ref, got)


def test_full_3000_iterations_real_build() -> None:
    net = build((_EXAMPLES / "pillar.hx").read_text(encoding="utf-8"), strict=False)
    assert net.seed3 is not None
    pos0 = np.array(net.seed3, dtype=np.float64)
    sig = net.lattice.sigma_A
    bonds = np.array([(i, j) for i, j, _ in net.bonds], dtype=np.int64)
    brest = np.full(len(bonds), sig)
    springs = np.array(_angle_springs(net), dtype=np.float64).reshape(-1, 3)
    _same(
        _ref_relax(pos0, bonds, brest, springs, sig),
        stick_relax_pinned(pos0, bonds, brest, springs, sig),
    )


def test_coincident_points_degenerate() -> None:
    pos = np.zeros((6, 3))
    pos[3:] = [[1.0, 0, 0], [1.0, 0, 0], [0, 2.0, 0]]
    bonds = np.array([[0, 1], [3, 4], [2, 5]], dtype=np.int64)
    brest = np.full(3, 1.4)
    springs = np.array([[0, 2, 2.4], [3, 5, 2.4]])
    ref = _ref_relax(pos, bonds, brest, springs, 1.42, iters=100)
    got = stick_relax_pinned(pos, bonds, brest, springs, 1.42, iters=100)
    _same(ref, got)


def test_nan_input_propagates_like_numpy() -> None:
    pos, bonds, brest, springs, sig = _random_case(5, 20)
    pos[3, 1] = np.nan
    with np.errstate(all="ignore"):
        ref = _ref_relax(pos, bonds, brest, springs, sig, iters=60)
    got = stick_relax_pinned(pos, bonds, brest, springs, sig, iters=60)
    _same(ref, got)
    assert np.isnan(got[1])


def test_zero_iters_and_empty() -> None:
    pos, bonds, brest, springs, sig = _random_case(3, 10)
    _same(
        _ref_relax(pos, bonds, brest, springs, sig, iters=0),
        stick_relax_pinned(pos, bonds, brest, springs, sig, iters=0),
    )
    out, mf = stick_relax_pinned(
        np.zeros((0, 3)),
        np.zeros((0, 2), dtype=np.int64),
        np.zeros(0),
        np.zeros((0, 3)),
        1.42,
        iters=5,
    )
    assert out.shape == (0, 3) and mf == 0.0


def test_readonly_noncontiguous_inputs_not_mutated() -> None:
    pos, bonds, brest, springs, sig = _random_case(4, 30)
    big = np.zeros((30, 6))
    big[:, ::2] = pos
    pos_nc = big[:, ::2]  # non-contiguous view
    assert not pos_nc.flags.c_contiguous
    pos_ro = pos.copy()
    pos_ro.flags.writeable = False
    bonds_ro = bonds.copy()
    bonds_ro.flags.writeable = False
    springs_f = np.asfortranarray(springs)
    mv = np.ones(30)[::1]
    mv_ro = mv.copy()
    mv_ro.flags.writeable = False
    ref = _ref_relax(pos, bonds, brest, springs, sig, iters=100)
    snap = pos_nc.copy()
    for p, b, s in [
        (pos_nc, bonds, springs),
        (pos_ro, bonds_ro, springs_f),
    ]:
        got = stick_relax_pinned(p, b, brest, s, sig, iters=100, movable=mv_ro)
        _same(ref, got)
    assert np.array_equal(pos_nc, snap)
    assert np.array_equal(pos_ro, pos)
    assert got[0] is not pos_ro
