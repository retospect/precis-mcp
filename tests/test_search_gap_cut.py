"""Relative cut regressions: offsets move, tight clusters stay together."""

from __future__ import annotations

import pytest

from precis.store import ChunkInsert, Store
from precis.store._search_cut import gap_cut_distance, semantic_fold_distance
from precis.utils import search_trace
from tests.test_search_floor_per_kind import _handlers, _seed, _vec_at
from tests.test_search_fold import _handler, _seed_corpus


@pytest.fixture(autouse=True)
def gap_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_SEARCH_FIXED_FLOORS", raising=False)
    monkeypatch.delenv("PRECIS_SEARCH_GAP_CUT", raising=False)


def test_gap_boundary_and_corpus_guard() -> None:
    assert gap_cut_distance([0.40, 0.41], 0.03) == pytest.approx(0.43)
    assert gap_cut_distance([0.70, 0.80], 0.15) == pytest.approx(0.65)
    assert gap_cut_distance([None], 0.15) == pytest.approx(0.65)


@pytest.mark.parametrize("invalid", ["nan", "inf", "-0.1", "bad"])
def test_invalid_override_uses_default(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    monkeypatch.setenv("PRECIS_SEARCH_GAP_CUT", invalid)
    assert semantic_fold_distance("memory", [0.4]) == pytest.approx(0.55)


def test_override_and_fixed_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_SEARCH_GAP_CUT", "0.03")
    assert semantic_fold_distance("memory", [0.4]) == pytest.approx(0.43)
    monkeypatch.setenv("PRECIS_SEARCH_FIXED_FLOORS", "1")
    assert semantic_fold_distance("memory", [0.4]) == pytest.approx(0.42)
    assert semantic_fold_distance("gripe", [0.4]) is None


def test_default_fold_and_machine_cut_agree_with_trace(store: Store) -> None:
    ids = _seed_corpus(store)
    handler = _handler(store)
    trace = search_trace.SearchTrace()
    token = search_trace.CURRENT.set(trace)
    try:
        body = handler.search(q="alpha beta").body
        assert "distance > 0.25, gap cut" in body
        assert f"memory {ids['weak']}" not in body
        assert f"memory {ids['corroborated']}" in body
        assert trace.candidates[f"me{ids['weak']}"]["cut"] == "gap"
        index = handler.search(q="alpha beta", view="index").body
        assert f"me{ids['weak']}" not in index
        assert f"me{ids['strong']}" in index
        assert f"me{ids['corroborated']}" in index
    finally:
        search_trace.CURRENT.reset(token)


@pytest.mark.parametrize("kind", ["paper", "patent", "web", "wikipedia"])
def test_hard_cut_is_relative_for_every_kind(store: Store, kind: str) -> None:
    near = _seed(store, kind, "cluster-one", 0.46)
    tight = _seed(store, kind, "cluster-two", 0.48)
    far = _seed(store, kind, "tail", 0.63)
    hits = _handlers(store)[kind].search_hits(q="alpha beta")
    assert {h.ref_id for h in hits} == {near, tight}
    assert far not in {h.ref_id for h in hits}


def test_all_noise_has_no_semantic_only_results(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="noise")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text="unrelated wombats",
                embedding=_vec_at(store.embedding_dim(), 0.71),
            )
        ],
    )
    handler = _handler(store)
    assert handler.search(q="alpha beta").result_count == 0
    assert f"me{ref.id}" not in handler.search(q="alpha beta", view="index").body


def test_tight_memory_cluster_survives_and_modes_bypass(store: Store) -> None:
    ids = []
    for i, distance in enumerate([0.48, 0.49, 0.51]):
        ref = store.insert_ref(kind="memory", slug=None, title=f"cluster {i}")
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text="unrelated wombats",
                    embedding=_vec_at(store.embedding_dim(), distance),
                )
            ],
        )
        ids.append(ref.id)
    handler = _handler(store)
    body = handler.search(q="alpha beta").body
    assert all(f"memory {rid}" in body for rid in ids)
    assert "omitted" not in body
    _seed_corpus(store)
    assert "weak match(es)" not in handler.search(q="alpha beta", mode="semantic").body
    assert "weak match(es)" not in handler.search(q="alpha beta", mode="lexical").body
    compact = handler._search_body_chunks(
        q="alpha beta", tags=None, page_size=20, compact="ids"
    )
    assert "omitted" not in compact.body
