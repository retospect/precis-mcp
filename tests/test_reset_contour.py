"""Slice 7: the guaranteed-reset hysteresis contour — same hand-checkable
grid as ``test_staircase_masks``: ``U`` = -1..1 in 0.25 steps (9 points),
``pH`` = 0..7 in 1.0 steps (8 points); one U column measures 2.0 V·pH,
the grid 18.0 V·pH.

A rung ``(delta_g0, n_e=1, eps=0)`` is viable for ``U <= -delta_g0``. The
overdrive proxy (η = 0.2 eV default) guarantees it where
``delta_g <= -0.2``, i.e. ``U <= -delta_g0 - 0.2`` — the contour sits
η/|n_e| volts inside the viability boundary, and the strip between them
is the hysteretic band. The rung's σ composes in series, pessimistically:
guaranteed where ``delta_g + σ <= -η``.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis_pathway import reset_contour as rc
from precis_pathway import staircase_masks as sm

U = np.linspace(-1.0, 1.0, 9)
PH = np.linspace(0.0, 7.0, 8)
COL_VPH = 2.0
GRID_VPH = 18.0


def _resting(wanted: np.ndarray | None = None) -> sm.RestingMap:
    w = np.ones((PH.size, U.size), dtype=bool) if wanted is None else wanted
    return sm.RestingMap(U, PH, w)


def _rung(name: str, g0: float, n_e: int = 1, **kw) -> sm.Rung:
    return sm.Rung(name=name, delta_g0=g0, n_e=n_e, **kw)


def _cols(r: sm.RestingMap, *u_values: float) -> np.ndarray:
    m = np.zeros(r.shape, dtype=bool)
    for u in u_values:
        m[:, int(np.argmin(np.abs(r.u_rhe - u)))] = True
    return m


# ── proxy 1: thermodynamic overdrive ─────────────────────────────────────


def test_overdrive_contour_sits_eta_inside_the_viability_line() -> None:
    """dg = -0.5 + U: viable U <= 0.5, guaranteed dg <= -0.2 → U <= 0.3 →
    cols through 0.25. Col 0.5 is the hysteretic band; 0.75/1.0 are
    outside the viable region entirely."""
    r = _resting()
    route = sm.Route("main", (_rung("r1", -0.5),))
    c = rc.reset_contour(r, route)
    assert np.array_equal(c.reset_ok, ~_cols(r, 0.5, 0.75, 1.0))
    assert np.array_equal(c.viable, ~_cols(r, 0.75, 1.0))
    assert np.array_equal(c.hysteretic, _cols(r, 0.5))
    assert c.reset_area == pytest.approx(6 * COL_VPH)
    assert c.reset_fraction == pytest.approx(6 / 9)
    assert c.hysteretic_area == pytest.approx(COL_VPH)
    assert c.estimators == ("overdrive",)
    # the mask is pH-flat on the RHE scale
    assert np.all(c.reset_ok == c.reset_ok[0:1, :])


def test_margin_and_limiting_name_the_binding_edge() -> None:
    """Two rungs: r1 needs U <= 0.3, r2 (dg = 0.1 + U) needs U <= -0.3 —
    r2 limits everywhere it binds; margin is r2's slack."""
    r = _resting()
    route = sm.Route("main", (_rung("r1", -0.5), _rung("r2", 0.1)))
    c = rc.reset_contour(r, route)
    assert np.array_equal(c.reset_ok, _cols(r, -1.0, -0.75, -0.5))
    # at U = -0.5: r1 slack 1.0 - 0.2 = 0.8, r2 slack 0.4 - 0.2 = 0.2
    pt = c.at(-0.5, 3.0)
    assert pt["margin"] == pytest.approx(0.2)
    assert pt["limiting"] == "r2"
    # at U = 0: r2 slack = -0.1 - 0.2 = -0.3 (r1's is +0.3); r2 is also
    # not viable there (dg = +0.1) so the point is outside the band
    pt = c.at(0.0, 3.0)
    assert pt["margin"] == pytest.approx(-0.3)
    assert pt["limiting"] == "r2"
    assert not pt["reset_ok"] and not pt["hysteretic"]
    # at U = -0.25: r2 dg = -0.15 is viable but short of overdrive — the
    # hysteretic band
    pt = c.at(-0.25, 3.0)
    assert pt["margin"] == pytest.approx(-0.05)
    assert pt["hysteretic"]


