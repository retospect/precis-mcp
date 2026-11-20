"""Conditions-completeness check (`src/precis/taproot/conditions.py`).

Pure-function tests over real hub sentences from the 2026-11-15 prod
measurement (`docs/backlog/claim-conditions-completeness.md`), plus the
`lint_claim_sentence` wiring and the `precis taproot conditions-report`
aggregation (no DB: the aggregation helper takes plain rows).
"""

from __future__ import annotations

import pytest

from precis.cli import taproot as cli_taproot
from precis.nanopub.gates import _BLOCKING_LINT_CODES
from precis.taproot.conditions import (
    PROFILES,
    ConditionGap,
    ConditionProfile,
    check_conditions,
    condition_family,
)
from precis.taproot.sentence_lint import lint_claim_sentence

FI679690 = (
    "DFT calculations show introducing Pd on Cu turns the *NOH to *NHOH step "
    "from uphill 0.34 eV on Cu to downhill −0.17 eV on the Cu−Pd heterojunction."
)
FI637008 = (
    "DFT calculations at 0 V vs RHE in alkaline conditions find the "
    "rate-determining step on Fe/FexOy to be adsorbed NO₃ to NO₂ with a "
    "barrier of 0.14 eV, whereas on β-FeOOH it is adsorbed NOH hydrogenation "
    "with a barrier of 0.90 eV."
)
FI349380 = (
    "HUST-38, a water-coordinated Co-MOF, delivers 95.7% NH3 Faradaic "
    "efficiency and an NH3 yield rate of 13.38 mg h⁻¹ mg_cat⁻¹ at −0.6 V vs "
    "RHE, outperforming the non-water-coordinated control HUST-39 "
    "(3.98 mg h⁻¹ mg_cat⁻¹)."
)
FI349795 = (
    "With 3.0% Pt cocatalyst and 0.1 M ascorbic acid as sacrificial agent, "
    "Py-4F-COF's photocatalytic hydrogen-production rate increases to "
    "8253 µmol g⁻¹ h⁻¹."
)
FI637922 = (
    "Fixed-bed measurements over NiFe₂O₄/TiO₂ at 350 °C (350 ppm NO, "
    "2000 ppm H₂, N₂ balance, GHSV 50000 h⁻¹) show NH₃ selectivity in NO "
    "hydrogenation of ≈97% under light versus 52% in the dark."
)
FI348945 = (
    "DFT calculations identify *NO hydrogenation as the potential-determining "
    "step of nitrate reduction to NH₃ on CoP/Cu₃P, Cu₃P, and CoP, with uphill "
    "free-energy changes of 0.12, 0.23, and 0.29 eV respectively."
)
FI348892 = (
    "A 5 wt.% Pd-CeO₂/C catalyst shows a volcano-shaped potential dependence "
    "for nitrate-to-ammonia electroreduction, reaching a maximum NH₃ Faradaic "
    "efficiency of 98.0% at −0.4 V vs RHE."
)


def _only(sentence: str) -> ConditionGap:
    gaps = check_conditions(sentence)
    assert len(gaps) == 1, gaps
    return gaps[0]


# ── contract ─────────────────────────────────────────────────────────────


def test_empty_and_odd_inputs_never_raise() -> None:
    assert check_conditions("") == []
    for s in ["\x00", "}{[", "~" * 50, "≈" * 20, "a" * 5000]:
        assert check_conditions(s) == []


def test_profiles_cover_the_five_families() -> None:
    assert {p.family for p in PROFILES} == {
        "dft-step-thermo",
        "dft-barrier",
        "ec-measurement",
        "photo-rate",
        "thermal",
    }
    assert all(isinstance(p, ConditionProfile) and p.defaults for p in PROFILES)


def test_gap_is_frozen_with_actionable_hint() -> None:
    gap = _only(FI679690)
    with pytest.raises(AttributeError):
        setattr(gap, "family", "x")
    assert gap.hint.startswith("state ") and gap.hint.endswith(".")


# ── DFT step thermodynamics ──────────────────────────────────────────────


