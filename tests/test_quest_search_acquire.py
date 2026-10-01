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
from tests._fakes import FakeStore as _FakeStoreBase


@pytest.fixture(autouse=True)
def _clear_s2_cache() -> None:
    s2mod._cache.clear()


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
            s2mod, "_search_fast_fail", lambda sch, q, limit: fake_results
        )
        out = s2mod.search_s2_papers("query text", limit=3)
        assert len(out) == 3
        assert out[0]["doi"] == "10.1/one"
        assert out[1]["doi"] == "10.1/two"
        assert out[2]["doi"] is None

    def test_respects_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_results = _FakeItems([_FakePaper(f"10.1/{i}") for i in range(5)])
        monkeypatch.setattr(
            s2mod, "_search_fast_fail", lambda sch, q, limit: fake_results
        )
        out = s2mod.search_s2_papers("query text", limit=2)
        assert len(out) == 2

    def test_empty_results_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            s2mod, "_search_fast_fail", lambda sch, q, limit: _FakeItems([])
        )
        assert s2mod.search_s2_papers("query text") == []

    def test_none_results_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(s2mod, "_search_fast_fail", lambda sch, q, limit: None)
        assert s2mod.search_s2_papers("query text") == []

    def test_exception_degrades_to_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(sch: Any, q: str, limit: int) -> Any:
            raise RuntimeError("network down")

        monkeypatch.setattr(s2mod, "_search_fast_fail", _boom)
        assert s2mod.search_s2_papers("query text") == []


# ── make_acquiring_search ────────────────────────────────────────────


class _Row:
    def __init__(self, id_: int, kind: str = "paper") -> None:
        self.id = id_
        self.kind = kind


class FakeStore(_FakeStoreBase):
    """Minimal store stub: the local leg's ``search_chunks_across_kinds`` and
    the quest-draft exclusion's ``links_for`` are what is exercised.

    ``held`` is a list of ref ids (papers) or ``(id, kind)`` pairs."""

    def __init__(self, held_ids: list[Any]) -> None:
        super().__init__()
        self._held = [h if isinstance(h, tuple) else (h, "paper") for h in held_ids]
        self.local_calls: list[dict[str, Any]] = []

    def search_chunks_across_kinds(self, **kw: Any) -> list[tuple[Any, _Row, float]]:
        self.local_calls.append(kw)
        return [(None, _Row(i, k), 1.0) for i, k in self._held]

    def links_for(self, *a: Any, **kw: Any) -> list[Any]:
        return []


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

        # held-corpus hits carry their ts_rank_cd score; S2-acquired
        # candidates carry score=None (no relevance signal of their own —
        # see make_acquiring_search's docstring).
        assert out == [(10, 1.0), (11, 1.0), (901, None), (902, None)]
        assert len(calls) == 2
        # No context_ref_id: acquire() must not link related-to->quest here
        # unconditionally (quest-tick-incident-fix.md item 5) — only
        # run_search_step's floor+MAX_LINK_PER_QUERY-bounded serves link may
        # associate an acquired paper with the quest.
        assert "context_ref_id" not in calls[0]
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

        assert out == [(10, 1.0)]

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

        assert out == [(10, 1.0), (902, None)]

    def test_s2_search_exception_still_returns_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(query: str, limit: int) -> Any:
            raise RuntimeError("s2 down")

        monkeypatch.setattr(s2mod, "search_s2_papers", _boom)

        fn = qsearch.make_acquiring_search(1, _fake_hub())
        store: Any = FakeStore(held_ids=[10, 11])
        out = fn(store, "q", [])

        assert out == [(10, 1.0), (11, 1.0)]

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


# ── HyDE searches payload (dossier-hygiene design) ──────────────────────


class TestParseSearchEntry:
    """:func:`precis.quest.search._parse_search_entry` — accepts either the
    legacy plain query string, or ``{"query": ..., "hypothetical": ...}``."""

    def test_plain_string_shape(self) -> None:
        entry = qsearch._parse_search_entry("  photocatalytic nitrate reduction  ")
        assert entry == qsearch.SearchQuery(
            query="photocatalytic nitrate reduction", hypothetical=None
        )

    def test_dict_shape_with_hypothetical(self) -> None:
        entry = qsearch._parse_search_entry(
            {
                "query": "rate limiting step",
                "hypothetical": "The rate-limiting step is proton transfer.",
            }
        )
        assert entry == qsearch.SearchQuery(
            query="rate limiting step",
            hypothetical="The rate-limiting step is proton transfer.",
        )

    def test_dict_shape_without_hypothetical(self) -> None:
        entry = qsearch._parse_search_entry({"query": "a query"})
        assert entry == qsearch.SearchQuery(query="a query", hypothetical=None)

    def test_blank_query_in_dict_is_none(self) -> None:
        assert qsearch._parse_search_entry({"query": "   "}) is None
        assert qsearch._parse_search_entry({"hypothetical": "no query given"}) is None

    def test_blank_string_is_none(self) -> None:
        assert qsearch._parse_search_entry("   ") is None
        assert qsearch._parse_search_entry(None) is None

    def test_blank_hypothetical_normalizes_to_none(self) -> None:
        entry = qsearch._parse_search_entry({"query": "q", "hypothetical": "   "})
        assert entry is not None
        assert entry.hypothetical is None


