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
   also has ``meta.wake_when`` set and no live compute (no
   ``compute_handle``, no ``submit_started_at``). Nobody holds a parked
   job, so it is cancelled where it sits. It used to be re-queued "so
   the coordinator's cancel-poll fires on its next slice", but
   ``STATUS:`` is closed-prefix: ``queued`` replaced the cancel and the
   next claim ran the job as if never cancelled (gr570507: a
   lane-parked run_slice launched a 2 h measure after its cancel). A
   row with a compute handle is its executor's — ``ssh_node``'s cancel
   drive stops the process — and is left alone here.

**Child-deadlock deadline (a sixth, distinct lease-epoch reclaim step).** A ``children_done`` park's
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

Cadence: its own daemon thread (:func:`start_wake_thread`) with its own
``Store``, ticking every ``PRECIS_WAKE_INTERVAL_S`` (default 3 s), off the
serial rotation. It used to ride ``runner.run_loop`` between passes, which
a ~10 min ``embed_batch`` / ``derived_drain`` job batch holds, so ``at_time``
wakes (``ci_verdict_poll`` on a release, quest ticks, lane parks) fired 5-19
min late (gr626353). The thread honours the same service gate as the
rotation pass (``service_config`` / ``--only`` / ``--skip``). The rotation
runs :func:`wake_pass_for_runner` only for a one-shot ``--once`` invocation,
where no thread is started.

Concurrency: every wake SELECT holds ``FOR UPDATE SKIP LOCKED`` only while
it selects; the lock is released at the selection commit, *before* the
re-queue write. Two passes (this thread, a peer host's) can therefore
select the same row, so each write re-reads the row's ``STATUS:`` under a
row lock and skips when it is no longer the ``waiting_*`` / ``cancel_requested``
state the selector saw (:func:`_still_in`): a second pass never re-queues a
job a first pass already woke and an executor already claimed.

See ``dft-phase-0-pr-3-coordinator-executor`` (git-only) §3.2.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from psycopg import Connection

from precis.utils.env import env_float
from precis.workers.executors._common import (
    CANCEL_REQUESTED as _CANCEL_REQUESTED,
)
from precis.workers.executors._common import (
    CANCELLED as _CANCELLED,
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
    current_status as _current_status,
)
from precis.workers.executors._common import (
    set_meta as _set_meta,
)
from precis.workers.executors._common import (
    set_status as _set_status,
)
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)


# ── Wake-condition selectors ──────────────────────────────────────


def _wake_children_done(conn: Connection, *, limit: int) -> list[int]:
    """Find ``waiting_children`` rows whose every child is terminal.

    ``meta.wake_when.payload.child_job_ids`` is a JSON array of
    int IDs. The NOT EXISTS subquery rejects any row that still
    has a *live*, non-terminal child. A soft-deleted child
    (``retired_at`` set; its tags persist) counts as terminal /
    absent — matching the hard-delete behaviour documented in
    ``dft-phase-0-pr-3-coordinator-executor`` (git-only) — so
    an operator soft-deleting a stuck child unblocks the wake
    instead of parking the coordinator forever.
    """
    rows = conn.execute(
        """
        SELECT r.ref_id
          FROM refs r
         WHERE r.kind = 'job'
           AND r.retired_at IS NULL
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
                    AND c.retired_at IS NULL
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
           AND c.retired_at IS NULL
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
    passed with at least one child STILL non-terminal (the child-deadlock
    wake deadline — "a parent never blocks forever on a
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
           AND r.retired_at IS NULL
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
           AND r.retired_at IS NULL
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
           AND r.retired_at IS NULL
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
           AND r.retired_at IS NULL
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
    """Find parked jobs whose ``STATUS:cancel_requested`` is set.

    Cancel pre-empts any other wake condition. A hit is cancelled in
    place by :func:`_cancel_parked`, never re-queued: ``STATUS:`` is
    closed-prefix, so the re-queue to ``queued`` erased the cancel and
    the next claim launched the job (gr570507).

    ``meta.wake_when`` marks a row that was parked (a coordinator yield,
    ``park_job_until``); nothing clears it, so a job that parked once and
    then launched still carries it. The compute filter keeps such a row
    out: with ``compute_handle`` or a live ``submit_started_at`` an
    executor holds the compute and its own cancel drive
    (``ssh_node._cancelling_jobs``) owns the stop. ``set_meta(key=None)``
    leaves a JSON null behind, hence ``->>`` rather than ``?``.
    """
    rows = conn.execute(
        """
        SELECT r.ref_id
          FROM refs r
         WHERE r.kind = 'job'
           AND r.retired_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = %s
               )
           AND r.meta ? 'wake_when'
           AND r.meta->>'compute_handle' IS NULL
           AND r.meta->>'submit_started_at' IS NULL
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


