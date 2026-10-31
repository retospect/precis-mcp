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
   piece 2), the pass terminalizes the job itself. A legacy process
   handle first needs a same-pass live poll on its owning node to authorize
   that deadline kill. A never-completing ``poll`` would otherwise renew the
   lease forever, and the sweeper's own wall-clock retirement (§H piece 6)
   leaves no other termination path for a detached job_type.
0.5. **Reap** detached orphans (gr520866): a terminal job still carrying
   ``meta.compute_handle`` — terminalized by a path that skipped
   ``spec.kill`` (the crash-loop guard, a sweeper transition, a direct
   STATUS tag) — leaves its detached child running unbounded.
   :func:`_reap_detached_orphans` claims unreleased handles after a 120s
   grace and calls the plugin's ``kill`` only when boot ID and process
   start ticks match on this host. Unverifiable identities are skipped.
1. Claim up to ``limit`` ``kind='job'`` rows with
   ``meta.executor == 'ssh_node'``, ``STATUS:queued``, lease expired
   or absent, not terminal — ``FOR UPDATE … SKIP LOCKED``.
2. Lease + ``STATUS:running`` under the claim tx. The lease must
   outlive the compute (GPAW relaxes run for hours), so it's sized from
   ``resources.wall_seconds`` or a declared job-type cap, plus margin.
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
import math
import os
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.workers.executors._common import (
    _ERROR_META_CAP,
    MAX_ATTEMPTS,
    claim_executor_jobs,
)
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
    RECLAIM_WHY_READOPT as _RECLAIM_WHY_READOPT,
)
from precis.workers.executors._common import (
    RUNNING as _RUNNING,
)
from precis.workers.executors._common import (
    TERMINAL as _TERMINAL,
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
from precis.workers.job_types import JobTypeSpec, get_job_type, known_job_types

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

_EXECUTOR_NAME = "ssh_node"

#: Lease floor + margin (seconds). The lease is
#: ``max(_LEASE_FLOOR_S, max(resources.wall_seconds, type cap) +
#: _LEASE_MARGIN_S)`` when either budget exists, so a slow relax
#: cannot expire its own lease and get double-claimed mid-run.
_LEASE_FLOOR_S = 7200
_LEASE_MARGIN_S = 3600

#: Max run-attempts before a job is treated as poison — re-exported from
#: ``_common`` (§H piece 3: the bump + the cap are now generalized/shared
#: across every ``reclaim_stale_running`` executor; this module just keeps
#: the historical name so existing call sites / tests are unaffected).
#: Crash recovery (``reclaim_stale_running``) re-runs a job whose worker
#: died mid-dispatch, but a job that crashes the worker *every* time would
#: steal itself forever — epoch and expiry reclaims both exhaust the
#: budget; the reason remains visible in meta.reclaims.
_MAX_ATTEMPTS = MAX_ATTEMPTS


def _lease_seconds(meta: dict[str, Any], spec: JobTypeSpec | None = None) -> int:
    """Size the first lease from the same wall bound as blocking dispatch."""
    return max(_LEASE_FLOOR_S, _wall_bound_seconds(meta, spec))


def _deadline_epoch(meta: dict[str, Any], spec: JobTypeSpec | None = None) -> float:
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
    return time.time() + _wall_bound_seconds(meta, spec)


def _wall_bound_seconds(meta: dict[str, Any], spec: JobTypeSpec | None = None) -> int:
    """Wall-clock bound (seconds) for one job: declared
    ``resources.wall_seconds`` or the job type's own cap, whichever is
    longer, plus :data:`_LEASE_MARGIN_S`; the lease floor when neither is
    declared. Shared by the initial lease, detached deadline and legacy
    blocking-dispatch cutoff, so a type's compute cannot outlive the lease
    before it reaches its own configured timeout."""
    resources = (meta.get("params") or {}).get("resources") or {}
    wall = int(resources.get("wall_seconds", 0) or 0)
    type_cap = (
        math.ceil(spec.wall_seconds())
        if spec is not None and spec.wall_seconds is not None
        else 0
    )
    if wall or type_cap:
        return max(wall, type_cap) + _LEASE_MARGIN_S
    return _LEASE_FLOOR_S


#: Job_type names already warned-about this process (§H piece 4): the
#: legacy-blocking-dispatch deprecation fires once per job_type, not once
#: per job — an unmigrated plugin still blocking every pass is a per-type
#: operational fact, not per-job noise.
_warned_legacy_job_types: set[str] = set()

#: How many orphaned detached computes one pass visits — bounded so a deep
#: terminal backlog can't make a tick expensive; drains oldest-first.
_ORPHAN_REAP_LIMIT = 25


# Let a terminal poll finish exiting before considering its handle orphaned.
_ORPHAN_REAP_GRACE_S = 120


def _detached_pgid(handle: Any) -> int | None:
    """Validate the exact group the kill hook uses; never fall back to pid."""
    if not isinstance(handle, dict):
        return None
    value = handle.get("pgid")
    if value is None:
        return None
    try:
        pgid = int(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if pgid <= 1 or pgid == os.getpgrp():
        return None
    return pgid


def _detached_group_alive(handle: Any) -> bool:
    """Probe group liveness; this alone never establishes process identity."""
    pgid = _detached_pgid(handle)
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except PermissionError:
        # Not ours: permission to observe a group is not permission to kill it.
        return False
    except OSError:
        return False


def _detached_identity_reason(handle: Any) -> str | None:
    """Fail closed unless the local group leader has the recorded identity."""
    pgid = _detached_pgid(handle)
    if pgid is None:
        return "invalid-handle"
    if not handle.get("boot_id") or handle.get("start_time") is None:
        return "missing-identity"
    try:
        start_time = int(handle["start_time"])
        if start_time < 0:
            return "invalid-handle"
        if (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            != handle["boot_id"]
        ):
            return "boot-id-mismatch"
        # comm (field 2) can include spaces and ')'. Field 22 is offset
        # 19 after its final ')', not offset 21 of whitespace-split stat.
        try:
            stat = Path(f"/proc/{pgid}/stat").read_text(
                encoding="utf-8", errors="replace"
            )
        except FileNotFoundError:
            # Boot matched, but a missing leader does not prove its group
            # is gone: descendants can outlive it. Only ESRCH permits
            # scratch cleanup; this path never authorizes a kill.
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return "group-unavailable"
            except OSError:
                pass
            return "identity-unreadable"
        if int(stat.rsplit(")", 1)[1].split()[19]) != start_time:
            return "start-time-mismatch"
    except (OSError, ValueError, TypeError, IndexError, UnicodeError):
        return "identity-unreadable"
    return None


def _readoptable(meta: dict[str, Any], node: str | None) -> bool:
    """True when a reclaimed row's detached compute is provably still the
    process this node launched and still inside its wall-clock deadline.

    Identity is boot ID + process start time of the group leader
    (:func:`_detached_identity_reason`), not pid alone — pids recycle
    across a day of restarts — plus a live group.  A row with no
    ``meta.deadline`` is never re-adoptable: without the bound a hung
    child could be re-adopted forever (gr541147)."""
    handle = meta.get("compute_handle")
    if not isinstance(handle, dict) or meta.get("compute_handle_released"):
        return False
    if (meta.get("params") or {}).get("target_node") != node:
        return False
    deadline = meta.get("deadline")
    try:
        if deadline is None or time.time() > float(deadline):
            return False
    except (TypeError, ValueError):
        return False
    return _detached_identity_reason(handle) is None and _detached_group_alive(handle)


def _refund_readopt_attempt(
    store: Store, conn: Any, ref_id: int, meta: dict[str, Any]
) -> None:
    """Undo the attempt ``claim_executor_jobs`` just counted for this
    reclaim and relabel it a re-adoption: a worker restart that finds the
    job's compute alive is not the job crashing.  Before gr541147 three
    deploys failed any seed longer than three restart gaps, and the
    poison path then killed its healthy child."""
    reclaims = list(meta.get("reclaims") or [])
    if not reclaims or reclaims[-1].get("why") == _RECLAIM_WHY_READOPT:
        return
    reclaims[-1] = {**reclaims[-1], "why": _RECLAIM_WHY_READOPT}
    attempts = max(0, int(meta.get("attempts") or 0) - 1)
    meta["attempts"] = attempts
    meta["reclaims"] = reclaims
    _set_meta(conn, ref_id, attempts=attempts, reclaims=reclaims)
    _append_chunk(
        store,
        ref_id,
        _JOB_EVENT_KIND,
        f"runner: worker restart found detached compute pid="
        f"{meta['compute_handle'].get('pid')} alive — re-adoption, not "
        "counted as a run attempt",
        conn=conn,
    )


def _orphaned_detached_jobs(
    store: Store, node: str | None, *, limit: int
) -> list[tuple[int, str, dict[str, Any]]]:
    """Unreleased identity-bearing handles, terminal on this node for 120s."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, r.meta
              FROM refs r
             WHERE r.kind = 'job'
               AND r.retired_at IS NULL
               AND r.meta->>'executor' = %s
               AND r.meta->'compute_handle' ? 'boot_id'
               AND r.meta->>'compute_handle_released' IS DISTINCT FROM 'true'
               AND NOT (r.meta ? 'detached_orphan_reaped')
               AND NOT (r.meta ? 'detached_orphan_skipped')
               AND (r.meta->'params'->>'target_node') = %s
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS'
                        AND t.value = ANY(%s)
                        AND rt.created_at < now() - make_interval(secs => %s)
                   )
             ORDER BY r.ref_id
             LIMIT %s
            """,
            (_EXECUTOR_NAME, node, list(_TERMINAL), _ORPHAN_REAP_GRACE_S, limit),
        ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2] or {})) for r in rows]


