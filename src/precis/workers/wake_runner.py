"""``wake_runner`` — re-queue paused coordinator jobs whose wake fires.

The :mod:`coordinator` executor parks a job at every yield by
setting ``STATUS:waiting_<reason>`` and stashing the wake
condition in ``meta.wake_when``. This ref pass scans for paused
jobs whose wake condition is satisfied and re-tags them
``STATUS:queued`` so the coordinator picks them up next slice.

Five wake conditions, each a separate SELECT bounded by the pass
``limit``:

1. ``children_done`` — every job in ``wake_when.payload.child_job_ids``
   is in a terminal STATUS (``succeeded`` / ``failed`` / ``cancelled``).
2. ``at_time`` — wall-clock has reached ``wake_when.payload.ts``.
3. ``tag_cleared`` — the tag (or glob suffix-match) in
   ``wake_when.payload.tag`` is gone. Default mapping uses this
   for ``ask-user:*`` human-approval pauses.
4. ``tag_added`` — the named tag (exact match) is present.
5. ``cancel_override`` — a ``STATUS:cancel_requested`` row that
   also has ``meta.wake_when`` set. Re-queues unconditionally so
   the coordinator's cancel-poll fires on its next slice.

Cadence: piggy-backs on the system worker's idle poll (2 s by
default). Low-latency enough for human-acknowledge → resume; if
sub-second wake matters later, run wake_runner in a tighter loop.

See ``docs/design/dft-phase-0-pr-3-coordinator-executor.md`` §3.2.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import Connection

from precis.workers.executors._common import (
    CANCEL_REQUESTED as _CANCEL_REQUESTED,
)
from precis.workers.executors._common import (
    QUEUED as _QUEUED,
)
from precis.workers.executors._common import (
    STATUS_NAMESPACE as _STATUS_NAMESPACE,
)
from precis.workers.executors._common import (
    WAITING_ASK_USER as _WAITING_ASK_USER,
)
from precis.workers.executors._common import (
    WAITING_CHILDREN as _WAITING_CHILDREN,
)
from precis.workers.executors._common import (
    WAITING_MANUAL_KICK as _WAITING_MANUAL_KICK,
)
from precis.workers.executors._common import (
    WAITING_TIME as _WAITING_TIME,
)
from precis.workers.executors._common import (
    append_chunk as _append_chunk,
)
from precis.workers.executors._common import (
    set_status as _set_status,
)
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


# ── Wake-condition selectors ──────────────────────────────────────


def _wake_children_done(conn: Connection, *, limit: int) -> list[int]:
    """Find ``waiting_children`` rows whose every child is terminal.

    ``meta.wake_when.payload.child_job_ids`` is a JSON array of
    int IDs. The NOT EXISTS subquery rejects any row that still
    has a non-terminal child.
    """
    rows = conn.execute(
        """
        SELECT r.ref_id
          FROM refs r
         WHERE r.kind = 'job'
           AND r.deleted_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = %s
               )
           AND r.meta->'wake_when'->>'kind' = 'children_done'
           AND NOT EXISTS (
                 SELECT 1
                   FROM refs c
                   JOIN jsonb_array_elements_text(
                            r.meta->'wake_when'->'payload'->'child_job_ids'
                        ) AS child_id_text(child_id) ON true
                  WHERE c.ref_id = child_id_text.child_id::bigint
                    AND c.kind = 'job'
                    AND COALESCE(
                          (SELECT t.value FROM ref_tags rt
                             JOIN tags t ON t.tag_id = rt.tag_id
                            WHERE rt.ref_id = c.ref_id
                              AND t.namespace = 'STATUS'
                            LIMIT 1),
                          'open'
                        ) NOT IN ('succeeded', 'failed', 'cancelled')
               )
         ORDER BY r.ref_id
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (_STATUS_NAMESPACE, _WAITING_CHILDREN, limit),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _wake_at_time(conn: Connection, *, limit: int) -> list[int]:
    """Find ``waiting_time`` rows whose ``ts`` is in the past."""
    rows = conn.execute(
        """
        SELECT r.ref_id
          FROM refs r
         WHERE r.kind = 'job'
           AND r.deleted_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = %s
               )
           AND r.meta->'wake_when'->>'kind' = 'at_time'
           AND (r.meta->'wake_when'->'payload'->>'ts')::bigint
               <= extract(epoch from now())::bigint
         ORDER BY r.ref_id
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (_STATUS_NAMESPACE, _WAITING_TIME, limit),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _wake_tag_cleared(conn: Connection, *, limit: int) -> list[int]:
    """Find ``waiting_ask_user`` rows whose pause-tag is gone.

    The pause-tag pattern lives in ``wake_when.payload.tag``. We
    accept either an exact tag (``ask-user:propose:approve_batch``)
    or a trailing-glob (``ask-user:propose:*``). Glob match is a
    SQL LIKE with the trailing ``*`` mapped to ``%``.
    """
    rows = conn.execute(
        """
        SELECT r.ref_id, r.meta->'wake_when'->'payload'->>'tag'
          FROM refs r
         WHERE r.kind = 'job'
           AND r.deleted_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = %s
               )
           AND r.meta->'wake_when'->>'kind' = 'tag_cleared'
         ORDER BY r.ref_id
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (_STATUS_NAMESPACE, _WAITING_ASK_USER, limit),
    ).fetchall()

    ready: list[int] = []
    for ref_id, tag in rows:
        if tag is None:
            log.warning(
                "wake_runner: job %d has waiting_ask_user but no "
                "wake_when.payload.tag; treating as ready",
                ref_id,
            )
            ready.append(int(ref_id))
            continue
        if _tag_present(conn, int(ref_id), tag):
            continue
        ready.append(int(ref_id))
    return ready