def _clear_lease(conn: Connection, ref_id: int) -> None:
    """Drop the stale claim markers from a job going back to ``queued``.

    ``meta.lease_until`` describes a *claim*. A re-queued job has none —
    its previous slice is over and no executor holds it — so carrying the
    old lease makes a healthy woken job indistinguishable from one whose
    executor died. That is not hypothetical: the quest reconciler
    (``quest/loop.py::_reap_orphaned_loop``) reaps a non-terminal loop
    whose lease is stale beyond the grace window and which has written no
    chunk inside it, and a job parked on ``at_time`` satisfies both the
    moment it is woken. Prod job 393999 was re-queued by a live
    wake_runner at 14:57 and cancelled as a "reboot-orphan" 13 minutes
    later; 47 quest_tick loops died that way in three days, 0 succeeded.

    The reconciler already treats a null lease as "never claimed, not my
    business", so clearing it puts a woken job back in exactly the state
    a freshly-minted one is in. The sweeper's ~1h stuck-job threshold
    remains the backstop if nothing claims it.
    """
    conn.execute(
        "UPDATE refs SET meta = meta - 'lease_until' WHERE ref_id = %s",
        (ref_id,),
    )


def _still_in(conn: Connection, ref_id: int, expect: tuple[str, ...]) -> bool:
    """Lock ``ref_id``'s row and report whether its ``STATUS:`` is still one
    of ``expect`` — the re-check that makes two concurrent wake passes safe.

    The selectors' ``FOR UPDATE SKIP LOCKED`` is released at the selection
    commit, before the write, so a peer pass (another host, or this host's
    wake thread) can hold the same ready row. The blocking row lock here
    serialises the two writers; the loser then reads the winner's committed
    state (a fresh statement under READ COMMITTED) and backs off instead of
    flipping a job the winner woke — or an executor has since claimed —
    back to ``queued``.
    """
    conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (ref_id,))
    return _current_status(conn, ref_id) in expect


