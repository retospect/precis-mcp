"""Tests for Semantic Scholar citation graph views (/cites, /cited-by)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from precis.handlers.paper import PaperHandler


@pytest.fixture
def handler():
    return PaperHandler()


# ── _get_s2_identifier ────────────────────────────────────────────────


class TestGetS2Identifier:
    def test_doi_preferred(self):
        ref = {"doi": "10.1234/foo", "s2_id": "abc123", "arxiv_id": "2301.00001"}
        assert PaperHandler._get_s2_identifier(ref) == "DOI:10.1234/foo"

    def test_s2_id_fallback(self):
        ref = {"s2_id": "abc123", "arxiv_id": "2301.00001"}
        assert PaperHandler._get_s2_identifier(ref) == "abc123"

    def test_arxiv_fallback(self):
        ref = {"arxiv_id": "2301.00001"}
        assert PaperHandler._get_s2_identifier(ref) == "ARXIV:2301.00001"

    def test_none_when_no_ids(self):
        ref = {"slug": "smith2020foo", "title": "Some Paper"}
        assert PaperHandler._get_s2_identifier(ref) is None

    def test_empty_doi_skipped(self):
        ref = {"doi": "", "s2_id": "abc123"}
        assert PaperHandler._get_s2_identifier(ref) == "abc123"

    def test_none_doi_skipped(self):
        ref = {"doi": None, "s2_id": "abc123"}
        assert PaperHandler._get_s2_identifier(ref) == "abc123"


# ── _read_s2_graph ───────────────────────────────────────────────────


MOCK_CITATIONS_RESULT = {
    "references": [
        {
            "title": "First ref paper",
            "doi": "10.1000/ref1",
            "year": 2019,
            "s2_id": "r1",
        },
        {"title": "Second ref paper", "doi": None, "year": 2020, "s2_id": "r2"},
    ],
    "cited_by": [
        {
            "title": "A citing paper",
            "doi": "10.1000/cite1",
            "year": 2023,
            "s2_id": "c1",
        },
    ],
}


class TestReadS2Graph:
    def test_no_identifier(self, handler):
        ref = {"slug": "noident2020", "title": "No IDs"}
        result = handler._read_s2_graph(ref, direction="references")
        assert "cannot query Semantic Scholar" in result
        assert "get(id='noident2020/meta')" in result

    @patch("precis.handlers.paper.citations", create=True)
    def test_cites_with_results(self, mock_citations, handler):
        with patch(
            "acatome_meta.citations.citations", return_value=MOCK_CITATIONS_RESULT
        ):
            ref = {"slug": "smith2020foo", "doi": "10.1234/foo"}
            result = handler._read_s2_graph(ref, direction="references")
            assert "2 references" in result
            assert "First ref paper" in result
            assert "Second ref paper" in result
            assert "doi:10.1000/ref1" in result
            assert "s2:r2" in result  # no DOI, falls back to s2_id
            assert "cited-by" in result  # hint for other direction

    @patch("precis.handlers.paper.citations", create=True)
    def test_cited_by_with_results(self, mock_citations, handler):
        with patch(
            "acatome_meta.citations.citations", return_value=MOCK_CITATIONS_RESULT
        ):
            ref = {"slug": "smith2020foo", "doi": "10.1234/foo"}
            result = handler._read_s2_graph(ref, direction="cited_by")
            assert "1 citing papers" in result
            assert "A citing paper" in result
            assert "cites" in result  # hint for other direction

    @patch("precis.handlers.paper.citations", create=True)
    def test_empty_references(self, mock_citations, handler):
        with patch(
            "acatome_meta.citations.citations",
            return_value={"references": [], "cited_by": []},
        ):
            ref = {"slug": "smith2020foo", "doi": "10.1234/foo"}
            result = handler._read_s2_graph(ref, direction="references")
            assert "0 references" in result
            assert "cited-by" in result  # suggests other direction

    @patch("precis.handlers.paper.citations", create=True)
    def test_s2_api_error(self, mock_citations, handler):
        with patch(
            "acatome_meta.citations.citations",
            side_effect=RuntimeError("S2 rate limit"),
        ):
            ref = {"slug": "smith2020foo", "doi": "10.1234/foo"}
            result = handler._read_s2_graph(ref, direction="references")
            assert "lookup failed" in result
            assert "S2 rate limit" in result

    @patch("precis.handlers.paper.citations", create=True)
    def test_uses_doi_prefix(self, mock_citations, handler):
        with patch(
            "acatome_meta.citations.citations",
            return_value={"references": [], "cited_by": []},
        ) as mock_fn:
            ref = {"slug": "smith2020foo", "doi": "10.1234/foo"}
            handler._read_s2_graph(ref, direction="references")
            mock_fn.assert_called_once_with("DOI:10.1234/foo")

    @patch("precis.handlers.paper.citations", create=True)
    def test_uses_arxiv_prefix(self, mock_citations, handler):
        with patch(
            "acatome_meta.citations.citations",
            return_value={"references": [], "cited_by": []},
        ) as mock_fn:
            ref = {"slug": "smith2020foo", "arxiv_id": "2301.00001"}
            handler._read_s2_graph(ref, direction="references")
            mock_fn.assert_called_once_with("ARXIV:2301.00001")


# ── views set ────────────────────────────────────────────────────────


class TestViewsRegistered:
    def test_cites_in_views(self):
        assert "cites" in PaperHandler.views

    def test_cited_by_in_views(self):
        assert "cited-by" in PaperHandler.views
