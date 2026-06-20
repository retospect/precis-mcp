"""Unit tests for ``precis.ingest.provenance``.

Pure-function tests for DOI validation, classification, slug
generation, and the dominant-status pick. Integration tests
(``check_doi`` with Crossref mocked, store=None path) live here too;
write-through tests against a live store land separately in Phase 2
under the ``db`` marker.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from precis.ingest.provenance import (
    Notice,
    ProvenanceResult,
    check_doi,
    check_dois,
    classify_update_type,
    dominant_status,
    make_notice_slug,
    parse_doi_list,
    validate_doi,
)

# ---------------------------------------------------------------------------
# Crossref response fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def crossref_clean() -> dict:
    """A Crossref ``message`` dict for a clean paper (no notices)."""
    return {
        "title": ["A perfectly fine paper"],
        "author": [
            {"family": "Wang", "given": "Wei"},
            {"family": "Doe", "given": "Alice"},
        ],
        "published-print": {"date-parts": [[2020, 3, 15]]},
        "DOI": "10.1234/clean",
        "type": "journal-article",
    }


@pytest.fixture
def crossref_retracted() -> dict:
    """A Crossref message with a retraction notice attached."""
    return {
        "title": ["Now-retracted findings on X"],
        "author": [{"family": "Hwang", "given": "Woo-Suk"}],
        "published-print": {"date-parts": [[2005, 6, 17]]},
        "DOI": "10.1038/nature05095",
        "type": "journal-article",
        "update-to": [
            {
                "DOI": "10.1038/nature05095-r1",
                "type": "retraction",
                "label": "Retraction Notice",
                "updated": {"date-parts": [[2006, 1, 12]]},
            },
        ],
    }


@pytest.fixture
def crossref_eoc() -> dict:
    """A Crossref message with an Expression of Concern."""
    return {
        "title": ["Contested findings under review"],
        "author": [{"family": "Smith", "given": "Bob"}],
        "DOI": "10.5678/contested",
        "update-to": [
            {
                "DOI": "10.5678/contested-eoc1",
                "type": "expression_of_concern",
                "updated": {"date-parts": [[2024, 3, 10]]},
            },
        ],
    }


@pytest.fixture
def crossref_multi_notice() -> dict:
    """A paper with both a correction and a later retraction."""
    return {
        "title": ["Paper with mixed history"],
        "DOI": "10.9999/mixed",
        "update-to": [
            {
                "DOI": "10.9999/mixed-c1",
                "type": "corrigendum",
                "updated": {"date-parts": [[2018, 5, 1]]},
            },
            {
                "DOI": "10.9999/mixed-r1",
                "type": "retraction",
                "updated": {"date-parts": [[2022, 8, 14]]},
            },
        ],
    }


# ---------------------------------------------------------------------------
# DOI validation
# ---------------------------------------------------------------------------


class TestValidateDoi:
    def test_bare_doi(self) -> None:
        assert validate_doi("10.1038/nature05095") == "10.1038/nature05095"

    def test_lowercases(self) -> None:
        assert validate_doi("10.1038/Nature05095") == "10.1038/nature05095"

    def test_strips_doi_prefix(self) -> None:
        assert validate_doi("doi:10.1234/foo") == "10.1234/foo"

    def test_strips_doi_prefix_uppercase(self) -> None:
        assert validate_doi("DOI:10.1234/Foo") == "10.1234/foo"

    def test_strips_url_form(self) -> None:
        assert validate_doi("https://doi.org/10.1234/foo") == "10.1234/foo"

    def test_strips_dx_url_form(self) -> None:
        assert validate_doi("https://dx.doi.org/10.1234/foo") == "10.1234/foo"

    def test_whitespace_stripped(self) -> None:
        assert validate_doi("  10.1234/foo  ") == "10.1234/foo"

    def test_rejects_empty(self) -> None:
        assert validate_doi("") is None

    def test_rejects_garbage(self) -> None:
        assert validate_doi("not-a-doi") is None

    def test_rejects_short_registrant(self) -> None:
        # registrant must be 4-9 digits
        assert validate_doi("10.12/foo") is None

    def test_rejects_no_slash(self) -> None:
        assert validate_doi("10.1234foo") is None

    def test_rejects_no_suffix(self) -> None:
        assert validate_doi("10.1234/") is None


# ---------------------------------------------------------------------------
# Update-type classification
# ---------------------------------------------------------------------------


class TestClassifyUpdateType:
    def test_retraction_is_blocker(self) -> None:
        result = classify_update_type("retraction")
        assert result is not None
        sev, status, rel = result
        assert sev == "blocker"
        assert status == "retracted"
        assert rel == "retracted-by"

    def test_partial_retraction_is_blocker(self) -> None:
        result = classify_update_type("partial_retraction")
        assert result is not None
        assert result[0] == "blocker"

    def test_withdrawal_is_blocker(self) -> None:
        assert classify_update_type("withdrawal")[0] == "blocker"  # type: ignore[index]

    def test_eoc_is_review(self) -> None:
        result = classify_update_type("expression_of_concern")
        assert result is not None
        sev, status, rel = result
        assert sev == "review"
        assert status == "expression_of_concern"
        assert rel == "concern-raised-by"

    def test_corrigendum_is_note(self) -> None:
        result = classify_update_type("corrigendum")
        assert result is not None
        sev, status, rel = result
        assert sev == "note"
        assert status == "corrected"
        assert rel == "corrected-by"

    def test_erratum_is_note(self) -> None:
        assert classify_update_type("erratum")[0] == "note"  # type: ignore[index]

    def test_addendum_is_info(self) -> None:
        assert classify_update_type("addendum")[0] == "info"  # type: ignore[index]

    def test_clarification_is_info(self) -> None:
        assert classify_update_type("clarification")[0] == "info"  # type: ignore[index]

    def test_new_version_is_unmapped(self) -> None:
        # new_version is informational and uses 'supersedes' (out of
        # scope for Phase 1)
        assert classify_update_type("new_version") is None

    def test_unknown_returns_none(self) -> None:
        assert classify_update_type("strange-publisher-type") is None

    def test_case_insensitive(self) -> None:
        assert classify_update_type("RETRACTION") is not None
        assert classify_update_type("Retraction") is not None

    def test_hyphen_vs_underscore(self) -> None:
        # Crossref uses underscores; agents may pass hyphens
        assert classify_update_type("expression-of-concern") is not None


# ---------------------------------------------------------------------------
# Notice slug
# ---------------------------------------------------------------------------


class TestMakeNoticeSlug:
    def test_retraction(self) -> None:
        assert make_notice_slug("hwang2005evidence", "retracted-by", 1) == (
            "hwang2005evidence-r1"
        )

    def test_correction(self) -> None:
        assert make_notice_slug("smith2020study", "corrected-by", 1) == (
            "smith2020study-c1"
        )

    def test_concern(self) -> None:
        assert make_notice_slug("doe2024contested", "concern-raised-by", 1) == (
            "doe2024contested-e1"
        )

    def test_sequence(self) -> None:
        assert make_notice_slug("foo2020bar", "corrected-by", 3) == "foo2020bar-c3"


# ---------------------------------------------------------------------------
# Dominant status
# ---------------------------------------------------------------------------


class TestDominantStatus:
    def test_empty(self) -> None:
        assert dominant_status([]) is None

    def test_single(self) -> None:
        assert dominant_status(["corrected"]) == "corrected"

    def test_retraction_dominates(self) -> None:
        assert (
            dominant_status(["corrected", "retracted", "expression_of_concern"])
            == "retracted"
        )

    def test_eoc_dominates_correction(self) -> None:
        assert (
            dominant_status(["corrected", "expression_of_concern"])
            == "expression_of_concern"
        )


# ---------------------------------------------------------------------------
# check_doi — Crossref mocked, store=None
# ---------------------------------------------------------------------------


class TestCheckDoiNoStore:
    """Happy paths with the store deliberately omitted — no write-through.

    The store=None branch is the pre-ingest workflow: an agent
    checks an arbitrary DOI before deciding whether to ingest.
    """

    def test_malformed_returns_early(self) -> None:
        result = check_doi("not-a-doi")
        assert result.status == "malformed"
        # No HTTP call should be made for a malformed DOI
        assert result.notices == []

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_unknown_doi(self, mock_fetch) -> None:
        mock_fetch.return_value = None
        result = check_doi("10.9999/never-existed")
        assert result.status == "unknown"
        assert result.doi == "10.9999/never-existed"

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_check_failed_on_exception(self, mock_fetch) -> None:
        mock_fetch.side_effect = RuntimeError("transport went sideways")
        result = check_doi("10.1234/foo")
        assert result.status == "check_failed"
        assert result.error is not None
        assert "transport went sideways" in result.error

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_clean_paper(self, mock_fetch, crossref_clean: dict) -> None:
        mock_fetch.return_value = crossref_clean
        result = check_doi("10.1234/clean")
        assert result.status == "ok"
        assert result.notices == []
        assert result.applied_status is None
        assert result.paper_in_store is False
        assert result.paper_title == "A perfectly fine paper"
        assert result.paper_year == 2020
        assert result.overall_severity == "info"

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_retracted_paper(self, mock_fetch, crossref_retracted: dict) -> None:
        mock_fetch.return_value = crossref_retracted
        result = check_doi("10.1038/nature05095")
        assert result.status == "ok"
        assert len(result.notices) == 1
        notice = result.notices[0]
        assert notice.severity == "blocker"
        assert notice.status == "retracted"
        assert notice.relation == "retracted-by"
        assert notice.notice_doi == "10.1038/nature05095-r1"
        assert notice.notice_date == datetime(2006, 1, 12)
        # No store → no persisted ref id
        assert notice.persisted_ref_id is None
        assert result.applied_status is None  # store=None, no write-through
        assert result.overall_severity == "blocker"

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_eoc_paper(self, mock_fetch, crossref_eoc: dict) -> None:
        mock_fetch.return_value = crossref_eoc
        result = check_doi("10.5678/contested")
        assert result.status == "ok"
        assert len(result.notices) == 1
        assert result.notices[0].severity == "review"
        assert result.notices[0].relation == "concern-raised-by"
        assert result.overall_severity == "review"

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_multi_notice_chronological(
        self,
        mock_fetch,
        crossref_multi_notice: dict,
    ) -> None:
        """Notices come back sorted by date (earliest first)."""
        mock_fetch.return_value = crossref_multi_notice
        result = check_doi("10.9999/mixed")
        assert result.status == "ok"
        assert len(result.notices) == 2
        # Correction is earlier (2018) — should come first
        assert result.notices[0].severity == "note"
        assert result.notices[0].notice_doi == "10.9999/mixed-c1"
        # Retraction is later (2022) — comes second
        assert result.notices[1].severity == "blocker"
        assert result.notices[1].notice_doi == "10.9999/mixed-r1"
        # Overall severity is the highest tier — blocker
        assert result.overall_severity == "blocker"

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_canonicalises_doi_in_result(
        self, mock_fetch, crossref_clean: dict
    ) -> None:
        """Input ``https://doi.org/10.X/Foo`` is canonicalised in the result."""
        mock_fetch.return_value = crossref_clean
        result = check_doi("https://doi.org/10.1234/Foo")
        assert result.doi == "10.1234/foo"


