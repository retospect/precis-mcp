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


# ===========================================================================
# Regression suite — 2026-04-25 mcp-critic review (v3 B6)
# ===========================================================================


# ---------------------------------------------------------------------------
# /cites and /cited-by paginate at 20 by default
# ---------------------------------------------------------------------------


class TestS2CitesPagination:
    """``/cites`` and ``/cited-by`` cap responses at
    ``_S2_PAGE_DEFAULT`` (20) by default; ``/N`` paginates by offset;
    ``/all`` returns the unbounded form.

    Review 2026-04-25 mcp-critic finding B6 — heavily-cited papers
    used to dump every reference in one shot, blowing the agent's
    context window with 3k+ tokens of metadata and offering no
    pagination knob.
    """

    @staticmethod
    def _fake_papers(n: int) -> list[dict]:
        return [
            {"title": f"Paper {i}", "year": 2020, "doi": f"10.x/{i}", "s2_id": f"s{i}"}
            for i in range(n)
        ]

    def test_default_caps_at_twenty(self):
        h = PaperHandler()
        ref = {"slug": "big2024paper", "doi": "10.x/big"}
        with patch(
            "acatome_meta.citations.citations",
            return_value={"references": self._fake_papers(52)},
        ):
            out = h._read_s2_graph(ref, "references", limit=20, offset=0)
        # Showing-clause names the slice; trailer carries next-page.
        assert "1\u201320 of 52" in out  # en-dash separator
        assert "get(id='big2024paper/cites/20')" in out
        assert "get(id='big2024paper/cites/all')" in out

    def test_offset_jumps_to_page(self):
        h = PaperHandler()
        ref = {"slug": "big2024paper", "doi": "10.x/big"}
        with patch(
            "acatome_meta.citations.citations",
            return_value={"references": self._fake_papers(52)},
        ):
            out = h._read_s2_graph(ref, "references", limit=20, offset=20)
        assert "21\u201340 of 52" in out
        # Trailer points at the third page.
        assert "get(id='big2024paper/cites/40')" in out

    def test_all_returns_unbounded(self):
        h = PaperHandler()
        ref = {"slug": "big2024paper", "doi": "10.x/big"}
        with patch(
            "acatome_meta.citations.citations",
            return_value={"references": self._fake_papers(52)},
        ):
            out = h._read_s2_graph(ref, "references", limit=None, offset=0)
        # No pagination clause; trailer has no next-page link.
        assert "52 references" in out
        assert "/cites/20" not in out

    def test_offset_past_end_emits_recovery_hint(self):
        h = PaperHandler()
        ref = {"slug": "small2024paper", "doi": "10.x/s"}
        with patch(
            "acatome_meta.citations.citations",
            return_value={"references": self._fake_papers(5)},
        ):
            out = h._read_s2_graph(ref, "references", limit=20, offset=100)
        assert "past the end" in out
        assert "get(id='small2024paper/cites/0')" in out

    def test_subview_parser_routes_default(self):
        h = PaperHandler()
        ref = {"slug": "x"}
        limit, offset = h._parse_s2_pagination(None, ref, "cites")
        assert limit == 20
        assert offset == 0

    def test_subview_parser_routes_all(self):
        h = PaperHandler()
        ref = {"slug": "x"}
        limit, offset = h._parse_s2_pagination("all", ref, "cites")
        assert limit is None
        assert offset == 0

    def test_subview_parser_routes_offset(self):
        h = PaperHandler()
        ref = {"slug": "x"}
        limit, offset = h._parse_s2_pagination("40", ref, "cites")
        assert limit == 20
        assert offset == 40

    def test_subview_parser_rejects_garbage(self):
        from precis.protocol import ErrorCode, PrecisError

        h = PaperHandler()
        ref = {"slug": "x"}
        with pytest.raises(PrecisError) as excinfo:
            h._parse_s2_pagination("garbage", ref, "cites")
        assert excinfo.value.code is ErrorCode.PARAM_INVALID
