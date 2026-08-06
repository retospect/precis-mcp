"""The scheduled OpenAlex abstract/metadata enrich pass — promote + fetch."""

from __future__ import annotations

from typing import Any

import pytest

from precis.ingest import openalex_meta
from precis.store import Store
from precis.workers.openalex_enrich import _STATE_KEY, run_openalex_enrich_pass

# A trimmed OpenAlex work object carrying a reconstructable abstract.
_WORK: dict[str, Any] = {
    "id": "https://openalex.org/W1",
    "abstract_inverted_index": {"Highly": [0], "sensitive": [1], "sensors": [2]},
}


def _paper(
    store: Store,
    *,
    slug: str,
    title: str = "X",
    doi: str | None = None,
    meta: dict[str, Any] | None = None,
) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=title, meta=meta or {})
    if doi:
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO ref_identifiers (ref_id, id_kind, id_value, source) "
                "VALUES (%s, 'doi', %s, 'manual')",
                (ref.id, doi),
            )
    return ref.id


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="paper", id=ref_id)
    assert ref is not None
    return ref.meta or {}


class TestEnrichRefPromotion:
    def test_promotes_reconstructed_abstract_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        monkeypatch.setattr(
            openalex_meta, "fetch_openalex_work", lambda doi, **k: _WORK
        )
        rid = _paper(store, slug="p1", doi="10.1/a")
        enr = openalex_meta.enrich_ref(store, rid, doi="10.1/a")
        assert enr is not None
        assert _meta(store, rid).get("abstract") == "Highly sensitive sensors"

    def test_does_not_overwrite_existing_top_level_abstract(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        monkeypatch.setattr(
            openalex_meta, "fetch_openalex_work", lambda doi, **k: _WORK
        )
        rid = _paper(
            store, slug="p2", doi="10.1/b", meta={"abstract": "Existing abstract."}
        )
        openalex_meta.enrich_ref(store, rid, doi="10.1/b")
        assert _meta(store, rid)["abstract"] == "Existing abstract."


class TestLaneA:
    def test_promotes_already_fetched_openalex_abstract_no_network(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        called = False

        def _boom(*a: Any, **k: Any) -> None:
            nonlocal called
            called = True
            raise AssertionError("Lane A must not touch the network")

        monkeypatch.setattr(openalex_meta, "fetch_openalex_work", _boom)
        rid = _paper(store, slug="p3", meta={"openalex": {"abstract": "X"}})
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        result = run_openalex_enrich_pass(store)

        assert not called
        assert result.claimed >= 1
        assert _meta(store, rid).get("abstract") == "X"


class TestLaneB:
    def test_fetches_and_enriches_a_doi_paper_with_no_openalex_block(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        monkeypatch.setattr(
            openalex_meta, "fetch_openalex_work", lambda doi, **k: _WORK
        )
        rid = _paper(store, slug="p4", doi="10.1/c")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        result = run_openalex_enrich_pass(store)

        assert result.claimed >= 1
        assert _meta(store, rid).get("abstract") == "Highly sensitive sensors"
        assert "openalex" in _meta(store, rid)

    def test_skips_a_paper_that_already_has_an_abstract(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        calls: list[str] = []

        def _fetch(doi: str, **k: Any) -> dict[str, Any]:
            calls.append(doi)
            return _WORK

        monkeypatch.setattr(openalex_meta, "fetch_openalex_work", _fetch)
        _paper(store, slug="p5", doi="10.1/d", meta={"abstract": "Already there."})
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        run_openalex_enrich_pass(store)

        assert calls == []


class TestThrottle:
    def test_second_immediate_run_is_a_noop(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        monkeypatch.setattr(
            openalex_meta, "fetch_openalex_work", lambda doi, **k: _WORK
        )
        _paper(store, slug="p6", doi="10.1/e")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        r1 = run_openalex_enrich_pass(store)
        assert r1.claimed >= 1

        r2 = run_openalex_enrich_pass(store)
        assert r2.claimed == 0
        assert r2.ok == 0
        assert r2.failed == 0
