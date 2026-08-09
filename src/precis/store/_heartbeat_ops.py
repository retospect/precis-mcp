"""Host heartbeat CRUD against the ``host_heartbeat`` table.

Mixin on :class:`precis.store.Store`. Backs the per-host liveness +
sensor snapshot the web Status tab renders (CPU temperature, load
average, last-seen). Migration ``0017_host_heartbeat.sql`` defines
the table as latest-snapshot-per-host (``host`` primary key); the
reporter (``precis heartbeat``) UPSERTs.

Helpers:

- :meth:`record_heartbeat` — UPSERT one host's snapshot into
  ``host_heartbeat``.
- :meth:`recent_heartbeats` — read all snapshots, ordered by host.
  Used by db-backed tests and any future ``precis status`` CLI; the
  web layer reads the same table via raw SQL so its fake-store tests
  need no method.
- :meth:`record_heartbeat_history` / :meth:`heartbeat_history` — the
  append-only time-series companion (``host_heartbeat_log``, migration
  0113): one narrow row per beat, pruned to a retention window in the
  same transaction, read back as hourly per-host rollups by
  ``precis stats --utilization``.
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
        always measured from the most recent report — EXCEPT
        ``meta.boot_ids`` (the worker boot epoch),
        which is nested-merged instead of replaced: a host can run more
        than one worker process (melchior runs both ``system`` and
        ``agent``), each advertising its own boot_id under the SAME
        ``host_heartbeat`` row (PK is ``host``, not ``(host, process)``),
        so a full ``meta`` replace by process A's beat would silently wipe
        process B's last-advertised boot_id. Merging old + new
        ``boot_ids`` (new entry wins per-process key) keeps every live
        process's generation visible regardless of write order.
        """
        sql = (
            "INSERT INTO host_heartbeat "
            "(host, ts, temp_c, load1, load5, load15, meta) "
            "VALUES (%s, now(), %s, %s, %s, %s, %s) "
            "ON CONFLICT (host) DO UPDATE SET "
            "ts = now(), temp_c = EXCLUDED.temp_c, load1 = EXCLUDED.load1, "
            "load5 = EXCLUDED.load5, load15 = EXCLUDED.load15, "
            "meta = EXCLUDED.meta || jsonb_build_object("
            "  'boot_ids',"
            "  COALESCE(host_heartbeat.meta->'boot_ids', '{}'::jsonb)"
            "    || COALESCE(EXCLUDED.meta->'boot_ids', '{}'::jsonb)"
            ")"
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

    def record_heartbeat_history(
        self,
        host: str,
        *,
        temp_c: float | None = None,
        load1: float | None = None,
        load5: float | None = None,
        load15: float | None = None,
        retention_days: float = 14.0,
    ) -> None:
        """Append one beat to ``host_heartbeat_log`` and prune the window.

        The INSERT + retention DELETE share one transaction so the table
        stays self-maintaining without a separate prune pass — at one beat
        per host per minute the DELETE (index on ``ts``) is trivially
        cheap. ``retention_days <= 0`` disables history entirely (no row
        written, nothing pruned).
        """
        if retention_days <= 0:
            return
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO host_heartbeat_log "
                    "(host, ts, temp_c, load1, load5, load15) "
                    "VALUES (%s, now(), %s, %s, %s, %s)",
                    (host, temp_c, load1, load5, load15),
                )
                conn.execute(
                    "DELETE FROM host_heartbeat_log "
                    "WHERE ts < now() - (%s || ' days')::interval",
                    (str(retention_days),),
                )

    def heartbeat_history(self, *, hours: float = 24.0) -> list[dict[str, Any]]:
        """Hourly per-host rollup of ``host_heartbeat_log``.

        One dict per (hour, host) in the trailing ``hours`` window:
        ``{"hr", "host", "beats", "load1_avg", "load1_max", "temp_max"}``,
        ordered by hour then host — the CPU half of
        ``precis stats --utilization``.
        """
        sql = (
            "SELECT date_trunc('hour', ts) AS hr, host, "
            "       count(*)::int AS beats, "
            "       avg(load1) AS load1_avg, "
            "       max(load1) AS load1_max, "
            "       max(temp_c) AS temp_max "
            "FROM host_heartbeat_log "
            "WHERE ts > now() - (%s || ' hours')::interval "
            "GROUP BY 1, 2 ORDER BY 1, 2"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (str(hours),)).fetchall()
        return [
            {
                "hr": r[0],
                "host": r[1],
                "beats": int(r[2]),
                "load1_avg": float(r[3]) if r[3] is not None else None,
                "load1_max": float(r[4]) if r[4] is not None else None,
                "temp_max": float(r[5]) if r[5] is not None else None,
            }
            for r in rows
        ]


__all__ = ["HeartbeatMixin", "HostHeartbeat"]
