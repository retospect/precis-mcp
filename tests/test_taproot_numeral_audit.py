"""``absent_numerals`` unit tests — the false-positive families found by
the gr503439 corpus check (278 flagged hubs re-read against their source
papers, 2026-10-23).

``_NUM_RE``'s word-boundary lookbehind drops digits glued to letters on
BOTH sides, so a source's ``GMTKN55`` contributed nothing to ``have``
while a claim's standalone ``55 subsets`` was reported absent.
``_NAME_DIGITS_RE`` widens the source side only — every fix below keeps
that asymmetry: the source side may gain forms, the claim side only ever
loses a match, so real misses still fire.
"""

from __future__ import annotations

from precis.taproot.numeral_audit import absent_numerals


def test_name_digits_in_source_match_standalone_claim_numeral() -> None:
    """The fi453546 shape: the source names ``GMTKN55``; the claim's
    standalone ``55`` refers to the same number."""
    assert (
        absent_numerals(
            "GMTKN55 aggregates 55 subsets",
            "the GMTKN55 benchmark database",
        )
        == []
    )


def test_claim_side_still_drops_name_digits() -> None:
    """The claim-side exclusion stays: ``GMTKN55`` in the claim extracts
    no ``55``, so nothing is reported (and nothing was gained)."""
    assert absent_numerals("the GMTKN55 set", "a benchmark") == []


def test_real_miss_still_reports() -> None:
    """A standalone claim numeral the source genuinely lacks still fires —
    the widening can't paper over a real absence."""
    assert absent_numerals("55 subsets", "a benchmark") == ["55"]


def test_name_digits_widen_only_the_source_side() -> None:
    """Claim ``B3LYP`` + source ``B3LYP``: the glued ``3`` enters neither
    side's claim set — and even where the claim writes it bare, the
    source's name still covers it."""
    assert absent_numerals("B3LYP with 3 frozen cores", "the B3LYP functional") == []


# ── corpus-check families, one test each (gr503439, 2026-10-23) ──────────


def test_html_sup_exponent_matches_claim() -> None:
    """fi177576 shape: the paper's ``0.5\\*10<sup>8</sup>`` is the claim's
    ``0.5×10⁸`` — the ``<sup>`` tag and the markdown-escaped ``\\*`` both
    fold before extraction."""
    assert (
        absent_numerals(
            "a photoconductive gain of 0.5×10⁸",
            "a photoconductive gain of 0.5\\*10<sup>8</sup> for 600 nm",
        )
        == []
    )


def test_html_sup_split_sign_and_outside_sign() -> None:
    """fi350124 / fi478331 shapes: the sign can sit in its own tag
    (``10<sup>-</sup> <sup>4</sup>``) or outside it (``10-<sup>30</sup>``)."""
    assert (
        absent_numerals(
            "H2 generation of 1.86×10-4 mol",
            "Efficient H2 generation (1.86 x 10<sup>-</sup> <sup>4</sup> mol/cm2)",
        )
        == []
    )
    assert (
        absent_numerals(
            "EDPs as low as 1.88 × 10^-30 J",
            "exhibit EDPs as low as 1.88 × 10-<sup>30</sup> Js/mm",
        )
        == []
    )


def test_spaced_sign_exponent_matches_claim() -> None:
    """fi349476 shape: PDF extraction spaces the exponent's sign —
    ``1.89 × 10 - 6`` is ``1.89e-6``."""
    assert (
        absent_numerals(
            "a peak NH3 yield of 1.89×10⁻⁶ mol",
            "a peak NH3 yield of 1.89 × 10 - 6 mol s - 1 cm - 2",
        )
        == []
    )


def test_bullet_reads_as_times() -> None:
    """fi349927 shape: ``\\bullet`` is a multiplication like ``\\times``."""
    assert (
        absent_numerals(
            "a mean absolute error of 1.34×10^-3 mol",
            "mean absolute error of 1.34 \\bullet 10^{-3} mol L^{-1}",
        )
        == []
    )


def test_slash_value_list_contributes_each_element() -> None:
    """fi349835 / fi350324 shapes: ``123/160/196`` and ``0.27/0.24`` —
    ``_NUM_RE`` keeps only the first element."""
    assert (
        absent_numerals(
            "overpotentials of 160 mV and 196 mV",
            "Overpotential 123/160/196 across acidified matrices",
        )
        == []
    )
    assert (
        absent_numerals(
            "an OER overpotential of 0.24 V",
            "(η_OER = 0.27/0.24 V, η_ORR = 0.23/0.27 V)",
        )
        == []
    )


def test_slash_list_guards_dates_and_dois() -> None:
    """The widening stays narrow: date fragments (a year-like element or
    all small integers) and DOI stems are not values."""
    assert absent_numerals("23 units", "measured on 10/23/2020") == ["23"]
    assert absent_numerals("23 units", "measured on 2020/10/23") == ["23"]
    assert absent_numerals("23 units", "measured on 10/23") == ["23"]
    assert absent_numerals("1.1840096 units", "doi 10.1063/1.1840096") == ["1.1840096"]


def test_miller_index_after_formula_digit_is_not_a_quantity() -> None:
    """fi498949 / fi453900 shapes: ``RuO2(110)`` and ``Fe2O3(1̅102)`` —
    the parenthesised index glued to a formula never extracts."""
    assert absent_numerals("kMC on RuO2(110) fell by 1000", "no numbers here") == [
        "1000"
    ]
    assert (
        absent_numerals(
            "Rh adatoms form on α-Fe₂O₃(1̅102) under water vapour", "UHV sintering"
        )
        == []
    )


def test_non_ascii_letter_glued_digits_widen_the_source_side() -> None:
    """fi348969 / fi349895 shapes: digits glued to a CJK character
    (``为0.03``) or PDF glyph noise (``þ0.13``) still count as present."""
    assert (
        absent_numerals(
            "net electron transfer of 0.03–0.60 e⁻",
            "净电子量为0.03~0.60 e^-，有助于NO的活化",
        )
        == []
    )
    assert absent_numerals("ΔG_H flips to +0.13 eV", "flips to þ0.13eV") == []


def test_word_dash_decimal_widens_the_source_side() -> None:
    """fi350021 shape: ``irradiation-12.7`` is a prose value glued by a
    dash — the claim's ``12.7`` matches it."""
    assert (
        absent_numerals(
            "a rate 12.7 times higher",
            "solar irradiation-12.7 times higher than pristine",
        )
        == []
    )
    # an integer after a word-dash stays a name suffix (``UiO-66``)
    assert absent_numerals("66 nm crystals", "the UiO-66 framework") == ["66 nm"]
