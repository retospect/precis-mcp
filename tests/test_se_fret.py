"""precis_se's optical (FRET) domain — the physics module
(:mod:`precis_se.fret`) and its L4 view (``view='fret'``,
:func:`precis_se.handler._render_fret`).

Two layers, two kinds of test. The physics pins below check the closed
forms against hand-computable numbers and one literature pair (Cy3/Cy5) —
regressions here are wrong photophysics, not a rendering bug. The
competition invariant is the single most load-bearing test in the file:
a donor broadcasts to every acceptor in range and the branching ratios
share one denominator, so a bug that quotes :func:`~precis_se.fret.
pair_efficiency` per link in a dense network overstates every one of
them — exactly the trap the module's own docstring names.

The view tests are pure in-memory (:func:`_tree`, ``test_se_bom.py``'s
convention): no store, no migration — :func:`precis_se.handler.
_render_fret` reads a live :class:`~precis_se.ops.SeTree` and nothing
else, so a DB round-trip here would just be testing the persistence
layer's own tests a second time.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from precis_se import fret
from precis_se.handler import _render_fret
from precis_se.ops import SeTree, apply_ops


def _tree(ops: list[Any]) -> SeTree:
    return apply_ops(SeTree(), ops)


# ── physics pins ─────────────────────────────────────────────────────────


def test_kappa_squared_collinear_head_to_tail_is_four() -> None:
    # Both dipoles point straight along the separation vector, in the
    # same sense — the "head to tail" arrangement the module docstring
    # calls out as the κ² = 4 extreme.
    kappa_sq = fret.kappa_squared([0, 0, 1], [0, 0, 1], [0, 0, 5.0])
    assert kappa_sq == pytest.approx(4.0)


def test_kappa_squared_parallel_perpendicular_to_separation_is_one() -> None:
    kappa_sq = fret.kappa_squared([1, 0, 0], [1, 0, 0], [0, 0, 5.0])
    assert kappa_sq == pytest.approx(1.0)


def test_kappa_squared_mutually_perpendicular_and_to_r_is_zero() -> None:
    # Both dipoles perpendicular to the separation AND to each other —
    # the dead orientation :func:`precis_se.fret.regime` reports as
    # ``ORIENTATION_NULL``.
    kappa_sq = fret.kappa_squared([1, 0, 0], [0, 1, 0], [0, 0, 5.0])
    assert kappa_sq == pytest.approx(0.0)


def test_efficiency_is_one_half_at_r0() -> None:
    r0 = 5e-9
    assert fret.pair_efficiency(r0, r0) == pytest.approx(0.5)


def test_distance_sensitivity_at_half_efficiency_is_three() -> None:
    assert fret.distance_sensitivity(0.5) == pytest.approx(3.0)


def test_separation_for_efficiency_inverts_pair_efficiency() -> None:
    r0 = 5e-9
    target = 0.3
    r = fret.separation_for_efficiency(target, r0)
    assert fret.pair_efficiency(r, r0) == pytest.approx(target)


def test_forster_radius_lands_in_the_cy3_cy5_literature_band() -> None:
    # Lakowicz's own Cy3/Cy5 numbers — the assertion is the published
    # 5.0-5.4 nm band widened slightly for the prefactor's own rounding,
    # never a digit-for-digit pin (the literature source rounds too).
    r0_m = fret.forster_radius(
        overlap=7e15, quantum_yield=0.15, kappa_sq=2.0 / 3.0, refractive_index=1.4
    )
    r0_nm = r0_m * 1e9
    assert 4.8 <= r0_nm <= 5.6


# ── the competition invariant ────────────────────────────────────────────


def _flat_chromophore(label: str, *, absorption_peak: float) -> fret.Chromophore:
    """A chromophore whose emission/absorption overlap by construction —
    all three share the same triangular band, so :func:`overlap_integral`
    is nonzero without hand-tuning a realistic dye pair."""
    return fret.Chromophore(
        label=label,
        dipole=np.array([1.0, 0.0, 0.0]),
        quantum_yield=1.0,
        lifetime_s=1e-9,
        emission=fret.Spectrum.of([(500.0, 0.0), (550.0, 1.0), (600.0, 0.0)]),
        absorption=fret.Spectrum.of(
            [(500.0, 0.0), (550.0, absorption_peak), (600.0, 0.0)]
        ),
    )


def test_two_identical_acceptors_split_the_excitation_and_conserve_it() -> None:
    """The load-bearing behaviour: competing acceptors share one
    denominator. Placed close enough that the donor would transfer
    almost everything to a *single* acceptor (isolated efficiency ≈ 1),
    a second identical twin at the same distance halves each channel's
    share — not because "half" is a general law (it isn't; the exact
    relation is ``E_shared = E_alone / (1 + E_alone)``), but because at
    E_alone ≈ 1 that relation IS ≈ 0.5, which is exactly the regime a
    placement optimiser would reach for. A bug that reused
    :func:`~precis_se.fret.pair_efficiency` per acceptor instead of
    :func:`~precis_se.fret.solve_donor` would report both channels near
    1.0 instead — a large, easily caught discrepancy."""
    donor = _flat_chromophore("D", absorption_peak=1.0)
    acceptor = _flat_chromophore("A", absorption_peak=50000.0)
    overlap = fret.overlap_integral(donor.emission, acceptor.absorption)
    r0 = fret.forster_radius(
        overlap=overlap, quantum_yield=donor.quantum_yield, kappa_sq=1.0,
        refractive_index=1.4,
    )
    # Deep inside R0 (isolated efficiency ≈ 1) but still OUTSIDE the Dexter
    # crossover. Both halves matter: `pair_efficiency` is the bare formula
    # and will happily quote 0.9999 at 0.2 nm, while `solve_donor` gates on
    # `regime` and contributes NO rate there — so a test that picks its
    # separation as a fraction of R0 alone can land in the gap and read as
    # a competition bug when it is really a regime boundary.
    r = 0.25 * r0
    assert r > fret.DEXTER_CROSSOVER_M, (
        f"test geometry is inside the Dexter crossover ({r:.3e} m) — "
        "solve_donor declines to quote a rate there by design"
    )
    isolated = fret.pair_efficiency(r, r0)
    assert isolated > 0.999  # the "alone" reference this test's docstring needs

    acceptors = [
        fret.PairGeometry(
            block_uid=1,
            chromophore=acceptor,
            separation=np.array([0.0, 0.0, r]),
            world_dipole=np.array([1.0, 0.0, 0.0]),
        ),
        fret.PairGeometry(
            block_uid=2,
            chromophore=acceptor,
            separation=np.array([0.0, 0.0, -r]),
            world_dipole=np.array([1.0, 0.0, 0.0]),
        ),
    ]
    budget = fret.solve_donor(
        donor_uid=0,
        donor=donor,
        donor_world_dipole=np.array([1.0, 0.0, 0.0]),
        acceptors=acceptors,
        refractive_index=1.4,
    )
    assert len(budget.channels) == 2
    e1, e2 = budget.channels[0].efficiency, budget.channels[1].efficiency
    assert e1 == pytest.approx(e2)
    assert e1 == pytest.approx(isolated / 2.0, rel=1e-2)
    # The bookkeeping law: every excitation either transfers or decays.
    total = sum(ch.efficiency for ch in budget.channels)
    assert total + budget.residual_efficiency == pytest.approx(1.0)


# ── regime classification ────────────────────────────────────────────────


def test_half_nm_pair_is_dexter() -> None:
    assert fret.regime(0.5e-9, 5e-9, kappa_sq=1.0) is fret.Regime.DEXTER


def test_thirty_nm_pair_is_negligible() -> None:
    assert fret.regime(30e-9, 5e-9, kappa_sq=1.0) is fret.Regime.NEGLIGIBLE


def test_nulled_orientation_pair_is_orientation_null() -> None:
    assert fret.regime(3e-9, 5e-9, kappa_sq=0.0) is fret.Regime.ORIENTATION_NULL


def test_orientation_null_pair_contributes_zero_rate_to_the_budget() -> None:
    """A dead-orientation acceptor is genuinely not a channel — it must
    not silently steal probability from the donor's real channels."""
    donor = _flat_chromophore("D", absorption_peak=1.0)
    nulled = _flat_chromophore("A", absorption_peak=50000.0)
    budget = fret.solve_donor(
        donor_uid=0,
        donor=donor,
        donor_world_dipole=np.array([1.0, 0.0, 0.0]),
        acceptors=[
            fret.PairGeometry(
                block_uid=1,
                chromophore=nulled,
                separation=np.array([0.0, 0.0, 3e-9]),
                # perpendicular to both the donor dipole and the
                # separation vector — κ² = 0.
                world_dipole=np.array([0.0, 1.0, 0.0]),
            ),
        ],
        refractive_index=1.4,
    )
    (channel,) = budget.channels
    assert channel.regime is fret.Regime.ORIENTATION_NULL
    assert channel.rate_hz == 0.0
    assert channel.efficiency == 0.0
    assert budget.residual_efficiency == pytest.approx(1.0)


