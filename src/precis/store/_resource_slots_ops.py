"""``resource_slots`` CRUD — the factory scheduler's per-host resource map.

Mixin on :class:`precis.store.Store`. Backs slice 6 of
``docs/backlog/factory-console-and-scheduling.md`` (§5). Migration
``0073_resource_slots.sql`` defines the table: one row per
``(host, resource)`` the host offers, with a materialized ``free`` counter.

6b's only writer is :meth:`sync_host_resource_slots`, called by the
``heartbeat`` reporter with the self-probe's verdict. The atomic
reserve/release helpers slice 6c adds land here too; 6b is upsert + read.

``resource_slot_holds`` (migration ``0119_resource_slot_holds.sql``) is a
TTL lease alongside the bare ``free`` counter, closing the crash-leak gap a
2026-08-10 fleet-wide outage exposed: a holder killed between reserve and
release never refunds. :func:`insert_slot_hold`/:func:`delete_slot_hold`
bracket a reservation's lifetime; :func:`reclaim_expired_slot_holds` (swept
by the heartbeat pass) deletes expired holds and refunds their units.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection


@dataclass(frozen=True, slots=True)
class ResourceSlot:
    """One row from ``resource_slots``.

    ``free`` is the live slot count (``capacity - Σ reservations``). Each
    reservation is backed by a TTL ``resource_slot_holds`` row so a crashed
    holder's unit is reclaimed (refunded) rather than leaked forever.
    """

    host: str
    resource: str
    capacity: int
    free: int
    kind: str
    updated_at: datetime


def _row_to_slot(row: tuple[Any, ...]) -> ResourceSlot:
    return ResourceSlot(
        host=str(row[0]),
        resource=str(row[1]),
        capacity=int(row[2]),
        free=int(row[3]),
        kind=str(row[4]),
        updated_at=row[5],
    )


_SLOT_COLS = "host, resource, capacity, free, kind, updated_at"

# UPSERT one capability row. On insert ``free = capacity`` (all free). On
# conflict, adjust ``free`` by the capacity delta so a capacity change
# (e.g. max_parallel 2→4) grows/shrinks the live counter without stomping
# any reservation slice 6c holds; LEAST clamps to satisfy the free<=capacity
# CHECK even if a prior invariant slipped. ``capacity`` and ``free`` seed
# from the same value on insert.
_UPSERT_SLOT = (
    "INSERT INTO resource_slots (host, resource, capacity, free, kind) "
    "VALUES (%s, %s, %s, %s, %s) "
    "ON CONFLICT (host, resource) DO UPDATE SET "
    "  capacity = EXCLUDED.capacity, "
    "  free = LEAST(EXCLUDED.capacity, "
    "               resource_slots.free "
    "                 + (EXCLUDED.capacity - resource_slots.capacity)), "
    "  kind = EXCLUDED.kind, "
    "  updated_at = now()"
)

_DELETE_SLOT = "DELETE FROM resource_slots WHERE host = %s AND resource = %s"

# UPSERT a *soft* gauge row (6d-deferred): unlike the hard capability path,
# ``free`` is the measured headroom set directly (a gauge, not a counter jobs
# decrement), so it must NOT go through the delta-adjust logic above. Clamped
# to satisfy the free<=capacity CHECK.
_UPSERT_GAUGE = (
    "INSERT INTO resource_slots (host, resource, capacity, free, kind) "
    "VALUES (%s, %s, %s, %s, %s) "
    "ON CONFLICT (host, resource) DO UPDATE SET "
    "  capacity = EXCLUDED.capacity, "
    "  free = LEAST(EXCLUDED.capacity, EXCLUDED.free), "
    "  kind = EXCLUDED.kind, "
    "  updated_at = now()"
)

#: Retract a soft gauge (e.g. ``container_agent`` after a host opts out). Scoped
#: to ``kind='soft'`` so it can never delete a same-named hard capability row.
_DELETE_GAUGE = (
    "DELETE FROM resource_slots WHERE host = %s AND resource = %s AND kind = 'soft'"
)


# ── Reserve / release (slice 6c) ──────────────────────────────────────────
#
# Module-level, connection-based so the claim path (which holds a raw
# ``conn``, not a ``Store``) can reserve inside the same transaction as the
# job lock — the conditional decrement IS the lock, no separate row needed.

# Conditional decrement: succeeds only while ``free >= units`` (hard
# discipline — refuse past 0). Zero rows back = shortfall OR the host
# doesn't offer the resource at all (no row) → the job isn't claimable here.
_RESERVE_ONE = (
    "UPDATE resource_slots SET free = free - %s "
    "WHERE host = %s AND resource = %s AND free >= %s "
    "RETURNING host"
)

# Give units back, capped at capacity so a double-release (terminal +
# sweeper both firing) can never inflate free past the real ceiling.
_RELEASE_ONE = (
    "UPDATE resource_slots SET free = LEAST(capacity, free + %s) "
    "WHERE host = %s AND resource = %s"
)


def reserve_resource_slots(
    conn: Connection, host: str, requirements: dict[str, int]
) -> bool:
    """All-or-nothing hard reservation on ``host`` inside ``conn``'s tx.

    Decrements ``free`` for each ``resource: units`` requirement. If any
    can't be satisfied (insufficient free, or the host offers no such
    resource), the ones already taken this call are refunded and ``False``
    is returned — so a multi-resource job never holds a partial
    reservation. ``True`` means every requirement is reserved; the caller
    must record what it reserved (``meta.reserved``) so it can be released.
    """
    taken: list[tuple[str, int]] = []
    for resource, units in requirements.items():
        u = int(units)
        row = conn.execute(_RESERVE_ONE, (u, host, resource, u)).fetchone()
        if row is None:
            for r2, u2 in taken:
                conn.execute(_RELEASE_ONE, (u2, host, r2))
            return False
        taken.append((resource, u))
    return True


def release_resource_slots(
    conn: Connection, host: str, requirements: dict[str, int]
) -> None:
    """Refund a prior reservation on ``host`` (``free += units``, capped)."""
    for resource, units in requirements.items():
        conn.execute(_RELEASE_ONE, (int(units), host, resource))


# ── Crash-safe reclaim (holds ledger, 0118) ───────────────────────────────
#
# Every reservation the local-serving path takes also opens a TTL hold row
# here; the heartbeat pass sweeps expired ones and refunds their units, so a
# holder killed before release() self-heals within one TTL instead of
# leaking `free` forever (2026-08-10 fleet-wide outage).

_INSERT_HOLD = (
    "INSERT INTO resource_slot_holds "
    "(host, resource, units, holder, expires_at, "
    " holder_host, holder_process, holder_boot_id) "
    "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s), %s, %s, %s) "
    "RETURNING id"
)

_DELETE_HOLD = "DELETE FROM resource_slot_holds WHERE id = %s RETURNING id"

# CTE: delete every expired hold, group its units by (host, resource), and
# refund each group in one UPDATE — capped at capacity, same discipline as
# `_RELEASE_ONE`, so a hold whose refund already happened via a normal
# release (this sweep runs first) can't inflate `free` past the ceiling.
# `resource_slots` may lack the row entirely (deleted/reseeded by the
# capability probe) — the join drops those groups, nothing to refund, but
# the hold is still deleted and still counted. The final SELECT references
# both writable CTEs (via scalar subqueries) so both always execute and the
# returned count is unconditional — independent of whether any refund UPDATE
# actually matched a row.
_RECLAIM_EXPIRED_HOLDS = (
    "WITH expired AS ("
    "  DELETE FROM resource_slot_holds WHERE expires_at < now() "
    "  RETURNING host, resource, units"
    "), grouped AS ("
    "  SELECT host, resource, SUM(units) AS units_sum FROM expired "
    "  GROUP BY host, resource"
    "), refunded AS ("
    "  UPDATE resource_slots s SET free = LEAST(s.capacity, s.free + g.units_sum) "
    "  FROM grouped g "
    "  WHERE s.host = g.host AND s.resource = g.resource "
    "  RETURNING s.host"
    ") "
    "SELECT (SELECT COUNT(*) FROM expired), (SELECT COUNT(*) FROM refunded)"
)


def insert_slot_hold(
    conn: Connection,
    host: str,
    resource: str,
    units: int,
    holder: str,
    ttl_s: float,
    *,
    holder_identity: tuple[str | None, str | None, str | None] | None = None,
) -> int:
    """Open a TTL hold bracketing a reservation. ``holder`` is a free-text
    identity (``host:pid``) for operator debugging only — reclaim doesn't
    consult it. ``holder_identity`` is the ``(boot_id, process, host)``
    triple from :func:`precis.liveness.worker_identity`: when non-NULL the
    reaper's epoch arm can reclaim this hold the moment the holder's
    generation is provably replaced, instead of waiting out the TTL; NULL
    (CLI / unadvertised worker) keeps TTL-only reclaim. Returns the new
    hold's id."""
    boot_id, process, holder_host = holder_identity or (None, None, None)
    row = conn.execute(
        _INSERT_HOLD,
        (
            host,
            resource,
            int(units),
            holder,
            float(ttl_s),
            holder_host,
            process,
            boot_id,
        ),
    ).fetchone()
    assert row is not None  # INSERT ... RETURNING always yields a row
    return int(row[0])


