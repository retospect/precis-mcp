"""Tests for the accession-number parser used by the ``edgar`` kind."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.handlers._edgar_accession import (
    Accession,
    looks_like_accession,
    parse_accession,
)


class TestNormalisation:
    """Whitespace stripping + dashless re-insertion → canonical slug."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0000320193-23-000106", "0000320193-23-000106"),
            ("  0000320193-23-000106  ", "0000320193-23-000106"),
            ("\t0000320193-23-000106\n", "0000320193-23-000106"),
            # Dashless form re-inserts dashes.
            ("000032019323000106", "0000320193-23-000106"),
            ("0001045810-24-000029", "0001045810-24-000029"),
            ("000104581024000029", "0001045810-24-000029"),
        ],
    )
    def test_canonical_slug(self, raw: str, expected: str) -> None:
        parsed = parse_accession(raw)
        assert parsed.slug == expected
        assert parsed.dashed == expected


class TestParts:
    """Parsed parts expose CIK / year / sequence + derived forms."""

    def test_apple_10k(self) -> None:
        a = parse_accession("0000320193-23-000106")
        assert a.cik == "320193"
        assert a.cik_padded == "0000320193"
        assert a.cik_int == 320193
        assert a.year2 == "23"
        assert a.seq == "000106"
        assert a.dashed == "0000320193-23-000106"
        assert a.dashless == "000032019323000106"
        assert a.archive_subpath == "320193/000032019323000106"
        assert a.disk_subpath == ("320193", "000032019323000106")

    def test_dashless_parses_identically(self) -> None:
        a = parse_accession("000032019323000106")
        assert a.dashed == "0000320193-23-000106"
        assert a.cik == "320193"

    def test_high_cik_no_leading_zeros(self) -> None:
        a = parse_accession("0001045810-24-000029")
        assert a.cik == "1045810"
        assert a.cik_padded == "0001045810"
        assert a.dashless == "000104581024000029"


class TestLooksLike:
    """Cheap shape predicate for routing accession-shaped misses."""

    @pytest.mark.parametrize(
        "raw",
        [
            "0000320193-23-000106",
            "000032019323000106",
            "  0000320193-23-000106  ",
        ],
    )
    def test_positive(self, raw: str) -> None:
        assert looks_like_accession(raw) is True

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "climate risk",
            "0000320193-23-00010",  # seq too short
            "000032019323",  # too few digits
            "0000320193/23/000106",  # wrong separators
            "ep1234567b1",  # a patent slug, not an accession
        ],
    )
    def test_negative(self, raw: str) -> None:
        assert looks_like_accession(raw) is False


class TestRejections:
    """BadInput on bad inputs + recovery hints."""

    def test_garbage_rejected(self) -> None:
        with pytest.raises(BadInput, match="not an SEC accession"):
            parse_accession("hello world")

    def test_empty_rejected(self) -> None:
        with pytest.raises(BadInput):
            parse_accession("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(BadInput):
            parse_accession("   ")

    def test_wrong_separator_rejected(self) -> None:
        with pytest.raises(BadInput, match="not an SEC accession"):
            parse_accession("0000320193/23/000106")

    def test_short_sequence_rejected(self) -> None:
        with pytest.raises(BadInput, match="not an SEC accession"):
            parse_accession("0000320193-23-00010")

    def test_non_string_input_rejected(self) -> None:
        with pytest.raises(BadInput):
            parse_accession(None)  # type: ignore[arg-type]

    def test_hint_shows_canonical_form(self) -> None:
        with pytest.raises(BadInput) as exc:
            parse_accession("not-an-accession")
        assert exc.value.next is not None
        assert "0000320193-23-000106" in exc.value.next


class TestDataclass:
    """``Accession`` is frozen and reconstructable."""

    def test_frozen(self) -> None:
        a = parse_accession("0000320193-23-000106")
        with pytest.raises(AttributeError):
            a.cik = "1"  # type: ignore[misc]

    def test_round_trip(self) -> None:
        a = parse_accession("0000320193-23-000106")
        assert Accession(cik=a.cik, year2=a.year2, seq=a.seq).dashed == a.dashed
