"""Phase 5 tests: paper view='health', view='exists', candidate hints.

Coverage:
- ``candidate_search`` against mocked Crossref ``/works`` query
- ``check_doi(suggest_candidates=True)`` populates ``result.candidates``
  on 404 and ONLY on 404 — never substitutes
- ``view='exists'`` renderer (tick/cross format)
- ``render_single`` surfaces candidates in the unknown-DOI section
- ``render_batch`` surfaces candidates indented under each unknown DOI

Paper-handler ``view='health'`` integration tests need a live store and
land under the ``db`` marker in a follow-up.
"""

from __future__ import annotations

from unittest.mock import patch

from precis.handlers._provenance_report import (
    render_batch,
    render_exists,
    render_single,
)
from precis.ingest.provenance import (
    BibEntry,
    CandidateMatch,
    ProvenanceResult,
    candidate_search,
    check_doi,
)


# ---------------------------------------------------------------------------
# candidate_search
# ---------------------------------------------------------------------------


class TestCandidateSearch:
    @patch("precis.ingest.provenance._search_crossref_works")
    def test_basic_search(self, mock_search) -> None:
        mock_search.return_value = [
            {
                "DOI": "10.5678/foo",
                "score": 94.5,
                "title": ["Quantum widgets in surface codes"],
                "author": [{"family": "Smith", "given": "Alice"}],
                "issued": {"date-parts": [[2019]]},
            },
            {
                "DOI": "10.5678/bar",
                "score": 81.2,
                "title": ["Quantum widgets"],
                "author": [{"family": "Jones"}],
                "issued": {"date-parts": [[2020]]},
            },
        ]
        bib = BibEntry(
            doi="10.x/typo",
            title="Quantum widgets in surface codes",
            authors=["Smith"],
            year=2019,
        )
        out = candidate_search(bib_entry=bib, mailto=None)
        assert len(out) == 2
        assert out[0].doi == "10.5678/foo"
        assert out[0].score == 94.5
        assert out[0].first_author == "Smith"
        assert out[0].year == 2019
        assert out[1].doi == "10.5678/bar"

    @patch("precis.ingest.provenance._search_crossref_works")
    def test_empty_when_no_title(self, mock_search) -> None:
        """Without a title we don't bother — author-only queries are too broad."""
        bib = BibEntry(doi="10.x/typo", authors=["Smith"])
        out = candidate_search(bib_entry=bib, mailto=None)
        assert out == []
        mock_search.assert_not_called()

    @patch("precis.ingest.provenance._search_crossref_works")
    def test_skips_items_without_doi(self, mock_search) -> None:
        mock_search.return_value = [
            {"DOI": "10.5678/foo", "title": ["With DOI"]},
            {"title": ["Without DOI"]},  # malformed item
            {"DOI": "10.5678/bar", "title": ["With DOI"]},
        ]
        bib = BibEntry(doi="10.x/typo", title="anything")
        out = candidate_search(bib_entry=bib, mailto=None)
        assert [c.doi for c in out] == ["10.5678/foo", "10.5678/bar"]


# ---------------------------------------------------------------------------
# check_doi with suggest_candidates
# ---------------------------------------------------------------------------


class TestCheckDoiCandidates:
    @patch("precis.ingest.provenance._search_crossref_works")
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_404_with_hints_emits_candidates(
        self, mock_fetch, mock_search
    ) -> None:
        mock_fetch.return_value = None  # DOI 404s
        mock_search.return_value = [
            {
                "DOI": "10.5678/correct",
                "score": 95.0,
                "title": ["The real title"],
                "author": [{"family": "Smith"}],
            }
        ]
        bib = BibEntry(
            doi="10.1234/typo",
            title="The real title",
            authors=["Smith"],
        )
        r = check_doi(
            "10.1234/typo",
            bib_entry=bib,
            suggest_candidates=True,
        )
        # Status MUST stay unknown — we never substitute
        assert r.status == "unknown"
        assert r.doi == "10.1234/typo"
        # Candidates surfaced as advisory
        assert len(r.candidates) == 1
        assert r.candidates[0].doi == "10.5678/correct"

    @patch("precis.ingest.provenance._search_crossref_works")
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_404_no_hints_no_candidates(
        self, mock_fetch, mock_search
    ) -> None:
        """No BibEntry → no candidate search even with flag set."""
        mock_fetch.return_value = None
        r = check_doi(
            "10.1234/typo",
            suggest_candidates=True,
        )
        assert r.status == "unknown"
        assert r.candidates == []
        mock_search.assert_not_called()

    @patch("precis.ingest.provenance._search_crossref_works")
    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_404_flag_off_no_candidates(
        self, mock_fetch, mock_search
    ) -> None:
        """Default behaviour: no candidate search."""
        mock_fetch.return_value = None
        bib = BibEntry(doi="10.1234/typo", title="anything")
        r = check_doi("10.1234/typo", bib_entry=bib)  # flag default False
        assert r.candidates == []
        mock_search.assert_not_called()

    @patch("precis.ingest.provenance._fetch_crossref_message")
    def test_resolved_doi_no_candidates_even_with_flag(
        self, mock_fetch
    ) -> None:
        """If the DOI resolves, candidate search never runs."""
        mock_fetch.return_value = {
            "title": ["Found it"],
            "DOI": "10.1234/foo",
        }
        bib = BibEntry(doi="10.1234/foo", title="anything")
        r = check_doi("10.1234/foo", bib_entry=bib, suggest_candidates=True)
        assert r.status == "ok"
        assert r.candidates == []


