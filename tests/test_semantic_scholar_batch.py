"""Tests for :func:`precis.ingest.semantic_scholar.get_papers_batch` +
the ``_normalize`` ``fields``/``citation_count`` extension.

No network: :func:`~precis.ingest.semantic_scholar._get_papers_with_retry`
is monkeypatched, matching the style of ``test_quest_search_acquire.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.ingest import semantic_scholar as s2mod


class _FakePaper:
    def __init__(
        self,
        *,
        paper_id: str,
        doi: str | None = None,
        arxiv: str | None = None,
        title: str = "a paper",
        s2_fields: list[dict[str, Any]] | None = None,
        legacy_fields: list[str] | None = None,
        citation_count: int | None = None,
    ) -> None:
        self.paperId = paper_id
        self.title = title
        self.authors: list[Any] = []
        self.year = 2024
        self.venue = ""
        self.abstract = ""
        external: dict[str, str] = {}
        if doi:
            external["DOI"] = doi
        if arxiv:
            external["ArXiv"] = arxiv
        self.externalIds = external
        self.s2FieldsOfStudy = s2_fields or []
        self.fieldsOfStudy = legacy_fields or []
        self.citationCount = citation_count


class TestGetPapersBatch:
    def test_empty_ids_returns_empty_list_no_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(sch: Any, ids: list[str]) -> Any:
            raise AssertionError("should not be called for an empty id list")

        monkeypatch.setattr(s2mod, "_get_papers_with_retry", _boom)
        assert s2mod.get_papers_batch([]) == []

    def test_resolves_in_order_with_none_for_unresolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paper_a = _FakePaper(paper_id="aaa", doi="10.1/a")
        paper_c = _FakePaper(paper_id="ccc", arxiv="2401.00001")

        def _fake(sch: Any, ids: list[str]) -> tuple[list[Any], list[str]]:
            assert ids == ["DOI:10.1/a", "bbb-not-found", "ARXIV:2401.00001"]
            return [paper_a, paper_c], ["bbb-not-found"]

        monkeypatch.setattr(s2mod, "_get_papers_with_retry", _fake)
        out = s2mod.get_papers_batch(
            ["DOI:10.1/a", "bbb-not-found", "ARXIV:2401.00001"]
        )
        assert len(out) == 3
        assert out[0] is not None and out[0]["doi"] == "10.1/a"
        assert out[1] is None
        assert out[2] is not None and out[2]["arxiv_id"] == "2401.00001"

    def test_matches_case_insensitively_by_bare_s2_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paper = _FakePaper(paper_id="AbC123")

        def _fake(sch: Any, ids: list[str]) -> tuple[list[Any], list[str]]:
            return [paper], []

        monkeypatch.setattr(s2mod, "_get_papers_with_retry", _fake)
        out = s2mod.get_papers_batch(["abc123"])
        assert out[0] is not None
        assert out[0]["s2_id"] == "AbC123"

    def test_chunks_at_500_ids_per_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _fake(sch: Any, ids: list[str]) -> tuple[list[Any], list[str]]:
            calls.append(list(ids))
            return [], list(ids)

        monkeypatch.setattr(s2mod, "_get_papers_with_retry", _fake)
        ids = [f"id{i}" for i in range(650)]
        out = s2mod.get_papers_batch(ids)
        assert len(out) == 650
        assert all(v is None for v in out)
        assert len(calls) == 2
        assert len(calls[0]) == 500
        assert len(calls[1]) == 150

    def test_whole_chunk_exception_degrades_every_id_in_it_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(sch: Any, ids: list[str]) -> Any:
            raise RuntimeError("network down")

        monkeypatch.setattr(s2mod, "_get_papers_with_retry", _boom)
        out = s2mod.get_papers_batch(["a", "b", "c"])
        assert out == [None, None, None]


class TestNormalizeFieldsAndCitationCount:
    def test_dedups_s2_and_legacy_fields_preserving_order(self) -> None:
        paper = _FakePaper(
            paper_id="x",
            s2_fields=[
                {"category": "Computer Science", "source": "s2-fos-model"},
                {"category": "Medicine", "source": "s2-fos-model"},
            ],
            legacy_fields=["Computer Science", "Biology"],
        )
        out = s2mod._normalize(paper)
        assert out["fields"] == ["Computer Science", "Medicine", "Biology"]

    def test_no_fields_returns_empty_list(self) -> None:
        paper = _FakePaper(paper_id="x")
        out = s2mod._normalize(paper)
        assert out["fields"] == []

    def test_citation_count_carried_through(self) -> None:
        paper = _FakePaper(paper_id="x", citation_count=42)
        out = s2mod._normalize(paper)
        assert out["citation_count"] == 42

    def test_missing_citation_count_is_none(self) -> None:
        paper = _FakePaper(paper_id="x")
        del paper.citationCount
        out = s2mod._normalize(paper)
        assert out["citation_count"] is None