def _reap_detached_job(
    store: Store,
    conn: Any,
    ref_id: int,
    title: str,
    meta: dict[str, Any],
    *,
    node: str | None,
    require_grace: bool = True,
) -> bool:
    """Claim and identity-check one orphan, holding the row lock through cleanup.

    The conditional UPDATE arbitrates competing passes. The provisional
    stamp is replaced by the final outcome in the same transaction; a
    competing UPDATE rechecks its predicates after the winner commits.
    Poison-guard uses its existing claim transaction and bypasses grace.
    """
    from psycopg.types.json import Jsonb

    from precis.store import Tag

    handle = meta.get("compute_handle")
    stamp = {"at": datetime.now(UTC).isoformat(), "action": "claimed"}
    claimed = conn.execute(
        """
        UPDATE refs r
           SET meta = meta || jsonb_build_object('detached_orphan_reaped', %s::jsonb)
         WHERE r.ref_id = %s
           AND r.kind = 'job' AND r.retired_at IS NULL
           AND r.meta->>'executor' = %s
           AND NOT (r.meta ? 'detached_orphan_reaped')
           AND NOT (r.meta ? 'detached_orphan_skipped')
           AND r.meta->>'compute_handle_released' IS DISTINCT FROM 'true'
           AND r.meta->'compute_handle' = %s::jsonb
           AND r.meta->'params'->>'target_node' = %s
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = 'STATUS' AND t.value = ANY(%s)
                    AND (NOT %s OR
                         rt.created_at < now() - make_interval(secs => %s))
               )
         RETURNING r.ref_id, r.meta ? 'finished_at'
        """,
        (
            Jsonb(stamp),
            ref_id,
            _EXECUTOR_NAME,
            Jsonb(handle),
            node,
            list(_TERMINAL),
            require_grace,
            _ORPHAN_REAP_GRACE_S,
        ),
    ).fetchone()
    if claimed is None:
        return False

    reason = _detached_identity_reason(handle)
    spec = get_job_type(str(meta.get("job_type") or ""))
    if reason is None and not _detached_group_alive(handle):
        reason = "group-unavailable"
    if reason is None and (spec is None or spec.kill is None):
        reason = "no-kill-hook"
    ctx = replace(
        _build_dispatch_context(store, ref_id, title, meta),
        completion_stamped=bool(claimed[1]),
    )
    if reason is None:
        assert spec is not None and spec.kill is not None
        try:
            spec.kill(ctx, handle)
        except Exception:  # pragma: no cover — defensive
            reason = "kill-raised"
            log.warning("ssh_node: orphan kill of job %d raised", ref_id, exc_info=True)
    if (
        reason in (None, "group-unavailable")
        and ctx.completion_stamped
        and spec is not None
        and spec.reap is not None
    ):
        # A missing group needs no signal, but its terminal job still owns
        # scratch. Identity failures must retain the directory untouched.
        try:
            spec.reap(ctx, handle)
        except Exception:  # pragma: no cover — defensive
            log.warning("ssh_node: orphan reap of job %d raised", ref_id, exc_info=True)
    if reason is not None:
        conn.execute(
            "UPDATE refs SET meta = (meta - 'detached_orphan_reaped') || "
            "jsonb_build_object('detached_orphan_skipped', %s::jsonb) WHERE ref_id = %s",
            (Jsonb({"at": datetime.now(UTC).isoformat(), "reason": reason}), ref_id),
        )
        summary = f"runner: skipped orphaned detached compute — {reason}"
    else:
        _set_meta(
            conn,
            ref_id,
            detached_orphan_reaped={
                "at": datetime.now(UTC).isoformat(),
                "action": "killed",
            },
            compute_handle_released=True,
        )
        store.add_tag(
            ref_id, Tag.open("reaped:detached-orphan"), set_by="system", conn=conn
        )
        summary = "runner: reaped orphaned detached compute of terminal job — killed"
    _append_chunk(store, ref_id, _JOB_EVENT_KIND, summary, conn=conn)
    return True


