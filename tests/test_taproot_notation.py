"""Advisory notation lint (`src/precis/taproot/notation.py`) + its two wire-in
points (`taproot/authoring.py::seed_claim_hub`,
`taproot/hub.py::refine_claim_sentence`).

Pure-function unit tests for `lint_notation` and `normalize_notation`,
including the canon v2 ASCII->UTF-8 fallback table and the tilde/caret
carve-out fixes (`docs/backlog/nanopub-corpus-remediation.md` Phase 0/1);
the wire-in tests are DB-backed (real `refs`/`chunks` via the `store`
fixture), mirroring the setup style of `tests/test_taproot_hub.py` /
`tests/test_taproot_authoring.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.taproot.authoring import seed_claim_hub
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub, refine_claim_sentence
from precis.taproot.notation import lint_notation, normalize_notation
from tests.workers._helpers import seed_ref

_CLEAN_SENTENCE = (
    "Hexagonal boron nitride encapsulation raises graphene mobility above "
    "10⁵ cm² V⁻¹ s⁻¹ at 300 K."
)


# ── never raises ────────────────────────────────────────────────────────


def test_lint_notation_empty_string_returns_empty_list() -> None:
    assert lint_notation("") == []


def test_lint_notation_never_raises_on_odd_input() -> None:
    # Advisory-only contract: no exception for any string input, including
    # ones that could confuse a naive regex (unmatched braces, stray
    # unicode, control chars).
    for s in ["\x00", "}{[", "\\" * 50, "^" * 50, "/" * 20, "a" * 5000]:
        assert isinstance(lint_notation(s), list)


# ── each pattern fires, named specifically ─────────────────────────────


def test_caret_exponent_fires() -> None:
    warnings = lint_notation("Mobility reaches 10^5 cm^2 V^-1 s^-1.")
    assert any("caret-exponent" in w for w in warnings)


def test_tex_residue_fires_on_dollar() -> None:
    warnings = lint_notation("Binding to C$_{60}$ is observed.")
    assert any("tex-residue" in w for w in warnings)


def test_tex_residue_fires_on_backslash() -> None:
    warnings = lint_notation("The moment is $\\mu_B$ per atom.")
    assert any("tex-residue" in w for w in warnings)


def test_digit_grouping_fires() -> None:
    warnings = lint_notation("The defect density reaches 4,600 per cm2.")
    assert any("digit-grouping" in w for w in warnings)


def test_ascii_multiplication_fires() -> None:
    warnings = lint_notation("Conductivity was 4.6 x 10 S per cm.")
    assert any("ascii-multiplication" in w for w in warnings)


def test_tilde_approximation_fires() -> None:
    warnings = lint_notation("Opening angles were observed near ~19 degrees.")
    assert any("tilde-approximation" in w for w in warnings)


def test_ascii_minus_exponent_fires() -> None:
    warnings = lint_notation("Mobility is quoted in cm2 V-1 s-1.")
    assert any("ascii-minus-exponent" in w for w in warnings)


def test_two_denominator_solidus_fires_on_cm2_per_vs() -> None:
    warnings = lint_notation("Mobility reaches 10 cm²/Vs at 300 K.")
    assert any("two-denominator-solidus" in w for w in warnings)


# ── canon v2: ASCII -> UTF-8 symbol respellings ─────────────────────────


def test_ascii_plusminus_fires_on_slash_form() -> None:
    warnings = lint_notation("The gap is 25 +/- 2 K.")
    assert any("ascii-plusminus" in w for w in warnings)


def test_ascii_plusminus_fires_on_bare_form() -> None:
    warnings = lint_notation("The gap is 25 +- 2 K.")
    assert any("ascii-plusminus" in w for w in warnings)


def test_ascii_micro_fires_on_ug_after_number() -> None:
    warnings = lint_notation("A dose of 50 ug was applied.")
    assert any("ascii-micro" in w for w in warnings)


def test_ascii_micro_fires_on_ug_slash_unit() -> None:
    warnings = lint_notation("The IC50 was reported in ug/mL.")
    assert any("ascii-micro" in w for w in warnings)


def test_ascii_micro_fires_on_micro_prefix_word() -> None:
    warnings = lint_notation("The complex binds at 5 micromolar concentration.")
    assert any("ascii-micro" in w for w in warnings)


# ── the unit-NAME vs unit-SYMBOL guard (`_NUMERAL_LEFT`) ────────────────
# SI writes a symbol with a numerical value and the name in words. Every
# rule below fired on adjectival prose before 2026-08-20; each case is a
# real corpus sentence that a rewrite would have damaged.


@pytest.mark.parametrize(
    "sentence,code",
    [
        # no value for the symbol to qualify -- the name is correct here
        ("The complex binds at micromolar concentration.", "ascii-micro"),
        ("Microsecond quantum coherence times have been demonstrated.", "ascii-micro"),
        ("Switching speeds of about a microsecond were reached.", "ascii-micro"),
        ("Bayesian force fields enable micron-scale catalysis.", "ascii-micrometre"),
        (
            "Assembled into cell-scale (micrometre-dimension) containers.",
            "ascii-micrometre",
        ),
        # a spelled-out numeral is still not a numerical value
        (
            "Ballistic transport over ten micrometres at room temperature.",
            "ascii-micrometre",
        ),
    ],
)
def test_unit_name_without_a_numeral_is_left_spelled_out(
    sentence: str, code: str
) -> None:
    assert not any(code in w for w in lint_notation(sentence))
    out, applied = normalize_notation(sentence)
    assert code not in applied
    assert out == sentence


def test_ascii_plusminus_does_not_eat_an_oxidation_state() -> None:
    """`Zn2+-sensing` -> `Zn2±sensing` destroys the charge and yields a
    meaningless string. Tolerance needs a numeral on BOTH sides; here the
    right-hand side is a hyphenated compound adjective."""
    for sentence in (
        "A BODIPY Zn2+-sensing gate was coupled via inner-filter transfer.",
        "RFdiffusion designed square-planar Ni2+-binding histidine sites.",
    ):
        assert not any("ascii-plusminus" in w for w in lint_notation(sentence))
        out, applied = normalize_notation(sentence)
        assert "ascii-plusminus" not in applied
        assert out == sentence


def test_ascii_degrees_converts_every_occurrence_including_the_singular() -> None:
    """A rule that rewrites some occurrences of a unit must rewrite all of
    them -- `deg(?:rees)?` matched only the plural, so this sentence came
    out carrying two spellings of one unit."""
    out, applied = normalize_notation(
        "Optimised thermal ramps (80 to 60 degrees C at 1 degree C/min) fold it."
    )
    assert applied == ["ascii-degrees"]
    assert out == "Optimised thermal ramps (80 to 60 °C at 1 °C/min) fold it."


def test_ascii_micro_does_not_fire_inside_ordinary_word() -> None:
    warnings = lint_notation("A software bug was fixed before the plugin shipped.")
    assert not any("ascii-micro" in w for w in warnings)


def test_ascii_degrees_fires_on_degrees_c() -> None:
    warnings = lint_notation("Annealing was carried out at 25 degrees C.")
    assert any("ascii-degrees" in w for w in warnings)


def test_ascii_degrees_fires_on_deg_c() -> None:
    warnings = lint_notation("Annealing was carried out at 25 deg C.")
    assert any("ascii-degrees" in w for w in warnings)


def test_ascii_degrees_fires_on_degc() -> None:
    warnings = lint_notation("Annealing was carried out at 25 degC.")
    assert any("ascii-degrees" in w for w in warnings)


def test_ascii_ohm_fires_on_bare_ohm() -> None:
    warnings = lint_notation("Sheet resistance is 50 Ohm per square.")
    assert any("ascii-ohm" in w for w in warnings)


def test_ascii_ohm_fires_on_kohm() -> None:
    warnings = lint_notation("Sheet resistance is 50 kOhm per square.")
    assert any("ascii-ohm" in w for w in warnings)


def test_ascii_ohm_fires_on_mohm() -> None:
    warnings = lint_notation("Contact resistance is 2 MOhm.")
    assert any("ascii-ohm" in w for w in warnings)


def test_ascii_angstrom_fires_on_spelled_out_angstrom() -> None:
    warnings = lint_notation("The bond length is 1.5 Angstrom.")
    assert any("ascii-angstrom" in w for w in warnings)


def test_ascii_angstrom_fires_case_insensitively() -> None:
    warnings = lint_notation("The bond length is 1.5 angstrom.")
    assert any("ascii-angstrom" in w for w in warnings)


def test_ascii_micrometre_fires_on_micrometer() -> None:
    warnings = lint_notation("The film is 500 micrometer thick.")
    assert any("ascii-micrometre" in w for w in warnings)


def test_ascii_micrometre_fires_on_micron() -> None:
    warnings = lint_notation("The film is 500 micron thick.")
    assert any("ascii-micrometre" in w for w in warnings)


def test_ascii_micrometre_fires_on_um_as_unit() -> None:
    warnings = lint_notation("The film is 500 um thick.")
    assert any("ascii-micrometre" in w for w in warnings)


def test_ascii_micrometre_does_not_fire_inside_ordinary_word() -> None:
    warnings = lint_notation("The forum discussed aluminum and vacuum chamber design.")
    assert not any("ascii-micrometre" in w for w in warnings)


def test_e_notation_fires_on_lowercase_e() -> None:
    warnings = lint_notation("The defect density reaches 4.6e-6 per cm2.")
    assert any("e-notation" in w for w in warnings)


def test_e_notation_fires_on_uppercase_e() -> None:
    warnings = lint_notation("The defect density reaches 4.6E-6 per cm2.")
    assert any("e-notation" in w for w in warnings)


def test_e_notation_fires_on_positive_exponent() -> None:
    warnings = lint_notation("The count reaches 1e3 events.")
    assert any("e-notation" in w for w in warnings)


# ── canon v3: hyphen-numeric-range, ascii-x-multiplier ──────────────────


def test_hyphen_numeric_range_fires() -> None:
    warnings = lint_notation("The resin gold capacity was 300-800 mg/g.")
    assert any("hyphen-numeric-range" in w for w in warnings)


def test_hyphen_numeric_range_fires_on_ratio_before_x() -> None:
    # '10-100x' -- a ratio range; the en dash is still correct here even
    # though a separate rule ('ascii-x-multiplier') also fires on the 'x'.
    warnings = lint_notation("Switches show a 10-100x stiffness ratio.")
    assert any("hyphen-numeric-range" in w for w in warnings)


def test_hyphen_numeric_range_does_not_fire_on_nomenclature() -> None:
    for sentence in _NOMENCLATURE_HYPHEN_SENTENCES:
        warnings = lint_notation(sentence)
        assert not any("hyphen-numeric-range" in w for w in warnings), (
            sentence,
            warnings,
        )


def test_hyphen_numeric_range_does_not_fire_on_named_method() -> None:
    # 'M06-2X' -- a DFT functional name, not a numeric range.
    warnings = lint_notation("Energies were computed at SMD/M06-2X level.")
    assert not any("hyphen-numeric-range" in w for w in warnings)


def test_ascii_x_multiplier_fires() -> None:
    warnings = lint_notation("Antennae provide a 1.61x U/V selectivity gain.")
    assert any("ascii-x-multiplier" in w for w in warnings)


def test_ascii_x_multiplier_does_not_fire_on_grid_label() -> None:
    # '2x2' -- a grid/supercell label, not a multiplier; the 'x' is
    # followed by another digit, so no word boundary sits after it.
    warnings = lint_notation("A 2x2 supercell was used for the calculation.")
    assert not any("ascii-x-multiplier" in w for w in warnings)


# ── 2026-08-20 numeric-value policy: range-unit-repeated ─────────────────


def test_range_unit_repeated_fires_on_en_dash() -> None:
    warnings = lint_notation("The bulk modulus spans 9 GPa–12 GPa across samples.")
    assert any("range-unit-repeated" in w for w in warnings)


def test_range_unit_repeated_fires_on_ascii_hyphen() -> None:
    warnings = lint_notation("The bulk modulus spans 9 GPa-12 GPa across samples.")
    assert any("range-unit-repeated" in w for w in warnings)


def test_range_unit_repeated_does_not_fire_on_unit_stated_once() -> None:
    # The correctly-formed shape -- unit once, after the second endpoint.
    warnings = lint_notation("The bulk modulus spans 9–12 GPa across samples.")
    assert not any("range-unit-repeated" in w for w in warnings)


def test_range_unit_repeated_does_not_fire_on_genuine_unit_change() -> None:
    # Not a duplication -- the two endpoints carry different units.
    warnings = lint_notation("Pressure rose from 9 GPa-12 kPa during the ramp.")
    assert not any("range-unit-repeated" in w for w in warnings)


def test_range_unit_repeated_does_not_fire_on_named_method() -> None:
    warnings = lint_notation("Energies were computed at SMD/M06-2X level.")
    assert not any("range-unit-repeated" in w for w in warnings)


def test_range_unit_repeated_does_not_fire_on_nomenclature() -> None:
    for sentence in _NOMENCLATURE_HYPHEN_SENTENCES:
        warnings = lint_notation(sentence)
        assert not any("range-unit-repeated" in w for w in warnings), (
            sentence,
            warnings,
        )


def test_range_unit_repeated_auto_fixes() -> None:
    out, codes = normalize_notation(
        "The bulk modulus spans 9 GPa-12 GPa across samples."
    )
    assert "range-unit-repeated" in codes
    assert "9–12 GPa" in out
    assert "GPa-12 GPa" not in out


# ── canon v3: formula-ascii-subscript (lint-only, never auto-rewritten) ──


def test_formula_ascii_subscript_fires_on_fullerene() -> None:
    warnings = lint_notation("The C60 fullerene was covalently attached.")
    assert any("formula-ascii-subscript" in w for w in warnings)


def test_formula_ascii_subscript_fires_on_multi_element_formula() -> None:
    warnings = lint_notation("Fe3O4 nanoparticles catalysed the reaction.")
    assert any("formula-ascii-subscript" in w for w in warnings)


def test_formula_ascii_subscript_does_not_fire_on_ionic_charge() -> None:
    # 'Mg2+' wants a superscript, not a subscript -- flagging it under
    # this code would misdirect a fix, so it's excluded outright.
    warnings = lint_notation("Selectivity for Mg2+ over Li+ was measured.")
    assert not any("formula-ascii-subscript" in w for w in warnings)


def test_formula_ascii_subscript_does_not_fire_on_nomenclature_hyphens() -> None:
    for sentence in _NOMENCLATURE_HYPHEN_SENTENCES:
        warnings = lint_notation(sentence)
        assert not any("formula-ascii-subscript" in w for w in warnings), (
            sentence,
            warnings,
        )


def test_normalize_notation_never_touches_formula_ascii_subscript() -> None:
    # Advisory-only: normalize_notation must never rewrite this class,
    # even though lint_notation detects it (corpus dry run found ~1/3 of
    # naive matches are nomenclature, not stoichiometry -- see module
    # docstring's "canon v3" paragraph).
    for sentence in [
        "The C60 fullerene was covalently attached.",
        "Fe3O4 nanoparticles catalysed the reaction.",
    ]:
        out, codes = normalize_notation(sentence)
        assert "formula-ascii-subscript" not in codes
        assert out.rstrip(".") == sentence.rstrip("."), (sentence, out)


# ── canon v2: tilde/caret carve-out fixes ───────────────────────────────


def test_tilde_proportionality_does_not_fire() -> None:
    # 'E_g ~ 1/W' -- proportionality between two expressions, not
    # approximation of a bare quantity.
    warnings = lint_notation("The band gap follows E_g ~ 1/W scaling.")
    assert not any("tilde-approximation" in w for w in warnings)


def test_tilde_approximation_still_fires_next_to_quantity() -> None:
    warnings = lint_notation("Opening angles were observed near ~19 degrees.")
    assert any("tilde-approximation" in w for w in warnings)


def test_caret_letter_superscript_does_not_fire() -> None:
    # '2^N' -- a letter superscript; canon keeps these ASCII.
    warnings = lint_notation("Conductance scales as 2^N with system size.")
    assert not any("caret-exponent" in w for w in warnings)


def test_caret_digit_exponent_still_fires() -> None:
    warnings = lint_notation("Mobility reaches 10^5 cm^2 V^-1 s^-1.")
    assert any("caret-exponent" in w for w in warnings)


def test_letter_subscripts_never_warn() -> None:
    # K_d, E_g, ΔG_aq, R_Q -- Unicode has no subscript d/g; canon keeps
    # underscore form. No rule should push these toward Unicode.
    for sentence in [
        "K_d for the complex is 3 nM.",
        "E_g increases with strain.",
        "ΔG_aq favors the bound state.",
        "R_Q sets the resistance quantum.",
    ]:
        assert lint_notation(sentence) == [], sentence


def test_approx_spacing_relation_form_does_not_fire() -> None:
    # 'n ≈ 10²²' -- binary relation, symbol on the left, space on both
    # sides is correct.
    warnings = lint_notation("Carrier density n ≈ 10²² cm⁻³ is reported.")
    assert not any("approx-spacing" in w for w in warnings)


def test_approx_spacing_relation_missing_space_fires() -> None:
    warnings = lint_notation("Carrier density n≈10²² cm⁻³ is reported.")
    assert any("approx-spacing" in w for w in warnings)


def test_approx_spacing_quantity_form_does_not_fire() -> None:
    # '≈1 Å' -- modifying a bare quantity, no space is correct.
    warnings = lint_notation("The gap is ≈1 Å at the interface.")
    assert not any("approx-spacing" in w for w in warnings)


def test_approx_spacing_quantity_extra_space_fires() -> None:
    warnings = lint_notation("The gap is ≈ 1 Å at the interface.")
    assert any("approx-spacing" in w for w in warnings)


# ── negative cases — must NOT fire ──────────────────────────────────────


def test_single_denominator_solidus_units_do_not_fire() -> None:
    for sentence in [
        "Sheet conductance is reported in S/cm.",
        "Transconductance was measured as mA/µm.",
        "The tunneling spectrum dI/dV shows a gap.",
        "Subthreshold swing is 70 mV/dec.",
    ]:
        warnings = lint_notation(sentence)
        assert not any("two-denominator-solidus" in w for w in warnings), (
            sentence,
            warnings,
        )


def test_nomenclature_hyphens_do_not_fire_ascii_minus_exponent() -> None:
    warnings = lint_notation(
        "The 5-8-5 defect pair is observed in the graphene lattice."
    )
    assert not any("ascii-minus-exponent" in w for w in warnings)


# `ascii-minus-exponent` false-positive corpus audit
# (docs/backlog/nanopub-corpus-remediation.md): a naive "letter immediately
# before a hyphen-digit" pattern fired on chemical/material nomenclature
# 146-of-149 times corpus-wide. The rule now fires only when a *standalone*
# accepted unit symbol (`_ACCEPTED_DENOMINATORS`) immediately precedes the
# hyphen -- so `ZSM-5` never matches even though `M` alone is an accepted
# unit, because the token there is `ZSM`, not `M`.
_NOMENCLATURE_HYPHEN_SENTENCES: list[str] = [
    "Fe-ZSM-5 selectively catalyzes the reaction.",
    "MOF-74 exhibits open metal sites for gas capture.",
    "UiO-66 shows exceptional thermal stability.",
    "MIL-101(Fe) was synthesized under solvothermal conditions.",
    "PCN-222-MBA displayed enhanced catalytic turnover.",
    "TJU-21 was characterized by powder XRD.",
    "HKUST-1 exhibits a Langmuir surface area above 2000 m2 per gram.",
    "VOTCPP-PIF-1 was deployed as a chemiresistive sensor.",
    "Features below sub-10-nm scale were resolved by TEM.",
]


def test_nomenclature_hyphens_never_fire_ascii_minus_exponent_lint() -> None:
    for sentence in _NOMENCLATURE_HYPHEN_SENTENCES:
        warnings = lint_notation(sentence)
        assert not any("ascii-minus-exponent" in w for w in warnings), (
            sentence,
            warnings,
        )


def test_nomenclature_hyphens_survive_normalize_notation_unchanged() -> None:
    for sentence in _NOMENCLATURE_HYPHEN_SENTENCES:
        out, codes = normalize_notation(sentence)
        assert "ascii-minus-exponent" not in codes, (sentence, codes)
        # No rule in this table should touch the sentence at all -- compare
        # ignoring only the unrelated no-terminal-period fix.
        assert out.rstrip(".") == sentence.rstrip("."), (sentence, out)


def test_real_unit_minus_exponents_still_fire_and_convert() -> None:
    # The discriminator: a standalone accepted unit symbol (not a
    # multi-letter compound-name token) immediately left of the hyphen.
    cases = [
        ("Mobility was 500 cm2 V-1 s-1.", "V⁻¹ s⁻¹"),
        ("The rate constant is 4.2 M-1 s-1.", "M⁻¹ s⁻¹"),
        ("Sheet resistance falls off as cm-2.", "cm⁻²"),
    ]
    for sentence, expected_fragment in cases:
        warnings = lint_notation(sentence)
        assert any("ascii-minus-exponent" in w for w in warnings), sentence
        out, codes = normalize_notation(sentence)
        assert "ascii-minus-exponent" in codes, (sentence, codes)
        assert expected_fragment in out, (sentence, out)


def test_fully_canon_compliant_sentence_produces_no_warnings() -> None:
    assert lint_notation(_CLEAN_SENTENCE) == []


# ── normalize_notation: deterministic fixes ─────────────────────────────

# (input, expected_output, expected_applied_codes) -- one row per
# mechanically-unambiguous rule, plus a combined/idempotence/no-touch
# corpus below.
_NORMALIZE_CASES: list[tuple[str, str, list[str]]] = [
    (
        "The defect density reaches 4,600 per cm2.",
        "The defect density reaches 4600 per cm2.",
        ["digit-grouping"],
    ),
    (
        "The defect density reaches 1,234,567 per cm2.",
        "The defect density reaches 1234567 per cm2.",
        ["digit-grouping"],
    ),
    (
        "Mobility reaches 10^5 cm^2 V^-1 s^-1",
        "Mobility reaches 10⁵ cm² V⁻¹ s⁻¹.",
        ["caret-exponent", "no-terminal-period"],
    ),
    (
        "Conductivity was 4.6 x 10 S per cm.",
        "Conductivity was 4.6×10 S per cm.",
        ["ascii-multiplication"],
    ),
    (
        "The defect density reaches 4.6e-6 per cm2.",
        "The defect density reaches 4.6×10⁻⁶ per cm2.",
        ["e-notation"],
    ),
    (
        "The gap is 25 +/- 2 K.",
        "The gap is 25 ± 2 K.",
        ["ascii-plusminus"],
    ),
    (
        "The gap is 25 +- 2 K.",
        "The gap is 25 ± 2 K.",
        ["ascii-plusminus"],
    ),
    (
        "A dose of 50 ug was applied.",
        "A dose of 50 µg was applied.",
        ["ascii-micro"],
    ),
    (
        "The IC50 was reported in ug/mL.",
        "The IC50 was reported in µg/mL.",
        ["ascii-micro"],
    ),
    (
        "The complex binds at 5 micromolar concentration.",
        "The complex binds at 5 µM concentration.",
        ["ascii-micro"],
    ),
    (
        # a hyphen between value and name is a compound adjective, kept
        "Entropy penalty per bond on 1-micrometre colloids is -14.6R.",
        "Entropy penalty per bond on 1-µm colloids is -14.6R.",
        ["ascii-micrometre"],
    ),
    (
        "Annealing was carried out at 25 degrees C.",
        "Annealing was carried out at 25 °C.",
        ["ascii-degrees"],
    ),
    (
        "Annealing was carried out at 25 deg C.",
        "Annealing was carried out at 25 °C.",
        ["ascii-degrees"],
    ),
    (
        "Sheet resistance is 50 Ohm per square.",
        "Sheet resistance is 50 Ω per square.",
        ["ascii-ohm"],
    ),
    (
        "Sheet resistance is 50 kOhm per square.",
        "Sheet resistance is 50 kΩ per square.",
        ["ascii-ohm"],
    ),
    (
        "The bond length is 1.5 Angstrom.",
        "The bond length is 1.5 Å.",
        ["ascii-angstrom"],
    ),
    (
        "The film is 500 micrometer thick.",
        "The film is 500 µm thick.",
        ["ascii-micrometre"],
    ),
    (
        "The film is 500 micron thick.",
        "The film is 500 µm thick.",
        ["ascii-micrometre"],
    ),
    (
        "The film is 500 um thick.",
        "The film is 500 µm thick.",
        ["ascii-micrometre"],
    ),
    (
        "Binding to C$_{60}$ is observed",
        "Binding to C₆₀ is observed.",
        ["tex-residue", "no-terminal-period"],
    ),
    (
        "Mobility reaches 10$^{2}$",
        "Mobility reaches 10².",
        ["tex-residue", "no-terminal-period"],
    ),
    (
        "Mobility is quoted in cm2 V-1 s-1.",
        "Mobility is quoted in cm2 V⁻¹ s⁻¹.",
        ["ascii-minus-exponent"],
    ),
    (
        "DFT predicts a 1.2 eV band gap",
        "DFT predicts a 1.2 eV band gap.",
        ["no-terminal-period"],
    ),
    (
        "The resin gold capacity was 300-800 mg/g.",
        "The resin gold capacity was 300–800 mg/g.",
        ["hyphen-numeric-range"],
    ),
    (
        "Antennae provide a 1.61x U/V selectivity gain.",
        "Antennae provide a 1.61× U/V selectivity gain.",
        ["ascii-x-multiplier"],
    ),
    (
        "Switches show a 10-100x stiffness ratio.",
        "Switches show a 10–100× stiffness ratio.",
        ["hyphen-numeric-range", "ascii-x-multiplier"],
    ),
    (
        "The bulk modulus spans 9 GPa-12 GPa across samples.",
        "The bulk modulus spans 9–12 GPa across samples.",
        ["range-unit-repeated"],
    ),
]


def test_normalize_notation_applies_each_rule() -> None:
    for sentence, expected_output, expected_codes in _NORMALIZE_CASES:
        out, codes = normalize_notation(sentence)
        assert out == expected_output, (sentence, out, expected_output)
        assert codes == expected_codes, (sentence, codes, expected_codes)


def test_normalize_notation_empty_string_returns_empty() -> None:
    assert normalize_notation("") == ("", [])


def test_normalize_notation_never_raises_on_odd_input() -> None:
    for s in ["\x00", "}{[", "\\" * 50, "^" * 50, "/" * 20, "a" * 5000]:
        out, codes = normalize_notation(s)
        assert isinstance(out, str)
        assert isinstance(codes, list)


def test_normalize_notation_leaves_two_denominator_solidus_alone() -> None:
    # Which factor moves under the exponent is a judgment call -- never
    # mechanically rewritten.
    sentence = "Mobility reaches 10 cm²/Vs at 300 K."
    out, codes = normalize_notation(sentence)
    assert out == sentence
    assert codes == []


def test_normalize_notation_leaves_letter_subscripts_and_superscripts_alone() -> None:
    # K_d, E_g, ΔG_aq, R_Q, 2^N -- canon v2 keeps these ASCII.
    for sentence in [
        "K_d for the complex is 3 nM.",
        "E_g increases with strain.",
        "ΔG_aq favors the bound state.",
        "R_Q sets the resistance quantum.",
        "Conductance scales as 2^N with system size.",
    ]:
        out, codes = normalize_notation(sentence)
        assert out == sentence, sentence
        assert codes == [], (sentence, codes)


def test_normalize_notation_leaves_ending_question_bang_colon_alone() -> None:
    for sentence in [
        "Does this hold at 300 K?",
        "The gap closes entirely!",
        "Findings for review:",
    ]:
        out, codes = normalize_notation(sentence)
        assert out == sentence
        assert "no-terminal-period" not in codes


def test_normalize_notation_does_not_double_count_combined_micro_rule() -> None:
    # Both the 'ug' sub-rule and the 'micro<unit>' sub-rule fire in one
    # sentence -- 'ascii-micro' must still appear exactly once.
    out, codes = normalize_notation(
        "A 50 ug dose at 5 micromolar concentration was applied."
    )
    assert codes.count("ascii-micro") == 1
    assert "µg" in out and "µM" in out


def test_normalize_notation_idempotent_over_corpus() -> None:
    corpus = [s for s, _, _ in _NORMALIZE_CASES] + [
        _CLEAN_SENTENCE,
        "Mobility reaches 10 cm²/Vs at 300 K.",
        "K_d for the complex is 3 nM.",
        "Conductance scales as 2^N with system size.",
        "The 5-8-5 defect pair is observed in the graphene lattice.",
        "",
    ]
    for sentence in corpus:
        first, _ = normalize_notation(sentence)
        second, _ = normalize_notation(first)
        assert second == first, sentence


def test_normalize_notation_never_truncates() -> None:
    # No individual rule in this module shrinks a matched span by more
    # than ~8 chars (the largest is a spelled-out word collapsing to a
    # 2-char SI-prefixed unit, e.g. 'micrometer' -> 'µm'); a generous
    # 20-char-per-sentence budget catches a real truncation bug (e.g. an
    # accidental slice) while tolerating legitimate respelling shrinkage.
    corpus = [s for s, _, _ in _NORMALIZE_CASES] + [_CLEAN_SENTENCE]
    for sentence in corpus:
        out, _ = normalize_notation(sentence)
        assert len(out) >= len(sentence) - 20, (sentence, out)


# ── wire-in: seed_claim_hub carries 'notation' ──────────────────────────


def test_seed_claim_hub_succeeds_and_carries_notation_key(store: Any) -> None:
    paper = seed_ref(store, title="Collins 2006", kind="paper")

    out = seed_claim_hub(
        store,
        sentence="Mobility reaches 10^5 cm^2 V^-1 s^-1 at 300 K.",
        scope={},
        supporters=[{"paper": paper, "role": "corroborates", "source_handle": "pc1"}],
    )

    assert out["attached"] == 1
    assert "notation" in out
    assert any("caret-exponent" in w for w in out["notation"])


def test_seed_claim_hub_clean_sentence_has_no_notation_warnings(store: Any) -> None:
    paper = seed_ref(store, title="Novoselov 2007", kind="paper")

    out = seed_claim_hub(
        store,
        sentence=_CLEAN_SENTENCE,
        scope={},
        supporters=[{"paper": paper, "role": "corroborates"}],
    )

    assert out["notation"] == []


# ── wire-in: refine_claim_sentence carries 'notation' ───────────────────


def test_refine_claim_sentence_succeeds_and_carries_notation_key(store: Any) -> None:
    claim = CanonicalClaim(
        sentence="Pd/C catalyzes Suzuki coupling at room temperature.",
        scope={"material": "Pd/C"},
    )
    hub = mint_hub(store, claim)

    result = refine_claim_sentence(
        store, hub, "The defect density reaches 4,600 per cm2."
    )

    assert result["hub_ref_id"] == hub
    assert "notation" in result
    assert any("digit-grouping" in w for w in result["notation"])


def test_refine_claim_sentence_clean_sentence_has_no_notation_warnings(
    store: Any,
) -> None:
    claim = CanonicalClaim(
        sentence="Pd/C catalyzes Suzuki coupling at room temperature.",
        scope={"material": "Pd/C"},
    )
    hub = mint_hub(store, claim)

    result = refine_claim_sentence(store, hub, _CLEAN_SENTENCE)

    assert result["notation"] == []
