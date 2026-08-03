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

**Child-deadlock deadline (§H piece 5, ``docs/proposals/compute-lane-
lease-epoch.md`` — a SIXTH, distinct step).** A ``children_done`` park's
wake condition can never fire at all if one of its children never reaches
a terminal STATUS on its own (e.g. permanently unschedulable — no live
executor ever claims it). The ``coordinator`` executor stamps
``meta.wake_deadline`` at park time (see ``executors/coordinator.py``); a
row still ``waiting_children`` past that deadline is re-queued "woken-
degraded" — a ``child-failed:<id>`` bubble tag on the coordinator job's
OWN PARENT (never on the coordinator job itself — ``child-failed:`` is a
doable-exclusion tag, and the coordinator's own claim excludes rows that
carry one; see :func:`_requeue_degraded`) for every still non-terminal
child (visibility, not a forced fail: the child's own executor/sweeper
still owns its terminalization), plus ``meta.degraded_children`` /
``meta.wake_degraded_at`` on the coordinator job itself, then
``STATUS:queued`` so the coordinator's next slice resumes control and
decides what to do about the stragglers. The master's "a parent never
blocks forever on a child" — now by design, not by sweeper accident.

Cadence: piggy-backs on the system worker's idle poll (2 s by
default). Low-latency enough for human-acknowledge → resume; if
sub-second wake matters later, run wake_runner in a tighter loop.