def _reap_detached_orphans(
    store: Store, node: str | None, *, limit: int = _ORPHAN_REAP_LIMIT
) -> int:
    """Backstop external terminalizations; legacy/released handles are excluded.

    No kill without matching boot ID and process start ticks. A missing
    leader permits only scratch cleanup after matching boot and confirming
    the entire group is gone. Claim and final stamp share a transaction.
    """
    reaped = 0
    for ref_id, title, meta in _orphaned_detached_jobs(store, node, limit=limit):
        with store.pool.connection() as conn:
            reaped += _reap_detached_job(store, conn, ref_id, title, meta, node=node)
            conn.commit()
    return reaped


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
            # Built once here — the (store, ref_id, title, meta) cluster
            # this job needs, threaded onward through _poll_one /
            # _kill_and_terminalize as one DispatchContext.
            ctx = _build_dispatch_context(store, ref_id, title, meta)
            terminal = _poll_one(ctx)
        except Exception as exc:  # pragma: no cover — defensive
            failed += 1
            log.warning(
                "ssh_node: poll of job %d raised: %s", ref_id, exc, exc_info=True
            )
            continue
        if terminal:
            ok += 1
        # False (still running) is a no-op: _poll_one renewed the lease
        # or deferred a contended legacy deadline until the next pass.

    # 0.5) Reap detached computes left orphaned by terminalization paths
    # that skipped spec.kill (gr520866: the poison-guard fail below, the
    # sweeper's dead-node/off-lane transitions, a direct STATUS tag) —
    # before claiming anything new, so freed memory/GPU is back before
    # the next submit.
    _reap_detached_orphans(store, node)

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
            # The bump (epoch or expiry) + reclaim classification already
            # happened inside claim_executor_jobs (§H piece 3);
            # poison_guard just applies the cap's consequence — an INFRA
            # failure (the worker/executor died mid-dispatch, not the
            # compute reporting a physical verdict), so a
            # `struct_relax`-style harvest reading `failure_class` must
            # not treat it as a rule-out. A reclaim that finds its
            # detached compute provably alive is refunded first: restarts
            # are not crashes (gr541147).
            if _readoptable(meta, node):
                _refund_readopt_attempt(store, conn, ref_id, meta)
            if _poison_guard(store, conn, ref_id, meta):
                if (
                    node is not None
                    and (meta.get("params") or {}).get("target_node") == node
                    and meta.get("compute_handle")
                    and not meta.get("compute_handle_released")
                ):
                    _reap_detached_job(
                        store,
                        conn,
                        ref_id,
                        title,
                        meta,
                        node=node,
                        require_grace=False,
                    )
                poisoned += 1
                continue
            spec = get_job_type(str(meta.get("job_type") or ""))
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "  'lease_until', (now() + make_interval(secs => %s))::text"
                ") WHERE ref_id = %s",
                (_lease_seconds(meta, spec), ref_id),
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
            ctx = _build_dispatch_context(store, ref_id, title, meta)
            if _run_one(ctx):
                # Blocking dispatch cut off at its wall-clock bound: the
                # job was failed by ``_dispatch_bounded``.
                failed += 1
            else:
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
                    _set_meta(
                        conn,
                        ref_id,
                        failure_class="infra",
                        error=repr(exc)[:_ERROR_META_CAP],
                    )
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
               AND r.retired_at IS NULL
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


