"""Hele-Shaw / Darcy depth-averaged flow on the reduced domain.

Solves ``div*(T* grad* p*) = 0`` with ``T* = h*^3`` (cubic gap law), Dirichlet
ports (``p* = 1`` inlet, ``p* = 0`` outlet), and natural no-flux walls (faces
to outside cells carry zero transmissivity). The scale choice
``U0 = h0^2 dp / (12 mu L)`` absorbs the viscous constant, so the reduced
velocity is exactly

    U* = -h*^2 grad* p*      dx*/dt* = U*

Face transmissivity is the harmonic mean of the two adjacent cell ``T*``
(correct for series conductances / sharp gap jumps at hole walls); the linear
system is symmetric and solved directly with ``scipy.sparse.linalg.spsolve``.
The assembly lives in :mod:`precis_flow.fv` (the shared finite-volume
operator of se-multiphysics.md slice 1) — this module keeps the velocity,
floor-gradient and resistance post-processing.

Returns the depth-averaged reduced velocity ``U*``, the reduced pressure
``p*``, the reduced floor gradient ``grad* b~`` (``b~ = 1 - h*``; the tracer's
vertical velocity is driven by it — evaluating ``w`` from ``grad* b~``
directly, not a numerical ``div* U*``, keeps ``w = 0`` wherever the floor is
flat and avoids spurious upwelling at the Dirichlet ports), and the reduced
cell resistance ``R* = dp* / Q*`` with ``Q*`` the net reduced flux out of the
inlet cells (``R* = (nx-1)/ny`` for a uniform rectangle, i.e. ``R =
12 mu L / (h0^3 W)`` re-dimensionalised).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import fv
from .domain import Domain


@dataclass(frozen=True)
class Field:
    """The solved reduced flow field over a :class:`Domain`."""

    domain: Domain
    p_star: np.ndarray  # (ny, nx) reduced pressure, NaN outside
    ux_star: np.ndarray  # (ny, nx) depth-averaged reduced x velocity
    uy_star: np.ndarray  # (ny, nx) depth-averaged reduced y velocity
    bx_star: np.ndarray  # (ny, nx) d*(floor)/dx*, floor b~ = 1 - h*
    by_star: np.ndarray  # (ny, nx) d*(floor)/dy*
    r_star: float  # R* = dp*/Q*; (nx-1)/ny for a uniform rectangle

    @property
    def u_star(self) -> np.ndarray:
        """(2, ny, nx) stack of the depth-averaged reduced velocity."""
        return np.stack([self.ux_star, self.uy_star])


def _masked_gradient(
    field: np.ndarray, inside: np.ndarray, dx: float, axis: int
) -> np.ndarray:
    """Central gradient that respects the domain mask.

    Uses a central difference where both neighbours are inside, falls back to a
    one-sided difference at domain edges, and yields zero where isolated. This
    enforces a homogeneous-Neumann (no-flux) behaviour at walls.
    """
    f = np.where(inside, field, 0.0)
    fp = np.roll(f, -1, axis=axis)
    fm = np.roll(f, 1, axis=axis)
    vp = np.roll(inside, -1, axis=axis) & inside
    vm = np.roll(inside, 1, axis=axis) & inside
    # np.roll wraps; kill the wrapped edge so opposite borders never couple.
    edge_hi: list[slice | int] = [slice(None)] * field.ndim
    edge_lo: list[slice | int] = [slice(None)] * field.ndim
    edge_hi[axis] = -1
    edge_lo[axis] = 0
    vp[tuple(edge_hi)] = False
    vm[tuple(edge_lo)] = False

    central = (fp - fm) / (2.0 * dx)
    forward = (fp - f) / dx
    backward = (f - fm) / dx

    grad = np.zeros_like(field, dtype=float)
    both = vp & vm
    grad = np.where(both, central, grad)
    grad = np.where(vp & ~vm, forward, grad)
    grad = np.where(vm & ~vp, backward, grad)
    grad[~inside] = 0.0
    return grad


def _harmonic(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Harmonic mean of two non-negative arrays (0 where either is 0)."""
    s = a + b
    return np.where(s > 0, 2.0 * a * b / np.where(s > 0, s, 1.0), 0.0)


def solve(domain: Domain) -> Field:
    """Assemble and solve the variable-transmissivity Poisson problem."""
    dx = domain.dx_star
    inside = domain.mask
    inlet = domain.ports.inlet
    outlet = domain.ports.outlet
    h = domain.h_star

    t_field = np.where(inside, h**3, 0.0)
    p = fv.elliptic(domain, t_field, [(inlet, 1.0), (outlet, 0.0)])

    # Depth-averaged reduced velocity U* = -h*^2 grad* p*.
    p_for_grad = np.where(inside, np.nan_to_num(p, nan=0.0), 0.0)
    dpdx = _masked_gradient(p_for_grad, inside, dx, axis=1)
    dpdy = _masked_gradient(p_for_grad, inside, dx, axis=0)
    coef = np.where(inside, h**2, 0.0)
    ux = -coef * dpdx
    uy = -coef * dpdy

    # Reduced floor b~ = 1 - h*; grad* b~ drives the tracer's vertical velocity.
    floor = np.where(inside, 1.0 - h, 0.0)
    bx = _masked_gradient(floor, inside, dx, axis=1)
    by = _masked_gradient(floor, inside, dx, axis=0)

    r_star = _resistance(domain, t_field, p)
    return Field(
        domain=domain,
        p_star=p,
        ux_star=ux,
        uy_star=uy,
        bx_star=bx,
        by_star=by,
        r_star=r_star,
    )


def _resistance(domain: Domain, t_field: np.ndarray, p: np.ndarray) -> float:
    """Reduced hydraulic resistance R* = dp*/Q*, Q* = flux out of inlet cells."""
    ny, nx = domain.shape
    tx = _harmonic(t_field, np.roll(t_field, -1, axis=1))
    tx[:, -1] = 0.0
    ty = _harmonic(t_field, np.roll(t_field, -1, axis=0))
    ty[-1, :] = 0.0
    pf = np.nan_to_num(p, nan=0.0)

    q = 0.0
    jj, ii = np.where(domain.ports.inlet)
    for j, i in zip(jj.tolist(), ii.tolist()):
        for nj, ni, tf in (
            (j, i - 1, tx[j, i - 1] if i - 1 >= 0 else 0.0),
            (j, i + 1, tx[j, i]),
            (j - 1, i, ty[j - 1, i] if j - 1 >= 0 else 0.0),
            (j + 1, i, ty[j, i]),
        ):
            if tf <= 0 or nj < 0 or ni < 0 or nj >= ny or ni >= nx:
                continue
            if not domain.mask[nj, ni] or domain.ports.inlet[nj, ni]:
                continue
            q += tf * (pf[j, i] - pf[nj, ni])  # outflow from inlet cell

    if q <= 1e-15:
        return float("inf")
    return float(1.0 / q)  # dp* = 1 by construction