# ---------------------------------------------------------------------------
# ProvenanceResult.overall_severity edge cases
# ---------------------------------------------------------------------------


class TestOverallSeverity:
    def test_no_notices_is_info(self) -> None:
        r = ProvenanceResult(doi="10.x/foo", status="ok")
        assert r.overall_severity == "info"

    def test_picks_highest(self) -> None:
        notices = [
            Notice(
                update_type="corrigendum",
                severity="note",
                status="corrected",
                relation="corrected-by",
                notice_doi="10.x/foo-c1",
                notice_date=None,
                notice_title=None,
                notice_authors=None,
                notice_year=None,
            ),
            Notice(
                update_type="expression_of_concern",
                severity="review",
                status="expression_of_concern",
                relation="concern-raised-by",
                notice_doi="10.x/foo-e1",
                notice_date=None,
                notice_title=None,
                notice_authors=None,
                notice_year=None,
            ),
        ]
        r = ProvenanceResult(doi="10.x/foo", status="ok", notices=notices)
        assert r.overall_severity == "review"


# ---------------------------------------------------------------------------
# Phase 2: batch input parsing
# ---------------------------------------------------------------------------


class TestParseDoiList:
    def test_comma_separated(self) -> None:
        assert parse_doi_list("10.x/a, 10.x/b, 10.x/c") == [
            "10.x/a",
            "10.x/b",
            "10.x/c",
        ]

    def test_whitespace_separated(self) -> None:
        assert parse_doi_list("10.x/a 10.x/b\t10.x/c") == [
            "10.x/a",
            "10.x/b",
            "10.x/c",
        ]

    def test_newline_separated(self) -> None:
        assert parse_doi_list("10.x/a\n10.x/b\n10.x/c") == [
            "10.x/a",
            "10.x/b",
            "10.x/c",
        ]

    def test_strips_comments(self) -> None:
        text = "# refs from manuscript v3\n10.x/a\n# section 2\n10.x/b\n"
        assert parse_doi_list(text) == ["10.x/a", "10.x/b"]

    def test_strips_bullets(self) -> None:
        text = "- 10.x/a\n* 10.x/b\n+ 10.x/c\n"
        assert parse_doi_list(text) == ["10.x/a", "10.x/b", "10.x/c"]

    def test_preserves_order(self) -> None:
        # Order matters for the preflight report (map back to lines)
        assert parse_doi_list("10.x/c, 10.x/a, 10.x/b") == [
            "10.x/c",
            "10.x/a",
            "10.x/b",
        ]

    def test_keeps_duplicates(self) -> None:
        # No silent de-dup — the caller may have a reason to check twice
        assert parse_doi_list("10.x/a, 10.x/a") == ["10.x/a", "10.x/a"]

    def test_empty_input(self) -> None:
        assert parse_doi_list("") == []
        assert parse_doi_list("   \n\t  ") == []
        assert parse_doi_list("# comment only\n") == []

    def test_malformed_tokens_passed_through(self) -> None:
        # The parser is permissive — validate_doi catches malformed
        # entries downstream so the report can show them explicitly.
        assert parse_doi_list("10.x/a, not-a-doi, 10.x/b") == [
            "10.x/a",
            "not-a-doi",
            "10.x/b",
        ]


