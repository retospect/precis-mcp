"""Phase 4 tests: transitive cite-walk.

Coverage:
- ``_extract_cited_dois`` from a Crossref message
- ``_check_cited_doi`` shallow-fetch + filtering (only ≥ 🟠 surface)
- Per-batch dedup cache (shared CiteCache hits Crossref once per DOI)
- ``check_doi(transitive=True)`` end-to-end
- Renderer surfaces "Cites retracted" section + bucket promotion
"""

from __future__ import annotations

from unittest.mock import patch

from precis.handlers._provenance_report import render_batch
from precis.ingest.provenance import (
    Notice,
    ProvenanceResult,
    TransitiveCiteFinding,
    _check_cited_doi,
    _extract_cited_dois,
    check_doi,
    check_dois,
)


# ---------------------------------------------------------------------------
# Reference extraction
# ---------------------------------------------------------------------------


class TestExtractCitedDois:
    def test_basic(self) -> None:
        msg = {
            "reference": [
                {"DOI": "10.1234/foo"},
                {"DOI": "10.5678/bar"},
            ]
        }
        assert _extract_cited_dois(msg) == ["10.1234/foo", "10.5678/bar"]

    def test_canonicalises_doi(self) -> None:
        msg = {
            "reference": [
                {"DOI": "https://doi.org/10.X/Foo"},
                {"DOI": "DOI:10.Y/Bar"},
            ]
        }
        assert _extract_cited_dois(msg) == ["10.x/foo", "10.y/bar"]

    def test_skips_references_without_doi(self) -> None:
        msg = {
            "reference": [
                {"DOI": "10.1234/foo"},
                {"unstructured": "Some book chapter"},  # no DOI
                {"DOI": "10.5678/bar"},
            ]
        }
        assert _extract_cited_dois(msg) == ["10.1234/foo", "10.5678/bar"]

    def test_deduplicates(self) -> None:
        msg = {
            "reference": [
                {"DOI": "10.1234/foo"},
                {"DOI": "10.1234/foo"},  # cited twice
                {"DOI": "10.5678/bar"},
            ]
        }
        assert _extract_cited_dois(msg) == ["10.1234/foo", "10.5678/bar"]

    def test_filters_malformed(self) -> None:
        msg = {
            "reference": [
                {"DOI": "10.1234/foo"},
                {"DOI": "not-a-doi"},
                {"DOI": ""},
            ]
        }
        assert _extract_cited_dois(msg) == ["10.1234/foo"]

    def test_empty_reference_list(self) -> None:
        assert _extract_cited_dois({}) == []
        assert _extract_cited_dois({"reference": []}) == []


# ---------------------------------------------------------------------------
# Shallow check
# ---------------------------------------------------------------------------


class TestCheckCitedDoi:
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_returns_none_for_clean(self, mock_fetch) -> None:
        mock_fetch.return_value = {"title": ["Clean"], "DOI": "10.1234/clean"}
        cache: dict = {}
        assert _check_cited_doi("10.1234/clean", mailto=None, cache=cache) is None

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_returns_finding_for_retracted(self, mock_fetch) -> None:
        mock_fetch.return_value = {
            "title": ["Retracted paper"],
            "DOI": "10.1234/bad",
            "published-print": {"date-parts": [[2019]]},
            "update-to": [
                {"DOI": "10.1234/bad-r1", "type": "retraction"},
            ],
        }
        cache: dict = {}
        f = _check_cited_doi("10.1234/bad", mailto=None, cache=cache)
        assert f is not None
        assert f.severity == "blocker"
        assert f.status == "retracted"
        assert f.notice_doi == "10.1234/bad-r1"
        assert f.cited_title == "Retracted paper"
        assert f.cited_year == 2019

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_returns_finding_for_eoc(self, mock_fetch) -> None:
        mock_fetch.return_value = {
            "title": ["Concerned paper"],
            "DOI": "10.1234/eoc",
            "update-to": [
                {"DOI": "10.1234/eoc-e1", "type": "expression_of_concern"},
            ],
        }
        f = _check_cited_doi("10.1234/eoc", mailto=None, cache={})
        assert f is not None
        assert f.severity == "review"

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_filters_corrigendum(self, mock_fetch) -> None:
        """Corrections are too noisy at depth=1; suppressed."""
        mock_fetch.return_value = {
            "title": ["Corrected paper"],
            "DOI": "10.1234/cor",
            "update-to": [
                {"DOI": "10.1234/cor-c1", "type": "corrigendum"},
            ],
        }
        assert _check_cited_doi("10.1234/cor", mailto=None, cache={}) is None

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_404_treated_as_clean(self, mock_fetch) -> None:
        """A 404 on a cited DOI shouldn't poison the parent check."""
        mock_fetch.return_value = None
        assert _check_cited_doi("10.1234/missing", mailto=None, cache={}) is None

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_transport_error_treated_as_clean(self, mock_fetch) -> None:
        mock_fetch.side_effect = RuntimeError("boom")
        assert _check_cited_doi("10.1234/down", mailto=None, cache={}) is None

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_cache_dedup(self, mock_fetch) -> None:
        """Second lookup of the same DOI shouldn't fetch again."""
        mock_fetch.return_value = {
            "title": ["X"],
            "DOI": "10.1234/x",
            "update-to": [
                {"DOI": "10.1234/x-r1", "type": "retraction"},
            ],
        }
        cache: dict = {}
        _check_cited_doi("10.1234/x", mailto=None, cache=cache)
        _check_cited_doi("10.1234/x", mailto=None, cache=cache)
        _check_cited_doi("10.1234/x", mailto=None, cache=cache)
        # One fetch total, despite three calls
        assert mock_fetch.call_count == 1