def _legacy_deadline_owned(conn: Any, ctx: DispatchContext, handle: Any) -> bool:
    """Recheck ownership without waiting; contention retries next pass."""
    from psycopg.types.json import Jsonb

    node = os.environ.get("PRECIS_NODE")
    if node is None:
        return False
    row = conn.execute(
        """
        SELECT r.ref_id FROM refs r
         WHERE r.ref_id = %s AND r.kind = 'job' AND r.retired_at IS NULL
           AND r.meta->>'executor' = %s
           AND r.meta->'params'->>'target_node' = %s
           AND r.meta->'compute_handle' = %s::jsonb
           AND r.meta->>'compute_handle_released' IS DISTINCT FROM 'true'
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = 'STATUS' AND t.value = %s
               )
         FOR UPDATE OF r NOWAIT
        """,
        (ctx.ref_id, _EXECUTOR_NAME, node, Jsonb(handle), _RUNNING),
    ).fetchone()
    return row is not None


def _kill_and_terminalize(
    ctx: DispatchContext,
    handle: Any,
    spec: Any,
    *,
    swept_tag: str,
    summary: str,
    failure_class: str | None = None,
    polled_alive: bool = False,
) -> bool:
    """Shared terminalize-via-kill path for both the wall-clock deadline and
    the §B-2 operator ``kill_requested`` backstop: call the job_type's
    optional ``spec.kill``, then a terminal ``STATUS:failed`` + the given
    ``swept:<tag>`` + a bubble to the parent — and, when the job's resolved
    requirements include ``gpu``, a best-effort GPU reclaim recorded on
    ``meta.kill_gpu_reset`` (:func:`_common.maybe_reset_gpu_after_kill`).

    Takes the caller's already-built :class:`DispatchContext` (``_poll_one``
    mints one per polled row) rather than the raw ``(store, ref_id, title,
    meta)`` cluster.

    ``failure_class`` (optional) is stamped on ``meta.failure_class`` with
    ``summary`` as ``meta.error``, so a deadline kill (``"timeout"``) can be
    counted from meta without reading each job's event log. ``polled_alive``
    is same-call evidence for the legacy deadline exception only: it still
    requires a locked running row owned by this node. Returns False on
    lock contention without changing the row; True after terminalization."""
    from psycopg.errors import LockNotAvailable

    from precis.store import Tag
    from precis.utils.job_bubble import bubble_job_failure

    store, ref_id, meta = ctx.store, ctx.ref_id, ctx.meta
    released = False
    # Opaque handles belong to other plugins' own kill protocols. Every
    # dict must validate the actual pgid before any process kill.
    process_handle = isinstance(handle, dict)
    skip_reason = _detached_identity_reason(handle) if process_handle else None
    with store.pool.connection() as conn:
        if (
            skip_reason == "missing-identity"
            and swept_tag in {"wall-timeout", "deadline"}
            and polled_alive
        ):
            try:
                owned = _legacy_deadline_owned(conn, ctx, handle)
            except LockNotAvailable:
                # No writes have occurred on this connection. Clear the
                # failed NOWAIT transaction and leave the row for next pass.
                conn.rollback()
                return False
            if owned:
                # This fact is local to _poll_one's current call, never persisted
                # in meta or accepted by terminal-orphan/poison/operator paths.
                skip_reason = None
        if process_handle and skip_reason is None and not _detached_group_alive(handle):
            skip_reason = "group-unavailable"
        if skip_reason is None and spec.kill is not None:
            try:
                spec.kill(ctx, handle)
                released = True
            except Exception:  # pragma: no cover — defensive
                log.warning(
                    "ssh_node: kill(handle=%r) of job %d raised",
                    handle,
                    ref_id,
                    exc_info=True,
                )
        gpu_reset = _maybe_reset_gpu_after_kill(meta) if released else None
        _append_chunk(store, ref_id, _JOB_EVENT_KIND, summary, conn=conn)
        store.add_tag(
            ref_id,
            Tag.parse_strict(f"swept:{swept_tag}"),
            set_by="system",
            conn=conn,
        )
        if skip_reason is not None:
            _set_meta(
                conn,
                ref_id,
                detached_orphan_skipped={
                    "at": datetime.now(UTC).isoformat(),
                    "reason": skip_reason,
                },
            )
        if released:
            _set_meta(conn, ref_id, compute_handle_released=True)
        if gpu_reset is not None:
            _set_meta(conn, ref_id, kill_gpu_reset=gpu_reset)
        if failure_class is not None:
            _set_meta(conn, ref_id, failure_class=failure_class, error=summary)
        _set_status(store, ref_id, _FAILED, conn=conn)
        conn.commit()
    bubble_job_failure(store, ref_id)
    if (released or skip_reason == "group-unavailable") and getattr(
        spec, "reap", None
    ) is not None:
        try:
            spec.reap(ctx, handle)
        except Exception:  # pragma: no cover — defensive
            log.warning(
                "ssh_node: reap(handle=%r) of job %d raised",
                handle,
                ref_id,
                exc_info=True,
            )
    return True


