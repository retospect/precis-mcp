"""Test helpers for ``tests/workers/`` — chunk seeding.

Plain functions (not pytest fixtures) so test modules can import
them on demand. The shape mirrors what ``precis.ingest.db_writer``
inserts in production but only the columns workers need.
"""

from __future__ import annotations

from collections.abc import Iterable

from precis.embedder import MockEmbedder
from precis.store import Store


def make_mock_bge_m3() -> MockEmbedder:
    """A deterministic mock embedder tagged as ``bge-m3``.

    The seeded ``embedders`` row is ``bge-m3``; using that model name
    keeps the FK on ``chunk_embeddings.embedder`` happy without
    inserting test-only rows. Tests that need a *second* embedder
    register one inline (see ``test_status_ignores_other_models``).
    """
    return MockEmbedder(dim=1024, model="bge-m3")


def seed_ref(store: Store, *, title: str = "seed paper") -> int:
    """Insert a minimal ``refs`` row; return its ``ref_id``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO refs (kind, set_by, title) "
            "VALUES ('paper', 'system', %s) RETURNING ref_id",
            (title,),
        ).fetchone()
        assert row is not None
        conn.commit()
        return int(row[0])


def seed_chunk(
    store: Store,
    *,
    ref_id: int,
    text: str,
    ord: int = 0,
    chunk_kind: str = "paragraph",
) -> int:
    """Insert a single chunk row; return its ``chunk_id``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text) "
            "VALUES (%s, 'system', %s, %s, %s) RETURNING chunk_id",
            (ref_id, ord, chunk_kind, text),
        ).fetchone()
        assert row is not None
        conn.commit()
        return int(row[0])


def seed_chunks(
    store: Store,
    texts: Iterable[str],
    *,
    chunk_kind: str = "paragraph",
) -> tuple[int, list[int]]:
    """Seed one ref + one chunk per ``text``; return ``(ref_id, chunk_ids)``."""
    ref_id = seed_ref(store)
    chunk_ids = [
        seed_chunk(store, ref_id=ref_id, ord=i, chunk_kind=chunk_kind, text=t)
        for i, t in enumerate(texts)
    ]
    return ref_id, chunk_ids


__all__ = [
    "make_mock_bge_m3",
    "seed_chunk",
    "seed_chunks",
    "seed_ref",
]
