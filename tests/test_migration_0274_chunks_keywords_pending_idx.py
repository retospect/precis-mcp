"""Migration 0274 guard: the ops-prebuilt partial index (gr520236 /
gr550973; docs/backlog/chunk-keywords-pending-index.md).

The migration must be idempotent on a small (test/dev) table, refuse an
INVALID pre-built index, refuse a pre-built index whose predicate has
drifted from :data:`precis.workers.chunk_keywords.PENDING_INDEX_DDL`,
and refuse to build inline once ``chunks`` looks prod-sized without a
pre-built index.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from precis.store import Store
from tests.conftest import MIGRATIONS_DIR, _active_dsn

pytestmark = pytest.mark.db


def _migration() -> str:
    return (MIGRATIONS_DIR / "0274_chunks_keywords_pending_idx.sql").read_text(
        encoding="utf-8"
    )


@pytest.fixture
def conn(store: Store) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    c = psycopg.connect(_active_dsn(), autocommit=True, row_factory=dict_row)
    try:
        c.execute("DROP INDEX IF EXISTS chunks_keywords_pending_idx")
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DROP INDEX IF EXISTS chunks_keywords_pending_idx")
        c.close()


def _scalar(c: psycopg.Connection[dict[str, Any]], q: str) -> Any:
    row = c.execute(q).fetchone()
    assert row is not None
    return next(iter(row.values()))


def test_0274_builds_inline_on_a_small_table(
    conn: psycopg.Connection[dict[str, Any]],
) -> None:
    """A fresh/test/dev DB has no pre-built index and few rows: the
    guard passes and ``CREATE INDEX IF NOT EXISTS`` builds it
    in-transaction."""
    conn.execute(_migration())
    assert (
        _scalar(
            conn,
            "SELECT to_regclass('chunks_keywords_pending_idx')",
        )
        is not None
    )
    assert _scalar(
        conn,
        "SELECT indisvalid FROM pg_index "
        "WHERE indexrelid = 'chunks_keywords_pending_idx'::regclass",
    )


def test_0274_is_idempotent(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """Re-applying after a correct pre-built index is a no-op, not a
    guard failure."""
    conn.execute(_migration())
    conn.execute(_migration())
    assert (
        _scalar(
            conn,
            "SELECT count(*) FROM pg_class WHERE relname = 'chunks_keywords_pending_idx'",
        )
        == 1
    )


def test_0274_refuses_an_invalid_pre_built_index(
    conn: psycopg.Connection[dict[str, Any]],
) -> None:
    """A failed CONCURRENTLY build leaves ``indisvalid = false``; the
    guard refuses rather than silently accepting it."""
    conn.execute(_migration())
    conn.execute(
        "UPDATE pg_index SET indisvalid = false "
        "WHERE indexrelid = 'chunks_keywords_pending_idx'::regclass"
    )
    with pytest.raises(
        psycopg.errors.RaiseException,
        match=re.escape("chunks_keywords_pending_idx exists but is INVALID"),
    ):
        conn.execute(_migration())
    conn.execute("ROLLBACK")


def test_0274_refuses_a_predicate_mismatch(
    conn: psycopg.Connection[dict[str, Any]],
) -> None:
    """A pre-built index under the right name but a drifted predicate
    (e.g. a stale ``MIN_CHUNK_CHARS``) must not be silently accepted —
    the claim's DESC walk would stop matching it."""
    conn.execute(
        "CREATE INDEX chunks_keywords_pending_idx ON chunks (chunk_id DESC) "
        "WHERE keywords IS NULL AND length(text) >= 1"
    )
    with pytest.raises(
        psycopg.errors.RaiseException,
        match=re.escape("predicate does not match"),
    ):
        conn.execute(_migration())
    conn.execute("ROLLBACK")


def test_0274_refuses_to_build_inline_on_a_large_table(
    conn: psycopg.Connection[dict[str, Any]],
) -> None:
    """Without a pre-built index, a prod-sized ``chunks`` (by
    ``reltuples`` statistic, not an actual 1M-row seed) must refuse
    rather than taking the SHARE-lock heap scan inline."""
    conn.execute("UPDATE pg_class SET reltuples = 2000000 WHERE relname = 'chunks'")
    try:
        with pytest.raises(
            psycopg.errors.RaiseException,
            match=re.escape("missing on a large chunks table"),
        ):
            conn.execute(_migration())
        conn.execute("ROLLBACK")
    finally:
        conn.execute(
            "UPDATE pg_class SET reltuples = "
            "(SELECT count(*) FROM chunks) WHERE relname = 'chunks'"
        )