# ---------------------------------------------------------------------------
# Phase 2: batch entry point
# ---------------------------------------------------------------------------


class TestCheckDois:
    def test_empty_input(self) -> None:
        assert check_dois([]) == []

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_single_doi_fast_path(self, mock_fetch, crossref_clean: dict) -> None:
        mock_fetch.return_value = crossref_clean
        results = check_dois(["10.1234/clean"])
        assert len(results) == 1
        assert results[0].status == "ok"

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_order_preserved(
        self,
        mock_fetch,
        crossref_clean: dict,
        crossref_retracted: dict,
        crossref_eoc: dict,
    ) -> None:
        """Results come back in input order — critical for preflight UX."""

        # Mock returns a different response per DOI by inspecting the
        # call args.
        def fake(doi: str, mailto) -> dict:
            return {
                "10.1234/clean": crossref_clean,
                "10.1038/nature05095": crossref_retracted,
                "10.5678/contested": crossref_eoc,
            }[doi]

        mock_fetch.side_effect = fake
        dois = ["10.1234/clean", "10.1038/nature05095", "10.5678/contested"]
        results = check_dois(dois)
        assert [r.doi for r in results] == dois
        assert results[0].overall_severity == "info"
        assert results[1].overall_severity == "blocker"
        assert results[2].overall_severity == "review"

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_failure_isolation(
        self,
        mock_fetch,
        crossref_clean: dict,
    ) -> None:
        """A transport error on one DOI doesn't kill the batch.

        The "broken" DOI uses a 4-digit registrant (``10.5555/…``) so it
        passes ``validate_doi`` and actually reaches the mock — an
        earlier draft used ``10.broken/foo`` which the validator
        rejected as malformed before the mock's side_effect could fire.
        """

        def fake(doi: str, mailto):
            if doi == "10.5555/broken":
                raise RuntimeError("transport went sideways")
            return crossref_clean

        mock_fetch.side_effect = fake
        results = check_dois(["10.1234/clean", "10.5555/broken", "10.1234/clean"])
        assert len(results) == 3
        assert results[0].status == "ok"
        assert results[1].status == "check_failed"
        assert results[2].status == "ok"

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_mixed_statuses_in_batch(
        self,
        mock_fetch,
        crossref_clean: dict,
    ) -> None:
        """Malformed DOIs are detected pre-flight; never hit Crossref."""
        mock_fetch.return_value = crossref_clean
        results = check_dois(["10.1234/clean", "not-a-doi", "10.1234/clean"])
        assert [r.status for r in results] == ["ok", "malformed", "ok"]
        # Malformed DOIs short-circuit before HTTP, so _fetch_crossref
        # is called only for the valid ones.
        assert mock_fetch.call_count == 2


