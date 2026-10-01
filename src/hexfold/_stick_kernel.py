"""numba kernel for :func:`hexfold.stick.stick_relax_pinned`.

The only hexfold module that imports numba; ``stick`` imports it lazily
so ``import hexfold`` stays light.  The loop is a scalar transcription of
the numpy version, in the *same* floating-point operation order
(``np.add.at`` applies updates sequentially, all ``ii`` scatters before all
``jj`` scatters), so results are bit-identical to it, including NaN
propagation (``error_model='numpy'``: x/0 -> inf/NaN, never a raise).
"""

from __future__ import annotations

import numpy as np
from numba import njit


@njit(cache=True, error_model="numpy")
def _rep_pairs_nb(pos, bonded, lim):
    n = pos.shape[0]
    cnt = 0
    for i in range(n):
        for j in range(i, n):
            if bonded[i, j]:
                continue
            dx = pos[j, 0] - pos[i, 0]
            dy = pos[j, 1] - pos[i, 1]
            dz = pos[j, 2] - pos[i, 2]
            r = np.sqrt(dx * dx + dy * dy + dz * dz)
            if r < lim and r > 0.0:
                cnt += 1
    out = np.empty((cnt, 2), dtype=np.int64)
    c = 0
    for i in range(n):
        for j in range(i, n):
            if bonded[i, j]:
                continue
            dx = pos[j, 0] - pos[i, 0]
            dy = pos[j, 1] - pos[i, 1]
            dz = pos[j, 2] - pos[i, 2]
            r = np.sqrt(dx * dx + dy * dy + dz * dz)
            if r < lim and r > 0.0:
                out[c, 0] = i
                out[c, 1] = j
                c += 1
    return out


@njit(cache=True, error_model="numpy")
def _spring_nb(pos, ii, jj, rest, k, out, fbuf):
    m = ii.shape[0]
    for q in range(m):
        dx = pos[jj[q], 0] - pos[ii[q], 0]
        dy = pos[jj[q], 1] - pos[ii[q], 1]
        dz = pos[jj[q], 2] - pos[ii[q], 2]
        r = np.sqrt(dx * dx + dy * dy + dz * dz)
        if r == 0.0:
            r = 1e-9
        s = k * (r - rest[q])
        fbuf[q, 0] = s * dx / r
        fbuf[q, 1] = s * dy / r
        fbuf[q, 2] = s * dz / r
    out[:, :] = 0.0
    for q in range(m):
        for c in range(3):
            out[ii[q], c] += fbuf[q, c]
    for q in range(m):
        for c in range(3):
            out[jj[q], c] += -fbuf[q, c]


@njit(cache=True, error_model="numpy")
def relax_kernel(
    pos,
    bonds,
    brest,
    si,
    sj,
    srest,
    bonded,
    mv,
    sigma,
    iters,
    dt,
    k_bond,
    k_angle,
    k_rep,
    rep_cut,
    refresh,
    rep_margin,
):
    """Relax ``pos`` in place; return the final max force magnitude."""
    n = pos.shape[0]
    nb = bonds.shape[0]
    ns = si.shape[0]
    bi = np.ascontiguousarray(bonds[:, 0])
    bj = np.ascontiguousarray(bonds[:, 1])
    lim = rep_margin * rep_cut * sigma
    cut = rep_cut * sigma
    pairs = _rep_pairs_nb(pos, bonded, lim)
    f = np.zeros((n, 3))
    f2 = np.empty((n, 3))
    fb1 = np.empty((nb, 3))
    fb2 = np.empty((ns, 3))
    for it in range(iters):
        if it % refresh == 0:
            pairs = _rep_pairs_nb(pos, bonded, lim)
        _spring_nb(pos, bi, bj, brest, k_bond, f, fb1)
        _spring_nb(pos, si, sj, srest, k_angle, f2, fb2)
        f += f2
        npair = pairs.shape[0]
        if npair > 0:
            rf = np.empty((npair, 3))
            keep = np.zeros(npair, dtype=np.bool_)
            for q in range(npair):
                a = pairs[q, 0]
                b = pairs[q, 1]
                dx = pos[b, 0] - pos[a, 0]
                dy = pos[b, 1] - pos[a, 1]
                dz = pos[b, 2] - pos[a, 2]
                r = np.sqrt(dx * dx + dy * dy + dz * dz)
                if r < cut:
                    keep[q] = True
                    if r == 0.0:
                        r = 1e-9
                    s = -k_rep * (cut - r)
                    rf[q, 0] = s * dx / r
                    rf[q, 1] = s * dy / r
                    rf[q, 2] = s * dz / r
            for q in range(npair):
                if keep[q]:
                    for c in range(3):
                        f[pairs[q, 0], c] += rf[q, c]
            for q in range(npair):
                if keep[q]:
                    for c in range(3):
                        f[pairs[q, 1], c] += -rf[q, c]
        for a in range(n):
            for c in range(3):
                f[a, c] *= mv[a]
        for a in range(n):
            for c in range(3):
                pos[a, c] += dt * f[a, c]
    # np.max semantics: NaN propagates
    best = 0.0
    isnan = False
    for a in range(n):
        m = np.sqrt(f[a, 0] * f[a, 0] + f[a, 1] * f[a, 1] + f[a, 2] * f[a, 2])
        if m != m:
            isnan = True
        elif a == 0 or m > best:
            best = m
    if isnan:
        best = np.nan
    return best
