"""Cell-centred finite-volume assembly on a :class:`Domain`.

The operator taken out of ``flow.solve`` (docs/backlog/se-multiphysics.md
shared machinery): pressure (``T = h³``), conduction (``hk``) and species
(``h𝔻``) all assemble the same way — harmonic-mean face transmissivities,
Dirichlet / no-flux / prescribed-flux boundary cells, a direct sparse
solve.

- :func:`elliptic` — ``div*(T* grad* φ) = 0`` with Dirichlet cells: the
  slice-1 pressure problem, ported verbatim from ``flow.solve`` (identical
  assembly order, so callers reproduce the old matrix to round-off).
- :func:`transport_matrix` — ``div*(F φ − T grad* φ) + s φ = rhs``:
  advection plus anisotropic (tensor) diffusion plus a per-cell sink, the
  species equation's shape. The tensor cross term needs the 9-point
  stencil: a face's tangential gradient is averaged from the two cells'
  centre differences, which couples the corner cells.
- :func:`solve_transport` — steady assembly + ``spsolve``.
- :func:`crank_nicolson` — transient steps of
  ``M ∂φ/∂t* + A φ = b`` with ``M`` the per-cell capacity (``h*``).

Reduced units throughout (``x*``, ``t*``); nothing dimensional enters.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

from .domain import Domain


def _harmonic(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Harmonic mean of two non-negative arrays (0 where either is 0)."""
    s = a + b
    return np.where(s > 0, 2.0 * a * b / np.where(s > 0, s, 1.0), 0.0)


def field_index(domain: Domain) -> np.ndarray:
    """(ny, nx) int row index per inside cell, -1 outside."""
    idx = -np.ones(domain.shape, dtype=int)
    idx[domain.mask] = np.arange(int(domain.mask.sum()))
    return idx


def elliptic(
    domain: Domain,
    t_field: np.ndarray,
    dirichlet: list[tuple[np.ndarray, float]],
) -> np.ndarray:
    """Solve ``div*(T* grad* φ) = 0`` with Dirichlet cells.

    ``dirichlet`` is a list of ``(mask, value)`` port conditions — the
    pressure solve's ``p* = 1`` inlet / ``p* = 0`` outlet. Faces to outside
    cells carry zero transmissivity (natural no-flux walls). Returns
    ``phi`` (NaN outside).
    """
    ny, nx = domain.shape
    inside = domain.mask
    dmask = np.zeros((ny, nx), dtype=bool)
    dvals = np.zeros((ny, nx), dtype=float)
    for mask, value in dirichlet:
        m = np.asarray(mask, dtype=bool)
        dmask |= m
        dvals = np.where(m, float(value), dvals)

    idx = field_index(domain)
    n = int(inside.sum())
    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    b = np.zeros(n)

    tx = _harmonic(t_field, np.roll(t_field, -1, axis=1))
    tx[:, -1] = 0.0
    ty = _harmonic(t_field, np.roll(t_field, -1, axis=0))
    ty[-1, :] = 0.0

    jj, ii = np.where(inside)
    for j, i in zip(jj.tolist(), ii.tolist()):
        k = idx[j, i]
        if dmask[j, i]:
            rows.append(k)
            cols.append(k)
            data.append(1.0)
            b[k] = dvals[j, i]
            continue
        diag = 0.0
        faces = (
            (j, i - 1, tx[j, i - 1] if i - 1 >= 0 else 0.0),
            (j, i + 1, tx[j, i]),
            (j - 1, i, ty[j - 1, i] if j - 1 >= 0 else 0.0),
            (j + 1, i, ty[j, i]),
        )
        for nj, ni, tf in faces:
            if tf <= 0.0 or nj < 0 or ni < 0 or nj >= ny or ni >= nx:
                continue
            if not inside[nj, ni]:
                continue
            diag += tf
            rows.append(k)
            cols.append(idx[nj, ni])
            data.append(-tf)
        if diag == 0.0:
            diag = 1.0  # isolated cell -> pin to 0 to keep matrix non-singular
        rows.append(k)
        cols.append(k)
        data.append(diag)

    a = sp.csr_matrix((data, (rows, cols)), shape=(n, n))
    phi_vec = spla.spsolve(a.tocsc(), b)
    phi = np.full((ny, nx), np.nan)
    phi[inside] = phi_vec
    return phi


class _Builder:
    """Sparse (A, b) accumulator over the domain's inside cells."""

    def __init__(self, domain: Domain) -> None:
        self.domain = domain
        self.idx = field_index(domain)
        self.n = int(domain.mask.sum())
        self.rows: list[int] = []
        self.cols: list[int] = []
        self.data: list[float] = []
        self.b = np.zeros(self.n)

    def add(self, j: int, i: int, col: int, coef: float) -> None:
        if coef == 0.0 or col < 0:
            return
        self.rows.append(self.idx[j, i])
        self.cols.append(col)
        self.data.append(coef)

    def matrix(self) -> tuple[sp.csr_matrix, np.ndarray]:
        return (
            sp.csr_matrix((self.data, (self.rows, self.cols)), shape=(self.n, self.n)),
            self.b,
        )