def _poll_one(ctx: DispatchContext) -> bool:
    """Poll one in-flight detached job via its job_type's ``poll``.

    Returns ``True`` once the plugin has driven the job to a terminal
    ``STATUS`` (via ``ctx`` — this function does nothing further, OR this
    function drove it terminal itself via the kill checks below);
    ``False`` while still running, normally renewing the lease here
    (``poll`` itself must not touch it). A busy legacy deadline row instead
    returns False without writes, so a later pass can retry the kill.

    Takes the caller's already-built :class:`DispatchContext` (the pass
    loop mints one per polled row) rather than the raw ``(store, ref_id,
    title, meta)`` cluster, and hands the SAME context on to
    :func:`_kill_and_terminalize` / ``spec.poll`` instead of rebuilding."""
    store, ref_id, meta = ctx.store, ctx.ref_id, ctx.meta
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
        return _kill_and_terminalize(
            ctx,
            handle,
            spec,
            swept_tag="killed-by-operator",
            summary=summary,
        )

    # §H piece 2: a never-completing ``poll`` would otherwise renew the
    # lease forever below — with the sweeper's wall-clock retirement (§H
    # piece 6) there is no OTHER termination path for a detached submit/
    # poll job_type. Past ``meta.deadline``, terminalize here (legacy
    # process handles first need a fresh live poll) — let the plugin stop the
    # compute first (``spec.kill``, optional; mirrors ``claude_docker``'s
    # own wall-clock deadline kill in ``_poll_job``).
    deadline = meta.get("deadline")
    if deadline is not None and time.time() > float(deadline):
        polled_alive = False
        node = os.environ.get("PRECIS_NODE")
        if (
            _detached_identity_reason(handle) == "missing-identity"
            and node is not None
            and (meta.get("params") or {}).get("target_node") == node
        ):
            # Legacy handles already running at deploy have no identity.
            # Only a fresh owning-executor poll may authorize their deadline
            # kill; a terminal poll finishes normally and needs no kill.
            if spec.poll(ctx, handle):
                return True
            polled_alive = _detached_group_alive(handle)
        return _kill_and_terminalize(
            ctx,
            handle,
            spec,
            swept_tag="wall-timeout",
            summary=f"runner: killed at wall-clock deadline (handle {handle!r})",
            failure_class="timeout",
            polled_alive=polled_alive,
        )

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