def test_sigma_launches_the_margin_from_the_far_band_edge() -> None:
    """§6.3: dg = -0.5 + U ± 0.1 — pessimistic launch needs
    dg + σ <= -0.2 → U <= 0.2 → cols through 0.0. The band between the
    nominal reset line (U = 0.3) and the pushed-out line is extra
    hysteretic zone, never averaged away."""
    r = _resting()
    route = sm.Route("main", (_rung("r1", -0.5, sigma=0.1),))
    c = rc.reset_contour(r, route)
    assert np.array_equal(c.reset_ok, ~_cols(r, 0.25, 0.5, 0.75, 1.0))
    assert np.array_equal(c.hysteretic, _cols(r, 0.25, 0.5))
    e = c.edges[0]
    assert e.margin_eV == pytest.approx(0.2)
    assert e.sigma_eV == pytest.approx(0.1)
    assert e.required_eV == pytest.approx(0.3)


def test_oxidation_rung_guarantees_on_the_high_u_side() -> None:
    """n_e = -1: dg = 0.1 - U — viable U >= 0.1, guaranteed U >= 0.3."""
    r = _resting()
    route = sm.Route("main", (_rung("ox", 0.1, n_e=-1),))
    c = rc.reset_contour(r, route)
    assert np.array_equal(c.reset_ok, _cols(r, 0.5, 0.75, 1.0))
    assert np.array_equal(c.hysteretic, _cols(r, 0.25))


def test_chemical_rung_is_all_or_nothing() -> None:
    """n_e = 0 makes dg U-flat: dg0 = -0.1 is viable everywhere and
    guaranteed nowhere — the whole map is hysteretic band; dg0 = -0.3 is
    guaranteed everywhere."""
    r = _resting()
    shallow = sm.Route("s", (_rung("ch", -0.1, n_e=0, kind="chemical"),))
    c = rc.reset_contour(r, shallow)
    assert not c.reset_ok.any()
    assert c.hysteretic.all()
    deep = sm.Route("d", (_rung("ch", -0.3, n_e=0, kind="chemical"),))
    c2 = rc.reset_contour(r, deep)
    assert c2.reset_ok.all()
    assert c2.reset_fraction == pytest.approx(1.0)


def test_eps_widens_viable_but_not_the_guarantee() -> None:
    """eps tolerates a slightly endergonic rung for viability; the reset
    margin is measured from downhill, so a tolerated rung is *more*
    hysteretic, not more guaranteed."""
    r = _resting()
    route = sm.Route("main", (_rung("t", -0.1, eps_eV=0.3),))
    c = rc.reset_contour(r, route)
    # viable -0.1 + U <= 0.3 → U <= 0.4 → cols through 0.25
    assert np.array_equal(c.viable, ~_cols(r, 0.5, 0.75, 1.0))
    # guaranteed dg <= -0.2 → U <= -0.1 → cols through -0.25
    assert np.array_equal(c.reset_ok, _cols(r, -1.0, -0.75, -0.5, -0.25))
    assert np.array_equal(c.hysteretic, _cols(r, 0.0, 0.25))


def test_eta_is_a_parameter() -> None:
    r = _resting()
    route = sm.Route("main", (_rung("r1", -0.5),))
    c = rc.reset_contour(r, route, eta_eV=0.05)
    # guaranteed dg <= -0.05 → U <= 0.45 → cols through 0.25
    assert np.array_equal(c.reset_ok, ~_cols(r, 0.5, 0.75, 1.0))
    c = rc.reset_contour(r, route, eta_eV=0.6)
    # dg <= -0.6 → U <= -0.1 → cols through -0.25
    assert np.array_equal(c.reset_ok, _cols(r, -1.0, -0.75, -0.5, -0.25))
    assert c.eta_eV == pytest.approx(0.6)


def test_empty_route_is_vacuously_guaranteed() -> None:
    r = _resting()
    c = rc.reset_contour(r, sm.Route("empty"))
    assert c.reset_ok.all() and c.viable.all() and not c.hysteretic.any()
    assert c.edges == ()
    pt = c.at(0.0, 3.0)
    assert pt["margin"] == pytest.approx(np.inf)
    assert pt["limiting"] == ""