# ---------------------------------------------------------------------------
# Renderer surfaces candidates
# ---------------------------------------------------------------------------


def _result_with_candidates() -> ProvenanceResult:
    return ProvenanceResult(
        doi="10.1234/typo",
        status="unknown",
        candidates=[
            CandidateMatch(
                doi="10.5678/correct",
                score=94.5,
                title="The real paper",
                first_author="Smith",
                year=2019,
            ),
            CandidateMatch(
                doi="10.5678/other",
                score=81.0,
                title="A similar-sounding paper",
                first_author="Jones",
                year=2020,
            ),
        ],
        input_index=1,
    )


class TestRenderCandidates:
    def test_render_single_shows_candidates(self) -> None:
        out = render_single(_result_with_candidates())
        assert "Status**: unknown" in out
        assert "advisory only" in out
        assert "10.5678/correct" in out
        assert "10.5678/other" in out
        assert "score 94.5" in out

    def test_render_batch_default_shows_candidates(self) -> None:
        out = render_batch([_result_with_candidates()], view="default")
        # The unknown DOI section appears
        assert "Unknown DOI (Crossref 404)" in out
        # The candidate hints are nested beneath
        assert "advisory only" in out
        assert "10.5678/correct" in out

    def test_render_batch_json_includes_candidates(self) -> None:
        import json as _json

        out = render_batch([_result_with_candidates()], view="json")
        payload = _json.loads(out)
        cands = payload["results"][0]["candidates"]
        assert len(cands) == 2
        assert cands[0]["doi"] == "10.5678/correct"
        assert cands[0]["score"] == 94.5

    def test_no_candidates_no_advisory_text(self) -> None:
        """When candidates list is empty, the 'advisory only' line is absent."""
        r = ProvenanceResult(doi="10.x/typo", status="unknown", input_index=1)
        out = render_batch([r], view="default")
        assert "Unknown DOI" in out
        assert "advisory only" not in out


# ---------------------------------------------------------------------------
# view='exists' renderer
# ---------------------------------------------------------------------------


class TestRenderExists:
    def test_mixed_batch(self) -> None:
        results = [
            ProvenanceResult(
                doi="10.1234/ok",
                status="ok",
                paper_title="Resolves fine",
                input_index=1,
            ),
            ProvenanceResult(
                doi="10.1234/missing",
                status="unknown",
                input_index=2,
            ),
            ProvenanceResult(
                doi="not-a-doi",
                status="malformed",
                input_index=3,
            ),
            ProvenanceResult(
                doi="10.1234/down",
                status="check_failed",
                error="connection reset",
                input_index=4,
            ),
        ]
        out = render_exists(results)
        assert "1/4 resolve" in out
        assert "✓" in out
        assert "✗" in out
        assert "⚠️" in out
        # Index prefixes present
        assert "**#1**" in out
        assert "**#2**" in out

    def test_all_clean(self) -> None:
        results = [
            ProvenanceResult(
                doi="10.1234/a",
                status="ok",
                paper_title="A",
                input_index=1,
            ),
            ProvenanceResult(
                doi="10.1234/b",
                status="ok",
                paper_title="B",
                input_index=2,
            ),
        ]
        out = render_exists(results)
        assert "2/2 resolve" in out
        assert "10.1234/a" in out
        assert "10.1234/b" in out

    def test_empty(self) -> None:
        out = render_exists([])
        assert "No DOIs" in out
