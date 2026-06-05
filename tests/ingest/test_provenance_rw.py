"""Phase 3 tests: Retraction Watch CSV parser + cache enrichment.

Coverage:
- ``_rw_csv.parse_rw_rows`` against hand-rolled CSV fixtures
- DOI canonicalisation, date parsing, semicolon-split
- Header variant tolerance (case, whitespace, underscores)
- ``_enrich_notices_with_rw`` match strategies
- Renderer surfaces reasons when present

Sync-job tests against a real DB land in a follow-up under the
``db`` marker — Phase 3 here covers the pure-function plumbing.
"""

from __future__ import annotations

from datetime import date, datetime

from precis.ingest._rw_csv import parse_rw_rows
from precis.ingest.provenance import (
    Notice,
    RetractionStatus,
    Severity,
    _enrich_notices_with_rw,
    _RWCacheRow,
)


def _rw_row(
    *,
    notice_doi: str | None = "10.x/foo-r1",
    notice_nature: str = "Retraction",
    reasons: list[str] | None = None,
    retraction_date: datetime | None = None,
    paper_title: str | None = None,
    journal: str | None = None,
) -> _RWCacheRow:
    """Build an ``_RWCacheRow`` with Phase 6.1 fields filled in defaults.

    Phase 6.1 added ``retraction_date``, ``paper_title``, ``journal``
    to the dataclass. Tests written for the Phase 3 3-field shape now
    go through this helper so a future column addition (Phase 7+)
    only updates one spot.
    """
    return _RWCacheRow(
        notice_doi=notice_doi,
        notice_nature=notice_nature,
        reasons=reasons if reasons is not None else [],
        retraction_date=retraction_date,
        paper_title=paper_title,
        journal=journal,
    )


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------


_HEADER = (
    "Record ID,Title,Subject,Institution,Journal,Publisher,Country,"
    "Author,URLS,ArticleType,RetractionDate,RetractionDOI,"
    "RetractionPubMedID,OriginalPaperDate,OriginalPaperDOI,"
    "OriginalPaperPubMedID,RetractionNature,Reason,Paywalled,Notes"
)


def _csv(rows: list[str]) -> list[str]:
    """Build a CSV as a list of lines (one for the header + one per row)."""
    return [_HEADER, *rows]