def test_reset_contour_ignores_the_resting_wanted_mask() -> None:
    """reset_ok is a property of the staircase alone — the caller
    intersects it into the window; a half-wanted resting map changes
    nothing."""
    wanted = _cols(_resting(), -1.0, -0.75, -0.5, -0.25, 0.0)
    c = rc.reset_contour(_resting(wanted), sm.Route("main", (_rung("r", -0.5),)))
    assert np.array_equal(c.reset_ok, ~_cols(_resting(), 0.5, 0.75, 1.0))


# ── the estimator ladder (bep / neb input slots on Rung) ─────────────────


def test_bep_margin_is_the_zero_barrier_driving_force() -> None:
    """E_a = 0.5·dg + 0.4 ≤ 0 → dg <= -0.8: the rung g0=-0.5, n_e=1 is
    guaranteed U <= -0.3 → cols -1..-0.5, labelled 'bep'."""
    r = _resting()
    route = sm.Route("main", (_rung("b", -0.5, bep=(0.5, 0.4)),))
    c = rc.reset_contour(r, route)
    assert c.estimators == ("bep",)
    assert c.edges[0].margin_eV == pytest.approx(0.8)
    assert np.array_equal(c.reset_ok, _cols(r, -1.0, -0.75, -0.5))


def test_bep_negative_intercept_is_barrierless_when_viable() -> None:
    """β < floor → m_e clipped at 0: guaranteed wherever the step is
    downhill — a barrierless step needs no overdrive."""
    r = _resting()
    route = sm.Route("main", (_rung("b", -0.5, bep=(0.5, -0.2)),))
    c = rc.reset_contour(r, route)
    assert c.edges[0].margin_eV == 0.0
    assert np.array_equal(c.reset_ok, c.viable)


def test_bep_floor_moves_the_margin() -> None:
    """floor = 0.1: E_a ≤ 0.1 → 0.5·dg + 0.4 ≤ 0.1 → dg <= -0.6."""
    r = _resting()
    route = sm.Route("main", (_rung("b", -0.5, bep=(0.5, 0.4)),))
    c = rc.reset_contour(r, route, ea_floor_eV=0.1)
    assert c.edges[0].margin_eV == pytest.approx(0.6)
    assert np.array_equal(c.reset_ok, _cols(r, -1.0, -0.75, -0.5, -0.25))


def test_bep_degenerate_alpha_falls_back_to_overdrive() -> None:
    """alpha <= 0 says the barrier doesn't drop with driving force —
    the fit can't extrapolate, so the edge is priced by flat η and the
    note says why."""
    r = _resting()
    route = sm.Route("main", (_rung("b", -0.5, bep=(0.0, 0.4)),))
    c = rc.reset_contour(r, route)
    assert c.estimators == ("overdrive",)
    assert c.edges[0].margin_eV == pytest.approx(rc.DEFAULT_ETA_EV)
    assert c.notes and "degenerate" in c.notes[0]


def test_neb_margin_is_the_measured_barrier() -> None:
    """A recorded forward barrier of 0.45 eV: guaranteed where the step
    is downhill by more than that — dg <= -0.45 → U <= 0.05."""
    r = _resting()
    route = sm.Route("main", (_rung("n", -0.5, barrier_ea=0.45),))
    c = rc.reset_contour(r, route)
    assert c.estimators == ("neb",)
    assert c.edges[0].margin_eV == pytest.approx(0.45)
    assert np.array_equal(c.reset_ok, _cols(r, -1.0, -0.75, -0.5, -0.25, 0.0))


def test_bep_wins_over_barrier_and_labels_stay_per_edge() -> None:
    """The ladder is per edge: a BEP-fitted edge prices itself, a
    barrier-only edge is 'neb', a bare edge stays 'overdrive'."""
    r = _resting()
    route = sm.Route(
        "main",
        (
            _rung("a", -0.5, bep=(0.5, 0.4), barrier_ea=0.9),
            _rung("b", -0.5, barrier_ea=0.05),
            _rung("c", -0.5),
        ),
    )
    c = rc.reset_contour(r, route)
    assert c.estimators == ("bep", "neb", "overdrive")
    margins = [e.margin_eV for e in c.edges]
    assert margins == pytest.approx([0.8, 0.05, 0.2])
    # the tightest edge (a, needs dg <= -0.8 → U <= -0.3) limits the route
    assert np.array_equal(c.reset_ok, _cols(r, -1.0, -0.75, -0.5))


def test_edge_rejects_an_unknown_estimator() -> None:
    with pytest.raises(ValueError, match="estimator"):
        rc.ResetEdge(rung=_rung("x", -0.5), estimator="guess", margin_eV=0.1)