def test_dft_step_without_potential_flags() -> None:
    gap = _only(FI679690)
    assert gap.family == "dft-step-thermo"
    assert gap.missing == ("potential",)
    assert "U = 0 V vs RHE" in gap.hint


def test_dft_step_with_potential_is_clean() -> None:
    assert check_conditions(FI637008) == []
    assert (
        check_conditions(
            "DFT with the computational hydrogen electrode gives a limiting "
            "potential of the *NO to *NOH step of 0.4 eV."
        )
        == []
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "DFT gives ΔG_H* of 0.12 eV on MoS2 edge sites.",
        "DFT gives ΔG*H of -0.08 eV on Pt(111).",
        "DFT calculations find a ΔG(NO3*) of -0.8 eV on Cu(111).",
        "The computed adsorption free energy of NO3 on Cu is -0.9 eV.",
    ],
)
def test_dft_adsorption_descriptor_is_exempt(sentence: str) -> None:
    assert check_conditions(sentence) == []


def test_adsorption_descriptor_with_step_word_is_not_exempt() -> None:
    s = "DFT gives ΔG*NO3 of -0.8 eV and an uphill 0.4 eV step to *NO2."
    assert _only(s).family == "dft-step-thermo"


# ── DFT barriers ─────────────────────────────────────────────────────────


def test_dft_barrier_flags_only_electrochemical_step_without_potential() -> None:
    s = "DFT shows the electrochemical step *NO to *NOH has a barrier of 0.45 eV on Cu."
    assert _only(s).family == "dft-barrier"


def test_dft_chemical_barrier_is_clean() -> None:
    s = "DFT gives an N-N coupling barrier of 0.62 eV on Cu(111)."
    assert check_conditions(s) == []


def test_dft_barrier_with_potential_is_clean() -> None:
    s = (
        "DFT at U = 0 V vs RHE gives a barrier of 0.45 eV for the "
        "proton-electron transfer step."
    )
    assert check_conditions(s) == []


# ── electrochemical measurement ──────────────────────────────────────────


def test_ec_potential_without_electrolyte_flags_electrolyte_only() -> None:
    gap = _only(FI349380)
    assert gap.family == "ec-measurement"
    assert gap.missing == ("electrolyte",)


def test_field_emission_is_not_ec_measurement() -> None:
    # gr713709: fi192819 — vacuum field emission carries a current density
    # and a bare "V" threshold field but has no electrolyte to state. Both
    # the space unit and the gripe's slash variant stay out of ec-measurement.
    for tail in ("at 10 mA cm⁻²", "at 10 mA/cm²"):
        s = (
            "Field emission measurements show that carbon nanobud and "
            "nanographite (few-layer graphite flake) films have an "
            "emission-site density exceeding 10⁵ cm⁻² with threshold fields "
            f"of 1–2 V/µm {tail}."
        )
        assert condition_family(s) != "ec-measurement"
        assert check_conditions(s) == []


def test_ec_with_potential_reference_and_electrolyte_is_clean() -> None:
    assert check_conditions(FI348892 + " in 0.1 M KOH") == []


def test_ec_no_potential_no_current_flags_potential() -> None:
    s = "Pd/C reaches an NH₃ Faradaic efficiency of 89% in 0.1 M Na₂SO₄."
    assert _only(s).missing == ("potential",)


def test_ec_bare_volt_is_its_own_missing_reference() -> None:
    s = "Cu reaches 90% NH₃ Faradaic efficiency at −0.6 V in 0.1 M KOH."
    gap = _only(s)
    assert gap.missing == ("reference",)
    assert "vs RHE" in gap.hint


def test_ec_galvanostatic_device_claim_satisfies_potential() -> None:
    s = (
        "In a 0.5 M K₂SO₄ electrolyzer, Cu reaches a 90% NH₃ Faradaic "
        "efficiency at 100 mA cm⁻²."
    )
    assert check_conditions(s) == []
    s2 = "At a cell voltage of 2.1 V, the electrolyzer gives 85% Faradaic efficiency in 1 M KOH."
    assert check_conditions(s2) == []


