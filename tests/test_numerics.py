"""Contract tests for :func:`precis.utils.numerics.extract_numerics`."""

from __future__ import annotations

import pytest

from precis.utils.numerics import extract_numerics, extract_numerics_wide

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

    def test_dedup_keeps_scanning_past_a_repeat(self) -> None:
        # A repeat skips that token only; later new tokens still land.
        assert extract_numerics("5 nm then 5 nm then 7 nm") == ["5 nm", "7 nm"]


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


class TestExtractNumericsWide:
    """Spellings PDF text uses that the plain extractor misses (gr478127)."""

    def test_superset_of_plain(self):
        t = "gap of 1.523 eV at 12% and 300 K"
        assert set(extract_numerics(t)) <= set(extract_numerics_wide(t))

    @pytest.mark.parametrize(
        ("text", "want"),
        [
            ("0.44 and 0.26 eV", "0.44 eV"),
            ("from 1.169 to 1.182 Å", "1.169 Å"),
            ("2,000 and 6,000 p.p.m.", "2000 ppm"),
            ("$1.78±0.11$ Å", "1.78 Å"),
            ("a pore of $0.5$ $\\mu$ m", "0.5 µm"),
            ("a 2 μm film", "2 µm"),
            ("10-20 nm wide", "10 nm"),
            ("| Pore aperture [Å] |\n|---|\n| 2.6 |", "2.6 Å"),
        ],
    )
    def test_reads(self, text, want):
        assert want in extract_numerics_wide(text)

    def test_empty(self):
        assert extract_numerics_wide("") == []


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        (r"$1.78\pm0.11$  Å", ["1.78 Å", "0.11 Å"]),
        (r"$1.72\pm0.06$  Å", ["1.72 Å", "0.06 Å"]),
        (r"$2.05\pm0.07$  Å", ["2.05 Å", "0.07 Å"]),
        (r"\(1.78 \pm 0.11\) Å", ["1.78 Å", "0.11 Å"]),
        (r"$$1.78\pm0.11$$ Å", ["1.78 Å", "0.11 Å"]),
        (r"$1.78 \pm 0.11 $ Å", ["1.78 Å", "0.11 Å"]),
        (r"$12\%$ loss", ["12%"]),
        (r"$25^\circ$C", ["25 °C"]),
        (r"$3 \times 10$ nm", ["10 nm"]),
    ],
)
def test_wide_reads_latex_inline_math(text, expect):
    assert set(expect) <= set(extract_numerics_wide(text))


def test_wide_reads_tex_micro_with_double_space():
    assert "5 µm" in extract_numerics_wide(r"a pore of $5$  $\mu$  m width")
    assert "5 µm" in extract_numerics_wide(r"a pore of 5 \mu m width")


def test_wide_leaves_dollar_amounts_alone():
    assert extract_numerics_wide("costs $5 and $10 each") == []
    assert extract_numerics_wide("a $5 nm rope") == ["5 nm"]
