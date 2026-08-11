"""Tests for the Semantic Scholar batched citation fetch (``citations_batch``).

Mocks ``precis.ingest.citations.SemanticScholar`` so nothing hits the
network — mirrors the ``@patch("precis.ingest.crossref.Crossref")`` pattern
in ``test_crossref.py``. ``citations()`` (the existing per-paper path) is
untouched by these changes and isn't re-tested here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from precis.ingest.citations import citations_batch


def _paper(
    *,
    paper_id: str,
    doi: str | None = None,
    arxiv: str | None = None,
    references: list[Any] | None = None,
    citations: list[Any] | None = None,
    reference_count: int | None = None,
    citation_count: int | None = None,
) -> SimpleNamespace:
    """A fake S2 ``Paper`` (batch-returned) with just the attributes
    ``citations.py`` reads via ``getattr``."""
    ext: dict[str, str] = {}
    if doi:
        ext["DOI"] = doi
    if arxiv:
        ext["ArXiv"] = arxiv
    refs = references if references is not None else []
    cites = citations if citations is not None else []
    return SimpleNamespace(
        paperId=paper_id,
        externalIds=ext,
        references=refs,
        citations=cites,
        referenceCount=reference_count if reference_count is not None else len(refs),
        citationCount=citation_count if citation_count is not None else len(cites),
    )


def _neighbor(
    *, title: str, doi: str | None, year: int | None, s2_id: str
) -> SimpleNamespace:
    """A fake nested reference/citation object (batch or paginated
    ``.paper``) — same attribute surface ``_shape_neighbor`` reads."""
    return SimpleNamespace(
        title=title,
        externalIds={"DOI": doi} if doi else {},
        year=year,
        paperId=s2_id,
    )


class TestCitationsBatchHappyPath:
    @patch("precis.ingest.citations.SemanticScholar")
    def test_multiple_papers_keyed_by_input_id(self, mock_cls: MagicMock) -> None:
        mock_sch = MagicMock()
        mock_cls.return_value = mock_sch

        paper_a = _paper(
            paper_id="S2A",
            doi="10.1000/aaa",
            references=[_neighbor(title="Ref A1", doi="10.2/x", year=2001, s2_id="R1")],
            citations=[_neighbor(title="Cite A1", doi=None, year=2020, s2_id="C1")],
        )
        paper_b = _paper(
            paper_id="S2B",
            arxiv="1234.5678",
            references=[],
            citations=[_neighbor(title="Cite B1", doi="10.3/y", year=2022, s2_id="C2")],
        )
        mock_sch.get_papers.return_value = [paper_a, paper_b]

        result = citations_batch(
            ["doi:10.1000/aaa", "arxiv:1234.5678"], api_key="test-key"
        )

        assert set(result.keys()) == {"doi:10.1000/aaa", "arxiv:1234.5678"}
        assert result["doi:10.1000/aaa"]["references"] == [
            {"title": "Ref A1", "doi": "10.2/x", "year": 2001, "s2_id": "R1"}
        ]
        assert result["doi:10.1000/aaa"]["cited_by"] == [
            {"title": "Cite A1", "doi": None, "year": 2020, "s2_id": "C1"}
        ]
        assert result["arxiv:1234.5678"]["references"] == []
        assert result["arxiv:1234.5678"]["cited_by"] == [
            {"title": "Cite B1", "doi": "10.3/y", "year": 2022, "s2_id": "C2"}
        ]

        # Batch call, not two-per-paper.
        mock_sch.get_papers.assert_called_once()
        call_args = mock_sch.get_papers.call_args
        assert call_args.args[0] == ["doi:10.1000/aaa", "ARXIV:1234.5678"]


class TestCitationsBatchNotFound:
    @patch("precis.ingest.citations.SemanticScholar")
    def test_not_found_id_still_gets_empty_entry(self, mock_cls: MagicMock) -> None:
        """``get_papers`` drops not-found ids from its result list — the
        third input id must still surface with empty lists, proving we
        don't zip the (shorter) result list positionally against inputs."""
        mock_sch = MagicMock()
        mock_cls.return_value = mock_sch

        found_a = _paper(paper_id="S2A", doi="10.1/aaa")
        found_c = _paper(paper_id="S2C", doi="10.1/ccc")
        # Only 2 of 3 requested papers come back (S2 silently drops the
        # not-found id rather than returning a positional null).
        mock_sch.get_papers.return_value = [found_a, found_c]

        result = citations_batch(
            ["doi:10.1/aaa", "doi:10.1/bbb", "doi:10.1/ccc"], api_key="test-key"
        )

        assert set(result.keys()) == {"doi:10.1/aaa", "doi:10.1/bbb", "doi:10.1/ccc"}
        assert result["doi:10.1/bbb"] == {"references": [], "cited_by": []}
        # And the found ones are correctly matched, not accidentally empty.
        assert result["doi:10.1/aaa"] == {"references": [], "cited_by": []}
        assert result["doi:10.1/ccc"] == {"references": [], "cited_by": []}


