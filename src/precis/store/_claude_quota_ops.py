"""Claude.ai OAuth quota snapshot CRUD against ``claude_quota_snapshot``.

Mixin on :class:`precis.store.Store`. Single-row-per-scope table; one
``UPSERT`` on every refresh + a read for the Status tab. See migration
0020 for schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb


@dataclass(frozen=True, slots=True)
class ClaudeQuotaRow:
    """One row from ``claude_quota_snapshot``.

    ``data`` carries the parsed snapshot envelope:
    ``{"windows": {...}, "representative_claim": "..." | null}``.
    Empty dict when nothing has been written yet for the scope.
    """

    scope: str
    ts: datetime
    data: dict[str, Any]


class ClaudeQuotaMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    def record_claude_quota(
        self,
        *,
        scope: str,
        data: dict[str, Any],
        conn: Connection | None = None,
    ) -> None:
        """Upsert the latest quota snapshot for a scope (default 'unified')."""
        sql = (
            "INSERT INTO claude_quota_snapshot (scope, ts, data) "
            "VALUES (%s, now(), %s) "
            "ON CONFLICT (scope) DO UPDATE SET "
            "ts = now(), data = EXCLUDED.data"
        )
        params = (scope, Jsonb(data))
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    c.execute(sql, params)

    def read_claude_quota(self, scope: str = "unified") -> ClaudeQuotaRow | None:
        """Return the snapshot for ``scope``, or ``None`` if nothing yet."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT scope, ts, data FROM claude_quota_snapshot WHERE scope = %s",
                (scope,),
            ).fetchone()
        if row is None:
            return None
        return ClaudeQuotaRow(
            scope=str(row[0]),
            ts=row[1],
            data=dict(row[2] or {}),
        )


__all__ = ["ClaudeQuotaMixin", "ClaudeQuotaRow"]
