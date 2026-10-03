"""Filtered ANN recall: a rare kind must not be starved by a paper-heavy index.

pgvector applies every predicate besides the ORDER BY (``r.kind``, ...) *after*
the HNSW scan, which yields only ``hnsw.ef_search`` candidates. When the nearest
candidates are all papers, ``kinds=['finding']`` came back empty. The store
now enables a strict-order iterative scan for these queries
(:func:`precis.store._chunks_ops._prepare_filtered_ann`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import numpy as np
import pytest
from psycopg import Connection

from precis.store import Store

_DEFAULT_EMBEDDER = "bge-m3"  # migration-seeded default (embedders.dim=1024)
_DIM = 1024
_N_PAPERS = 200
_N_FINDINGS = 3


def _unit(v: np.ndarray) -> list[float]:
    return (v / np.linalg.norm(v)).tolist()


def _insert_embedded(store: Store, ref_id: int, text: str, vec: list[float]) -> None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, 0, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, text),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, %s, %s, 'ok', 1)",
            (int(row[0]), _DEFAULT_EMBEDDER, vec),
        )


@pytest.fixture
def force_hnsw(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the planner take the HNSW index on every store connection.

    ``SET LOCAL`` at the head of each pooled transaction, so the setting is
    transaction-scoped (no leak into the shared pool) and the store's own
    ANN query — which runs after it in the same transaction — sees it.
    """
    pool = store.pool
    real = pool.connection

    @contextmanager
    def wrapped(*a: Any, **kw: Any) -> Iterator[Connection]:
        with real(*a, **kw) as conn:
            # No seqscan and no explicit Sort → the ORDER BY can only be
            # served by the HNSW index (the tiny-table planner would
            # otherwise pick refs_kind_idx + sort).
            conn.execute("SET LOCAL enable_seqscan = off")
            conn.execute("SET LOCAL enable_sort = off")
            yield conn

    monkeypatch.setattr(pool, "connection", wrapped)


def _seed(store: Store) -> list[float]:
    rng = np.random.default_rng(7)
    base = rng.standard_normal(_DIM)
    base /= np.linalg.norm(base)

    def at_distance(a: float) -> list[float]:
        # base + a * (unit noise ⟂ base) → cosine distance 1 - 1/sqrt(1+a²)
        noise = rng.standard_normal(_DIM)
        noise -= noise.dot(base) * base
        noise /= np.linalg.norm(noise)
        return _unit(base + a * noise)

    for i in range(_N_PAPERS):
        p = store.insert_ref(
            kind="paper", slug=f"ann-p{i}", title=f"Paper {i}", meta={}
        )
        _insert_embedded(store, p.id, f"paper body {i}", at_distance(0.05))
    for i in range(_N_FINDINGS):
        f = store.insert_ref(kind="finding", slug=None, title=f"Finding {i}", meta={})
        _insert_embedded(store, f.id, f"finding body {i}", at_distance(0.8 + 0.1 * i))
    return base.tolist()


def test_rare_kind_is_not_starved_by_nearer_papers(
    store: Store, force_hnsw: None
) -> None:
    v = _seed(store)

    # The planner must really be on the HNSW index, else this test is vacuous.
    with store.pool.connection() as conn:
        plan = "\n".join(
            str(r[0])
            for r in conn.execute(
                "EXPLAIN SELECT ce.chunk_id FROM chunk_embeddings ce "
                "JOIN chunks c ON c.chunk_id = ce.chunk_id "
                "JOIN refs r ON r.ref_id = c.ref_id "
                "WHERE ce.status = 'ok' AND ce.vector IS NOT NULL "
                "AND r.kind = ANY(%s) "
                "ORDER BY ce.vector <=> %s::vector LIMIT 24",
                (["finding"], v),
            ).fetchall()
        )
    assert "chunk_embeddings_vec_hnsw_idx" in plan, re.sub(r"\[[^\]]*\]", "[..]", plan)

    hits = store.chunks.search_chunks_semantic(
        query_vec=v, kinds=["finding"], limit=24, max_distance=0.6
    )
    assert sorted(ref.title for _c, ref, _d in hits) == [
        f"Finding {i}" for i in range(_N_FINDINGS)
    ]
    dists = [d for _c, _r, d in hits]
    assert dists == sorted(dists), "strict_order must keep the result ordered"
    assert all(0.1 < d < 0.4 for d in dists), dists


def test_distance_floor_outside_the_scan_keeps_rows_and_pages(
    store: Store, force_hnsw: None
) -> None:
    """The floor filters the ordered nearest-``limit + offset`` prefix, so a
    floored page is exactly the unfloored list cut at the floor, then
    paged. Findings sit at cosine distance ~0.219 / 0.257 / 0.293."""
    v = _seed(store)

    def titles(rows: list[tuple[Any, Any, Any]]) -> list[str]:
        return [ref.title for _c, ref, _d in rows]

    unfloored = store.chunks.search_chunks_semantic(
        query_vec=v, kinds=["finding"], limit=10
    )
    within = [r for r in unfloored if r[2] < 0.27]
    assert titles(within) == ["Finding 0", "Finding 1"]

    floored = store.chunks.search_chunks_semantic(
        query_vec=v, kinds=["finding"], limit=10, max_distance=0.27
    )
    assert titles(floored) == titles(within)

    page2 = store.chunks.search_chunks_semantic(
        query_vec=v, kinds=["finding"], limit=1, offset=1, max_distance=0.27
    )
    assert titles(page2) == ["Finding 1"]
    page3 = store.chunks.search_chunks_semantic(
        query_vec=v, kinds=["finding"], limit=1, offset=2, max_distance=0.27
    )
    assert page3 == []

    # The fused semantic leg applies the same floor. A query with no lexical
    # hit leaves only that leg.
    fused = store.chunks.search_chunks_fused(
        q="zzqxnomatch", query_vec=v, kind="finding", limit=10, max_distance=0.27
    )
    assert sorted(ref.title for _c, ref, *_ in fused) == ["Finding 0", "Finding 1"]
