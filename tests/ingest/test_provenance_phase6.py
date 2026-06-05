"""Phase 6.1 tests: RW cache as Crossref fallback.

Coverage:
- ``_classify_rw_nature`` mapping
- ``_rw_row_to_notice`` synthesis
- ``_merge_crossref_and_rw_notices`` — exact DOI dedup, nature
  fallback, RW-only synthesis, consumed-row tracking
- ``check_doi`` end-to-end:
  - Crossref clean + RW has retraction → surfaces RW retraction
  - Crossref 404 + RW has retraction → status=ok (not unknown)
  - Crossref fails + RW has retraction → status=ok, ``error`` populated
  - Crossref 404 + RW empty → status=unknown (unchanged)
  - Crossref fails + RW empty → status=check_failed (unchanged)
- Renderer surfaces RW-only notice with ``(RW)`` label and the
  degraded-source banner when Crossref failed
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from precis.handlers._provenance_report import render_single
from precis.ingest.provenance import (
    Notice,
    _classify_rw_nature,
    _merge_crossref_and_rw_notices,
    _rw_row_to_notice,
    _RWCacheRow,
    check_doi,
)

# ---------------------------------------------------------------------------
# Classifier + synthesiser
# ---------------------------------------------------------------------------


class TestClassifyRwNature:
    def test_retraction(self) -> None:
        sev, status, rel = _classify_rw_nature("Retraction")  # type: ignore[misc]
        assert sev == "blocker"
        assert status == "retracted"
        assert rel == "retracted-by"

    def test_partial_retraction(self) -> None:
        out = _classify_rw_nature("Partial Retraction")
        assert out is not None
        assert out[0] == "blocker"

    def test_expression_of_concern(self) -> None:
        sev, status, rel = _classify_rw_nature("Expression of Concern")  # type: ignore[misc]
        assert sev == "review"
        assert status == "expression_of_concern"

    def test_correction(self) -> None:
        sev, *_ = _classify_rw_nature("Correction")  # type: ignore[misc]
        assert sev == "note"

    def test_reinstatement_unmapped(self) -> None:
        """Reinstatement (paper un-retracted) intentionally doesn't classify."""
        assert _classify_rw_nature("Reinstatement") is None

    def test_unknown_returns_none(self) -> None:
        assert _classify_rw_nature("Weird Publisher Nature") is None


class TestRwRowToNotice:
    def test_basic(self) -> None:
        row = _RWCacheRow(
            notice_doi="10.x/foo-r1",
            notice_nature="Retraction",
            reasons=["+Falsification/Fabrication of Data"],
            retraction_date=datetime(2022, 8, 14),
            paper_title="Bad paper",
            journal="Bad Journal",
        )
        n = _rw_row_to_notice(row)
        assert n is not None
        assert n.update_type == ""  # signals RW-only
        assert n.severity == "blocker"
        assert n.status == "retracted"
        assert n.notice_doi == "10.x/foo-r1"
        assert n.notice_date == datetime(2022, 8, 14)
        assert n.rw_reasons == ["+Falsification/Fabrication of Data"]
        assert n.rw_notice_nature == "Retraction"

    def test_empty_notice_doi_handled(self) -> None:
        """RW often has no notice DOI for older retractions (Hwang case)."""
        row = _RWCacheRow(
            notice_doi=None,
            notice_nature="Retraction",
            reasons=[],
            retraction_date=None,
            paper_title=None,
            journal=None,
        )
        n = _rw_row_to_notice(row)
        assert n is not None
        assert n.notice_doi == ""  # falsy → renderer skips the DOI line

    def test_unclassified_nature_dropped(self) -> None:
        row = _RWCacheRow(
            notice_doi="10.x/foo-r1",
            notice_nature="Reinstatement",
            reasons=[],
            retraction_date=None,
            paper_title=None,
            journal=None,
        )
        assert _rw_row_to_notice(row) is None


# ---------------------------------------------------------------------------
# Merge function
# ---------------------------------------------------------------------------


def _crossref_notice(
    notice_doi: str = "10.x/foo-r1",
    severity: str = "blocker",
    status: str = "retracted",
) -> Notice:
    return Notice(
        update_type="retraction",
        severity=severity,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        relation="retracted-by",
        notice_doi=notice_doi,
        notice_date=datetime(2022, 8, 14),
        notice_title=None,
        notice_authors=None,
        notice_year=None,
    )


def _rw_row(
    notice_doi: str | None = "10.x/foo-r1",
    nature: str = "Retraction",
    reasons: list[str] | None = None,
) -> _RWCacheRow:
    return _RWCacheRow(
        notice_doi=notice_doi,
        notice_nature=nature,
        reasons=reasons or ["+Falsification/Fabrication of Data"],
        retraction_date=datetime(2022, 8, 14),
        paper_title="Bad paper",
        journal=None,
    )


