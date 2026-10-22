"""``absent_numerals`` unit tests — the name-digits false-positive family
(gr503439).

``_NUM_RE``'s word-boundary lookbehind drops digits glued to letters on
BOTH sides, so a source's ``GMTKN55`` contributed nothing to ``have``
while a claim's standalone ``55 subsets`` was reported absent.
``_NAME_DIGITS_RE`` widens the source side only.
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