def _pin_to_submitting_node(conn: Any, ref_id: int, handle: Any) -> None:
    """Pin an un-pinned job to the node that just launched its compute.

    The handle names a process and a scratch dir on THIS host, so only this
    host can poll it. Left un-pinned, ``_polling_jobs`` on a ``PRECIS_NODE``
    worker never selects it (a NULL pin is claimable by any node but polled
    by none) and any node can reclaim it: 542535/542536 finished in two
    minutes and failed on the crash-loop guard three hours later, results
    uncollected. A node-less worker polls the NULL pin and needs nothing."""
    node = os.environ.get("PRECIS_NODE")
    if node is None or handle is None:
        return
    conn.execute(
        "UPDATE refs SET meta = meta || jsonb_build_object('params',"
        "  COALESCE(meta->'params', '{}'::jsonb)"
        "  || jsonb_build_object('target_node', %s::text))"
        " WHERE ref_id = %s AND meta->'params'->>'target_node' IS NULL",
        (node, ref_id),
    )


def _run_one(ctx: DispatchContext) -> bool:
    """Dispatch one claimed job — detached submit (§H piece 4, preferred)
    when the job_type exposes ``submit``/``poll``, else the legacy
    blocking ``dispatch`` (deprecated, gr187627).

    Takes the caller's already-built :class:`DispatchContext` (the pass
    loop mints one per freshly-claimed row) rather than the raw ``(store,
    ref_id, title, meta)`` cluster."""
    store, ref_id, meta = ctx.store, ctx.ref_id, ctx.meta
    job_type_name = meta.get("job_type")
    if not job_type_name:
        _record_failure(
            store,
            ref_id,
            "missing meta.job_type",
            gripe_rollback=None,
            failure_class="infra",
        )
        return False
    spec = get_job_type(str(job_type_name))
    if spec is None:
        _record_failure(
            store,
            ref_id,
            f"unknown job_type {job_type_name!r}; known: {known_job_types()}",
            gripe_rollback=None,
            failure_class="infra",
        )
        return False

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
            return False

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
                deadline=_deadline_epoch(meta, spec),
                submit_started_at=None,
            )
            _pin_to_submitting_node(conn, ref_id, handle)
            conn.commit()
        return False

    if spec.dispatch is None:
        _record_failure(
            store,
            ref_id,
            f"job_type {spec.name!r} has no submit/poll or dispatch; "
            "ssh_node runs plugin dispatchers only",
            gripe_rollback=None,
            failure_class="infra",
        )
        return False

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

    return _dispatch_bounded(ctx, spec)


