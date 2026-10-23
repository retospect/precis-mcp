"""Species advection–diffusion with Taylor dispersion and reactive walls
(docs/backlog/se-multiphysics.md slice 1).

The depth-averaged Eulerian solve on a solved :class:`Field`:

    ∂(h* c*)/∂t* + ∇*·(h* U* c*) = ∇*·(h* 𝔻* ∇* c*) − Σ_w k*_eff,w c*
    𝔻* = ε [ I/Pe + (Pe/210) h*² U_s* U_s*ᵀ ]
    1/k*_eff,w = 1/k*_s,w + 2 h*/(Sh_w D*)

with ``D* = 1/(Pe ε)``, ``k*_s = Da_w/(Pe ε)`` and ``Sh_w = 7.54`` where
both walls are reactive at that cell, ``4.86`` where one is. ``c*`` is
``c/c_in``; the reduced sink ``s*(x, y) = Σ_w k*_eff,w`` is the spec's
``(L/(h0 u0)) Σ_w k_eff,w`` term.

Regime gate: the depth average is valid while ``Pe·ε ≤ 0.1`` (transverse
mixing time a tenth of transit); past it :func:`transport` raises
``ValueError`` naming ``regime.taylor`` and the Lagrangian alternative
(``trace`` with ``pe=``/``walls=``), which is also the reference in the
mixed regime — the depth average drops the O(Da_w) shift of the axial
coefficient and centroid speed (Sankarasubramanian & Gill 1973).

The inlet is the Danckwerts combined-flux condition (the Robin row
``T ∂c/∂n̂ = (F·n̂)(c − c_in)`` is the same prescription); outlet cells are
zero-gradient. Advection is second-order central, with the face-Péclet
upwind fallback of :func:`precis_flow.fv.transport_matrix` where the
local face Péclet exceeds 2 (``limiter=`` controls it; the result records
the maximum and whether it fired). Transient runs use Crank–Nicolson.

A one-step chain ``A → B`` is linear: B's wall *source* is A's wall sink
(``source=`` the per-cell field ``s*_A c*_A``), solved after A.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

from . import fv
from .domain import Domain
from .flow import Field

#: Developed Sherwood numbers for parallel plates (table in the spec):
#: both walls reactive / one wall.
SH_BOTH_WALLS = 7.54
SH_ONE_WALL = 4.86

#: The depth-average validity bound: transverse mixing time h0²/D at most
#: a tenth of the transit L/u0.
TAYLOR_MAX_PE_EPS = 0.1


@dataclass(frozen=True)
class ReactiveWall:
    """A reactive wall: ``wall`` is ``'floor'`` or ``'lid'``, ``da_w`` the
    wall Damköhler ``k_s h0 / D`` (``inf`` = perfectly absorbing — the
    transport limit), ``cells`` an optional ``(ny, nx)`` bool restricting
    the reaction to a patch (``None`` = the whole wall)."""

    wall: str
    da_w: float
    cells: np.ndarray | None = None


@dataclass(frozen=True)
class Pulse:
    """A transient inlet pulse: ``c_in(t*)`` is 1 on ``(t0, t0 + width)``
    and 0 outside; the solve runs Crank–Nicolson steps of ``dt_star`` to
    ``t_end``."""

    t0: float
    width: float
    t_end: float
    dt_star: float


@dataclass(frozen=True)
class SpeciesResult:
    """Reduced-units species result: the ``c*`` field plus the flux
    budget and diagnostics the card and measures read."""

    c_star: np.ndarray  # (ny, nx), NaN outside
    inlet_flux: float  # Σ prescribed inlet species flux (F·n̂ inflow · c_in)
    outlet_flux: float  # Σ (F·n̂ outflow · c*) over outlet boundary faces
    outlet_mean_c: float  # flux-weighted mean c* at the outlet
    wall_sink: float  # Σ s* c* dx*² over the domain
    per_wall_sink: tuple[float, ...]  # per ReactiveWall, same order
    cell_pe_max: float
    limiter_active: bool
    rtd: tuple[np.ndarray, np.ndarray] | None = None  # (t*, outlet c*(t*)) pulse runs

    @property
    def conversion(self) -> float:
        """1 − outlet/inlet species flux."""
        if self.inlet_flux <= 0:
            return 0.0
        return 1.0 - self.outlet_flux / self.inlet_flux

    @property
    def outlet_c_star(self) -> float:
        """Flux-weighted mean c* at the outlet."""
        return self.outlet_mean_c

    @property
    def mass_residual(self) -> float:
        """|inflow − outflow − wall consumption| / inflow."""
        if self.inlet_flux <= 0:
            return float("nan")
        return (
            abs(self.inlet_flux - self.outlet_flux - self.wall_sink) / self.inlet_flux
        )


def taylor_tensor(
    field: Field,
    epsilon: float,
    pe: float,
    shear_velocity: np.ndarray | None = None,
) -> np.ndarray:
    """The (ny, nx, 2, 2) reduced dispersion tensor ``𝔻*``.

    Built from the *shear-carrying* part of ``U*`` (the Poiseuille part;
    a uniform plug adds nothing) — ``shear_velocity`` defaults to the
    field's own ``U*``, which is all of it in slice 1 (later slices pass
    their own shear part).
    """
    h = field.domain.h_star
    us = np.asarray(field.u_star if shear_velocity is None else shear_velocity)
    out = np.zeros(field.domain.shape + (2, 2), dtype=float)
    out[:, :, 0, 0] = epsilon * (1.0 / pe + (pe / 210.0) * h * h * us[0] * us[0])
    out[:, :, 1, 1] = epsilon * (1.0 / pe + (pe / 210.0) * h * h * us[1] * us[1])
    cross = epsilon * (pe / 210.0) * h * h * us[0] * us[1]
    out[:, :, 0, 1] = cross
    out[:, :, 1, 0] = cross
    out[~field.domain.mask] = 0.0
    return out


def wall_sink_star(
    field: Field,
    epsilon: float,
    pe: float,
    walls: list[ReactiveWall] | tuple[ReactiveWall, ...],
) -> tuple[np.ndarray, list[np.ndarray]]:
    """The reduced sink field ``s* = Σ_w k*_eff,w`` plus each wall's share.

    ``k*_eff,w = 1/(1/k*_s,w + 2 h*/(Sh_w D*))`` with ``D* = 1/(Pe ε)``,
    ``k*_s = Da_w/(Pe ε)`` and ``Sh_w`` per cell 7.54 where both walls
    react, 4.86 where one does (the developed Sherwood pair).
    """
    mask = field.domain.mask
    h = field.domain.h_star
    per_wall: list[np.ndarray] = []
    n_reactive = np.zeros(mask.shape, dtype=int)
    cell_of: list[np.ndarray] = []
    for w in walls:
        cells = mask if w.cells is None else (np.asarray(w.cells, dtype=bool) & mask)
        cell_of.append(cells)
        n_reactive += cells.astype(int)
    sh = np.where(n_reactive >= 2, SH_BOTH_WALLS, SH_ONE_WALL)
    d_star = 1.0 / (pe * epsilon)
    h_safe = np.where(h > 0, h, 1.0)
    for w, cells in zip(walls, cell_of):
        if np.isposinf(w.da_w):
            k_eff = sh * d_star / (2.0 * h_safe)
        else:
            k_s = w.da_w / (pe * epsilon)
            k_eff = 1.0 / (1.0 / k_s + 2.0 * h_safe / (sh * d_star))
        per_wall.append(np.where(cells, k_eff, 0.0))
    sink = np.zeros(mask.shape, dtype=float)
    for wf in per_wall:
        sink += wf
    return sink, per_wall


def transport(
    field: Field,
    *,
    epsilon: float,
    pe: float,
    walls: list[ReactiveWall] | tuple[ReactiveWall, ...] = (),
    c_in: float = 1.0,
    source: np.ndarray | None = None,
    shear_velocity: np.ndarray | None = None,
    profile: str = "poiseuille",
    limiter: bool = True,
    pulse: Pulse | None = None,
) -> SpeciesResult:
    """Steady (or pulsed) Eulerian solve on ``field``.

    ``epsilon`` is ``h0/L`` and ``pe`` is ``u0 h0/D`` — both stated by the
    caller (the kernel is dimension-free; the regime card computes them).
    A ``Pulse`` runs Crank–Nicolson and records the flux-weighted outlet
    ``c*(t*)`` series in ``rtd``.
    """
    if profile != "poiseuille":
        raise ValueError(
            f"species.transport: unknown profile {profile!r} — 'poiseuille' "
            "is the only shear profile slice 1 carries"
        )
    if epsilon <= 0 or pe <= 0:
        raise ValueError("species.transport needs epsilon > 0 and pe > 0")
    if pe * epsilon > TAYLOR_MAX_PE_EPS:
        raise ValueError(
            f"regime.taylor: Pe·ε = {pe * epsilon:g} > {TAYLOR_MAX_PE_EPS:g} — "
            "the depth-averaged solve needs transverse mixing faster than "
            "transit; run the Lagrangian solver instead (trace with pe= and "
            "walls=)"
        )
    domain = field.domain
    h = domain.h_star
    tensor = taylor_tensor(field, epsilon, pe, shear_velocity) * h[:, :, None, None]
    flux = field.u_star * h[None]
    sink, per_wall = wall_sink_star(field, epsilon, pe, list(walls))

    if pulse is None:
        c, pe_max, lim = fv.solve_transport(
            domain,
            flux=flux,
            tensor=tensor,
            sink=sink,
            inlet=domain.ports.inlet,
            inlet_value=c_in,
            outlet=domain.ports.outlet,
            rhs=source,
            limiter=limiter,
        )
        return _result(domain, flux, sink, per_wall, c, c_in, pe_max, lim)

    # Transient pulse: A stays fixed; b toggles with c_in(t*).
    a0, _b0, pe_max, lim = fv.transport_matrix(
        domain,
        flux=flux,
        tensor=tensor,
        sink=sink,
        inlet=domain.ports.inlet,
        inlet_value=0.0,
        outlet=domain.ports.outlet,
        rhs=source,
        limiter=limiter,
    )
    _a, b_on, _p, _l = fv.transport_matrix(
        domain,
        flux=flux,
        tensor=tensor,
        sink=sink,
        inlet=domain.ports.inlet,
        inlet_value=c_in,
        outlet=domain.ports.outlet,
        rhs=source,
        limiter=limiter,
    )
    inside = domain.mask
    dt = pulse.dt_star
    steps = max(1, round(pulse.t_end / dt))
    m = h[inside] / dt
    mmat = sp.diags(m, format="csc")
    lhs = mmat + 0.5 * a0.tocsc()
    rhs_op = mmat - 0.5 * a0.tocsr()
    lu = spla.splu(lhs)
    c = np.zeros(int(inside.sum()))
    t_axis: list[float] = []
    c_axis: list[float] = []
    for step in range(1, steps + 1):
        t = step * dt
        b = b_on if pulse.t0 <= t < pulse.t0 + pulse.width else np.zeros_like(b_on)
        c = lu.solve(rhs_op @ c + b)
        full = np.full(domain.shape, np.nan)
        full[inside] = c
        t_axis.append(t)
        c_axis.append(_outlet_mean(domain, flux, full))
    c_full = np.full(domain.shape, np.nan)
    c_full[inside] = c
    res = _result(domain, flux, sink, per_wall, c_full, c_in, pe_max, lim)
    return SpeciesResult(
        c_star=res.c_star,
        inlet_flux=res.inlet_flux,
        outlet_flux=res.outlet_flux,
        outlet_mean_c=res.outlet_mean_c,
        wall_sink=res.wall_sink,
        per_wall_sink=res.per_wall_sink,
        cell_pe_max=res.cell_pe_max,
        limiter_active=res.limiter_active,
        rtd=(np.asarray(t_axis), np.asarray(c_axis)),
    )


def _face_flux(
    domain: Domain, flux: np.ndarray, cells: np.ndarray, *, inflow: bool
) -> float:
    """Net carrier flux through the boundary faces of ``cells``."""
    ny, nx = domain.shape
    inside = domain.mask
    fx, fy = flux[0], flux[1]
    total = 0.0
    jj, ii = np.where(np.asarray(cells, dtype=bool) & inside)
    for j, i in zip(jj.tolist(), ii.tolist()):
        for bj, bi, q in (
            (j, i - 1, -fx[j, i]),
            (j, i + 1, fx[j, i]),
            (j - 1, i, -fy[j, i]),
            (j + 1, i, fy[j, i]),
        ):
            if 0 <= bj < ny and 0 <= bi < nx and inside[bj, bi]:
                continue
            if inflow and q < 0:
                total += -q * domain.dx_star
            elif not inflow and q > 0:
                total += q * domain.dx_star
    return total


def _outlet_mean(domain: Domain, flux: np.ndarray, c: np.ndarray) -> float:
    """Flux-weighted mean c* across the outlet cells' boundary faces."""
    ny, nx = domain.shape
    inside = domain.mask
    fx, fy = flux[0], flux[1]
    num = den = 0.0
    jj, ii = np.where(domain.ports.outlet & inside)
    for j, i in zip(jj.tolist(), ii.tolist()):
        for bj, bi, q in (
            (j, i - 1, -fx[j, i]),
            (j, i + 1, fx[j, i]),
            (j - 1, i, -fy[j, i]),
            (j + 1, i, fy[j, i]),
        ):
            if 0 <= bj < ny and 0 <= bi < nx and inside[bj, bi]:
                continue
            if q > 0:
                num += q * domain.dx_star * float(c[j, i])
                den += q * domain.dx_star
    return num / den if den > 0 else 0.0