class TestCitationsBatchChunking:
    @patch("precis.ingest.citations.SemanticScholar")
    def test_over_500_ids_split_into_multiple_batch_calls(
        self, mock_cls: MagicMock
    ) -> None:
        mock_sch = MagicMock()
        mock_cls.return_value = mock_sch
        mock_sch.get_papers.return_value = []

        paper_ids = [f"doi:10.1/{i}" for i in range(600)]
        result = citations_batch(paper_ids, api_key="test-key")

        assert mock_sch.get_papers.call_count == 2
        first_call_ids = mock_sch.get_papers.call_args_list[0].args[0]
        second_call_ids = mock_sch.get_papers.call_args_list[1].args[0]
        assert len(first_call_ids) == 500
        assert len(second_call_ids) == 100
        # Every input id still gets an entry.
        assert len(result) == 600
        assert all(v == {"references": [], "cited_by": []} for v in result.values())


class TestCitationsBatchTruncationFallback:
    @patch("precis.ingest.citations.SemanticScholar")
    def test_truncated_references_fall_back_to_paginated_endpoint(
        self, mock_cls: MagicMock
    ) -> None:
        mock_sch = MagicMock()
        mock_cls.return_value = mock_sch

        # Batch response claims referenceCount=3 but the nested array was
        # silently truncated to 1 entry — must trigger the fallback.
        truncated = _paper(
            paper_id="S2TRUNC",
            doi="10.1/trunc",
            references=[_neighbor(title="Only One", doi=None, year=2000, s2_id="R1")],
            reference_count=3,
            citations=[_neighbor(title="Full Cite", doi=None, year=2019, s2_id="C1")],
            citation_count=1,  # matches returned len -> no citations fallback
        )
        mock_sch.get_papers.return_value = [truncated]

        # The paginated fallback recovers the full reference set.
        full_ref_1 = SimpleNamespace(
            paper=_neighbor(title="Full Ref 1", doi=None, year=1999, s2_id="R1")
        )
        full_ref_2 = SimpleNamespace(
            paper=_neighbor(title="Full Ref 2", doi=None, year=1998, s2_id="R2")
        )
        full_ref_3 = SimpleNamespace(
            paper=_neighbor(title="Full Ref 3", doi=None, year=1997, s2_id="R3")
        )
        mock_sch.get_paper_references.return_value = [
            full_ref_1,
            full_ref_2,
            full_ref_3,
        ]

        result = citations_batch(["doi:10.1/trunc"], api_key="test-key")

        assert result["doi:10.1/trunc"]["references"] == [
            {"title": "Full Ref 1", "doi": None, "year": 1999, "s2_id": "R1"},
            {"title": "Full Ref 2", "doi": None, "year": 1998, "s2_id": "R2"},
            {"title": "Full Ref 3", "doi": None, "year": 1997, "s2_id": "R3"},
        ]
        # citationCount matched the returned length -> no citations fallback.
        mock_sch.get_paper_citations.assert_not_called()
        assert result["doi:10.1/trunc"]["cited_by"] == [
            {"title": "Full Cite", "doi": None, "year": 2019, "s2_id": "C1"}
        ]

        mock_sch.get_paper_references.assert_called_once()
        assert mock_sch.get_paper_references.call_args.args[0] == "S2TRUNC"
