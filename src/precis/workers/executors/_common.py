"""Shared substrate for the job executors and the wake_runner.

``claude_inproc``, ``coordinator``, and :mod:`precis.workers.wake_runner`
share one closed ``STATUS:*`` tag namespace, the same ``kind='job'`` claim
SQL shape, and the same job-row helpers — this module is their single home.

Executors import these under their existing ``_name`` aliases: bare-name
references in their bodies, and tests' ``monkeypatch.setattr(module,
"_set_status", ...)``, keep working unchanged.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.types.json import Jsonb

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
from precis.store._todo_sql import _doable_exclusion_clause
from precis.store.types import ChunkInsert, Tag
from precis.utils.llm.failure import backoff_hours as _failure_backoff_hours
from precis.utils.secret_scan import mask_secrets, mask_secrets_deep
from precis.workers.executors import suspended_job_types
from precis.workers.nursery import HOST_DARK_SILENCE_MIN
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

#: Every waiting status — a parked job (coordinator Yield, or a
#: non-coordinator lane's :func:`park_job_until`). Non-terminal but NOT
#: still-running either: a finalize pass must leave these alone.
WAITING = (WAITING_CHILDREN, WAITING_TIME, WAITING_ASK_USER, WAITING_MANUAL_KICK)

#: A clean mid-run claim release (relax-slices): a checkpointed job past
#: its slice budget with work waiting behind it stops, yields its claim
#: and re-queues — NOT a coordinator ``waiting_*`` park (no wake
#: condition owns it; it is claimable immediately) and NOT terminal.
#: The fresh tag stamp is the re-queue: the row lands behind jobs
#: already waiting, at its unchanged prio/origin.
YIELDED = "yielded"

#: Statuses the fresh-work claim may take — ``queued`` plus ``yielded``.
#: Order inside the queue is unchanged: the status tag's ``created_at``
#: is the queue clock, and a yield re-stamps it.
CLAIMABLE = (QUEUED, YIELDED)

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

#: Claim ageing (claude-inproc-lane-starves-old-jobs): every this many
#: seconds a queued job has waited, its in-class claim rank improves by
#: one prio band. Strict prio order let a steady stream of fresh prio-2
#: cron work (``news_poll``) hold prio-8 jobs queued for ~3h on a live
#: lane; with in-class ageing a prio-``p`` job outranks every *fresh*
#: prio-``q`` arrival of its class after ``(p - q)`` steps, and anything
#: queued later than that ranks behind it, so its wait is bounded.
#: In-class ageing stops at the class's best prio: it lifts a job to
#: tie its class's most urgent members but never past them; the prio-1
#: chat/preempt band stays absolute across classes. Override with
#: :data:`CLAIM_AGE_STEP_ENV`; ``0`` restores strict prio order.
DEFAULT_CLAIM_AGE_STEP_S = 600.0
CLAIM_AGE_STEP_ENV = "PRECIS_CLAIM_AGE_STEP_S"

#: How long a queued row must wait before it promotes one class
#: (job-scheduling-fairness D3): a background row climbs a full class
#: per interval until it ties the top non-prio-1 class queued.
DEFAULT_CLAIM_CLASS_AGE_S = 1800.0
CLAIM_CLASS_AGE_ENV = "PRECIS_CLAIM_CLASS_AGE_S"

#: ``0`` restores the exact pre-slice-2 claim order (aged prio, queued
#: age, ref_id — the 1.5 floor); anything else keeps the class order.
CLAIM_CLASS_RANK_ENV = "PRECIS_CLAIM_CLASS_RANK"

#: ``meta.origin`` -> base class rank (D2: human 0, agent 1, recurring 2,
#: background 3). A missing origin (pre-slice-1 rows) reads as
#: ``recurring`` at prio <= 2 — the old cron band — else ``background``.
_CLASS_RANK_CASE = (
    "CASE r.meta->>'origin' "
    "WHEN 'human' THEN 0 WHEN 'agent' THEN 1 WHEN 'recurring' THEN 2 "
    "WHEN 'background' THEN 3 "
    "ELSE (CASE WHEN COALESCE(r.prio, %s)::float8 <= 2 THEN 2 ELSE 3 END) END"
)
_BASE_CLASS_RANK = {"human": 0, "agent": 1, "recurring": 2, "background": 3}

#: Code-shipped per-host running caps by ``job_type`` (D4 cap half) —
#: the derived_drain band can never occupy every ``job_inproc`` process.
#: An operator overrides per setting ``claim.max_running.<job_type>``
#: in ``app_state`` (an int; ``0`` suspends the type entirely).
_MAX_RUNNING_DEFAULTS: dict[str, int] = {"derived_drain": 2}
_MAX_RUNNING_KEY_PREFIX = "claim.max_running."

# ── Job slicing (docs/backlog/relax-slices.md) ────────────────────────
#: Feature flag — OFF by default until r91 shows a real checkpoint
#: resume on prod. Enabled per lane by env OR ``app_state``
#: ``claim.slicing.<executor>`` (a live flip, no redeploy). Off ⇒ no
#: code path can produce ``STATUS:yielded``.
SLICING_ENV = "PRECIS_JOB_SLICING"
_SLICING_KEY_PREFIX = "claim.slicing."

#: Per-claim wall budget (seconds) for one slice: wall time, not steps —
#: a resume pays model load + setup (MACE ~10-30 s), so slices are
#: minutes. ``PRECIS_JOB_SLICE_S`` overrides the default; ``app_state``
#: ``claim.slice_s.<executor>`` overrides both (the
#: ``claim.max_running.<job_type>`` code-default + DB-override pattern).
DEFAULT_SLICE_S = 1200.0
SLICE_S_ENV = "PRECIS_JOB_SLICE_S"
_SLICE_S_KEY_PREFIX = "claim.slice_s."


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def slice_budget_s(conn: Connection, executor: str) -> float | None:
    """The lane's per-claim wall budget in seconds — ``None`` while
    slicing is off (flag unset on env AND ``app_state``).

    Enable: ``PRECIS_JOB_SLICING`` truthy, or ``app_state``
    ``claim.slicing.<executor>`` truthy (per-lane live flip). Budget:
    ``app_state`` ``claim.slice_s.<executor>`` wins, else
    ``PRECIS_JOB_SLICE_S``, else :data:`DEFAULT_SLICE_S`.
    """
    rows: dict[str, Any] = dict(
        conn.execute(
            "SELECT key, value FROM app_state WHERE key = ANY(%s)",
            (
                [
                    f"{_SLICING_KEY_PREFIX}{executor}",
                    f"{_SLICE_S_KEY_PREFIX}{executor}",
                ],
            ),
        ).fetchall()
    )
    if not (
        _truthy(os.environ.get(SLICING_ENV))
        or _truthy(rows.get(f"{_SLICING_KEY_PREFIX}{executor}"))
    ):
        return None
    raw = rows.get(f"{_SLICE_S_KEY_PREFIX}{executor}", os.environ.get(SLICE_S_ENV))
    if raw is None:
        return DEFAULT_SLICE_S
    try:
        return max(0.0, float(str(raw)))
    except ValueError:
        log.warning(
            "claim: %s%s=%r is not a number; using %gs",
            _SLICE_S_KEY_PREFIX,
            executor,
            raw,
            DEFAULT_SLICE_S,
        )
        return DEFAULT_SLICE_S


def lane_has_waiters(
    conn: Connection,
    *,
    executor: str,
    node: str | None,
    exclude_ref_id: int,
) -> bool:
    """True when a waiter would claim this lane AHEAD of the running
    row once it re-queues — otherwise a high-class job would yield to a
    waiter it beats anyway and pay a model reload (10-30 s on MACE)
    every slice for nothing.

    Lane gates mirror :func:`claim_executor_jobs` for this host: same
    ``meta.executor``, the ``target_node`` pin check, a claimable
    status, a free lease, not operator-suspended. The ranking compares
    each waiter's full claim key — ``(claim_rank, aged_prio,
    queued_since, ref_id)`` from :func:`_claim_order_sql` — against the
    running job's key evaluated as-if-just-yielded: its base class (the
    yield re-stamps the tag, so no accumulated age or promotion;
    ``-1`` for prio 1), its raw aged prio, ``now()`` as its queue
    stamp. ``now()`` is after every real tag stamp, so a waiter that
    ties on rank+aged still wins the age tiebreak — exactly the
    "behind jobs already waiting" rule."""
    age_step = claim_age_step_s()
    class_age = claim_class_age_s()
    class_rank = claim_class_rank_enabled()
    rank_sql, aged_sql, order_params = _claim_order_sql(
        age_step, class_age, class_rank=class_rank
    )
    suspend_sql = ""
    suspend_params: tuple[list[str], ...] = ()
    suspended = suspended_job_types()
    if suspended:
        suspend_sql = " AND COALESCE(r.meta->>'job_type', '') <> ALL(%s)"
        suspend_params = (sorted(suspended),)
    if class_rank:
        my_key = (
            "(CASE WHEN me.prio = 1 THEN -1 ELSE me.base_class END,"
            " GREATEST(me.prio,"
            "  LEAST(me.prio,"
            "   CASE WHEN me.base_class < 2 THEN 5 ELSE 2 END)),"
            " now(), me.ref_id)"
        )
        waiter_key = "(o.crank, o.aged, q.qs, r.ref_id)"
    else:
        # Kill switch (PRECIS_CLAIM_CLASS_RANK=0): order is
        # (aged_prio, queued_since, ref_id) — no classes.
        my_aged = (
            "GREATEST(me.prio, LEAST(me.prio, 1.5))" if age_step > 0 else "me.prio"
        )
        my_key = f"({my_aged}, now(), me.ref_id)"
        waiter_key = "(o.aged, q.qs, r.ref_id)"
    row = conn.execute(
        f"""
        WITH me AS (
          SELECT r.ref_id AS ref_id,
                 {_CLASS_RANK_CASE} AS base_class,
                 COALESCE(r.prio, %s)::float8 AS prio
            FROM refs r WHERE r.ref_id = %s
        )
        SELECT 1
          FROM refs r
          CROSS JOIN me
          CROSS JOIN LATERAL (
                SELECT max(rt.created_at) AS qs
                  FROM ref_tags rt JOIN tags t USING (tag_id)
                 WHERE rt.ref_id = r.ref_id
                   AND t.namespace = %s AND t.value = ANY(%s)
              ) q
          CROSS JOIN LATERAL (
                SELECT COALESCE(EXTRACT(EPOCH FROM (now() - q.qs))::float8, 0)
                       AS age_s,
                       {_CLASS_RANK_CASE} AS base_class
              ) b
          CROSS JOIN LATERAL (
                SELECT min({_CLASS_RANK_CASE.replace("r.", "r3.")}) AS top_class
                  FROM refs r3
                 WHERE r3.kind = 'job' AND r3.retired_at IS NULL
                   AND r3.meta->>'executor' = %s
                   AND EXISTS (
                         SELECT 1 FROM ref_tags rt3 JOIN tags t3 USING (tag_id)
                          WHERE rt3.ref_id = r3.ref_id
                            AND t3.namespace = %s AND t3.value = ANY(%s)
                       )
                   AND COALESCE(r3.prio, %s)::float8 <> 1
              ) t
          CROSS JOIN LATERAL (
                SELECT {rank_sql} AS crank, {aged_sql} AS aged
              ) o
         WHERE r.kind = 'job' AND r.retired_at IS NULL
           AND r.ref_id <> %s
           AND r.meta->>'executor' = %s
           AND (
                (r.meta->'params'->>'target_node') IS NULL
             OR (r.meta->'params'->>'target_node') = %s
           )
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s AND t.value = ANY(%s)
               )
           AND (
                (r.meta->>'lease_until') IS NULL
             OR (r.meta->>'lease_until')::timestamptz < now()
           ){suspend_sql}
           AND {waiter_key} < {my_key}
         LIMIT 1
        """,
        (
            _DEFAULT_JOB_PRIO,
            _DEFAULT_JOB_PRIO,
            exclude_ref_id,
            STATUS_NAMESPACE,
            list(CLAIMABLE),
            _DEFAULT_JOB_PRIO,
            _DEFAULT_JOB_PRIO,
            executor,
            STATUS_NAMESPACE,
            list(CLAIMABLE),
            _DEFAULT_JOB_PRIO,
            *order_params,
            exclude_ref_id,
            executor,
            node,
            STATUS_NAMESPACE,
            list(CLAIMABLE),
            *suspend_params,
        ),
    ).fetchone()
    return row is not None


def yield_claim(
    store: Store,
    ref_id: int,
    *,
    meta_updates: dict[str, Any] | None = None,
) -> str | None:
    """Release a running claim into ``STATUS:yielded`` — one transaction.

    Returns ``None`` when the yield applied, or the live STATUS value
    when the row left ``running`` between the executor's stop decision
    and this transaction (e.g. an operator ``cancel_requested`` landed
    in between — the yield must not silently overwrite it; the caller
    drives the raced transition instead).

    On a yield it refunds ``meta.reserved`` (the slot frees for the
    next claim, as at a terminal transition), clears
    ``meta.lease_until`` (a live lease would keep the row unclaimable
    AND read as an orphaned claim to the sweeper/reconciler paths —
    the ``wake_runner._clear_lease`` precedent), merges
    ``meta_updates`` (the caller's slice record), then flips the
    status. The fresh ``STATUS:yielded`` tag stamp IS the re-queue:
    the row lands behind jobs already waiting, at unchanged
    prio/origin — no attempts bump, no failure write."""
    with store.tx() as conn:
        conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (ref_id,))
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = %s",
            (ref_id, STATUS_NAMESPACE),
        ).fetchone()
        live = row[0] if row else None
        if live != RUNNING:
            return live
        release_job_reservation(conn, ref_id)
        conn.execute(
            "UPDATE refs SET meta = (meta - 'lease_until') || %s::jsonb "
            "WHERE ref_id = %s",
            (Jsonb(mask_secrets_deep(_finite_json(meta_updates or {}))), ref_id),
        )
        set_status(store, ref_id, YIELDED, conn=conn)
        return None


def job_type_running_caps(conn: Connection) -> dict[str, int]:
    """``job_type -> max running rows per host``: the code-shipped
    :data:`_MAX_RUNNING_DEFAULTS` overlaid with any ``app_state``
    ``claim.max_running.<job_type>`` operator overrides (D4 cap half).
    Shared by :func:`claim_executor_jobs`'s cap exclusion and the
    materializer's "is this band's lane actually saturated" stuck check."""
    caps = dict(_MAX_RUNNING_DEFAULTS)
    for key, value in conn.execute(
        "SELECT key, value FROM app_state WHERE key LIKE %s",
        (f"{_MAX_RUNNING_KEY_PREFIX}%",),
    ).fetchall():
        try:
            caps[str(key)[len(_MAX_RUNNING_KEY_PREFIX) :]] = int(str(value))
        except ValueError:
            log.warning("claim: %s=%r is not an int; ignored", key, value)
    return caps


def _share_key_of(job_type: str, params_pass: Any) -> str:
    """The fair-share discriminator (D4 share half): a ``job_type``
    whose rows carry a ``params.pass`` sub-queue (the materialize SMALL
    bands — two sources sharing one ``derived_drain`` job_type,
    discriminated by ``meta.params.pass``) shares capacity per
    ``<job_type>:<pass>``, not per job_type — so one band can't hold a
    capped lane while its sibling starves (gr506754). Types without the
    discriminator share by job_type alone (the spec's original key)."""
    return f"{job_type}:{params_pass}" if params_pass is not None else job_type


def _share_key(meta: dict[str, Any]) -> str:
    params = meta.get("params")
    params_pass = params.get("pass") if isinstance(params, dict) else None
    return _share_key_of(str(meta.get("job_type") or ""), params_pass)


def claim_age_step_s() -> float:
    """The claim-ageing step in seconds (``0`` = no ageing)."""
    raw = os.environ.get(CLAIM_AGE_STEP_ENV)
    if not raw:
        return DEFAULT_CLAIM_AGE_STEP_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning(
            "claim: %s=%r is not a number; using %gs",
            CLAIM_AGE_STEP_ENV,
            raw,
            DEFAULT_CLAIM_AGE_STEP_S,
        )
        return DEFAULT_CLAIM_AGE_STEP_S


def claim_class_age_s() -> float:
    """The class-promotion bound in seconds (D3)."""
    raw = os.environ.get(CLAIM_CLASS_AGE_ENV)
    if not raw:
        return DEFAULT_CLAIM_CLASS_AGE_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning(
            "claim: %s=%r is not a number; using %gs",
            CLAIM_CLASS_AGE_ENV,
            raw,
            DEFAULT_CLAIM_CLASS_AGE_S,
        )
        return DEFAULT_CLAIM_CLASS_AGE_S


def claim_class_rank_enabled() -> bool:
    """``PRECIS_CLAIM_CLASS_RANK != '0'`` — the slice-2 order; ``0`` is
    the kill switch restoring the pre-slice-2 aged-prio order."""
    return os.environ.get(CLAIM_CLASS_RANK_ENV) != "0"


#: How long an ``llm:``-affine job (see :func:`~precis.workers.dispatch.
#: _served_model_requirement`) waits for a host that actually advertises the
#: required model before the claim gate gives up on affinity and claims it
#: host-agnostic anyway (the model is still reachable via the LLM router HTTP
#: path from any host — co-location is a performance preference, not a
#: correctness constraint). Set below the nursery dispatch-stall threshold
#: (15min) so this fallback fires — and the job starts moving — before that
#: critical alert would ever fire on it.
LLM_AFFINITY_GRACE_MIN = 10

#: Staleness ceiling (minutes) for the dark-window claim defer: a
#: requires-bearing job whose non-``llm:`` resources aren't advertised on
#: the reservation host defers instead of claiming unreserved while that
#: host's heartbeat is stale-but-recent (gr491098: a post-restart claim
#: burst on castor dropped every ``gpu:1`` gate — heartbeat dark → the slot
#: map read empty — and co-claimed five cpuset-pinned NEB seeds onto one
#: GPU box, all deadline-killed). Past this the host reads as fossil and
#: the unadvertised-requires fallback fires as before, so a permanently
#: dark advertisement can't strand the queue.
_DARK_CLAIM_DEFER_MAX_MIN = 240

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
# Both epoch and expiry reclaims count toward ``meta.attempts``; the reason
# remains in ``meta.reclaims`` (both re-exported from precis.liveness).

#: Cap on ``meta.reclaims`` length — forensics, not an unbounded log.
_RECLAIMS_CAP = 10


def max_job_attempts() -> int:
    """Max run-attempts before a re-claimed job is treated as poison.

    Default 3, ``PRECIS_MAX_JOB_ATTEMPTS``-overridable, floor 1.
    Both lease-expiry and boot-epoch reclaims bump ``meta.attempts``:
    worker restarts can be caused by the job itself, not only redeploys.
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


def job_type_requires(meta: dict[str, Any]) -> frozenset[str]:
    """The job_type's own declared ``JobTypeSpec.requires`` (the submit-time
    executor-capability vocabulary — ``workers/job_types/__init__.py`` —
    checked at ``put()`` against the claiming executor's ``EXECUTOR_
    PROVIDES``), deliberately DISTINCT from :func:`effective_requires`'s
    ``ServiceSpec.requires`` (the ``resource_slots`` RESERVATION vocabulary,
    registry.py).

    A counted reservation is only refunded at a job's TERMINAL transition
    (:func:`release_job_reservation`) — safe for a bounded job (``struct_
    relax`` runs once, to completion, per claim) but WRONG for a Yield-heavy
    ``coordinator`` job_type (``quest_tick``): each slice re-claims a *fresh*
    ``STATUS:queued`` row after a park (``coordinator`` never sets
    ``reclaim_stale_running``), so reserving on every claim with no
    intervening refund would exhaust a capacity-1 slot after the job's very
    first ``Yield`` — permanently stranding it (gr335087 postmortem). This
    reads the SAME capability vocabulary (``claude_bin`` et al.) but only
    ever feeds a CHECK (:func:`_coordinator_capability_ok`), never a
    reservation.

    Lazy import: some ``job_type`` modules (``diagnose_gripe``) import THIS
    module, so importing ``precis.workers.job_types`` at module level here
    would cycle.
    """
    job_type = meta.get("job_type")
    if not job_type:
        return frozenset()
    from precis.workers.job_types import get_job_type

    spec = get_job_type(str(job_type))
    return spec.requires if spec is not None else frozenset()


def _coordinator_capability_ok(
    meta: dict[str, Any], advertised: dict[str, set[str]], default_host: str
) -> bool:
    """Check-only (never reserves) per-host capability gate for a
    ``coordinator`` job_type's declared :func:`job_type_requires`.

    ``True`` when the job_type declares nothing, or every token it declares
    is advertised on the host that would actually run it (its ``target_node``
    pin, else this claiming host — mirroring the ``res_host`` derivation
    :func:`claim_executor_jobs`'s reservation path uses) — ``False`` drops
    the row from THIS claim (the lock frees at commit; it stays queued for a
    capable host/worker to pick up next cycle). Opt-in via
    ``claim_executor_jobs(check_job_type_requires=True)`` — only
    ``coordinator`` passes that today (gr335087: a claude-less host's
    coordinator worker was claiming ``quest_tick`` and failing every tick at
    the LLM stage). If literally no host in the fleet ever advertises a
    declared token, the sweeper's ``_alert_unschedulable_jobs`` pages instead
    of the job silently starving forever.
    """
    requires = job_type_requires(meta)
    if not requires:
        return True
    params = meta.get("params") or {}
    host = str(params.get("target_node") or default_host)
    return requires <= advertised.get(host, set())


def _advertised_by_host(conn: Connection) -> dict[str, set[str]]:
    """``host -> {resources it currently advertises}`` from ``resource_slots``.

    The self-gating map (slice 6d): a required resource that no host — or
    not *this* host — advertises is not reserved (it falls back to the
    ``target_node`` pin), so activating ``requires`` can't strand a job in
    the window before the heartbeat self-probe has populated the table.

    gr333274 (follow-up to gr333205's sweeper fix, ``_alert_unschedulable_
    jobs``): joined to ``host_heartbeat`` and filtered to
    :data:`~precis.workers.nursery.HOST_DARK_SILENCE_MIN` freshness — the
    SAME staleness cutoff, same shape. A ``resource_slots`` row is only
    retracted by the retract-on-absent discipline inside a *live* heartbeat
    pass; a host that's retired outright (decommissioned, or just stopped
    heartbeating) leaves its last advertisement frozen forever. Without
    this filter, a job pinned via ``target_node`` to that fossil host would
    read the dead capability as live and pass the reservation gate, even
    though nothing will ever run it there.

    Filtering is UNIFORM (every host, not just ``target_node`` pins): the
    real heartbeat probe (``heartbeat.py``'s ``_collect_and_upsert``) always
    writes ``host_heartbeat`` before it calls ``_report_resource_slots`` to
    sync ``resource_slots`` — in the SAME pass — so any host with a
    ``resource_slots`` row necessarily has a heartbeat row at least that
    fresh. The claiming host's own advertisement (the ``default_host``
    fallback when there's no pin) is never legitimately "advertised but
    heartbeat-less" in production; there is no first-boot gap to special-
    case here (unlike the *derivation* self-gate above, which handles the
    window before the probe has populated ``resource_slots`` at all — a
    missing row, not a stale one).
    """
    rows = conn.execute(
        "SELECT rs.host, rs.resource FROM resource_slots rs "
        "JOIN host_heartbeat hh ON hh.host = rs.host "
        "WHERE hh.ts > now() - %(silence)s::interval",
        {"silence": f"{HOST_DARK_SILENCE_MIN} minutes"},
    ).fetchall()
    out: dict[str, set[str]] = {}
    for host, resource in rows:
        out.setdefault(str(host), set()).add(str(resource))
    return out


def _host_recently_dark(conn: Connection, host: str) -> bool:
    """True when ``host`` has a ``host_heartbeat`` row that is stale but not
    ancient — i.e. it beat recently enough that its slot map should come
    back with the next healthy pass.

    Both other shapes deliberately read False: no row at all (the host has
    never heartbeated — its first probe hasn't populated the table, the
    exact window the unadvertised-requires fallback exists for) and a row
    stale past :data:`_DARK_CLAIM_DEFER_MAX_MIN` (a fossil/permanently-down
    advertisement — the fallback must still fire or a pinned queue strands
    forever)."""
    row = conn.execute(
        "SELECT ts FROM host_heartbeat WHERE host = %s", (host,)
    ).fetchone()
    if row is None or row[0] is None:
        return False
    age_min = (datetime.now(UTC) - row[0]).total_seconds() / 60.0
    return HOST_DARK_SILENCE_MIN < age_min <= _DARK_CLAIM_DEFER_MAX_MIN


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


def _service_pinned_hosts(conn: Connection) -> dict[str, frozenset[str]]:
    """service → the set of hosts a host-scoped ``prio > 0``
    ``service_config`` row enables it on (``'*'`` wildcard excluded —
    a wildcard row lights the service everywhere, it doesn't pin it).

    The *placement* read of ``service_config`` (gr506045): a job_type
    declaring ``JobTypeSpec.service_pin`` may only be claimed on a host
    in this set, so its jobs wait queued while the pinned host is down
    instead of drifting to any live lane or failing. The host identity
    is the heartbeat key (``reserve_host()``), matching what the
    ``precis service`` CLI writes — NOT ``PRECIS_NODE``, which names a
    capability lane, not a machine.
    """
    rows = conn.execute(
        "SELECT service, host FROM service_config WHERE prio > 0 AND host <> '*'"
    ).fetchall()
    out: dict[str, set[str]] = {}
    for service, host in rows:
        out.setdefault(str(service), set()).add(str(host))
    return {k: frozenset(v) for k, v in out.items()}


def _service_pin_hosts_for(
    meta: dict[str, Any], pinned: dict[str, frozenset[str]]
) -> frozenset[str] | None:
    """The enabled-host set this job row's ``service_pin`` resolves to.

    ``None`` when the job_type declares no ``service_pin``, is unknown,
    or the named service has no host-scoped enabled row right now — an
    unpinned job stays host-agnostic (a dark service gate-skips at
    dispatch anyway)."""
    job_type = meta.get("job_type")
    if not job_type:
        return None
    # Lazy: workers.job_types must not be a module-level import here —
    # job_type modules themselves import from executors land (cycle).
    from precis.workers.job_types import get_job_type

    spec = get_job_type(str(job_type))
    if spec is None or not spec.service_pin:
        return None
    hosts = pinned.get(spec.service_pin)
    return hosts if hosts else None


def _claim_order_sql(
    age_step_s: float,
    class_age_s: float,
    *,
    class_rank: bool = True,
) -> tuple[str, str, tuple[Any, ...]]:
    """The D2/D3 claim order: ``(claim_rank_sql, aged_prio_sql, params)``.

    The expressions read ``b.age_s`` / ``b.base_class`` from the LATERALs
    in :func:`claim_executor_jobs` (queued-tag age and base origin class,
    computed once per row — :data:`_CLASS_RANK_CASE`; missing origin
    reads as the old cron band, 2, at prio <= 2, else background 3).
    ``claim_rank`` is ``-1`` for a prio-1 preempt (absolute across
    classes — ruled 2026-10-23); otherwise ``base - floor(age /
    class_age_s)``, floored at 0 — a row promotes one full class per
    interval until it ties the top class, so a class flood can hold a
    lower class down only for a bounded wait (D3; the Python re-rank
    clamps at the top class actually present in the candidate set).
    ``aged_prio`` is the in-class order: prio less one band per
    ``age_step_s`` of age, floored at the effective class's best prio
    (5 for human/agent — the puts' default — 2 for recurring/
    background) so a prio-8 row can tie but never beat its class's
    prio-2 members. ``class_rank=False`` (the
    ``PRECIS_CLAIM_CLASS_RANK=0`` kill switch) restores the pre-slice-2
    order verbatim: aged prio with the 1.5 floor in both columns, so the
    ORDER BY is the old ``aged, queued_since, ref_id`` — and drops the
    share term (:func:`claim_executor_jobs` adds it only under the
    class order).
    """
    if not class_rank:
        if age_step_s <= 0:
            sql = "COALESCE(r.prio, %s)::float8"
            return sql, sql, (_DEFAULT_JOB_PRIO, _DEFAULT_JOB_PRIO)
        sql = (
            "GREATEST(COALESCE(r.prio, %s)::float8 - b.age_s / %s,"
            " LEAST(COALESCE(r.prio, %s)::float8, %s))"
        )
        params = (_DEFAULT_JOB_PRIO, age_step_s, _DEFAULT_JOB_PRIO, 1.5)
        # Both columns carry the same expression — params bind twice.
        return sql, sql, (*params, *params)
    #: The floor mirrors the Python re-rank: a row promotes to
    #: ``min(base, max(top_class, 2))`` — it may tie the top class queued
    #: (``t.top_class``, the min base class among queued non-prio-1
    #: candidates) but never reaches agent/human rank, so a promoted
    #: background backlog can't fill the claim window ahead of a fresh
    #: foreground row.
    _eff_floor = "LEAST(b.base_class, GREATEST(COALESCE(t.top_class, b.base_class), 2))"
    claim_rank = (
        "CASE WHEN COALESCE(r.prio, %s)::float8 = 1 THEN -1"
        f" ELSE GREATEST(b.base_class - FLOOR(b.age_s / %s), {_eff_floor}) END"
    )
    rank_params = (_DEFAULT_JOB_PRIO, class_age_s)
    aged_params: tuple[Any, ...]
    if age_step_s > 0:
        aged = (
            "GREATEST(COALESCE(r.prio, %s)::float8 - b.age_s / %s,"
            " LEAST(COALESCE(r.prio, %s)::float8,"
            " CASE WHEN GREATEST(b.base_class - FLOOR(b.age_s / %s),"
            f" {_eff_floor}) < 2 THEN 5 ELSE 2 END))"
        )
        aged_params = (
            _DEFAULT_JOB_PRIO,
            age_step_s,
            _DEFAULT_JOB_PRIO,
            class_age_s,
        )
    else:
        aged = "COALESCE(r.prio, %s)::float8"
        aged_params = (_DEFAULT_JOB_PRIO,)
    return claim_rank, aged, (*rank_params, *aged_params)


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
    check_job_type_requires: bool = False,
) -> list[tuple[int, str, dict[str, Any]]]:
    """Lock up to ``limit`` claimable jobs for ``executor``.

    Claimable = ``kind='job'``, ``meta.executor`` matches,
    ``STATUS:queued`` or ``STATUS:yielded`` (a released slice re-queues
    here behind the rows already waiting), not terminal, lease expired
    or absent — and the
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
    ``/todo`` dashboard to split a job's queue wait from its run time;
    absent on any job claimed before this shipped.

    **Attempt cap (§H piece 3, generalized from ssh_node's original
    guard).** Every RE-claim of a ``STATUS:running`` row (``reclaim_
    stale_running=True`` only) bumps ``meta.attempts``. An epoch mismatch
    proves only that the holder was replaced, not *why* it died: a worker
    repeatedly killed by its own job must also exhaust the budget. The
    reason remains in a capped ``meta.reclaims: [{at, why}]``
    forensic list. This function only does the bookkeeping — the actual
    cap enforcement (fail + bubble at :data:`MAX_ATTEMPTS`) is
    :func:`poison_guard`, which every ``reclaim_stale_running`` caller
    invokes on its own claimed rows before running them (mirrors
    ssh_node's original inline guard, now shared).

    **Claim ordering (slice 6a, FIFO fix 2026-09-25, ageing 2026-10-15;
    class rank 2026-10-23, job-scheduling-fairness D2/D3; share term
    2026-10-24, D4 share half).** ``ORDER BY claim_rank ASC,
    share_running ASC, aged_prio ASC, queued_since ASC, ref_id ASC``
    where ``claim_rank`` is the row's origin class (human 0 < agent 1 <
    recurring 2 < background 3; a missing origin reads as the old cron
    band at prio <= 2, else background) stepped down one class per
    :func:`claim_class_age_s` of wait until it ties the top class
    queued, ``-1`` for a prio-1 preempt (absolute across classes), and
    ``aged_prio`` is ``COALESCE(prio, 5)`` less one band per
    :func:`claim_age_step_s` of ``queued_since`` age, floored at the
    effective class's best prio (5 above recurring, 2 at or below it —
    see :data:`DEFAULT_CLAIM_AGE_STEP_S`: a steady fresh prio-2 stream
    otherwise starves prio-5/8 rows for hours). Jobs queued moments
    apart still order by
    ``prio`` exactly as before — LOWER
    ``refs.prio`` first, the ``0014_refs_prio.sql``
    convention every writer follows (prio=1 chat/preempt · 2 cron · 5
    default; NULL reads as 5): the dispatcher propagates the parent
    todo's prio onto the job, so a high-urgency (low-number) quest/
    project has its compute claimed ahead of commodity work. Within a
    prio band, ``queued_since`` — the ``created_at`` of the row's current
    ``STATUS:queued`` tag (a status replace is DELETE+INSERT, so a
    re-queue stamps it afresh) — orders by how long the row has been
    claimable, NOT by ``ref_id``. Pure ``ref_id`` order starved every
    long-lived coordinator loop younger than the claim cap: prod
    2026-09-25, coordinator ``limit=4``, four older ``quest_tick`` loops
    re-parked on a 5-min heartbeat and were re-queued by every ~30-min
    rotation, so three consecutive cycles claimed the same four lowest
    ref_ids while four younger loops sat ``queued`` for hours with no
    ``lease_until`` (the wake_runner lease fix made them survive; before
    it the reaper freed the slots by killing them). For rows queued once
    and never re-queued (the common case) ``queued_since`` order equals
    ``ref_id`` order, so nothing else moves. Oldest-first
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

    **Job-type capability gate (gr335087).** When ``check_job_type_requires``
    is True (``coordinator`` only), a row whose job_type declares a
    :func:`job_type_requires` token not advertised on the host that would run
    it (its ``target_node`` pin, else this claiming host) is dropped from
    THIS claim via :func:`_coordinator_capability_ok` — check-only, no
    reservation (see that function's docstring for why the reservation path
    above is unsafe for a Yield-heavy coordinator job_type like
    ``quest_tick``). Off by default: every other caller is unaffected.

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
    service_pinned = _service_pinned_hosts(conn)
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
        bumps ``meta.attempts`` for either reason and appends a
        capped forensic entry to ``meta.reclaims``. The caller (each
        executor's own claim wrapper) is responsible for checking
        ``meta['attempts']`` against :data:`MAX_ATTEMPTS` and poison-
        failing — this function only does the bookkeeping, mirroring
        ssh_node's original split of "compute the count" vs "act on it".

        The SQL candidate and the heartbeat snapshot used for the forensic
        reason may see different epochs. This cannot change the attempt
        count or steal a live row: the candidate was already row-locked.
        """
        # Lazy: handlers.skill is the build-info owner (precis-status);
        # workers import it at call time only (workers→handlers layering).
        from precis.handlers.skill import code_stamp

        fields: dict[str, Any] = {
            "lease_boot_id": lease_boot_id,
            "lease_process": lease_process,
            "lease_host": lease_host,
            # Which build ran it — re-stamped on every (re)claim, since the
            # reclaiming worker's code is what runs from here (gr346951).
            "lease_code": code_stamp(),
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
        meta["lease_code"] = fields["lease_code"]
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
        pin_hosts = _service_pin_hosts_for(meta, service_pinned)
        if pin_hosts is not None and default_host not in pin_hosts:
            # Placement pin (gr506045): the service's host-scoped enabled
            # rows name the host(s) this job_type may run on. Every other
            # host skips it — the row stays queued, it does not fail; the
            # lock frees at commit so it re-gates next cycle.
            log.debug(
                "claim: skip job %s on %s — service_pin=%s pins to %s",
                ref_id,
                default_host,
                meta.get("job_type"),
                sorted(pin_hosts),
            )
            return None
        if check_job_type_requires and not _coordinator_capability_ok(
            meta, advertised, default_host
        ):
            # Check-only job-type capability gate (gr335087) — see
            # claim_executor_jobs's docstring. Runs before anything else so a
            # capability-less host never reaches (and never reserves against)
            # the resource_slots path below for this row.
            return None
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
        dropped = sorted(
            res
            for res in requires
            if not res.startswith("llm:") and res not in advertised_here
        )
        if dropped and _host_recently_dark(conn, res_host):
            # The reservation host's heartbeat is stale-but-recent — its
            # slot map dropped out of ``advertised`` mid-outage, so claiming
            # now would run the job UNRESERVED into a co-claimed pile-up
            # (gr491098: five gpu:1 autocatpath seeds claimed on a dark
            # castor in 25s, all pinned to the same cpuset; the 5400s-wall
            # pair was deadline-killed while their pollux twins finished in
            # ~72min). Defer: the lock frees at commit and the job re-gates
            # once the heartbeat/probe repopulates. ``_host_recently_dark``'s
            # staleness ceiling keeps a permanently dark host fall-through.
            log.info(
                "claim: defer job %s — reservation host %s heartbeat dark, "
                "would drop requires %s",
                ref_id,
                res_host,
                dropped,
            )
            return None
        if reservable and not reserve_resource_slots(conn, res_host, reservable):
            # A live slot is full → drop it; the lock frees at commit and
            # the job waits for capacity on that host.
            return None
        if reservable:
            reserved = {"host": res_host, "slots": reservable}
            conn.execute(
                "UPDATE refs SET meta = (meta - 'requires_dropped') || "
                "jsonb_build_object('reserved', %s::jsonb) WHERE ref_id = %s",
                (json.dumps(reserved), ref_id),
            )
            meta["reserved"] = reserved
            meta.pop("requires_dropped", None)  # stale stamp from an earlier
            # unreserved claim — this run holds its declared gate
        if dropped:
            # Durable record that this run went unreserved — ``meta.reserved``
            # is popped at terminalization, so without the stamp a post-mortem
            # can't tell "ran under its slot gate" from "the gate silently
            # dropped" (the gr491098 timeout vs. a genuinely oversized seed).
            conn.execute(
                "UPDATE refs SET meta = meta || "
                "jsonb_build_object('requires_dropped', %s::jsonb) "
                "WHERE ref_id = %s",
                (json.dumps(dropped), ref_id),
            )
            meta["requires_dropped"] = dropped
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
               AND r.retired_at IS NULL
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
    # ``STATUS:queued|yielded`` only — the reclaim pre-pass above already
    # handled this cycle's bounded slice of stale-running candidates, so
    # this query no longer carries the running-row OR-arms (moved above).
    # ``queued_since`` is needed twice — as a column and inside the aged
    # Per-job_type running caps (D4 cap half): code-shipped
    # :data:`_MAX_RUNNING_DEFAULTS` plus ``app_state``
    # ``claim.max_running.<job_type>`` overrides bound how many rows of a
    # type may be RUNNING on this host — the derived_drain band can never
    # occupy every ``job_inproc`` process. At-cap types are EXCLUDED from
    # the candidate set, so a capped band can't starve rows queued behind
    # it. A per-type advisory lock serializes the count+claim pair in
    # this transaction, so two concurrent claimers can't both pass the
    # cap.
    cap_sql = ""
    cap_params: tuple[Any, ...] = ()
    caps: dict[str, int] = {}
    running_by_type: dict[str, int] = {}
    running_by_share: dict[str, int] = {}
    claim_host = node or reserve_host_id
    if claim_host is not None:
        caps = job_type_running_caps(conn)
        for jt in sorted(caps):
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
                (f"claim-cap:{jt}",),
            )
        # One grouped read feeds both the per-type cap counts and the
        # D4-share-half fair-share counts (the ``params.pass`` band
        # discriminator — :func:`_share_key_of`).
        running_by_type = {}
        for jt, ps, cnt in conn.execute(
            "SELECT r2.meta->>'job_type', r2.meta->'params'->>'pass', "
            "count(*) FROM refs r2 "
            "WHERE r2.kind = 'job' AND r2.retired_at IS NULL "
            "AND r2.meta->>'executor' = %s "
            "AND r2.meta->>'lease_host' = %s "
            "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t "
            "USING (tag_id) WHERE rt.ref_id = r2.ref_id "
            "AND t.namespace = %s AND t.value = %s) "
            "GROUP BY 1, 2",
            (executor, claim_host, STATUS_NAMESPACE, RUNNING),
        ).fetchall():
            jt_s = str(jt)
            running_by_type[jt_s] = running_by_type.get(jt_s, 0) + int(cnt)
            running_by_share[_share_key_of(jt_s, ps)] = int(cnt)
        at_cap = sorted(
            jt for jt, cap in caps.items() if running_by_type.get(jt, 0) >= cap
        )
        if at_cap:
            cap_sql = " AND COALESCE(r.meta->>'job_type', '') <> ALL(%s)"
            cap_params = (at_cap,)

    # ── Fresh-work pass (unchanged ordering/semantics) ──────────────────
    # ``q.qs`` (the queued-tag's stamp) and ``b.age_s``/``b.base_class``
    # are computed once per row by LATERALs — the claim-order expressions
    # read the aliases instead of repeating correlated subqueries.
    class_rank_on = claim_class_rank_enabled()
    claim_rank_sql, aged_prio_sql, order_params = _claim_order_sql(
        claim_age_step_s(),
        claim_class_age_s(),
        class_rank=class_rank_on,
    )
    # D4 share half (job-scheduling-fairness slice 5, activated by the
    # gr506754 measurement): among candidates of the winning class, prefer
    # the share — ``job_type`` refined by the ``params.pass`` band
    # discriminator (:func:`_share_key_of`) — with the FEWEST running rows
    # on this lane, *then* the aged-prio/queued_since/ref_id key. The
    # materialize SMALL bands share one ``derived_drain`` job_type, so a
    # job_type-granular share can't separate them: the sibling band that
    # holds the capped lane starves the other for hours. The join feeds the
    # ORDER BY so the overfetch window actually contains the under-running
    # share's rows; the Python re-rank reads the same counts through
    # ``running_by_share``. Runs only under the class order — the
    # ``PRECIS_CLAIM_CLASS_RANK=0`` kill switch keeps the pre-slice-2
    # ORDER BY verbatim.
    share_join_sql = ""
    share_col_sql = "0"
    share_order_sql = ""
    share_params: tuple[Any, ...] = ()
    if class_rank_on and claim_host is not None:
        share_col_sql = "COALESCE(s.n, 0)"
        share_order_sql = "share_running ASC, "
        share_join_sql = """
          LEFT JOIN (
                SELECT r2.meta->>'job_type' AS sjt,
                       COALESCE(r2.meta->'params'->>'pass', '') AS sps,
                       count(*) AS n
                  FROM refs r2
                 WHERE r2.kind = 'job' AND r2.retired_at IS NULL
                   AND r2.meta->>'executor' = %s
                   AND r2.meta->>'lease_host' = %s
                   AND EXISTS (
                         SELECT 1 FROM ref_tags rt2 JOIN tags t2 USING (tag_id)
                          WHERE rt2.ref_id = r2.ref_id
                            AND t2.namespace = %s AND t2.value = %s
                       )
                 GROUP BY 1, 2
              ) s ON s.sjt = r.meta->>'job_type'
                 AND s.sps = COALESCE(r.meta->'params'->>'pass', '')"""
        share_params = (executor, claim_host, STATUS_NAMESPACE, RUNNING)
    rows = conn.execute(
        f"""
        SELECT r.ref_id, r.title, r.meta, r.prio, r.created_at,
               q.qs AS queued_since,
               {claim_rank_sql} AS claim_rank,
               {aged_prio_sql} AS aged_prio,
               {share_col_sql} AS share_running
          FROM refs r
          CROSS JOIN LATERAL (
                SELECT max(rt.created_at) AS qs
                  FROM ref_tags rt JOIN tags t USING (tag_id)
                 WHERE rt.ref_id = r.ref_id
                   AND t.namespace = %s AND t.value = ANY(%s)
              ) q
          CROSS JOIN LATERAL (
                SELECT COALESCE(EXTRACT(EPOCH FROM (now() - q.qs))::float8, 0)
                       AS age_s,
                       {_CLASS_RANK_CASE} AS base_class
              ) b
          CROSS JOIN LATERAL (
                SELECT min({_CLASS_RANK_CASE.replace("r.", "r3.")}) AS top_class
                  FROM refs r3
                 WHERE r3.kind = 'job' AND r3.retired_at IS NULL
                   AND r3.meta->>'executor' = %s
                   AND EXISTS (
                         SELECT 1 FROM ref_tags rt3 JOIN tags t3 USING (tag_id)
                          WHERE rt3.ref_id = r3.ref_id
                            AND t3.namespace = %s AND t3.value = ANY(%s)
                       )
                   AND COALESCE(r3.prio, %s)::float8 <> 1
              ) t{share_join_sql}
         WHERE r.kind = 'job'
           AND r.retired_at IS NULL
           AND r.meta->>'executor' = %s
           AND (
                (r.meta->'params'->>'target_node') IS NULL
             OR (r.meta->'params'->>'target_node') = %s
           )
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s AND t.value = ANY(%s)
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
               ){exclusion_sql}{parent_sql}{suspend_sql}{cap_sql}
         ORDER BY claim_rank ASC, {share_order_sql}aged_prio ASC,
                  q.qs ASC, r.ref_id ASC
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """,
        (
            *order_params,
            STATUS_NAMESPACE,
            list(CLAIMABLE),
            _DEFAULT_JOB_PRIO,
            _DEFAULT_JOB_PRIO,
            executor,
            STATUS_NAMESPACE,
            list(CLAIMABLE),
            _DEFAULT_JOB_PRIO,
            *share_params,
            executor,
            node,
            STATUS_NAMESPACE,
            list(CLAIMABLE),
            STATUS_NAMESPACE,
            list(TERMINAL),
            *suspend_params,
            *cap_params,
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

    # D2/D3 effective rank over the locked candidates: the SQL
    # ``claim_rank`` promotes a row one class per class-age interval with
    # an absolute floor; the effective clamp here is the top BASE class
    # actually present among non-prio-1 candidates — a row climbs until
    # it ties that top class and then wins ties by in-class age and
    # queued_since, so a class flood can hold a lower class down only
    # for a bounded wait (D3 acceptance: no background row waits more
    # than 3 h on a live lane).
    age_step = claim_age_step_s()
    class_age = claim_class_age_s()
    now_utc = datetime.now(UTC)

    def _row_base(r: Any) -> int:
        origin = (dict(r[2] or {})).get("origin")
        if isinstance(origin, str) and origin in _BASE_CLASS_RANK:
            return _BASE_CLASS_RANK[origin]
        p = float(r[3] if r[3] is not None else _DEFAULT_JOB_PRIO)
        return 2 if p <= 2 else 3

    def _row_prio(r: Any) -> float:
        return float(r[3] if r[3] is not None else _DEFAULT_JOB_PRIO)

    top_class = min((_row_base(r) for r in rows if _row_prio(r) != 1.0), default=0)

    def _row_age_s(r: Any) -> float:
        qs = r[5] or r[4]
        if qs is None:
            return 0.0
        if getattr(qs, "tzinfo", None) is None:
            qs = qs.replace(tzinfo=UTC)
        return max(0.0, (now_utc - qs).total_seconds())

    def _order_key(r: Any) -> tuple[float, float, float, float, Any, int]:
        prio = _row_prio(r)
        age = _row_age_s(r)
        if class_rank_on and prio == 1.0:
            rank, floor = -1.0, 5.0
        elif class_rank_on:
            base = _row_base(r)
            # May tie the top class queued, never reaches agent/human.
            rank = float(
                max(base - int(age // class_age), min(base, max(top_class, 2)))
            )
            floor = 5.0 if rank < 2 else 2.0
        else:
            # Kill switch: the pre-slice-2 order — aged prio, 1.5 floor.
            rank, floor = 0.0, 1.5
        aged = max(prio - age / age_step, min(prio, floor)) if age_step > 0 else prio
        scarcity = _scarcity(effective_requires(dict(r[2] or {})), host_count)
        # D4 share half: reads ``running_by_share`` as it stands NOW (the
        # pick loop bumps it per claim), so a multi-claim batch
        # round-robins the shares instead of draining one share's prefix.
        share = (
            float(running_by_share.get(_share_key(dict(r[2] or {})), 0))
            if class_rank_on
            else 0.0
        )
        # ``queued_since`` (r[5]) before ``ref_id``: the same FIFO-within-band
        # rule the SQL ORDER BY applies — see the "Claim ordering" note in
        # :func:`claim_executor_jobs`. Never NULL for a row the EXISTS above
        # admitted, but ``or r[4]`` keeps the key total if a race retags it.
        return (-scarcity, rank, share, aged, r[5] or r[4], int(r[0]))

    fresh_claimed = 0
    remaining = sorted(rows, key=_order_key)
    while remaining and fresh_claimed < limit:
        # Pick the best row under the CURRENT share counts — popping
        # argmin each time keeps a ``limit``-sized batch interleaving
        # shares rather than claiming a statically-sorted prefix.
        r = remaining.pop(
            min(range(len(remaining)), key=lambda i: _order_key(remaining[i]))
        )
        ref_id, title, meta = int(r[0]), str(r[1]), dict(r[2] or {})
        job_type = str(meta.get("job_type") or "")
        cap = caps.get(job_type)
        if cap is not None and running_by_type.get(job_type, 0) >= cap:
            continue  # hit the cap mid-claim — the SQL exclusion only sees the count at query time
        claim = _finish_claim(ref_id, title, meta, QUEUED, r[4])
        if claim is None:
            continue
        claimed.append(claim)
        fresh_claimed += 1
        running_by_type[job_type] = running_by_type.get(job_type, 0) + 1
        share_key = _share_key(meta)
        running_by_share[share_key] = running_by_share.get(share_key, 0) + 1
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
    previous implementation called ``store.list_chunks_for_ref`` which
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
        blocks = store.chunks.list_chunks_for_ref(ref_id)
        next_pos = len(blocks)
    store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(
                ord=next_pos,
                text=mask_secrets(text),
                meta={"chunk_kind": chunk_kind},
            )
        ],
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
    (docs/backlog/autocatpath-seed-health.md). ``None`` is
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
    """Merge ``fields`` into ``refs.meta`` (non-finite floats → ``null``).

    Job transcripts, results and errors are captured output that cannot be
    rephrased, so credential-shaped spans are masked on the way in."""
    conn.execute(
        "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
        (Jsonb(mask_secrets_deep(_finite_json(fields))), ref_id),
    )


# ── transient-failure classification (retryable child-failed,
#    docs/backlog/todo-parked-transient-failures.md) ─────────────────


def classify_transient_backoff_hours(reason: str) -> float | None:
    """Backoff (hours) when ``reason`` reads as a transient failure, else
    ``None``. The table is the router's (:mod:`precis.utils.llm.failure`,
    one owner): a Claude quota notice backs off to its parsed reset
    instant, an account API usage-limit notice to its absolute date, a
    rate limit fifteen minutes, a spend cap two hours, a transport fault
    thirty minutes. Matched against :func:`record_failure`'s ``reason`` —
    the one funnel every executor's failure passes through — so
    classification needs no per-executor wiring. A false negative keeps
    today's 12h·2ᴺ unpark cool-down; a false positive only means one
    earlier retry, still bounded by ``sweeper.UNPARK_CAP``."""
    return _failure_backoff_hours(reason, now=datetime.now(UTC))


#: Cap on the ``reason`` string mirrored into ``refs.meta.error`` by
#: :func:`record_failure` — the full text always lands in the ``job_event``
#: chunk regardless; this is just a bounded breadcrumb so a downstream
#: consumer (e.g. an auto-filed gripe's ``detail`` dict) has *something*
#: actionable without risking a huge traceback bloating ``refs.meta``.
_ERROR_META_CAP = 500

#: Park horizon when a window-scoped pause names no retry instant — the
#: quota wording's own two-hour fallback
#: (:data:`precis.utils.llm.failure.DEFAULT_HORIZON`).
PAUSE_BACKOFF = timedelta(hours=2)

#: Successive parks before a job stops deferring and fails normally. A
#: window pause is indefinite upstream, but a job that can never be
#: admitted shouldn't sleep behind ``STATUS:waiting_time`` forever —
#: same bound deferred_llm_call's ``MAX_DEFERRALS`` puts on its
#: coordinator-lane re-parks (six five-hour windows is over a day).
MAX_PAUSE_PARKS = 6


def park_job_until(
    store: Store,
    conn: Connection,
    ref_id: int,
    wake_at: datetime | None,
    note: str,
) -> bool:
    """Park a running job until ``wake_at`` — the non-coordinator lane's
    Yield (gr482938): ``STATUS:waiting_time`` + ``meta.wake_when``
    ``{'kind': 'at_time', 'payload': {'ts': …}}``, which the
    wake_runner's ``at_time`` selector re-queues once the instant passes
    (the selector is executor-agnostic — the re-queued row goes back to
    whichever executor claimed it). ``wake_at=None`` parks for
    :data:`PAUSE_BACKOFF`.

    Use for a *window-scoped* pause — a quota taper / dollar cap breaker
    trip, a quota-exhaustion notice, a saturated local slot — where the
    SAME job must re-run after the window clears, instead of
    :func:`record_failure` (which terminal-fails it and trips the
    all-failing watchdog on a deliberate pause). The reservation is
    released so a parked job holds no slots while it sleeps; the wake
    re-queue clears the stale lease (``wake_runner._requeue``), so the
    next claim is a clean fresh-work claim with no attempt bump.

    ``True`` when the job parked; ``False`` once it has already parked
    :data:`MAX_PAUSE_PARKS` times (``meta.pause_parks``) — the caller then
    records the pause as a normal failure instead of letting a
    never-lifting window hide the job forever. Caller commits.
    """
    row = conn.execute(
        "SELECT COALESCE((meta->>'pause_parks')::int, 0) FROM refs WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    parks = int(row[0]) if row and row[0] is not None else 0
    if parks >= MAX_PAUSE_PARKS:
        return False
    at = wake_at if wake_at is not None else datetime.now(UTC) + PAUSE_BACKOFF
    append_chunk(store, ref_id, JOB_EVENT_KIND, note, conn=conn)
    set_meta(
        conn,
        ref_id,
        wake_when={
            "kind": "at_time",
            "payload": {"ts": int(at.timestamp())},
        },
        pause_parks=parks + 1,
    )
    release_job_reservation(conn, ref_id)
    set_status(store, ref_id, WAITING_TIME, conn=conn)
    return True


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
    instead of laundering both into the same bare ``STATUS:failed``. When
    ``failure_class`` is given, ``reason`` (truncated to
    ``_ERROR_META_CAP``) is also stamped onto ``refs.meta.error`` alongside
    it — the full, untruncated ``reason`` always lands in the ``job_event``
    chunk either way; the meta copy exists so a downstream consumer that
    only reads ``meta`` (e.g. an auto-filed infra gripe) isn't stuck with
    a bare ``failure_class`` and no idea what actually happened.

    ``open_tag`` (optional, parked-leaf-recovery,
    docs/backlog/parked-leaf-recovery.md) stamps an ``OPEN:<open_tag>`` tag
    on the job BEFORE it's marked failed — the generic layer a caller that
    observed a classifiable infra condition (e.g. a compute-executor
    subprocess dying by signal, or exiting without its expected result
    file — ``precis_pathway.seed_job``'s ``infra:child-killed``) reaches for
    instead of duplicating the tag-then-fail dance itself. Written inside
    the SAME transaction that drives ``STATUS:failed`` and calls
    :func:`~precis.utils.job_bubble.bubble_job_failure` below, so a tag
    value that's also a member of
    ``precis.utils.job_bubble.INFRA_FAILURE_TAGS`` is guaranteed visible
    to the bubble's infra-classification read — enabling the same bounded
    auto-retry a lease-expiry orphan gets, rather than an immediate latch.

    A ``reason`` matching :func:`classify_transient_backoff_hours`
    (rate/spend limits, transient API faults) additionally stamps
    ``meta.retry_after`` on the job — the sweeper's unpark phase re-arms
    the parked parent at that time instead of the 12h·2ᴺ cool-down.
    """
    with store.pool.connection() as conn:
        if open_tag is not None:
            store.add_tag(ref_id, Tag.open(open_tag), set_by="system", conn=conn)
        append_chunk(store, ref_id, JOB_EVENT_KIND, reason, conn=conn)
        set_status(store, ref_id, FAILED, conn=conn)
        if failure_class is not None:
            set_meta(
                conn,
                ref_id,
                failure_class=failure_class,
                error=reason[:_ERROR_META_CAP],
            )
        # Transient classification (docs/backlog/
        # todo-parked-transient-failures.md): a retryable cause stamps
        # ``meta.retry_after`` (ISO) on the job — the tag shape the bubble
        # writes stays uniform; the sweeper's unpark phase reads this to
        # re-arm the parked parent on the real backoff instead of the
        # 12h·2ᴺ cool-down. Same tx as the STATUS flip + bubble.
        backoff_hours = classify_transient_backoff_hours(reason)
        if backoff_hours is not None:
            transient_fields: dict[str, Any] = {
                "retry_after": (
                    datetime.now(UTC) + timedelta(hours=backoff_hours)
                ).isoformat()
            }
            if failure_class is None:
                transient_fields["failure_class"] = "transient"
                transient_fields["error"] = reason[:_ERROR_META_CAP]
            set_meta(conn, ref_id, **transient_fields)
        if gripe_rollback is not None:
            set_status(store, gripe_rollback, "open", conn=conn)
        # Slice-5 failure bubble.
        from precis.utils.job_bubble import bubble_job_failure

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
    (bumped for either epoch or expiry reclaims). This applies the cap's
    consequence, generalized from ssh_node's original inline guard:
    at the cap, the row is INFRA-failed + bubbled instead of run again
    — a job that crash-loops its worker every generation eventually
    stops stealing itself forever, even when every reclaim is an epoch.

    Returns True (and fails the job, in ``conn``'s transaction) when
    poisoned; False when the caller should proceed to run it normally.
    ``max_attempts`` overrides :data:`MAX_ATTEMPTS` for a caller with its
    own cap (defaults to the shared one — no known caller needs a
    different value today, but the hook costs nothing).

    **Legacy backstop.** Old epoch reclaims did not increment attempts.
    A job carrying that history still fails when its capped forensic
    ``meta.reclaims`` list fills, even if its new attempt count is low.
    """
    cap = MAX_ATTEMPTS if max_attempts is None else max_attempts
    attempts = int(meta.get("attempts") or 0)
    reclaims = meta.get("reclaims") or []
    reclaim_churn = len(reclaims) >= _RECLAIMS_CAP
    if attempts < cap and not reclaim_churn:
        return False
    if reclaim_churn and attempts < cap:
        reason = (
            f"runner: {len(reclaims)} reclaims recorded (reclaim-churn cap "
            f"{_RECLAIMS_CAP} reached) while only {attempts} run attempt(s) "
            "were counted — legacy uncounted epoch reclaims; failing as "
            "a poison-guard backstop"
        )
    else:
        last_why = reclaims[-1].get("why") if reclaims else None
        cause = (
            "worker restart (epoch)"
            if last_why == RECLAIM_WHY_EPOCH
            else "lease expiry"
            if last_why == RECLAIM_WHY_EXPIRY
            else "unknown"
        )
        reason = (
            f"runner: reached {cap} run attempts (crash-loop guard; "
            f"last reclaim: {cause}) — failing"
        )
    append_chunk(store, ref_id, JOB_EVENT_KIND, reason, conn=conn)
    set_meta(conn, ref_id, failure_class="infra", error=reason[:_ERROR_META_CAP])
    set_status(store, ref_id, FAILED, conn=conn)
    from precis.utils.job_bubble import bubble_job_failure

    bubble_job_failure(store, ref_id, conn=conn)
    return True


__all__ = [
    "CANCELLED",
    "CANCEL_REQUESTED",
    "FAILED",
    "JOB_EVENT_KIND",
    "JOB_SUMMARY_KIND",
    "MAX_ATTEMPTS",
    "MAX_PAUSE_PARKS",
    "PAUSE_BACKOFF",
    "QUEUED",
    "RECLAIM_WHY_EPOCH",
    "RECLAIM_WHY_EXPIRY",
    "RUNNING",
    "STATUS_NAMESPACE",
    "SUCCEEDED",
    "TERMINAL",
    "WAITING",
    "WAITING_ASK_USER",
    "WAITING_CHILDREN",
    "WAITING_MANUAL_KICK",
    "WAITING_TIME",
    "append_chunk",
    "claim_executor_jobs",
    "current_status",
    "effective_requires",
    "is_cancel_requested",
    "job_type_requires",
    "max_job_attempts",
    "maybe_reset_gpu_after_kill",
    "park_job_until",
    "poison_guard",
    "record_failure",
    "release_job_reservation",
    "renew_lease_if_mine",
    "reserve_host",
    "set_meta",
    "set_status",
]
