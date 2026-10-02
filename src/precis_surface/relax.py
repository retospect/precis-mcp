"""Spring + umbrella relaxation of a fitted carbon net (smooth-drum slice).

Energy (arbitrary units, Å): harmonic bonds about ``bond_A``, harmonic 1-3
pairs about ``second_A`` (fixes the bond angles), and an umbrella term
``k_bend * |x_a - mean(neighbours)|^2`` on 3-coordinated atoms. Without the
umbrella the springs alone let pentagons cone out to a pyramidalisation of
~18 deg and saddles buckle; with it the net stays within the C60 bound.
Minimised by FIRE in numpy (scipy is not a core dependency).

Pure functions over arrays; unit-agnostic in form but the defaults are Å.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "angle_dev_by_atom",
    "energy_grad",
    "relax_net",
    "theta_p_by_atom",
    "theta_p_deg",
]

_F = NDArray[np.float64]
_I = NDArray[np.int64]


def _neighbours(n: int, bonds: _I) -> list[list[int]]:
    nbr: list[list[int]] = [[] for _ in range(n)]
    for i, j in bonds.tolist():
        nbr[i].append(j)
        nbr[j].append(i)
    return nbr


def _pairs_13(nbr: list[list[int]]) -> _I:
    out = [(a, b) for ns in nbr for k, a in enumerate(ns) for b in ns[k + 1 :]]
    return np.asarray(out, dtype=np.int64).reshape(-1, 2)


def _umbrella_sets(nbr: list[list[int]]) -> tuple[_I, _I]:
    centres = [a for a, ns in enumerate(nbr) if len(ns) == 3]
    return (
        np.asarray(centres, dtype=np.int64),
        np.asarray([nbr[a] for a in centres], dtype=np.int64).reshape(-1, 3),
    )


def _scatter(n: int, idx: _I, vec: _F) -> _F:
    return np.stack(
        [np.bincount(idx, weights=vec[:, k], minlength=n) for k in range(3)], axis=1
    )


def _spring(x: _F, pairs: _I, r0: float, k: float) -> tuple[float, _F]:
    v = x[pairs[:, 0]] - x[pairs[:, 1]]
    length = np.linalg.norm(v, axis=1)
    g = (2.0 * k * (length - r0) / np.maximum(length, 1e-12))[:, None] * v
    n = len(x)
    grad = _scatter(n, pairs[:, 0], g) - _scatter(n, pairs[:, 1], g)
    return float(k * ((length - r0) ** 2).sum()), grad


def energy_grad(
    x: _F,
    bonds: _I,
    p13: _I,
    centres: _I,
    around: _I,
    *,
    bond_A: float = 1.42,
    second_A: float = 2.46,
    k_bond: float = 1.0,
    k_second: float = 0.3,
    k_bend: float = 2.0,
    foot: tuple[_F, _F] | None = None,
    k_surface: float = 0.0,
) -> tuple[float, _F]:
    """Total energy and its analytic gradient ``(N, 3)``. ``foot`` =
    ``(p, n)``, both ``(N, 2)`` in ``(rho, z)``: per-atom foot point and
    unit normal of a target surface of revolution (see
    :func:`_surface_foot`); adds ``k_surface * d^2`` with ``d`` the signed
    distance of the atom's ``(rho, z)`` from that tangent line."""
    e1, g1 = _spring(x, bonds, bond_A, k_bond)
    e2, g2 = _spring(x, p13, second_A, k_second)
    grad = g1 + g2
    e3 = 0.0
    if len(centres):
        h = x[centres] - x[around].mean(axis=1)
        e3 = float(k_bend * (h**2).sum())
        n = len(x)
        grad = grad + _scatter(n, centres, 2.0 * k_bend * h)
        for c in range(3):
            grad = grad - _scatter(n, around[:, c], 2.0 * k_bend * h / 3.0)
    e4 = 0.0
    if foot is not None and k_surface > 0.0:
        p, nrm = foot
        rho = np.maximum(np.hypot(x[:, 0], x[:, 1]), 1e-12)
        dist = (rho - p[:, 0]) * nrm[:, 0] + (x[:, 2] - p[:, 1]) * nrm[:, 1]
        e4 = float(k_surface * (dist**2).sum())
        coef = (2.0 * k_surface * dist)[:, None]
        grad = grad + coef * np.stack(
            [nrm[:, 0] * x[:, 0] / rho, nrm[:, 0] * x[:, 1] / rho, nrm[:, 1]], axis=1
        )
    return e1 + e2 + e3 + e4, grad