class TestHydeCorpusHits:
    """:func:`precis.quest.search._hyde_corpus_hits` — the corpus leg that
    fuses ``query``/``hypothetical`` via
    :class:`precis.handlers._paper_search.FusedBlockSearch` instead of
    plain lexical (dossier-hygiene design). The fusion call itself is
    mocked — this is a routing test, not an integration test of broad
    retrieval."""

    def test_routes_query_and_hypothetical_as_queries_and_answers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.handlers._paper_search import FusedBlockSearch

        calls: list[dict[str, Any]] = []

        def _fake_run(self: Any, **kw: Any) -> Any:
            calls.append(kw)
            return SimpleNamespace(
                hits=[
                    (None, SimpleNamespace(id=42), 1.0),
                    (None, SimpleNamespace(id=7), 0.5),
                ]
            )

        monkeypatch.setattr(FusedBlockSearch, "run", _fake_run)

        store: Any = object()
        out = qsearch._hyde_corpus_hits(
            store,
            None,
            1,
            "rate limiting step",
            "The rate-limiting step is proton transfer to the surface oxygen.",
            [],
        )

        assert out == [(42, 1.0), (7, 0.5)]
        assert len(calls) == 1
        assert calls[0]["queries"] == ["rate limiting step"]
        assert calls[0]["answers"] == [
            "The rate-limiting step is proton transfer to the surface oxygen."
        ]
        assert calls[0]["q"] == "rate limiting step"

    def test_excludes_and_dedups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from precis.handlers._paper_search import FusedBlockSearch

        def _fake_run(self: Any, **kw: Any) -> Any:
            return SimpleNamespace(
                hits=[
                    (None, SimpleNamespace(id=1), 1.0),
                    (None, SimpleNamespace(id=2), 0.9),
                    (None, SimpleNamespace(id=1), 0.8),  # dup ref, e.g. 2 blocks
                ]
            )

        monkeypatch.setattr(FusedBlockSearch, "run", _fake_run)

        store: Any = object()
        out = qsearch._hyde_corpus_hits(
            store, None, 1, "q", "a hypothetical passage", [2]
        )
        # 2 excluded, 1 deduped to a single entry (first-seen score kept)
        assert out == [(1, 1.0)]

    def test_degrades_to_empty_on_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.handlers._paper_search import FusedBlockSearch

        def _boom(self: Any, **kw: Any) -> Any:
            raise RuntimeError("fusion blew up")

        monkeypatch.setattr(FusedBlockSearch, "run", _boom)

        store: Any = object()
        out = qsearch._hyde_corpus_hits(store, None, 1, "q", "a hypothetical", [])
        assert out == []


# ── local-first (quest-tick-local-first-search) ──────────────────────────


