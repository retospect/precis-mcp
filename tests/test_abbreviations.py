"""Schwartz-Hearst abbreviation-detection contract tests.

Pins the find / substitute behaviour so future tweaks to the
regex or the verifier don't silently change what gets shortened
before RAKE runs. Lightweight (no network, no model) — runs in
every CI pass.
"""

from __future__ import annotations

import pytest

from precis.utils.abbreviations import find, strip_trailing_aside, substitute

# ── find: positive cases ────────────────────────────────────────────


class TestFindPositive:
    def test_classic_scientific_definition(self) -> None:
        text = "Fourier Transform Infrared (FTIR) spectroscopy was performed"
        assert find(text) == {"FTIR": "Fourier Transform Infrared"}

    def test_three_word_long_form(self) -> None:
        text = "Density Functional Theory (DFT) calculations confirm the trend"
        assert find(text) == {"DFT": "Density Functional Theory"}

    def test_two_word_long_form(self) -> None:
        text = "The Membrane Electrode Assembly (MEA) design follows"
        assert find(text) == {"MEA": "Membrane Electrode Assembly"}

    def test_mixed_case_abbrev(self) -> None:
        text = "Time-of-Flight Secondary Ion Mass Spectrometry (ToF-SIMS) analysis"
        result = find(text)
        # ToF-SIMS matches "Time-of-Flight Secondary Ion Mass Spectrometry"
        # via Schwartz-Hearst letter verification: T(ime)o(f)F(light)
        # S(econdary)I(on)M(ass)S(pectrometry).
        assert "ToF-SIMS" in result, f"expected ToF-SIMS in result; got {result!r}"

    def test_multiple_in_same_text(self) -> None:
        text = (
            "We used X-ray Photoelectron Spectroscopy (XPS) and "
            "Density Functional Theory (DFT) together."
        )
        result = find(text)
        assert result["XPS"] == "X-ray Photoelectron Spectroscopy"
        assert result["DFT"] == "Density Functional Theory"

    def test_first_definition_wins_for_duplicate_short(self) -> None:
        text = (
            "Membrane Electrode Assembly (MEA) - here "
            "and later Methylene Ether Aldehyde (MEA) - different"
        )
        # Only the first definition is kept; a smart system would
        # warn, but this is a conservative correct-by-default choice
        # for per-paper preprocessing.
        result = find(text)
        assert result.get("MEA") == "Membrane Electrode Assembly"


# ── find: negative cases (must NOT detect) ────────────────────────────


class TestFindNegative:
    def test_single_letter_parenthetical_rejected(self) -> None:
        # (A) is a figure label, not an abbreviation
        assert find("See panel (A) for the spectrum") == {}

    def test_year_citation_rejected(self) -> None:
        # (2023) is a date, has no letters
        assert find("Smith et al. (2023) report") == {}

    def test_doesnt_match_when_short_letters_arent_in_long(self) -> None:
        # "(XYZ)" can't be an abbreviation of "spectroscopy" — fails
        # the Schwartz-Hearst letter verifier.
        text = "spectroscopy (XYZ) was used"
        assert find(text) == {}

    def test_empty_input(self) -> None:
        assert find("") == {}

    def test_no_parens_no_match(self) -> None:
        assert find("There are no abbreviations defined in this sentence.") == {}


# ── substitute ───────────────────────────────────────────────────────


class TestSubstitute:
    def test_collapses_defining_parenthetical(self) -> None:
        text = "Fourier Transform Infrared (FTIR) spectroscopy was performed"
        abbrevs = {"FTIR": "Fourier Transform Infrared"}
        out = substitute(text, abbrevs)
        assert out == "FTIR spectroscopy was performed"

    def test_replaces_later_long_form_mentions(self) -> None:
        text = (
            "Fourier Transform Infrared (FTIR) data was collected. "
            "Fourier Transform Infrared peaks at 1600 cm-1."
        )
        abbrevs = {"FTIR": "Fourier Transform Infrared"}
        out = substitute(text, abbrevs)
        assert "FTIR data was collected" in out
        assert "FTIR peaks at 1600 cm-1" in out

    def test_existing_short_form_unchanged(self) -> None:
        text = "FTIR shows OH stretches and FTIR shows aromatic C-H."
        abbrevs = {"FTIR": "Fourier Transform Infrared"}
        out = substitute(text, abbrevs)
        assert out == text

    def test_idempotent(self) -> None:
        text = "Density Functional Theory (DFT) calculations using Density Functional Theory"
        abbrevs = {"DFT": "Density Functional Theory"}
        once = substitute(text, abbrevs)
        twice = substitute(once, abbrevs)
        assert once == twice

    def test_empty_abbrev_dict_is_noop(self) -> None:
        text = "FTIR spectroscopy was performed"
        assert substitute(text, {}) == text

    def test_empty_text(self) -> None:
        assert substitute("", {"FTIR": "Fourier Transform Infrared"}) == ""

    def test_word_boundary_prevents_partial_matches(self) -> None:
        # "Spectroscopy" appearing inside another word shouldn't match.
        text = "Microspectroscopy is not Spectroscopy in the strict sense"
        abbrevs = {"S": "Spectroscopy"}
        out = substitute(text, abbrevs)
        assert "Microspectroscopy" in out  # untouched (no word boundary)
        assert "is not S in the strict sense" in out


