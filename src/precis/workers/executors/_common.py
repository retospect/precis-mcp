"""Shared substrate for the job executors and the wake_runner.

The two executors (:mod:`claude_inproc`, :mod:`coordinator`) and the
:mod:`precis.workers.wake_runner` pass all speak the same closed
``STATUS:*`` tag namespace, claim ``kind='job'`` rows with the same
SQL shape, and manipulate job rows with the same handful of helpers.
This module is the single home for that substrate so the three
modules stop re-declaring the constants and reaching into each
other's privates (the previous arrangement had ``coordinator`` and
``wake_runner`` importing helpers straight out of ``claude_inproc``,
and all three re-stating the STATUS values "to avoid a circular
import" that module-level constants can't actually cause).

The executors import these under their existing ``_name`` aliases so
the bare-name references in their bodies — and the tests that
``monkeypatch.setattr(module, "_set_status", ...)`` — keep working.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.handlers._todo_views import _doable_exclusion_clause
from precis.liveness import (
    NO_ADVERTISED_BOOT_ID,
    RECLAIM_WHY_EPOCH,
    RECLAIM_WHY_EXPIRY,
    reclaim_reason,
    worker_identity,
)
from precis.liveness import advertised_boot_ids as _advertised_boot_ids
from precis.store._resource_slots_ops import (
    release_resource_slots,
    reserve_resource_slots,
)
from precis.store.types import BlockInsert, Tag
from precis.workers.executors import suspended_job_types
from precis.workers.registry import SERVICES_BY_NAME
from precis.workers.service_config import reserve_active

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

# ── STATUS:* closed-namespace tag values ──────────────────────────
STATUS_NAMESPACE = "STATUS"
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCEL_REQUESTED = "cancel_requested"
CANCELLED = "cancelled"

# STATUS:waiting_* values written by a coordinator Yield; each maps to
# one ``WakeWhen.kind`` so the wake_runner's selectivity stays cheap
# (exact match on a closed-status value, not a LIKE).
WAITING_CHILDREN = "waiting_children"
WAITING_TIME = "waiting_time"
WAITING_ASK_USER = "waiting_ask_user"
WAITING_MANUAL_KICK = "waiting_manual_kick"

# Terminal STATUS values — a row carrying any of these is not
# claimable. Waiting statuses are NOT terminal; they're paused.
TERMINAL = (SUCCEEDED, FAILED, CANCELLED)

# Chunk kinds the executors write.
JOB_EVENT_KIND = "job_event"
JOB_SUMMARY_KIND = "job_summary"

#: Claim-ordering weight for a job whose ``refs.prio`` is unset (NULL).
#: The mid-point of the 1..10 ``refs.prio`` scale, matching
#: ``service_config``'s ``DEFAULT_PRIO`` — so an all-unset queue orders by
#: age alone (``ref_id`` ASC), byte-identical to the pre-6a FIFO claim.
_DEFAULT_JOB_PRIO = 5

#: How long an ``llm:``-affine job (see :func:`~precis.workers.dispatch.
#: _served_model_requirement`) waits for a host that actually advertises the
#: required model before the claim gate gives up on affinity and claims it
#: host-agnostic anyway (the model is still reachable via the LLM router HTTP
#: path from any host — co-location is a performance preference, not a
#: correctness constraint). Set below the nursery dispatch-stall threshold
#: (15min) so this fallback fires — and the job starts moving — before that
#: critical alert would ever fire on it.
LLM_AFFINITY_GRACE_MIN = 10

#: Candidate over-fetch factor for the scarcity re-rank (6d-deferred, §5.3).
#: The SQL fetches ``limit × this`` rows in prio/age order, then the scarcity
#: term re-ranks in Python and the top ``limit`` are reserved — so a rare-
#: capability job can surface ahead of the prio/age head without an unbounded
#: ``FOR UPDATE`` lock set. 1 (or no ``resource_slots``) ⇒ pre-6d behaviour.
_CLAIM_OVERFETCH = 3

# Boot-epoch machinery: extracted to precis.liveness (self-healing-spine
# Layer 1 slice 1) so slot holds and agentlogs share the same predicate.
# The names below keep their meaning; the "why" docstrings live there now.
_NO_ADVERTISED_BOOT_ID = NO_ADVERTISED_BOOT_ID

# ── Attempt cap (§H piece 3, generalized from ssh_node's original guard) ──
# RECLAIM_WHY_EPOCH does NOT burn the poison guard; RECLAIM_WHY_EXPIRY DOES
# count toward ``meta.attempts``. See :func:`claim_executor_jobs`'s docstring
# (both re-exported from precis.liveness).

#: Cap on ``meta.reclaims`` length — forensics, not an unbounded log.
_RECLAIMS_CAP = 10


def max_job_attempts() -> int:
    """Max run-attempts before a re-claimed job is treated as poison.

    Default 3, ``PRECIS_MAX_JOB_ATTEMPTS``-overridable, floor 1. Only
    lease-EXPIRY reclaims bump ``meta.attempts`` (see
    :func:`claim_executor_jobs`) — a job crash-looping its worker every
    generation eventually poison-fails; a job merely caught by redeploy
    churn does not burn this budget.
    """
    raw = os.environ.get("PRECIS_MAX_JOB_ATTEMPTS")
    if raw is None:
        return 3
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


#: Computed once at import (mirrors ``sweeper.STUCK_JOB_HOURS``) — every
#: executor that opts into ``reclaim_stale_running`` compares against this
#: same cap, so ``ssh_node._MAX_ATTEMPTS`` et al re-export it rather than
#: defining their own.
MAX_ATTEMPTS = max_job_attempts()


def _scarcity(requires: dict[str, int], host_count: dict[str, int]) -> float:
    """A job's capability-scarcity score — the first claim-order key (§5.3).

    Rarer capability → higher score → claimed first, because the scarce
    resource is the bottleneck the schedule should fill before commodity work.
    Score = the max over the job's required resources of ``1 / (hosts
    advertising it)``; a resource no host advertises contributes 0 (it can't be
    reserved here anyway — it self-gates to the ``target_node`` pin). A job that
    requires nothing scores 0, so a queue with no ``requires`` collapses to the
    prio/age order (pre-6d, byte-identical).
    """
    best = 0.0
    for res in requires:
        n = host_count.get(res, 0)
        if n > 0:
            best = max(best, 1.0 / n)
    return best


def reserve_host() -> str:
    """This host's identity for resource reservation (slice 6c).

    Delegates to :func:`precis.liveness.reserve_host` — the reservation
    host is the *heartbeat* identity, not the ``PRECIS_NODE`` claim-gate
    identity, so a decrement lands on the same row the probe advertised.
    """
    from precis.liveness import reserve_host as _impl

    return _impl()


def _this_worker_lease_identity() -> tuple[str | None, str | None, str | None]:
    """``(boot_id, process, host)`` this worker stamps onto every job it
    claims — :func:`precis.liveness.worker_identity` (the
    unadvertised-must-not-stamp guard and its rationale live there).
    """
    return worker_identity()


def effective_requires(meta: dict[str, Any]) -> dict[str, int]:
    """The resource requirements of a job (slice 6d).

    An explicit ``meta.requires`` (``{resource: units}``) wins; otherwise
    the requirement is *derived* from the job's ``job_type`` via the
    registry — a ``struct_relax``/``fold`` job matches a ``ServiceSpec``
    whose ``requires={"gpu"}``, so it needs ``{"gpu": 1}`` without any mint
    change. A job_type with no matching spec (or an empty ``requires``)
    needs nothing — the common path. The vocabulary is the counted
    ``resource_slots`` tokens (``gpu``/``podman``/``tts``), not the
    executor-capability tokens the dispatcher validates.
    """
    explicit = meta.get("requires")
    if explicit:
        return {str(k): int(v) for k, v in dict(explicit).items()}
    job_type = meta.get("job_type")
    spec = SERVICES_BY_NAME.get(str(job_type)) if job_type else None
    if spec is not None and spec.requires:
        return {tok: 1 for tok in spec.requires}
    return {}


def _advertised_by_host(conn: Connection) -> dict[str, set[str]]:
    """``host -> {resources it currently advertises}`` from ``resource_slots``.

    The self-gating map (slice 6d): a required resource that no host — or
    not *this* host — advertises is not reserved (it falls back to the
    ``target_node`` pin), so activating ``requires`` can't strand a job in
    the window before the heartbeat self-probe has populated the table.
    """
    rows = conn.execute("SELECT host, resource FROM resource_slots").fetchall()
    out: dict[str, set[str]] = {}
    for host, resource in rows:
        out.setdefault(str(host), set()).add(str(resource))
    return out


def _reclaim_reason(
    meta: dict[str, Any], advertised_boot_ids: dict[tuple[str, str], str]
) -> str:
    """Classify a running row's reclaim as ``epoch`` or ``expiry`` (§H piece
    3) — :func:`precis.liveness.reclaim_reason` with the job-lease
    ``meta.lease_*`` key mapping applied (the storage-key names stay HERE,
    per the liveness module's interface contract).
    """
    return reclaim_reason(
        meta.get("lease_boot_id"),
        meta.get("lease_host"),
        meta.get("lease_process"),
        advertised_boot_ids,
    )


def _mem_pressured_hosts(conn: Connection) -> set[str]:
    """Hosts under measured memory pressure (6d-deferred, soft veto).

    A host whose soft ``mem`` gauge (:meth:`Store.sync_soft_signal`) has hit
    ``free = 0`` is out of headroom; the claim skips reserving *heavy*
    (requires-bearing) jobs there so a jetsam-prone box (macOS) isn't handed
    another GPU/container/TTS job while it's thrashing. Dark until the
    heartbeat writes a ``mem`` row — no rows ⇒ empty set ⇒ no veto.
    """
    rows = conn.execute(
        "SELECT host FROM resource_slots WHERE resource = 'mem' "
        "AND kind = 'soft' AND free <= 0"
    ).fetchall()
    return {str(r[0]) for r in rows}


def renew_lease_if_mine(
    conn: Connection, ref_id: int, meta: dict[str, Any], lease_seconds: int
) -> bool:
    """Renew ``ref_id``'s ``lease_until`` IFF this claim's lease identity
    still matches the DB row right now — the "still mine" half of a
    long-running in-proc drain loop's mid-run lease keepalive (e.g.
    ``job_inproc``'s ``embed_batch``, whose ``params.limit`` batch can run
    past ``PRECIS_JOB_INPROC_LEASE_S`` on a slow embedder).

    Compares ``meta``'s ``lease_boot_id``/``lease_process``/``lease_host``
    — the SAME triple :func:`claim_executor_jobs`'s ``_stamp_lease_identity``
    stamped at claim time — against the row's CURRENT values, using
    null-safe ``IS NOT DISTINCT FROM`` on all three (a worker with no
    minted boot_id stamps all-``None`` identity — see
    :func:`_this_worker_lease_identity` — and plain SQL ``=`` never
    matches ``NULL = NULL``, which would make every renewal on such a
    worker spuriously "fail").

    Returns ``True`` (lease extended by ``lease_seconds``) when the
    identity still matches; ``False`` when the row's identity has since
    changed (or the row is gone) — i.e. another worker generation has
    already reclaimed it (epoch or expiry arm) while this one kept
    running. The caller MUST stop its work immediately without writing a
    terminal status on a lost lease: the new claimant now owns the job and
    will drive it to completion (or failure) itself; a stale write here
    would race it.
    """
    row = conn.execute(
        """
        UPDATE refs
           SET meta = meta || jsonb_build_object(
                 'lease_until', (now() + make_interval(secs => %s))::text
               )
         WHERE ref_id = %s
           AND meta->>'lease_boot_id' IS NOT DISTINCT FROM %s
           AND meta->>'lease_process' IS NOT DISTINCT FROM %s
           AND meta->>'lease_host' IS NOT DISTINCT FROM %s
         RETURNING ref_id
        """,
        (
            lease_seconds,
            ref_id,
            meta.get("lease_boot_id"),
            meta.get("lease_process"),
            meta.get("lease_host"),
        ),
    ).fetchone()
    return row is not None


def release_job_reservation(conn: Connection, ref_id: int) -> None:
    """Refund + clear a job's ``meta.reserved`` slots (idempotent).

    A no-op for a job that reserved nothing (no ``meta.reserved``). Clears
    the key after refunding so a second terminal transition — e.g. the
    sweeper racing an executor — can't double-refund (the capped release
    also guards the counter). Called at every terminal transition.
    """
    row = conn.execute(
        "SELECT meta->'reserved' FROM refs WHERE ref_id = %s", (ref_id,)
    ).fetchone()
    reserved = row[0] if row and row[0] else None
    if not reserved:
        return
    host = reserved.get("host")
    slots = reserved.get("slots") or {}
    if host and slots:
        release_resource_slots(conn, str(host), dict(slots))
    conn.execute(
        "UPDATE refs SET meta = meta - 'reserved' WHERE ref_id = %s", (ref_id,)
    )


# ── Claim ─────────────────────────────────────────────────────────


def claim_executor_jobs(
    conn: Connection,
    *,
    executor: str,
    limit: int,
    exclude_paused: bool = False,
    node: str | None = None,
    parent_not_paused: bool = False,
    reserve_host_id: str | None = None,
    reclaim_stale_running: bool = False,
    respect_reserve: bool = False,
) -> list[tuple[int, str, dict[str, Any]]]:
    """Lock up to ``limit`` claimable jobs for ``executor``.

    Claimable = ``kind='job'``, ``meta.executor`` matches,
    ``STATUS:queued``, not terminal, lease expired or absent — and the
    row's ``meta.job_type`` is not operator-suspended
    (:func:`~precis.workers.executors.suspended_job_types`,
    ``PRECIS_SUSPENDED_JOB_TYPES``): a suspended type's rows wait queued,
    untouched, until the hold clears; in-flight rows are unaffected.

    **Crash recovery (``reclaim_stale_running``).** Off by default (only
    ``coordinator`` leaves it off today), so a caller that doesn't opt in
    keeps yesterday's expiry-only steal behaviour unchanged. When on
    (``ssh_node``, ``claude_inproc``, ``claude_docker``), a
    ``STATUS:running`` row is claimable via EITHER of two independent
    arms:

    * **Expiry arm** (unchanged) — the lease has *provably* expired
      (``lease_until`` non-null and ``< now()``). The lease, sized to
      outlive the job plus margin, is the death-presumption signal when no
      faster proof exists.
    * **Epoch arm** (the lease-epoch reclaim arm) —
      the row's stamped ``meta.lease_boot_id`` is non-null and does NOT
      match the boot_id currently advertised (``host_heartbeat.meta.
      boot_ids``) for ``(meta.lease_host, meta.lease_process)`` — i.e. the
      process that claimed it has been replaced (deploy bounce, jetsam
      cull) — even while ``lease_until`` is still in the future. A row
      whose ``lease_boot_id`` IS the currently-advertised value (a live
      holder) is never stolen by this arm regardless of lease age. A row
      with a null ``lease_boot_id`` (pre-epoch / a caller that never
      minted one) falls back to the expiry arm alone — no regression.

    Either arm stealing re-runs the job; a stolen row's stale
    ``meta.reserved`` slots are refunded here before it re-reserves, so a
    crash can't leak resource slots.

    **Starvation-bound reclaim pre-pass** (prod incident: ``plan_tick`` job
    188721 sat ``STATUS:running`` 35h before reclaim; gr191124/gr192372/
    gr191125). The two arms above are correct but, before this fix,
    competed with fresh ``STATUS:queued`` work inside ONE prio/age-ordered
    selection window (see "Claim ordering" below) — a continuously
    re-minted stream of higher-priority (lower prio-number) cron work
    could out-rank a default-prio stale-running row on EVERY cycle, so it
    was never selected at all: starvation, not a lease defect. Every call
    with ``reclaim_stale_running=True`` now runs a small bounded
    reclaim-only pre-pass FIRST — up to ``K = min(2, limit)`` stale-running
    candidates matching either arm above, locked ``FOR UPDATE SKIP LOCKED``
    exactly like the fresh-work query, but ordered oldest-``ref_id``-first
    (prio-blind — a stale row's priority is irrelevant to how long it's
    been dead) — and reclaims them exactly as today (same reservation
    refund, lease-identity stamp, attempts/epoch bookkeeping — shared with
    the fresh-work pass via ``_finish_claim``/``_stamp_lease_identity``).
    The fresh-work query below then claims up to the FULL ``limit`` of
    ``STATUS:queued`` work only, unchanged ordering/semantics. The
    pre-pass reclaims are ADDITIVE to ``limit`` — no caller slices the
    return to ``limit``, each just iterates the full list — so fresh-work
    throughput is exactly unchanged and a stale row is reclaimed within
    one or two claim cycles no matter how much fresh work is queued.

    **Lease-identity stamp.** Every successful claim (fresh-queued OR
    reclaimed) through this function stamps ``meta.lease_boot_id`` /
    ``meta.lease_process`` / ``meta.lease_host`` with THIS worker's own
    identity (:func:`_this_worker_lease_identity`) — uniformly, for every
    executor, whether or not it opts into ``reclaim_stale_running`` — so a
    later successor (of any executor that DOES opt in) can tell this claim
    apart from its own. ``lease_boot_id`` is null when this process never
    minted a boot_id (see :func:`_this_worker_lease_identity`).

    The same stamp writes ``meta.started_at`` — the wall-clock instant of
    the job's FIRST claim, and the only durable record of when it began
    running (``STATUS:running`` is replaced, not expired, on the terminal
    write). Set-if-absent, so a reclaim leaves it alone: a re-stamp would
    make a job that has been nominally running for hours report as having
    just started, and its stalled time would read as queue wait. The
    reclaim instants live in ``meta.reclaims`` instead. Read by the
    ``/tasks`` dashboard to split a job's queue wait from its run time;
    absent on any job claimed before this shipped.

    **Attempt cap (§H piece 3, generalized from ssh_node's original
    guard).** Every RE-claim of a ``STATUS:running`` row (``reclaim_
    stale_running=True`` only) bumps ``meta.attempts`` — EXCEPT an
    ``epoch``-reason reclaim (the holder was provably replaced by a
    redeploy/bounce, not a crash-loop) does NOT bump it; only ``expiry``-
    reason reclaims (a hang, or no successor info) count. Either way the
    reclaim is appended to a capped ``meta.reclaims: [{at, why}]``
    forensic list. This function only does the bookkeeping — the actual
    cap enforcement (fail + bubble past :data:`MAX_ATTEMPTS`) is
    :func:`poison_guard`, which every ``reclaim_stale_running`` caller
    invokes on its own claimed rows before running them (mirrors
    ssh_node's original inline guard, now shared).

    **Claim ordering (slice 6a).** ``ORDER BY COALESCE(prio, 5) ASC,
    ref_id ASC`` — LOWER ``refs.prio`` first, the ``0014_refs_prio.sql``
    convention every writer follows (prio=1 chat/preempt · 2 cron · 5
    default; NULL reads as 5): the dispatcher propagates the parent
    todo's prio onto the job, so a high-urgency (low-number) quest/
    project has its compute claimed ahead of commodity work, oldest-first
    (``ref_id``) as the within-prio tiebreak / anti-starvation term. An
    all-unset queue collapses to ``ref_id`` ASC — the pre-6a FIFO. The
    capability-rarity term (§5.3) is layered on in 6d.

    **Reserve-at-claim (slices 6c/6d).** A job's resource requirements —
    explicit ``meta.requires`` or *derived* from its ``job_type`` via the
    registry (:func:`effective_requires`; ``struct_relax``/``fold`` →
    ``{"gpu": 1}``) — are reserved in this transaction before the job is
    handed back: the conditional decrement is the lock. The reservation
    host is the job's ``target_node`` if pinned (the resource lives where
    the job runs — an ssh_node GPU job reserves on the GPU box), else this
    host (``reserve_host_id`` / :func:`reserve_host`). Reservation
    *self-gates* on what that host advertises (slice 6d): an unadvertised
    requirement is left to the ``target_node`` node-gate rather than
    blocking, so activating ``requires`` can't strand a job before the
    heartbeat probe has populated ``resource_slots``. A job whose reserved
    slot is full is dropped from the batch (lock frees at commit → waits
    for capacity). What was actually reserved is stamped on
    ``meta.reserved`` for :func:`release_job_reservation` to refund at
    terminal. Jobs needing nothing are unaffected — the common path.

    When ``exclude_paused`` is True, also exclude rows carrying an
    open-namespace pause tag (``ask-user:*`` / ``halt:*`` /
    ``child-failed:*``) via the shared
    :func:`_doable_exclusion_clause` so the vocabulary stays in sync
    with the dispatcher's candidate query.

    **Reserve mode (§B-2, workers/service_config.py).** When
    ``respect_reserve`` is True (``ssh_node``/``claude_docker`` — the
    box-heavy compute lane; ``coordinator``/``claude_inproc`` do NOT pass
    this, the light cloud lane keeps running), an active reserve row for
    THIS claiming host (:func:`~precis.workers.service_config.
    reserve_active`, using the SAME host identity as ``lease_host`` —
    ``reserve_host_id`` or :func:`reserve_host`, never the ``node``/
    ``PRECIS_NODE`` claim-gate identity, so the reserved box and the gated
    box can't diverge) short-circuits this call to ``[]`` before any row is
    even looked at — no new claims, fresh or reclaimed, including a
    prio=1 job (reserve is absolute; the operator releases it explicitly).
    In-flight rows are completely untouched: this only gates NEW claims, so
    polls/leases/finalization of already-running work continue exactly as
    if nothing changed — that is the "in-flight finishes cleanly" contract.
    Checked fresh every pass (no cache), so a reserve set OR an expiry
    reached takes effect within one claim cycle.

    **Node gate (the structure atomistic IR #3).** A job may pin itself to a node via
    ``meta.params.target_node`` (``struct_relax`` sets it so the GPU
    relax is claimed by the node that ssh+stages it, keeping the NFS
    bind paths consistent). A worker passes its own ``node`` (from
    ``PRECIS_NODE``; ``None`` when unset): an un-pinned job is claimable
    by anyone, a pinned job only by the matching node. A node-less
    worker therefore claims only un-pinned jobs — the ``= NULL`` compare
    is never true, so it can't grab a job meant for a specific box.

    **Parent gate (§23 #3).** When ``parent_not_paused`` is True, skip a
    job whose *parent* todo carries an open-namespace pause tag — a
    halted / asking-user / child-failed project must not burn heavy
    compute until the owner unblocks it.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    if respect_reserve and reserve_active(conn, reserve_host_id or reserve_host()):
        # Absolute stop on new heavy claims for this host — in-flight rows
        # are never touched here, only this claim call returns empty.
        return []

    exclusion_sql = ""
    if exclude_paused:
        exclusion_sql = f"""
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = 'OPEN'
                    AND {_doable_exclusion_clause()}
               )"""

    parent_sql = ""
    if parent_not_paused:
        parent_sql = f"""
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                  WHERE rt.ref_id = r.parent_id
                    AND t.namespace = 'OPEN'
                    AND {_doable_exclusion_clause()}
               )"""

    # Operator hold switch (``PRECIS_SUSPENDED_JOB_TYPES``): a suspended
    # job_type is never claimed — fresh or reclaimed — so its queued rows
    # wait in place; in-flight rows keep polling/finishing normally. The
    # COALESCE keeps a job with no ``meta.job_type`` claimable (it can't
    # match any suspended name).
    suspended = suspended_job_types()
    suspend_sql = ""
    suspend_params: tuple[list[str], ...] = ()
    if suspended:
        suspend_sql = """
           AND COALESCE(r.meta->>'job_type', '') <> ALL(%s)"""
        suspend_params = (sorted(suspended),)

    # ── Shared per-row bookkeeping (reservation + lease-identity stamp) ──
    # Computed once; used by BOTH the reclaim-only pre-pass and the
    # fresh-work pass below, so a reclaimed row and a freshly-queued row go
    # through the exact same reservation/stamp path (§ starvation-bound
    # reclaim pre-pass, docstring).
    default_host = reserve_host_id or reserve_host()
    advertised = _advertised_by_host(conn)
    pressured = _mem_pressured_hosts(conn)
    lease_boot_id, lease_process, lease_host = _this_worker_lease_identity()
    # Only fetched when needed — a caller that never reclaims running rows
    # (``coordinator``) never re-claims, so there's nothing to classify.
    advertised_boot_ids = _advertised_boot_ids(conn) if reclaim_stale_running else {}

    def _stamp_lease_identity(
        ref_id: int, meta: dict[str, Any], status_value: str | None
    ) -> None:
        """Stamp THIS worker's lease identity onto every claim (§H), and —
        for a RE-claim of a running row — the generalized attempt cap (§H
        piece 3). See the docstring's "Lease-identity stamp" and "Attempt
        cap" sections.

        A fresh claim (``status_value != RUNNING``, i.e. the row was
        ``STATUS:queued``) only gets the lease-identity stamp — uniform
        across every executor, whether or not it opts into
        ``reclaim_stale_running``. A RE-claim additionally: classifies WHY
        (:func:`_reclaim_reason`) using the row's OLD ``lease_boot_id``
        (still in ``meta`` at this point — not yet overwritten below);
        bumps ``meta.attempts`` only for an ``expiry`` reclaim (an
        ``epoch`` reclaim — the holder was provably replaced by a
        redeploy/bounce — must NOT burn the poison guard); and appends a
        capped forensic entry to ``meta.reclaims``. The caller (each
        executor's own claim wrapper) is responsible for checking
        ``meta['attempts']`` against :data:`MAX_ATTEMPTS` and poison-
        failing — this function only does the bookkeeping, mirroring
        ssh_node's original split of "compute the count" vs "act on it".

        Note (Finding 6): the classification above reads ``advertised_
        boot_ids`` — a Python snapshot of ``host_heartbeat`` taken once,
        up front, by :func:`_advertised_boot_ids` — while the SQL epoch
        arm that decided this row was even claimable read the SAME table
        via its own correlated subquery, at a slightly earlier instant (a
        separate statement in the same transaction). A heartbeat landing
        in the gap between those two reads can only ever make this
        classification WRONG about *why* (e.g. call an actually-epoch
        reclaim an ``expiry`` one, bumping ``attempts`` one extra time it
        "shouldn't" have) — it can never cause a THEFT: the candidate set
        was already ``FOR UPDATE … SKIP LOCKED`` row-locked by the SQL
        query before either read, so no other worker can claim the same
        row concurrently regardless of which side of the heartbeat gap
        either read landed on.
        """
        fields: dict[str, Any] = {
            "lease_boot_id": lease_boot_id,
            "lease_process": lease_process,
            "lease_host": lease_host,
            # Wall-clock instant of the FIRST claim — the only durable
            # record of when a job started running. ``STATUS:running`` is
            # written with ``replace_prefix=True``, so the moment a job
            # reaches a terminal state its ``running`` ``ref_tags`` row is
            # *deleted*, not expired: after the fact, "queued at" and
            # "finished at" survive but "started at" is gone, and run time
            # can't be told apart from queue time. ``lease_until`` is
            # claim+lease and gets renewed, so it can't stand in.
            #
            # Set-if-absent, NOT re-stamped on a reclaim. A reclaim would
            # otherwise rewrite the start of a job that has been nominally
            # running for hours, and every reader downstream would fold
            # that dead time into the *queued* bucket
            # (``created_at → started_at``) — mislabelling 35h of stalled
            # run as queue wait, and making the stalest job in the tree
            # report as the freshest. The reclaim instant is not lost:
            # ``meta.reclaims`` below already records every one as
            # ``{at, why}``.
            "started_at": meta.get("started_at") or datetime.now(UTC).isoformat(),
        }
        if reclaim_stale_running and status_value == RUNNING:
            why = _reclaim_reason(meta, advertised_boot_ids)
            attempts = int(meta.get("attempts") or 0)
            if why == RECLAIM_WHY_EXPIRY:
                attempts += 1
            reclaims = list(meta.get("reclaims") or [])
            reclaims.append({"at": datetime.now(UTC).isoformat(), "why": why})
            reclaims = reclaims[-_RECLAIMS_CAP:]
            fields["attempts"] = attempts
            fields["reclaims"] = reclaims
            meta["attempts"] = attempts
            meta["reclaims"] = reclaims
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (Jsonb(fields), ref_id),
        )
        meta["lease_boot_id"] = lease_boot_id
        meta["lease_process"] = lease_process
        meta["lease_host"] = lease_host
        meta["started_at"] = fields["started_at"]

    def _finish_claim(
        ref_id: int,
        title: str,
        meta: dict[str, Any],
        status_value: str | None,
        created_at: datetime | None = None,
    ) -> tuple[int, str, dict[str, Any]] | None:
        """Reserve resources (if any) + stamp lease identity for one row
        already locked by the caller's ``FOR UPDATE`` select. Returns the
        claimable tuple, or ``None`` if a live resource slot is full / the
        reservation host is memory-pressured / the row is still within its
        ``llm:`` affinity grace window on a non-serving host (the row's lock
        frees at commit either way, so it's retried next cycle). ``created_at``
        (the row's ``refs.created_at``) is the "queued since" signal for that
        grace window; ``None`` (genuinely unavailable) reads as age zero — the
        conservative choice, keeping the row inside the grace window rather
        than fast-pathing an unknown-age job straight past affinity. Shared by
        both the reclaim-only pre-pass and the fresh-work pass below — a
        reclaimed row and a freshly-queued row go through identical
        bookkeeping.
        """
        if reclaim_stale_running and meta.get("reserved"):
            # A stolen (crash-recovered) job still carries the dead worker's
            # reservation — refund it before this claim re-reserves, so slots
            # don't leak on the reserving host. No-op for fresh queued work.
            release_job_reservation(conn, ref_id)
            meta.pop("reserved", None)
        requires = effective_requires(meta)
        if not requires:
            _stamp_lease_identity(ref_id, meta, status_value)
            return (ref_id, title, meta)
        # The resource lives where the job runs: its target_node (an
        # ssh_node GPU job reserves on the GPU box, not the claimer), else
        # this host. Self-gate to what that host actually advertises — an
        # unadvertised requirement falls back to the node-gate/pin (no
        # stall in the window before the probe populates the slot map).
        params = meta.get("params") or {}
        res_host = str(params.get("target_node") or default_host)
        # Soft memory-pressure veto (6d-deferred): a heavy job's reservation
        # host is out of RAM headroom → skip it this round (the lock frees at
        # commit; it retries once pressure clears). Dark until a ``mem`` row
        # with free=0 exists.
        if res_host in pressured:
            return None
        advertised_here = advertised.get(res_host, set())
        # An ``llm:``-prefixed requirement is a soft *affinity*, not a hard
        # host gate: the job prefers to run where its served model lives, but
        # the model is also reachable from any host over the LLM router HTTP
        # path, so co-location is a performance preference, not a
        # correctness constraint. If ``res_host`` doesn't advertise the
        # slot, skip the claim for up to ``LLM_AFFINITY_GRACE_MIN`` — giving
        # a serving host a window to pick it up first, auto-following
        # wherever the ``llm:`` slot lives (no hard-coded ``target_node``) —
        # then fall back to claiming it host-agnostic anyway, so a model
        # served only on a host that runs no executor for this job (a real
        # incident: ``llm:`` advertised only on a system-profile-only host)
        # can't strand the job forever. Non-``llm:`` requires (e.g. ``gpu``)
        # keep the soft self-gate below (unadvertised → dropped, job
        # proceeds) untouched.
        unserved_llm = sorted(
            res
            for res in requires
            if res.startswith("llm:") and res not in advertised_here
        )
        if unserved_llm:
            age_min = (
                (datetime.now(UTC) - created_at).total_seconds() / 60.0
                if created_at is not None
                else 0.0
            )
            if age_min <= LLM_AFFINITY_GRACE_MIN:
                log.debug(
                    "claim: skip job %s on %s — required served model(s) %s "
                    "not advertised there (queued %.1fmin, awaiting the "
                    "serving host, grace %dmin)",
                    ref_id,
                    res_host,
                    unserved_llm,
                    age_min,
                    LLM_AFFINITY_GRACE_MIN,
                )
                return None
            log.info(
                "claim: llm-affinity fallback — job %s queued %.0fmin, "
                "model(s) %s not served on %s; claiming host-agnostic "
                "(model reachable via router)",
                ref_id,
                age_min,
                unserved_llm,
                res_host,
            )
            # Fall through: proceed as if the unserved ``llm:`` requirement(s)
            # weren't present for gating/reservation purposes. No special-
            # casing needed below — ``reservable`` already filters to
            # ``res in advertised_here``, so an unserved ``llm:`` resource is
            # never reserved (there's no slot for it on this host anyway).
        reservable = {
            res: units for res, units in requires.items() if res in advertised_here
        }
        if reservable and not reserve_resource_slots(conn, res_host, reservable):
            # A live slot is full → drop it; the lock frees at commit and
            # the job waits for capacity on that host.
            return None
        if reservable:
            reserved = {"host": res_host, "slots": reservable}
            conn.execute(
                "UPDATE refs SET meta = meta || "
                "jsonb_build_object('reserved', %s::jsonb) WHERE ref_id = %s",
                (json.dumps(reserved), ref_id),
            )
            meta["reserved"] = reserved
        _stamp_lease_identity(ref_id, meta, status_value)
        return (ref_id, title, meta)

    claimed: list[tuple[int, str, dict[str, Any]]] = []

    # ── Reclaim-only pre-pass (starvation fix — see the docstring's
    # "Starvation-bound reclaim pre-pass") ──────────────────────────────
    # Runs BEFORE the fresh-work query below, prio-blind (a stale row's
    # priority doesn't matter to how long it's been dead), bounded to a
    # small K so it can never itself starve fresh-work throughput. Additive
    # to ``limit`` — the fresh-work pass below still gets the FULL ``limit``
    # regardless of how many rows this reclaims.
    if reclaim_stale_running:
        reclaim_limit = min(2, limit)
        reclaim_rows = conn.execute(
            f"""
            SELECT r.ref_id, r.title, r.meta, r.created_at
              FROM refs r
             WHERE r.kind = 'job'
               AND r.deleted_at IS NULL
               AND r.meta->>'executor' = %s
               AND (
                    (r.meta->'params'->>'target_node') IS NULL
                 OR (r.meta->'params'->>'target_node') = %s
               )
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = %s AND t.value = %s
                   )
               AND (
                    (
                         (r.meta->>'lease_until') IS NOT NULL
                     AND (r.meta->>'lease_until')::timestamptz < now()
                    )
                 OR (
                         (r.meta->>'lease_boot_id') IS NOT NULL
                     AND (r.meta->>'lease_boot_id') <> COALESCE(
                           (SELECT hh.meta #>> ARRAY['boot_ids', r.meta->>'lease_process']
                              FROM host_heartbeat hh
                             WHERE hh.host = r.meta->>'lease_host'),
                           %s
                         )
                    )
               )
               AND NOT EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = %s
                        AND t.value = ANY(%s)
                   ){exclusion_sql}{parent_sql}{suspend_sql}
             ORDER BY r.ref_id ASC
             LIMIT %s
               FOR UPDATE OF r SKIP LOCKED
            """,
            (
                executor,
                node,
                STATUS_NAMESPACE,
                RUNNING,
                _NO_ADVERTISED_BOOT_ID,
                STATUS_NAMESPACE,
                list(TERMINAL),
                *suspend_params,
                reclaim_limit,
            ),
        ).fetchall()
        for rr_ref_id, rr_title, rr_meta, rr_created_at in reclaim_rows:
            claim = _finish_claim(
                int(rr_ref_id),
                str(rr_title),
                dict(rr_meta or {}),
                RUNNING,
                rr_created_at,
            )
            if claim is not None:
                claimed.append(claim)

    # ── Fresh-work pass (unchanged ordering/semantics) ──────────────────
    # ``STATUS:queued`` only — the reclaim pre-pass above already handled
    # this cycle's bounded slice of stale-running candidates, so this query
    # no longer carries the running-row OR-arms (moved above).
    rows = conn.execute(
        f"""
        SELECT r.ref_id, r.title, r.meta, r.prio, r.created_at
          FROM refs r
         WHERE r.kind = 'job'
           AND r.deleted_at IS NULL
           AND r.meta->>'executor' = %s
           AND (
                (r.meta->'params'->>'target_node') IS NULL
             OR (r.meta->'params'->>'target_node') = %s
           )
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s AND t.value = %s
               )
           AND (
                (r.meta->>'lease_until') IS NULL
             OR (r.meta->>'lease_until')::timestamptz < now()
           )
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s
                    AND t.value = ANY(%s)
               ){exclusion_sql}{parent_sql}{suspend_sql}
         ORDER BY COALESCE(r.prio, %s) ASC, r.ref_id ASC
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (
            executor,
            node,
            STATUS_NAMESPACE,
            QUEUED,
            STATUS_NAMESPACE,
            list(TERMINAL),
            *suspend_params,
            _DEFAULT_JOB_PRIO,
            limit * _CLAIM_OVERFETCH,
        ),
    ).fetchall()

    # Scarcity re-rank (6d-deferred, §5.3): capability-rarity is the FIRST
    # claim-order key, then prio (0014 direction — LOWER is more urgent), then
    # age. ``host_count[res]`` = how many hosts advertise ``res`` (rarer →
    # higher scarcity). The SQL already returned rows in prio/age order and
    # stably; re-sorting by (-scarcity, prio, ref_id) keeps that order within a
    # scarcity tier — so a queue with no ``requires`` (scarcity 0 everywhere)
    # is byte-identical to the pre-6d prio/age claim.
    host_count: dict[str, int] = {}
    for _h, res_set in advertised.items():
        for res in res_set:
            host_count[res] = host_count.get(res, 0) + 1

    def _order_key(r: Any) -> tuple[float, int, int]:
        prio = int(r[3]) if r[3] is not None else _DEFAULT_JOB_PRIO
        scarcity = _scarcity(effective_requires(dict(r[2] or {})), host_count)
        return (-scarcity, prio, int(r[0]))

    ranked = sorted(rows, key=_order_key)

    fresh_claimed = 0
    for r in ranked:
        if fresh_claimed >= limit:
            break  # scarcity-ranked top ``limit`` reserved; leave the rest locked-free
        ref_id, title, meta = int(r[0]), str(r[1]), dict(r[2] or {})
        claim = _finish_claim(ref_id, title, meta, QUEUED, r[4])
        if claim is not None:
            claimed.append(claim)
            fresh_claimed += 1
    return claimed


# ── Status / chunk / meta helpers ─────────────────────────────────


def set_status(
    store: Store, ref_id: int, value: str, *, conn: Connection | None = None
) -> None:
    """Replace the current ``STATUS:`` tag with ``value`` on ``ref_id``.

    A terminal value also refunds any resource reservation the job holds
    (slice 6c) — release rides the same status write so a job's slots come
    back the instant it stops running, in the caller's transaction.
    """
    from precis.store import Tag

    tag = Tag.parse_strict(f"STATUS:{value}")
    store.add_tag(
        ref_id,
        tag,
        set_by="agent",
        replace_prefix=True,
        conn=conn,
    )
    if value in TERMINAL:
        if conn is not None:
            release_job_reservation(conn, ref_id)
        else:
            with store.pool.connection() as c:
                with c.transaction():
                    release_job_reservation(c, ref_id)


def is_cancel_requested(conn: Connection, ref_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
         WHERE rt.ref_id = %s
           AND t.namespace = %s
           AND t.value = %s
         LIMIT 1
        """,
        (ref_id, STATUS_NAMESPACE, CANCEL_REQUESTED),
    ).fetchone()
    return row is not None


def current_status(conn: Connection, ref_id: int) -> str | None:
    """Return the ref's current ``STATUS:`` value, or ``None`` if unset.

    There is one ``STATUS:`` tag per ref at a time (the handler writes
    with ``replace_prefix=True``), so this is an unambiguous read. Used
    to tell whether a job has already reached a terminal state before
    the executor applies its own transition.
    """
    row = conn.execute(
        """
        SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
         WHERE rt.ref_id = %s
           AND t.namespace = %s
         LIMIT 1
        """,
        (ref_id, STATUS_NAMESPACE),
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def append_chunk(
    store: Store,
    ref_id: int,
    chunk_kind: str,
    text: str,
    *,
    conn: Connection | None = None,
) -> None:
    """Append a chunk at the next ``ord`` for the ref.

    When ``conn`` is provided we count via that connection so back-to-
    back appends inside the same tx see each other's INSERTs. The
    previous implementation called ``store.list_blocks_for_ref`` which
    opens its own pool connection — uncommitted INSERTs in ``conn``
    were invisible, leading to two calls computing the same
    ``next_pos`` and a unique-constraint violation on ``(ref_id, ord)``.
    """
    if conn is not None:
        row = conn.execute(
            "SELECT COALESCE(MAX(ord) + 1, 0) FROM chunks "
            "WHERE ref_id = %s AND ord >= 0",
            (ref_id,),
        ).fetchone()
        next_pos = int(row[0]) if row and row[0] is not None else 0
    else:
        blocks = store.blocks.list_blocks_for_ref(ref_id)
        next_pos = len(blocks)
    store.blocks.insert_blocks(
        ref_id,
        [BlockInsert(pos=next_pos, text=text, meta={"chunk_kind": chunk_kind})],
        conn=conn,
    )


def _finite_json(value: Any) -> Any:
    """Map non-finite floats to ``None``, recursively.

    Python's json accepts ``Infinity``/``NaN`` (``json.load`` produces them,
    ``json.dumps`` re-emits them), but Postgres ``jsonb`` is strict RFC 8259
    and REJECTS those tokens — so a compute-derived payload carrying one
    (e.g. an empty-set ``min()`` seeded with ``inf``) fails the whole write
    with ``InvalidTextRepresentation``. That once discarded 48 finished GPU
    seed results and misclassified them as ``infra:child-killed``
    (docs/backlog/autocatpath-seed-child-killed-shredding.md). ``None`` is
    the honest jsonb spelling of "no finite value here".
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(v) for v in value]
    return value


def set_meta(conn: Connection, ref_id: int, **fields: Any) -> None:
    """Merge ``fields`` into ``refs.meta`` (non-finite floats → ``null``)."""
    conn.execute(
        "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
        (Jsonb(_finite_json(fields)), ref_id),
    )


def record_failure(
    store: Store,
    ref_id: int,
    reason: str,
    *,
    gripe_rollback: int | None,
    failure_class: str | None = None,
    open_tag: str | None = None,
) -> None:
    """Tag a job ``STATUS:failed`` with a reason event chunk.

    ``failure_class`` (optional) distinguishes *why* the job failed — e.g.
    ``"infra"`` (the runner/container/executor itself died: subprocess
    exception, non-zero container exit, malformed/missing output) vs
    ``"non-convergence"`` (the compute actually ran and reported a genuine
    physical/numeric failure) — stamped onto ``refs.meta.failure_class`` so a
    downstream harvest can tell "couldn't run" apart from "ran and failed"
    instead of laundering both into the same bare ``STATUS:failed``.

    ``open_tag`` (optional, parked-leaf-recovery,
    docs/backlog/parked-leaf-recovery.md) stamps an ``OPEN:<open_tag>`` tag
    on the job BEFORE it's marked failed — the generic layer a caller that
    observed a classifiable infra condition (e.g. a compute-executor
    subprocess dying by signal, or exiting without its expected result
    file — ``precis_pathway.seed_job``'s ``infra:child-killed``) reaches for
    instead of duplicating the tag-then-fail dance itself. Written inside
    the SAME transaction that drives ``STATUS:failed`` and calls
    :func:`~precis.handlers._job_bubble.bubble_job_failure` below, so a tag
    value that's also a member of
    ``precis.handlers._job_bubble.INFRA_FAILURE_TAGS`` is guaranteed visible
    to the bubble's infra-classification read — enabling the same bounded
    auto-retry a lease-expiry orphan gets, rather than an immediate latch.
    """
    with store.pool.connection() as conn:
        if open_tag is not None:
            store.add_tag(ref_id, Tag.open(open_tag), set_by="system", conn=conn)
        append_chunk(store, ref_id, JOB_EVENT_KIND, reason, conn=conn)
        set_status(store, ref_id, FAILED, conn=conn)
        if failure_class is not None:
            set_meta(conn, ref_id, failure_class=failure_class)
        if gripe_rollback is not None:
            set_status(store, gripe_rollback, "open", conn=conn)
        # Slice-5 failure-bubble.
        from precis.handlers._job_bubble import bubble_job_failure

        bubble_job_failure(store, ref_id, conn=conn)
        conn.commit()


def maybe_reset_gpu_after_kill(meta: dict[str, Any]) -> bool | None:
    """Best-effort GPU reclaim after a kill (wall-clock deadline OR the §B-2
    operator ``kill_requested`` backstop) of a job whose resolved
    requirements include ``gpu`` (:func:`effective_requires` — an explicit
    ``meta.requires`` or a ``struct_relax``/``fold`` job_type).

    Returns the ``reset_gpu()`` result when attempted (record it on
    ``meta.kill_gpu_reset``); ``None`` when the job never required a GPU —
    the common, no-op path, so a non-GPU kill (the vast majority: sandbox
    containers, plugin dispatchers with no ``requires``) is unaffected.

    Lazy-imports :mod:`precis.workers.job_types.struct_relax` (which itself
    already lazy-imports in a few places) so importing this module never
    pulls that one in — only a GPU-requiring kill ever reaches it.
    """
    if "gpu" not in effective_requires(meta):
        return None
    from precis.workers.job_types import struct_relax

    node = (meta.get("params") or {}).get("target_node")
    return struct_relax.reset_gpu(node=node)


def poison_guard(
    store: Store,
    conn: Connection,
    ref_id: int,
    meta: dict[str, Any],
    *,
    max_attempts: int | None = None,
) -> bool:
    """The crash-loop guard's ACTION (§H piece 3) — shared by every
    executor that opts into ``reclaim_stale_running``.

    ``claim_executor_jobs`` already computed ``meta['attempts']``
    (bumped only for ``expiry`` reclaims — an ``epoch`` reclaim never
    reaches this cap on its own). This just applies the cap's
    consequence, generalized from ssh_node's original inline guard:
    past the cap, the row is INFRA-failed + bubbled instead of run again
    — a job that crash-loops its worker every generation eventually
    stops stealing itself forever, but a job merely caught by redeploy
    churn (all-``epoch`` reclaims) never trips this.

    Returns True (and fails the job, in ``conn``'s transaction) when
    poisoned; False when the caller should proceed to run it normally.
    ``max_attempts`` overrides :data:`MAX_ATTEMPTS` for a caller with its
    own cap (defaults to the shared one — no known caller needs a
    different value today, but the hook costs nothing).

    **Reclaim-churn backstop (Finding 4).** ``epoch`` reclaims never bump
    ``meta.attempts`` by design (a redeploy isn't a crash-loop), which
    leaves a gap: a job whose HOST reboots (or otherwise bounces its
    worker generation) every single run is reclaimed via the epoch arm
    every time, ``attempts`` stays at 0 forever, and the attempt cap above
    never trips — the job "poison"-loops in a way the attempt cap was
    built to catch, just via a different door. ``meta.reclaims`` (capped
    at :data:`_RECLAIMS_CAP`, forensic list of every reclaim regardless of
    reason) already bounds this either way, so once its length hits the
    cap this guard trips too, REGARDLESS of ``attempts`` — a churn-only
    poison job still eventually fails + bubbles instead of reclaiming
    forever.
    """
    cap = MAX_ATTEMPTS if max_attempts is None else max_attempts
    attempts = int(meta.get("attempts") or 0)
    reclaims = meta.get("reclaims") or []
    reclaim_churn = len(reclaims) >= _RECLAIMS_CAP
    if attempts <= cap and not reclaim_churn:
        return False
    if reclaim_churn and attempts <= cap:
        reason = (
            f"runner: {len(reclaims)} reclaims recorded (reclaim-churn cap "
            f"{_RECLAIMS_CAP} reached) while only {attempts} run attempt(s) "
            "were counted — an epoch-reclaim-only crash-loop (e.g. a host "
            "that reboots every run) evades the attempt cap by never "
            "bumping it; failing as a poison-guard backstop"
        )
    else:
        reason = f"runner: exceeded {cap} run attempts (crash-loop guard) — failing"
    append_chunk(store, ref_id, JOB_EVENT_KIND, reason, conn=conn)
    set_meta(conn, ref_id, failure_class="infra")
    set_status(store, ref_id, FAILED, conn=conn)
    from precis.handlers._job_bubble import bubble_job_failure

    bubble_job_failure(store, ref_id, conn=conn)
    return True


__all__ = [
    "CANCELLED",
    "CANCEL_REQUESTED",
    "FAILED",
    "JOB_EVENT_KIND",
    "JOB_SUMMARY_KIND",
    "MAX_ATTEMPTS",
    "QUEUED",
    "RECLAIM_WHY_EPOCH",
    "RECLAIM_WHY_EXPIRY",
    "RUNNING",
    "STATUS_NAMESPACE",
    "SUCCEEDED",
    "TERMINAL",
    "WAITING_ASK_USER",
    "WAITING_CHILDREN",
    "WAITING_MANUAL_KICK",
    "WAITING_TIME",
    "append_chunk",
    "claim_executor_jobs",
    "current_status",
    "effective_requires",
    "is_cancel_requested",
    "max_job_attempts",
    "maybe_reset_gpu_after_kill",
    "poison_guard",
    "record_failure",
    "release_job_reservation",
    "renew_lease_if_mine",
    "reserve_host",
    "set_meta",
    "set_status",
]