def _requeue(
    store: Store,
    ref_id: int,
    reason: str,
    *,
    expect: tuple[str, ...] | None = None,
) -> bool:
    """Transition ``ref_id`` back to ``STATUS:queued`` + audit chunk.

    ``expect`` (the wake pass passes the ``waiting_*`` status its selector
    saw) makes the write conditional on the row still being in that state;
    returns ``False`` without writing when a concurrent pass got there
    first (see :func:`_still_in`). ``None`` (the ``job wake`` handler's
    manual wake) writes unconditionally.

    Holds the connection only for the status + chunk writes so
    contention with the coordinator pass is minimal.

    When the dispatcher annotated its park with ``wake_when.payload.why``
    (a free-text note naming what the park waits on — a saturated lane,
    a child, a throttle) the audit line echoes it: a job that loops on
    ``at_time`` parks otherwise records 50 identical "re-queued
    (at_time)" lines and nothing about what it waited for (gr508162).
    """
    with store.pool.connection() as conn:
        if expect is not None and not _still_in(conn, ref_id, expect):
            conn.rollback()
            return False
        row = conn.execute(
            "SELECT meta->'wake_when'->'payload'->>'why' FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
        why = str(row[0]).strip()[:200] if row and row[0] else ""
        _set_status(store, ref_id, _QUEUED, conn=conn)
        _clear_lease(conn, ref_id)
        # Audit chunk so the lifecycle reads cleanly:
        # waiting_children → wake_runner: re-queued (children_done) → running → ...
        _append_chunk(
            store,
            ref_id,
            "job_event",
            f"wake_runner: re-queued ({reason}{' — ' + why if why else ''})",
            conn=conn,
        )
        conn.commit()
    return True


def _cancel_parked(store: Store, ref_id: int) -> bool:
    """Transition a parked ``STATUS:cancel_requested`` job to
    ``STATUS:cancelled`` + audit chunk (gr570507).

    The executors' own "cancel requested before run" write, done here
    because a parked row never reaches an executor: event chunk, then the
    terminal status (``set_status`` releases the job reservation). No
    release hook runs — a parked job holds no lease on its work
    (``park_job_until`` released the reservation; a run_slice park happens
    before the todo is claimed) — the same as the coordinator's
    slice-boundary cancel. The park's ``wake_when.payload.why`` is echoed
    like a re-queue's, so the record says what the job was waiting for
    when it was cancelled.

    Returns ``False`` without writing when the row is no longer
    ``cancel_requested`` (a concurrent pass cancelled it first).
    """
    with store.pool.connection() as conn:
        if not _still_in(conn, ref_id, (_CANCEL_REQUESTED,)):
            conn.rollback()
            return False
        row = conn.execute(
            "SELECT meta->'wake_when'->'payload'->>'why' FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
        why = str(row[0]).strip()[:200] if row and row[0] else ""
        _append_chunk(
            store,
            ref_id,
            "job_event",
            f"wake_runner: cancelled while parked{' — ' + why if why else ''}",
            conn=conn,
        )
        _set_status(store, ref_id, _CANCELLED, conn=conn)
        conn.commit()
    return True


def _requeue_degraded(
    store: Store, ref_id: int, still_running_child_ids: list[int]
) -> bool:
    """Re-queue a past-deadline ``waiting_children`` parent "woken-
    degraded" (§H piece 5) — the master's "a parent never blocks forever
    on a child".

    **Bug fixed (review Finding 1): never tag the coordinator job itself.**
    ``child-failed:`` is one of ``_DOABLE_EXCLUSION_TAGS``
    (``todo/views.py``), and the coordinator's own claim
    (``executors/coordinator.py``'s ``_claim_jobs``) passes
    ``exclude_paused=True`` — whose ``NOT EXISTS`` checks tags on the
    claimed row (``r.ref_id``) itself. Stamping ``child-failed:<id>`` on
    ``ref_id`` here would make the just-re-queued job permanently
    invisible to its own executor — a self-wedge, not a bubble. So the
    bubble tag(s) go on ``ref_id``'s own PARENT (``r.parent_id`` — the
    standard bubble target :func:`precis.utils.job_bubble.
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
        if not _still_in(conn, ref_id, (_WAITING_CHILDREN,)):
            conn.rollback()
            return False
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
        _clear_lease(conn, ref_id)
        _append_chunk(
            store,
            ref_id,
            "job_event",
            "wake_runner: re-queued degraded (children_deadline) — still "
            f"non-terminal children: {still_running_child_ids}",
            conn=conn,
        )
        conn.commit()
    return True


# ── Pass entry point ──────────────────────────────────────────────

#: The ``waiting_*`` STATUS each re-queueing selector's rows sit in — what
#: :func:`_requeue` re-checks under the row lock before writing.
_WAKE_STATUS: dict[str, str] = {
    "children_done": _WAITING_CHILDREN,
    "at_time": _WAITING_TIME,
    "tag_cleared": _WAITING_ASK_USER,
    "tag_added": _WAITING_MANUAL_KICK,
}


def run_wake_pass(store: Store, *, limit: int = 16) -> dict[str, int]:
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

    skipped = 0
    for ref_id, label in ready:
        try:
            if label == "cancel":
                done = _cancel_parked(store, ref_id)
            else:
                done = _requeue(store, ref_id, label, expect=(_WAKE_STATUS[label],))
            if done:
                ok += 1
            else:
                skipped += 1
        except Exception:  # pragma: no cover — defensive
            failed += 1
            log.warning(
                "wake_runner: %s of job %d failed",
                "cancel" if label == "cancel" else "re-queue",
                ref_id,
                exc_info=True,
            )

    for ref_id, still_running in degraded:
        try:
            if _requeue_degraded(store, ref_id, still_running):
                ok += 1
            else:
                skipped += 1
        except Exception:  # pragma: no cover — defensive
            failed += 1
            log.warning(
                "wake_runner: degraded re-queue of job %d failed",
                ref_id,
                exc_info=True,
            )

    # ``claimed`` counts rows this pass acted on; a row a concurrent pass
    # already woke (``skipped``) is that pass's claim, not ours.
    return {
        "claimed": len(ready) + len(degraded) - skipped,
        "ok": ok,
        "failed": failed,
    }


def wake_pass_for_runner(store: Store, batch_size: int) -> BatchResult:
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


# ── Dedicated wake thread (gr626353) ──────────────────────────────

#: ``service_config`` / registry name this thread gates under.
SERVICE_NAME = "wake_runner"

#: Env var overriding :data:`DEFAULT_WAKE_INTERVAL_S`.
WAKE_INTERVAL_ENV = "PRECIS_WAKE_INTERVAL_S"

#: Seconds between wake passes. A wake pass is a handful of indexed SELECTs
#: plus a status flip per ready row, so a few seconds is cheap and keeps an
#: ``at_time`` wake within one interval of its instant.
DEFAULT_WAKE_INTERVAL_S = 3.0

#: An idle thread still logs one ``payload.handler`` liveness row this often,
#: so ``conditions``' pass-dead detector (which reads those rows) sees the
#: pass alive without a row every few seconds.
_LIVENESS_LOG_EVERY_S = 60.0


def _wake_interval_s() -> float:
    return env_float(WAKE_INTERVAL_ENV, DEFAULT_WAKE_INTERVAL_S, lo=0.01)


def _sleep_with_stop_check(duration: float, should_stop: Callable[[], bool]) -> None:
    """Sleep ``duration`` s in <= 0.5 s steps, returning as soon as
    ``should_stop()`` flips so shutdown is noticed promptly."""
    remaining = duration
    while remaining > 0 and not should_stop():
        step = min(0.5, remaining)
        time.sleep(step)
        remaining -= step


def _close_quietly(store: Store | None) -> None:
    if store is not None:
        try:
            store.close()
        except Exception:
            pass


def _wake_tick(store: Store, batch_size: int, last_logged: float) -> float:
    """One wake pass plus its (rate-limited) liveness row; returns the
    monotonic time of the last logged row."""
    t0 = time.monotonic()
    r = run_wake_pass(store, limit=batch_size)
    now = time.monotonic()
    if not (r["claimed"] or r["failed"] or now - last_logged >= _LIVENESS_LOG_EVERY_S):
        return last_logged
    log.info(
        "worker: %s claimed=%d ok=%d failed=%d",
        SERVICE_NAME,
        r["claimed"],
        r["ok"],
        r["failed"],
        extra={
            "payload": {
                "handler": SERVICE_NAME,
                "claimed": r["claimed"],
                "ok": r["ok"],
                "failed": r["failed"],
                "report_only": False,
                "duration_ms": int((now - t0) * 1000),
            }
        },
    )
    return now


def _wake_thread_body(
    dsn: str,
    batch_size: int,
    gate: Callable[[str], bool] | None,
    should_stop: Callable[[], bool],
) -> None:
    """The :func:`start_wake_thread` loop body — see its docstring."""
    from precis.store import Store

    store: Store | None = None
    last_logged = float("-inf")
    while not should_stop():
        try:
            try:
                enabled = gate is None or gate(SERVICE_NAME)
            except Exception:
                # The gate is a DB-backed resolver: if it errors, skip this
                # tick rather than wake against an unknown run-control state.
                log.warning("wake thread: gate check failed; skipping", exc_info=True)
                enabled = False
            if not enabled:
                # Gate off: idle, and hand the connection back.
                _close_quietly(store)
                store = None
            else:
                if store is None:
                    try:
                        store = Store.connect(dsn)
                    except Exception:
                        log.warning(
                            "wake thread: failed to connect; retrying", exc_info=True
                        )
                if store is not None:
                    last_logged = _wake_tick(store, batch_size, last_logged)
        except Exception:
            # A bug or a dead connection must never take the worker down.
            # The store is dropped too — its connection may be the fault —
            # and reconnected on the next tick.
            log.warning("wake thread: tick failed", exc_info=True)
            _close_quietly(store)
            store = None

        _sleep_with_stop_check(_wake_interval_s(), should_stop)

    _close_quietly(store)


def start_wake_thread(
    dsn: str,
    *,
    batch_size: int = 32,
    gate: Callable[[str], bool] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> threading.Thread:
    """Start a daemon thread that runs :func:`run_wake_pass` every
    ``PRECIS_WAKE_INTERVAL_S`` (default 3 s), off the serial rotation
    (gr626353).

    ``workers/runner.py``'s ``run_loop`` runs one pass at a time, and the
    in-line job lane's ``embed_batch`` / ``derived_drain`` batches hold it
    for up to ~10 min, so the in-rotation wake pass (even interleaved) ran
    5-19 min late and an ``at_time`` ``ci_verdict_poll`` stalled a release.
    The thread has its own ``Store.connect(dsn)`` (psycopg connections are
    not usable across threads) and the rotation does not also run the pass
    (``--once`` excepted: no thread is started there).

    Safe beside other hosts' copies and the one-shot rotation pass: the
    selectors are ``FOR UPDATE SKIP LOCKED`` and each write re-checks the
    row's STATUS under a row lock (:func:`_still_in`), so a row is woken
    once however many passes race for it.

    ``gate`` is the worker's per-cycle service gate (``cli/worker.py``'s
    ``_pass_gate``), consulted each tick as ``gate("wake_runner")``: a
    ``service_config`` prio-0 row or ``--skip`` idles the thread (and closes
    its connection) within one resolver TTL, exactly as it dims the rotation
    pass. ``None`` (``--only``) means ungated.

    The thread never crashes the worker: a connect failure, gate error or
    pass exception is logged, the store is dropped, and the next tick
    reconnects. It exits when ``should_stop()`` is true.

    Returns the started daemon thread.
    """
    stop = should_stop or (lambda: False)
    thread = threading.Thread(
        target=_wake_thread_body,
        args=(dsn, batch_size, gate, stop),
        name="wake_runner",
        daemon=True,
    )
    thread.start()
    return thread


__all__ = ["run_wake_pass", "start_wake_thread", "wake_pass_for_runner"]