def _dispatch_bounded(ctx: DispatchContext, spec: Any) -> bool:
    """Run the legacy blocking ``spec.dispatch`` under an outer wall-clock
    bound (gr336604). Without it a hung dispatch (no submit/poll, nothing
    observing ``kill_requested``) holds the worker pass for hours.

    The dispatch runs on a daemon thread; on expiry the job is failed
    (``failure_class="timeout"``) and the pass moves on. A Python thread
    cannot be killed, and a legacy dispatch has no handle for
    ``spec.kill``, so the hung thread (and any remote process it spawned)
    is abandoned, not reaped — the log line names the job so an operator
    can find the orphan. A dispatch that must be killable runs its compute
    through :func:`precis.workers.child_compute.run_in_child` with its own
    cap below this bound (``hexfold_relax``, gr506395): the child is killed
    and confirmed gone before the dispatch terminalizes the job, so this
    outer bound never fires on it.

    Returns ``True`` if the dispatch timed out and the job was failed here,
    ``False`` if it finished (or raised, which propagates) in time."""
    import dataclasses
    import threading

    bound = _wall_bound_seconds(ctx.meta, spec)
    outcome: dict[str, BaseException] = {}
    abandoned = threading.Event()

    def _warn_ignored(name: str) -> None:
        log.warning(
            "ssh_node: job %d: abandoned dispatch thread called ctx.%s "
            "after the wall-clock bound fired; ignored (gr336604)",
            ctx.ref_id,
            name,
        )

    def _guard(name: str, fn: Any, blocked: Any = None) -> Any:
        def _wrapped(*a: Any, **k: Any) -> Any:
            if abandoned.is_set():
                _warn_ignored(name)
                return blocked
            return fn(*a, **k)

        return _wrapped

    # Closures each open their own pool connection (see
    # ``_build_dispatch_context``), so the thread shares no connection or
    # transaction with the pass loop; the guard only silences job writes
    # once the bound has fired. ``ctx.store`` itself is deliberately NOT
    # guarded: a plugin writing through the raw store handle bypasses this.
    from precis.workers.executors._context import DispatchContext as _Ctx

    class _GuardedCtx(_Ctx):
        def spawn_child(self, *a: Any, **k: Any) -> Any:
            if abandoned.is_set():
                _warn_ignored("spawn_child")
                return None
            return super().spawn_child(*a, **k)

    gctx = _GuardedCtx(
        **{
            **{f.name: getattr(ctx, f.name) for f in dataclasses.fields(ctx)},
            "set_status": _guard("set_status", ctx.set_status),
            "append_chunk": _guard("append_chunk", ctx.append_chunk),
            "set_meta": _guard("set_meta", ctx.set_meta),
            "record_failure": _guard("record_failure", ctx.record_failure),
            "renew_lease": (
                _guard("renew_lease", ctx.renew_lease, blocked=False)
                if ctx.renew_lease is not None
                else None
            ),
        }
    )

    def _target() -> None:
        try:
            spec.dispatch(gctx, spec)
        except BaseException as exc:  # re-raised in the caller below
            outcome["exc"] = exc

    worker = threading.Thread(
        target=_target, name=f"ssh_node-dispatch-{ctx.ref_id}", daemon=True
    )
    worker.start()
    worker.join(bound)
    # Set BEFORE re-checking liveness: a thread finishing in the gap is
    # then treated as finished, and one still running can no longer write.
    abandoned.set()
    if worker.is_alive():
        with ctx.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
                "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
                (ctx.ref_id,),
            ).fetchone()
        if row is not None and row[0] != _RUNNING:
            # Reached a terminal state itself just before the guard closed.
            return False
        reason = (
            f"runner: blocking dispatch of job_type {spec.name!r} exceeded "
            f"its {bound}s wall-clock bound (gr336604) — failing the job; "
            "the dispatch thread and any remote process it started are NOT "
            "killed (legacy dispatch has no kill handle) and may be orphaned"
        )
        log.error("ssh_node: job %d: %s", ctx.ref_id, reason)
        _record_failure(
            ctx.store,
            ctx.ref_id,
            reason,
            gripe_rollback=None,
            failure_class="timeout",
        )
        return True
    if "exc" in outcome:
        raise outcome["exc"]
    return False


__all__ = ["run_ssh_node_pass"]