# ---------------------------------------------------------------------------
# Phase 2: batch renderer + views
# ---------------------------------------------------------------------------


class TestRenderBatch:
    def _make_clean(self, doi: str = "10.x/clean") -> ProvenanceResult:
        return ProvenanceResult(
            doi=doi,
            status="ok",
            paper_title="A clean paper",
            paper_year=2020,
        )

    def _make_retracted(self, doi: str = "10.x/bad") -> ProvenanceResult:
        return ProvenanceResult(
            doi=doi,
            status="ok",
            paper_title="A retracted paper",
            notices=[
                Notice(
                    update_type="retraction",
                    severity="blocker",
                    status="retracted",
                    relation="retracted-by",
                    notice_doi=f"{doi}-r1",
                    notice_date=None,
                    notice_title=None,
                    notice_authors=None,
                    notice_year=None,
                ),
            ],
        )

    def test_empty(self) -> None:
        from precis.handlers._provenance_report import render_batch

        out = render_batch([])
        assert "No DOIs to check" in out

    def test_default_view_groups_by_severity(self) -> None:
        from precis.handlers._provenance_report import render_batch

        results = [
            self._make_clean("10.x/clean1"),
            self._make_retracted("10.x/bad1"),
            self._make_clean("10.x/clean2"),
        ]
        out = render_batch(results, view="default")
        # Summary line with counts
        assert "3/3" in out  # 3 resolved
        assert "🔴 1" in out
        # Blocker section appears
        assert "🔴 Blocker (1)" in out
        # Clean section appears
        assert "🟢 Clean (2)" in out

    def test_blockers_view_hides_low_severity(self) -> None:
        from precis.handlers._provenance_report import render_batch

        results = [
            self._make_clean("10.x/clean1"),
            self._make_retracted("10.x/bad1"),
            self._make_clean("10.x/clean2"),
        ]
        out = render_batch(results, view="blockers")
        # Blocker section appears
        assert "🔴 Blocker (1)" in out
        # Clean section suppressed
        assert "🟢 Clean" not in out
        # Suppression note shows the count
        assert "2 entries hidden" in out

    def test_json_view_is_valid_json(self) -> None:
        import json as _json

        from precis.handlers._provenance_report import render_batch

        results = [self._make_retracted("10.x/bad1"), self._make_clean("10.x/clean1")]
        out = render_batch(results, view="json")
        payload = _json.loads(out)
        assert payload["count"] == 2
        assert len(payload["results"]) == 2
        # overall_severity is a property — it should be in the JSON
        # even though it's computed at access time.
        first = payload["results"][0]
        assert first["overall_severity"] == "blocker"
        assert first["doi"] == "10.x/bad1"

    def test_default_view_surfaces_malformed(self) -> None:
        from precis.handlers._provenance_report import render_batch

        results = [
            ProvenanceResult(doi="not-a-doi", status="malformed"),
            self._make_clean(),
        ]
        out = render_batch(results, view="default")
        assert "Malformed DOI (1)" in out
        assert "not-a-doi" in out

    def test_default_view_surfaces_check_failed(self) -> None:
        from precis.handlers._provenance_report import render_batch

        results = [
            ProvenanceResult(
                doi="10.x/down",
                status="check_failed",
                error="connection reset",
            ),
        ]
        out = render_batch(results, view="default")
        assert "Check failed (transport error)" in out
        assert "connection reset" in out


