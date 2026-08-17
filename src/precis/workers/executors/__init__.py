"""Executors — runner classes for `job` work.

A job_type declares what *needs* to happen (`fix_gripe` prepares a
candidate fix branch); an executor declares *how* it can happen.

The dispatcher matches the two at submit time: a `job_type`
declares `COMPATIBLE_EXECUTORS` (which executors can run it) and
`REQUIRES` (what the host must provide); each executor declares
`PROVIDES`. A put is rejected if executor ∉ COMPATIBLE_EXECUTORS
or REQUIRES \\ PROVIDES is non-empty.

The lanes
---------
* ``claude_inproc`` — spawns ``claude -p`` as a subprocess of the
  worker; the planner-coroutine lane (``plan_tick``, ``fix_gripe``, the
  cast composes). Claiming is gateway-only in practice (needs OAuth +
  ``PRECIS_MCP_CONFIG``) while minting is cluster-wide — the SPOF the
  nursery's ``dispatch-stall`` detector watches (gripe 55748).
* ``coordinator`` — the yield/resume executor for long-running
  coordinator job_types; dispatches child jobs, never computes.
* ``ssh_node`` — runs a job's plugin on a (possibly remote) compute
  node. A job_type exposing ``spec.submit``/``spec.poll`` runs DETACHED:
  ``submit`` launches (nohup / ``docker run -d`` / sbatch) and returns a
  handle persisted to ``meta.compute_handle``; each pass polls in-flight
  handles — a cheap status check + lease renewal, never a multi-hour
  block. A legacy blocking ``spec.dispatch`` still works but logs a
  deprecation naming gr187627 (a blocking call starved a worker's whole
  rotation long enough to trip host-dark). A reclaimed row that already
  carries a handle is re-adopted, never re-submitted.
* ``claude_docker`` — runs ``sandbox_run`` as a detached container on a
  sandbox host: launch, poll by name, reap. A reclaimed row
  carrying ``meta.container`` is re-adopted by name-match, never
  relaunched.
* ``job_inproc`` — the generic bounded in-proc lane: one job per pass
  tick, synchronous ``dispatch(ctx, spec)``, no submit/poll, no kill
  hook. Exists for job_types that need a counted ``resource_slots``
  reservation and finish in minutes (``embed_batch`` is the first).

Claim substrate (``_common.py``)
--------------------------------
``claim_executor_jobs`` is shared by all lanes:

* **Ordering** — ``COALESCE(prio, 5) ASC, ref_id ASC``; an all-unset
  queue is byte-identical to FIFO. ``dispatch`` mints children with the
  parent todo's prio, so urgency flows down the DAG.
* **Reservation** — a job's ``meta.requires`` (``{resource: units}``,
  or derived from its job_type spec via ``effective_requires``) is
  reserved all-or-nothing in the claim txn against ``resource_slots``;
  an unadvertised resource soft-falls back to the node-gate pin — except
  an ``llm:``-prefixed requirement, which is a HARD claim veto (only a
  host advertising that served-model slot may claim; agents follow the
  models they need). Refunded at terminal and on crash recovery.
* **Reserve mode** — ``respect_reserve=True`` (only ``ssh_node`` /
  ``claude_docker`` pass it) checks the operator reserve row live inside
  the claim txn; the light cloud lane keeps running.
* **Crash recovery, boot-epoch** — every worker mints a
  ``worker_boot_id`` at startup, advertised via
  ``host_heartbeat.meta.boot_ids``; every claim stamps
  ``meta.lease_boot_id``/``lease_host``/``lease_process``. With
  ``reclaim_stale_running=True`` (``ssh_node``, ``claude_inproc``,
  ``claude_docker``) a ``STATUS:running`` row is reclaimable on lease
  **expiry** or on **epoch** mismatch (the holder was provably replaced,
  e.g. a deploy bounce) — a live holder is never stolen. ``poison_guard``
  bumps ``meta.attempts`` on *expiry* reclaims only (a redeploy can't
  burn the crash-loop budget) and fails the job
  (``failure_class='infra'``) past ``PRECIS_MAX_JOB_ATTEMPTS`` (3).
  These three lanes are excluded from the sweeper's wall-clock backstop
  (racing it would strand results as ``failed``); ``coordinator`` has no
  reclaim path and deliberately keeps the sweeper.
* **Kill paths** — ``precis jobs kill`` stamps ``meta.kill_requested``;
  the OWNING executor's poll honors it at the next tick, the same
  terminal path as the wall-clock ``meta.deadline`` kill. Either path
  best-effort GPU-resets the node when the job's requires include
  ``gpu``.

``wake_runner`` re-queues a ``waiting_children`` parent past its
``meta.wake_deadline`` "woken-degraded" (a ``child-failed:<id>`` tag per
non-terminal child, visibility only) — closes the "blocks forever" gap of
a permanently-unschedulable child that never reaches a terminal STATUS.

Every spawned job container splices ``container_limit_flags()``
(``utils/container_limits.py``) so heavy compute stays off reserved
system cores — a container does not inherit the worker's ``nice``.
"""

