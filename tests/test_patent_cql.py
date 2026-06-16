"""Tests for ``_patent_cql.build_cql`` and the tag-to-CQL lift."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.handlers._patent_cql import (
    build_cql,
    slugify_applicant,
    validate_strict_cql,
)

# ---------------------------------------------------------------------------
# Stubs for the store-dependent applicant lookup
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal stand-in implementing the ``_StoreProto`` shape."""

    def __init__(self, mapping: dict[str, dict] | None = None) -> None:
        # tag-string -> meta dict
        self._mapping = mapping or {}

    def find_first_meta_for_open_tag(self, *, kind: str, tag: str) -> dict | None:
        if kind != "patent":
            return None
        return self._mapping.get(tag)


# ---------------------------------------------------------------------------
# q= auto-promotion vs passthrough
# ---------------------------------------------------------------------------


class TestQPromotion:
    def test_bare_keyword_promoted_to_ti_or_ab(self) -> None:
        cql = build_cql(q="photocatalytic NOx reduction", tags=None)
        assert (
            cql
            == '(ti="photocatalytic NOx reduction" OR ab="photocatalytic NOx reduction")'
        )

    def test_cql_field_passed_through(self) -> None:
        cql = build_cql(q="cpc=B01J27/24", tags=None)
        assert cql == "(cpc=B01J27/24)"

    def test_cql_with_boolean_passed_through(self) -> None:
        cql = build_cql(q="ti=nanobud and pa=siemens", tags=None)
        assert cql == "(ti=nanobud and pa=siemens)"

    def test_cql_within_passed_through(self) -> None:
        cql = build_cql(q='pd within "2020 2025"', tags=None)
        assert cql == '(pd within "2020 2025")'

    def test_quotes_in_q_are_escaped(self) -> None:
        # Bare keyword path; double-quotes need to be escaped.
        cql = build_cql(q='say "hello"', tags=None)
        assert cql == '(ti="say \\"hello\\"" OR ab="say \\"hello\\"")'


# ---------------------------------------------------------------------------
# Tag → CQL lift
# ---------------------------------------------------------------------------


class TestTagLift:
    @pytest.mark.parametrize(
        ("tag", "expected_clause"),
        [
            ("cpc:b01j27/24", 'cpc="B01J27/24"'),
            ("ipc:h01m", 'ipc="H01M"'),
            ("country:ep", 'pact="EP"'),
            ("kind:b1", 'kind="B1"'),
            ("family:12345678", 'famn="12345678"'),
        ],
    )
    def test_simple_lift(self, tag: str, expected_clause: str) -> None:
        cql = build_cql(q="solar", tags=[tag])
        assert expected_clause in cql

    def test_open_prefix_skipped(self) -> None:
        # topic: is a local-only narrowing tag; should not produce a CQL clause.
        cql = build_cql(q="solar", tags=["topic:my-project"])
        # Only the q= part survives.
        assert cql == '(ti="solar" OR ab="solar")'

    def test_unknown_prefix_skipped(self) -> None:
        cql = build_cql(q="solar", tags=["mystery:value"])
        assert "mystery" not in cql

    def test_bare_flag_skipped(self) -> None:
        # No colon at all → not a prefix tag.
        cql = build_cql(q="solar", tags=["star"])
        assert "star" not in cql

    def test_empty_value_skipped(self) -> None:
        cql = build_cql(q="solar", tags=["cpc:"])
        assert "cpc" not in cql

    def test_multiple_tags_anded(self) -> None:
        cql = build_cql(q="solar", tags=["cpc:b01j27/24", "country:ep"])
        # All clauses joined with " and ".
        assert ' and cpc="B01J27/24"' in cql
        assert ' and pact="EP"' in cql


# ---------------------------------------------------------------------------
# Applicant resolution (lossy slug → canonical via meta lookup)
# ---------------------------------------------------------------------------