def _surface_foot(x: _F, curve: _F) -> tuple[_F, _F]:
    """Nearest point on the polyline ``curve`` ``(M, 2)`` of ``(rho, z)``
    for every atom, and the unit normal of the segment it lies on."""
    q = np.stack([np.hypot(x[:, 0], x[:, 1]), x[:, 2]], axis=1)
    m = len(curve)
    j = np.empty(len(q), dtype=np.int64)
    for lo in range(0, len(q), 1024):
        dd = ((q[lo : lo + 1024, None, :] - curve[None, :, :]) ** 2).sum(axis=2)
        j[lo : lo + 1024] = dd.argmin(axis=1)
    best_d = np.full(len(q), np.inf)
    foot = np.zeros_like(q)
    nrm = np.zeros_like(q)
    for seg in (np.maximum(j - 1, 0), np.minimum(j, m - 2)):
        a, b = curve[seg], curve[seg + 1]
        t_vec = b - a
        t_len2 = np.maximum((t_vec**2).sum(axis=1), 1e-24)
        u = np.clip(((q - a) * t_vec).sum(axis=1) / t_len2, 0.0, 1.0)
        pt = a + u[:, None] * t_vec
        d2 = ((q - pt) ** 2).sum(axis=1)
        better = d2 < best_d
        best_d = np.where(better, d2, best_d)
        foot[better] = pt[better]
        unit = np.stack([-t_vec[:, 1], t_vec[:, 0]], axis=1) / np.sqrt(t_len2)[:, None]
        nrm[better] = unit[better]
    return foot, nrm


def relax_net(
    atoms: _F,
    bonds: _I,
    *,
    bond_A: float = 1.42,
    second_A: float = 2.46,
    k_bond: float = 1.0,
    k_second: float = 0.3,
    k_bend: float = 2.0,
    surface: _F | None = None,
    k_surface: float = 0.0,
    refresh: int = 25,
    fmax: float = 1e-3,
    max_steps: int = 4000,
    dt_start: float = 0.1,
    dt_max: float = 0.8,
    max_move_A: float = 0.1,
) -> tuple[_F, dict[str, float | int | bool]]:
    """FIRE-minimise the spring + umbrella energy; returns ``(xyz, info)``
    with ``steps``, ``energy``, ``fmax`` (final largest per-atom force) and
    ``converged``."""
    x = np.array(atoms, dtype=np.float64)
    b = np.asarray(bonds, dtype=np.int64).reshape(-1, 2)
    nbr = _neighbours(len(x), b)
    p13 = _pairs_13(nbr)
    centres, around = _umbrella_sets(nbr)

    # Optional normal-only tether to a target surface of revolution given as
    # a (rho, z) polyline ``surface``: atoms may slide along it (so the fit's
    # tangential strain relaxes) but not leave it. The foot point is
    # re-found every ``refresh`` steps -- the free relaxation of a strained
    # fit otherwise drifts the whole shape by several Å.
    use_surface = surface is not None and k_surface > 0.0
    curve = np.asarray(surface if surface is not None else [], dtype=np.float64)
    foot = _surface_foot(x, curve) if use_surface else None

    def ef(pos: _F) -> tuple[float, _F]:
        return energy_grad(
            pos,
            b,
            p13,
            centres,
            around,
            bond_A=bond_A,
            second_A=second_A,
            k_bond=k_bond,
            k_second=k_second,
            k_bend=k_bend,
            foot=foot,
            k_surface=k_surface,
        )

    # FIRE (Bitzek et al. 2006), unit masses.
    n_min, f_inc, f_dec, alpha0, f_alpha = 5, 1.1, 0.5, 0.1, 0.99
    dt, alpha, since_neg = dt_start, alpha0, 0
    v = np.zeros_like(x)
    e, g = ef(x)
    f = -g
    fm = float(np.linalg.norm(f, axis=1).max()) if len(x) else 0.0
    steps = 0
    while fm > fmax and steps < max_steps:
        power = float((f * v).sum())
        if power > 0.0:
            since_neg += 1
            fn = float(np.linalg.norm(f))
            vn = float(np.linalg.norm(v))
            v = (1.0 - alpha) * v + alpha * (vn / max(fn, 1e-30)) * f
            if since_neg > n_min:
                dt = min(dt * f_inc, dt_max)
                alpha *= f_alpha
        else:
            since_neg = 0
            dt *= f_dec
            alpha = alpha0
            v[:] = 0.0
        # semi-implicit Euler, step length capped per atom
        v = v + dt * f
        step = dt * v
        mag = np.linalg.norm(step, axis=1)
        scale = np.minimum(1.0, max_move_A / np.maximum(mag, 1e-30))
        x = x + step * scale[:, None]
        e, g = ef(x)
        f = -g
        fm = float(np.linalg.norm(f, axis=1).max())
        steps += 1
        if use_surface and steps % refresh == 0:
            foot = _surface_foot(x, curve)
            e, g = ef(x)
            f = -g
            fm = float(np.linalg.norm(f, axis=1).max())
    info: dict[str, float | int | bool] = {
        "steps": steps,
        "energy": float(e),
        "fmax": fm,
        "converged": bool(fm <= fmax),
    }
    return x, info