class TestMergeCrossrefAndRwNotices:
    def test_no_rw_rows_passthrough(self) -> None:
        crossref = [_crossref_notice()]
        merged, consumed = _merge_crossref_and_rw_notices(crossref, [])
        assert merged == crossref
        assert consumed == set()

    def test_exact_doi_match_enriches_and_does_not_synthesize(self) -> None:
        crossref = [_crossref_notice()]
        rw = [_rw_row()]
        merged, consumed = _merge_crossref_and_rw_notices(crossref, rw)
        # Only one notice in the merged list (Crossref enriched, RW
        # row was consumed so no synthesised dupe)
        assert len(merged) == 1
        assert merged[0].rw_reasons == ["+Falsification/Fabrication of Data"]
        assert consumed == {0}

    def test_rw_only_synthesised_when_no_crossref(self) -> None:
        """The Hwang case: Crossref has no notice, but RW does."""
        crossref: list[Notice] = []
        rw = [_rw_row(notice_doi="10.x/missing-from-crossref")]
        merged, consumed = _merge_crossref_and_rw_notices(crossref, rw)
        assert len(merged) == 1
        # Synthesised: update_type is empty, severity/status correct
        assert merged[0].update_type == ""
        assert merged[0].severity == "blocker"
        assert merged[0].rw_notice_nature == "Retraction"
        assert consumed == {0}

    def test_partial_overlap(self) -> None:
        """Crossref has one notice; RW has it PLUS another the publisher missed."""
        crossref = [_crossref_notice(notice_doi="10.x/foo-r1")]
        rw = [
            _rw_row(notice_doi="10.x/foo-r1"),  # matches Crossref → enrich
            _rw_row(
                notice_doi="10.x/foo-e1", nature="Expression of Concern"
            ),  # synthesise
        ]
        merged, consumed = _merge_crossref_and_rw_notices(crossref, rw)
        assert len(merged) == 2
        # First is the enriched Crossref notice (still has update_type)
        assert merged[0].update_type == "retraction"
        assert merged[0].rw_reasons  # enriched
        # Second is synthesised from RW (empty update_type)
        assert merged[1].update_type == ""
        assert merged[1].severity == "review"
        assert consumed == {0, 1}

    def test_synthesize_skipped_when_disabled(self) -> None:
        """Back-compat mode (used by deprecated _enrich_notices_with_rw)."""
        crossref: list[Notice] = []
        rw = [_rw_row()]
        merged, consumed = _merge_crossref_and_rw_notices(
            crossref, rw, _synthesize_rw_only=False
        )
        assert merged == []
        assert consumed == set()


# ---------------------------------------------------------------------------
# check_doi end-to-end
# ---------------------------------------------------------------------------


def _store_with_rw(rows: list[_RWCacheRow]) -> MagicMock:
    """Build a mock Store whose ``_lookup_rw_cache`` returns the given rows.

    We mock the lookup helper directly rather than the store's
    pool — this isolates the test from psycopg shape and lets us
    inject arbitrary cache rows for cases that don't trivially map
    to SQL fixtures.
    """
    return MagicMock(name="store")


