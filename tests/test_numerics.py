"""Contract tests for :func:`precis.utils.numerics.extract_numerics`."""

from __future__ import annotations

import pytest

from precis.utils.numerics import extract_numerics

# ── trivial ─────────────────────────────────────────────────────────


class TestTrivial:
    def test_empty(self) -> None:
        assert extract_numerics("") == []

    def test_no_units(self) -> None:
        assert extract_numerics("just prose with no numbers or units") == []

    def test_bare_number(self) -> None:
        # Numbers without recognized units are NOT included — keeps
        # the index focused on quantitative claims, not page numbers
        # or counts that happen to appear in body text.
        assert extract_numerics("we tested 12 samples") == []


# ── core units ──────────────────────────────────────────────────────


class TestCoreUnits:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("bandgap of 1.523 eV measured", ["1.523 eV"]),
            ("achieved 12% FE for CO2 reduction", ["12%"]),
            ("operating at -0.3 V vs RHE", ["-0.3 V"]),
            ("absorbance at 1670 cm-1 was strong", ["1670 cm-1"]),
            ("cell sustained 500 cycles at 80% retention", ["500 cycles", "80%"]),
            ("synthesized at 120 °C for 24 h", ["120 °C", "24 h"]),
            ("scan from 10 nm to 500 nm", ["10 nm", "500 nm"]),
            ("yield of 85% in all batches", ["85%"]),
        ],
    )
    def test_extracts_expected(self, text: str, expected: list[str]) -> None:
        got = extract_numerics(text)
        for e in expected:
            assert e in got, f"expected {e!r} in {got}"


class TestDeduplication:
    def test_repeated_token_dedup(self) -> None:
        out = extract_numerics("12% then later 12% again with 12% reuse")
        assert out.count("12%") == 1

    def test_preserves_first_seen_casing(self) -> None:
        # ``eV`` and ``ev`` are different — paper used "eV", so that
        # casing is what we store.
        out = extract_numerics("1.5 eV measured")
        assert "1.5 eV" in out

    def test_order_is_first_occurrence(self) -> None:
        out = extract_numerics("first 5 nm then 3 nm later 5 nm")
        # 5 nm first, 3 nm second, 5 nm dedup'd.
        assert out == ["5 nm", "3 nm"]


# ── non-trivia ──────────────────────────────────────────────────────


class TestEdgeCases:
    def test_number_with_exponent_notation(self) -> None:
        out = extract_numerics("decay rate 2.5e-3 s")
        assert "2.5e-3 s" in out

    def test_negative_number(self) -> None:
        out = extract_numerics("temperature dropped to -40 °C overnight")
        assert "-40 °C" in out

    def test_unit_without_space(self) -> None:
        # "12%" no space — common idiom — should still match.
        out = extract_numerics("yield 12% across batches")
        assert "12%" in out

    def test_does_not_match_unit_inside_word(self) -> None:
        # "carbon" must not be a hit even though "carb" + "on" exist.
        # Lookaround anchors guard against word-boundary leaks.
        out = extract_numerics("we used 5 mg of carbon as the substrate")
        assert "5 mg" in out
        # No spurious "5 mg" → ["5 m", "5 mg"] or similar.
        # Specifically check that "mg" wasn't truncated:
        assert "5 m" not in out

    def test_does_not_match_word_boundary_into_unit(self) -> None:
        # "12 Average" must not match "12 A" (units must be word-
        # boundary terminated).
        out = extract_numerics("12 Average across replicates")
        assert "12 A" not in out
        assert out == []

    def test_decimal_at_end_of_sentence(self) -> None:
        # "1.5." — number followed by period followed by space. The
        # number ``1.5`` should still be matched if followed by
        # a unit; the trailing period is sentence punctuation, not
        # part of the number.
        out = extract_numerics("rate of 1.5 V. Then ramp up.")
        assert "1.5 V" in out

    def test_no_false_match_on_version_numbers(self) -> None:
        # "Python 3.11" must NOT extract "3.11 K" or anything weird.
        out = extract_numerics("requires Python 3.11 to run")
        assert out == []
