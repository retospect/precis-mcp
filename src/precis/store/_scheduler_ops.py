"""``scheduler_leases`` claim — the decentralized recurring-work trigger.

Mixin on :class:`precis.store.Store`. Backs slice 10 of
``docs/design/factory-console-and-scheduling.md`` (§15i). Migration
``0074_scheduler_leases.sql`` defines the table: one row per folded
thin-timer cadence, carrying a ``next_fire_at`` lease clock.

The one writer is :meth:`SchedulerLeasesMixin.claim_scheduler_lease`, called
by the ``scheduler`` worker pass once per cadence per cycle. It is the
reserve-at-claim pattern (§5.2) applied to *time*: an atomic conditional
advance where the ``UPDATE`` matching the row IS the lock. Every worker runs
the pass; exactly one wins each due cadence; a down worker never drops a fire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection

#: Seed a missing lease as immediately-due (``next_fire_at = now()``), matching
#: the launchd timers' ``RunAtLoad=true`` — one fire right after the flag flips,
#: then every ``interval_s``. Only one worker's conditional advance wins it.
_SEED_LEASE = (
    "INSERT INTO scheduler_leases (name, interval_s, next_fire_at) "
    "VALUES (%s, %s, now()) ON CONFLICT (name) DO NOTHING"
)

#: The advance IS the lock (§15i): only the worker whose ``UPDATE`` matches the
#: ``next_fire_at <= now()`` predicate fires; the rest see the row already
#: advanced. ``now() + interval`` (not ``next_fire_at + interval``) collapses a
#: fleet-wide outage to a single catch-up fire, not a backlog burst. The
#: interval is a code fact (passed in), so a cadence change takes immediate
#: effect and ``interval_s`` is kept in sync for the console.
_ADVANCE_LEASE = (
    "UPDATE scheduler_leases "
    "SET next_fire_at = now() + make_interval(secs => %s), "
    "    interval_s = %s, last_fired_at = now(), last_host = %s "
    "WHERE name = %s AND next_fire_at <= now() "
    "RETURNING name"
)

_LEASE_COLS = "name, interval_s, next_fire_at, last_fired_at, last_host"


@dataclass(frozen=True, slots=True)
class SchedulerLease:
    """One row from ``scheduler_leases`` — a cadence's lease clock."""

    name: str
    interval_s: int
    next_fire_at: datetime
    last_fired_at: datetime | None
    last_host: str | None


def _row_to_lease(row: tuple[Any, ...]) -> SchedulerLease:
    return SchedulerLease(
        name=str(row[0]),
        interval_s=int(row[1]),
        next_fire_at=row[2],
        last_fired_at=row[3],
        last_host=None if row[4] is None else str(row[4]),
    )


class SchedulerLeasesMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    def claim_scheduler_lease(
        self,
        name: str,
        interval_s: int,
        host: str,
        *,
        conn: Connection | None = None,
    ) -> bool:
        """Try to claim cadence ``name`` for one fire. Returns ``True`` iff this
        caller won it (its conditional advance matched the due row).

        Seeds a missing lease as immediately-due, then does the atomic advance.
        Decentralized + exactly-once: run concurrently on every worker, only one
        gets ``True`` per interval; the losers get ``False`` and skip.
        """

        def _apply(c: Connection) -> bool:
            c.execute(_SEED_LEASE, (name, interval_s))
            row = c.execute(
                _ADVANCE_LEASE, (interval_s, interval_s, host, name)
            ).fetchone()
            return row is not None

        if conn is not None:
            return _apply(conn)
        with self.pool.connection() as c:
            with c.transaction():
                return _apply(c)

    def scheduler_leases(self) -> list[SchedulerLease]:
        """Every cadence's lease clock, ordered by name (console + tests)."""
        sql = f"SELECT {_LEASE_COLS} FROM scheduler_leases ORDER BY name"
        with self.pool.connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [_row_to_lease(r) for r in rows]


__all__ = [
    "SchedulerLease",
    "SchedulerLeasesMixin",
]
