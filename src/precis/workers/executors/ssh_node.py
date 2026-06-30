"""ssh_node executor — claim a job and run it on a remote node.

Sibling of :mod:`claude_inproc` / :mod:`coordinator` (ADR 0017): a
``run_ssh_node_pass`` function the CLI registers as a ``RefPass``.
Where ``claude_inproc`` spawns ``claude -p`` locally, ``ssh_node``
hands the job to its plugin ``dispatch(ctx, spec)`` callable, which
typically shells out to ``ssh <node> docker run …`` and blocks until
the remote workload finishes (precis-dft's ``gpaw_relax`` is the
first consumer).

This executor runs **plugin dispatchers only** — it has no in-tree
built-in switch (``claude_inproc`` keeps ``fix_gripe`` / ``plan_tick``).
A job_type compatible with ``ssh_node`` must declare ``spec.dispatch``.

Each pass:

1. Claim up to ``limit`` ``kind='job'`` rows with
   ``meta.executor == 'ssh_node'``, ``STATUS:queued``, lease expired
   or absent, not terminal — ``FOR UPDATE … SKIP LOCKED``.
2. Lease + ``STATUS:running`` under the claim tx. The lease must
   outlive the remote job (GPAW relaxes run for hours), so it's sized
   from the job's ``resources.wall_seconds`` plus margin.
3. Per job: resolve the job_type, cancel-poll, then
   ``spec.dispatch(ctx, spec)``. The dispatcher owns its terminal
   ``STATUS`` transition via the ctx.

Concurrency: remote workloads are heavy and the dispatch blocks the
worker thread for the whole run, so the default ``limit`` is 1.
"""

from __future__ import annotations

import logging
import os
from typing import Any

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
    RUNNING as _RUNNING,
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
    set_status as _set_status,
)

# Reuse the DispatchContext builder the coordinator already shares
# from claude_inproc (it's executor-agnostic — closures over the
# store handle + ref_id).
from precis.workers.executors.claude_inproc import _build_dispatch_context
from precis.workers.job_types import get_job_type, known_job_types

log = logging.getLogger(__name__)

_EXECUTOR_NAME = "ssh_node"

#: Lease floor + margin (seconds). The lease is
#: ``max(_LEASE_FLOOR_S, resources.wall_seconds + _LEASE_MARGIN_S)``
#: so a slow GPAW relax can't expire its own lease and get
#: double-claimed mid-run.
_LEASE_FLOOR_S = 7200
_LEASE_MARGIN_S = 3600


def _lease_seconds(meta: dict[str, Any]) -> int:
    resources = (meta.get("params") or {}).get("resources") or {}
    wall = int(resources.get("wall_seconds", 0) or 0)
    return max(_LEASE_FLOOR_S, wall + _LEASE_MARGIN_S)


def run_ssh_node_pass(store: Any, *, limit: int = 1) -> dict[str, int]:
    """Process up to ``limit`` ssh_node jobs.

    Returns ``{claimed, ok, failed}`` for runner aggregation. Default
    ``limit=1`` because each dispatch blocks the worker on a
    multi-hour remote run.
    """
    with store.pool.connection() as conn:
        # Node gate: only the node a job pins itself to (meta.params.
        # target_node) claims it, so the worker that stages to NFS is the
        # same box the container runs on (§23 #3). Parent gate: skip jobs
        # whose parent project is paused / halted / asking-user.
        rows = claim_executor_jobs(
            conn,
            executor=_EXECUTOR_NAME,
            limit=limit,
            node=os.environ.get("PRECIS_NODE"),
            parent_not_paused=True,
        )
        if not rows:
            conn.commit()
            return {"claimed": 0, "ok": 0, "failed": 0}
        for ref_id, _title, meta in rows:
            conn.execute(
                "UPDATE refs SET meta = meta || "
                "jsonb_build_object("
                "  'lease_until', (now() + make_interval(secs => %s))::text"
                ") "
                "WHERE ref_id = %s",
                (_lease_seconds(meta), ref_id),
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
                    _set_status(store, ref_id, _FAILED, conn=conn)
                    conn.commit()
            except Exception:  # pragma: no cover
                log.warning("ssh_node: failed to record failure", exc_info=True)
    return {"claimed": len(rows), "ok": ok, "failed": failed}


def _run_one(store: Any, ref_id: int, title: str, meta: dict[str, Any]) -> None:
    """Dispatch one claimed job to its plugin ``dispatch(ctx, spec)``."""
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

    if spec.dispatch is None:
        _record_failure(
            store,
            ref_id,
            f"job_type {spec.name!r} has no dispatch; ssh_node runs plugin "
            "dispatchers only",
            gripe_rollback=None,
        )
        return

    ctx = _build_dispatch_context(store, ref_id, title, meta)
    spec.dispatch(ctx, spec)


__all__ = ["run_ssh_node_pass"]