def transport_matrix(
    domain: Domain,
    *,
    flux: np.ndarray,
    tensor: np.ndarray,
    sink: np.ndarray,
    inlet: np.ndarray,
    inlet_value: float,
    outlet: np.ndarray,
    rhs: np.ndarray | None = None,
    limiter: bool = True,
) -> tuple[sp.csr_matrix, np.ndarray, float, bool]:
    """Assemble ``div*(F c − T grad* c) + s c = rhs`` in conservative form.

    - ``flux`` ``(2, ny, nx)``: the cell carrier-flux field ``F* = h* U*``.
      Interior faces take the arithmetic mean of the two cells; the
      advected face value is central (second order) unless the local face
      Péclet ``|F_n| dx* / T_nn`` exceeds 2 *and* ``limiter`` is on, where
      the upwind donor value is used instead (the spec's TVD fallback).
    - ``tensor`` ``(ny, nx, 2, 2)``: the cell transmissivity ``h* 𝔻*`` —
      the off-diagonal cross term is what needs the 9-point stencil.
    - ``sink`` ``(ny, nx)``: per-cell first-order loss coefficient on c.
    - ``inlet`` ``(ny, nx)`` bool: Danckwerts cells — each boundary face
      whose carrier flux points inward prescribes the *combined* influx
      ``(F·n̂) c_in`` (the Robin statement ``T ∂c/∂n̂ = (F·n̂)(c − c_in)``
      rearranged; the face's total flux is exactly ``(F·n̂) c_in`` either
      way).
    - ``outlet`` ``(ny, nx)`` bool: zero-gradient outflow — a boundary face
      carries the advective flux ``(F·n̂) c_P`` only.
    - other boundary faces carry nothing (no-flux walls).

    Per-cell budget: face terms carry a ``1/dx*`` (face area dx*, cell
    volume dx*²) and ``sink`` a ``1`` (it is already the per-volume
    coefficient — the equation as written is per unit area of the
    footprint).

    Returns ``(A, b, cell_pe_max, limiter_active)``.
    """
    ny, nx = domain.shape
    dx = domain.dx_star
    inside = domain.mask
    inlet = np.asarray(inlet, dtype=bool)
    outlet = np.asarray(outlet, dtype=bool)
    sink = np.asarray(sink, dtype=float)
    fx = np.asarray(flux[0], dtype=float)
    fy = np.asarray(flux[1], dtype=float)
    txx = np.asarray(tensor[:, :, 0, 0], dtype=float)
    txy = np.asarray(tensor[:, :, 0, 1], dtype=float)
    tyx = np.asarray(tensor[:, :, 1, 0], dtype=float)
    tyy = np.asarray(tensor[:, :, 1, 1], dtype=float)

    bld = _Builder(domain)

    # Vectorised face quantities in +x / +y (arithmetic carrier mean,
    # harmonic transmissivity, arithmetic tangential coefficient).
    fx_face = 0.5 * (fx + np.roll(fx, -1, axis=1))
    fy_face = 0.5 * (fy + np.roll(fy, -1, axis=0))
    tx_face = _harmonic(txx, np.roll(txx, -1, axis=1))
    ty_face = _harmonic(tyy, np.roll(tyy, -1, axis=0))
    txy_face = 0.5 * (txy + np.roll(txy, -1, axis=1))
    tyx_face = 0.5 * (tyx + np.roll(tyx, -1, axis=0))

    # Face Peclet |F_n| dx* / T_nn (0 where the face carries no diffusion).
    pe_x = np.where(
        tx_face > 0, np.abs(fx_face) * dx / np.where(tx_face > 0, tx_face, 1.0), 0.0
    )
    pe_y = np.where(
        ty_face > 0, np.abs(fy_face) * dx / np.where(ty_face > 0, ty_face, 1.0), 0.0
    )
    cell_pe_max = float(
        max(pe_x[inside].max(initial=0.0), pe_y[inside].max(initial=0.0))
    )
    upx = (pe_x > 2.0) if limiter else np.zeros((ny, nx), dtype=bool)
    upy = (pe_y > 2.0) if limiter else np.zeros((ny, nx), dtype=bool)
    limiter_active = bool((upx | upy)[inside].any())

    jj, ii = np.where(inside)
    for j, i in zip(jj.tolist(), ii.tolist()):
        diag = 0.0
        if rhs is not None:
            bld.b[bld.idx[j, i]] += float(rhs[j, i])
        s = float(sink[j, i])
        if s:
            diag += s
        # ── interior faces ──────────────────────────────────────────
        # (neighbour, face-index into the face arrays, is_x)
        for nj, ni, fj, fk, is_x in (
            (j, i - 1, j, i - 1, True),
            (j, i + 1, j, i, True),
            (j - 1, i, j - 1, i, False),
            (j + 1, i, j, i, False),
        ):
            if nj < 0 or ni < 0 or nj >= ny or ni >= nx or not inside[nj, ni]:
                continue
            f_n = (fx_face if is_x else fy_face)[fj, fk]
            t_nn = (tx_face if is_x else ty_face)[fj, fk]
            t_tn = (txy_face if is_x else tyx_face)[fj, fk]
            sign = 1.0 if (ni == i + 1 or nj == j + 1) else -1.0
            f_out = sign * f_n  # carrier flux leaving cell (j, i)
            # diffusive normal part: T_nn (c_N - c_P) / dx²
            if t_nn > 0.0:
                coef = t_nn / (dx * dx)
                diag += coef
                bld.add(j, i, bld.idx[nj, ni], -coef)
            # tangential cross term: q += sign * T_tn * dphi/dtan|face,
            # the face gradient averaged from the two cells' central
            # differences (the 9-point corner coupling).
            if t_tn != 0.0:
                tc = sign * t_tn
                for cj, ci, w in ((j, i, 0.5), (nj, ni, 0.5)):
                    for nb, sgn in ((1, 1.0), (-1, -1.0)):
                        qj, qi = (cj + nb, ci) if is_x else (cj, ci + nb)
                        if not (0 <= qj < ny and 0 <= qi < nx and inside[qj, qi]):
                            # Corner outside the domain: drop the term. A
                            # stand-in would break the face's antisymmetry
                            # with the neighbour row — the leaked remainder
                            # shows up as a nonzero mass residual.
                            continue
                        coef = tc * w * sgn / (2.0 * dx * dx)
                        bld.add(j, i, bld.idx[qj, qi], coef)
            # advection: central unless the face Peclet trips the limiter
            if f_out != 0.0:
                if (upx if is_x else upy)[fj, fk]:
                    if f_out > 0:
                        diag += f_out / dx
                    else:
                        bld.add(j, i, bld.idx[nj, ni], f_out / dx)
                else:
                    diag += 0.5 * f_out / dx
                    bld.add(j, i, bld.idx[nj, ni], 0.5 * f_out / dx)
        # ── boundary faces ──────────────────────────────────────────
        for bj, bi, q_out in (
            (j, i - 1, -fx[j, i]),
            (j, i + 1, fx[j, i]),
            (j - 1, i, -fy[j, i]),
            (j + 1, i, fy[j, i]),
        ):
            if 0 <= bj < ny and 0 <= bi < nx and inside[bj, bi]:
                continue
            if inlet[j, i] and q_out < 0.0:
                # prescribed total influx (F·n̂ < 0 ⇒ inflow):
                # outward flux = q_out * c_in (negative) → move to b
                bld.b[bld.idx[j, i]] += (-q_out) * inlet_value / dx
            elif outlet[j, i] and q_out > 0.0:
                # zero-gradient outflow: advective flux only, c_face = c_P
                diag += q_out / dx
            # else: no-flux wall face — nothing
        if diag == 0.0 and s == 0.0:
            diag = 1e-12  # never a singular isolated row
        bld.add(j, i, bld.idx[j, i], diag)

    a, b = bld.matrix()
    return a, b, cell_pe_max, limiter_active


