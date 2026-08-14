"""ssh_node executor — claim a job and run it on a remote node.

Sibling of :mod:`claude_inproc` / :mod:`coordinator`: a
``run_ssh_node_pass`` function the CLI registers as a ``RefPass``.
Where ``claude_inproc`` spawns ``claude -p`` locally, ``ssh_node`` hands
the job to its plugin, which typically shells out to
``ssh <node> docker run …`` (precis-dft's ``gpaw_relax``, catpath's
``autocatpath_seed`` are the known consumers).

This executor runs **plugin dispatchers only** — it has no in-tree
built-in switch (``claude_inproc`` keeps ``fix_gripe`` / ``plan_tick``).

**Detached submit/poll (gr187627) — the preferred protocol.** A job_type that sets
BOTH ``spec.submit`` and ``spec.poll`` never blocks a worker pass: each
pass polls every in-flight handle THIS host submitted (cheap — a status
check, a lease renewal), then claims + ``submit()``s any newly-queued
work (also non-blocking — the plugin owns HOW it detaches: ``nohup``,
``docker run -d``, ``sbatch``). The worker's pass rotation stays live for
the whole multi-hour run — heartbeats keep beating, other passes (nursery,
sweeper, dispatch) keep running on the same rotation. See
:class:`precis.workers.job_types.JobTypeSpec`'s docstring for the exact
``submit``/``poll`` contract.

**Legacy blocking ``dispatch`` — deprecated, kept for backward compat.**
A job_type with only ``spec.dispatch`` still works exactly as before: the
call blocks the claiming worker's whole pass for the run's duration
(the original gr187627 bug — 7 of 8 running seeds were deploy-bounce
orphans during one live incident because the blocking dispatch starved
heartbeats long enough to trip host-dark). ``ssh_node`` logs a
deprecation warning (once per process per job_type) naming gr187627 the
first time it falls back to this mode, so an unmigrated plugin's
operational cost stays visible without being fatal.

Each pass:

0. **Poll** in-flight (``STATUS:running`` + ``meta.compute_handle``)
   detached jobs pinned to this host: ``spec.poll(ctx, handle)`` — renews
   the lease while still running, lets the plugin drive it terminal via
   ``ctx`` when done. A row carrying ``meta.kill_requested`` (§B-2,
   ``precis jobs kill`` — an operator-requested force-kill, e.g. the
   injected-hang drill) is terminalized the SAME way, at THIS poll tick,
   ahead of the deadline check below — takes effect at the next poll for a
   detached job; a no-op for a legacy blocking ``dispatch`` (below) until
   that call returns, since this executor can't observe it mid-run. Past
   ``meta.deadline`` (stamped at submit time, §H
   piece 2), the pass stops calling ``poll`` and terminalizes the job
   itself instead — a never-completing ``poll`` would otherwise renew the
   lease forever, and the sweeper's own wall-clock retirement (§H piece 6)
   leaves no other termination path for a detached job_type.
1. Claim up to ``limit`` ``kind='job'`` rows with
   ``meta.executor == 'ssh_node'``, ``STATUS:queued``, lease expired
   or absent, not terminal — ``FOR UPDATE … SKIP LOCKED``.
2. Lease + ``STATUS:running`` under the claim tx. The lease must
   outlive the remote job (GPAW relaxes run for hours), so it's sized
   from the job's ``resources.wall_seconds`` plus margin.
3. Per claimed job: resolve the job_type, cancel-poll, then either
   ``spec.submit(ctx, spec)`` (detached — returns immediately, the
   handle is persisted to ``meta.compute_handle`` for the next pass's
   poll step) or the legacy blocking ``spec.dispatch(ctx, spec)``.

Concurrency: a legacy blocking dispatch occupies the worker thread for
the whole run, so the default ``limit`` stays 1 — a detached submit/poll
job_type could safely raise it, but the default is conservative until
every known consumer has migrated.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

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
    MAX_ATTEMPTS,
    claim_executor_jobs,
)
from precis.workers.executors._common import (
    RUNNING as _RUNNING,
)
from precis.workers.executors._common import (
    append_chunk as _append_chunk,
)
from precis.workers.executors._common import (
    is_cancel_requested as _is_cancel_requested,
)
from precis.workers.executors._common import (
    maybe_reset_gpu_after_kill as _maybe_reset_gpu_after_kill,
)
from precis.workers.executors._common import (
    poison_guard as _poison_guard,
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

# Reuse the DispatchContext builder the coordinator already shares
# from claude_inproc (it's executor-agnostic — closures over the
# store handle + ref_id).
from precis.workers.executors.claude_inproc import _build_dispatch_context
from precis.workers.job_types import get_job_type, known_job_types

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

_EXECUTOR_NAME = "ssh_node"

#: Lease floor + margin (seconds). The lease is
#: ``max(_LEASE_FLOOR_S, resources.wall_seconds + _LEASE_MARGIN_S)``
#: so a slow GPAW relax can't expire its own lease and get
#: double-claimed mid-run.
_LEASE_FLOOR_S = 7200
_LEASE_MARGIN_S = 3600

#: Max run-attempts before a job is treated as poison — re-exported from
#: ``_common`` (§H piece 3: the bump + the cap are now generalized/shared
#: across every ``reclaim_stale_running`` executor; this module just keeps
#: the historical name so existing call sites / tests are unaffected).
#: Crash recovery (``reclaim_stale_running``) re-runs a job whose worker
#: died mid-dispatch, but a job that crashes the worker *every* time would
#: steal itself forever — past this many EXPIRY reclaims it's failed +
#: bubbled instead (an ``epoch`` reclaim — redeploy churn — never counts
#: toward this cap; see ``_common.claim_executor_jobs``).
_MAX_ATTEMPTS = MAX_ATTEMPTS


def _lease_seconds(meta: dict[str, Any]) -> int:
    resources = (meta.get("params") or {}).get("resources") or {}
    wall = int(resources.get("wall_seconds", 0) or 0)
    return max(_LEASE_FLOOR_S, wall + _LEASE_MARGIN_S)


def _deadline_epoch(meta: dict[str, Any]) -> float:
    """Wall-clock kill deadline (unix epoch seconds) for a detached
    submit (§H piece 2) — stamped once at submit time so ``_poll_one``
    has a termination bound even though a never-completing ``poll`` would
    otherwise renew the lease forever (no other wall-clock backstop exists
    once the sweeper retires for lease-owning executors, §H piece 6).

    Mirrors ``claude_docker``'s ``deadline = time.time() + wall_seconds``
    shape: the job's declared ``resources.wall_seconds`` plus the same
    margin :func:`_lease_seconds` uses, when declared; falls back to the
    lease horizon itself (:func:`_lease_seconds`, which is already
    ``max(floor, wall + margin)``) when the job declares no wall_seconds
    at all — so an undeclared-budget job still gets SOME bound rather than
    an unbounded poll.
    """
    resources = (meta.get("params") or {}).get("resources") or {}
    wall = int(resources.get("wall_seconds", 0) or 0)
    if wall:
        return time.time() + wall + _LEASE_MARGIN_S
    return time.time() + _lease_seconds(meta)


#: Job_type names already warned-about this process (§H piece 4): the
#: legacy-blocking-dispatch deprecation fires once per job_type, not once
#: per job — an unmigrated plugin still blocking every pass is a per-type
#: operational fact, not per-job noise.
_warned_legacy_job_types: set[str] = set()


def run_ssh_node_pass(store: Store, *, limit: int = 1) -> dict[str, int]:
    """Process up to ``limit`` ssh_node jobs.

    Returns ``{claimed, ok, failed}`` for runner aggregation. ``claimed``
    counts newly (re)claimed rows this tick (submit/dispatch attempts);
    ``ok``/``failed`` cover BOTH the poll step (a handle driven to a clean
    terminal state, or one that raised) and the claim step (a submit/
    dispatch that succeeded/raised, or a poison-fail). Default ``limit=1``
    because a legacy blocking ``dispatch`` still occupies the worker
    thread for the whole run — a submit/poll-only fleet could safely
    raise this.
    """
    node = os.environ.get("PRECIS_NODE")
    ok = 0
    failed = 0

    # 0) Poll in-flight detached jobs pinned to this host (§H piece 4) —
    # cheap, never blocks longer than one status check per handle.
    for ref_id, title, meta in _polling_jobs(store, node):
        try:
            terminal = _poll_one(store, ref_id, title, meta)
        except Exception as exc:  # pragma: no cover — defensive
            failed += 1
            log.warning(
                "ssh_node: poll of job %d raised: %s", ref_id, exc, exc_info=True
            )
            continue
        if terminal:
            ok += 1
        # False (still running) is a no-op this tick — the lease was
        # already renewed inside _poll_one.

    poisoned = 0
    to_run: list[tuple[int, str, dict[str, Any]]] = []
    with store.pool.connection() as conn:
        # Node gate: only the node a job pins itself to (meta.params.
        # target_node) claims it, so the worker that stages to NFS is the
        # same box the container runs on (§23 #3). Parent gate: skip jobs
        # whose parent project is paused / halted / asking-user.
        # reclaim_stale_running: steal an expired-lease STATUS:running job whose
        # worker died mid-dispatch (e.g. a deploy restart) — for a legacy
        # blocking dispatch that means dead compute; a detached submit/poll
        # job's compute may still be alive — see ``_run_one``'s note on
        # re-adopting a row that already carries ``meta.compute_handle``.
        rows = claim_executor_jobs(
            conn,
            executor=_EXECUTOR_NAME,
            limit=limit,
            node=node,
            parent_not_paused=True,
            reclaim_stale_running=True,
            respect_reserve=True,
        )
        if not rows:
            conn.commit()
            return {"claimed": 0, "ok": ok, "failed": failed}
        for ref_id, title, meta in rows:
            # The bump (expiry-only) + reclaim classification already
            # happened inside claim_executor_jobs (§H piece 3);
            # poison_guard just applies the cap's consequence — an INFRA
            # failure (the worker/executor died mid-dispatch, not the
            # compute reporting a physical verdict), so a
            # `struct_relax`-style harvest reading `failure_class` must
            # not treat it as a rule-out.
            if _poison_guard(store, conn, ref_id, meta):
                poisoned += 1
                continue
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "  'lease_until', (now() + make_interval(secs => %s))::text"
                ") WHERE ref_id = %s",
                (_lease_seconds(meta), ref_id),
            )
            _set_status(store, ref_id, _RUNNING, conn=conn)
            to_run.append((ref_id, title, meta))
        conn.commit()

    failed += poisoned
    for ref_id, title, meta in to_run:
        try:
            if meta.get("compute_handle"):
                # Re-adopt (§H, REQUIRED — mirrors claude_docker's
                # re-adopt branch). This claim reclaimed (epoch or expiry
                # arm) a row whose plugin already called ``submit()``
                # under a PRIOR worker generation — the detached compute
                # survives a worker restart independent of the worker, so
                # calling ``submit()`` again here would double-launch it.
                # Never re-submit: STATUS:running + meta.compute_handle is
                # exactly what ``_polling_jobs`` selects, so THIS pass's
                # step-0 poll (or the next pass's, if this reclaim only
                # just re-stamped the stale lease_boot_id that was
                # tripping the epoch arm) resumes ownership by handle —
                # no relaunch. Counts as ok (no new failure surfaced by
                # the reclaim itself).
                _append_chunk(
                    store,
                    ref_id,
                    _JOB_EVENT_KIND,
                    f"runner: re-adopted job {ref_id} (compute_handle "
                    f"{meta.get('compute_handle')!r} already submitted by "
                    "a prior worker generation) — resuming poll, not "
                    "re-submitting",
                )
                ok += 1
                continue
            if meta.get("submit_started_at"):
                # gr191673: the prior generation died INSIDE the submit
                # window — after committing the intent marker, before
                # persisting the handle. The detached compute may or may
                # not be running, and there is no handle to adopt or
                # kill. Re-submitting is exactly the double-launch the
                # marker exists to prevent — fail loudly instead; the
                # event chunk is the pointer for finding the orphan.
                _record_failure(
                    store,
                    ref_id,
                    "submit outcome unknown: worker died between "
                    "submit() and the compute_handle persist "
                    f"(submit_started_at={meta.get('submit_started_at')})"
                    " — a detached compute may be orphaned on the "
                    "target node; not resubmitting (gr191673)",
                    gripe_rollback=None,
                    failure_class="infra",
                )
                # Clear the marker so a deliberate requeue can run.
                with store.pool.connection() as conn:
                    _set_meta(conn, ref_id, submit_started_at=None)
                    conn.commit()
                failed += 1
                continue
            _run_one(store, ref_id, title, meta)
            ok += 1
        except Exception as exc:  # pragma: no cover — defensive
            failed += 1
            log.warning("ssh_node: job %d raised: %s", ref_id, exc, exc_info=True)
            try:
                with store.pool.connection() as conn:
                    _append_chunk(
                        store,
                        ref_id,
                        _JOB_EVENT_KIND,
                        f"runner: uncaught exception: {exc!r}",
                        conn=conn,
                    )
                    # An uncaught exception in the dispatcher is the executor
                    # itself dying, not a physical verdict — INFRA (see
                    # ``_MAX_ATTEMPTS`` guard above for the same reasoning).
                    _set_meta(conn, ref_id, failure_class="infra")
                    _set_status(store, ref_id, _FAILED, conn=conn)
                    conn.commit()
            except Exception:  # pragma: no cover
                log.warning("ssh_node: failed to record failure", exc_info=True)
    return {"claimed": len(rows), "ok": ok, "failed": failed}


def _polling_jobs(
    store: Store, node: str | None
) -> list[tuple[int, str, dict[str, Any]]]:
    """In-flight detached ssh_node jobs (``STATUS:running`` +
    ``meta.compute_handle``) pinned to this host — mirrors
    ``claude_docker._running_jobs``. A job whose plugin never migrated to
    submit/poll never carries ``compute_handle``, so it's naturally
    invisible here (it's driven the old way, inline in ``_run_one``)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, r.meta
              FROM refs r
             WHERE r.kind = 'job'
               AND r.deleted_at IS NULL
               AND r.meta->>'executor' = %s
               AND r.meta ? 'compute_handle'
               AND (r.meta->'params'->>'target_node') IS NOT DISTINCT FROM %s
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS'
                        AND t.value = %s
                   )
             ORDER BY r.ref_id
            """,
            (_EXECUTOR_NAME, node, _RUNNING),
        ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2] or {})) for r in rows]


def _kill_and_terminalize(
    store: Store,
    ref_id: int,
    title: str,
    meta: dict[str, Any],
    handle: Any,
    spec: Any,
    *,
    swept_tag: str,
    summary: str,
) -> None:
    """Shared terminalize-via-kill path for both the wall-clock deadline and
    the §B-2 operator ``kill_requested`` backstop: call the job_type's
    optional ``spec.kill``, then a terminal ``STATUS:failed`` + the given
    ``swept:<tag>`` + a bubble to the parent — and, when the job's resolved
    requirements include ``gpu``, a best-effort GPU reclaim recorded on
    ``meta.kill_gpu_reset`` (:func:`_common.maybe_reset_gpu_after_kill`)."""
    ctx = _build_dispatch_context(store, ref_id, title, meta)
    if spec.kill is not None:
        try:
            spec.kill(ctx, handle)
        except Exception:  # pragma: no cover — defensive
            log.warning(
                "ssh_node: kill(handle=%r) of job %d raised",
                handle,
                ref_id,
                exc_info=True,
            )
    from precis.handlers._job_bubble import bubble_job_failure
    from precis.store import Tag

    gpu_reset = _maybe_reset_gpu_after_kill(meta)
    with store.pool.connection() as conn:
        _append_chunk(store, ref_id, _JOB_EVENT_KIND, summary, conn=conn)
        store.add_tag(
            ref_id,
            Tag.parse_strict(f"swept:{swept_tag}"),
            set_by="system",
            conn=conn,
        )
        if gpu_reset is not None:
            _set_meta(conn, ref_id, kill_gpu_reset=gpu_reset)
        _set_status(store, ref_id, _FAILED, conn=conn)
        conn.commit()
    bubble_job_failure(store, ref_id)


def _poll_one(store: Store, ref_id: int, title: str, meta: dict[str, Any]) -> bool:
    """Poll one in-flight detached job via its job_type's ``poll``.

    Returns ``True`` once the plugin has driven the job to a terminal
    ``STATUS`` (via ``ctx`` — this function does nothing further, OR this
    function drove it terminal itself via the kill checks below);
    ``False`` while still running, having renewed the lease here (``poll``
    itself must not touch the lease — that's the executor's job, exactly
    like the claim-time stamp)."""
    job_type_name = meta.get("job_type")
    spec = get_job_type(str(job_type_name)) if job_type_name else None
    handle = meta.get("compute_handle")
    if spec is None or spec.poll is None or handle is None:
        # Shouldn't happen — ``compute_handle`` is only ever stamped right
        # after a successful ``spec.submit`` call, for a job_type that (by
        # definition, to have reached submit) had ``spec.poll`` set too.
        # Defensive INFRA-fail rather than polling nothing forever.
        _record_failure(
            store,
            ref_id,
            "runner: in-flight job has no resolvable poll() (missing "
            "job_type / plugin / compute_handle) — cannot continue polling",
            gripe_rollback=None,
            failure_class="infra",
        )
        return True

    # §B-2 operator kill backstop (`precis jobs kill`) — takes precedence
    # over the wall-clock deadline below (an operator kill is deliberate;
    # honor it even if the deadline hasn't tripped yet). Same terminal path
    # as a deadline kill, just a different swept tag/summary.
    kill_requested = meta.get("kill_requested")
    if kill_requested:
        note = kill_requested.get("note") if isinstance(kill_requested, dict) else None
        summary = (
            f"runner: killed by operator ({note})"
            if note
            else "runner: killed by operator"
        )
        _kill_and_terminalize(
            store,
            ref_id,
            title,
            meta,
            handle,
            spec,
            swept_tag="killed-by-operator",
            summary=summary,
        )
        return True

    # §H piece 2: a never-completing ``poll`` would otherwise renew the
    # lease forever below — with the sweeper's wall-clock retirement (§H
    # piece 6) there is no OTHER termination path for a detached submit/
    # poll job_type. Past ``meta.deadline``, stop polling and terminalize
    # here instead — give the plugin a chance to actually stop the remote
    # compute first (``spec.kill``, optional; mirrors ``claude_docker``'s
    # own wall-clock deadline kill in ``_poll_job``).
    deadline = meta.get("deadline")
    if deadline is not None and time.time() > float(deadline):
        _kill_and_terminalize(
            store,
            ref_id,
            title,
            meta,
            handle,
            spec,
            swept_tag="wall-timeout",
            summary=f"runner: killed at wall-clock deadline (handle {handle!r})",
        )
        return True

    ctx = _build_dispatch_context(store, ref_id, title, meta)
    terminal = bool(spec.poll(ctx, handle))
    if not terminal:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "  'lease_until', (now() + make_interval(secs => %s))::text"
                ") WHERE ref_id = %s",
                (_lease_seconds(meta), ref_id),
            )
            conn.commit()
    return terminal


def _run_one(store: Store, ref_id: int, title: str, meta: dict[str, Any]) -> None:
    """Dispatch one claimed job — detached submit (§H piece 4, preferred)
    when the job_type exposes ``submit``/``poll``, else the legacy
    blocking ``dispatch`` (deprecated, gr187627)."""
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

    # Cooperative cancel before doing remote work.
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

    if spec.submit is not None and spec.poll is not None:
        # gr191673: commit a submit-INTENT marker before launching the
        # detached compute. A worker crash between ``submit()`` and the
        # handle-persist tx below leaves STATUS:running with no
        # ``compute_handle`` — invisible to ``_polling_jobs`` — and the
        # reclaim used to re-enter here and launch a SECOND compute
        # while the first leaked. With the marker committed first, the
        # reclaim path can tell "never submitted" (safe to run) from
        # "submit outcome unknown" (fail loudly, never blind-resubmit).
        with store.pool.connection() as conn:
            _set_meta(conn, ref_id, submit_started_at=time.time())
            conn.commit()
        ctx = _build_dispatch_context(store, ref_id, title, meta)
        try:
            handle = spec.submit(ctx, spec)
        except BaseException:
            # In-process submit failure routes to the pass loop's
            # failure handler (STATUS:failed) — clear the marker so a
            # deliberate requeue isn't mistaken for a mid-submit crash.
            with store.pool.connection() as conn:
                _set_meta(conn, ref_id, submit_started_at=None)
                conn.commit()
            raise
        with store.pool.connection() as conn:
            # §H piece 2: stamp a wall-clock kill deadline alongside the
            # handle — see ``_deadline_epoch`` / ``_poll_one``'s docstrings
            # for why a detached submit/poll job otherwise has no
            # termination path at all.
            _set_meta(
                conn,
                ref_id,
                compute_handle=handle,
                deadline=_deadline_epoch(meta),
                submit_started_at=None,
            )
            conn.commit()
        return

    if spec.dispatch is None:
        _record_failure(
            store,
            ref_id,
            f"job_type {spec.name!r} has no submit/poll or dispatch; "
            "ssh_node runs plugin dispatchers only",
            gripe_rollback=None,
            failure_class="infra",
        )
        return

    if spec.name not in _warned_legacy_job_types:
        _warned_legacy_job_types.add(spec.name)
        log.warning(
            "ssh_node: job_type %r uses the legacy blocking `dispatch` "
            "(deprecated, gr187627) — it will occupy this worker's whole "
            "pass rotation for the run's duration (heartbeats/nursery/"
            "sweeper stall alongside it); migrate to submit/poll "
            "(precis.workers.job_types.JobTypeSpec) when able",
            spec.name,
        )

    ctx = _build_dispatch_context(store, ref_id, title, meta)
    spec.dispatch(ctx, spec)


__all__ = ["run_ssh_node_pass"]