class _S2Stub:
    """Records S2 calls; optionally raises."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.raises = raises

    def __call__(self, query: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append(query)
        if self.raises is not None:
            raise self.raises
        return [{"doi": "10.1/new", "title": "new"}]


def _stub_acquire(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _acq(self: Any, **kw: Any) -> _FakeResponse:
        calls.append(kw)
        return _FakeResponse(body="acquire: minted stub paper id=901")

    monkeypatch.setattr("precis.handlers.paper.PaperHandler.acquire", _acq)
    return calls


class TestLocalFirst:
    def test_answered_by_held_finding_acquires_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s2 = _S2Stub()
        monkeypatch.setattr(s2mod, "search_s2_papers", s2)
        acquires = _stub_acquire(monkeypatch)

        fn = qsearch.make_acquiring_search(1, _fake_hub())
        store: Any = FakeStore(
            held_ids=[(50, "finding"), (51, "finding"), (52, "paper")]
        )
        out = fn(store, "claim provenance", [])

        assert [rid for rid, _ in out] == [50, 51, 52]
        assert s2.calls == []  # zero S2 calls
        assert acquires == []
        rep = fn.report_for("claim provenance")
        assert rep is not None
        assert (rep.local, rep.acquired, rep.external_ran) == (3, 0, False)
        # the hybrid cross-kind primitive over all five kinds
        assert store.local_calls[0]["kinds"] == list(qsearch.LOCAL_KINDS)

    def test_no_local_hit_still_acquires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s2 = _S2Stub()
        monkeypatch.setattr(s2mod, "search_s2_papers", s2)
        _stub_acquire(monkeypatch)

        fn = qsearch.make_acquiring_search(1, _fake_hub())
        store: Any = FakeStore(held_ids=[])
        out = fn(store, "novel topic", [])

        assert out == [(901, None)]
        assert s2.calls == ["novel topic"]
        rep = fn.report_for("novel topic")
        assert rep is not None
        assert (rep.local, rep.acquired, rep.external_ran) == (0, 1, True)

    def test_s2_raising_keeps_local_hits_and_records_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s2 = _S2Stub(raises=RuntimeError("429 rate limited"))
        monkeypatch.setattr(s2mod, "search_s2_papers", s2)
        _stub_acquire(monkeypatch)

        fn = qsearch.make_acquiring_search(1, _fake_hub())
        store: Any = FakeStore(held_ids=[(50, "finding")])
        out = fn(store, "q", [])

        assert out == [(50, 1.0)]
        rep = fn.report_for("q")
        assert rep is not None
        assert rep.external_error is not None and "429" in rep.external_error

    def test_local_search_failure_degrades_to_external(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(s2mod, "search_s2_papers", _S2Stub())
        _stub_acquire(monkeypatch)

        class _Boom(FakeStore):
            def search_chunks_across_kinds(self, **kw: Any) -> Any:
                raise RuntimeError("db down")

        fn = qsearch.make_acquiring_search(1, _fake_hub())
        boom: Any = _Boom(held_ids=[])
        out = fn(boom, "q", [])
        assert out == [(901, None)]

    def test_already_linked_hits_count_toward_enough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s2 = _S2Stub()
        monkeypatch.setattr(s2mod, "search_s2_papers", s2)
        fn = qsearch.make_acquiring_search(1, _fake_hub())
        store: Any = FakeStore(held_ids=[10, 11, 12])
        out = fn(store, "q", [10, 11])
        assert [rid for rid, _ in out] == [12]
        assert s2.calls == []  # graph answered even though two are already linked

    def test_floor_applies_to_enough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s2 = _S2Stub()
        monkeypatch.setattr(s2mod, "search_s2_papers", s2)
        _stub_acquire(monkeypatch)
        monkeypatch.setenv("PRECIS_QUEST_SEARCH_FLOOR", "5.0")
        fn = qsearch.make_acquiring_search(1, _fake_hub())
        store: Any = FakeStore(held_ids=[10, 11, 12])  # all score 1.0 < floor
        fn(store, "q", [])
        assert s2.calls == ["q"]


class _StepStore(FakeStore):
    """Adds what ``run_search_step`` touches beyond the search itself."""

    def __init__(self, held_ids: list[Any]) -> None:
        super().__init__(held_ids)
        self.links: list[dict[str, Any]] = []

    def add_link(self, **kw: Any) -> None:
        self.links.append(kw)

    def add_tag(self, *a: Any, **kw: Any) -> None:
        return None


class TestRunSearchStepLocalFirst:
    @pytest.fixture(autouse=True)
    def _plumbing(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        monkeypatch.setattr(qsearch, "_live_servers", lambda store, qid: [])
        monkeypatch.setattr(qsearch, "quest_tag_value", lambda qid, store: f"qu{qid}")
        monkeypatch.setattr(
            qsearch, "append_entry", lambda store, qid, **kw: entries.append(kw)
        )
        self.entries = entries
        return entries

    def test_logbook_names_local_hits_and_acquisitions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(s2mod, "search_s2_papers", _S2Stub())
        _stub_acquire(monkeypatch)
        fn = qsearch.make_acquiring_search(7, _fake_hub())
        store: Any = _StepStore(held_ids=[(50, "finding")])
        step = qsearch.run_search_step(store, 7, ["thin query"], search_fn=fn)

        assert step.papers_linked == 2  # the finding + the acquired paper
        assert "local 1, acquired 1" in self.entries[0]["text"]
        assert "graph thin, outside searched" in self.entries[0]["text"]

    def test_answered_query_logbook_says_outside_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s2 = _S2Stub()
        monkeypatch.setattr(s2mod, "search_s2_papers", s2)
        fn = qsearch.make_acquiring_search(7, _fake_hub())
        store: Any = _StepStore(
            held_ids=[(50, "finding"), (51, "finding"), (52, "paper"), (53, "paper")]
        )
        qsearch.run_search_step(store, 7, ["q"], search_fn=fn)

        assert "local 4, acquired 0" in self.entries[0]["text"]
        assert "outside skipped" in self.entries[0]["text"]
        assert s2.calls == []
        # bounded by MAX_LINK_PER_QUERY
        assert [ln["src_ref_id"] for ln in store.links] == [50, 51, 52]
        assert all(ln["relation"] == "serves" for ln in store.links)

    def test_draft_and_memory_hits_count_but_are_not_linked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s2 = _S2Stub()
        monkeypatch.setattr(s2mod, "search_s2_papers", s2)
        fn = qsearch.make_acquiring_search(7, _fake_hub())
        store: Any = _StepStore(
            held_ids=[(60, "draft"), (61, "memory"), (62, "concept"), (63, "paper")]
        )
        qsearch.run_search_step(store, 7, ["q"], search_fn=fn)

        assert s2.calls == []  # 4 local hits answered it
        assert [ln["src_ref_id"] for ln in store.links] == [62, 63]

    def test_s2_failure_does_not_raise_and_is_logged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            s2mod, "search_s2_papers", _S2Stub(raises=RuntimeError("429"))
        )
        fn = qsearch.make_acquiring_search(7, _fake_hub())
        store: Any = _StepStore(held_ids=[(50, "finding")])
        step = qsearch.run_search_step(store, 7, ["q"], search_fn=fn)

        assert step.papers_linked == 1
        assert "outside search failed" in self.entries[0]["text"]