# ── the view ─────────────────────────────────────────────────────────────

_EMISSION = [[500.0, 0.0], [550.0, 1.0], [600.0, 0.0]]
_DONOR_ABSORPTION = [[400.0, 0.0], [450.0, 1.0], [500.0, 0.0]]
_ACCEPTOR_ABSORPTION = [[500.0, 0.0], [550.0, 50000.0], [600.0, 0.0]]


def _chromophore_op(block: str, *, dipole: list[float], acceptor: bool) -> dict[str, Any]:
    return {
        "op": "set_chromophore",
        "block": block,
        "label": block,
        "dipole": dipole,
        "quantum_yield": 1.0,
        "lifetime_s": 1e-9,
        "emission": _EMISSION,
        "absorption": _ACCEPTOR_ABSORPTION if acceptor else _DONOR_ABSORPTION,
    }


def _donor_acceptor_r0() -> float:
    """The R0 the view itself will compute for the donor/acceptor pair
    below, at the view's assumed default medium index — derived from the
    real physics functions, never hand-typed, so a prefactor change
    can't silently desync the test from the module it's testing."""
    overlap = fret.overlap_integral(
        fret.Spectrum.of(_EMISSION), fret.Spectrum.of(_ACCEPTOR_ABSORPTION)
    )
    return fret.forster_radius(
        overlap=overlap, quantum_yield=1.0, kappa_sq=1.0,
        refractive_index=fret.DEFAULT_MEDIUM_INDEX,
    )


