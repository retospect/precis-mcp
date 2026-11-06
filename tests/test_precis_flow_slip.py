"""precis_flow Navier-slip kernel tests — se-multiphysics.md slice 5.

The flux-factor core (``Domain.slip_factor`` scaling ``flow.solve``) and the
slip regime card landed earlier; these cover the *species-side* that
:mod:`precis_flow.slip` adds — the gap profile and its Taylor coefficient —
plus the flux factor and series resistance that lock the pressure wiring.

S1. Flux factor — ``R* = (nx-1)/(ny F)`` on a fully slipped rectangle.
S2. Series — equal slip on the downstream half: ``R* = ((nx-1)/2)(1+1/F)/ny``.
S3. Gap profile — ``∫₀¹ w dψ = 1`` and ``w = 6ψ(1-ψ)`` at no slip.
S4. Taylor coefficient — ``κ`` against the five closed-form references.
S5. Taylor emerges — the Eulerian ``shear_velocity = U*·√(210κ)`` wiring and
    the Lagrangian ``SlipProfile`` both reproduce the slip-reduced dispersion.

The se surface (a ``slip-length`` patch measurand, the computer wiring) rides a
coordinator-assigned seed migration and lands with the adapter.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest

from precis_flow import Domain, Ports, Seed, solve, trace
from precis_flow.slip import SlipProfile, flux_factor, gap_profile, taylor_coefficient
from precis_flow.species import Pulse, transport

EPS = 0.005
PE = 10.0


def _rect(
    nx: int,
    ny: int = 20,
    *,
    slip_floor: np.ndarray | float | None = None,
    slip_lid: np.ndarray | float | None = None,
) -> Domain:
    """Uniform rectangle (h*=1), full-width ports on the first/last column."""
    mask = np.ones((ny, nx), dtype=bool)
    inlet = np.zeros((ny, nx), dtype=bool)
    inlet[:, 0] = True
    outlet = np.zeros((ny, nx), dtype=bool)
    outlet[:, -1] = True

    def _field(v: np.ndarray | float | None) -> np.ndarray | None:
        if v is None:
            return None
        return np.broadcast_to(np.asarray(v, dtype=float), (ny, nx)).copy()

    return Domain(
        mask=mask,
        h_star=np.ones((ny, nx)),
        ports=Ports(inlet=inlet, outlet=outlet),
        slip_floor_star=_field(slip_floor),
        slip_lid_star=_field(slip_lid),
    )


# ── S1. Flux factor on a fully slipped rectangle ──────────────────────────


@pytest.mark.parametrize(
    "b1, b2, f",
    [(0.1, 0.1, 1.6), (0.1, 0.0, 14.0 / 11.0), (0.1, 0.3, 74.0 / 35.0)],
)
def test_s1_flux_factor_resistance(b1: float, b2: float, f: float) -> None:
    assert float(flux_factor(b1, b2)) == pytest.approx(f, rel=1e-12)
    nx, ny = 40, 20
    field = solve(_rect(nx, ny, slip_floor=b1, slip_lid=b2))
    assert field.r_star == pytest.approx((nx - 1) / (ny * f), rel=1e-10)
    # No-slip baseline is exactly 1/F faster-resisting.
    base = solve(_rect(nx, ny))
    assert field.r_star == pytest.approx(base.r_star / f, rel=1e-10)


# ── S2. Series resistance: equal slip on the downstream half ──────────────


def test_s2_series_downstream_half() -> None:
    nx, ny, b = 40, 20, 0.1
    f = float(flux_factor(b, b))
    floor = np.zeros((ny, nx))
    lid = np.zeros((ny, nx))
    floor[:, nx // 2 :] = b
    lid[:, nx // 2 :] = b
    field = solve(_rect(nx, ny, slip_floor=floor, slip_lid=lid))
    expected = ((nx - 1) / 2.0) * (1.0 + 1.0 / f) / ny
    assert field.r_star == pytest.approx(expected, rel=1e-10)


# ── S3. Gap profile normalisation and no-slip limit ───────────────────────


def _gl01(fn, *args) -> float:
    """3-point Gauss-Legendre of ``fn`` on [0, 1] — exact for the quadratic
    ``w(ψ)`` to machine precision."""
    x, w = np.polynomial.legendre.leggauss(3)
    psi = 0.5 * (x + 1.0)
    return float(0.5 * np.sum(w * fn(psi, *args)))


@pytest.mark.parametrize("b1, b2", [(0.0, 0.0), (0.1, 0.1), (0.1, 0.0), (1.0, 0.3)])
def test_s3_profile_depth_average_is_one(b1: float, b2: float) -> None:
    assert _gl01(gap_profile, b1, b2) == pytest.approx(1.0, abs=1e-12)


def test_s3_no_slip_is_hele_shaw_parabola() -> None:
    psi = np.linspace(0.0, 1.0, 11)
    assert np.allclose(gap_profile(psi, 0.0, 0.0), 6.0 * psi * (1.0 - psi), atol=1e-12)


def test_s3_slip_profile_refuses_no_slip_domain() -> None:
    with pytest.raises(ValueError, match="no-slip"):
        SlipProfile(_rect(10, 4))


# ── S4. Taylor coefficient against the closed-form references ──────────────


def test_s4_no_slip() -> None:
    assert float(taylor_coefficient(0.0, 0.0)) == pytest.approx(1.0 / 210.0, abs=1e-9)


@pytest.mark.parametrize("beta", [0.05, 0.1, 0.5, 1.0])
def test_s4_equal_slip(beta: float) -> None:
    expected = 1.0 / (210.0 * (1.0 + 6.0 * beta) ** 2)
    assert float(taylor_coefficient(beta, beta)) == pytest.approx(expected, abs=1e-9)


def test_s4_asymmetric_references() -> None:
    assert 210.0 * float(taylor_coefficient(0.1, 0.0)) == pytest.approx(
        46.0 / 49.0, abs=1e-9
    )
    assert 210.0 * float(taylor_coefficient(1.0, 0.0)) == pytest.approx(2.68, abs=1e-9)


def test_s4_large_slip_limit() -> None:
    assert float(taylor_coefficient(1e6, 0.0)) == pytest.approx(2.0 / 105.0, abs=1e-7)


# ── S5. Taylor dispersion emerges with the slip profile ───────────────────


def _slip_rtd_variance(nx: int, eps: float, pe: float, kappa: float) -> float:
    """Outlet RTD variance of a pulse on a mean-1 field whose shear carries
    the slip Taylor coefficient (``shear_velocity = U*·√(210κ)``)."""
    field = solve(_rect(nx=nx, ny=4))
    shear = field.u_star * math.sqrt(210.0 * kappa)
    w, dt, t_end = 0.02, 0.002, 1.8
    res = transport(
        field,
        epsilon=eps,
        pe=pe,
        limiter=False,
        shear_velocity=shear,
        pulse=Pulse(t0=0.0, width=w, t_end=t_end, dt_star=dt),
    )
    assert res.rtd is not None
    t, e = res.rtd
    norm = np.trapezoid(e, t)
    mu = np.trapezoid(t * e, t) / norm
    var = np.trapezoid((t - mu) ** 2 * e, t) / norm
    return float(var - w * w / 12.0)


@pytest.mark.parametrize(
    "b1, b2",
    [(0.1, 0.1), (1.0, 0.0)],
)
def test_s5_eulerian_slip_reduces_dispersion(b1: float, b2: float) -> None:
    """The slip Taylor coefficient ties to the ``(1+6β)⁻²`` / 2.68 forms and
    the pulse variance matches ``2 D_L*`` with that reduced coefficient."""
    kappa = float(taylor_coefficient(b1, b2))
    if b1 == b2:
        formula = (PE / 210.0) / (1.0 + 6.0 * b1) ** 2
    else:
        formula = 2.68 * PE / 210.0  # (1, 0): 210κ = 2.68
    assert PE * kappa == pytest.approx(formula, abs=1e-12)
    d_l = EPS * (1.0 / PE + PE * kappa)
    v1 = _slip_rtd_variance(1400, EPS, PE, kappa)
    v2 = _slip_rtd_variance(2800, EPS, PE, kappa)
    est = v2 + (v2 - v1) / 3.0  # second-order spatial Richardson
    assert abs(est / (2.0 * d_l) - 1.0) <= 0.05


def _slip_lagrangian_variance(
    b1: float, b2: float | None, eps: float, pe: float
) -> float:
    """Residence-time variance of the slip-profile tracer on a mean-1 field,
    with no Taylor term added by hand (``SlipProfile`` carries the shear)."""
    dom = _rect(nx=100, ny=60, slip_floor=b1, slip_lid=b2)
    field = solve(dom)
    ubar = float(np.mean(field.ux_star[dom.mask]))
    field = dataclasses.replace(
        field, ux_star=field.ux_star / ubar, uy_star=field.uy_star / ubar
    )
    paths = trace(
        field,
        Seed.across_inlet(dom, z_lo=0.0, z_hi=1.0),
        20000,
        d_v_star=0.0,
        seed_rng=0,
        pe=pe,
        epsilon=eps,
        cell_profile=SlipProfile(dom),
        max_steps=8000,
    )
    assert not paths.capped
    t_res = paths.residence[np.isfinite(paths.residence)]
    return float(t_res.var())


def test_s5_lagrangian_equal_slip_emerges() -> None:
    kappa = float(taylor_coefficient(0.1, 0.1))
    d_l = EPS * (1.0 / PE + PE * kappa)
    var = _slip_lagrangian_variance(0.1, 0.1, EPS, PE)
    assert abs(var / (2.0 * d_l) - 1.0) <= 0.10


def test_s5_lagrangian_one_wall_emerges() -> None:
    kappa = float(taylor_coefficient(1.0, 0.0))  # 210κ = 2.68
    d_l = EPS * (1.0 / PE + PE * kappa)
    var = _slip_lagrangian_variance(1.0, None, EPS, PE)
    assert abs(var / (2.0 * d_l) - 1.0) <= 0.10
