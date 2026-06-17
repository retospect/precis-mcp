"""``coordinator`` executor — yield/resume for long-running orchestrators.

A coordinator job (precis-dft's ``dft_campaign`` is the first real
consumer) runs in many short slices over hours, days, or weeks.
Each slice claims the job, runs one phase, and either returns
:class:`Done` (terminal) or :class:`Yield` (pause until a wake
condition fires). The :mod:`precis.workers.wake_runner` watches
for satisfied wake conditions and re-tags paused jobs
``STATUS:queued`` so this executor picks them up again.

This executor is a sibling of :mod:`claude_inproc` — it uses the
same job substrate, the same ``DispatchContext`` shape, and many
of the same helpers (``_set_status`` / ``_append_chunk`` / …).
Differences from ``claude_inproc``:

- ``meta.executor = 'coordinator'`` (not ``'claude_inproc'``).
- Short lease (5 minutes per slice). Each yield/resume cycle
  brings the lease back; the cumulative lifetime is unbounded.
- The claim SQL excludes ``ask-user:*`` / ``asking-reto:*`` /
  ``halt:*`` open-namespace exclusion tags via the existing
  :func:`precis.handlers._todo_views._doable_exclusion_clause`
  helper. Pause-for-human use the same tagging convention
  Hermes / asa-bot already render in the attention view.
- The dispatcher path is ``spec.dispatch(ctx, spec)`` only —
  built-in fallback ``if/elif`` doesn't apply here (coordinator
  has no built-in job_types).

See ``docs/design/dft-phase-0-pr-3-coordinator-executor.md`` for
the full design rationale.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from psycopg import Connection

from precis.handlers._todo_views import _doable_exclusion_clause
from precis.workers.executors import EXECUTOR_PROVIDES
from precis.workers.executors.claude_inproc import (
    _append_chunk,
    _build_dispatch_context,
    _is_cancel_requested,
    _record_failure,
    _set_status,
)
from precis.workers.job_types import get_job_type, known_job_types

if TYPE_CHECKING:
    from precis.workers.executors._context import DispatchContext  # noqa: F401

log = logging.getLogger(__name__)


_EXECUTOR_NAME = "coordinator"

# Status tag values used by the coordinator path. These join the
# closed STATUS:* namespace introduced by claude_inproc; they're
# distinct values within the same namespace, not a new namespace.
_STATUS_NAMESPACE = "STATUS"
_QUEUED = "queued"
_RUNNING = "running"
_SUCCEEDED = "succeeded"
_FAILED = "failed"
_CANCELLED = "cancelled"
_CANCEL_REQUESTED = "cancel_requested"

#: STATUS:waiting_* values written by a Yield. Each maps to one
#: ``WakeWhen.kind`` so the wake_runner's selectivity stays cheap
#: (exact match on closed-status value, not a LIKE).
_WAITING_CHILDREN = "waiting_children"
_WAITING_TIME = "waiting_time"
_WAITING_ASK_USER = "waiting_ask_user"
_WAITING_MANUAL_KICK = "waiting_manual_kick"

#: Map ``WakeWhen.kind`` (defined in ``_yield.py``) onto the
#: closed STATUS:* value the executor sets when persisting a
#: Yield. Centralised so the wake_runner reads the same table.
_STATUS_FOR_WAKE_KIND: dict[str, str] = {
    "children_done": _WAITING_CHILDREN,
    "at_time": _WAITING_TIME,
    "tag_cleared": _WAITING_ASK_USER,
    "tag_added": _WAITING_MANUAL_KICK,
}

# Terminal STATUS values — a row carrying any of these is not
# claimable. Waiting statuses are NOT terminal; they're paused.
_TERMINAL = (_SUCCEEDED, _FAILED, _CANCELLED)

# Chunk kinds the executor writes.
_JOB_EVENT_KIND = "job_event"
_JOB_SUMMARY_KIND = "job_summary"

# Slice lease. Short on purpose: each active slice is meant to be
# brief (read state, submit children, write checkpoint, yield).
# The cumulative job lifetime is unbounded because each yield
# releases the slot.
_LEASE_MINUTES = 5


# ── Claim ─────────────────────────────────────────────────────────


def _claim_jobs(
    conn: Connection, *, limit: int
) -> list[tuple[int, str, dict[str, Any]]]:
    """Lock up to ``limit`` claimable coordinator jobs.

    Claimable = ``kind='job'``, executor matches, ``STATUS:queued``,
    not terminal, lease expired or absent, AND no exclusion tag
    is set on the row (``ask-user:*`` / ``asking-reto:*`` /
    ``halt:*`` / ``child-failed:*``). The exclusion check uses the
    existing :func:`_doable_exclusion_clause` SQL so its vocabulary
    stays in sync with the dispatcher.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    rows = conn.execute(
        f"""
        SELECT r.ref_id, r.title, r.meta
          FROM refs r
         WHERE r.kind = 'job'
           AND r.deleted_at IS NULL
           AND r.meta->>'executor' = %s
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = %s
               )
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = ANY(%s)
               )
           AND NOT EXISTS (
                 -- Existing open-namespace exclusion vocabulary:
                 -- ask-user:* / asking-reto:* / halt:* / etc.
                 -- Shared with the dispatcher's candidate query so
                 -- drift between them is impossible.
                 SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = 'OPEN'
                    AND {_doable_exclusion_clause()}
               )
           AND (
                (r.meta->>'lease_until') IS NULL
             OR (r.meta->>'lease_until')::timestamptz < now()
           )
         ORDER BY r.ref_id
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (
            _EXECUTOR_NAME,
            _STATUS_NAMESPACE,
            _QUEUED,
            _STATUS_NAMESPACE,
            list(_TERMINAL),
            limit,
        ),
    ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2] or {})) for r in rows]


# ── Pass entry point ──────────────────────────────────────────────


def run_coordinator_pass(store: Any, *, limit: int = 4) -> dict[str, int]:
    """Process up to ``limit`` coordinator jobs.

    Returns ``{claimed, ok, failed}`` for the ref-pass aggregator.
    Each claimed job runs one slice and either reaches terminal
    status or yields. Wake_runner re-queues yielded jobs when their
    wake condition fires.
    """
    with store.pool.connection() as conn:
        rows = _claim_jobs(conn, limit=limit)
        if not rows:
            conn.commit()
            return {"claimed": 0, "ok": 0, "failed": 0}
        for ref_id, _title, _meta in rows:
            # Short lease. The slice is meant to finish quickly; if
            # it doesn't, another worker can take over once the
            # lease expires.
            conn.execute(
                "UPDATE refs SET meta = meta || "
                "jsonb_build_object("
                f"  'lease_until', (now() + interval '{_LEASE_MINUTES} minutes')::text"
                ") "
                "WHERE ref_id = %s",
                (ref_id,),
            )
            _set_status(store, ref_id, _RUNNING, conn=conn)
        conn.commit()

    ok = 0
    failed = 0
    for ref_id, title, meta in rows:
        try:
            _run_one(store, ref_id, title, meta)
            ok += 1
        except Exception as exc:  # pragma: no cover — defensive
            failed += 1
            log.warning(
                "coordinator: job %d raised: %s", ref_id, exc, exc_info=True
            )
            try:
                with store.pool.connection() as conn:
                    _append_chunk(
                        store,
                        ref_id,
                        _JOB_EVENT_KIND,
                        f"runner: uncaught exception: {exc!r}",
                        conn=conn,
                    )
                    _set_status(store, ref_id, _FAILED, conn=conn)
                    conn.commit()
            except Exception:  # pragma: no cover
                log.warning(
                    "coordinator: failed to record failure", exc_info=True
                )
    return {"claimed": len(rows), "ok": ok, "failed": failed}


# ── Per-job dispatch ──────────────────────────────────────────────


def _run_one(store: Any, ref_id: int, title: str, meta: dict[str, Any]) -> None:
    """Dispatch a single claimed coordinator job.

    The coordinator path expects every job_type to declare its own
    ``dispatch`` callable — there is no built-in fallback (unlike
    ``claude_inproc`` which still hosts ``fix_gripe`` / ``plan_tick``
    in-tree). A spec without ``dispatch`` is a misconfiguration:
    the submit-time validation in ``JobHandler.put`` rejects
    job_types whose ``COMPATIBLE_EXECUTORS`` doesn't include
    ``coordinator``.
    """
    job_type_name = meta.get("job_type")
    if not job_type_name:
        _record_failure(
            store, ref_id, "missing meta.job_type", gripe_rollback=None
        )
        return
    spec = get_job_type(str(job_type_name))
    if spec is None:
        _record_failure(
            store,
            ref_id,
            f"unknown job_type {job_type_name!r}; known: {known_job_types()}",
            gripe_rollback=None,
        )
        return

    if spec.dispatch is None:
        _record_failure(
            store,
            ref_id,
            (
                f"job_type {spec.name!r} is configured for coordinator but "
                "has no spec.dispatch callable — plugin must export one"
            ),
            gripe_rollback=None,
        )
        return

    # Cooperative cancel check before doing real work. Cancel
    # observed at every slice boundary, so a job that runs many
    # short slices over a long lifetime effectively gets cancel-
    # polled at every yield.
    with store.pool.connection() as conn:
        if _is_cancel_requested(conn, ref_id):
            _append_chunk(
                store,
                ref_id,
                _JOB_EVENT_KIND,
                "runner: cancel requested before run",
                conn=conn,
            )
            _set_status(store, ref_id, _CANCELLED, conn=conn)
            conn.commit()
            return

    ctx = _build_dispatch_context(store, ref_id, title, meta)
    spec.dispatch(ctx, spec)


__all__ = ["run_coordinator_pass"]
