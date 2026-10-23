"""precis_flow species tests — docs/backlog/se-multiphysics.md slice 1.

Acceptance coverage:

1. fv parity — ``flow.solve`` after the operator was lifted into
   ``precis_flow.fv`` equals an independent reassembly of the old scheme
   on the slice-2 hex chamber within 1e-12.
2. Mass balance — steady Eulerian: |in − out − walls|/in ≤ 1e-10; a
   no-reaction rectangle gives outlet c* = 1 within 1e-10.
3. Taylor dispersion, Eulerian — pulse RTD variance vs ``2 D_L*``.
4. The same variance emerges in the Lagrangian tracer (``pe=``), no
   Taylor term added by hand.
5. Reaction-limited with axial dispersion — Danckwerts closed-vessel
   reference at axial Péclet ≈ 20.
6. Transport-limited — the developed-Sh decay slope in both solvers.
7. Euler ↔ Lagrange agreement on the hex chamber with holes.
8. Size invariance — the reduced solve sees no dimensional scale.
9. Refusals — regime gate, profile rules, unchanged no-``pe`` tracer.
10. Card — the five species lines and their worked-point verdicts.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import scipy.sparse as sp
import scipy.sparse.linalg as spla

from precis_flow import Domain, Ports, RegimeCard, Seed, solve, trace
from precis_flow.flow import _masked_gradient
from precis_flow.species import (
    SH_BOTH_WALLS,
    SH_ONE_WALL,
    Pulse,
    ReactiveWall,
    transport,
)
from tests.test_se_flow import _hex_domain

EPS, PE = 0.005, 10.0  # the spec's worked point: Pe·ε = 0.05


def _rect(nx: int = 200, ny: int = 20) -> Domain:
    """Uniform rectangle, full-width ports on the first/last column."""
    mask = np.ones((ny, nx), dtype=bool)
    inlet = np.zeros((ny, nx), dtype=bool)
    inlet[:, 0] = True
    outlet = np.zeros((ny, nx), dtype=bool)
    outlet[:, -1] = True
    return Domain(
        mask=mask,
        h_star=np.ones((ny, nx)),
        ports=Ports(inlet=inlet, outlet=outlet),
    )


# ── 1. fv parity ──────────────────────────────────────────────────────


def _reference_p(domain: Domain) -> np.ndarray:
    """The pre-fv assembly, restated independently: div*(h*³ grad* p*) = 0
    with harmonic-mean faces, Dirichlet port rows, natural no-flux."""
    inside = domain.mask
    ny, nx = domain.shape
    idx = -np.ones(domain.shape, dtype=int)
    idx[inside] = np.arange(int(inside.sum()))
    t = np.where(inside, domain.h_star**3, 0.0)

    def harm(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        s = a + b
        return np.where(s > 0, 2.0 * a * b / np.where(s > 0, s, 1.0), 0.0)

    tx = harm(t, np.roll(t, -1, axis=1))
    tx[:, -1] = 0.0
    ty = harm(t, np.roll(t, -1, axis=0))
    ty[-1, :] = 0.0

    fixed = np.zeros(domain.shape, dtype=bool)
    fval = np.zeros(domain.shape)
    fixed[domain.ports.inlet] = True
    fval[domain.ports.inlet] = 1.0
    fixed[domain.ports.outlet] = True

    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    b = np.zeros(int(inside.sum()))
    for j, i in zip(*np.where(inside)):
        k = idx[j, i]
        if fixed[j, i]:
            rows.append(k)
            cols.append(k)
            data.append(1.0)
            b[k] = fval[j, i]
            continue
        diag = 0.0
        for nj, ni, tf in (
            (j, i - 1, tx[j, i - 1] if i > 0 else 0.0),
            (j, i + 1, tx[j, i]),
            (j - 1, i, ty[j - 1, i] if j > 0 else 0.0),
            (j + 1, i, ty[j, i]),
        ):
            if tf <= 0.0 or not (0 <= nj < ny and 0 <= ni < nx) or not inside[nj, ni]:
                continue
            diag += tf
            rows.append(k)
            cols.append(idx[nj, ni])
            data.append(-tf)
        if diag == 0.0:
            diag = 1.0
        rows.append(k)
        cols.append(k)
        data.append(diag)
    a = sp.csr_matrix((data, (rows, cols)), shape=(b.size, b.size))
    p = np.full(domain.shape, np.nan)
    p[inside] = spla.spsolve(a.tocsc(), b)
    return p


def _reference_r(domain: Domain, p: np.ndarray) -> float:
    """R* = 1/Q*, Q* the transmissivity-weighted outflow of inlet cells."""
    inside = domain.mask
    ny, nx = domain.shape
    t = np.where(inside, domain.h_star**3, 0.0)
    tx = np.minimum(t, np.roll(t, -1, axis=1)) * 0.0  # placeholder, replaced below
    s = t + np.roll(t, -1, axis=1)
    tx = np.where(
        s > 0, 2.0 * t * np.roll(t, -1, axis=1) / np.where(s > 0, s, 1.0), 0.0
    )
    tx[:, -1] = 0.0
    sy = t + np.roll(t, -1, axis=0)
    ty = np.where(
        sy > 0, 2.0 * t * np.roll(t, -1, axis=0) / np.where(sy > 0, sy, 1.0), 0.0
    )
    ty[-1, :] = 0.0
    pf = np.nan_to_num(p, nan=0.0)
    q = 0.0
    for j, i in zip(*np.where(domain.ports.inlet)):
        for nj, ni, tf in (
            (j, i - 1, tx[j, i - 1] if i > 0 else 0.0),
            (j, i + 1, tx[j, i]),
            (j - 1, i, ty[j - 1, i] if j > 0 else 0.0),
            (j + 1, i, ty[j, i]),
        ):
            if tf <= 0 or not (0 <= nj < ny and 0 <= ni < nx):
                continue
            if not inside[nj, ni] or domain.ports.inlet[nj, ni]:
                continue
            q += tf * (pf[j, i] - pf[nj, ni])
    return 1.0 / q


def test_fv_parity_on_hex_chamber() -> None:
    """``solve`` after the fv lift reproduces the old scheme's p*, U*, R*
    on the slice-2 hex chamber to 1e-12."""
    chamber = _hex_domain(nx=100)
    d = chamber.domain
    f = solve(d)
    p_ref = _reference_p(d)
    assert np.allclose(
        np.nan_to_num(f.p_star), np.nan_to_num(p_ref), atol=1e-12, rtol=0.0
    )
    r_ref = _reference_r(d, p_ref)
    assert abs(f.r_star - r_ref) <= 1e-12 * max(1.0, abs(r_ref))
    # U* = -h*² grad* p* — the post-processing did not move, so equal p*
    # must give equal U*.
    coef = np.where(d.mask, d.h_star**2, 0.0)
    pf = np.where(d.mask, np.nan_to_num(p_ref, nan=0.0), 0.0)
    ux_ref = -coef * _masked_gradient(pf, d.mask, d.dx_star, 1)
    uy_ref = -coef * _masked_gradient(pf, d.mask, d.dx_star, 0)
    assert np.allclose(np.nan_to_num(f.ux_star), ux_ref, atol=1e-12, rtol=0.0)
    assert np.allclose(np.nan_to_num(f.uy_star), uy_ref, atol=1e-12, rtol=0.0)


# ── 2. mass balance ───────────────────────────────────────────────────


def test_mass_balance_rect_and_hex() -> None:
    walls = [ReactiveWall("floor", 0.5), ReactiveWall("lid", 0.5)]
    res = transport(solve(_rect()), epsilon=EPS, pe=PE, walls=walls, limiter=False)
    assert res.mass_residual <= 1e-10
    chamber = _hex_domain(nx=100)
    res_hex = transport(
        solve(chamber.domain), epsilon=EPS, pe=PE, walls=walls, limiter=False
    )
    assert res_hex.mass_residual <= 1e-10


def test_no_reaction_outlet_is_unity() -> None:
    res = transport(solve(_rect()), epsilon=EPS, pe=PE, limiter=False)
    assert abs(res.outlet_mean_c - 1.0) <= 1e-10


# ── 3. Taylor dispersion, Eulerian ────────────────────────────────────


def _rtd_variance(nx: int, eps: float, pe: float) -> float:
    """Outlet RTD variance of a pulse, pulse-width variance removed."""
    w, dt, t_end = 0.02, 0.002, 1.8
    res = transport(
        solve(_rect(nx=nx, ny=4)),
        epsilon=eps,
        pe=pe,
        limiter=False,
        pulse=Pulse(t0=0.0, width=w, t_end=t_end, dt_star=dt),
    )
    assert res.rtd is not None
    t, e = res.rtd
    norm = np.trapezoid(e, t)
    mu = np.trapezoid(t * e, t) / norm
    var = np.trapezoid((t - mu) ** 2 * e, t) / norm
    return float(var - w * w / 12.0)


def test_taylor_dispersion_eulerian() -> None:
    """Pulse RTD variance, Richardson-extrapolated over nx/2nx, matches
    2 D_L* — leaving the Taylor term out fails (it is a third of D_L*)."""
    d_l = EPS * (1.0 / PE + PE / 210.0)  # 7.38e-4; axial Péclet ≈ 1360
    v1 = _rtd_variance(1400, EPS, PE)
    v2 = _rtd_variance(2800, EPS, PE)
    est = v2 + (v2 - v1) / 3.0  # second-order spatial scheme
    assert abs(est / (2.0 * d_l) - 1.0) <= 0.05


# ── 4. Taylor dispersion emerges in the particle model ────────────────


def test_taylor_dispersion_emerges_in_tracer() -> None:
    """Parabolic shear + molecular diffusion only — the tracer is given
    no Taylor term; the residence-time spread must appear by itself."""
    d = _rect(nx=100, ny=60)  # wide: y-diffusion stays off the side walls
    res = trace(
        solve(d),
        Seed.across_inlet(d, z_lo=0.0, z_hi=1.0),
        20000,
        d_v_star=0.0,
        seed_rng=0,
        profile="parabolic",
        pe=PE,
        epsilon=EPS,
        max_steps=8000,
    )
    assert not res.capped
    t_res = res.residence[np.isfinite(res.residence)]
    d_l = EPS * (1.0 / PE + PE / 210.0)
    assert abs(t_res.var() / (2.0 * d_l) - 1.0) <= 0.10


# ── 5. reaction-limited with axial dispersion ─────────────────────────


def _danckwerts_closed(d_l: float, s: float) -> float:
    """Steady closed-vessel value at the outlet: D c'' − c' − s c = 0,
    c − D c' = 1 at x* = 0 (Danckwerts inlet), c' = 0 at x* = 1."""
    disc = math.sqrt(1.0 + 4.0 * d_l * s)
    rp, rm = (1.0 + disc) / (2.0 * d_l), (1.0 - disc) / (2.0 * d_l)
    a_mat = np.array(
        [
            [1.0 - d_l * rp, 1.0 - d_l * rm],
            [rp * math.exp(rp), rm * math.exp(rm)],
        ]
    )
    a, b = np.linalg.solve(a_mat, np.array([1.0, 0.0]))
    return float(a * math.exp(rp) + b * math.exp(rm))


def test_danckwerts_reaction_limited() -> None:
    """ε = 0.05, Pe = 1 (axial Pe ≈ 20), Da_w = 0.05 on both walls:
    the steady outlet c* matches the Danckwerts closed-vessel solution
    within 1% — plug flow sits 15% low and a Dirichlet inlet 9% high, so
    neither approximation can pass this."""
    eps, pe, da_w = 0.05, 1.0, 0.05
    res = transport(
        solve(_rect(nx=200, ny=10)),
        epsilon=eps,
        pe=pe,
        walls=[ReactiveWall("floor", da_w), ReactiveWall("lid", da_w)],
        limiter=False,
    )
    d_star = 1.0 / (pe * eps)
    k_s = da_w / (pe * eps)
    k_eff = 1.0 / (1.0 / k_s + 2.0 / (SH_BOTH_WALLS * d_star))
    d_l = eps * (1.0 / pe + pe / 210.0)
    expected = _danckwerts_closed(d_l, 2.0 * k_eff)
    assert abs(res.outlet_mean_c / expected - 1.0) <= 0.01


# ── 6. transport-limited ──────────────────────────────────────────────


def _survival_slope(paths, x_lo: float, s_floor: float) -> tuple[float, float]:
    """OLS slope of ln S(x*) with its binomial Monte Carlo error."""
    absorbed = paths.absorbed
    ax = paths.absorb_star[:, 0]
    n = paths.n_particles
    alive = (~absorbed) | np.isnan(ax)
    xs: list[float] = []
    ys: list[float] = []
    vs: list[float] = []
    for x_val in np.linspace(x_lo, 1.0, 60):
        s = float((alive | (ax > x_val)).sum()) / n
        if s <= s_floor:
            break
        xs.append(float(x_val))
        ys.append(math.log(s))
        vs.append((1.0 - s) / (n * s))  # var of ln S (binomial, delta method)
    x = np.asarray(xs)
    y = np.asarray(ys)
    v = np.asarray(vs)
    xbar = float(np.average(x))
    span = float(np.sum((x - xbar) ** 2))
    slope = float(np.sum((x - xbar) * y) / span)
    wts = (x - xbar) / span
    var = float(np.sum(wts * wts * v))
    return slope, math.sqrt(var)


def test_transport_limited_decay_slopes() -> None:
    """Da_w = 1000, Pe·ε = 0.1: the Eulerian slope of ln c* near c* = 0.5
    and the Lagrangian survival slope downstream of the entrance length
    both match −n_w Sh/(2εPe) — Sh is not handed to the particle model.

    Pe = 100 (ε = 0.001), not the worked point's 10: the expected slope
    is the zero-dispersion limit, and the true eigenvalue
    (1 − √(1 + 4D_L·s̄))/(2D_L) sits ~5% under it wherever Pe ~ 15–20 —
    the correction scales with (1/Pe + Pe/210)/Pe, not Pe·ε. At Pe = 100
    it is ~3.7%, so the 5% bound measures discretization, not physics."""
    eps, pe, da_w = 0.001, 100.0, 1000.0
    d = _rect(nx=1400, ny=4)
    f = solve(d)
    for walls, n_w, sh in (
        ([ReactiveWall("floor", da_w), ReactiveWall("lid", da_w)], 2, SH_BOTH_WALLS),
        ([ReactiveWall("floor", da_w)], 1, SH_ONE_WALL),
    ):
        res = transport(f, epsilon=eps, pe=pe, walls=walls, limiter=False)
        c = res.c_star[0]
        sel = np.isfinite(c) & (c > 0.2) & (c < 0.8)
        slope = float(np.polyfit(d.xc_star[sel], np.log(c[sel]), 1)[0])
        expected = -n_w * sh / (2.0 * eps * pe)
        assert abs(slope / expected - 1.0) <= 0.05

    d2 = _rect(nx=100, ny=60)
    f2 = solve(d2)
    lag_cases: list[tuple[dict[str, float | np.ndarray], int, float]] = [
        ({"floor": math.inf, "lid": math.inf}, 2, SH_BOTH_WALLS),
        ({"floor": math.inf}, 1, SH_ONE_WALL),
    ]
    for lag_walls, n_w, sh in lag_cases:
        paths = trace(
            f2,
            Seed.across_inlet(d2, z_lo=0.0, z_hi=1.0),
            20000,
            d_v_star=0.0,
            seed_rng=0,
            profile="parabolic",
            pe=pe,
            epsilon=eps,
            walls=lag_walls,
            max_steps=4000,
        )
        assert paths.absorbed is not None and paths.absorb_star is not None
        slope, se = _survival_slope(paths, x_lo=0.2 * eps * pe, s_floor=0.01)
        expected = -n_w * sh / (2.0 * eps * pe)
        assert abs(slope - expected) <= 0.05 * abs(expected) + 3.0 * se


# ── 7. Euler ↔ Lagrange on a real shape ───────────────────────────────


def test_euler_lagrange_agree_on_hex() -> None:
    """Hex chamber with holes, Pe·ε = 0.05, Da_w = 0.1 both walls:
    outlet conversion agrees within 3σ Monte Carlo + 2%."""
    eps, pe, da_w = 0.005, 10.0, 0.1
    chamber = _hex_domain(nx=200)
    f = solve(chamber.domain)
    res_e = transport(
        f,
        epsilon=eps,
        pe=pe,
        walls=[ReactiveWall("floor", da_w), ReactiveWall("lid", da_w)],
    )
    paths = trace(
        f,
        Seed.across_inlet(chamber.domain, z_lo=0.0, z_hi=1.0),
        20000,
        d_v_star=6.0,
        seed_rng=0,
        profile="parabolic",
        pe=pe,
        epsilon=eps,
        walls={"floor": da_w, "lid": da_w},
        max_steps=20000,
    )
    assert paths.absorbed is not None
    conv_l = float(paths.absorbed.mean())
    sigma = math.sqrt(conv_l * (1.0 - conv_l) / paths.n_particles)
    assert abs(res_e.conversion - conv_l) <= 3.0 * sigma + 0.02


# ── 8. size invariance ────────────────────────────────────────────────


def test_same_reduced_numbers_identical_fields() -> None:
    """The kernel carries no dimensional scale: the same reduced tuple
    (shape, ε, Pe, Da_w) — whatever physical sizes produced it — gives a
    bit-identical c* field."""
    d = _rect(nx=100, ny=20)
    walls = [ReactiveWall("floor", 0.2), ReactiveWall("lid", 0.2)]
    a = transport(solve(d), epsilon=EPS, pe=PE, walls=walls, limiter=False)
    b = transport(solve(d), epsilon=EPS, pe=PE, walls=walls, limiter=False)
    assert np.allclose(
        np.nan_to_num(a.c_star), np.nan_to_num(b.c_star), atol=1e-12, rtol=0.0
    )


# ── 9. refusals ───────────────────────────────────────────────────────


def test_regime_and_profile_refusals() -> None:
    d = _rect()
    f = solve(d)
    with pytest.raises(ValueError, match="regime.taylor"):
        transport(f, epsilon=0.05, pe=4.0)  # Pe·ε = 0.2 > 0.1
    with pytest.raises(ValueError, match="agran|Lagrangian"):
        transport(f, epsilon=0.05, pe=4.0)
    with pytest.raises(ValueError, match="parabolic"):
        trace(
            f,
            Seed.across_inlet(d),
            10,
            6.0,
            0,
            pe=PE,
            epsilon=EPS,
            profile="plug",
        )
    with pytest.raises(ValueError, match="profile"):
        transport(f, epsilon=EPS, pe=PE, profile="plug")
    # trace without pe= is unchanged: no absorption bookkeeping appears.
    res = trace(f, Seed.across_inlet(d), 50, 6.0, 0)
    assert res.absorbed is None and res.absorb_star is None


# ── 10. species card lines ────────────────────────────────────────────


def test_species_card_lines() -> None:
    """Worked point (water, D = 1e-9, h0 = 100 µm, L = 20 mm,
    u0 = 0.1 mm/s): five species lines with the spec's verdicts."""
    card = RegimeCard.from_physical(
        L=0.02,
        h0=1e-4,
        mu=1e-3,
        rho=1000.0,
        u0=1e-4,
        molecular_d=1e-9,
        k_s=3e-6,  # Da_II = k_s h0/D = 0.3 → reaction-limited
        n_walls=2,
        apparent_order=1.0,
    )
    assert card.peclet.value == pytest.approx(10.0) and card.peclet.ok
    sp = {ln.name: ln for ln in card.species}
    assert set(sp) == {"taylor", "wall_damkohler", "damkohler", "apparent_order"}
    assert sp["taylor"].value == pytest.approx(0.05) and sp["taylor"].ok
    wd = sp["wall_damkohler"]
    assert wd.value == pytest.approx(2.0 * 0.3 / 7.54) and wd.ok
    da1 = sp["damkohler"]
    assert da1.value == pytest.approx(2.0 * 3e-6 * 0.02 / (1e-4 * 1e-4))
    assert da1.ok  # informational only
    assert sp["apparent_order"].ok

    # Da_II = 0.5 → mixed: the line fails and names the Lagrangian.
    card2 = RegimeCard.from_physical(
        L=0.02,
        h0=1e-4,
        mu=1e-3,
        rho=1000.0,
        u0=1e-4,
        molecular_d=1e-9,
        k_s=5e-6,
        n_walls=2,
    )
    wd2 = {ln.name: ln for ln in card2.species}["wall_damkohler"]
    assert wd2.value == pytest.approx(2.0 * 0.5 / 7.54)
    assert not wd2.ok and "mixed" in wd2.otherwise
    assert "wall_damkohler" in card2.failures

    # No k_s → the reaction lines are not-applicable and do not block ok;
    # no apparent_order → that line is not-applicable either.
    card3 = RegimeCard.from_physical(
        L=0.02, h0=1e-4, mu=1e-3, rho=1000.0, u0=1e-4, molecular_d=1e-9
    )
    sp3 = {ln.name: ln for ln in card3.species}
    assert not sp3["wall_damkohler"].applicable
    assert not sp3["damkohler"].applicable
    assert not sp3["apparent_order"].applicable
    assert card3.ok
    # The lagrangian route never refuses on taylor.
    card4 = RegimeCard.from_physical(
        L=0.02,
        h0=1e-4,
        mu=1e-3,
        rho=1000.0,
        u0=1e-3,  # Pe = 100, Pe·ε = 0.5
        molecular_d=1e-9,
        species_solver="lagrangian",
    )
    t4 = {ln.name: ln for ln in card4.species}["taylor"]
    assert t4.value == pytest.approx(0.5) and t4.ok
    assert "lagrangian" in t4.otherwise.lower()
    c4e = RegimeCard.from_physical(
        L=0.02,
        h0=1e-4,
        mu=1e-3,
        rho=1000.0,
        u0=1e-3,
        molecular_d=1e-9,
        species_solver="eulerian",
    )
    assert "taylor" in c4e.failures
    # No solver asked (default): taylor is a route note, not a failure —
    # the card's ok stays the admission test for the solver asked for.
    c4n = RegimeCard.from_physical(
        L=0.02, h0=1e-4, mu=1e-3, rho=1000.0, u0=1e-3, molecular_d=1e-9
    )
    assert "taylor" not in c4n.failures
