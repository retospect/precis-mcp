"""derive_* job_types — pure-numerical post-processing.

Two layers, same as the rest of the suite:

- The pure functions in ``precis_dft.jobs.derive`` (arithmetic +
  ASE harmonic thermo) tested directly.
- The ``derive_eads`` / ``derive_freeenergy`` shims tested via
  ``run()`` against the mock store, then the derived values fed
  back through ``reaction_evaluate`` to prove the round-trip the
  campaign relies on.
"""

from __future__ import annotations

import math

import pytest

from precis_dft.jobs.derive import (
    FREQ_FLOOR_CM1,
    adsorption_energy,
    harmonic_free_energy,
)

# ── adsorption_energy (pure) ─────────────────────────────────────


class TestAdsorptionEnergy:
    def test_norskov_oh_reference_arithmetic(self) -> None:
        # *OH ← E_H2O − ½E_H2. With E_bound−E_clean = −5.0 and
        # E_ref = (−14.0) − 0.5·(−7.0) = −10.5, E_ads = −5.0 − (−10.5) = 5.5.
        rec = adsorption_energy(
            e_bound=-105.0,
            e_clean=-100.0,
            species="*OH",
            scheme="norskov_water_h2",
            gas_energies={"h2o": -14.0, "h2": -7.0},
        )
        assert math.isclose(rec["components"]["E_ref"], -10.5)
        assert math.isclose(rec["value"], 5.5)
        assert rec["key"] == f"norskov_water_h2:{rec['conditions_hash']}"

    def test_ooh_reference_uses_two_water_minus_three_half_h2(self) -> None:
        # *OOH ← 2E_H2O − 3/2 E_H2.
        rec = adsorption_energy(
            e_bound=-110.0,
            e_clean=-100.0,
            species="*OOH",
            gas_energies={"h2o": -14.0, "h2": -7.0},
        )
        assert math.isclose(rec["components"]["E_ref"], 2 * -14.0 - 1.5 * -7.0)

    def test_molecular_o2_scheme(self) -> None:
        # *O ← ½O2.
        rec = adsorption_energy(
            e_bound=-101.0,
            e_clean=-100.0,
            species="*O",
            scheme="molecular_o2",
            gas_energies={"o2": -9.0, "h2": -7.0},
        )
        assert math.isclose(rec["components"]["E_ref"], 0.5 * -9.0)
        assert math.isclose(rec["value"], -1.0 - (-4.5))

    def test_che_explicit_shifts_by_n_h_times_u(self) -> None:
        base = adsorption_energy(
            e_bound=-105.0,
            e_clean=-100.0,
            species="*OH",
            scheme="norskov_water_h2",
            gas_energies={"h2o": -14.0, "h2": -7.0},
        )
        shifted = adsorption_energy(
            e_bound=-105.0,
            e_clean=-100.0,
            species="*OH",
            scheme="che_explicit",
            gas_energies={"h2o": -14.0, "h2": -7.0},
            conditions={"U_RHE": 1.23},
        )
        # One H in *OH, shifted by −1·1.23.
        assert math.isclose(shifted["value"], base["value"] - 1.23)
        # Different scheme + conditions ⇒ a distinct storage key.
        assert shifted["key"] != base["key"]

    def test_unsupported_element_raises(self) -> None:
        with pytest.raises(ValueError, match="only references O/H"):
            adsorption_energy(
                e_bound=-1.0,
                e_clean=0.0,
                species="*CO",
                gas_energies={"h2o": -14.0, "h2": -7.0},
            )

    def test_missing_gas_energy_raises(self) -> None:
        with pytest.raises(ValueError, match="needs gas energies"):
            adsorption_energy(
                e_bound=-1.0,
                e_clean=0.0,
                species="*OH",
                gas_energies={"h2": -7.0},  # no h2o
            )


# ── harmonic_free_energy (pure) ──────────────────────────────────


class TestHarmonicFreeEnergy:
    def test_zpe_is_half_sum_hbar_omega(self) -> None:
        from ase.units import invcm

        freqs = [200.0, 400.0, 1200.0]
        rec = harmonic_free_energy(e_tot=-100.0, frequencies_cm1=freqs)
        expected_zpe = 0.5 * sum(f * invcm for f in freqs)
        assert math.isclose(rec["zpe"], expected_zpe, rel_tol=1e-9)
        # G includes E_tot; with positive ZPE and small −TS it sits
        # just above E_tot at room temperature.
        assert rec["value"] > -100.0
        assert rec["n_floored"] == 0

    def test_soft_modes_are_floored(self) -> None:
        rec = harmonic_free_energy(
            e_tot=-100.0,
            frequencies_cm1=[10.0, 30.0, 800.0],
        )
        assert rec["n_floored"] == 2
        assert any("floored_2_modes" in w for w in rec["warnings"])

    def test_imaginary_modes_floored_by_magnitude(self) -> None:
        # Negative (imaginary) frequency taken by magnitude, then floored.
        rec = harmonic_free_energy(e_tot=-100.0, frequencies_cm1=[-25.0, 900.0])
        assert rec["n_floored"] == 1
        assert math.isfinite(rec["value"])

    def test_electrochemical_without_solvation_warns(self) -> None:
        rec = harmonic_free_energy(
            e_tot=-100.0,
            frequencies_cm1=[900.0],
            conditions={"electrochemical": True},
        )
        assert "no_solvation_correction" in rec["warnings"]

    def test_floor_constant_is_50(self) -> None:
        assert FREQ_FLOOR_CM1 == 50.0

    def test_empty_frequencies_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one frequency"):
            harmonic_free_energy(e_tot=-100.0, frequencies_cm1=[])


# ── shim round-trips against the mock store ──────────────────────