See ``docs/design/dft-phase-0-pr-3-coordinator-executor.md`` §3.2.
"""

from __future__ import annotations

import logging
import time
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
    set_meta as _set_meta,
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
    has a *live*, non-terminal child. A soft-deleted child
    (``deleted_at`` set; its tags persist) counts as terminal /
    absent — matching the hard-delete behaviour documented in
    ``docs/design/dft-phase-0-pr-3-coordinator-executor.md`` — so
    an operator soft-deleting a stuck child unblocks the wake
    instead of parking the coordinator forever.
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
                    AND c.deleted_at IS NULL
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


def _non_terminal_children(conn: Connection, child_job_ids: list[int]) -> list[int]:
    """Which of ``child_job_ids`` are NOT yet terminal (§H piece 5).

    Same terminal predicate as :func:`_wake_children_done`'s ``NOT
    EXISTS`` (a soft-deleted or gone child counts as terminal/absent — it
    won't appear here). Used only by the deadline path below; the normal
    all-terminal wake stays a single query.
    """
    if not child_job_ids:
        return []
    rows = conn.execute(
        """
        SELECT c.ref_id
          FROM refs c
         WHERE c.ref_id = ANY(%s)
           AND c.kind = 'job'
           AND c.deleted_at IS NULL
           AND COALESCE(
                 (SELECT t.value FROM ref_tags rt
                    JOIN tags t ON t.tag_id = rt.tag_id
                   WHERE rt.ref_id = c.ref_id
                     AND t.namespace = 'STATUS'
                   LIMIT 1),
                 'open'
               ) NOT IN ('succeeded', 'failed', 'cancelled')
         ORDER BY c.ref_id
        """,
        (child_job_ids,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _wake_children_deadline_exceeded(
    conn: Connection, *, limit: int
) -> list[tuple[int, list[int]]]:
    """Find ``waiting_children`` rows whose ``meta.wake_deadline`` has
    passed with at least one child STILL non-terminal (§H piece 5,
    compute-lane-lease-epoch.md — "a parent never blocks forever on a
    child"). Returns ``(ref_id, still_non_terminal_child_ids)`` so the
    caller can bubble a ``child-failed:<id>`` marker per straggler before
    re-queueing "woken-degraded".

    A row whose deadline has passed but whose children are now ALL
    terminal is deliberately excluded here — :func:`_wake_children_done`
    already wakes it the clean way (same pass or the very next one); the
    deadline path exists only for a genuine timeout, not as a second route
    to the same happy ending.
    """
    rows = conn.execute(
        """
        SELECT r.ref_id, r.meta->'wake_when'->'payload'->'child_job_ids'
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
           AND (r.meta->>'wake_deadline') IS NOT NULL
           AND (r.meta->>'wake_deadline')::double precision
               <= extract(epoch from now())
         ORDER BY r.ref_id
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (_STATUS_NAMESPACE, _WAITING_CHILDREN, limit),
    ).fetchall()

    out: list[tuple[int, list[int]]] = []
    for ref_id, child_ids_json in rows:
        child_ids = [int(c) for c in (child_ids_json or [])]
        still_running = _non_terminal_children(conn, child_ids)
        if not still_running:
            continue
        out.append((int(ref_id), still_running))
    return out


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


def _requeue_degraded(
    store: Any, ref_id: int, still_running_child_ids: list[int]
) -> None:
    """Re-queue a past-deadline ``waiting_children`` parent "woken-
    degraded" (§H piece 5) — the master's "a parent never blocks forever
    on a child".

    **Bug fixed (review Finding 1): never tag the coordinator job itself.**
    ``child-failed:`` is one of ``_DOABLE_EXCLUSION_TAGS``
    (``handlers/_todo_views.py``), and the coordinator's own claim
    (``executors/coordinator.py``'s ``_claim_jobs``) passes
    ``exclude_paused=True`` — whose ``NOT EXISTS`` checks tags on the
    claimed row (``r.ref_id``) itself. Stamping ``child-failed:<id>`` on
    ``ref_id`` here would make the just-re-queued job permanently
    invisible to its own executor — a self-wedge, not a bubble. So the
    bubble tag(s) go on ``ref_id``'s own PARENT (``r.parent_id`` — the
    standard bubble target :func:`precis.handlers._job_bubble.
    bubble_job_failure` uses when a JOB fails; here it's the coordinator's
    own parent, not a child's, so that helper doesn't directly apply — the
    coordinator's children were never tagged as failed, they're just still
    running) instead. ``meta.degraded_children`` / ``meta.wake_degraded_at``
    are stamped on the coordinator job so its own next slice can see what
    happened without re-deriving it. Deliberately does NOT touch the child
    rows themselves — a still-running child may finish (or get reclaimed/
    attempt-capped/swept) entirely on its own; the parent just stops
    waiting on it and resumes control on its next slice, where the
    coordinator's own ``dispatch`` re-examines its children's actual
    status (the same read it would do on a clean wake) and decides what
    to do about the stragglers.
    """
    from precis.store import Tag

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT parent_id FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        parent_id = int(row[0]) if row and row[0] is not None else None
        if parent_id is not None:
            for child_id in still_running_child_ids:
                store.add_tag(
                    parent_id,
                    Tag.open(f"child-failed:{child_id}"),
                    set_by="system",
                    conn=conn,
                )
        else:
            log.warning(
                "wake_runner: degraded parent job %d has no parent_id — "
                "no bubble target for stragglers %s",
                ref_id,
                still_running_child_ids,
            )
        _set_meta(
            conn,
            ref_id,
            degraded_children=still_running_child_ids,
            wake_degraded_at=time.time(),
        )
        _set_status(store, ref_id, _QUEUED, conn=conn)
        _append_chunk(
            store,
            ref_id,
            "job_event",
            "wake_runner: re-queued degraded (children_deadline) — still "
            f"non-terminal children: {still_running_child_ids}",
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

    A SIXTH, separate step (§H piece 5) re-queues a ``waiting_children``
    parent past its ``meta.wake_deadline`` "woken-degraded" (a
    ``child-failed:<id>`` bubble per still non-terminal child) even
    though its normal wake condition never fired — see
    :func:`_wake_children_deadline_exceeded` / :func:`_requeue_degraded`.
    Kept separate from the ``selectors`` list above because it needs a
    different write (per-child bubble tags, not just a status flip).

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
    degraded: list[tuple[int, list[int]]] = []
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
        try:
            for ref_id, still_running in _wake_children_deadline_exceeded(
                conn, limit=limit
            ):
                if ref_id in seen:
                    continue
                seen.add(ref_id)
                degraded.append((ref_id, still_running))
        except Exception:  # pragma: no cover — defensive
            log.warning(
                "wake_runner: children_deadline selector raised; continuing",
                exc_info=True,
            )
            conn.rollback()
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

    for ref_id, still_running in degraded:
        try:
            _requeue_degraded(store, ref_id, still_running)
            ok += 1
        except Exception:  # pragma: no cover — defensive
            failed += 1
            log.warning(
                "wake_runner: degraded re-queue of job %d failed",
                ref_id,
                exc_info=True,
            )

    return {"claimed": len(ready) + len(degraded), "ok": ok, "failed": failed}


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