def test_ec_zero_gap_mea_needs_no_liquid_electrolyte() -> None:
    s = (
        "In a zero-gap membrane electrode assembly, Pt/np-Co₂P reaches an "
        "NH₃ Faradaic efficiency of 100 ± 3.5% at 375 mA cm⁻²."
    )
    assert check_conditions(s) == []


# ── photocatalytic rate ──────────────────────────────────────────────────


def test_photo_rate_without_light_source_flags() -> None:
    gap = _only(FI349795)
    assert gap.family == "photo-rate"
    assert gap.missing == ("light",)


def test_photo_rate_with_light_source_is_clean() -> None:
    assert (
        check_conditions(FI349795[:-1] + " under a 300 W Xe lamp (λ > 420 nm).") == []
    )


def test_computed_sth_is_not_photo_rate() -> None:
    s = "The computed STH efficiency of the heterojunction reaches 12% with a 1.9 eV band gap."
    assert check_conditions(s) == []
    assert condition_family(s) is None


# ── thermal ──────────────────────────────────────────────────────────────


def test_thermal_with_temperature_is_clean() -> None:
    assert check_conditions(FI637922) == []


def test_thermal_without_temperature_flags() -> None:
    s = "Over Pt/Al₂O₃ in a fixed-bed reactor (GHSV 50000 h⁻¹) NO conversion reaches 95%."
    gap = _only(s)
    assert gap.family == "thermal"
    assert gap.missing == ("temperature", "feed")  # feed group added 2026-11-15


def test_thermal_does_not_fire_in_nitrate_electrochemical_context() -> None:
    s = "Nitrate conversion reaches 95% in a flow reactor over Cu."
    assert all(g.family != "thermal" for g in check_conditions(s))


# ── exempt: qualitative ──────────────────────────────────────────────────


def test_qualitative_mechanism_claim_is_exempt() -> None:
    s = (
        "DFT calculations show the *NOH to *NHOH step is the "
        "potential-determining step on Cu."
    )
    assert check_conditions(s) == []
    assert (
        check_conditions("Cu promotes nitrate electroreduction to ammonia selectively.")
        == []
    )


# ── lint_claim_sentence wiring ───────────────────────────────────────────


def test_lint_claim_sentence_surfaces_advisory_conditions_warning() -> None:
    hits = [
        w
        for w in lint_claim_sentence(FI679690)
        if w.startswith("conditions-incomplete")
    ]
    assert len(hits) == 1
    w = hits[0]
    assert w.startswith("conditions-incomplete:dft-step-thermo:")
    assert "potential" in w and "U = 0 V vs RHE" in w
    assert w.endswith("Advisory only.")


def test_lint_claim_sentence_clean_on_complete_sentence() -> None:
    assert not [
        w
        for w in lint_claim_sentence(FI637008)
        if w.startswith("conditions-incomplete")
    ]


def test_conditions_code_is_never_blocking() -> None:
    assert "conditions-incomplete" not in _BLOCKING_LINT_CODES
    assert not any(c.startswith("conditions-incomplete") for c in _BLOCKING_LINT_CODES)


# ── conditions-report aggregation ────────────────────────────────────────


def test_conditions_report_counts_families_gaps_and_bullets() -> None:
    rows = [
        (1, FI679690, 3),  # dft gap, 3 bullets
        (2, FI637008, 1),  # dft clean
        (3, FI349380, 2),  # ec gap
        (4, "Cu promotes nitrate electroreduction selectively.", 5),  # qualitative
    ]
    rep = cli_taproot._conditions_report(rows)
    assert rep["hubs"] == 4
    assert rep["families"]["dft-step-thermo"] == {
        "quantitative": 1,
        "gaps": 1,
        "bullets": 3,
    }
    assert rep["families"]["ec-measurement"] == {
        "quantitative": 1,
        "gaps": 1,
        "bullets": 2,
    }
    assert [(g["hub"], g["family"], g["missing"]) for g in rep["gaps"]] == [
        ("fi1", "dft-step-thermo", ["potential"]),
        ("fi3", "ec-measurement", ["electrolyte"]),
    ]