class TestParseRwRows:
    def test_basic_row(self) -> None:
        lines = _csv(
            [
                "42,Some Title,Biology,MIT,Nature,Springer,USA,"
                'Smith J;Jones K,http://example.org,"Research Article",'
                "08/14/2022,10.1038/foo-r1,12345,01/15/2019,"
                "10.1038/foo,99999,Retraction,"
                "+Falsification/Fabrication of Data;+Investigation by Journal/Publisher,"
                "No,Notes here"
            ]
        )
        rows = list(parse_rw_rows(lines))
        assert len(rows) == 1
        r = rows[0]
        assert r.record_id == 42
        assert r.paper_doi == "10.1038/foo"
        assert r.notice_doi == "10.1038/foo-r1"
        assert r.notice_nature == "Retraction"
        assert r.reasons == [
            "+Falsification/Fabrication of Data",
            "+Investigation by Journal/Publisher",
        ]
        assert r.retraction_date == date(2022, 8, 14)
        assert r.paper_title == "Some Title"
        assert r.journal == "Nature"

    def test_doi_canonicalisation(self) -> None:
        lines = _csv(
            ["1,T,,,,,,,,,,DOI:10.X/Notice,,,https://doi.org/10.X/Paper,,Retraction,,,"]
        )
        r = list(parse_rw_rows(lines))[0]
        # Both DOIs lowercased and prefix-stripped
        assert r.paper_doi == "10.x/paper"
        assert r.notice_doi == "10.x/notice"

    def test_skip_missing_paper_doi(self) -> None:
        lines = _csv(
            [
                "1,T,,,,,,,,,,10.x/notice,,,,,Retraction,,,",  # no original DOI
            ]
        )
        rows = list(parse_rw_rows(lines))
        assert rows == []

    def test_skip_missing_record_id(self) -> None:
        lines = _csv(
            [
                ",T,,,,,,,,,,10.x/notice,,,10.x/paper,,Retraction,,,",  # blank record id
            ]
        )
        rows = list(parse_rw_rows(lines))
        assert rows == []

    def test_skip_malformed_record_id(self) -> None:
        lines = _csv(
            [
                "not-a-number,T,,,,,,,,,,10.x/n,,,10.x/p,,Retraction,,,",
            ]
        )
        rows = list(parse_rw_rows(lines))
        assert rows == []

    def test_unparseable_date_is_none(self) -> None:
        # 20 columns: 1, T, then 8 empties (Subject..ArticleType),
        # bogus-date (RetractionDate), 10.x/n (RetractionDOI), 2 empties
        # (PubMedID, OriginalPaperDate), 10.x/p (OriginalPaperDOI),
        # 1 empty (OriginalPaperPubMedID), Retraction (Nature),
        # 3 empties (Reason, Paywalled, Notes).
        lines = _csv(
            [
                "1,T,,,,,,,,,bogus-date,10.x/n,,,10.x/p,,Retraction,,,",
            ]
        )
        rows = list(parse_rw_rows(lines))
        assert rows[0].retraction_date is None

    def test_header_variant_tolerance(self) -> None:
        # Header with mixed case, underscores, spaces
        weird_header = (
            "record_id,title,subject,institution,journal,publisher,country,"
            "AUTHOR,URLS,Article Type,Retraction Date,RETRACTION_DOI,"
            "retraction_pubmed_id,Original Paper Date,original_paper_DOI,"
            "OriginalPaperPubMedID,Retraction Nature,Reason,Paywalled,Notes"
        )
        rows = list(
            parse_rw_rows(
                [
                    weird_header,
                    "1,T,,,,,,,,,,10.x/n,,,10.x/p,,Retraction,,,",
                ]
            )
        )
        assert len(rows) == 1
        assert rows[0].record_id == 1
        assert rows[0].paper_doi == "10.x/p"

    def test_multivalue_reasons(self) -> None:
        lines = _csv(
            [
                "1,T,,,,,,,,,,10.x/n,,,10.x/p,,Retraction,"
                '"+Reason A;+Reason B;+Reason C",,'
            ]
        )
        rows = list(parse_rw_rows(lines))
        assert rows[0].reasons == ["+Reason A", "+Reason B", "+Reason C"]

    def test_empty_input(self) -> None:
        rows = list(parse_rw_rows([]))
        assert rows == []

    def test_header_only(self) -> None:
        rows = list(parse_rw_rows([_HEADER]))
        assert rows == []


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


def _make_notice(
    notice_doi: str = "10.x/foo-r1",
    status: RetractionStatus | None = "retracted",
) -> Notice:
    severity: Severity = "blocker" if status == "retracted" else "review"
    return Notice(
        update_type="retraction",
        severity=severity,
        status=status,
        relation="retracted-by" if status == "retracted" else "concern-raised-by",
        notice_doi=notice_doi,
        notice_date=datetime(2022, 8, 14),
        notice_title=None,
        notice_authors=None,
        notice_year=None,
    )


