"""Weak semantic-only memory hits are omitted with an explicit count.

``view='index'`` hard-cuts the same rows, compact views retain the complete
top-N handle list, and kinds absent from ``SEMANTIC_ONLY_FOLD_DISTANCE``
never cut. No continuation cursor is involved.
"""

from __future__ import annotations

import math

import pytest

from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.handlers.memory import MemoryHandler
from precis.store import SEMANTIC_ONLY_FOLD_DISTANCE, ChunkInsert, Store


def _unit_vec(dim: int, pairs: list[tuple[int, float]]) -> list[float]:
    v = [0.0] * dim
    for i, x in pairs:
        v[i] = x
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


class _FixedEmbedder:
    def __init__(self, vec: list[float]) -> None:
        self._vec = list(vec)

    @property
    def dim(self) -> int:
        return len(self._vec)

    @property
    def model(self) -> str:
        return "fixed"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vec) for _ in texts]

    def embed_one(self, text: str) -> list[float]:
        return list(self._vec)

    def is_ready(self) -> bool:
        return True

    def warmup(self) -> None:
        pass

    def unload(self) -> None:
        pass


def _seed_corpus(store: Store) -> dict[str, int]:
    """Four memory refs at fixed distances from the query vector."""
    dim = store.embedding_dim()
    near = _unit_vec(dim, [(0, 0.9), (1, math.sqrt(0.19))])
    far = _unit_vec(dim, [(0, 0.5), (1, math.sqrt(0.75))])

    strong = store.insert_ref(kind="memory", slug=None, title="strong node")
    store.chunks.insert_chunks(
        strong.id,
        [ChunkInsert(ord=0, text="prose about quokkas entirely", embedding=near)],
    )
    weak = store.insert_ref(kind="memory", slug=None, title="weak node")
    store.chunks.insert_chunks(
        weak.id,
        [
            ChunkInsert(
                ord=0, text="prose about wombats entirely unrelated", embedding=far
            )
        ],
    )
    corroborated = store.insert_ref(kind="memory", slug=None, title="corroborated node")
    store.chunks.insert_chunks(
        corroborated.id,
        [
            ChunkInsert(
                ord=0,
                text="alpha appears here without the other term",
                embedding=far,
            )
        ],
    )
    lex_only = store.insert_ref(kind="memory", slug=None, title="lex node")
    store.chunks.insert_chunks(
        lex_only.id,
        [ChunkInsert(ord=0, text="alpha and beta both appear here")],
    )
    return {
        "strong": strong.id,
        "weak": weak.id,
        "corroborated": corroborated.id,
        "lex_only": lex_only.id,
    }


def _handler(store: Store) -> MemoryHandler:
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    return MemoryHandler(hub=Hub(store=store, embedder=_FixedEmbedder(q_vec)))


def test_mixed_distances_omit_only_weak_uncorroborated(store: Store) -> None:
    ids = _seed_corpus(store)
    body = _handler(store).search(q="alpha beta").body

    assert "# 3 of 4 memory matches for 'alpha beta'" in body
    assert f"memory {ids['strong']}" in body
    assert f"memory {ids['corroborated']}" in body
    assert f"memory {ids['lex_only']}" in body
    assert f"memory {ids['weak']}" not in body
    assert "(0.10)" in body
    assert "(rank=" in body
    assert "1 weak match(es) omitted (distance > 0.42)" in body
    assert "view='index'" in body
    assert "more(cursor=" not in body


def test_or_only_lexical_match_counts_as_corroboration(store: Store) -> None:
    ids = _seed_corpus(store)
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    info: dict[int, tuple[float | None, bool]] = {}
    hits = store.chunks.search_chunks(
        q="alpha beta",
        query_vec=q_vec,
        kind="memory",
        limit=10,
        max_distance=0.65,
        fold_info_out=info,
    )
    by_ref = {ref.id: info[block.id] for block, ref, _ in hits}
    sem_d, corroborated = by_ref[ids["corroborated"]]
    assert corroborated is True
    assert sem_d == pytest.approx(0.5, abs=0.01)
    assert by_ref[ids["weak"]][1] is False
    assert by_ref[ids["lex_only"]][0] is None


def test_all_weak_prints_no_strong_match(store: Store) -> None:
    dim = store.embedding_dim()
    far = _unit_vec(dim, [(0, 0.5), (1, math.sqrt(0.75))])
    ref = store.insert_ref(kind="memory", slug=None, title="only weak")
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="unrelated prose entirely", embedding=far)],
    )
    body = _handler(store).search(q="alpha beta").body
    assert "no strong match" in body
    assert "1 weak match(es) omitted (distance > 0.42)" in body
    assert "of 1 memory match" not in body


def test_index_view_hard_cuts_without_hint(store: Store) -> None:
    ids = _seed_corpus(store)
    body = _handler(store).search(q="alpha beta", view="index").body
    assert f"me{ids['strong']}" in body
    assert f"me{ids['corroborated']}" in body
    assert f"me{ids['lex_only']}" in body
    assert f"me{ids['weak']}" not in body
    assert "weak matches" not in body
    assert "more(cursor=" not in body


