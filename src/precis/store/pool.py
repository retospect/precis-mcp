"""psycopg 3 connection pool factory.

Per-connection setup registers pgvector codecs lazily — only when the
`vector` extension exists in the target database — so the pool also
works against a freshly-created DB before migrations have applied the
extension.

JSONB round-trip happens via the `Jsonb()` adapter wrapper at call
sites; we don't register a global dict-as-jsonb adapter because that
breaks ordinary JSON columns and is too implicit.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool


def _configure_connection(conn: Connection) -> None:
    """Per-connection setup. Called by the pool's `configure=` hook.

    Must leave the connection in IDLE state — psycopg pool rejects
    connections still inside a transaction. We commit after the
    introspection SELECT to be safe.
    """
    try:
        has_vector = conn.execute(
            "SELECT 1 FROM pg_type WHERE typname = 'vector' LIMIT 1"
        ).fetchone()
        if has_vector is not None:
            from pgvector.psycopg import register_vector

            register_vector(conn)
    finally:
        conn.commit()


def create_pool(
    dsn: str,
    *,
    min_size: int = 2,
    max_size: int = 10,
    open_timeout: float = 10.0,
    **kwargs: Any,
) -> ConnectionPool:
    """Create a psycopg connection pool with pgvector codec registered.

    The pool is opened immediately so callers can use it without a
    separate ``open()`` call. Caller owns the pool — close with
    ``pool.close()`` on shutdown.

    Fail-fast: ``open_timeout`` bounds how long ``pool.open(wait=True)``
    will block waiting for ``min_size`` healthy connections. If the DB
    is unreachable we raise ``PoolTimeout`` instead of hanging forever.
    """
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=min_size,
        max_size=max_size,
        configure=_configure_connection,
        open=False,
        **kwargs,
    )
    pool.open(wait=True, timeout=open_timeout)
    return pool
