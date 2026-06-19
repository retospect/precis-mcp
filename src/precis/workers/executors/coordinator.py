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
- The claim SQL excludes ``ask-user:*`` / ``halt:*``
  open-namespace exclusion tags via the existing
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

from precis.workers.executors._common import (
    CANCELLED as _CANCELLED,
)
from precis.workers.executors._common import (
    FAILED as _FAILED,
)
from precis.workers.executors._common import (
    JOB_EVENT_KIND as _JOB_EVENT_KIND,
)
from precis.workers.executors._common import (
    JOB_SUMMARY_KIND as _JOB_SUMMARY_KIND,
)
from precis.workers.executors._common import (
    RUNNING as _RUNNING,
)
from precis.workers.executors._common import (
    SUCCEEDED as _SUCCEEDED,
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
    claim_executor_jobs,
)
from precis.workers.executors._common import (
    is_cancel_requested as _is_cancel_requested,
)
from precis.workers.executors._common import (
    record_failure as _record_failure,
)
from precis.workers.executors._common import (
    set_meta as _set_meta,
)
from precis.workers.executors._common import (
    set_status as _set_status,
)
from precis.workers.executors._yield import Done, Yield

# ``_build_dispatch_context`` stays in ``claude_inproc`` so its helper
# closures bind to that module's globals (which its tests patch); the
# coordinator just reuses it.
from precis.workers.executors.claude_inproc import _build_dispatch_context
from precis.workers.job_types import get_job_type, known_job_types

if TYPE_CHECKING:
    from precis.workers.executors._context import DispatchContext  # noqa: F401

log = logging.getLogger(__name__)


_EXECUTOR_NAME = "coordinator"

#: Map ``WakeWhen.kind`` (defined in ``_yield.py``) onto the
#: closed STATUS:* value the executor sets when persisting a
#: Yield. Centralised so the wake_runner reads the same table.
_STATUS_FOR_WAKE_KIND: dict[str, str] = {
    "children_done": _WAITING_CHILDREN,
    "at_time": _WAITING_TIME,
    "tag_cleared": _WAITING_ASK_USER,
    "tag_added": _WAITING_MANUAL_KICK,
}

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

    Same shape as the claude_inproc claim but with ``exclude_paused``:
    rows carrying an open-namespace pause tag (``ask-user:*`` /
    ``halt:*`` / ``child-failed:*``) are skipped via
    the shared exclusion clause so the vocabulary stays in sync with the
    dispatcher's candidate query.
    """
    return claim_executor_jobs(
        conn, executor=_EXECUTOR_NAME, limit=limit, exclude_paused=True
    )


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
            log.warning("coordinator: job %d raised: %s", ref_id, exc, exc_info=True)
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
                log.warning("coordinator: failed to record failure", exc_info=True)
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
        _record_failure(store, ref_id, "missing meta.job_type", gripe_rollback=None)
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
    # The dispatcher returns Done | Yield (see _yield.py). A resumed
    # slice reads its checkpoint from ``ctx.meta['coordinator_state']``
    # (persisted by the previous Yield below). Discarding this return —
    # the bug this method guards against — left the job stuck at
    # STATUS:running, never terminal, never re-queued.
    result = spec.dispatch(ctx, spec)
    _persist_dispatch_result(store, ref_id, result)


def _persist_dispatch_result(store: Any, ref_id: int, result: Any) -> None:
    """Advance a coordinator job from its dispatcher's return.

    ``Done`` → write the ``job_summary`` chunk, merge final scalars into
    ``refs.meta``, transition to ``succeeded`` / ``failed``. ``Yield`` →
    checkpoint ``state`` into ``meta.coordinator_state``, record
    ``meta.wake_when``, and set the ``STATUS:waiting_*`` value the
    wake_runner watches (it re-queues to ``STATUS:queued`` when the wake
    condition fires). Any other return is a contract violation — fail
    loudly rather than leave the job pinned at ``STATUS:running``.
    """
    if isinstance(result, Done):
        with store.pool.connection() as conn:
            _append_chunk(store, ref_id, _JOB_SUMMARY_KIND, result.summary, conn=conn)
            if result.summary_meta:
                _set_meta(conn, ref_id, **result.summary_meta)
            _set_status(
                store, ref_id, _SUCCEEDED if result.success else _FAILED, conn=conn
            )
            conn.commit()
        return

    if isinstance(result, Yield):
        wake = result.wake_when
        status = _STATUS_FOR_WAKE_KIND.get(wake.kind)
        if status is None:
            _record_failure(
                store,
                ref_id,
                f"Yield with unknown wake kind {wake.kind!r}; "
                f"known: {sorted(_STATUS_FOR_WAKE_KIND)}",
                gripe_rollback=None,
            )
            return
        with store.pool.connection() as conn:
            _set_meta(
                conn,
                ref_id,
                coordinator_state=result.state,
                wake_when={"kind": wake.kind, "payload": wake.payload},
            )
            _set_status(store, ref_id, status, conn=conn)
            conn.commit()
        return

    _record_failure(
        store,
        ref_id,
        f"dispatch returned {type(result).__name__}, expected Done|Yield",
        gripe_rollback=None,
    )


__all__ = ["run_coordinator_pass"]
