"""``resource_slots`` CRUD — the factory scheduler's per-host resource map.

Mixin on :class:`precis.store.Store`. Backs slice 6 of
``docs/design/factory-console-and-scheduling.md`` (§5). Migration
``0073_resource_slots.sql`` defines the table: one row per
``(host, resource)`` the host offers, with a materialized ``free`` counter.

6b's only writer is :meth:`sync_host_resource_slots`, called by the
``heartbeat`` reporter with the self-probe's verdict. The atomic
reserve/release helpers slice 6c adds land here too; 6b is upsert + read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import Connection


@dataclass(frozen=True, slots=True)
class ResourceSlot:
    """One row from ``resource_slots``.

    ``free`` is the live slot count (``capacity - Σ reservations``); until
    slice 6c wires reserve-at-claim it always equals ``capacity``.
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


__all__ = [
    "ResourceSlot",
    "ResourceSlotsMixin",
    "release_resource_slots",
    "reserve_resource_slots",
]
