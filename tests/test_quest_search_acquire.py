"""Tests for the ACQUIRING quest lit-search (``precis.quest.search``).

Covers the two new primitives:

* :func:`precis.ingest.semantic_scholar.search_s2_papers` — a multi-result S2
  free-text search, degrading to ``[]`` on any error.
* :func:`precis.quest.search.make_acquiring_search` — a ``search_fn`` that
  layers S2 + ``PaperHandler.acquire`` on top of the held-corpus lexical
  default, swallowing per-candidate acquire failures.

No DB is touched: ``search_refs_lexical`` and ``PaperHandler.acquire`` are
both stubbed, matching the style of ``tests/test_quest_tick_job.py``.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from precis.ingest import semantic_scholar as s2mod
from precis.quest import search as qsearch

# ── search_s2_papers ─────────────────────────────────────────────────


class _FakeItems:
    def __init__(self, items: list[Any]) -> None:
        self.items = items


class _FakePaper:
    def __init__(self, doi: str | None, title: str = "a paper") -> None:
        self.title = title
        self.authors: list[Any] = []
        self.year = 2024
        self.externalIds = {"DOI": doi} if doi else {}
        self.paperId = "abc123"
        self.venue = ""
        self.abstract = ""


class TestSearchS2Papers:
    def test_returns_list_of_dicts_with_doi(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_results = _FakeItems(
            [_FakePaper("10.1/one"), _FakePaper("10.1/two"), _FakePaper(None)]
        )
        monkeypatch.setattr(
            s2mod, "_search_with_retry", lambda sch, q, limit: fake_results
        )
        out = s2mod.search_s2_papers("query text", limit=3)
        assert len(out) == 3
        assert out[0]["doi"] == "10.1/one"
        assert out[1]["doi"] == "10.1/two"
        assert out[2]["doi"] is None

    def test_respects_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_results = _FakeItems([_FakePaper(f"10.1/{i}") for i in range(5)])
        monkeypatch.setattr(
            s2mod, "_search_with_retry", lambda sch, q, limit: fake_results
        )
        out = s2mod.search_s2_papers("query text", limit=2)
        assert len(out) == 2

    def test_empty_results_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            s2mod, "_search_with_retry", lambda sch, q, limit: _FakeItems([])
        )
        assert s2mod.search_s2_papers("query text") == []

    def test_none_results_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(s2mod, "_search_with_retry", lambda sch, q, limit: None)
        assert s2mod.search_s2_papers("query text") == []

    def test_exception_degrades_to_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(sch: Any, q: str, limit: int) -> Any:
            raise RuntimeError("network down")

        monkeypatch.setattr(s2mod, "_search_with_retry", _boom)
        assert s2mod.search_s2_papers("query text") == []


# ── make_acquiring_search ────────────────────────────────────────────


class _Row:
    def __init__(self, id_: int) -> None:
        self.id = id_


class FakeStore:
    """Minimal store stub: only ``search_refs_lexical`` is exercised."""

    def __init__(self, held_ids: list[int]) -> None:
        self._held_ids = held_ids

    def search_refs_lexical(
        self, *, q: str, kind: str, limit: int
    ) -> list[tuple[_Row, float]]:
        return [(_Row(i), 1.0) for i in self._held_ids]


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body


def _fake_hub() -> Any:
    # PaperHandler.__init__ only touches hub.store / hub.embedder.
    return SimpleNamespace(store=object(), embedder=None)


class TestMakeAcquiringSearch:
    def test_returns_callable_with_search_fn_arity(self) -> None:
        fn = qsearch.make_acquiring_search(1, _fake_hub())
        assert callable(fn)
        params = list(inspect.signature(fn).parameters)
        assert params == ["store", "query", "exclude_ref_ids"]

    def test_held_plus_acquired_deduped_and_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            s2mod,
            "search_s2_papers",
            lambda query, limit: [
                {"doi": "10.1/aaa", "title": "paper a"},
                {"doi": "10.1/bbb", "title": "paper b"},
            ],
        )

        calls: list[dict[str, Any]] = []

        def _fake_acquire(self: Any, **kw: Any) -> _FakeResponse:
            calls.append(kw)
            n = 901 if kw["identifier"] == "doi:10.1/aaa" else 902
            return _FakeResponse(body=f"acquire: minted stub paper id={n} (…)")

        monkeypatch.setattr("precis.handlers.paper.PaperHandler.acquire", _fake_acquire)

        fn = qsearch.make_acquiring_search(164903, _fake_hub())
        store: Any = FakeStore(held_ids=[10, 11])
        out = fn(store, "NO NH3 Pd catalyst", [])

        assert out == [10, 11, 901, 902]
        assert len(calls) == 2
        assert calls[0]["context_ref_id"] == 164903
        assert "quest lit-search" in calls[0]["reason"]

    def test_exclude_ref_ids_removed_from_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            s2mod,
            "search_s2_papers",
            lambda query, limit: [{"doi": "10.1/aaa", "title": "paper a"}],
        )
        monkeypatch.setattr(
            "precis.handlers.paper.PaperHandler.acquire",
            lambda self, **kw: _FakeResponse(body="acquire: minted stub paper id=901"),
        )

        fn = qsearch.make_acquiring_search(1, _fake_hub())
        store: Any = FakeStore(held_ids=[10, 11])
        out = fn(store, "q", [11, 901])

        assert out == [10]

    def test_acquire_exception_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            s2mod,
            "search_s2_papers",
            lambda query, limit: [
                {"doi": "10.1/bad", "title": "bad paper"},
                {"doi": "10.1/good", "title": "good paper"},
            ],
        )

        def _fake_acquire(self: Any, **kw: Any) -> _FakeResponse:
            if kw["identifier"] == "doi:10.1/bad":
                raise RuntimeError("resolver timed out")
            return _FakeResponse(body="acquire: minted stub paper id=902")

        monkeypatch.setattr("precis.handlers.paper.PaperHandler.acquire", _fake_acquire)

        fn = qsearch.make_acquiring_search(1, _fake_hub())
        store: Any = FakeStore(held_ids=[10])
        out = fn(store, "q", [])

        assert out == [10, 902]

    def test_s2_search_exception_still_returns_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(query: str, limit: int) -> Any:
            raise RuntimeError("s2 down")

        monkeypatch.setattr(s2mod, "search_s2_papers", _boom)

        fn = qsearch.make_acquiring_search(1, _fake_hub())
        store: Any = FakeStore(held_ids=[10, 11])
        out = fn(store, "q", [])

        assert out == [10, 11]

    def test_no_doi_candidates_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            s2mod,
            "search_s2_papers",
            lambda query, limit: [{"doi": None, "title": "no doi"}],
        )
        calls: list[dict[str, Any]] = []

        def _rec_acquire(self: Any, **kw: Any) -> _FakeResponse:
            calls.append(kw)
            return _FakeResponse(body="id=999")

        monkeypatch.setattr("precis.handlers.paper.PaperHandler.acquire", _rec_acquire)

        fn = qsearch.make_acquiring_search(1, _fake_hub())
        store: Any = FakeStore(held_ids=[])
        out = fn(store, "q", [])

        assert out == []
        assert calls == []