from __future__ import annotations

import os

#: Capability set each executor provides. The dispatcher checks
#: a job_type's REQUIRES against the chosen executor's PROVIDES;
#: any missing capability is reason to reject the submit.
EXECUTOR_PROVIDES: dict[str, frozenset[str]] = {
    "claude_inproc": frozenset(
        {
            "claude_bin",
            "git",
            "clones_dir",
            "claude_config_mount",
            # Planner-coroutine slice: claude_inproc forwards the
            # ``$PRECIS_MCP_CONFIG`` env into the claude subprocess
            # (--mcp-config) so the planner can call back via MCP.
            # See workers/job_types/plan_tick.py for the wiring.
            "mcp_config",
        }
    ),
    # ``coordinator`` is the yield/resume executor for long-running
    # coordinator job_types (precis-dft's ``dft_campaign`` is the
    # first consumer). It dispatches; it doesn't compute. The empty
    # PROVIDES set is intentional — a job_type compatible with
    # ``coordinator`` declares ``REQUIRES=frozenset()`` because the
    # actual work happens in the child jobs the coordinator spawns.
    "coordinator": frozenset(),
    # ``ssh_node`` runs a job's plugin ``dispatch`` on a remote node
    # (precis-dft's ``gpaw_relax`` shells out to ``ssh spark docker
    # run …``). Phase 1: static capability set = the spark node.
    # Host-aware PROVIDES is a later refinement (precis-dispatch doc).
    "ssh_node": frozenset({"has_gpaw"}),
    # ``claude_docker`` runs a ``sandbox_run`` job as a detached,
    # cgroup-capped container on an ``agent_sandbox_host`` — launch,
    # poll by name, reap. The
    # pass is registered only where ``PRECIS_SANDBOX_ENABLED=1`` (the
    # sandbox hosts), so this capability set is only *satisfiable*
    # there; a data host without podman + the OAuth token can't run it.
    "claude_docker": frozenset({"podman", "claude_oauth"}),
    # ``job_inproc`` (§F cycle a) runs a BOUNDED job_type's plugin
    # ``dispatch`` in-process, synchronously, one per pass tick — the
    # generic sibling of ``claude_inproc`` for non-claude work that also
    # wants slot reservation (``respect_reserve=True``). Empty PROVIDES:
    # the actual resource gate (``embed_batch`` needing an ``embedder``
    # slot) is the separate `resource_slots` reservation
    # (``ServiceSpec.requires`` / ``effective_requires``), not this
    # executor-capability check.
    "job_inproc": frozenset(),
}


#: Default executor when a `put(kind='job', ...)` call omits the
#: `executor=` field. `claude_inproc` stays the safest default;
#: the heavier lanes (`ssh_node`, `claude_docker`, …) are opt-in
#: explicitly per job.
DEFAULT_EXECUTOR = "claude_inproc"

#: Executors whose jobs cannot spend LLM budget: remote/in-process compute
#: lanes that never route through a paid LLM placement. The dispatcher's
#: global daily cost ceiling (an *LLM* budget — it sums ``llm_call_log``)
#: exempts candidates minting onto these lanes; blocking a pure-numpy
#: ``autocatpath_aggregate`` because Claude ticks burned the window starved
#: every pathway rollup for 29h (2026-08-16/17).
ZERO_LLM_EXECUTORS: frozenset[str] = frozenset({"ssh_node", "job_inproc"})


def is_known_executor(name: str) -> bool:
    return name in EXECUTOR_PROVIDES


def suspended_job_types() -> frozenset[str]:
    """Job types operator-suspended via ``PRECIS_SUSPENDED_JOB_TYPES``
    (comma-separated list; deploy var ``precis_suspended_job_types``).

    The operator hold switch for a compute lane: a suspended type's queued
    jobs are never claimed — fresh or reclaimed — by ``claim_executor_jobs``,
    so they wait in place until the env clears; in-flight rows are untouched
    (they poll/finish normally). Minting sites may also consult this to stop
    creating new work (``quest.compute.dispatch_autocatpath`` does). Read
    per-call, not cached, so a plist re-render + bounce takes effect on the
    next claim cycle.
    """
    raw = os.environ.get("PRECIS_SUSPENDED_JOB_TYPES", "")
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


__all__ = [
    "DEFAULT_EXECUTOR",
    "EXECUTOR_PROVIDES",
    "ZERO_LLM_EXECUTORS",
    "is_known_executor",
    "suspended_job_types",
]