def _umbrella_units(xyz: _F, bonds: _I) -> tuple[_I, _F]:
    """The 3-coordinated atoms and, per atom, its three bond unit vectors
    (shape ``(m, 3, 3)``)."""
    b = np.asarray(bonds, dtype=np.int64).reshape(-1, 2)
    centres, around = _umbrella_sets(_neighbours(len(xyz), b))
    if not len(centres):
        return centres, np.zeros((0, 3, 3), dtype=np.float64)
    d = xyz[around] - xyz[centres][:, None, :]
    return centres, d / np.linalg.norm(d, axis=2, keepdims=True)


def theta_p_by_atom(xyz: _F, bonds: _I) -> _F:
    """POAV1 pyramidalisation (deg) for every atom, NaN where the atom is
    not 3-coordinated or its three bonds are collinear. The POAV axis ``v``
    makes equal angles with the three bond unit vectors ``u`` (``u @ v = 1``
    up to scale); theta_p is the angle between ``v`` and a bond minus 90
    deg."""
    out = np.full(len(xyz), np.nan, dtype=np.float64)
    centres, u = _umbrella_units(xyz, bonds)
    if not len(centres):
        return out
    # Cramer on u @ v = 1: v is proportional to n = u1 x u2 + u2 x u0 + u0 x u1
    # (the plane normal when the three bonds are coplanar), so a planar atom
    # needs no special case. theta_p = |angle(u0, v) - 90| = asin|u0 . n_hat|.
    n = (
        np.cross(u[:, 1], u[:, 2])
        + np.cross(u[:, 2], u[:, 0])
        + np.cross(u[:, 0], u[:, 1])
    )
    norm = np.linalg.norm(n, axis=1)
    ok = norm > 1e-12
    cosang = np.abs((u[ok, 0, :] * n[ok]).sum(axis=1)) / norm[ok]
    out[centres[ok]] = np.degrees(np.arcsin(np.clip(cosang, 0.0, 1.0)))
    return out


def theta_p_deg(xyz: _F, bonds: _I) -> _F:
    """POAV1 pyramidalisation (deg) per 3-coordinated atom, in atom-index
    order of those atoms; collinear triples are skipped
    (:func:`theta_p_by_atom` without its NaNs)."""
    t = theta_p_by_atom(xyz, bonds)
    return t[~np.isnan(t)]


def angle_dev_by_atom(xyz: _F, bonds: _I, ideal_deg: float = 120.0) -> _F:
    """RMS deviation (deg) of each 3-coordinated atom's three bond angles
    from ``ideal_deg``, NaN for every other atom. The in-plane counterpart
    of :func:`theta_p_by_atom`: a flat but distorted hexagon scores here and
    not there."""
    out = np.full(len(xyz), np.nan, dtype=np.float64)
    centres, u = _umbrella_units(xyz, bonds)
    if not len(centres):
        return out
    cos = np.stack(
        [
            (u[:, 0] * u[:, 1]).sum(axis=1),
            (u[:, 1] * u[:, 2]).sum(axis=1),
            (u[:, 2] * u[:, 0]).sum(axis=1),
        ],
        axis=1,
    )
    ang = np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))
    out[centres] = np.sqrt(((ang - ideal_deg) ** 2).mean(axis=1))
    return out