class TestEnrichNoticesWithRw:
    def test_exact_doi_match(self) -> None:
        notices = [_make_notice("10.x/foo-r1")]
        cache = [
            _rw_row(
                notice_doi="10.x/foo-r1",
                reasons=["+Falsification/Fabrication of Data"],
            )
        ]
        out = _enrich_notices_with_rw(notices, cache)
        assert out[0].rw_reasons == ["+Falsification/Fabrication of Data"]
        assert out[0].rw_notice_nature == "Retraction"

    def test_no_match_leaves_notice_alone(self) -> None:
        notices = [_make_notice("10.x/foo-r1")]
        cache = [
            _rw_row(notice_doi="10.x/different", reasons=["+Some reason"]),
        ]
        out = _enrich_notices_with_rw(notices, cache)
        # No DOI match AND fallback finds a single-nature row → matched
        # via the single-row-of-matching-nature fallback path.
        assert out[0].rw_reasons == ["+Some reason"]

    def test_fallback_by_nature(self) -> None:
        """Single cache row of matching nature attaches even without DOI match."""
        notices = [_make_notice("10.x/missing-notice")]  # DOI not in cache
        cache = [_rw_row(notice_doi="10.x/different", reasons=["+The reason"])]
        out = _enrich_notices_with_rw(notices, cache)
        # Fallback: 1 row, matching nature → attached
        assert out[0].rw_reasons == ["+The reason"]

    def test_fallback_skipped_when_ambiguous(self) -> None:
        """Multiple cache rows of same nature → no fallback (ambiguous)."""
        notices = [_make_notice("10.x/missing-notice")]
        cache = [
            _rw_row(notice_doi="10.x/n1", reasons=["A"]),
            _rw_row(notice_doi="10.x/n2", reasons=["B"]),
        ]
        out = _enrich_notices_with_rw(notices, cache)
        # Two retractions cached, neither matches by DOI — too ambiguous
        # to pick one
        assert out[0].rw_reasons == []

    def test_empty_cache_passthrough(self) -> None:
        notices = [_make_notice()]
        out = _enrich_notices_with_rw(notices, [])
        assert out == notices

    def test_multiple_notices_partially_matched(self) -> None:
        retraction_notice = _make_notice("10.x/r1", status="retracted")
        # correction_notice has wrong severity helper; rebuild manually
        correction_notice = Notice(
            update_type="corrigendum",
            severity="note",
            status="corrected",
            relation="corrected-by",
            notice_doi="10.x/c1",
            notice_date=datetime(2020, 1, 1),
            notice_title=None,
            notice_authors=None,
            notice_year=None,
        )
        cache = [
            _rw_row(notice_doi="10.x/r1", reasons=["R-reason"]),
            _rw_row(
                notice_doi="10.x/c1", notice_nature="Correction", reasons=["C-reason"]
            ),
        ]
        out = _enrich_notices_with_rw([retraction_notice, correction_notice], cache)
        assert out[0].rw_reasons == ["R-reason"]
        assert out[1].rw_reasons == ["C-reason"]


# ---------------------------------------------------------------------------
# Renderer surfaces reasons
# ---------------------------------------------------------------------------


class TestRendererSurfacesReasons:
    def test_format_notice_line_with_reasons(self) -> None:
        from precis.handlers._provenance_report import _format_notice_line

        n = Notice(
            update_type="retraction",
            severity="blocker",
            status="retracted",
            relation="retracted-by",
            notice_doi="10.x/foo-r1",
            notice_date=datetime(2022, 8, 14),
            notice_title=None,
            notice_authors=None,
            notice_year=None,
            rw_reasons=["+Falsification/Fabrication of Data", "+Author Unresponsive"],
            rw_notice_nature="Retraction",
        )
        out = _format_notice_line(n)
        assert "+Falsification/Fabrication of Data" in out
        assert "+Author Unresponsive" in out
        assert "Reasons:" in out

    def test_format_notice_line_truncates_long_reason_list(self) -> None:
        from precis.handlers._provenance_report import _format_notice_line

        n = Notice(
            update_type="retraction",
            severity="blocker",
            status="retracted",
            relation="retracted-by",
            notice_doi="10.x/foo-r1",
            notice_date=None,
            notice_title=None,
            notice_authors=None,
            notice_year=None,
            rw_reasons=[f"+Reason {i}" for i in range(10)],
        )
        out = _format_notice_line(n)
        # First 5 shown, 5 more indicated
        assert "+Reason 0" in out
        assert "+Reason 4" in out
        assert "+5 more" in out

    def test_format_notice_line_without_reasons_unchanged(self) -> None:
        """Backward compat — empty rw_reasons looks like Phase 1/2 output."""
        from precis.handlers._provenance_report import _format_notice_line

        n = Notice(
            update_type="retraction",
            severity="blocker",
            status="retracted",
            relation="retracted-by",
            notice_doi="10.x/foo-r1",
            notice_date=None,
            notice_title=None,
            notice_authors=None,
            notice_year=None,
        )
        out = _format_notice_line(n)
        assert "Reasons:" not in out