class TestApplicantLift:
    def test_title_case_unslug_always(self) -> None:
        """The applicant lift now title-cases the slug regardless of
        store state — the meta-lookup path was retired when patent
        ingest stopped auto-tagging applicant:* (see commit body for
        T10.4). OPS phrase matching is case-forgiving so the lossy
        capitalisation still finds the right records."""
        store = _FakeStore(
            {
                "applicant:siemens-ag": {
                    "applicants": [{"name": "Siemens AG"}],
                },
            }
        )
        cql = build_cql(q=None, tags=["applicant:siemens-ag"], store=store)
        assert cql == 'pa="Siemens Ag"'

    def test_cold_start_no_store(self) -> None:
        cql = build_cql(q=None, tags=["applicant:hewlett-packard"])
        # No store → naive title-case unslug.
        assert cql == 'pa="Hewlett Packard"'

    def test_cold_start_no_match_in_store(self) -> None:
        store = _FakeStore({})
        cql = build_cql(q=None, tags=["applicant:basf-se"], store=store)
        assert cql == 'pa="Basf Se"'


# ---------------------------------------------------------------------------
# Empty-input rejection
# ---------------------------------------------------------------------------


class TestEmptyInput:
    def test_no_q_no_tags_rejected(self) -> None:
        with pytest.raises(BadInput, match="search requires q="):
            build_cql(q=None, tags=None)

    def test_empty_string_q_rejected(self) -> None:
        with pytest.raises(BadInput):
            build_cql(q="   ", tags=None)

    def test_only_open_tags_rejected(self) -> None:
        # topic: doesn't lift to CQL → no clauses → BadInput.
        with pytest.raises(BadInput):
            build_cql(q=None, tags=["topic:foo"])


# ---------------------------------------------------------------------------
# slugify_applicant — round-trip helper
# ---------------------------------------------------------------------------


class TestValidateStrictCQL:
    """``validate_strict_cql`` rejects bare keywords; accepts explicit CQL."""

    @pytest.mark.parametrize(
        "cql",
        [
            "cpc=B01J27/24",
            'pa="Siemens AG"',
            "ti=nanobud or ab=nanobud",
            'cpc=B01J27/24 and pa="basf"',
            'ti="photocatalysis" not pa="exclude me"',
            'pd within "2020 2025"',
        ],
    )
    def test_explicit_cql_accepted(self, cql: str) -> None:
        assert validate_strict_cql(cql) == cql.strip()

    def test_strips_surrounding_whitespace(self) -> None:
        assert validate_strict_cql("   cpc=B01J27/24   ") == "cpc=B01J27/24"

    @pytest.mark.parametrize(
        "cql",
        [
            "photocatalysis",
            "carbon nanotube",
            "  multiple words but no operators  ",
        ],
    )
    def test_bare_keyword_rejected(self, cql: str) -> None:
        with pytest.raises(BadInput, match="bare keyword"):
            validate_strict_cql(cql)

    def test_empty_rejected(self) -> None:
        with pytest.raises(BadInput, match="empty"):
            validate_strict_cql("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(BadInput, match="empty"):
            validate_strict_cql("   ")

    def test_non_string_rejected(self) -> None:
        with pytest.raises(BadInput, match="must be a string"):
            validate_strict_cql(None)  # type: ignore[arg-type]

    def test_recovery_hint_present(self) -> None:
        # Bare keyword case must point at explicit-field examples.
        with pytest.raises(BadInput) as exc:
            validate_strict_cql("photocatalysis")
        assert exc.value.next is not None
        assert "cpc=" in exc.value.next or "ti=" in exc.value.next


class TestSlugify:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Siemens AG", "siemens-ag"),
            ("BASF SE", "basf-se"),
            ("Hewlett-Packard", "hewlett-packard"),
            ("  Siemens   AG  ", "siemens-ag"),
            ("3M Company", "3m-company"),
        ],
    )
    def test_round_trip(self, name: str, expected: str) -> None:
        assert slugify_applicant(name) == expected
