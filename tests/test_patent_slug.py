"""Tests for the DOCDB id parser used by the ``patent`` kind."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.handlers._patent_slug import DocDbId, parse_docdb_id


class TestNormalisation:
    """Whitespace stripping + lowercasing → canonical slug."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ep1234567b1", "ep1234567b1"),
            ("EP1234567B1", "ep1234567b1"),
            ("EP 1234567 B1", "ep1234567b1"),
            ("  ep1234567b1  ", "ep1234567b1"),
            ("EP\t1234567\nB1", "ep1234567b1"),
            ("us20240012345a1", "us20240012345a1"),
            ("US20240012345A1", "us20240012345a1"),
            ("WO2023123456A1", "wo2023123456a1"),
            # Kind code with no sequence digit ('a' on its own).
            ("ep1000000a", "ep1000000a"),
        ],
    )
    def test_canonical_slug(self, raw: str, expected: str) -> None:
        parsed = parse_docdb_id(raw)
        assert parsed.slug == expected


class TestParts:
    """Parsed parts expose country / number / kind."""

    def test_three_letter_kind(self) -> None:
        p = parse_docdb_id("EP1234567B1")
        assert p.country == "ep"
        assert p.number == "1234567"
        assert p.kind_code == "b"
        assert p.seq == "1"
        assert p.kind_full == "b1"
        assert p.disk_subpath == ("ep", "1234567", "b1")
        assert p.display == "EP1234567B1"

    def test_kind_without_seq(self) -> None:
        p = parse_docdb_id("ep1000000a")
        assert p.kind_code == "a"
        assert p.seq == ""
        assert p.kind_full == "a"
        assert p.disk_subpath == ("ep", "1000000", "a")

    def test_us_long_application_number(self) -> None:
        p = parse_docdb_id("US20240012345A1")
        assert p.country == "us"
        assert p.number == "20240012345"
        assert p.kind_full == "a1"


class TestRejections:
    """BadInput on bad inputs + recovery hints."""

    def test_dotted_form_rejected_with_hint(self) -> None:
        with pytest.raises(BadInput) as exc:
            parse_docdb_id("EP.1234567.B1")
        assert "dotted" in str(exc.value).lower()
        # Recovery hint on .next should suggest the dot-stripped form.
        assert exc.value.next is not None
        assert "ep1234567b1" in exc.value.next.lower()

    def test_garbage_rejected(self) -> None:
        with pytest.raises(BadInput, match="not a DOCDB id"):
            parse_docdb_id("hello world")

    def test_empty_rejected(self) -> None:
        with pytest.raises(BadInput):
            parse_docdb_id("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(BadInput):
            parse_docdb_id("   ")

    def test_unknown_country_code_rejected(self) -> None:
        # XX is not a real WIPO ST.3 code.
        with pytest.raises(BadInput, match="unknown patent authority"):
            parse_docdb_id("XX1234567B1")

    def test_no_kind_code_rejected(self) -> None:
        # Just country + digits, no kind letter → not a DOCDB id.
        with pytest.raises(BadInput, match="not a DOCDB id"):
            parse_docdb_id("EP1234567")

    def test_too_many_seq_digits_rejected(self) -> None:
        # Kind code allows at most one sequence digit (regex \d?).
        with pytest.raises(BadInput, match="not a DOCDB id"):
            parse_docdb_id("EP1234567B12")

    def test_non_string_input_rejected(self) -> None:
        with pytest.raises(BadInput):
            parse_docdb_id(None)  # type: ignore[arg-type]


class TestDataclass:
    """``DocDbId`` is frozen and reconstructable."""

    def test_frozen(self) -> None:
        p = parse_docdb_id("EP1234567B1")
        with pytest.raises(AttributeError):
            p.country = "us"  # type: ignore[misc]

    def test_round_trip(self) -> None:
        p = parse_docdb_id("EP1234567B1")
        assert (
            DocDbId(
                country=p.country,
                number=p.number,
                kind_code=p.kind_code,
                seq=p.seq,
            ).slug
            == p.slug
        )