def test_conditions_report_subcommand_parses() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="precis")
    sub = parser.add_subparsers(dest="cmd")
    cli_taproot.add_parser(sub)
    a = parser.parse_args(
        [
            "taproot",
            "conditions-report",
            "--draft",
            "dr348633",
            "--json",
            "--limit",
            "5",
        ]
    )
    assert (a.taproot_cmd, a.draft, a.json, a.limit, a.all) == (
        "conditions-report",
        "dr348633",
        True,
        5,
        False,
    )
    with pytest.raises(SystemExit):  # one of --draft/--all is required
        parser.parse_args(["taproot", "conditions-report"])


def test_not_reported_marker_satisfies_its_group() -> None:
    from precis.taproot.conditions import not_reported_marker, reported_as_missing

    ec = "The Faradaic efficiency of NH3 reaches 90% on Cu."
    (gap,) = check_conditions(ec)
    assert "potential" in gap.missing
    marker = not_reported_marker(gap.missing)
    assert marker.endswith("not reported)")
    assert reported_as_missing(marker) == set(gap.missing)
    assert check_conditions(ec[:-1] + " " + marker + ".") == []
    # Only the named condition is satisfied.
    part = ec[:-1] + " (electrolyte not reported)."
    (rest,) = check_conditions(part)
    assert "electrolyte" not in rest.missing and "potential" in rest.missing
    # "not reported" outside a parenthetical marker does nothing.
    assert check_conditions("The potential was not reported; FE reaches 90%.")


def test_iron_is_not_faradaic_efficiency() -> None:
    assert condition_family("Fe catalysts achieve 90% NO conversion at 300 °C.") != (
        "ec-measurement"
    )


def test_light_source_needs_a_lamp_wavelength_or_power() -> None:
    base = (
        "Photocatalytic H2 evolution with methanol reaches a rate of 5 mmol h⁻¹ g⁻¹ {}."
    )
    for ok in ("under a 300 W Xe lamp", "under 420 nm light", "under AM 1.5 G"):
        assert check_conditions(base.format(ok)) == [], ok
    for bad in (
        "under visible light",
        "under UV light",
        "with a well enabled and controlled setup",
        "using 5 wt% Pt",
    ):
        (gap,) = check_conditions(base.format(bad))
        assert gap.missing == ("light",), bad


def test_electrolyte_molarity_is_case_sensitive() -> None:
    base = "The Faradaic efficiency is 90% at -0.5 V vs RHE{}."
    assert check_conditions(base.format(" in 0.5 M KCl")) == []
    assert check_conditions(base.format(" in 5 mM KCl")) == []
    (gap,) = check_conditions(base.format(" on a 5 mm electrode"))
    assert gap.missing == ("electrolyte",)


# ── 2026-11-15 revision against reporting standards ──────────────────────

FI349104 = (
    "DFT calculations show UV irradiation on the Cu2O-TiO2+x interface "
    "strengthens *H adsorption (ΔGabs shifting from 0.40 eV to −0.41 eV) and "
    "NO3− adsorption (ΔGabs shifting from −0.34 eV to −0.64 eV)."
)


def test_charged_adsorption_free_energy_is_not_exempt() -> None:
    gap = _only(FI349104)
    assert gap.family == "dft-step-thermo"
    assert gap.missing == ("potential",)
    for s in (
        "DFT gives a nitrate adsorption free energy of -0.5 eV on Cu(111).",
        "DFT gives ΔG_ads of NO₃⁻ of -0.6 eV on Cu(111).",
        "DFT gives ΔGads of the anion of -0.2 eV on Au.",
    ):
        assert _only(s).family == "dft-step-thermo", s
    clean = FI349104[:-1] + " at U = 0 V vs RHE (CHE model)."
    assert check_conditions(clean) == []


def test_neutral_adsorption_spellings_stay_exempt() -> None:
    for s in (
        "DFT gives ΔGabs of 0.12 eV for *H on MoS2.",
        "DFT gives ΔG_abs of 0.12 eV for H* on MoS2.",
        "DFT gives ΔG_ads of 0.12 eV for O* on Pt.",
        "DFT gives ΔGads of 0.3 eV for OH* on Pt.",
        "DFT gives an adsorption free energy of 0.3 eV for H* on Pt.",
    ):
        assert check_conditions(s) == [], s