# ---------------------------------------------------------------------------
# Phase 3.5: numbered-result rendering (standardised LLM output)
# ---------------------------------------------------------------------------


class TestInputIndex:
    """``input_index`` is the 1-based input position, stable across views."""

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_index_preserved_in_input_order(
        self,
        mock_fetch,
        crossref_clean: dict,
    ) -> None:
        """Index reflects input order, not thread-pool completion order."""
        mock_fetch.return_value = crossref_clean
        dois = ["10.1234/a", "10.5678/b", "10.9999/c"]
        results = check_dois(dois)
        assert [r.input_index for r in results] == [1, 2, 3]

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_single_doi_batch_gets_index_one(
        self,
        mock_fetch,
        crossref_clean: dict,
    ) -> None:
        """Single-DOI fast path still assigns index=1 for consistency."""
        mock_fetch.return_value = crossref_clean
        results = check_dois(["10.1234/clean"])
        assert len(results) == 1
        assert results[0].input_index == 1

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_direct_check_doi_index_zero(
        self,
        mock_fetch,
        crossref_clean: dict,
    ) -> None:
        """Direct check_doi (not via batch) leaves input_index=0 — the
        sentinel that says "no batch position; don't render #N"."""
        mock_fetch.return_value = crossref_clean
        result = check_doi("10.1234/clean")
        assert result.input_index == 0

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_failed_result_still_carries_index(self, mock_fetch) -> None:
        """An exception in a worker doesn't lose the input_index — the
        check_failed result keeps its position."""

        def fake(doi: str, mailto):
            if "broken" in doi:
                raise RuntimeError("boom")
            return {"title": ["Ok"], "DOI": doi}

        mock_fetch.side_effect = fake
        results = check_dois(["10.1234/ok", "10.5555/broken", "10.9999/ok"])
        assert [r.input_index for r in results] == [1, 2, 3]
        assert results[1].status == "check_failed"
        assert results[1].input_index == 2

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_malformed_input_keeps_position(self, mock_fetch) -> None:
        """Malformed DOIs short-circuit before HTTP but still get an index."""
        mock_fetch.return_value = {"title": ["Ok"], "DOI": "x"}
        results = check_dois(["10.1234/a", "not-a-doi", "10.5678/b"])
        assert [r.input_index for r in results] == [1, 2, 3]
        assert results[1].status == "malformed"

    def test_index_renders_in_default_view(self) -> None:
        from precis.handlers._provenance_report import render_batch

        results = [
            ProvenanceResult(
                doi="10.x/a",
                status="ok",
                paper_title="Title A",
                input_index=1,
            ),
            ProvenanceResult(
                doi="10.x/b",
                status="ok",
                paper_title="Title B",
                input_index=2,
            ),
        ]
        out = render_batch(results, view="default")
        assert "**#1**" in out
        assert "**#2**" in out

    def test_index_renders_in_blockers_view_skipping_hidden(self) -> None:
        """A 🔴 at position 47 shows as #47, even with #1..#46 hidden."""
        from precis.handlers._provenance_report import render_batch

        results = []
        # 46 clean entries (will be hidden in blockers view)
        for i in range(1, 47):
            results.append(
                ProvenanceResult(
                    doi=f"10.x/clean{i}",
                    status="ok",
                    input_index=i,
                )
            )
        # One retracted at position 47
        results.append(
            ProvenanceResult(
                doi="10.x/bad",
                status="ok",
                input_index=47,
                notices=[
                    Notice(
                        update_type="retraction",
                        severity="blocker",
                        status="retracted",
                        relation="retracted-by",
                        notice_doi="10.x/bad-r1",
                        notice_date=None,
                        notice_title=None,
                        notice_authors=None,
                        notice_year=None,
                    ),
                ],
            )
        )
        out = render_batch(results, view="blockers")
        assert "**#47**" in out
        # The clean ones (#1-#46) should be hidden
        assert "**#1** ·" not in out

    def test_index_in_json_view(self) -> None:
        import json as _json

        from precis.handlers._provenance_report import render_batch

        results = [
            ProvenanceResult(
                doi="10.x/a",
                status="ok",
                input_index=1,
            ),
            ProvenanceResult(
                doi="10.x/b",
                status="ok",
                input_index=2,
            ),
        ]
        payload = _json.loads(render_batch(results, view="json"))
        # input_index is part of the dataclass; asdict picks it up
        assert payload["results"][0]["input_index"] == 1
        assert payload["results"][1]["input_index"] == 2

    def test_index_renders_for_malformed_unknown(self) -> None:
        from precis.handlers._provenance_report import render_batch

        results = [
            ProvenanceResult(doi="not-a-doi", status="malformed", input_index=1),
            ProvenanceResult(doi="10.x/missing", status="unknown", input_index=2),
        ]
        out = render_batch(results, view="default")
        assert "**#1**" in out  # malformed entry
        assert "**#2**" in out  # unknown entry

    def test_zero_index_renders_no_prefix(self) -> None:
        """When ``input_index=0`` (single-result-not-in-batch), no #N shown."""
        from precis.handlers._provenance_report import render_batch

        results = [
            ProvenanceResult(
                doi="10.x/a",
                status="ok",
                paper_title="Title A",
                input_index=0,  # sentinel
            ),
        ]
        out = render_batch(results, view="default")
        assert "**#" not in out


def test_fetch_crossref_message_connection_error_raises_upstream() -> None:
    """A transport failure (no HTTP ``response``) surfaces as a clean,
    retryable ``Upstream`` instead of leaking the raw "Connection
    aborted" exception to the agent (gripe #39244). A real 404 still
    maps to ``None`` (DOI simply not found)."""
    import habanero

    from precis.errors import Upstream
    from precis.ingest.provenance import _fetch_crossref_message

    class _BoomCrossref:
        def __init__(self, *a, **k) -> None: ...

        def works(self, *a, **k):
            raise ConnectionError("('Connection aborted.', RemoteDisconnected(...))")

    with patch.object(habanero, "Crossref", _BoomCrossref):
        with pytest.raises(Upstream):
            _fetch_crossref_message("10.1038/nature05095", mailto=None)
