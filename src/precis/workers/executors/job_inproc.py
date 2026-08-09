"""``job_inproc`` executor — claim ONE bounded job and run it synchronously
in the worker pass (§F cycle a, ``docs/backlog/cluster-scheduling.md``
§F).

Sibling of ``claude_inproc``/``ssh_node``, but deliberately the
thinnest of the three:

* **No submit/poll.** An in-proc run can't be detached — the claiming
  worker's pass rotation blocks for the run's duration, exactly like
  ``claude_inproc``'s in-tree ``fix_gripe``/``plan_tick`` dispatch. Every
  job_type on this lane MUST self-limit its own work (minutes, not hours —
  ``embed_batch``'s ``params.limit`` is the first example).
* **No kill hook.** Nothing to cancel mid-run once it's started —
  ``meta.kill_requested`` is ``claude_docker``/``ssh_node`` territory
  (they own a detached process/container they CAN signal).
* **No coordinator semantics.** One job, one dispatch, one terminal
  transition — no yield/resume, no fan-out.

The one thing this lane exists FOR that ``claude_inproc`` doesn't offer:
``respect_reserve=True`` — the box-heavy claim gate (§B-2) — and, via
``claim_executor_jobs``'s reserve-at-claim (6c/6d), a job whose job_type
declares ``requires`` (e.g. ``embed_batch`` → ``{"embedder": 1}``) reserves
that slot in the claim transaction; the slot refunds on any terminal
``set_status`` (crash-reclaimed too — see ``claim_executor_jobs``'s epoch/
expiry arms). ``limit=1`` (one job per pass tick) keeps the pass rotation
live even while a claimed job runs bounded-long: a fresh tick still gets a
turn at the next job once this one finishes.

Plugin dispatch only (mirrors ``claude_inproc``'s plugin branch): the
job_type's ``dispatch(ctx, spec)`` does the work via the shared
``DispatchContext``, then this executor drives the job to ``SUCCEEDED``
unless the dispatcher already left it terminal (failed/cancelled) — reuses
``claude_inproc._build_dispatch_context`` / ``_finalize_plugin_dispatch``
rather than re-deriving them. A job_type with no ``dispatch`` (or an
unknown ``job_type``) is a config error, not a retryable one — INFRA-failed
immediately.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from precis.workers.executors._common import (
    FAILED as _FAILED,
)
from precis.workers.executors._common import (
    JOB_EVENT_KIND as _JOB_EVENT_KIND,
)
from precis.workers.executors._common import (
    RUNNING as _RUNNING,
)
from precis.workers.executors._common import (
    append_chunk as _append_chunk,
)
from precis.workers.executors._common import (
    claim_executor_jobs,
)
from precis.workers.executors._common import (
    poison_guard as _poison_guard,
)
from precis.workers.executors._common import (
    record_failure as _record_failure,
)
from precis.workers.executors._common import (
    renew_lease_if_mine as _renew_lease_if_mine,
)
from precis.workers.executors._common import (
    set_meta as _set_meta,
)
from precis.workers.executors._common import (
    set_status as _set_status,
)
from precis.workers.executors.claude_inproc import (
    _build_dispatch_context,
    _finalize_plugin_dispatch,
)
from precis.workers.job_types import get_job_type, known_job_types

log = logging.getLogger(__name__)

_EXECUTOR_NAME = "job_inproc"

#: Lease window (seconds) for a claimed job_inproc job — must comfortably
#: outlive the bounded work order. ``embed_batch``'s default
#: ``params.limit=2000`` chunks at a 32-chunk microbatch is well under this
#: on any real embedder; override via ``PRECIS_JOB_INPROC_LEASE_S`` for a
#: slower host or a larger limit. Clamped to a 60s floor so a misconfigured
#: value can't produce an instantly-expiring lease.
_DEFAULT_LEASE_S = 1800


#: ``ctx.meta`` key a lost renewal stamps (see :func:`renew_own_lease`) —
#: read back by :func:`_run_one` after ``dispatch`` returns to skip the
#: happy-path SUCCEEDED finalize on a job whose lease was reclaimed out
#: from under it mid-drain. Local to this module; not a DB column.
_LEASE_LOST_META_KEY = "_lease_lost"


def _lease_seconds() -> int:
    raw = os.environ.get("PRECIS_JOB_INPROC_LEASE_S")
    if raw is None:
        return _DEFAULT_LEASE_S
    try:
        return max(60, int(raw))
    except ValueError:
        log.warning(
            "job_inproc: PRECIS_JOB_INPROC_LEASE_S=%r is not an int; using default %ds",
            raw,
            _DEFAULT_LEASE_S,
        )
        return _DEFAULT_LEASE_S


def renew_own_lease(store: Any, ref_id: int, meta: dict[str, Any]) -> bool:
    """Renew THIS job's own lease mid-drain — called by a long-running
    job_type's dispatch loop (``embed_batch``'s ``claim_batch`` iteration)
    so a batch slower than :func:`_lease_seconds` doesn't get epoch/
    expiry-reclaimed while the original process is still working it (which
    would let a second worker claim the same job — and, for a
    ``requires``-bearing job_type, double-reserve its capacity-limited
    slot — while the first is still running).

    Extends ``lease_until`` by :func:`_lease_seconds` when this claim's
    lease identity still matches the DB row
    (:func:`~precis.workers.executors._common.renew_lease_if_mine`);
    returns that same ``True``/``False``. On ``False`` (lease lost —
    reclaimed by another worker generation), also stamps
    ``meta[_LEASE_LOST_META_KEY] = True`` on the SAME ``meta`` dict object
    the caller holds (mirrors ``DispatchContext.meta`` being the identical
    object ``_run_one`` passed in) so :func:`_run_one` can see it after
    ``dispatch`` returns and skip the happy-path SUCCEEDED finalize — the
    new claimant now owns the job.
    """
    with store.pool.connection() as conn:
        renewed = _renew_lease_if_mine(conn, ref_id, meta, _lease_seconds())
        conn.commit()
    if not renewed:
        meta[_LEASE_LOST_META_KEY] = True
    return renewed


def run_job_inproc_pass(store: Any, *, limit: int = 1) -> dict[str, int]:
    """Process up to ``limit`` (default 1) job_inproc jobs.

    Returns ``{claimed, ok, failed}`` for runner aggregation.
    ``respect_reserve=True``: the box-heavy lane, gated by an operator
    ``precis service reserve`` the same way ``ssh_node``/``claude_docker``
    are (§B-2) — a reserved host claims nothing new here either.
    ``reclaim_stale_running=True``: a worker restart mid-run means the
    compute genuinely died (it ran inside THIS process), the same
    "worker death = compute death" assumption ``claude_inproc`` runs
    under — the epoch/expiry arms + ``poison_guard`` crash-loop cap apply
    uniformly (§H).
    """
    node = os.environ.get("PRECIS_NODE")
    poisoned = 0
    to_run: list[tuple[int, str, dict[str, Any]]] = []
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor=_EXECUTOR_NAME,
            limit=limit,
            node=node,
            reclaim_stale_running=True,
            respect_reserve=True,
        )
        if not rows:
            conn.commit()
            return {"claimed": 0, "ok": 0, "failed": 0}
        for ref_id, title, meta in rows:
            # The bump (expiry-only) + reclaim classification already
            # happened inside claim_executor_jobs (§H piece 3); this just
            # applies the cap's consequence, same as every other
            # reclaim_stale_running executor.
            if _poison_guard(store, conn, ref_id, meta):
                poisoned += 1
                continue
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "  'lease_until', (now() + make_interval(secs => %s))::text"
                ") WHERE ref_id = %s",
                (_lease_seconds(), ref_id),
            )
            _set_status(store, ref_id, _RUNNING, conn=conn)
            to_run.append((ref_id, title, meta))
        conn.commit()

    ok = 0
    failed = poisoned
    for ref_id, title, meta in to_run:
        try:
            _run_one(store, ref_id, title, meta)
            ok += 1
        except Exception as exc:  # pragma: no cover — defensive
            failed += 1
            log.warning("job_inproc: job %d raised: %s", ref_id, exc, exc_info=True)
            try:
                with store.pool.connection() as conn:
                    _append_chunk(
                        store,
                        ref_id,
                        _JOB_EVENT_KIND,
                        f"runner: uncaught exception: {exc!r}",
                        conn=conn,
                    )
                    _set_meta(conn, ref_id, failure_class="infra")
                    _set_status(store, ref_id, _FAILED, conn=conn)
                    conn.commit()
            except Exception:  # pragma: no cover
                log.warning("job_inproc: failed to record failure", exc_info=True)
    return {"claimed": len(rows), "ok": ok, "failed": failed}


def _run_one(store: Any, ref_id: int, title: str, meta: dict[str, Any]) -> None:
    """Dispatch one claimed job — plugin ``dispatch`` only. job_inproc has
    no in-tree built-in switch (unlike ``claude_inproc``'s fix_gripe/
    plan_tick) and no legacy blocking-``dispatch``-vs-submit/poll split
    (unlike ``ssh_node``) — every job_type on this lane declares
    ``dispatch``, full stop."""
    job_type_name = meta.get("job_type")
    if not job_type_name:
        _record_failure(
            store,
            ref_id,
            "missing meta.job_type",
            gripe_rollback=None,
            failure_class="infra",
        )
        return
    spec = get_job_type(str(job_type_name))
    if spec is None:
        _record_failure(
            store,
            ref_id,
            f"unknown job_type {job_type_name!r}; known: {known_job_types()}",
            gripe_rollback=None,
            failure_class="infra",
        )
        return
    if spec.dispatch is None:
        _record_failure(
            store,
            ref_id,
            f"job_type {spec.name!r} has no dispatch(); job_inproc runs "
            "plugin dispatchers only",
            gripe_rollback=None,
            failure_class="infra",
        )
        return

    ctx = _build_dispatch_context(store, ref_id, title, meta)
    spec.dispatch(ctx, spec)
    if meta.get(_LEASE_LOST_META_KEY):
        # A mid-drain renewal (renew_own_lease) found this job's lease
        # already reclaimed by another worker generation — the new
        # claimant owns the job now and will drive its own terminal
        # transition. Finalizing here would race it (and, on the
        # happy path, wrongly stamp SUCCEEDED over whatever the new
        # owner is still doing).
        log.warning(
            "job_inproc: job %d's lease was lost mid-dispatch (reclaimed "
            "by another worker generation) — leaving the terminal "
            "transition to the new owner",
            ref_id,
        )
        return
    # Mirrors claude_inproc's plugin-dispatch finalize: the dispatcher does
    # its work and appends a summary but leaves the happy-path terminal
    # transition to the executor; a dispatcher that already recorded a
    # failure/cancellation is left untouched.
    _finalize_plugin_dispatch(store, ref_id)


__all__ = ["renew_own_lease", "run_job_inproc_pass"]