def _result(
    domain: Domain,
    flux: np.ndarray,
    sink: np.ndarray,
    per_wall: list[np.ndarray],
    c: np.ndarray,
    c_in: float,
    pe_max: float,
    lim: bool,
) -> SpeciesResult:
    inlet_f = _face_flux(domain, flux, domain.ports.inlet, inflow=True) * c_in
    ny, nx = domain.shape
    inside = domain.mask
    dx = domain.dx_star
    out_f = 0.0
    fx, fy = flux[0], flux[1]
    jj, ii = np.where(domain.ports.outlet & inside)
    for j, i in zip(jj.tolist(), ii.tolist()):
        for bj, bi, q in (
            (j, i - 1, -fx[j, i]),
            (j, i + 1, fx[j, i]),
            (j - 1, i, -fy[j, i]),
            (j + 1, i, fy[j, i]),
        ):
            if 0 <= bj < ny and 0 <= bi < nx and inside[bj, bi]:
                continue
            if q > 0:
                out_f += q * dx * float(c[j, i])
    c_clean = np.nan_to_num(c)
    wall_total = float(np.sum(sink * c_clean * dx * dx))
    per = [float(np.sum(wf * c_clean * dx * dx)) for wf in per_wall]
    return SpeciesResult(
        c_star=c,
        inlet_flux=inlet_f,
        outlet_flux=out_f,
        outlet_mean_c=_outlet_mean(domain, flux, c),
        wall_sink=wall_total,
        per_wall_sink=tuple(per),
        cell_pe_max=pe_max,
        limiter_active=lim,
    )