def test_bare_charged_adsorption_energy_is_left_alone() -> None:
    s = "DFT gives a nitrate adsorption energy of -2.13 eV on Cu(111)."
    assert check_conditions(s) == []


def test_photo_h2_rate_needs_sacrificial_agent() -> None:
    s = "Photocatalytic H2 evolution reaches 5 mmol h⁻¹ g⁻¹ under a 300 W Xe lamp."
    assert _only(s).missing == ("sacrificial",)
    for ok in ("with 10 vol% methanol", "in pure water", "without sacrificial agent"):
        assert check_conditions(s[:-1] + f" {ok}.") == [], ok
    n = "Photocatalytic nitrate reduction reaches 80% conversion under a 300 W Xe lamp."
    assert _only(n).missing == ("sacrificial",)
    assert check_conditions(n[:-1] + " with formic acid as hole scavenger.") == []


def test_aqy_needs_wavelength_and_sth_needs_am15() -> None:
    aqy = "The AQY reaches 12% for photocatalytic ammonia production under a 300 W Xe lamp."
    assert _only(aqy).missing == ("wavelength",)
    assert check_conditions(aqy[:-1] + " at 420 nm.") == []
    sth = "The photocatalyst reaches an STH of 1.2% under a 300 W Xe lamp."
    assert _only(sth).missing == ("irradiance",)
    assert (
        check_conditions("The photocatalyst reaches an STH of 1.2% under AM 1.5G.")
        == []
    )


def test_thermal_needs_feed_composition() -> None:
    s = "Over Pt/Al₂O₃ in a fixed-bed reactor at 350 °C NO conversion reaches 95%."
    assert _only(s).missing == ("feed",)
    assert check_conditions(s[:-1] + " with 350 ppm NO and 2000 ppm H₂.") == []
    assert check_conditions(s[:-1] + " (H₂/NO = 5).") == []


def test_profiles_carry_sources_and_finding_slot() -> None:
    by = {p.family: p for p in PROFILES}
    assert "10.1021/jp047349j" in by["dft-step-thermo"].sources
    assert "10.1021/acscatal.6b00183" in by["thermal"].sources
    assert len(by["ec-measurement"].sources) == 3
    assert all(p.sources for p in PROFILES)
    assert by["ec-measurement"].finding == "fi689288"
    assert by["dft-step-thermo"].finding == by["dft-barrier"].finding == "fi689291"
    # photo-rate / thermal: their standards have no full text yet (2026-11-15).
    assert by["photo-rate"].finding is None and by["thermal"].finding is None
    gap = check_conditions(FI679690)[0]
    assert gap.hint.endswith("Rule: [fi689291].")
    assert any("onset potential" in d for d in by["ec-measurement"].defaults)
    assert (
        check_conditions(
            "The onset potential is -0.2 V vs RHE and Faradaic efficiency 90% in 0.1 M KOH."
        )
        == []
    )


def test_chemical_steps_need_no_potential() -> None:
    """H* dimerization (Tafel), desorption and dissociation are chemical
    steps under CHE: no -eU term (prod dry run 2026-11-15, fi349344)."""
    chem = (
        "DFT calculations show defective NiO gives a free energy change of "
        "-0.42 eV for H* dimerization to H2, versus -2.58 eV on pristine NiO."
    )
    assert check_conditions(chem) == []
    pds = (
        "DFT calculations identify *NH₃ desorption as the potential-determining "
        "step with ΔG = 0.62 eV on Cu(111)."
    )
    assert check_conditions(pds)[0].missing == ("potential",)


def test_her_free_energy_is_the_neutral_descriptor() -> None:
    for s in (
        "DFT calculations on N-doped SiC nanotubes give the most favorable HER "
        "Gibbs free energy change, ΔG = −0.072 eV.",
        "Scandium vacancies in Sc2CF2/MoSTe optimize the DFT-computed hydrogen "
        "evolution reaction free energy to −0.29 eV.",
    ):
        assert check_conditions(s) == [], s