class TestCheckDoiRwFallback:
    """Phase 6.1: RW cache contributes regardless of Crossref outcome."""

    @patch("precis.ingest.provenance._lookup_rw_cache")
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_crossref_clean_rw_has_retraction(self, mock_fetch, mock_lookup) -> None:
        """The Hwang case — Crossref doesn't know but RW does."""
        mock_fetch.return_value = {
            "title": ["Patient-Specific Embryonic Stem Cells"],
            "author": [{"family": "Hwang"}],
            "published-print": {"date-parts": [[2005]]},
            "DOI": "10.1126/science.1112286",
        }
        mock_lookup.return_value = [_rw_row(notice_doi=None)]
        # store can be a MagicMock; check_doi tries find_ref_by_identifier
        # which we don't care about for this test (no write-through path)
        store = MagicMock()
        store.find_ref_by_identifier.return_value = None
        r = check_doi("10.1126/science.1112286", store=store)
        assert r.status == "ok"
        assert r.overall_severity == "blocker"
        assert len(r.notices) == 1
        assert r.notices[0].update_type == ""  # synthesised
        assert r.notices[0].severity == "blocker"
        assert r.notices[0].rw_notice_nature == "Retraction"

    @patch("precis.ingest.provenance._lookup_rw_cache")
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_crossref_404_rw_has_retraction(self, mock_fetch, mock_lookup) -> None:
        """Crossref didn't index the paper but RW has the retraction."""
        mock_fetch.return_value = None  # 404
        mock_lookup.return_value = [_rw_row(notice_doi="10.x/bad-r1")]
        store = MagicMock()
        store.find_ref_by_identifier.return_value = None
        r = check_doi("10.1234/bad", store=store)
        # CRITICAL: status='ok' not 'unknown' — we have data
        assert r.status == "ok"
        assert r.overall_severity == "blocker"
        # No paper_title from Crossref → falls back to RW
        assert r.paper_title == "Bad paper"

    @patch("precis.ingest.provenance._lookup_rw_cache")
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_crossref_fails_rw_has_retraction(self, mock_fetch, mock_lookup) -> None:
        """Crossref transport error but RW cache has data — degrade gracefully."""
        mock_fetch.side_effect = RuntimeError("handshake timed out")
        mock_lookup.return_value = [_rw_row()]
        store = MagicMock()
        store.find_ref_by_identifier.return_value = None
        r = check_doi("10.1234/bad", store=store)
        assert r.status == "ok"  # not check_failed — we have data
        assert r.error == "handshake timed out"  # surface degraded source
        assert r.overall_severity == "blocker"

    @patch("precis.ingest.provenance._lookup_rw_cache")
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_crossref_404_rw_empty_still_unknown(self, mock_fetch, mock_lookup) -> None:
        """Genuinely unknown DOI — neither source has it."""
        mock_fetch.return_value = None
        mock_lookup.return_value = []
        store = MagicMock()
        store.find_ref_by_identifier.return_value = None
        r = check_doi("10.1234/never-existed", store=store)
        assert r.status == "unknown"

    @patch("precis.ingest.provenance._lookup_rw_cache")
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_crossref_fails_rw_empty_still_check_failed(
        self, mock_fetch, mock_lookup
    ) -> None:
        mock_fetch.side_effect = RuntimeError("network down")
        mock_lookup.return_value = []
        store = MagicMock()
        store.find_ref_by_identifier.return_value = None
        r = check_doi("10.1234/foo", store=store)
        assert r.status == "check_failed"
        assert "network down" in (r.error or "")

    @patch("precis.ingest.provenance._lookup_rw_cache")
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_no_store_skips_rw_lookup(self, mock_fetch, mock_lookup) -> None:
        """store=None → no RW lookup, regardless of Crossref outcome."""
        mock_fetch.return_value = {
            "title": ["Anything"],
            "DOI": "10.1234/foo",
        }
        r = check_doi("10.1234/foo")  # store=None default
        assert r.status == "ok"
        # _lookup_rw_cache should not have been called
        mock_lookup.assert_not_called()


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestRenderRwOnly:
    def test_rw_only_notice_shows_rw_label(self) -> None:
        from precis.handlers._provenance_report import _format_notice_line

        n = Notice(
            update_type="",  # RW-only
            severity="blocker",
            status="retracted",
            relation="retracted-by",
            notice_doi="10.x/foo-r1",
            notice_date=datetime(2006, 1, 12),
            notice_title=None,
            notice_authors=None,
            notice_year=None,
            rw_reasons=["+Falsification/Fabrication of Data"],
            rw_notice_nature="Retraction",
        )
        out = _format_notice_line(n)
        assert "(RW)" in out
        assert "**Retraction**" in out
        assert "10.x/foo-r1" in out
        assert "+Falsification/Fabrication of Data" in out

    def test_empty_notice_doi_omitted(self) -> None:
        from precis.handlers._provenance_report import _format_notice_line

        n = Notice(
            update_type="",
            severity="blocker",
            status="retracted",
            relation="retracted-by",
            notice_doi="",  # no DOI on file — common for older retractions
            notice_date=None,
            notice_title=None,
            notice_authors=None,
            notice_year=None,
            rw_notice_nature="Retraction",
        )
        out = _format_notice_line(n)
        assert "notice DOI" not in out

    def test_degraded_source_banner_in_render_single(self) -> None:
        """When Crossref failed but we have data, the banner shows."""
        from precis.ingest.provenance import ProvenanceResult

        r = ProvenanceResult(
            doi="10.1234/foo",
            status="ok",
            paper_title="Some title",
            error="connection reset",
            notices=[
                Notice(
                    update_type="",
                    severity="blocker",
                    status="retracted",
                    relation="retracted-by",
                    notice_doi="10.1234/foo-r1",
                    notice_date=None,
                    notice_title=None,
                    notice_authors=None,
                    notice_year=None,
                    rw_notice_nature="Retraction",
                )
            ],
        )
        out = render_single(r)
        assert "Crossref unavailable" in out
        assert "connection reset" in out
        # Notice still renders normally
        assert "(RW)" in out
