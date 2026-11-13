"""precis_flow Darcy / Brinkman kernel tests — se-multiphysics.md slice 4.

Kernel acceptance (the se surface — permeability/porosity patch measures
and D7 refusals — rides a coordinator-assigned seed migration and lands with
the adapter):

D1. Closed form — fully porous rectangle: ``R* = (nx-1)/(ny T*)`` and the
    bracket ``T*/(12 κ* h*)`` matches 0.95101 / 0.80002 / 0.41891 / 0.07577.
D2. Hele-Shaw limit — ``T*/h*³ − 1 = −1/(10κ*)`` at κ*=1e8 (needs the series
    branch; the naive bracket cancels to −5e-8 there).
D3. Series plug — an interior porous plug: ``R* = ((nx-1-n_p) + n_p/T*)/ny``.
D4. Continuity — net flux through every column of the plug case is equal.
D5. Local gap — ``T*`` over a deepened floor (h*=2) uses the local gap.
D6. Darcy + species (kernel) — inside the region ``taylor_tensor`` equals
    ``ε(φ/τ)/Pe · I`` plus ``(α_L/L)|U*|`` along ``U*``; steady mass balance
    holds; transient runs and an overlapping reactive wall refuse (M15).
Card — the darcy group (kappa_ratio, reynolds_pore, full_height) and its
    refusals.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from precis_flow import Domain, Ports, RegimeCard, solve
from precis_flow.darcy import (
    PorousRegion,
    brinkman_transmissivity,
    porous_t_factor,
)
from precis_flow.flow import _harmonic


def _rect(nx: int, ny: int = 20, h_star: float = 1.0) -> Domain:
    """Uniform rectangle, full-width ports on the first/last column."""
    mask = np.ones((ny, nx), dtype=bool)
    inlet = np.zeros((ny, nx), dtype=bool)
    inlet[:, 0] = True
    outlet = np.zeros((ny, nx), dtype=bool)
    outlet[:, -1] = True
    return Domain(
        mask=mask,
        h_star=np.full((ny, nx), h_star),
        ports=Ports(inlet=inlet, outlet=outlet),
    )


def _bracket_ref(r: float) -> float:
    """``g = T*/(12 κ* h*) = 1 − tanh(z)/z`` with z = 1/(2√r), r = κ*/h*²."""
    z = 1.0 / (2.0 * math.sqrt(r))
    return 1.0 - math.tanh(z) / z


# ── D1. Closed form on a fully porous rectangle ───────────────────────────


@pytest.mark.parametrize(
    "ratio, bracket",
    [(6e-4, 0.95101), (1e-2, 0.80002), (0.1, 0.41891), (1.0, 0.07577)],
)
def test_d1_bracket_and_resistance(ratio: float, bracket: float) -> None:
    # Uniform gap h*=1, so κ/h² = κ*. The bracket matches the spec table.
    assert brinkman_transmissivity(ratio, 1.0) / (12.0 * ratio) == pytest.approx(
        bracket, abs=1e-5
    )
    assert _bracket_ref(ratio) == pytest.approx(bracket, abs=1e-5)

    nx, ny = 40, 20
    dom = _rect(nx, ny)
    region = np.ones((ny, nx), dtype=bool)
    tf = porous_t_factor(dom.h_star, ratio, region)
    t_star = float(brinkman_transmissivity(ratio, 1.0))
    field = solve(dom, t_factor=tf)
    assert field.r_star == pytest.approx((nx - 1) / (ny * t_star), rel=1e-10)


# ── D2. Hele-Shaw limit needs the series branch ───────────────────────────


def test_d2_hele_shaw_series_branch() -> None:
    kappa = 1e8  # κ*; h*=1 so z = 1/(2√κ*) ≈ 5e-5, deep in the series branch
    t_over_cube = float(brinkman_transmissivity(kappa, 1.0))  # /h*³ = /1
    assert t_over_cube - 1.0 == pytest.approx(-1.0 / (10.0 * kappa), abs=1e-12)
    # The naive bracket cancels catastrophically here, confirming the branch is
    # load-bearing: 1 − tanh(z)/z evaluated directly misses by orders.
    z = 1.0 / (2.0 * math.sqrt(kappa))
    naive = 12.0 * kappa * (1.0 - math.tanh(z) / z)
    assert abs((naive - 1.0) - (-1.0 / (10.0 * kappa))) > 1e-9


# ── D3. Series resistance of an interior plug ─────────────────────────────


def test_d3_series_plug_resistance() -> None:
    nx, ny, n_p = 60, 20, 12
    kappa = 0.1
    dom = _rect(nx, ny)
    region = np.zeros((ny, nx), dtype=bool)
    i0 = (nx - n_p) // 2
    region[:, i0 : i0 + n_p] = True
    tf = porous_t_factor(dom.h_star, kappa, region)
    t_star = float(brinkman_transmissivity(kappa, 1.0))
    field = solve(dom, t_factor=tf)
    expected = ((nx - 1 - n_p) + n_p / t_star) / ny
    assert field.r_star == pytest.approx(expected, rel=1e-10)


# ── D4. Continuity: equal net flux through every column ───────────────────


def test_d4_column_flux_conservation() -> None:
    nx, ny, n_p = 60, 20, 12
    kappa = 0.05
    dom = _rect(nx, ny)
    region = np.zeros((ny, nx), dtype=bool)
    region[:, (nx - n_p) // 2 : (nx - n_p) // 2 + n_p] = True
    tf = porous_t_factor(dom.h_star, kappa, region)
    field = solve(dom, t_factor=tf)

    t_field = np.where(dom.mask, dom.h_star**3 * tf, 0.0)
    tx = _harmonic(t_field, np.roll(t_field, -1, axis=1))
    tx[:, -1] = 0.0
    p = np.nan_to_num(field.p_star, nan=0.0)
    flux = np.array(
        [float((tx[:, i] * (p[:, i] - p[:, i + 1])).sum()) for i in range(nx - 1)]
    )
    assert flux.std() / abs(flux.mean()) <= 1e-10


# ── D5. Local gap: the porous form uses the local h* ──────────────────────


def test_d5_local_gap() -> None:
    kappa = 1.0
    # Direct closed form at h*=2 (z = h*/(2√κ*) = 1.0, well outside the series).
    z = 2.0 / (2.0 * math.sqrt(kappa))
    ref = 12.0 * kappa * 2.0 * (1.0 - math.tanh(z) / z)
    assert float(brinkman_transmissivity(kappa, 2.0)) == pytest.approx(ref, abs=1e-12)

    # porous_t_factor over a deepened region uses that local gap: t = T*/h*³.
    ny, nx = 10, 20
    h = np.ones((ny, nx))
    region = np.zeros((ny, nx), dtype=bool)
    region[:, 5:9] = True
    h[region] = 2.0
    tf = porous_t_factor(h, kappa, region)
    assert tf[0, 6] == pytest.approx(ref / 2.0**3, abs=1e-12)
    assert tf[0, 0] == 1.0  # open cell untouched


# ── D5b. Darcy and Hele-Shaw limits of the transmissivity ─────────────────


def test_limits() -> None:
    # Darcy: g → 1 as κ* → 0, so T* → 12 κ* h*. The approach is O(2√κ*), so
    # κ* must be tiny for the limit to bind tightly (~2e-7 at κ*=1e-14).
    assert float(brinkman_transmissivity(1e-14, 1.0)) == pytest.approx(
        12.0 * 1e-14, rel=1e-5
    )
    # Hele-Shaw: T* → h*³ as κ* → ∞.
    assert float(brinkman_transmissivity(1e10, 1.3)) == pytest.approx(1.3**3, rel=1e-8)


# ── Refusals ──────────────────────────────────────────────────────────────


def test_refusals() -> None:
    region = np.ones((4, 6), dtype=bool)
    with pytest.raises(ValueError, match="κ.*> 0"):
        porous_t_factor(np.ones((4, 6)), 0.0, region)
    with pytest.raises(ValueError, match="κ.*> 0"):
        porous_t_factor(np.ones((4, 6)), -1.0, region)
    with pytest.raises(ValueError, match="h.*> 0"):
        brinkman_transmissivity(0.1, 0.0)
    with pytest.raises(ValueError, match="shape"):
        porous_t_factor(np.ones((4, 6)), 0.1, np.ones((4, 5), dtype=bool))


# ── D6. Darcy + species: porous dispersion replaces the Taylor term ───────

_EPS, _PE = 0.05, 1.0  # Pe·ε = 0.05, inside the depth-average gate


def _plug_case(
    nx: int = 60, ny: int = 12, n_p: int = 12, kappa: float = 0.05
) -> tuple[Any, np.ndarray]:
    dom = _rect(nx, ny)
    region = np.zeros((ny, nx), dtype=bool)
    region[:, (nx - n_p) // 2 : (nx - n_p) // 2 + n_p] = True
    return solve(dom, t_factor=porous_t_factor(dom.h_star, kappa, region)), region


@pytest.mark.parametrize("alpha_l", [0.0, 0.02])
def test_d6_tensor_inside_region(alpha_l: float) -> None:
    from precis_flow.species import taylor_tensor

    field, region = _plug_case()
    phi, tau = 0.4, 1.5
    porous = PorousRegion(region, porosity=phi, tortuosity=tau, alpha_l_star=alpha_l)
    got = taylor_tensor(field, _EPS, _PE, porous=porous)
    u = field.u_star
    speed = np.hypot(u[0], u[1])
    mol = _EPS * (phi / tau) / _PE
    for j, i in zip(*np.where(region)):
        uhat = np.array([u[0][j, i], u[1][j, i]]) / speed[j, i]
        ref = mol * np.eye(2) + alpha_l * speed[j, i] * np.outer(uhat, uhat)
        np.testing.assert_allclose(got[j, i], ref, rtol=0, atol=1e-12)
    # Outside the region the open-gap Taylor tensor is untouched (C1).
    open_ = taylor_tensor(field, _EPS, _PE)
    np.testing.assert_array_equal(got[~region], open_[~region])


def test_d6_mass_balance() -> None:
    from precis_flow.species import ReactiveWall, transport

    field, region = _plug_case()
    reactive = np.zeros_like(region)
    reactive[:, :10] = True  # upstream of the plug, off the region
    walls = [ReactiveWall("floor", 0.05, reactive), ReactiveWall("lid", 0.05, reactive)]
    porous = PorousRegion(region, porosity=0.3, tortuosity=2.0, alpha_l_star=0.01)
    res = transport(field, epsilon=_EPS, pe=_PE, walls=walls, porous=porous)
    assert res.mass_residual <= 1e-10
    assert np.all(np.isfinite(res.c_star[field.domain.mask]))


def test_d6_refusals() -> None:
    from precis_flow.species import Pulse, ReactiveWall, transport

    field, region = _plug_case()
    porous = PorousRegion(region, porosity=0.4, tortuosity=1.5)
    with pytest.raises(ValueError, match="steady-only"):
        transport(
            field,
            epsilon=_EPS,
            pe=_PE,
            porous=porous,
            pulse=Pulse(t0=0.0, width=0.1, t_end=1.0, dt_star=0.05),
        )
    with pytest.raises(ValueError, match="overlaps the porous region"):
        transport(
            field,
            epsilon=_EPS,
            pe=_PE,
            walls=[ReactiveWall("floor", 0.1)],
            porous=porous,
        )
    with pytest.raises(ValueError, match="porosity"):
        PorousRegion(region, porosity=0.0, tortuosity=1.0)
    with pytest.raises(ValueError, match="porosity"):
        PorousRegion(region, porosity=1.2, tortuosity=1.0)
    with pytest.raises(ValueError, match="tortuosity"):
        PorousRegion(region, porosity=0.5, tortuosity=0.0)
    with pytest.raises(ValueError, match="alpha_l_star"):
        PorousRegion(region, porosity=0.5, tortuosity=1.0, alpha_l_star=-1.0)


# ── Card: the darcy regime group ──────────────────────────────────────────

_BASE: dict[str, Any] = {"L": 0.02, "h0": 1e-4, "mu": 1e-3, "rho": 1e3, "u0": 1e-3}


def test_card_group_present_only_with_permeability() -> None:
    plain = RegimeCard.from_physical(**_BASE)
    assert all(name != "darcy" for name, _ in plain.groups)

    card = RegimeCard.from_physical(**_BASE, permeability=1e-12)
    group = dict(card.groups)["darcy"]
    names = [ln.name for ln in group]
    assert names == ["kappa_ratio", "reynolds_pore", "full_height"]
    kappa_ratio, re_pore, full = group
    assert kappa_ratio.value == pytest.approx(1e-12 / _BASE["h0"] ** 2)
    assert not kappa_ratio.applicable or kappa_ratio.ok  # informational
    assert re_pore.value == pytest.approx(
        _BASE["rho"] * _BASE["u0"] * math.sqrt(1e-12) / _BASE["mu"]
    )
    assert re_pore.ok  # ~1e-3, creeping
    assert not math.isfinite(full.value) and full.ok


def test_card_reynolds_pore_gates_ok() -> None:
    # A large permeability with a fast flow pushes the pore Reynolds past 1.
    card = RegimeCard.from_physical(
        L=0.02, h0=1e-4, mu=1e-5, rho=1e3, u0=1.0, permeability=1e-6
    )
    re_pore = dict(card.groups)["darcy"][1]
    assert not re_pore.ok
    assert "reynolds_pore" in card.failures
    assert not card.ok


def test_card_names_nearer_limit() -> None:
    darcy_near = dict(RegimeCard.from_physical(**_BASE, permeability=1e-16).groups)[
        "darcy"
    ][0]
    assert "Darcy limit" in darcy_near.otherwise
    hs_near = dict(
        RegimeCard.from_physical(**_BASE, permeability=_BASE["h0"] ** 2 * 2).groups
    )["darcy"][0]
    assert "Hele-Shaw" in hs_near.otherwise


def test_card_refuses_nonpositive_permeability() -> None:
    with pytest.raises(ValueError, match="permeability must be > 0"):
        RegimeCard.from_physical(**_BASE, permeability=0.0)