def _wake_tag_added(conn: Connection, *, limit: int) -> list[int]:
    """Find ``waiting_manual_kick`` rows whose named tag is now present."""
    rows = conn.execute(
        """
        SELECT r.ref_id, r.meta->'wake_when'->'payload'->>'tag'
          FROM refs r
         WHERE r.kind = 'job'
           AND r.deleted_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = %s
               )
           AND r.meta->'wake_when'->>'kind' = 'tag_added'
         ORDER BY r.ref_id
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (_STATUS_NAMESPACE, _WAITING_MANUAL_KICK, limit),
    ).fetchall()

    ready: list[int] = []
    for ref_id, tag in rows:
        if tag is None:
            continue
        if _tag_present(conn, int(ref_id), tag):
            ready.append(int(ref_id))
    return ready


def _wake_cancel_override(conn: Connection, *, limit: int) -> list[int]:
    """Find waiting jobs whose ``STATUS:cancel_requested`` is set.

    Cancel pre-empts any other wake condition. Re-queues the job
    so the coordinator's cancel-poll fires on its next slice and
    transitions to ``STATUS:cancelled``.

    Note the STATUS::* tag is closed-prefix — setting
    ``STATUS:cancel_requested`` replaces ``STATUS:waiting_*``. The
    ``meta.wake_when`` field is what marks this row as having
    been paused by the coordinator.
    """
    rows = conn.execute(
        """
        SELECT r.ref_id
          FROM refs r
         WHERE r.kind = 'job'
           AND r.deleted_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = %s
               )
           AND r.meta ? 'wake_when'
         ORDER BY r.ref_id
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (_STATUS_NAMESPACE, _CANCEL_REQUESTED, limit),
    ).fetchall()
    return [int(r[0]) for r in rows]


# ── Helpers ───────────────────────────────────────────────────────