def solve_transport(domain: Domain, **kw: Any) -> tuple[np.ndarray, float, bool]:
    """Steady :func:`transport_matrix` + ``spsolve``.

    Returns ``(phi, cell_pe_max, limiter_active)`` with phi NaN outside.
    """
    a, b, pe, lim = transport_matrix(domain, **kw)
    vec = spla.spsolve(a.tocsc(), b)
    phi = np.full(domain.shape, np.nan)
    phi[domain.mask] = vec
    return phi, pe, lim


def crank_nicolson(
    domain: Domain,
    mass: np.ndarray,
    a: sp.csr_matrix,
    b: np.ndarray,
    dt: float,
    steps: int,
    phi0: np.ndarray,
    observer=None,
) -> np.ndarray:
    """Transient ``M dφ/dt* + A φ = b`` steps; returns final φ (NaN outside).

    ``mass`` is the per-cell capacity field (``h*`` for species); ``b`` is
    held constant over the run — a pulse inlet is cycled by rebuilding ``b``
    per step upstream of this helper. ``observer`` (optional callable) gets
    ``(step, phi_full)`` after each step.
    """
    inside = domain.mask
    m = np.where(inside, np.asarray(mass, dtype=float), 0.0)[inside] / dt
    mmat = sp.diags(m, format="csc")
    lhs = mmat + 0.5 * a.tocsc()
    rhs_op = mmat - 0.5 * a.tocsr()
    lu = spla.splu(lhs)
    phi = np.nan_to_num(np.asarray(phi0, dtype=float), nan=0.0)[inside].copy()
    for step in range(steps):
        phi = lu.solve(rhs_op @ phi + b)
        if observer is not None:
            full = np.full(domain.shape, np.nan)
            full[inside] = phi
            observer(step + 1, full)
    out = np.full(domain.shape, np.nan)
    out[inside] = phi
    return out
