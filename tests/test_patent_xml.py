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


class TestBoilerplateFilter:
    """OPS description XML interleaves page-header fragments between
    real paragraphs (``PATENT``, ``ATTORNEY DOCKET NO: …``, bare
    page numbers). Without filtering they become embedded blocks
    and surface as noise top-K hits for unrelated queries (MCP
    critic, 2026-05).
    """

    def _parse(self, body: str) -> list[str]:
        xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<description xmlns="http://www.epo.org/exchange" lang="en">'
            + body.encode("utf-8")
            + b"</description>"
        )
        return list(parse_patent(description_xml=xml).description_paragraphs)

    def test_plain_patent_header_dropped(self) -> None:
        paragraphs = self._parse("<p>PATENT</p><p>[0001] Real content here.</p>")
        assert paragraphs == ["[0001] Real content here."]

    def test_numbered_patent_header_dropped(self) -> None:
        paragraphs = self._parse(
            "<p>[0001] PATENT</p><p>[0002] Real content paragraph.</p>"
        )
        assert paragraphs == ["[0002] Real content paragraph."]

    def test_attorney_docket_dropped(self) -> None:
        paragraphs = self._parse(
            "<p>ATTORNEY DOCKET NO: 51198-064WO2</p>"
            "<p>[0001] Real paragraph about the invention.</p>"
        )
        assert paragraphs == ["[0001] Real paragraph about the invention."]

    def test_numbered_attorney_docket_dropped(self) -> None:
        paragraphs = self._parse(
            "<p>[0002] ATTORNEY DOCKET NO: 51198-064WO2</p>"
            "<p>[0003] Real content.</p>"
            "<p>[0011] ATTORNEY DOCKET NO: 51198-064WO2</p>"
        )
        assert paragraphs == ["[0003] Real content."]

    def test_bare_page_number_dropped(self) -> None:
        paragraphs = self._parse(
            "<p>4</p><p>Page 4 of 12</p><p>[0001] Substantive text here.</p>"
        )
        assert paragraphs == ["[0001] Substantive text here."]

    def test_short_paragraphs_dropped(self) -> None:
        # < 10 chars after [NNNN] strip — too short to be
        # meaningful content.
        paragraphs = self._parse(
            "<p>[0001] x</p><p>[0002] This one is long enough.</p>"
        )
        assert paragraphs == ["[0002] This one is long enough."]

    def test_real_paragraph_kept(self) -> None:
        paragraphs = self._parse(
            "<p>[0001] The invention provides a new catalyst for CO2 reduction.</p>"
        )
        assert paragraphs == [
            "[0001] The invention provides a new catalyst for CO2 reduction."
        ]


class TestClaims:
    def test_claims_extracted(self, claims_xml: bytes) -> None:
        p = parse_patent(claims_xml=claims_xml)
        assert len(p.claim_texts) == 3

    def test_first_claim(self, claims_xml: bytes) -> None:
        p = parse_patent(claims_xml=claims_xml)
        first = p.claim_texts[0]
        assert first.startswith("1. A photocatalytic system")
        assert "Z-scheme heterojunction" in first


class TestClaimBoilerplateStrip:
    """OPS inlines a ``PATENT ATTORNEY DOCKET NO: ... CLAIMS`` page
    header into the first claim's text on many WO / US filings.
    The strip runs at parse time so downstream blocks start at the
    claim number itself (MCP critic 2026-05).
    """

    def _parse(self, body: str) -> list[str]:
        xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<claims xmlns="http://www.epo.org/exchange" lang="en">'
            + body.encode("utf-8")
            + b"</claims>"
        )
        return list(parse_patent(claims_xml=xml).claim_texts)

    def test_claim_header_stripped(self) -> None:
        claims = self._parse(
            "<claim><claim-text>PATENT ATTORNEY DOCKET NO: 51198-064WO2 "
            "CLAIMS 1. A system comprising: an electrochemical cell."
            "</claim-text></claim>"
        )
        assert claims == ["1. A system comprising: an electrochemical cell."]

    def test_claim_without_header_unchanged(self) -> None:
        claims = self._parse(
            "<claim><claim-text>1. A photocatalytic system comprising: "
            "a Z-scheme heterojunction.</claim-text></claim>"
        )
        assert claims == [
            "1. A photocatalytic system comprising: a Z-scheme heterojunction."
        ]

    def test_strip_case_insensitive(self) -> None:
        claims = self._parse(
            "<claim><claim-text>Patent Attorney Docket No: X-123 "
            "Claims 1. Foo bar baz.</claim-text></claim>"
        )
        assert claims == ["1. Foo bar baz."]


class TestClaimRunSplitting:
    """A single ``<claim>`` element often holds the *entire* claims section
    (OPS ``<claim-text>`` children are arbitrary fragments, not per-claim).
    The parser splits such a block on the sequential claim number so each
    claim becomes its own text (docs/design/patent-authoring-loop.md — the
    FTO digest wants per-claim granularity, not one giant blob)."""

    def _parse(self, body: str) -> list[str]:
        xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<claims xmlns="http://www.epo.org/exchange" lang="en">'
            + body.encode("utf-8")
            + b"</claims>"
        )
        return list(parse_patent(claims_xml=xml).claim_texts)

    def test_single_claim_element_blob_splits_per_claim(self) -> None:
        # One <claim> holding three numbered claims → three claim texts.
        claims = self._parse(
            "<claim><claim-text>CLAIMS 1. A molecular computer comprising a "
            "processor and an array. 2. The computer of claim 1, wherein the "
            "array is addressable. 3. The computer of claim 1, further "
            "comprising a controller.</claim-text></claim>"
        )
        assert len(claims) == 3
        assert claims[0].startswith("1. A molecular computer")
        assert claims[1].startswith("2. The computer of claim 1")
        assert claims[2].startswith("3. The computer of claim 1")
        # The "CLAIMS" header before claim 1 is dropped (slice starts at "1.").
        assert "CLAIMS" not in claims[0]

    def test_fragment_children_concatenate_then_split(self) -> None:
        # Real OPS shape: one <claim> with several <claim-text> fragments
        # that straddle claim boundaries mid-sentence.
        claims = self._parse(
            "<claim>"
            "<claim-text>1. A system comprising: an array configured to</claim-text>"
            "<claim-text>evolve toward a solution. 2. The system of claim 1,</claim-text>"
            "<claim-text>wherein the array is molecular.</claim-text>"
            "</claim>"
        )
        assert len(claims) == 2
        assert claims[0].startswith("1. A system comprising")
        assert "evolve toward a solution" in claims[0]
        assert claims[1].startswith("2. The system of claim 1")
        assert "wherein the array is molecular" in claims[1]

    def test_claim_reference_does_not_false_split(self) -> None:
        # "according to claim 2" is a dependency reference, not a boundary.
        claims = self._parse(
            "<claim><claim-text>1. A method comprising step A and step B. "
            "2. The method according to claim 1. 3. The method according to "
            "claim 2, wherein B precedes A.</claim-text></claim>"
        )
        assert len(claims) == 3
        assert claims[2].startswith("3. The method according to claim 2")

    def test_single_claim_not_split(self) -> None:
        # A genuinely single claim (no "2.") passes through intact.
        claims = self._parse(
            "<claim><claim-text>1. A composition comprising component A, "
            "component B, and at least 2. 0 wt% of C.</claim-text></claim>"
        )
        assert claims == [
            "1. A composition comprising component A, component B, "
            "and at least 2. 0 wt% of C."
        ]


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
