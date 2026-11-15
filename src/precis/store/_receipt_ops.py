"""Durable exact-read receipts — ``read_receipts`` (migration 0325).

Mixin on :class:`precis.store.Store`.

The receipt a successful exact-handle ``get`` leaves behind is what
licenses ``text={'$ref': handle}`` on a later ``put``/``edit`` (see
:mod:`precis.runtime.handle_deref`). Session-bound receipts used to live
only in process memory — every MCP restart dropped them, and
``stateless_http`` could never work at all (gr660588). Callers whose
dispatch carries a bound owner stamp (:func:`precis.principal.current_owner_key`)
write here instead: the row outlives the session and the process, and any
other session of the same credential can use it within the TTL.

Operational, **not corpus** — a small TTL table like ``tool_calls``'s
peers, never embedded. Every write sweeps that owner's expired rows on
the same connection so the table stays small without a sweeper pass;
:meth:`prune_read_receipts` is the global sweep for whoever wants one.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool


class ReceiptsMixin:
    """``read_receipts`` upsert / probe / sweep."""

    pool: ConnectionPool

    def record_receipt(
        self,
        *,
        owner_key: str,
        identity: str,
        ttl_seconds: float = 3600.0,
    ) -> None:
        """Upsert ``(owner_key, identity)`` with a fresh ``expires_at``.

        A re-read renews the receipt — the clock restarts on each exact
        read, same as the in-memory receipts' move-to-end behaviour. The
        same connection deletes every expired row first (an
        ``expires_at`` index range delete): one round trip keeps the
        table bounded at the write site instead of growing a sweeper
        dependency.
        """
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM read_receipts WHERE expires_at <= now()")
            conn.execute(
                "INSERT INTO read_receipts (owner_key, identity, expires_at)"
                " VALUES (%s, %s, now() + (%s || ' seconds')::interval)"
                " ON CONFLICT (owner_key, identity) DO UPDATE"
                " SET expires_at = EXCLUDED.expires_at",
                (owner_key, identity, str(ttl_seconds)),
            )
            conn.commit()

    def has_receipt(self, *, owner_key: str, identity: str) -> bool:
        """True when ``(owner_key, identity)`` holds a live (unexpired) row."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM read_receipts"
                " WHERE owner_key = %s AND identity = %s"
                " AND expires_at > now() LIMIT 1",
                (owner_key, identity),
            ).fetchone()
        return row is not None

    def prune_read_receipts(self) -> int:
        """Delete every expired receipt; return the row count removed."""
        with self.pool.connection() as conn:
            cur = conn.execute("DELETE FROM read_receipts WHERE expires_at <= now()")
            conn.commit()
        return cur.rowcount
