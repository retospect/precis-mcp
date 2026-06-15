"""Host heartbeat CRUD against the ``host_heartbeat`` table.

Mixin on :class:`precis.store.Store`. Backs the per-host liveness +
sensor snapshot the web Status tab renders (CPU temperature, load
average, last-seen). Migration ``0017_host_heartbeat.sql`` defines
the table as latest-snapshot-per-host (``host`` primary key); the
reporter (``precis heartbeat``) UPSERTs.

Two helpers:

- :meth:`record_heartbeat` — UPSERT one host's snapshot. The
  reporter's only write.
- :meth:`recent_heartbeats` — read all snapshots, ordered by host.
  Used by db-backed tests and any future ``precis status`` CLI; the
  web layer reads the same table via raw SQL so its fake-store tests
  need no method.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb


@dataclass(frozen=True, slots=True)
class HostHeartbeat:
    """One row from ``host_heartbeat``.

    ``temp_c`` and the ``load*`` fields are ``None`` when the
    reporting host couldn't read them (macOS without a temp command,
    a platform without ``getloadavg``). ``meta`` is the parsed JSONB
    dict (empty dict when NULL — readers don't special-case).
    """

    host: str
    ts: datetime
    temp_c: float | None
    load1: float | None
    load5: float | None
    load15: float | None
    meta: dict[str, Any]


def _row_to_heartbeat(row: tuple[Any, ...]) -> HostHeartbeat:
    return HostHeartbeat(
        host=str(row[0]),
        ts=row[1],
        temp_c=float(row[2]) if row[2] is not None else None,
        load1=float(row[3]) if row[3] is not None else None,
        load5=float(row[4]) if row[4] is not None else None,
        load15=float(row[5]) if row[5] is not None else None,
        meta=dict(row[6] or {}),
    )


_HEARTBEAT_COLS = "host, ts, temp_c, load1, load5, load15, meta"


class HeartbeatMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    def record_heartbeat(
        self,
        host: str,
        *,
        temp_c: float | None = None,
        load1: float | None = None,
        load5: float | None = None,
        load15: float | None = None,
        meta: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> None:
        """UPSERT one host's snapshot, stamping ``ts = now()``.

        Re-running for the same ``host`` overwrites the previous row
        (latest-snapshot semantics) and bumps ``ts`` so staleness is
        always measured from the most recent report.
        """
        sql = (
            "INSERT INTO host_heartbeat "
            "(host, ts, temp_c, load1, load5, load15, meta) "
            "VALUES (%s, now(), %s, %s, %s, %s, %s) "
            "ON CONFLICT (host) DO UPDATE SET "
            "ts = now(), temp_c = EXCLUDED.temp_c, load1 = EXCLUDED.load1, "
            "load5 = EXCLUDED.load5, load15 = EXCLUDED.load15, "
            "meta = EXCLUDED.meta"
        )
        params = (
            host,
            temp_c,
            load1,
            load5,
            load15,
            Jsonb(meta) if meta is not None else None,
        )
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    c.execute(sql, params)

    def recent_heartbeats(self) -> list[HostHeartbeat]:
        """Return every host's latest snapshot, ordered by host name."""
        sql = f"SELECT {_HEARTBEAT_COLS} FROM host_heartbeat ORDER BY host"
        with self.pool.connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [_row_to_heartbeat(r) for r in rows]


__all__ = ["HeartbeatMixin", "HostHeartbeat"]