# ── round-trip integration ───────────────────────────────────────────


def test_find_then_substitute_full_round_trip() -> None:
    """Realistic scientific-prose fixture: detect, substitute, verify
    the resulting text is shorter and reads naturally."""
    text = (
        "We performed Fourier Transform Infrared (FTIR) spectroscopy "
        "alongside Density Functional Theory (DFT) calculations. "
        "Fourier Transform Infrared spectra confirm the predicted "
        "Density Functional Theory peaks."
    )
    abbrevs = find(text)
    out = substitute(text, abbrevs)
    assert "FTIR spectroscopy" in out
    assert "DFT calculations" in out
    assert "FTIR spectra confirm" in out
    assert "DFT peaks" in out
    # Defining parentheticals collapsed — no orphan "(FTIR)" or "(DFT)".
    assert "(FTIR)" not in out
    assert "(DFT)" not in out


# ── strip_trailing_aside ─────────────────────────────────────────────


class TestStripTrailingAside:
    def test_strips_the_dr173020_case(self) -> None:
        # gr: exported PDF showed "... density functional theory
        # (Grimme D3) (DFT-D3) calculations ..." — a hand-authored term
        # chunk's definition text embedded its own trailing aside, which
        # then doubled up with the exporter's own "(SHORT)".
        long_form = "dispersion-corrected density functional theory (Grimme D3)"
        assert (
            strip_trailing_aside(long_form)
            == "dispersion-corrected density functional theory"
        )

    def test_noop_when_no_trailing_parenthetical(self) -> None:
        assert strip_trailing_aside("Fourier Transform Infrared") == (
            "Fourier Transform Infrared"
        )

    def test_noop_when_stripping_would_empty_the_string(self) -> None:
        # Degenerate input: the whole value is one parenthetical. Keep it
        # rather than discard the only content we have.
        assert strip_trailing_aside("(Grimme D3)") == "(Grimme D3)"

    def test_only_strips_the_trailing_aside(self) -> None:
        # An earlier, non-trailing parenthetical is left alone — only the
        # outermost trailing one is dropped.
        long_form = "density functional theory (DFT) (Grimme D3)"
        assert strip_trailing_aside(long_form) == "density functional theory (DFT)"

    def test_idempotent(self) -> None:
        long_form = "dispersion-corrected density functional theory (Grimme D3)"
        once = strip_trailing_aside(long_form)
        assert strip_trailing_aside(once) == once

    def test_regex_derived_long_forms_are_never_affected(self) -> None:
        # find()'s token pattern excludes parens, so its long-form values
        # never need this — confirms the helper is a true no-op on the
        # inline-detection path's own output.
        text = "Fourier Transform Infrared (FTIR) spectroscopy was performed"
        (long_form,) = find(text).values()
        assert strip_trailing_aside(long_form) == long_form


def test_short_form_propagates_to_keyword_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-trip with the actual keyword_summary wrapper: after
    abbreviation substitution, RAKE returns the short form."""
    from precis.utils.rake import keyword_summary

    text = (
        "Membrane Electrode Assembly (MEA) design for "
        "lithium-mediated electrochemical nitrogen reduction."
    )
    abbrevs = find(text)
    summary_with = keyword_summary(text, top_k=5, abbreviations=abbrevs)
    summary_without = keyword_summary(text, top_k=5)
    # With substitution, "MEA" should appear; without, the long form does.
    assert "mea" in summary_with.lower(), summary_with
    assert "membrane electrode assembly" in summary_without.lower(), summary_without
