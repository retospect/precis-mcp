"""Pure term coverage (``precis.taproot.coverage``): acronym detection,
in-paper acronym expansion (Schwartz–Hearst style), and which claim terms
no passage carries. No DB."""

from __future__ import annotations

import pytest

from precis.taproot import coverage
from precis.taproot.coverage import (
    KIND_ACRONYM,
    KIND_MODE,
    KIND_NUMBER,
    Term,
    acronym_map,
    claim_terms,
    find_term,
    uncovered_terms,
)

#: fi189535's paper, in miniature.
_PAPER = (
    "High-resolution transmission electron microscopy (TEM) and scanning "
    "tunnelling microscopy (STM) and spectroscopy (STS) were used. "
    "Our atomistic density functional theory (DFT) calculations agree. "
    "The non-equilibrium Green's function (NEGF) method was applied to "
    "hexaiminotriphenylene (HITP) films, with B3LYP (Becke, 3-parameter, "
    "Lee-Yang-Parr) hybrids. MOF (metal-organic framework) films were made."
)


def _kinds(terms: list[Term], kind: str) -> list[str]:
    return [t.text for t in terms if t.kind == kind]


# ------------------------------------------------------------ claim terms


def test_acronyms_detected_with_digits_and_length_bounds() -> None:
    terms = claim_terms("TEM, STS, DFT, NEGF, B3LYP and HITP all appear.")
    assert _kinds(terms, KIND_ACRONYM) == [
        "TEM",
        "STS",
        "DFT",
        "NEGF",
        "B3LYP",
        "HITP",
    ]


@pytest.mark.parametrize(
    "token",
    [
        "eV",  # units are mixed case: never match the all-caps shape
        "mV",
        "nm",
        "K",  # one capital
        "GHz",
        "III",  # roman numerals
        "IV",
        "XII",
        "CO2",  # formulas: all single-letter elements + a digit
        "H2O",
        "CH4",
        "NO",  # two single-letter elements
        "HF",
        "TiO2",  # mixed-case formula
        "NaCl",
        "C60",  # one capital
        "2D",  # starts with a digit
        "NM",  # all-caps unit on the stoplist
        "GPA",
    ],
)
def test_units_numerals_and_formulas_are_not_acronyms(token: str) -> None:
    assert _kinds(claim_terms(f"The value {token} was reported."), KIND_ACRONYM) == []


def test_method_acronyms_that_look_like_elements_survive() -> None:
    # Three+ letters from single-letter elements, no digit: kept (PVC, KOH);
    # a letter outside the element set makes any token an acronym.
    assert _kinds(claim_terms("PVC and KOH and CNT and MD."), KIND_ACRONYM) == [
        "PVC",
        "KOH",
        "CNT",
        "MD",
    ]


def test_numbers_use_the_migrate_tokenizer_and_drop_units_and_dimensions() -> None:
    terms = claim_terms("A 5nm gap, 2.5 eV, 400:1 ratio, 3D lattice, 10^4-10^6 range.")
    nums = _kinds(terms, KIND_NUMBER)
    assert "5" in nums and "2.5" in nums and "400" in nums
    assert "3" not in nums  # 3D is a dimension, not a measurement
    assert "eV" not in nums


def test_modes_come_from_the_epistemic_lint() -> None:
    terms = claim_terms("Calculations and measurements disagree.")
    assert _kinds(terms, KIND_MODE) == ["Calculations", "measurements"]


# ---------------------------------------------------------- acronym map


def test_acronym_map_long_form_then_acronym() -> None:
    amap = acronym_map([_PAPER])
    assert amap["TEM"][0].words == "transmission electron microscopy"
    assert amap["DFT"][0].keys == ("densit", "functional", "theor")
    assert amap["NEGF"][0].keys[-1] == "function"
    assert amap["B3LYP"][0].keys[0] == "becke"


def test_acronym_map_acronym_then_long_form() -> None:
    amap = acronym_map(
        [
            "We use the SCC-DFTB (self-consistent charge density functional tight binding) scheme."
        ]
    )
    # SF before the parenthesis: DFTB is the word beside it, hyphenated SCC is split off.
    assert "DFTB" in amap
    assert amap["DFTB"][0].keys[-1] == "binding"


