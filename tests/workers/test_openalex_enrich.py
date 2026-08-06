"""The scheduled OpenAlex abstract/metadata enrich pass — promote + fetch."""

from __future__ import annotations

from typing import Any

import pytest

from precis.ingest import openalex_meta
from precis.store import Store
from precis.workers import openalex_enrich
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


def _card(store: Store, ref_id: int, kind: str, text: str, *, ord_: int = -1) -> int:
    with store.pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
                "VALUES (%s, %s, %s, %s) RETURNING chunk_id",
                (ref_id, ord_, kind, text),
            ).fetchone()
    assert row is not None
    return int(row[0])


def _embed(store: Store, chunk_id: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder) VALUES (%s, 'bge-m3')",
            (chunk_id,),
        )
        conn.commit()


def _chunk_row(store: Store, ref_id: int, kind: str) -> tuple[int, str] | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT chunk_id, text FROM chunks WHERE ref_id = %s AND chunk_kind = %s",
            (ref_id, kind),
        ).fetchone()
    return (int(row[0]), str(row[1])) if row else None


def _has_embedding(store: Store, chunk_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM chunk_embeddings WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    return row is not None


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


class TestCardRebuild:
    def test_lane_a_promotion_rebuilds_cards_and_mints_missing_abstract_card(
        self, store: Store
    ) -> None:
        rid = _paper(
            store,
            slug="p7",
            title="Sensor Fusion",
            meta={"openalex": {"abstract": "Highly sensitive sensors"}},
        )
        # Simulate an abstract-less ingest: only card_combined exists (no
        # card_abstract at all — pipeline._build_cards only builds one
        # ``if abstract``).
        combined_id = _card(store, rid, "card_combined", "Sensor Fusion")
        _embed(store, combined_id)
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        result = run_openalex_enrich_pass(store)

        assert result.claimed >= 1
        assert _meta(store, rid).get("abstract") == "Highly sensitive sensors"

        # card_abstract now exists, carrying the abstract text, NULL embedding.
        abstract_card = _chunk_row(store, rid, "card_abstract")
        assert abstract_card is not None
        abstract_chunk_id, abstract_text = abstract_card
        assert abstract_text == "Highly sensitive sensors"
        assert not _has_embedding(store, abstract_chunk_id)

        # card_combined's text now folds in the abstract, and its stale
        # embedding was dropped so embed:bge-m3 re-claims it.
        combined = _chunk_row(store, rid, "card_combined")
        assert combined is not None
        assert "Highly sensitive sensors" in combined[1]
        assert not _has_embedding(store, combined_id)

    def test_lane_b_fetch_that_lands_an_abstract_rebuilds_cards(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        monkeypatch.setattr(
            openalex_meta, "fetch_openalex_work", lambda doi, **k: _WORK
        )
        rid = _paper(store, slug="p8", title="Fused Sensors", doi="10.1/f")
        combined_id = _card(store, rid, "card_combined", "Fused Sensors")
        _embed(store, combined_id)
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        run_openalex_enrich_pass(store)

        assert _meta(store, rid).get("abstract") == "Highly sensitive sensors"
        abstract_card = _chunk_row(store, rid, "card_abstract")
        assert abstract_card is not None
        assert abstract_card[1] == "Highly sensitive sensors"
        combined = _chunk_row(store, rid, "card_combined")
        assert combined is not None
        assert "Highly sensitive sensors" in combined[1]
        assert not _has_embedding(store, combined_id)


class TestOpenAlexMissStamp:
    def test_genuine_miss_is_stamped_and_not_reselected(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        monkeypatch.setattr(openalex_meta, "fetch_openalex_work", lambda doi, **k: None)
        rid = _paper(store, slug="p9", doi="10.1/miss")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        run_openalex_enrich_pass(store)

        meta = _meta(store, rid)
        assert meta.get("openalex", {}).get("miss") is True
        assert "tried_at" in meta.get("openalex", {})
        # No longer eligible for Lane B's next fetch batch.
        assert rid not in {r for r, _ in openalex_enrich._fetch_batch(store, limit=50)}

    def test_transient_fetch_error_is_not_stamped_stays_eligible(
        self, monkeypatch: pytest.MonkeyPatch, store: Store
    ) -> None:
        def _boom(doi: str, **k: Any) -> dict[str, Any]:
            raise RuntimeError("network blip")

        monkeypatch.setattr(openalex_meta, "fetch_openalex_work", _boom)
        rid = _paper(store, slug="p10", doi="10.1/transient")
        store.set_setting(_STATE_KEY, "2000-01-01T00:00:00+00:00")

        run_openalex_enrich_pass(store)

        assert _meta(store, rid).get("openalex") is None
        # Still eligible — a transient error must keep retrying.
        assert rid in {r for r, _ in openalex_enrich._fetch_batch(store, limit=50)}
