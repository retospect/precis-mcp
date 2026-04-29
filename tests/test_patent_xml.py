"""Tests for the OPS XML parser (``_patent_xml.py``).

Fixtures live under ``tests/fixtures/patent/``. They're hand-crafted
to exercise the namespace-blind path-finding code; they don't claim
to be byte-for-byte identical to live OPS output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.handlers._patent_xml import (
    parse_patent,
    parse_search_response,
)

FIXTURES = Path(__file__).parent / "fixtures" / "patent"


@pytest.fixture
def biblio_xml() -> bytes:
    return (FIXTURES / "ep1234567b1_biblio.xml").read_bytes()


@pytest.fixture
def description_xml() -> bytes:
    return (FIXTURES / "ep1234567b1_description.xml").read_bytes()


@pytest.fixture
def claims_xml() -> bytes:
    return (FIXTURES / "ep1234567b1_claims.xml").read_bytes()


@pytest.fixture
def search_xml() -> bytes:
    return (FIXTURES / "search_cpc_b01j2724.xml").read_bytes()


# ---------------------------------------------------------------------------
# Biblio extraction
# ---------------------------------------------------------------------------


class TestBiblio:
    def test_title_prefers_english(self, biblio_xml: bytes) -> None:
        p = parse_patent(biblio_xml=biblio_xml)
        assert p.title == "Photocatalytic NOx reduction system"

    def test_abstract(self, biblio_xml: bytes) -> None:
        p = parse_patent(biblio_xml=biblio_xml)
        assert p.abstract is not None
        assert "Z-scheme" in p.abstract

    def test_publication_date(self, biblio_xml: bytes) -> None:
        p = parse_patent(biblio_xml=biblio_xml)
        assert p.publication_date == "2020-01-15"

    def test_application_date(self, biblio_xml: bytes) -> None:
        p = parse_patent(biblio_xml=biblio_xml)
        assert p.application_date == "2018-03-10"

    def test_family_id(self, biblio_xml: bytes) -> None:
        p = parse_patent(biblio_xml=biblio_xml)
        assert p.family_id == "012345678"

    def test_applicants_deduped(self, biblio_xml: bytes) -> None:
        # Fixture has the same applicant in epodoc and original formats.
        p = parse_patent(biblio_xml=biblio_xml)
        assert len(p.applicants) == 1
        assert p.applicants[0]["name"] == "SIEMENS AG"
        assert p.applicants[0]["country"] == "DE"

    def test_inventors(self, biblio_xml: bytes) -> None:
        p = parse_patent(biblio_xml=biblio_xml)
        assert len(p.inventors) == 1
        assert p.inventors[0]["name"] == "MUSTERMANN, MAX"

    def test_cpc_classes(self, biblio_xml: bytes) -> None:
        p = parse_patent(biblio_xml=biblio_xml)
        assert "B01J27/24" in p.cpc_classes
        assert "B01D53/86" in p.cpc_classes

    def test_ipc_classes(self, biblio_xml: bytes) -> None:
        p = parse_patent(biblio_xml=biblio_xml)
        # IPC values may carry whitespace in source; parser collapses it.
        assert any(c == "B01J 27/24" or c == "B01J27/24" for c in p.ipc_classes)


# ---------------------------------------------------------------------------
# Description + claims extraction
# ---------------------------------------------------------------------------


class TestDescription:
    def test_paragraphs_extracted(self, description_xml: bytes) -> None:
        p = parse_patent(description_xml=description_xml)
        # Empty <p num="0004"/> dropped; four real paragraphs remain.
        assert len(p.description_paragraphs) == 4

    def test_paragraph_text_collapsed(self, description_xml: bytes) -> None:
        p = parse_patent(description_xml=description_xml)
        assert "Z-scheme heterojunction" in p.description_paragraphs[2]


class TestClaims:
    def test_claims_extracted(self, claims_xml: bytes) -> None:
        p = parse_patent(claims_xml=claims_xml)
        assert len(p.claim_texts) == 3

    def test_first_claim(self, claims_xml: bytes) -> None:
        p = parse_patent(claims_xml=claims_xml)
        first = p.claim_texts[0]
        assert first.startswith("1. A photocatalytic system")
        assert "Z-scheme heterojunction" in first


# ---------------------------------------------------------------------------
# Combined parse
# ---------------------------------------------------------------------------


class TestCombined:
    def test_all_three_endpoints(
        self,
        biblio_xml: bytes,
        description_xml: bytes,
        claims_xml: bytes,
    ) -> None:
        p = parse_patent(
            biblio_xml=biblio_xml,
            description_xml=description_xml,
            claims_xml=claims_xml,
        )
        assert p.title == "Photocatalytic NOx reduction system"
        assert len(p.description_paragraphs) == 4
        assert len(p.claim_texts) == 3
        assert "B01J27/24" in p.cpc_classes


class TestDegradation:
    def test_no_input_returns_empty_record(self) -> None:
        p = parse_patent()
        assert p.title == "(untitled patent)"
        assert p.applicants == []
        assert p.description_paragraphs == []

    def test_malformed_xml_returns_empty(self) -> None:
        p = parse_patent(biblio_xml=b"<<<not xml>>>")
        assert p.title == "(untitled patent)"

    def test_empty_bytes_returns_empty(self) -> None:
        p = parse_patent(biblio_xml=b"")
        assert p.title == "(untitled patent)"


# ---------------------------------------------------------------------------
# Search-response parsing
# ---------------------------------------------------------------------------


class TestSearchResponse:
    def test_total_count(self, search_xml: bytes) -> None:
        hits, total = parse_search_response(search_xml)
        assert total == 42

    def test_hits_decoded(self, search_xml: bytes) -> None:
        hits, _ = parse_search_response(search_xml)
        slugs = {h.docdb_id for h in hits}
        assert "ep1234567b1" in slugs
        assert "wo2023123456a1" in slugs

    def test_hit_titles(self, search_xml: bytes) -> None:
        hits, _ = parse_search_response(search_xml)
        by_slug = {h.docdb_id: h for h in hits}
        assert "Photocatalytic" in by_slug["ep1234567b1"].title
        assert "visible-light" in by_slug["wo2023123456a1"].title.lower()

    def test_hit_applicants(self, search_xml: bytes) -> None:
        hits, _ = parse_search_response(search_xml)
        by_slug = {h.docdb_id: h for h in hits}
        assert "SIEMENS AG" in by_slug["ep1234567b1"].applicants
        assert "BASF SE" in by_slug["wo2023123456a1"].applicants

    def test_publication_date_in_hit(self, search_xml: bytes) -> None:
        hits, _ = parse_search_response(search_xml)
        by_slug = {h.docdb_id: h for h in hits}
        assert by_slug["ep1234567b1"].publication_date == "2020-01-15"

    def test_empty_xml_returns_empty(self) -> None:
        hits, total = parse_search_response(b"<root/>")
        assert hits == []
        assert total == 0