def delete_slot_hold(conn: Connection, hold_id: int) -> bool:
    """Close a hold on normal release. ``True`` iff a row was deleted — a
    miss means the sweep already reclaimed it (the refund already
    happened), so the caller must NOT refund again."""
    return conn.execute(_DELETE_HOLD, (hold_id,)).fetchone() is not None


def reclaim_expired_slot_holds(conn: Connection) -> int:
    """Delete every expired hold and refund its units back to
    ``resource_slots.free`` (grouped + capped at capacity in one
    statement). Returns the number of expired holds deleted."""
    row = conn.execute(_RECLAIM_EXPIRED_HOLDS).fetchone()
    return int(row[0]) if row is not None else 0


class ResourceSlotsMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    def sync_host_resource_slots(
        self,
        host: str,
        slots: dict[str, int | None],
        *,
        kinds: dict[str, str] | None = None,
        conn: Connection | None = None,
    ) -> None:
        """Reconcile one host's advertised resources to the probe verdict.

        ``slots`` maps ``resource -> capacity|None`` (the self-probe output):

        * ``capacity > 0`` — UPSERT the row (present, this many slots).
        * ``capacity == 0`` — DELETE the row (definitively absent → stop
          advertising the capability).
        * ``None`` — leave any existing row untouched (probe couldn't tell;
          a transient failure must not retract a real capability).

        ``kinds`` overrides the reservation discipline per resource
        (default ``hard``). Runs in one transaction so a heartbeat presents
        a consistent map. Resources the probe didn't evaluate are not
        mentioned and so are left alone.
        """
        kinds = kinds or {}

        def _apply(c: Connection) -> None:
            for resource, capacity in slots.items():
                if capacity is None:
                    continue  # unknown — do not touch the row
                if capacity <= 0:
                    c.execute(_DELETE_SLOT, (host, resource))
                    continue
                c.execute(
                    _UPSERT_SLOT,
                    (host, resource, capacity, capacity, kinds.get(resource, "hard")),
                )

        if conn is not None:
            _apply(conn)
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    _apply(c)

    def sync_soft_signal(
        self,
        host: str,
        resource: str,
        free: int | None,
        capacity: int,
        *,
        conn: Connection | None = None,
    ) -> None:
        """Write a soft advisory gauge (memory pressure, 6d-deferred).

        ``free`` is the measured headroom (``0`` = under pressure … ``capacity``
        = plenty), set directly — soft rows are a gauge the claim reads as a
        veto, not a counter jobs reserve against. ``free is None`` (unmeasurable)
        leaves any existing row untouched, matching the hard-probe discipline.
        """
        if free is None:
            return

        def _apply(c: Connection) -> None:
            c.execute(_UPSERT_GAUGE, (host, resource, capacity, max(0, free), "soft"))

        if conn is not None:
            _apply(conn)
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    _apply(c)

    def delete_soft_signal(
        self, host: str, resource: str, *, conn: Connection | None = None
    ) -> None:
        """Retract a soft gauge row for a host — idempotent (absent → no-op).

        The soft counterpart to the hard path's delete-on-absent discipline:
        :meth:`sync_soft_signal` can only leave a row (``free is None``) or set
        it, never remove it, so a gauge that becomes *definitively* absent (a
        host that opted out of ``PRECIS_AGENT_CONTAINER`` no longer advertising
        ``container_agent``) would otherwise render a stale chip on ``/factory``
        forever. Scoped to ``kind='soft'`` so a hard row is never touched."""

        def _apply(c: Connection) -> None:
            c.execute(_DELETE_GAUGE, (host, resource))

        if conn is not None:
            _apply(conn)
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    _apply(c)

    def resource_slots_for_host(self, host: str) -> list[ResourceSlot]:
        """This host's advertised resources, ordered by resource name."""
        sql = (
            f"SELECT {_SLOT_COLS} FROM resource_slots WHERE host = %s ORDER BY resource"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (host,)).fetchall()
        return [_row_to_slot(r) for r in rows]

    def all_resource_slots(self) -> list[ResourceSlot]:
        """Every host's advertised resources, ordered by host then resource."""
        sql = f"SELECT {_SLOT_COLS} FROM resource_slots ORDER BY host, resource"
        with self.pool.connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [_row_to_slot(r) for r in rows]

    def reconcile_llm_served_slots(
        self, desired: dict[tuple[str, str], int]
    ) -> tuple[int, int]:
        """Reconcile the ``llm:<model>`` resource rows to the catalog's declared
        local serving (slice 7 / §6). ``desired`` maps ``(host, resource)`` →
        capacity, where ``resource`` is ``"llm:<model_id>"`` and capacity is the
        ``served_by`` entry's ``max_parallel``.

        A full sync scoped to the ``llm:`` namespace: every desired row is
        UPSERTed (reservation-safe — the delta-adjust preserves any slice-6c
        reservation across a capacity change), and any existing ``llm:`` row NOT
        in ``desired`` is DELETEd (a card stopped serving, or a ``served_by``
        entry was removed). Hardware rows (gpu/podman/tts/mem) are untouched —
        the namespace prefix keeps the two seeding sources from colliding.

        Returns ``(upserted, deleted)``.
        """
        with self.pool.connection() as conn:
            with conn.transaction():
                for (host, resource), capacity in desired.items():
                    cap = max(1, int(capacity))
                    conn.execute(_UPSERT_SLOT, (host, resource, cap, cap, "hard"))
                # Delete stale llm: rows no longer declared.
                existing = conn.execute(
                    "SELECT host, resource FROM resource_slots "
                    "WHERE resource LIKE 'llm:%'"
                ).fetchall()
                deleted = 0
                for host, resource in existing:
                    if (str(host), str(resource)) not in desired:
                        conn.execute(_DELETE_SLOT, (host, resource))
                        deleted += 1
        return len(desired), deleted

    def reclaim_expired_slot_holds(self) -> int:
        """Sweep expired ``resource_slot_holds`` and refund their units
        (heartbeat pass, best-effort self-heal for a crashed holder)."""
        with self.pool.connection() as conn:
            with conn.transaction():
                return reclaim_expired_slot_holds(conn)


__all__ = [
    "ResourceSlot",
    "ResourceSlotsMixin",
    "delete_slot_hold",
    "insert_slot_hold",
    "reclaim_expired_slot_holds",
    "release_resource_slots",
    "reserve_resource_slots",
]