def test_gripe_never_cuts(store: Store) -> None:
    assert "gripe" not in SEMANTIC_ONLY_FOLD_DISTANCE
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    gh = GripeHandler(hub=Hub(store=store, embedder=_FixedEmbedder(q_vec)))
    gh.put(text="unrelated gripe prose entirely")
    ref_id = next(
        r.id
        for r in store.list_refs(kind="gripe")
        if r.title == "unrelated gripe prose entirely"
    )
    far = _unit_vec(dim, [(0, 0.5), (1, math.sqrt(0.75))])
    for chunk in store.chunks.list_chunks_for_ref(ref_id):
        store.chunks.update_chunk_embedding(chunk.id, far)

    body = gh.search(q="alpha beta").body
    assert f"gripe {ref_id}" in body
    assert "(rank=" in body
    assert "weak matches" not in body


def test_cross_kind_stream_hard_cuts(store: Store) -> None:
    ids = _seed_corpus(store)
    hits = _handler(store).search_hits(q="alpha beta")
    hit_ids = {h.ref_id for h in hits}
    assert ids["weak"] not in hit_ids
    assert ids["strong"] in hit_ids
    assert ids["corroborated"] in hit_ids


def test_compact_view_enumerates_weak_hits(store: Store) -> None:
    ids = _seed_corpus(store)
    resp = _handler(store)._search_body_chunks(
        q="alpha beta", tags=None, page_size=10, compact="ids"
    )
    for rid in ids.values():
        assert f"me{rid}" in resp.body
    assert "weak matches" not in resp.body
    assert "more(cursor=" not in resp.body


def test_boundary_distance_kept_by_both_paths(store: Store) -> None:
    dim = store.embedding_dim()

    def at(c: float) -> list[float]:
        return _unit_vec(dim, [(0, c), (1, math.sqrt(1 - c * c))])

    just_under = at(0.581)
    just_over = at(0.579)
    under = store.insert_ref(kind="memory", slug=None, title="under boundary")
    store.chunks.insert_chunks(
        under.id,
        [ChunkInsert(ord=0, text="unrelated prose entirely", embedding=just_under)],
    )
    over = store.insert_ref(kind="memory", slug=None, title="over boundary")
    store.chunks.insert_chunks(
        over.id,
        [ChunkInsert(ord=0, text="unrelated prose entirely", embedding=just_over)],
    )
    handler = _handler(store)

    body = handler.search(q="alpha beta").body
    assert f"memory {under.id}" in body
    assert "(0.42)" in body
    assert f"memory {over.id}" not in body
    assert "1 weak match(es) omitted (distance > 0.42)" in body

    index = handler.search(q="alpha beta", view="index").body
    assert f"me{under.id}" in index
    assert f"me{over.id}" not in index


def test_fused_order_unchanged_by_fold_info(store: Store) -> None:
    _seed_corpus(store)
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    plain = store.chunks.search_chunks(
        q="alpha beta", query_vec=q_vec, kind="memory", limit=10, max_distance=0.65
    )
    info: dict[int, tuple[float | None, bool]] = {}
    probed = store.chunks.search_chunks(
        q="alpha beta",
        query_vec=q_vec,
        kind="memory",
        limit=10,
        max_distance=0.65,
        fold_info_out=info,
    )
    assert [block.id for block, _, _ in plain] == [block.id for block, _, _ in probed]
    assert len(info) == len(probed)


def test_weak_count_preserves_fused_order_without_drain(store: Store) -> None:
    dim = store.embedding_dim()
    weak_ids = []
    for i, first in enumerate([0.45, 0.40, 0.36]):
        ref = store.insert_ref(kind="memory", slug=None, title=f"weak {i}")
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text=f"unrelated prose number {i} entirely",
                    embedding=_unit_vec(
                        dim, [(0, first), (1, math.sqrt(1 - first**2))]
                    ),
                )
            ],
        )
        weak_ids.append(ref.id)
    strong = store.insert_ref(kind="memory", slug=None, title="strong")
    store.chunks.insert_chunks(
        strong.id,
        [
            ChunkInsert(
                ord=0,
                text="prose about quokkas entirely",
                embedding=_unit_vec(dim, [(0, 0.9), (1, math.sqrt(0.19))]),
            )
        ],
    )
    body = _handler(store).search(q="alpha beta").body
    assert "# 1 of 4 memory match for 'alpha beta'" in body
    assert "3 weak match(es) omitted (distance > 0.42)" in body
    assert all(f"memory {rid}" not in body for rid in weak_ids)
    assert "more(cursor=" not in body


def test_or_probe_splits_on_nonword(store: Store) -> None:
    from precis.store._chunks_ops import _or_corroborates

    assert _or_corroborates("cloud-brightening")
    assert _or_corroborates("alpha beta")
    assert not _or_corroborates("alpha")
    assert not _or_corroborates("alpha_beta")

    dim = store.embedding_dim()
    far = _unit_vec(dim, [(0, 0.5), (1, math.sqrt(0.75))])
    ref = store.insert_ref(kind="memory", slug=None, title="wombat node")
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="wombats roam the fields", embedding=far)],
    )
    body = _handler(store).search(q="quokka-wombat").body
    assert f"memory {ref.id}" in body
    assert "(0.50)" in body
