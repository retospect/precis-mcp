"""Event-log CRUD against the v2 ``ref_events`` table. Mixin on
:class:`precis.store.Store`.

Backs the cross-subsystem per-ref audit trail defined in
``0001_initial.sql`` (originally introduced in the archived
``0009_ref_events.sql``). Every long-lived worker / API consumer
writes events here; consumers read either chronologically per ref
(``view='log'`` on a handler) or cross-ref by ``(source, event)``
for incident timelines.

Two write helpers:

- :meth:`append_event` — single-row insert; the typical case.
- :meth:`append_events` — bulk insert; useful when a worker pass
  produces many events and wants one round-trip.

Two read helpers:

- :meth:`events_for` — chronological per ref, optionally filtered
  by source / event.
- :meth:`recent_events` — cross-ref by source, ordered ts DESC.

All event slugs are free-text. Convention: ``<subsystem>`` or
``<subsystem>:<provider>`` for ``source``; the verb that happened
for ``event``. See :file:`docs/design/finding-chase.md` §"LLM
hooks" for the chase vocabulary; future subsystems pick their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb


@dataclass(frozen=True, slots=True)
class RefEvent:
    """One row from ``ref_events``.

    ``payload`` is the parsed JSONB dict (empty dict when the column
    is NULL — readers don't have to special-case). ``cost_usd`` is a
    float at the Python edge — Postgres ``NUMERIC`` round-trips
    through psycopg as :class:`Decimal` but float is what every
    caller actually wants.
    """

    event_id: int
    ref_id: int
    ts: datetime
    source: str
    event: str
    payload: dict[str, Any]
    duration_ms: int | None
    cost_usd: float | None


def _row_to_event(row: tuple[Any, ...]) -> RefEvent:
    cost = row[7]
    if isinstance(cost, Decimal):
        cost = float(cost)
    return RefEvent(
        event_id=int(row[0]),
        ref_id=int(row[1]),
        ts=row[2],
        source=str(row[3]),
        event=str(row[4]),
        payload=dict(row[5] or {}),
        duration_ms=int(row[6]) if row[6] is not None else None,
        cost_usd=cost,
    )


_EVENT_COLS = "event_id, ref_id, ts, source, event, payload, duration_ms, cost_usd"


class EventsMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    # ── write ──────────────────────────────────────────────────────

    def append_event(
        self,
        ref_id: int,
        *,
        source: str,
        event: str,
        payload: dict[str, Any] | None = None,
        duration_ms: int | None = None,
        cost_usd: float | None = None,
        conn: Connection | None = None,
    ) -> int:
        """Append one row to ``ref_events`` and return its ``event_id``.

        ``conn=`` lets the caller participate in an outer transaction
        (e.g. write a chase event as part of the same tx that flips
        the STATUS tag). Without ``conn``, runs on a one-shot
        connection from the pool.

        ``payload`` accepts any JSON-encodable dict; passing ``None``
        stores NULL.
        """
        sql = (
            "INSERT INTO ref_events "
            "(ref_id, source, event, payload, duration_ms, cost_usd) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING event_id"
        )
        params = (
            ref_id,
            source,
            event,
            Jsonb(payload) if payload is not None else None,
            duration_ms,
            cost_usd,
        )
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, params).fetchone()
        assert row is not None
        return int(row[0])

    # ── read ───────────────────────────────────────────────────────

    def events_for(
        self,
        ref_id: int,
        *,
        source: str | None = None,
        event: str | None = None,
        limit: int = 100,
    ) -> list[RefEvent]:
        """Return events for ``ref_id`` newest-first.

        Optional ``source`` and ``event`` filters compose with AND.
        ``limit`` caps the result; the per-ref index is sorted by
        ``ts DESC`` so this is a cheap range scan.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        where = ["ref_id = %s"]
        params: list[Any] = [ref_id]
        if source is not None:
            where.append("source = %s")
            params.append(source)
        if event is not None:
            where.append("event = %s")
            params.append(event)
        params.append(limit)
        sql = (
            f"SELECT {_EVENT_COLS} FROM ref_events "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY ts DESC LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def recent_events(
        self,
        *,
        source: str,
        event: str | None = None,
        limit: int = 100,
    ) -> list[RefEvent]:
        """Return recent events across all refs for one ``source``.

        Used by ``precis stubs`` (latest fetcher event per ref),
        cost roll-ups, and incident timelines. Uses the
        ``(source, event, ts DESC)`` compound index.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        where = ["source = %s"]
        params: list[Any] = [source]
        if event is not None:
            where.append("event = %s")
            params.append(event)
        params.append(limit)
        sql = (
            f"SELECT {_EVENT_COLS} FROM ref_events "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY ts DESC LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]


__all__ = ["EventsMixin", "RefEvent"]
