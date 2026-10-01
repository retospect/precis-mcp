"""Compiled pair kernels for the structure package — numba ``@njit`` MIC loops.

:meth:`precis.structure.cell.Cell.mic` costs ~27 small numpy ops per pair; the
O(N²) callers (``probe.detect_bonds``/``coordination``, ``validate``'s overlap
rule, ``invariants._min_dist``, ``relax._relax_clean``) multiplied that by every
pair. These kernels reproduce its image search *exactly* — same per-axis base
shift ``-rint(d0)``, same ``(-1, 0, 1)`` block on periodic axes (``0`` only on
non-periodic ones), same ``na → nb → nc`` visit order with a strict ``<`` so
the first of equal-distance images wins — over plain float64 arrays.

Only this module imports numba (it is slow to import), and the structure
modules import *this* module lazily inside their functions so
``precis.cli.main`` never pays for it. Å-native like the rest of the package.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numba import njit

from . import elements


def cell_arrays(cell: Any) -> tuple[np.ndarray, np.ndarray]:
    """``(lattice, pbc)`` as the contiguous float64 / bool arrays the kernels take."""
    return (
        np.ascontiguousarray(cell.lattice, dtype=np.float64),
        np.array(cell.pbc, dtype=np.bool_),
    )


def pack_scene(scene: Any) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray]:
    """Pack a Scene once: ``(labels, frac (N,3), lattice, pbc)``."""
    labels = list(scene.atoms)
    frac = np.empty((len(labels), 3), dtype=np.float64)
    for k, label in enumerate(labels):
        frac[k] = scene.atoms[label].frac
    lat, pbc = cell_arrays(scene.cell)
    return labels, frac, lat, pbc


def radii_of(scene: Any, labels: list[str]) -> np.ndarray:
    """Per-atom covalent radius (Å), in ``labels`` order."""
    return np.array(
        [elements.covalent_radius(scene.atoms[la].element) for la in labels],
        dtype=np.float64,
    )


@njit(cache=True)
def _mic(
    fi: np.ndarray, fj: np.ndarray, lat: np.ndarray, pbc: np.ndarray
) -> tuple[float, int, int, int]:
    """Scalar MIC: ``(distance, ia, ib, ic)`` — mirrors ``Cell.mic`` exactly."""
    d0 = np.empty(3)
    base = np.zeros(3, dtype=np.int64)
    lo = np.zeros(3, dtype=np.int64)
    hi = np.zeros(3, dtype=np.int64)
    for ax in range(3):
        d0[ax] = fj[ax] - fi[ax]
        if pbc[ax]:
            base[ax] = -np.int64(np.rint(d0[ax]))
            lo[ax] = -1
            hi[ax] = 1
    best = np.inf
    b0 = np.int64(0)
    b1 = np.int64(0)
    b2 = np.int64(0)
    for na in range(lo[0], hi[0] + 1):
        i0 = base[0] + na
        v0 = d0[0] + i0
        for nb in range(lo[1], hi[1] + 1):
            i1 = base[1] + nb
            v1 = d0[1] + i1
            for nc in range(lo[2], hi[2] + 1):
                i2 = base[2] + nc
                v2 = d0[2] + i2
                x = v0 * lat[0, 0] + v1 * lat[1, 0] + v2 * lat[2, 0]
                y = v0 * lat[0, 1] + v1 * lat[1, 1] + v2 * lat[2, 1]
                z = v0 * lat[0, 2] + v1 * lat[1, 2] + v2 * lat[2, 2]
                d2 = x * x + y * y + z * z
                if d2 < best:
                    best = d2
                    b0 = i0
                    b1 = i1
                    b2 = i2
    return float(np.sqrt(best)), int(b0), int(b1), int(b2)


@njit(cache=True)
def mic_scalar(
    fi: np.ndarray, fj: np.ndarray, lat: np.ndarray, pbc: np.ndarray
) -> tuple[float, int, int, int]:
    """One pair's MIC (the ``Cell.mic`` delegate)."""
    return _mic(fi, fj, lat, pbc)