# ---------------------------------------------------------------------------
# check_doi end-to-end with transitive=True
# ---------------------------------------------------------------------------


class TestCheckDoiTransitive:
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_clean_paper_with_retracted_citation(self, mock_fetch) -> None:
        """Parent is clean but cites a retracted work — surfaces a finding."""

        def fake(doi, mailto):
            if doi == "10.1234/parent":
                return {
                    "title": ["Clean parent"],
                    "DOI": "10.1234/parent",
                    "reference": [{"DOI": "10.5678/bad"}, {"DOI": "10.5678/ok"}],
                }
            if doi == "10.5678/bad":
                return {
                    "title": ["Bad cited"],
                    "DOI": "10.5678/bad",
                    "update-to": [
                        {"DOI": "10.5678/bad-r1", "type": "retraction"},
                    ],
                }
            if doi == "10.5678/ok":
                return {"title": ["Ok cited"], "DOI": "10.5678/ok"}
            raise AssertionError(f"unexpected doi: {doi}")

        mock_fetch.side_effect = fake
        r = check_doi("10.1234/parent", transitive=True)
        assert r.status == "ok"
        assert r.overall_severity == "info"  # parent itself clean
        assert len(r.cited_findings) == 1
        assert r.cited_findings[0].cited_doi == "10.5678/bad"
        assert r.cited_findings[0].severity == "blocker"

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_transitive_off_skips_cite_walk(self, mock_fetch) -> None:
        mock_fetch.return_value = {
            "title": ["Parent"],
            "DOI": "10.1234/parent",
            "reference": [{"DOI": "10.5678/bad"}],
        }
        r = check_doi("10.1234/parent")  # transitive=False default
        # No cite-walk → no fetches for cited DOIs (mock would have raised
        # since fake side_effect not configured)
        assert r.cited_findings == []
        assert mock_fetch.call_count == 1


class TestCheckDoisTransitive:
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_dedup_across_parents(self, mock_fetch) -> None:
        """Two parents citing the same paper share a cache — one fetch."""

        def fake(doi, mailto):
            if doi in ("10.1234/p1", "10.1234/p2"):
                return {
                    "title": [f"Parent {doi}"],
                    "DOI": doi,
                    "reference": [{"DOI": "10.5678/shared-bad"}],
                }
            if doi == "10.5678/shared-bad":
                return {
                    "title": ["Shared bad"],
                    "DOI": "10.5678/shared-bad",
                    "update-to": [
                        {"DOI": "10.5678/shared-bad-r1", "type": "retraction"},
                    ],
                }
            raise AssertionError(f"unexpected: {doi}")

        mock_fetch.side_effect = fake
        results = check_dois(["10.1234/p1", "10.1234/p2"], transitive=True)
        # Each parent has the same cited finding
        assert len(results) == 2
        assert results[0].cited_findings[0].cited_doi == "10.5678/shared-bad"
        assert results[1].cited_findings[0].cited_doi == "10.5678/shared-bad"
        # Two parent fetches + one shared-cited fetch (not two)
        assert mock_fetch.call_count == 3


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def _result_with_cite_finding() -> ProvenanceResult:
    return ProvenanceResult(
        doi="10.1234/clean-citer",
        status="ok",
        paper_title="A clean paper that cites retracted work",
        cited_findings=[
            TransitiveCiteFinding(
                cited_doi="10.5678/bad",
                severity="blocker",
                status="retracted",
                cited_title="The cited retracted paper",
                cited_year=2018,
                notice_doi="10.5678/bad-r1",
                rw_reasons=["+Falsification/Fabrication of Data"],
            )
        ],
        input_index=1,
    )


class TestRenderCiteFindings:
    def test_default_view_promotes_to_review_bucket(self) -> None:
        out = render_batch([_result_with_cite_finding()], view="default")
        # A clean-itself paper with cite findings appears under Review
        assert "🟠 Review (1)" in out
        # The cited paper is shown
        assert "10.5678/bad" in out
        # The Reasons line appears
        assert "+Falsification/Fabrication of Data" in out

    def test_blockers_view_shows_clean_citer(self) -> None:
        """A clean paper that cites retracted work is visible in blockers."""
        out = render_batch([_result_with_cite_finding()], view="blockers")
        # Should NOT be hidden as "clean"
        assert "10.5678/bad" in out

    def test_json_view_includes_cited_findings(self) -> None:
        import json as _json

        out = render_batch([_result_with_cite_finding()], view="json")
        payload = _json.loads(out)
        first = payload["results"][0]
        assert "cited_findings" in first
        assert len(first["cited_findings"]) == 1
        assert first["cited_findings"][0]["cited_doi"] == "10.5678/bad"

    def test_no_cite_findings_no_section(self) -> None:
        r = ProvenanceResult(
            doi="10.1234/genuinely-clean",
            status="ok",
            paper_title="Nothing to see here",
            input_index=1,
        )
        out = render_batch([r], view="default")
        assert "Cites retracted" not in out
