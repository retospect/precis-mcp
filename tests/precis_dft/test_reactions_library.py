"""Built-in reaction_network library loads and exposes expected ids."""

from __future__ import annotations

import precis_dft.reactions as reactions

_EXPECTED_IDS = {
    "reaction:oer_4step_acid",
    "reaction:oer_4step_alkaline",
    "reaction:oer_lom",
    "reaction:oer_dual_site",
    "reaction:her_volmer_heyrovsky",
    "reaction:her_volmer_tafel",
    "reaction:orr_4step_associative",
    "reaction:orr_4step_dissociative",
    "reaction:co2rr_to_co",
    "reaction:co2rr_to_methanol",
    "reaction:co2rr_to_ethylene",
    "reaction:nrr_alternating",
    "reaction:nrr_distal",
}


class TestLibrary:
    def test_load_library_returns_all_expected_networks(self) -> None:
        lib = reactions.load_library()
        assert set(lib) == _EXPECTED_IDS

    def test_every_network_has_species_and_steps(self) -> None:
        lib = reactions.load_library()
        for nid, net in lib.items():
            assert net["species"], f"{nid} has no species"
            assert net["steps"], f"{nid} has no steps"

    def test_oer_acid_has_four_pcet_steps(self) -> None:
        lib = reactions.load_library()
        oer = lib["reaction:oer_4step_acid"]
        steps = oer["steps"]
        assert len(steps) == 4
        assert all(int(s.get("n_e", 0)) == 1 for s in steps)

    def test_her_v_h_has_two_pcet_steps(self) -> None:
        lib = reactions.load_library()
        her = lib["reaction:her_volmer_heyrovsky"]
        pcet_steps = [s for s in her["steps"] if int(s.get("n_e", 0)) > 0]
        assert len(pcet_steps) == 2


class TestConditionsHash:
    def test_same_conditions_same_hash(self) -> None:
        a = reactions.conditions_hash({"T": 298, "pH": 0, "U_RHE": 1.23})
        b = reactions.conditions_hash({"U_RHE": 1.23, "pH": 0, "T": 298})
        assert a == b  # key order should not matter

    def test_different_conditions_different_hash(self) -> None:
        a = reactions.conditions_hash({"T": 298, "pH": 0, "U_RHE": 1.23})
        b = reactions.conditions_hash({"T": 298, "pH": 0, "U_RHE": 0.0})
        assert a != b


class TestEvaluate:
    """The OER-on-Pt(111) sanity check.

    Using representative literature-style values for Pt(111) at
    U=0, the limiting step is *O → *OOH (step 3), and the
    overpotential is about 0.4-0.9 V. Exact numbers vary by
    functional + solvation, so we test the framework structure:
    four step ΔGs, RDS at the predicted step, η nonnegative.
    """

    def test_oer_pt111_gives_step3_rds(self) -> None:
        lib = reactions.load_library()
        oer = lib["reaction:oer_4step_acid"]
        # Pt(111)-like CHE-referenced free energies (eV).
        # Numbers are illustrative; the test is on the framework.
        G = {"*OH": 0.71, "*O": 1.74, "*OOH": 3.84}
        out = reactions.evaluate(
            oer,
            free_energies=G,
            conditions={"T": 298, "pH": 0, "U_RHE": 0.0},
        )

        assert len(out["step_dG"]) == 4
        # Step 3 (*O → *OOH) should be limiting with these values.
        assert out["rate_determining_step"] == 2  # 0-indexed
        # η = max(ΔG_i) - 1.23 ≈ 2.10 - 1.23 = 0.87 V
        assert 0.7 < out["overpotential"] < 1.0
        assert out["u_equilibrium"] == 1.23
        assert out["missing_intermediates"] == []

    def test_evaluate_records_missing_species(self) -> None:
        lib = reactions.load_library()
        oer = lib["reaction:oer_4step_acid"]
        # *OOH missing → reported in missing_intermediates.
        out = reactions.evaluate(
            oer,
            free_energies={"*OH": 0.71, "*O": 1.74},
        )
        assert "*OOH" in out["missing_intermediates"]

    def test_her_volmer_heyrovsky_runs(self) -> None:
        lib = reactions.load_library()
        her = lib["reaction:her_volmer_heyrovsky"]
        # *H near 0 → near-zero overpotential for Pt-like surfaces.
        out = reactions.evaluate(her, free_energies={"*H": 0.05})
        assert len(out["step_dG"]) == 2
        # Overpotential should be small.
        assert out["overpotential"] < 0.2