@njit(cache=True)
def row_mic(
    frac: np.ndarray, idx: int, lat: np.ndarray, pbc: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """MIC from atom ``idx`` to every atom: ``(d (N,), img (N,3))``."""
    n = frac.shape[0]
    d = np.empty(n)
    img = np.empty((n, 3), dtype=np.int64)
    for j in range(n):
        dj, a, b, c = _mic(frac[idx], frac[j], lat, pbc)
        d[j] = dj
        img[j, 0] = a
        img[j, 1] = b
        img[j, 2] = c
    return d, img


@njit(cache=True)
def pairs_within(
    frac: np.ndarray,
    lat: np.ndarray,
    pbc: np.ndarray,
    radii: np.ndarray,
    factor: float,
    inclusive: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Every pair ``i < j`` with MIC distance within ``(r_i + r_j) * factor``.

    ``inclusive`` → ``d <= cutoff`` (bond detection), else ``d < cutoff``
    (overlap floor). Returns ``(i, j, d, img)`` in row-major pair order.
    """
    n = frac.shape[0]
    cap = 64
    oi = np.empty(cap, dtype=np.int64)
    oj = np.empty(cap, dtype=np.int64)
    od = np.empty(cap)
    oimg = np.empty((cap, 3), dtype=np.int64)
    m = 0
    for i in range(n):
        for j in range(i + 1, n):
            d, a, b, c = _mic(frac[i], frac[j], lat, pbc)
            cut = (radii[i] + radii[j]) * factor
            hit = d <= cut if inclusive else d < cut
            if hit:
                if m == cap:
                    cap *= 2
                    ni = np.empty(cap, dtype=np.int64)
                    nj = np.empty(cap, dtype=np.int64)
                    nd = np.empty(cap)
                    nimg = np.empty((cap, 3), dtype=np.int64)
                    ni[:m] = oi[:m]
                    nj[:m] = oj[:m]
                    nd[:m] = od[:m]
                    nimg[:m] = oimg[:m]
                    oi, oj, od, oimg = ni, nj, nd, nimg
                oi[m] = i
                oj[m] = j
                od[m] = d
                oimg[m, 0] = a
                oimg[m, 1] = b
                oimg[m, 2] = c
                m += 1
    return oi[:m].copy(), oj[:m].copy(), od[:m].copy(), oimg[:m].copy()


@njit(cache=True)
def coordination_counts(
    frac: np.ndarray,
    lat: np.ndarray,
    pbc: np.ndarray,
    radii: np.ndarray,
    factor: float,
    counts_as_neighbor: np.ndarray,
) -> np.ndarray:
    """Per-atom neighbour count: others within ``(r_i + r_j) * factor`` (``<=``).

    Atom ``j`` only counts toward ``i`` when ``counts_as_neighbor[j]`` (all
    ``True`` → plain coordination; metals ``False`` → covalent coordination).
    Self is skipped by index, as ``probe.coordination`` skips by label.
    """
    n = frac.shape[0]
    out = np.zeros(n, dtype=np.int64)
    for i in range(n):
        for j in range(i + 1, n):
            if not (counts_as_neighbor[i] or counts_as_neighbor[j]):
                continue
            d, _a, _b, _c = _mic(frac[i], frac[j], lat, pbc)
            if d <= (radii[i] + radii[j]) * factor:
                if counts_as_neighbor[j]:
                    out[i] += 1
                if counts_as_neighbor[i]:
                    out[j] += 1
    return out


@njit(cache=True)
def min_distance(
    frac: np.ndarray, lat: np.ndarray, pbc: np.ndarray, cap: float
) -> float:
    """Smallest pair MIC distance, floored at ``cap`` (``min(cap, d)`` fold)."""
    n = frac.shape[0]
    best = cap
    for i in range(n):
        for j in range(i + 1, n):
            d, _a, _b, _c = _mic(frac[i], frac[j], lat, pbc)
            if d < best:
                best = d
    return best


@njit(cache=True)
def clean_displacements(
    frac: np.ndarray, lat: np.ndarray, pbc: np.ndarray, radii: np.ndarray
) -> np.ndarray:
    """One ``relax._relax_clean`` sweep's Cartesian displacement per atom.

    Pairs closer than 98 % of the covalent-radii sum push apart by half the
    deficit each; accumulated in the original ``i < j`` order.
    """
    n = frac.shape[0]
    disp = np.zeros((n, 3))
    for i in range(n):
        for j in range(i + 1, n):
            d, a, b, c = _mic(frac[i], frac[j], lat, pbc)
            target = radii[i] + radii[j]
            if 1e-6 < d < target * 0.98:
                v0 = frac[j, 0] + a - frac[i, 0]
                v1 = frac[j, 1] + b - frac[i, 1]
                v2 = frac[j, 2] + c - frac[i, 2]
                push = (target - d) * 0.5
                for k in range(3):
                    vk = v0 * lat[0, k] + v1 * lat[1, k] + v2 * lat[2, k]
                    u = vk / d
                    disp[i, k] -= u * push
                    disp[j, k] += u * push
    return disp


# error_model="numpy": a coincident atom pair divides by zero → NaN, as the old
# numpy loop did, instead of raising ZeroDivisionError.
@njit(cache=True, error_model="numpy")
def relax_graph_loop(
    coords: np.ndarray,
    bonds: np.ndarray,
    bond_target: np.ndarray,
    radii: np.ndarray,
    bonded: np.ndarray,
    movable: np.ndarray,
    tri: np.ndarray,
    tri_theta0: np.ndarray,
    iters: int,
    step: float,
    angle_step: float,
    angle_k: float,
    repulsion_margin: float,
    tol: float,
    use_tol: bool,
) -> tuple[bool, int, np.ndarray]:
    """The ``georelax.relax_graph`` iteration loop, in place on ``coords``.

    Returns ``(converged, step_count, curve)`` where ``curve`` holds the raw
    (unrounded) per-iteration max displacement; the caller rounds. Same
    per-iteration order: bond springs, non-bond repulsion, angle terms.
    """
    n = coords.shape[0]
    nb = bonds.shape[0]
    nt = tri.shape[0]
    curve = np.zeros(iters)
    converged = False
    step_count = 0
    disp = np.zeros((n, 3))
    d = np.empty(3)
    ri = np.empty(3)
    rj = np.empty(3)
    for it in range(1, iters + 1):
        step_count = it
        disp[:] = 0.0
        for e in range(nb):
            i = bonds[e, 0]
            j = bonds[e, 1]
            s = 0.0
            for k in range(3):
                d[k] = coords[j, k] - coords[i, k]
                s += d[k] * d[k]
            dist = np.sqrt(s)
            if dist < 1e-9:
                continue
            f = step * (dist - bond_target[e])
            for k in range(3):
                fk = f * (d[k] / dist)
                disp[i, k] += fk * movable[i]
                disp[j, k] -= fk * movable[j]
        for i in range(n):
            for j in range(i + 1, n):
                if bonded[i, j]:
                    continue
                cutoff = 1.2 * (radii[i] + radii[j]) * repulsion_margin
                s = 0.0
                for k in range(3):
                    d[k] = coords[j, k] - coords[i, k]
                    s += d[k] * d[k]
                dist = np.sqrt(s)
                if dist >= cutoff or dist < 1e-9:
                    continue
                f = step * (cutoff - dist)
                for k in range(3):
                    fk = f * (d[k] / dist)
                    disp[i, k] -= fk * movable[i]
                    disp[j, k] += fk * movable[j]
        for t in range(nt):
            i = tri[t, 0]
            kk = tri[t, 1]
            j = tri[t, 2]
            dki = 0.0
            dkj = 0.0
            dot = 0.0
            for k in range(3):
                ri[k] = coords[i, k] - coords[kk, k]
                rj[k] = coords[j, k] - coords[kk, k]
                dki += ri[k] * ri[k]
                dkj += rj[k] * rj[k]
                dot += ri[k] * rj[k]
            d_ki = np.sqrt(dki)
            d_kj = np.sqrt(dkj)
            cos_t = dot / (d_ki * d_kj)
            if cos_t > 1.0:
                cos_t = 1.0
            elif cos_t < -1.0:
                cos_t = -1.0
            theta = np.arccos(cos_t)
            sin_t = max(np.sin(theta), 1e-3)
            f = angle_step * angle_k * (theta - tri_theta0[t])
            for k in range(3):
                gi = rj[k] / (d_ki * d_kj) - cos_t * ri[k] / (d_ki**2)
                gj = ri[k] / (d_ki * d_kj) - cos_t * rj[k] / (d_kj**2)
                gk = -(gi + gj)
                disp[i, k] -= f * (-gi / sin_t) * movable[i]
                disp[j, k] -= f * (-gj / sin_t) * movable[j]
                disp[kk, k] -= f * (-gk / sin_t) * movable[kk]
        max_disp = 0.0
        for i in range(n):
            s = 0.0
            for k in range(3):
                coords[i, k] += disp[i, k]
                s += disp[i, k] * disp[i, k]
            m = np.sqrt(s)
            # NaN propagates (old ``np.max`` semantics): never converges on NaN
            if np.isnan(m):
                max_disp = np.nan
            elif m > max_disp:
                max_disp = m
        curve[it - 1] = max_disp
        if use_tol and max_disp < tol:
            converged = True
            break
    return converged, step_count, curve[:step_count].copy()
