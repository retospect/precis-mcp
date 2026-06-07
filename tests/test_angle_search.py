"""Store engine for the ``angle`` spray (DB-backed).

Covers seed-vector resolution (``like=``), card-inclusive ANN snap, the
kind filter, dedup/exclude, and the ``angle=1`` identity. The pure
anchor math is pinned separately in ``test_angle.py``; here we exercise
the SQL + snapping against real ``chunk_embeddings`` rows.
"""

from __future__ import annotations

import random

import pytest

from precis.embedder import MockEmbedder
from precis.store import Store

_EMB = MockEmbedder(dim=1024)
_DEFAULT_EMBEDDER = "bge-m3"  # the migration-seeded default (embedders.dim=1024)


def _embed_chunk(store: Store, ref_id: int, ord_: int, text: str) -> tuple[int, list]:
    """Insert a chunk + its embedding under the default embedder."""
    vec = _EMB.embed_one(text)
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, %s, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, ord_, text),
        ).fetchone()
        assert row is not None
        cid = int(row[0])
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, %s, %s, 'ok', 1)",
            (cid, _DEFAULT_EMBEDDER, vec),
        )
    return cid, vec


def _embed_card(store: Store, ref_id: int, text: str) -> int:
    cid = store.upsert_card_combined(ref_id, text)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, %s, %s, 'ok', 1)",
            (cid, _DEFAULT_EMBEDDER, _EMB.embed_one(text)),
        )
    return cid


# ── seed-vector resolution ─────────────────────────────────────────


def test_get_chunk_vector_round_trips(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="p1", title="P1", meta={})
    cid, vec = _embed_chunk(store, ref.id, 0, "copper nitrate")
    got = store.get_chunk_vector(cid)
    assert got is not None
    # pgvector stores float4 — compare with single-precision tolerance.
    assert got == pytest.approx(vec, abs=1e-5)


def test_get_chunk_vector_none_when_unembedded(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="p2", title="P2", meta={})
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, 0, 'paragraph', 'x', '{}'::jsonb) RETURNING chunk_id",
            (ref.id,),
        ).fetchone()
    assert store.get_chunk_vector(int(row[0])) is None


def test_seed_chunk_for_ref_prefers_card(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="m", meta={})
    _embed_chunk(store, ref.id, 0, "body chunk text")
    card = _embed_card(store, ref.id, "the card summary")
    assert store.seed_chunk_for_ref(ref.id) == card


def test_seed_chunk_for_ref_falls_back_to_body(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="pb", title="B", meta={})
    head, _ = _embed_chunk(store, ref.id, 0, "head")
    _embed_chunk(store, ref.id, 1, "tail")
    assert store.seed_chunk_for_ref(ref.id) == head


def test_seed_chunk_for_ref_none_when_unembedded(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="pn", title="N", meta={})
    assert store.seed_chunk_for_ref(ref.id) is None


# ── angle_neighbours ────────────────────────────────────────────────


def test_angle_one_snaps_to_the_seed_item(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="pa", title="A", meta={})
    cid, vec = _embed_chunk(store, ref.id, 0, "alpha")
    _embed_chunk(store, ref.id, 1, "beta")
    hits = store.angle_neighbours(vec, angle=1.0, n=1, rng=random.Random(0))
    assert len(hits) == 1
    block, _ref, cosine = hits[0]
    assert block.id == cid
    assert cosine == pytest.approx(1.0, abs=1e-4)


def test_exclude_skips_the_seed(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="pe", title="E", meta={})
    cid, vec = _embed_chunk(store, ref.id, 0, "alpha")
    other, _ = _embed_chunk(store, ref.id, 1, "beta")
    hits = store.angle_neighbours(
        vec, angle=1.0, n=1, exclude_chunk_ids=[cid], rng=random.Random(0)
    )
    assert [b.id for b, _r, _c in hits] == [other]


def test_results_are_distinct(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="pd", title="D", meta={})
    vecs = [_embed_chunk(store, ref.id, i, f"chunk {i}")[1] for i in range(5)]
    hits = store.angle_neighbours(vecs[0], angle=0.3, n=5, rng=random.Random(1))
    ids = [b.id for b, _r, _c in hits]
    assert len(ids) == len(set(ids))  # dedup holds across anchors


def test_kind_filter_excludes_non_targets(store: Store) -> None:
    paper = store.insert_ref(kind="paper", slug="pk", title="K", meta={})
    _, pvec = _embed_chunk(store, paper.id, 0, "target")
    todo = store.insert_ref(kind="todo", slug=None, title="t", meta={})
    _embed_chunk(store, todo.id, 0, "target")  # same text → near-identical vec
    hits = store.angle_neighbours(
        pvec, angle=1.0, n=10, kinds=("paper", "memory"), rng=random.Random(0)
    )
    assert all(r.kind in ("paper", "memory") for _b, r, _c in hits)


def test_empty_seed_returns_empty(store: Store) -> None:
    assert store.angle_neighbours([], angle=1.0, n=4) == []


def test_deterministic_under_seeded_rng(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="pr", title="R", meta={})
    vecs = [_embed_chunk(store, ref.id, i, f"c{i}")[1] for i in range(6)]
    a = store.angle_neighbours(vecs[0], angle=0.4, n=4, rng=random.Random(7))
    b = store.angle_neighbours(vecs[0], angle=0.4, n=4, rng=random.Random(7))
    assert [x.id for x, _r, _c in a] == [x.id for x, _r, _c in b]
