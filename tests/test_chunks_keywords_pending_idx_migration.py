"""0277 guard for ``chunks_keywords_pending_idx`` (gr520236).

The migration never builds the index on a large table and takes no table
lock when the pre-built index is already valid and exact. Each case runs
the migration's DO block inside a transaction that is rolled back, so the
shared test DB is never altered.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from precis.store import Store
from precis.workers.chunk_keywords import PENDING_INDEX_DDL

pytestmark = pytest.mark.db

_SQL = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "precis"
    / "migrations"
    / "0277_chunks_keywords_pending_idx.sql"
).read_text(encoding="utf-8")
# The guard body only: the file's own BEGIN/COMMIT cannot nest in the
# test's rolled-back transaction.
_GUARD = _SQL[_SQL.index("DO $$") : _SQL.index("$$;", _SQL.index("DO $$")) + 3]
_PLAIN_DDL = PENDING_INDEX_DDL.replace(" CONCURRENTLY", "")


def _pred(conn: psycopg.Connection) -> str | None:
    row = conn.execute(
        "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = 'chunks_keywords_pending_idx'"
    ).fetchone()
    return row[0] if row else None


def test_fresh_db_builds_index_matching_python_ddl(store: Store) -> None:
    """Migrated DB has the index; dropping it and re-running the guard
    rebuilds it with a predicate identical to the one the python DDL
    (and so the claim) produces."""
    with store.pool.connection() as conn:
        conn.execute("DROP INDEX IF EXISTS chunks_keywords_pending_idx")
        conn.execute(_PLAIN_DDL)
        want = _pred(conn)
        conn.execute("DROP INDEX chunks_keywords_pending_idx")
        conn.execute(_GUARD)
        assert _pred(conn) == want
        conn.execute(_GUARD)  # valid + exact: second run is a no-op
        conn.rollback()


def test_refuses_other_predicate(store: Store) -> None:
    with store.pool.connection() as conn:
        conn.execute("DROP INDEX IF EXISTS chunks_keywords_pending_idx")
        conn.execute(
            "CREATE INDEX chunks_keywords_pending_idx ON chunks (chunk_id DESC) "
            "WHERE keywords IS NULL"
        )
        with pytest.raises(psycopg.errors.RaiseException, match="predicate"):
            conn.execute(_GUARD)
        conn.rollback()


def test_refuses_inline_build_on_large_table(store: Store) -> None:
    with store.pool.connection() as conn:
        conn.execute("DROP INDEX IF EXISTS chunks_keywords_pending_idx")
        conn.execute("UPDATE pg_class SET reltuples = 5000000 WHERE relname = 'chunks'")
        with pytest.raises(psycopg.errors.RaiseException, match="missing"):
            conn.execute(_GUARD)
        conn.rollback()