def test_split_phrase_stm_and_spectroscopy_sts() -> None:
    """ "scanning tunnelling microscopy (STM) and spectroscopy (STS)": STS
    expands to scanning ... tunnelling ... spectroscopy."""
    amap = acronym_map([_PAPER])
    sts = amap["STS"][0]
    assert sts.keys == ("scann", "tunnel", "spectroscop")
    assert amap["STM"][0].keys == ("scann", "tunnel", "microscop")
    # So the human's literal search phrase is found through the map:
    assert (
        find_term(Term(KIND_ACRONYM, "STS"), "tunnelling spectroscopy images", amap)
        is not None
    )
    # ...and so is the paper's own split phrasing, but NOT the STM-only text.
    split = "scanning tunnelling microscopy (STM) and spectroscopy"
    assert find_term(Term(KIND_ACRONYM, "STS"), split, amap) is not None
    assert (
        find_term(
            Term(KIND_ACRONYM, "STS"), "scanning tunnelling microscopy only", amap
        )
        is None
    )


def test_single_word_expansion_by_classic_sh_fallback() -> None:
    amap = acronym_map([_PAPER])
    assert amap["HITP"][0].keys == ("hexaiminotriphenylene",)


def test_no_global_list_an_undefined_acronym_has_no_expansion() -> None:
    assert acronym_map(["Samples were imaged by TEM at 200 kV."]) == {}


def test_non_acronym_parentheticals_are_not_mapped() -> None:
    assert (
        acronym_map(["As shown earlier (see Fig. 2) and elsewhere (p < 0.05)."]) == {}
    )


# --------------------------------------------------------- uncovered_terms


def test_acronym_covered_by_itself_case_sensitive_word_boundary() -> None:
    assert uncovered_terms("TEM images were taken.", ["The TEM grid."], {}) == []
    # lowercase / embedded is not the acronym
    assert [
        t.text for t in uncovered_terms("TEM images.", ["a tem item system"], {})
    ] == ["TEM"]
    assert [t.text for t in uncovered_terms("TEM images.", ["ATEM, TEMS2"], {})] == [
        "TEM"
    ]


def test_acronym_covered_by_its_long_form_via_the_map() -> None:
    amap = acronym_map([_PAPER])
    out = uncovered_terms(
        "TEM images were taken.",
        ["Transmission  electron\nmicroscopy of the sample."],
        amap,
    )
    assert out == []


def test_long_form_in_claim_covered_by_the_acronym_in_passage() -> None:
    amap = acronym_map([_PAPER])
    claim = "Transmission electron microscopy shows buds."
    assert uncovered_terms(claim, ["A TEM image of the buds."], amap) == []
    # ...and uncovered (reported as the acronym, with its expansion) when absent.
    out = uncovered_terms(claim, ["Buds were weighed."], amap)
    assert [(t.kind, t.text) for t in out] == [(KIND_ACRONYM, "TEM")]
    assert out[0].expansion == "transmission electron microscopy"


def test_fi189535_shape_tem_and_sts_uncovered_but_stm_present() -> None:
    amap = acronym_map([_PAPER])
    claim = (
        "Transmission electron microscopy and scanning tunnelling "
        "spectroscopy identify a nanobud as a fullerene on a nanotube."
    )
    passage = "Figure 2 shows a typical STM topographic image of the NanoBud."
    out = uncovered_terms(claim, [passage], amap)
    assert [(t.kind, t.text) for t in out] == [
        (KIND_ACRONYM, "TEM"),
        (KIND_ACRONYM, "STS"),
    ]
    # The mode words inside the spelled-out phrases are subsumed, not doubled.
    assert not [t for t in out if t.kind == KIND_MODE]


def test_split_phrase_passage_covers_sts_by_acronym_or_expansion() -> None:
    amap = acronym_map([_PAPER])
    claim = "STS identifies the bud."
    for passage in (
        "Using STS we see it.",
        "scanning tunnelling microscopy (STM) and spectroscopy were used",
        "scanning tunneling spectroscopy resolves it",  # US spelling
    ):
        assert uncovered_terms(claim, [passage], amap) == [], passage


def test_generic_head_is_satisfied_by_any_specific_method_not_the_reverse() -> None:
    # "calculations" is a generic head; a passage naming a specific method grounds it.
    assert (
        uncovered_terms(
            "Calculations find a gap.", ["We use the SCC-DFTB algorithm."], {}
        )
        == []
    )
    # A specific method in the claim is NOT grounded by a generic head alone.
    out = uncovered_terms(
        "Molecular dynamics finds a gap.", ["The simulations were long."], {}
    )
    assert [t.text.lower() for t in out] == ["molecular dynamics"]


