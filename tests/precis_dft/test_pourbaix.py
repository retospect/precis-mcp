"""Bulk Pourbaix diagram compute: grid map + SVG."""

from __future__ import annotations

import pytest

from precis_dft.jobs.pourbaix import (
    PourbaixEntry,
    PourbaixResult,
    compute,
    from_materials,
)

# ── Two-phase Pt / PtO ───────────────────────────────────────────


def _pt_simple_set() -> list[PourbaixEntry]:
    """Minimal three-phase set for Pt: metal (Pt), oxide (PtO), and
    dissolved Pt^2+. Numbers are illustrative; the test verifies the
    grid-min logic, not the chemistry."""
    return [
        PourbaixEntry(
            material_id="material:Pt_metal",
            formula="Pt",
            formation_energy=0.0,
            n_e=0,
            n_H=0,
            aqueous=False,
        ),
        # PtO ~ Pt + 0.5 O2 → 1 metal + 1 O; formation under H2O ref:
        # Pt + H2O → PtO + 2H+ + 2e- (ΔG ≈ 0.98 eV)
        PourbaixEntry(
            material_id="material:PtO",
            formula="PtO",
            formation_energy=0.98,
            n_e=2,
            n_H=2,
            aqueous=False,
        ),
        # Pt2+ ~ Pt - 2 e-; ΔG_diss = +0.7 eV roughly (very simplified)
        PourbaixEntry(
            material_id="material:Pt2plus",
            formula="Pt²⁺",
            formation_energy=2.4,
            n_e=2,
            n_H=0,
            aqueous=True,
        ),
    ]


class TestComputeSmall:
    def test_compute_returns_full_result(self) -> None:
        result = compute(entries=_pt_simple_set())
        assert isinstance(result, PourbaixResult)
        assert result.n_u >= 16
        assert result.n_ph >= 8
        # Grid is rectangular.
        assert all(len(row) == result.n_u for row in result.dominant_grid)
        assert len(result.dominant_grid) == result.n_ph

    def test_pt_metal_dominates_at_low_U_acid(self) -> None:
        """At negative U and pH 0, the metal is the stable phase
        (no driving force to oxidise or dissolve)."""
        result = compute(entries=_pt_simple_set())
        # Find the cell at (pH=0, U=-0.5).
        u_lo, u_hi = result.u_range
        ph_lo, ph_hi = result.ph_range
        j = round((((-0.5) - u_lo) / (u_hi - u_lo)) * (result.n_u - 1))
        i = round((0.0 - ph_lo) / (ph_hi - ph_lo) * (result.n_ph - 1))
        phase_idx = result.dominant_grid[i][j]
        assert result.entries[phase_idx].formula == "Pt"

    def test_oxide_dominates_at_high_U_high_pH(self) -> None:
        """At positive U and high pH, the oxide is favoured (high
        n_H makes the pH lever push it down)."""
        result = compute(entries=_pt_simple_set())
        u_lo, u_hi = result.u_range
        ph_lo, ph_hi = result.ph_range
        # (U=2.5, pH=14)
        j = round((2.5 - u_lo) / (u_hi - u_lo) * (result.n_u - 1))
        i = round((14.0 - ph_lo) / (ph_hi - ph_lo) * (result.n_ph - 1))
        phase_idx = result.dominant_grid[i][j]
        assert result.entries[phase_idx].formula == "PtO"

    def test_summary_lists_all_phases(self) -> None:
        result = compute(entries=_pt_simple_set())
        for entry in result.entries:
            assert entry.formula in result.summary

    def test_summary_carries_limitations_note(self) -> None:
        result = compute(entries=_pt_simple_set())
        assert "v1 Pourbaix" in result.summary
        assert "pymatgen" in result.summary

    def test_svg_is_self_contained(self) -> None:
        result = compute(entries=_pt_simple_set())
        assert result.svg.startswith("<svg")
        assert result.svg.endswith("</svg>")
        # No JavaScript, no external refs.
        assert "<script" not in result.svg
        assert "xlink:href" not in result.svg
        # Legend names each phase.
        assert "Pt" in result.svg
        assert "PtO" in result.svg


# ── Empty / degenerate ─────────────────────────────────────────────