def _base_ops(separation_m: float, *, acceptor_dipole: list[float]) -> list[dict[str, Any]]:
    return [
        {"op": "add_block", "name": "donor"},
        {"op": "add_block", "name": "acceptor"},
        {"op": "add_port", "block": "donor", "name": "p"},
        {"op": "add_port", "block": "acceptor", "name": "p"},
        {"op": "connect", "a": "donor.p", "b": "acceptor.p"},
        _chromophore_op("donor", dipole=[1, 0, 0], acceptor=False),
        _chromophore_op("acceptor", dipole=acceptor_dipole, acceptor=True),
        {"op": "set_pose", "block": "acceptor", "pose": [0, 0, separation_m]},
        {"op": "set_optical_link", "a": "donor.p", "b": "acceptor.p", "min_efficiency": 0.5},
    ]


def test_declared_link_below_r0_passes() -> None:
    r0 = _donor_acceptor_r0()
    body = _render_fret(_tree(_base_ops(0.7 * r0, acceptor_dipole=[1, 0, 0])))
    assert "PASS" in body
    assert "FAIL" not in body


def test_declared_link_above_r0_fails() -> None:
    r0 = _donor_acceptor_r0()
    body = _render_fret(_tree(_base_ops(1.5 * r0, acceptor_dipole=[1, 0, 0])))
    assert "FAIL" in body
    assert "requirement" in body


def test_orientation_nulled_declared_link_gets_a_rotation_finding() -> None:
    r0 = _donor_acceptor_r0()
    # donor dipole +x, acceptor dipole +y, separation +z — mutually
    # perpendicular and both perpendicular to the separation: κ² = 0.
    body = _render_fret(_tree(_base_ops(0.7 * r0, acceptor_dipole=[0, 1, 0])))
    assert "orientation-nulled" in body
    assert "rotat" in body


def test_undeclared_strongly_coupled_pair_is_a_crosstalk_finding() -> None:
    r0 = _donor_acceptor_r0()
    ops = _base_ops(1.5 * r0, acceptor_dipole=[1, 0, 0])  # the declared link: weak/FAIL
    ops += [
        {"op": "add_block", "name": "bystander"},
        _chromophore_op("bystander", dipole=[1, 0, 0], acceptor=True),
        # No connect/optical link to 'bystander' at all — an undeclared
        # pair, placed at R0 (50% efficiency) so it clears the notable
        # threshold easily.
        {"op": "set_pose", "block": "bystander", "pose": [0, 0, r0]},
    ]
    body = _render_fret(_tree(ops))
    assert "undeclared crosstalk" in body
    assert "bystander" in body


def test_empty_design_renders_without_raising() -> None:
    body = _render_fret(SeTree())
    assert "no optical domain" in body


def test_single_chromophore_design_renders_without_raising() -> None:
    tree = _tree(
        [
            {"op": "add_block", "name": "lonely"},
            _chromophore_op("lonely", dipole=[1, 0, 0], acceptor=False),
        ]
    )
    body = _render_fret(tree)
    assert "only one chromophore block" in body
    assert "lonely" in body


def test_header_says_assumed_when_optics_undeclared() -> None:
    tree = _tree(
        [
            {"op": "add_block", "name": "a"},
            {"op": "add_block", "name": "b"},
            _chromophore_op("a", dipole=[1, 0, 0], acceptor=False),
            _chromophore_op("b", dipole=[1, 0, 0], acceptor=True),
            {"op": "set_pose", "block": "b", "pose": [0, 0, 5e-9]},
        ]
    )
    body = _render_fret(tree)
    assert "ASSUMED" in body


def test_header_does_not_say_assumed_when_optics_declared() -> None:
    tree = _tree(
        [
            {"op": "add_block", "name": "a"},
            {"op": "add_block", "name": "b"},
            _chromophore_op("a", dipole=[1, 0, 0], acceptor=False),
            _chromophore_op("b", dipole=[1, 0, 0], acceptor=True),
            {"op": "set_pose", "block": "b", "pose": [0, 0, 5e-9]},
            {"op": "set_optics", "medium_index": 1.33},
        ]
    )
    body = _render_fret(tree)
    assert "ASSUMED" not in body
    assert "1.33" in body