def test_mode_covered_when_a_passage_acronym_expands_to_it() -> None:
    amap = acronym_map([_PAPER])
    out = uncovered_terms("Spectroscopy shows a gap.", ["STS resolves the gap."], amap)
    assert [t for t in out if t.kind == KIND_MODE] == []


def test_numbers_follow_migrate_token_rules() -> None:
    claim = "A 400:1 ratio at 5.2 nm and 77 K."
    out = uncovered_terms(claim, ["The ratio reaches 400:1 over 5.2nm."], {})
    # 400 and 5.2 covered (5.2nm == 5.2 nm); 77 is not in the passage.
    assert [(t.kind, t.text) for t in out if t.kind == KIND_NUMBER] == [
        (KIND_NUMBER, "77")
    ]
    # An integer does not ground on a longer run (19 vs 1900).
    assert [
        t.text for t in uncovered_terms("At 19 degrees.", ["At 1900 degrees."], {})
    ] == ["19"]


def test_units_do_not_false_hit() -> None:
    out = uncovered_terms(
        "A 2.0 eV gap at 300 K in 5 nm films.",
        ["The gap is 2.0 eV at 300 K in 5 nm films."],
        {},
    )
    assert out == []


def test_no_passages_means_nothing_to_compare() -> None:
    assert uncovered_terms("TEM shows 5 nm.", [], {}) == []
    assert uncovered_terms("TEM shows 5 nm.", ["", "  "], {}) == []


def test_markup_in_passages_is_stripped() -> None:
    assert (
        uncovered_terms("The sp3 carbon at 5 nm.", ["sp<sup>3</sup> carbon, 5 nm"], {})
        == []
    )


def test_public_surface() -> None:
    assert coverage.KINDS == (KIND_ACRONYM, KIND_MODE, KIND_NUMBER)


def test_chirality_pairs_and_cycloaddition_labels_are_not_loose_numbers() -> None:
    nums = _kinds(
        claim_terms("A (10,0) tube and two [2+2] conformers, 0.5 eV."), KIND_NUMBER
    )
    assert "(10,0)" in nums and "0.5" in nums
    assert "10" not in nums and "2" not in nums
    # The pair matches as a unit, whatever the spacing.
    assert uncovered_terms("A (10,0) tube.", ["the (10, 0) nanotube"], {}) == []
    assert [
        t.text for t in uncovered_terms("A (10,0) tube.", ["the (10,10) nanotube"], {})
    ] == ["(10,0)"]


def test_spelled_out_mode_is_covered_by_its_initialism_without_a_definition() -> None:
    claim = "High-resolution transmission electron microscopy observes fullerenes."
    assert uncovered_terms(claim, ["The TEM image shows spheres."], {}) == []
    assert [
        t.text for t in uncovered_terms(claim, ["The image shows spheres."], {})
    ] == ["transmission electron microscopy"]


def test_generic_head_is_dropped_beside_a_missing_named_method() -> None:
    amap = acronym_map(["Molecular dynamics (MD) was used."])
    out = uncovered_terms(
        "Molecular dynamics simulations show a gap.", ["The gap is large."], amap
    )
    assert [(t.kind, t.text) for t in out] == [(KIND_ACRONYM, "MD")]


def test_count_term_is_a_density_signal() -> None:
    term = Term(KIND_ACRONYM, "TEM")
    assert coverage.count_term(term, "TEM, TEM and TEM.", {}) == 3
    assert coverage.count_term(term, "no mention", {}) == 0


def test_trailing_zero_and_latex_math_fences_do_not_hide_a_number() -> None:
    assert (
        uncovered_terms("A moment of 6.0 μB.", ["about $6\\mu_B$ per cell"], {}) == []
    )
    assert (
        uncovered_terms("A gap of 0.50 eV.", ["a gap of 0.5 eV"], {}) != []
    )  # 0.50 != 0.5


def test_mathematical_modes_are_not_coverage_terms():
    # "proof"/"theorem" pass the sentence lint as a way of knowing but name no
    # method a passage must carry: no coverage term, no method-claim ordering.
    sentence = "A computability proof shows that no algorithm decides the theorem."
    kinds = {(t.kind, t.text.lower()) for t in coverage.claim_terms(sentence)}
    assert not any(text in coverage.NON_METHOD_MODES for _, text in kinds)
    gaps = coverage.uncovered_terms(
        sentence, ["The tiling problem admits no decision procedure."], {}
    )
    assert all(t.text.lower() not in coverage.NON_METHOD_MODES for t in gaps)