class TestEmpty:
    def test_no_entries_returns_empty_svg(self) -> None:
        result = compute(entries=[])
        assert result.entries == []
        assert "no Pourbaix entries" in result.svg

    def test_single_phase_dominates_everywhere(self) -> None:
        entries = [
            PourbaixEntry(
                material_id="material:Pt_metal",
                formula="Pt",
                formation_energy=0.0,
            )
        ]
        result = compute(entries=entries)
        # Every cell points at index 0.
        for row in result.dominant_grid:
            for cell in row:
                assert cell == 0


# ── from_materials ────────────────────────────────────────────────


class TestFromMaterials:
    def test_extracts_entries_with_pourbaix_block(self) -> None:
        materials: list[tuple[str, dict]] = [
            (
                "material:Pt_metal",
                {
                    "derived": {
                        "pourbaix": {
                            "formula": "Pt",
                            "formation_energy": 0.0,
                            "n_e": 0,
                            "n_H": 0,
                            "aqueous": False,
                        }
                    }
                },
            ),
            (
                "material:Cu",
                {
                    "derived": {
                        # Missing pourbaix → skipped.
                    }
                },
            ),
            (
                "material:PtO",
                {
                    "derived": {
                        "pourbaix": {
                            "formula": "PtO",
                            "formation_energy": 0.98,
                            "n_e": 2,
                            "n_H": 2,
                        }
                    }
                },
            ),
        ]
        entries = from_materials(materials)
        ids = [e.material_id for e in entries]
        assert "material:Pt_metal" in ids
        assert "material:PtO" in ids
        assert "material:Cu" not in ids

    def test_malformed_pourbaix_block_is_skipped(self) -> None:
        materials = [
            (
                "material:bad",
                {
                    "derived": {
                        "pourbaix": {
                            # missing required formation_energy
                            "formula": "X"
                        }
                    }
                },
            ),
        ]
        entries = from_materials(materials)
        assert entries == []


# ── Sanity ────────────────────────────────────────────────────────


class TestChemicalPotential:
    def test_higher_U_favors_more_electron_phases(self) -> None:
        """The oxide phase has n_e=2; metal has n_e=0. At high U the
        oxide is favoured (the -n_e*U term pulls its μ down faster)."""
        from precis_dft.jobs.pourbaix import _mu

        metal = PourbaixEntry(
            material_id="material:M", formula="M", formation_energy=0.0
        )
        oxide = PourbaixEntry(
            material_id="material:MO",
            formula="MO",
            formation_energy=1.0,
            n_e=2,
            n_H=2,
        )
        # At U=0, pH=0: metal wins (μ_metal = 0 < μ_oxide = 1).
        assert _mu(metal, 0.0, 0.0) < _mu(oxide, 0.0, 0.0)
        # At U=2.0, pH=0: oxide wins (μ_oxide = 1 - 4 = -3 < 0).
        assert _mu(oxide, 2.0, 0.0) < _mu(metal, 2.0, 0.0)

    def test_higher_pH_favors_more_proton_phases(self) -> None:
        from precis_dft.jobs.pourbaix import _mu

        m = PourbaixEntry(material_id="material:M", formula="M", formation_energy=0.0)
        m_with_h = PourbaixEntry(
            material_id="material:MH",
            formula="MH",
            formation_energy=0.5,
            n_e=0,
            n_H=2,
        )
        # At pH=0: metal wins.
        assert _mu(m, 0.0, 0.0) < _mu(m_with_h, 0.0, 0.0)
        # At pH=14: μ_MH = 0.5 - 0.0592 * 2 * 14 ≈ -1.16 → MH wins.
        assert _mu(m_with_h, 0.0, 14.0) < _mu(m, 0.0, 14.0)


# ── Grid resolution ──────────────────────────────────────────────


class TestGridResolution:
    @pytest.mark.parametrize("n_u, n_ph", [(32, 8), (96, 28), (160, 40)])
    def test_grid_dimensions_match_request(self, n_u: int, n_ph: int) -> None:
        result = compute(entries=_pt_simple_set(), n_u=n_u, n_ph=n_ph)
        assert result.n_u == n_u
        assert result.n_ph == n_ph
        assert len(result.dominant_grid) == n_ph
        assert len(result.dominant_grid[0]) == n_u