def _tag_present(conn: Connection, ref_id: int, tag_pattern: str) -> bool:
    """Is a tag matching ``tag_pattern`` set on ``ref_id``?

    Pattern is either a literal value or a trailing-glob ending in
    ``*``. Exact match uses ``t.value = pattern``; glob match
    uses ``LIKE`` with ``*`` mapped to ``%``. Open-namespace only
    (closed-namespace tags like ``STATUS:*`` are handled by their
    own dedicated SELECTs in each wake helper).
    """
    if tag_pattern.endswith("*"):
        prefix = tag_pattern[:-1]
        row = conn.execute(
            """
            SELECT 1
              FROM ref_tags rt
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = %s
               AND t.namespace = 'OPEN'
               AND t.value LIKE %s
             LIMIT 1
            """,
            (ref_id, prefix + "%"),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT 1
              FROM ref_tags rt
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = %s
               AND t.namespace = 'OPEN'
               AND t.value = %s
             LIMIT 1
            """,
            (ref_id, tag_pattern),
        ).fetchone()
    return row is not None


def _requeue(store: Any, ref_id: int, reason: str) -> None:
    """Transition ``ref_id`` back to ``STATUS:queued`` + audit chunk.

    Holds the connection only for the status + chunk writes so
    contention with the coordinator pass is minimal.
    """
    with store.pool.connection() as conn:
        _set_status(store, ref_id, _QUEUED, conn=conn)
        # Audit chunk so the lifecycle reads cleanly:
        # waiting_children → wake_runner: re-queued (children_done) → running → ...
        _append_chunk(
            store,
            ref_id,
            "job_event",
            f"wake_runner: re-queued ({reason})",
            conn=conn,
        )
        conn.commit()


# ── Pass entry point ──────────────────────────────────────────────


def run_wake_pass(store: Any, *, limit: int = 16) -> dict[str, int]:
    """Re-queue paused jobs whose wake conditions have fired.

    Runs five SELECTs (one per wake kind plus the cancel override),
    each bounded by ``limit``. The combined budget per pass is
    ``5 * limit`` re-queues — more generous than the coordinator
    pass's own limit because a re-queue is a status flip + chunk
    write, much cheaper than a coordinator slice.

    Returns ``{claimed, ok, failed}`` matching the ref-pass
    aggregator contract.
    """
    ok = 0
    failed = 0
    ready: list[tuple[int, str]] = []

    # All selectors run in their own short-lived connections so
    # we don't hold a long lock while running five queries.
    selectors = [
        ("cancel", _wake_cancel_override),
        ("children_done", _wake_children_done),
        ("at_time", _wake_at_time),
        ("tag_cleared", _wake_tag_cleared),
        ("tag_added", _wake_tag_added),
    ]
    seen: set[int] = set()
    with store.pool.connection() as conn:
        for label, fn in selectors:
            try:
                ids = fn(conn, limit=limit)
            except Exception:  # pragma: no cover — defensive
                log.warning(
                    "wake_runner: %s selector raised; continuing",
                    label,
                    exc_info=True,
                )
                conn.rollback()
                continue
            for ref_id in ids:
                if ref_id in seen:
                    continue
                seen.add(ref_id)
                ready.append((ref_id, label))
        conn.commit()

    for ref_id, label in ready:
        try:
            _requeue(store, ref_id, label)
            ok += 1
        except Exception:  # pragma: no cover — defensive
            failed += 1
            log.warning(
                "wake_runner: re-queue of job %d failed",
                ref_id,
                exc_info=True,
            )

    return {"claimed": len(ready), "ok": ok, "failed": failed}


def wake_pass_for_runner(store: Any, batch_size: int) -> BatchResult:
    """Adapter matching :data:`precis.workers.runner.RefPass` shape.

    The CLI wiring in ``cli/worker.py`` registers this closure
    directly so the round-robin loop's logging path sees the
    canonical ``(claimed, ok, failed)`` shape.
    """
    r = run_wake_pass(store, limit=batch_size)
    return BatchResult(
        handler="wake_runner",
        claimed=r["claimed"],
        ok=r["ok"],
        failed=r["failed"],
    )


__all__ = ["run_wake_pass", "wake_pass_for_runner"]
