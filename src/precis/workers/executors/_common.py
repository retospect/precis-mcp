"""Shared substrate for the job executors and the wake_runner.

``claude_inproc``, ``coordinator``, and :mod:`precis.workers.wake_runner`
share one closed ``STATUS:*`` tag namespace, the same ``kind='job'`` claim
SQL shape, and the same job-row helpers — this module is their single home.

Executors import these under their existing ``_name`` aliases: bare-name
references in their bodies, and tests' ``monkeypatch.setattr(module,
"_set_status", ...)``, keep working unchanged.

The non-terminal status policy lives here too: ``poison_guard`` (the
§H crash-loop cap) and ``handle_memory_kill`` (gr595538 — a memory-cap
kill requeues once with the footprint estimate bumped and a
``killed:memory`` flag; a second kill parks at ``STATUS:parked`` for the
``memory_triage`` job_type, which decides drop / gripe / ask-user). The
``parked`` value rides the ``WAITING`` tuple so finalize passes leave it
alone, but no wake_runner selector matches it — a parked job waits on a
verdict, not on time.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
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
from precis.store._todo_sql import (
    _doable_exclusion_clause,
    not_served_wait_sql,
    parent_gate_exclusion_sql,
)
from precis.store.types import ChunkInsert, Tag
from precis.todo import TERMINAL_TODO_STATUSES, blocker_terminal_sql
from precis.utils import dispatch_score as _dscore
from precis.utils import handle_registry, job_footprint
from precis.utils.llm.failure import backoff_hours as _failure_backoff_hours
from precis.utils.secret_scan import mask_secrets, mask_secrets_deep
from precis.workers import footprint_admission as _footprint_admission
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
#: Terminal wall-deadline verdict (gr647914 / td648501): the wall-clock
#: kill's distinct status, exposed only when the job_type declares a
#: ``deadline_kill_meta`` verdict (``land_slice``) — a plain deadline kill
#: of any other type still lands ``failed``. Hyphenated (not
#: ``timed_out``) so the job card renders the verdict as ``timed-out``;
#: only ``ssh_node``'s deadline path writes it. ``run_slice`` never
#: carries it, which is what keeps ``plans/lanes.py``'s restated
#: terminal tuple (run_slice lane slots, unreadable from here) correct.
TIMED_OUT = "timed-out"

# STATUS:waiting_* values written by a coordinator Yield; each maps to
# one ``WakeWhen.kind`` so the wake_runner's selectivity stays cheap
# (exact match on a closed-status value, not a LIKE).
WAITING_CHILDREN = "waiting_children"
WAITING_TIME = "waiting_time"
WAITING_ASK_USER = "waiting_ask_user"
WAITING_MANUAL_KICK = "waiting_manual_kick"

#: A job killed for memory a second time (gr595538) — parked for the
#: ``memory_triage`` job_type, never woken by a selector: a parked job
#: waits for the triage verdict (drop / gripe / ask-user), not for time.
PARKED = "parked"

#: Every waiting status — a parked job (coordinator Yield, a
#: non-coordinator lane's :func:`park_job_until`, or a twice-memory-killed
#: row's :data:`PARKED`). Non-terminal but NOT still-running either: a
#: finalize pass must leave these alone.
WAITING = (
    WAITING_CHILDREN,
    WAITING_TIME,
    WAITING_ASK_USER,
    WAITING_MANUAL_KICK,
    PARKED,
)

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
TERMINAL = (SUCCEEDED, FAILED, CANCELLED, TIMED_OUT)

#: Seconds a job must have been terminal (its STATUS tag's ``created_at``)
#: before its detached child counts as orphaned: the ssh_node reaper's
#: grace, and the same wait before a terminal job's spawn holds are
#: refunded under a child that may still be exiting (gr762788).
ORPHAN_REAP_GRACE_S = 120

#: Non-terminal values that mean the job LEFT ``STATUS:running``
#: (checkpoint yield, park, requeue) — the footprint segment folds
#: there. ``cancel_requested`` is absent on purpose: the job is still
#: running; the fold happens at its real leave or terminal stamp.
LEAVE_RUNNING = frozenset({QUEUED, YIELDED, *WAITING})

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

#: Reserved background share (td656098): the class order lets a promoted
#: background row tie recurring but never beat agent work, so an
#: always-busy agent queue starved it. About one claim in this many goes
#: to the oldest waiting background row — unless a human-class or prio-1
#: row waits (they still preempt). Spare capacity drains background
#: freely anyway. The tick is per process and per executor.
_BG_SHARE_EVERY = 10
_bg_share_ticks: dict[str, int] = {}

#: ``0`` restores the exact pre-slice-2 claim order (aged prio, queued
#: age, ref_id — the 1.5 floor); anything else keeps the class order.
CLAIM_CLASS_RANK_ENV = "PRECIS_CLAIM_CLASS_RANK"

#: Release-pass legs (gr584136, S3 of td584381) plus the dark-factory /
#: release heartbeat ticks (td593845 review): these ``job_type``s claim
#: in a band strictly between ``recurring`` and ``background`` — a
#: re-queued ``ci_verdict_poll`` (10-minute wake cadence), a
#: cut/deploy/confirm stage boundary, or a ``release_poll`` /
#: ``plan_dispatch`` / ``factory_rescue_tick`` / ``ci_watch`` tick must
#: claim within one worker-slot turnover even behind a full
#: ``derived_drain`` backlog, while still ranking behind recurring work.
#: The ticks mint under recurring todos, so their ``recurring`` origin
#: left them at class 2 — an aged background row promotes to tie them
#: there and wins ``queued_since`` (gr592211: ``release_poll`` sat 15+
#: min behind drains). Pinned at 2.5 with the freeze, no lower class
#: reaches them while one is claimable.
RELEASE_LEG_JOB_TYPES: tuple[str, ...] = (
    "ci_verdict_poll",
    "release_cut",
    "release_deploy",
    "release_confirm",
    "release_poll",
    "factory_rescue_tick",
    "ci_watch",
    "plan_dispatch",
)

#: The release band's class rank — strictly between recurring (2) and
#: background (3). Fractional on purpose: the integer class-ageing step
#: (``base - floor(age / class_age)``) can never land a promoted
#: lower-class row ON the band, and no existing rank moves.
_RELEASE_LEG_RANK = 2.5

#: The release job_types as a SQL ``IN`` literal — module constants only,
#: never row data.
_RELEASE_LEG_TYPES_SQL = (
    "(" + ", ".join(f"'{jt}'" for jt in RELEASE_LEG_JOB_TYPES) + ")"
)

#: The dark-factory pipeline job types (gr657333): the ops that move a
#: slice or a release forward on the box-heavy lanes. Within ONE claim
#: class they rank ahead of bulk seed/drain work — jo656686's
#: ``run_slice`` sat FIFO behind 58 queued ``autocatpath_seed`` rows for
#: 51+ min on ``ssh_node``: the class order ties them (both
#: ``background``) and the lane-deficit term only prefers the lane the
#: host has under-served, so a fresh pipeline op can still lose an
#: equal-deficit pick to every older bulk row. The ``release_*`` ops
#: are listed for completeness — the 2.5 band already outranks
#: background, so their
#: membership is a no-op today that keeps the set self-describing.
FACTORY_PIPELINE_JOB_TYPES: tuple[str, ...] = (
    "run_slice",
    "land_slice",
    "repo_release",
    "pin_bump",
    "release_cut",
    "release_deploy",
    "release_confirm",
    "ci_red_digest",
    "ci_rerun",
    "dep_check",
    "pr_action",
    "lane_health",
)

#: The factory job_types as a SQL ``IN`` literal — module constants only.
_FACTORY_TYPES_SQL = (
    "(" + ", ".join(f"'{jt}'" for jt in FACTORY_PIPELINE_JOB_TYPES) + ")"
)

#: ``meta.job_type``/``meta.origin`` -> base class rank (D2: human 0, agent
#: 1, recurring 2, release 2.5, background 3). The release band is keyed
#: on ``job_type`` (the legs' origin stays ``background``); a missing
#: origin (pre-slice-1 rows) reads as ``recurring`` at prio <= 2 — the old
#: cron band — else ``background``. A job whose ``meta.claim_band`` is
#: ``release`` (:data:`precis.utils.dispatch_score.RELEASE_CLAIM_BAND`)
#: claims in the release band too: the waiver gripe's build and land
#: (``plans/red_waiver``, a broken build) rank next to the release legs,
#: ahead of ordinary gripe builds.
_CLASS_RANK_CASE = (
    f"CASE WHEN r.meta->>'job_type' IN {_RELEASE_LEG_TYPES_SQL} "
    f"OR r.meta->>'{_dscore.CLAIM_BAND_META_KEY}' = '{_dscore.RELEASE_CLAIM_BAND}' "
    f"THEN {_RELEASE_LEG_RANK} ELSE CASE r.meta->>'origin' "
    "WHEN 'human' THEN 0 WHEN 'agent' THEN 1 WHEN 'recurring' THEN 2 "
    "WHEN 'background' THEN 3 "
    "ELSE (CASE WHEN COALESCE(r.prio, %s)::float8 <= 2 THEN 2 ELSE 3 END) "
    "END END"
)
_BASE_CLASS_RANK = {"human": 0, "agent": 1, "recurring": 2, "background": 3}

#: Code-shipped per-host running caps by ``job_type`` (D4 cap half) —
#: the derived_drain band can never occupy every ``job_inproc`` process.
#: An operator overrides per setting ``claim.max_running.<job_type>``
#: in ``app_state`` (an int; ``0`` suspends the type entirely).
#:
#: ``derived_drain`` is 1, deliberately below the dedicated drain-lane
#: count (``precis_worker_drain_lanes``, default 2 in
#: ``deploy/playbooks/20d-precis-worker-drain.yml``): the collapsed and
#: agent-lane ``job_inproc`` passes are serial and busy for tens of
#: minutes to hours with other work, so the drain lanes are the free
#: ``job_inproc`` slots, and a cap equal to their count let two drains
#: hold every slot while release_poll / ci_verdict_poll / release_cut sat
#: queued 15-35 min per stall (gr646119, prod 2026-11-12; ranking orders
#: the queue but cannot free a slot). Keep this strictly below the lane
#: count; ``tests/workers/test_claim_fairness.py`` pins the relation
#: against the playbook default.
_MAX_RUNNING_DEFAULTS: dict[str, int] = {"derived_drain": 1}
_MAX_RUNNING_KEY_PREFIX = "claim.max_running."

# ── Executor lane cap + per-class slot reservation (gr603792) ──────────
#: Code-shipped per-host running caps by ``executor``: the total rows of
#: one executor that may hold ``STATUS:running`` with this host's
#: ``meta.lease_host``. ``ssh_node`` gets 8 — the shape that produced the
#: gripe (seven autocatpath seeds left one slot for the whole factory
#: lane); every other executor stays uncapped. An operator overrides per
#: ``app_state`` ``claim.executor_cap.<executor>`` (an int; ``0``
#: suspends the executor's claims on the host entirely; absent = code
#: default, which for a non-listed executor is uncapped).
_EXECUTOR_CAP_DEFAULTS: dict[str, int] = {"ssh_node": 8}
_EXECUTOR_CAP_KEY_PREFIX = "claim.executor_cap."

#: Slots inside the executor cap held back for named job types
#: (gr603792): ``ssh_node`` keeps one slot for ``run_slice`` and one for
#: ``land_slice``, so a gripe build or a land never waits behind a deep
#: science queue — a compute may not take the last reserved slots. An
#: operator replaces the whole map via ``app_state``
#: ``claim.reserve.<executor>`` (a JSON ``{"<job_type>": <slots>}``).
#: The reservation is static, not demand-driven: the reserved slots are
#: always held back, so a waiting factory job finds them already free
#: instead of needing a compute to finish first.
_EXECUTOR_RESERVE_DEFAULTS: dict[str, dict[str, int]] = {
    "ssh_node": {"run_slice": 1, "land_slice": 1}
}
_RESERVE_KEY_PREFIX = "claim.reserve."


def _executor_lane_lock_key(executor: str, host: str) -> str:
    """Advisory-lock key serializing one executor lane's count+claim on
    one host (gr603792 review): the per-type caps lock on
    ``claim-cap:<job_type>`` — the executor cap locks on this."""
    return f"claim-exec-cap:{executor}:{host}"


# ── Job slicing (me692466) ────────────────────────
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


#: Spawn-footprint CPU override (gr741695): ``app_state``
#: ``claim.footprint.<work>.cpu_milli`` replaces the code default
#: ``cpu_milli`` of one run_slice work class
#: (:data:`precis.workers.job_types.run_slice._WORK_FOOTPRINTS`), read live
#: at claim (the ``launch_fits`` probe) and at the launch's own spawn
#: admission, so an operator re-sizes the reservation without a deploy.
#: A positive int; anything else falls back to the code default.
_FOOTPRINT_KEY_PREFIX = "claim.footprint."
_FOOTPRINT_CPU_FIELD = "cpu_milli"

#: Ceiling on a ``claim.footprint.<work>.cpu_milli`` override — 256 cores.
#: The setting door refuses a bigger value; the runtime read clamps to it,
#: so a row written straight to ``app_state`` can't go unbounded either.
FOOTPRINT_CPU_MAX_MILLI = 256_000

#: ``(key, raw)`` pairs already warned about, so a bad override warns once
#: per worker process, not once per claim candidate per pass.
_footprint_bad_warned: set[tuple[str, str]] = set()


def footprint_cpu_key(work: str) -> str:
    """The ``app_state`` key of ``work``'s CPU override."""
    return f"{_FOOTPRINT_KEY_PREFIX}{work}.{_FOOTPRINT_CPU_FIELD}"


def parse_footprint_cpu_milli(raw: Any) -> int | None:
    """A stored override as a positive int of millicores, else ``None``.

    Integrality is checked on the exact decimal value, before any
    rounding: an int, an int-valued string (``"1000"``) and an exactly
    integral float or float string (``1000.0``, ``"1000.0"``) pass; a bool,
    zero, a negative, a fraction (``"1.5"``, ``"0.99999999999999999"``) or
    garbage is ``None`` — the caller keeps the code default. A value above
    :data:`FOOTPRINT_CPU_MAX_MILLI` clamps to it.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        num = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    if not num.is_finite() or num != num.to_integral_value() or num <= 0:
        return None
    if num >= FOOTPRINT_CPU_MAX_MILLI:
        return FOOTPRINT_CPU_MAX_MILLI
    return int(num)


def footprint_cpu_milli_override(conn: Connection, work: str) -> int | None:
    """``work``'s live ``claim.footprint.<work>.cpu_milli``, or ``None``
    when unset or unusable (warned once per bad value)."""
    key = footprint_cpu_key(work)
    row = conn.execute("SELECT value FROM app_state WHERE key = %s", (key,)).fetchone()
    if row is None:
        return None
    parsed = parse_footprint_cpu_milli(row[0])
    if parsed is None:
        marker = (key, str(row[0]))
        if marker not in _footprint_bad_warned:
            _footprint_bad_warned.add(marker)
            log.warning(
                "claim: %s=%r is not a positive int; using the code default",
                key,
                row[0],
            )
    return parsed


#: A run_slice claim refused by ``launch_fits`` logs at most once per job
#: per this many seconds (gr741695): the claim loop re-evaluates every
#: queued row every pass (~10 s), so an unthrottled line would flood.
_LAUNCH_REFUSAL_LOG_S = 600.0

#: ``ref_id -> monotonic`` of the last refusal line per job.
_launch_refusal_logged: dict[int, float] = {}


def _log_launch_refusal(
    conn: Connection, ref_id: int, host: str, work: str, footprint: Any
) -> None:
    """INFO line naming host, work class and needed vs free units when a
    fresh run_slice claim does not fit the host's spawn ledger.

    Throttled per job (:data:`_LAUNCH_REFUSAL_LOG_S`); the ledger read runs
    only when the line is actually emitted, so a full host's every-pass
    refusals cost one dict lookup each."""
    now = time.monotonic()
    last = _launch_refusal_logged.get(ref_id)
    if last is not None and now - last < _LAUNCH_REFUSAL_LOG_S:
        return
    if len(_launch_refusal_logged) > 4096:
        cutoff = now - _LAUNCH_REFUSAL_LOG_S
        for stale in [k for k, t in _launch_refusal_logged.items() if t < cutoff]:
            del _launch_refusal_logged[stale]
    _launch_refusal_logged[ref_id] = now
    need: dict[str, int] = dict(footprint.requirements())
    rows = conn.execute(
        "SELECT resource, capacity, free FROM resource_slots "
        "WHERE host = %s AND resource = ANY(%s)",
        (host, list(need)),
    ).fetchall()
    have = {str(r): (int(cap), int(free)) for r, cap, free in rows}
    parts = []
    for resource, units in need.items():
        if resource in have:
            cap, free = have[resource]
            parts.append(f"{resource} need {units} free {free}/{cap}")
        else:
            parts.append(f"{resource} need {units} (not published)")
    log.info(
        "claim: hold job %s on %s — %s launch does not fit the spawn ledger: %s",
        ref_id,
        host,
        work,
        "; ".join(parts),
    )


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
    each waiter's claim key — ``(claim_rank, factory_pref, aged_prio,
    queued_since, ref_id)`` from :func:`_claim_order_sql` and
    :data:`FACTORY_PIPELINE_JOB_TYPES` — against the running job's key
    evaluated as-if-just-yielded: its base class (the yield re-stamps
    the tag, so no accumulated age or promotion; ``-1`` for prio 1), its
    factory flag, its raw aged prio, ``now()`` as its queue stamp.
    ``now()`` is after every real tag stamp, so a waiter that ties on
    rank+factory+aged still wins the age tiebreak — exactly the "behind
    jobs already waiting" rule. The factory element lets a queued
    ``run_slice``/``land_slice`` preempt a running bulk seed at its next
    slice boundary (gr657333) — a bulk waiter never preempts a running
    factory op. The lane-deficit share term (gr595541) and the
    Python-only scarcity/affinity terms are not mirrored here — the
    predicate tracks only the SQL rank keys, so it can miss a yield a
    starved lane's waiter would earn on deficit, or take one the real
    order's deficit term would deny; yield checks stay heuristic by
    design."""
    age_step = claim_age_step_s()
    class_age = claim_class_age_s()
    class_rank = claim_class_rank_enabled()
    rank_sql, aged_sql, order_params = _claim_order_sql(
        age_step, class_age, class_rank=class_rank, release_freeze=True
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
            f" CASE WHEN me.job_type IN {_FACTORY_TYPES_SQL} THEN 0 ELSE 1 END,"
            " GREATEST(me.prio,"
            "  LEAST(me.prio,"
            "   CASE WHEN me.base_class < 2 THEN 5 ELSE 2 END)),"
            " now(), me.ref_id)"
        )
        waiter_key = (
            "(o.crank,"
            f" CASE WHEN r.meta->>'job_type' IN {_FACTORY_TYPES_SQL}"
            "  THEN 0 ELSE 1 END,"
            " o.aged, q.qs, r.ref_id)"
        )
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
                 COALESCE(r.prio, %s)::float8 AS prio,
                 r.meta->>'job_type' AS job_type
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
                SELECT min({_CLASS_RANK_CASE.replace("r.", "r3.")}) AS top_class,
                       bool_or({_CLASS_RANK_CASE.replace("r.", "r3.")}
                               = {_RELEASE_LEG_RANK}) AS has_release
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


def executor_lane_limits(
    conn: Connection, executor: str, host: str | None = None
) -> tuple[int | None, dict[str, int]]:
    """``(running cap, {job_type: reserved slots})`` for ``executor``
    on this host (gr603792).

    The cap is the code-shipped :data:`_EXECUTOR_CAP_DEFAULTS` entry
    overlaid with ``app_state`` ``claim.executor_cap.<executor>`` — and
    with ``claim.executor_cap.<executor>.<host>`` when ``host`` is given
    (gr676054: per-host caps on a builder fleet — the host-suffixed key
    replaces the unsuffixed one wholesale); the reserve map is
    :data:`_EXECUTOR_RESERVE_DEFAULTS` REPLACED wholesale by
    ``claim.reserve.<executor>[.<host>]`` (a JSON
    ``{"<job_type>": <slots>}``). ``cap=None`` means uncapped — the
    reserve map is then inert (an unbounded pool has no slots to hold
    back). Read live per claim pass, same as
    :func:`job_type_running_caps`.
    """
    cap: int | None = _EXECUTOR_CAP_DEFAULTS.get(executor)
    reserve = dict(_EXECUTOR_RESERVE_DEFAULTS.get(executor, {}))
    keys = [
        f"{_EXECUTOR_CAP_KEY_PREFIX}{executor}",
        f"{_RESERVE_KEY_PREFIX}{executor}",
    ]
    host_keys = [f"{key}.{host}" for key in keys] if host else []
    rows: dict[str, str] = dict(
        conn.execute(
            "SELECT key, value FROM app_state WHERE key = ANY(%s)",
            (keys + host_keys,),
        ).fetchall()
    )
    cap_key = host_keys[0] if host_keys and host_keys[0] in rows else keys[0]
    raw_cap = rows.get(cap_key)
    if raw_cap is not None:
        try:
            cap = max(0, int(str(raw_cap)))
        except ValueError:
            log.warning("claim: %s=%r is not an int; ignored", cap_key, raw_cap)
    reserve_key = host_keys[1] if host_keys and host_keys[1] in rows else keys[1]
    raw_reserve = rows.get(reserve_key)
    if raw_reserve is not None:
        try:
            parsed = json.loads(str(raw_reserve))
            reserve = {str(k): max(0, int(v)) for k, v in dict(parsed).items()}
        except (ValueError, TypeError, AttributeError):
            log.warning(
                "claim: %s=%r is not a {job_type: slots} map; ignored",
                reserve_key,
                raw_reserve,
            )
    return cap, reserve


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
    stamped = meta.get("share_key")
    if isinstance(stamped, str) and stamped:
        return stamped
    return _share_key_of(str(meta.get("job_type") or ""), params_pass)


#: SQL twin of :func:`_share_key` over ``refs`` alias ``{a}``.
_LANE_KEY_SQL = (
    "COALESCE(NULLIF({a}.meta->>'share_key', ''),"
    " COALESCE({a}.meta->>'job_type', '')"
    " || COALESCE(':' || ({a}.meta->'params'->>'pass'), ''))"
)

#: ``app_state`` prefix of a lane's share weight (default 1.0 — equal).
_SHARE_WEIGHT_KEY_PREFIX = "claim.share."
CLAIM_SHARE_WINDOW_ENV = "PRECIS_CLAIM_SHARE_WINDOW_S"
DEFAULT_CLAIM_SHARE_WINDOW_S = 3600.0
#: Lane-seconds a claim inside one batch is charged, so a multi-claim
#: call interleaves the lanes instead of draining one lane's prefix.
_BATCH_CLAIM_CHARGE_S = 60.0


def claim_share_window_s() -> float:
    """Sliding window (seconds) the lane deficit is measured over."""
    raw = os.environ.get(CLAIM_SHARE_WINDOW_ENV)
    if not raw:
        return DEFAULT_CLAIM_SHARE_WINDOW_S
    try:
        v = float(raw)
    except ValueError:
        v = 0.0
    if v <= 0:
        log.warning(
            "claim: %s=%r is not a positive number; using %gs",
            CLAIM_SHARE_WINDOW_ENV,
            raw,
            DEFAULT_CLAIM_SHARE_WINDOW_S,
        )
        return DEFAULT_CLAIM_SHARE_WINDOW_S
    return v


def lane_share_weights(conn: Connection) -> dict[str, float]:
    """``lane -> share weight`` from ``app_state`` ``claim.share.<lane>``;
    a lane without a row weighs 1.0 (equal shares)."""
    out: dict[str, float] = {}
    for key, value in conn.execute(
        "SELECT key, value FROM app_state WHERE key LIKE %s",
        (f"{_SHARE_WEIGHT_KEY_PREFIX}%",),
    ).fetchall():
        try:
            w = float(str(value))
        except ValueError:
            log.warning("claim: %s=%r is not a number; ignored", key, value)
            continue
        if w > 0:
            out[str(key)[len(_SHARE_WEIGHT_KEY_PREFIX) :]] = w
    return out


def lane_seconds_in_window(
    conn: Connection, executor: str, host: str, window_s: float
) -> dict[str, float]:
    """``lane -> lane-seconds`` this host gave each lane over the last
    ``window_s`` (gr595541). A job row counts from its ``started_at`` (the
    ``RUNNING`` tag stamp when absent) to now while running, else to the
    stamp of the status it left running for, clipped to the window. A
    yielded/reclaimed row keeps its first ``started_at``, so its queued gap
    reads as service — a small over-credit that errs toward the lane that
    already held the host."""
    key = _LANE_KEY_SQL.format(a="r2")
    rows = conn.execute(
        f"""
        SELECT {key} AS lane,
               sum(GREATEST(0, EXTRACT(EPOCH FROM (
                   LEAST(CASE WHEN st.value = %s THEN now() ELSE st.stamp END,
                         now())
                   - GREATEST(COALESCE(
                         precis_try_timestamptz(r2.meta->>'started_at'),
                         st.stamp),
                       now() - make_interval(secs => %s))))))::float8
          FROM refs r2
          CROSS JOIN LATERAL (
                SELECT t2.value, rt2.created_at AS stamp
                  FROM ref_tags rt2 JOIN tags t2 USING (tag_id)
                 WHERE rt2.ref_id = r2.ref_id AND t2.namespace = %s
                 ORDER BY rt2.created_at DESC LIMIT 1
              ) st
         WHERE r2.kind = 'job' AND r2.retired_at IS NULL
           AND r2.meta->>'executor' = %s
           AND r2.meta->>'lease_host' = %s
           AND (st.value = %s
                OR st.stamp > now() - make_interval(secs => %s))
         GROUP BY 1
        """,
        (
            RUNNING,
            window_s,
            STATUS_NAMESPACE,
            executor,
            host,
            RUNNING,
            window_s,
        ),
    ).fetchall()
    return {str(lane): float(sec or 0.0) for lane, sec in rows}


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
#: burst on the local-serving node dropped every ``gpu:1`` gate — heartbeat dark →
#: the slot map read empty — and co-claimed five cpuset-pinned NEB seeds onto one
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

#: ``meta.reclaims[].why`` for a reclaim the executor proved was a
#: re-adoption of a still-live detached compute (same boot, same process
#: start time, group alive, inside its deadline): the worker restarted,
#: the job did not crash.  Refunded from ``meta.attempts`` and left out of
#: the reclaim-churn backstop (gr541147).
RECLAIM_WHY_READOPT = "readopt"


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
    if spec is None:
        return frozenset()
    requires = spec.requires
    params = meta.get("params")
    if spec.conditional_requires is not None and isinstance(params, dict):
        requires = requires | spec.conditional_requires(params)
    return requires


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
    ``claim_executor_jobs(check_job_type_requires=True)`` — passed by
    ``coordinator`` (gr335087: a claude-less host's coordinator worker was
    claiming ``quest_tick`` and failing every tick at the LLM stage) and
    ``job_inproc`` (gr537770: a worker without ``PRECIS_UPLOAD_STAGING``
    was claiming ``paper_pull`` upload legs and failing them at the
    staged-file read). If literally no host in the fleet ever advertises a
    declared token, the sweeper's ``_alert_unschedulable_jobs`` pages instead
    of the job silently starving forever.
    """
    requires = job_type_requires(meta)
    if not requires:
        return True
    params = meta.get("params") or {}
    host = str(params.get("target_node") or default_host)
    return requires <= advertised.get(host, set())


#: Lane adapter → the ``resource_slots`` token its binary advertises
#: (gr676054). Restated rather than imported: ``job_types`` modules
#: pull this file, not the reverse — keep it in sync with
#: ``_slice_adapters.ADAPTERS`` (a test pins it).
_ADAPTER_PLACEMENT_TOKENS: dict[str, str] = {
    "claude_p": "claude_bin",
    "codex_exec": "codex_bin",
    "kimi_tree": "kimi_bin",
    "devin_tree": "devin_bin",
}


def lane_adapter_map(conn: Connection) -> dict[str, str]:
    """``{lane name: adapter}`` from ``llm_account`` — the lane→adapter
    resolution ``placement_requires`` needs to turn a ``params.lane``
    pin into its binary token (gr676054)."""
    return {
        str(name): str(adapter)
        for name, adapter in conn.execute(
            "SELECT name, adapter FROM llm_account"
        ).fetchall()
    }


def placement_requires(
    meta: dict[str, Any], lane_adapters: Mapping[str, str] | None = None
) -> frozenset[str]:
    """The job_type's ``ServiceSpec.placement`` tokens (td543917): host
    capabilities checked at claim, never reserved. Empty for a job_type with
    no spec or none declared — every job_type but the dark-factory ops.

    A ``run_slice`` job swaps the ``builder`` floor for the adapter's
    bin token when the job names its lane: ``meta.adapter`` (stamped by
    a prior submit) or a ``params.lane`` pin resolved through
    ``lane_adapters`` ({lane: adapter}, :func:`lane_adapter_map`; absent
    or unresolved → the ``builder`` floor stands).

    A ``run_slice`` / ``land_slice`` / ``repo_release`` job for a second repo
    (``params.repo`` a non-empty name other than the default) also needs
    that repo's ``repo_checkout_<name>`` token, so only a host holding a
    clone claims it. A name with no profile still adds its token: no host
    advertises it, so the job waits (fail closed)."""
    from precis.plans import lanes

    job_type = meta.get("job_type")
    spec = SERVICES_BY_NAME.get(str(job_type)) if job_type else None
    if spec is None:
        return frozenset()
    tokens = set(spec.placement)
    if str(job_type) in ("run_slice", "land_slice", "repo_release"):
        repo = (meta.get("params") or {}).get("repo")
        if isinstance(repo, str) and repo and repo != lanes.DEFAULT_REPO:
            tokens.add(lanes.repo_checkout_token(repo))
    if str(job_type) != "run_slice" or "builder" not in tokens:
        return frozenset(tokens)
    adapter = meta.get("adapter")
    if not adapter:
        lane = (meta.get("params") or {}).get("lane")
        adapter = (lane_adapters or {}).get(str(lane)) if lane else None
    tokens.discard("builder")
    tokens.add(_ADAPTER_PLACEMENT_TOKENS.get(str(adapter), "builder"))
    return frozenset(tokens)


#: Placement tokens that bind only once some live host advertises them
#: together with the rest of the job's tokens (gr701559). ``gate_image``
#: needs both gate images on the probing heartbeat's docker and is new: a
#: fleet where no host advertises it yet would leave every build and land
#: unplaceable. Until one host does, the job places on its other tokens
#: and the submit preflight (``capability_probe.gate_host_lack``) refuses
#: a host that cannot run the gate.
ROLLOUT_PLACEMENT_TOKENS = frozenset({"gate_image", "uv_bin"})


def effective_placement(
    meta: dict[str, Any],
    advertised: dict[str, set[str]],
    lane_adapters: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """The :func:`placement_requires` tokens the claim enforces against
    ``advertised`` (``host -> tokens``, live hosts): the full set when one
    host offers all of it, else the set without
    :data:`ROLLOUT_PLACEMENT_TOKENS`. The check spans the fleet, not a
    ``target_node`` pin, so once gate hosts exist a land pinned to a bare
    host waits (and pages) instead of failing its preflight on every
    claim."""
    tokens = placement_requires(meta, lane_adapters)
    rollout = tokens & ROLLOUT_PLACEMENT_TOKENS
    if not rollout or any(tokens <= have for have in advertised.values()):
        return tokens
    return tokens - rollout


def _placement_ok(
    meta: dict[str, Any],
    advertised: dict[str, set[str]],
    default_host: str,
    lane_adapters: Mapping[str, str] | None = None,
) -> bool:
    """Check-only placement gate (td543917): ``True`` when the job_type
    declares no :func:`effective_placement` tokens, or the host that would
    run it (its ``target_node`` pin, else this claiming host — the
    :func:`_coordinator_capability_ok` derivation) advertises every one.
    Applied on every claim path, unlike that opt-in gate: the static
    ``JobTypeSpec.requires`` of an ``ssh_node`` job (``has_gpaw``) is not a
    ``resource_slots`` token, so ``ssh_node`` cannot opt into
    ``check_job_type_requires`` without stranding every GPU job."""
    tokens = effective_placement(meta, advertised, lane_adapters)
    if not tokens:
        return True
    params = meta.get("params") or {}
    host = str(params.get("target_node") or default_host)
    return tokens <= advertised.get(host, set())


def advertised_by_host(conn: Connection) -> dict[str, set[str]]:
    """Public read of :func:`_advertised_by_host` — what each live host
    advertises in ``resource_slots`` (the plan tick's queued-job note)."""
    return _advertised_by_host(conn)


def placement_gap(
    meta: dict[str, Any],
    advertised: dict[str, set[str]],
    lane_adapters: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """The job's :func:`effective_placement` tokens when NO live host in
    ``advertised`` (``host -> tokens``) offers all of them at once, else
    empty. A ``target_node`` pin narrows the search to that host. Shared by
    the sweeper's unschedulable alert and the plan tick's queued note."""
    tokens = effective_placement(meta, advertised, lane_adapters)
    if not tokens:
        return frozenset()
    pin = (meta.get("params") or {}).get("target_node")
    hosts = [advertised.get(str(pin), set())] if pin else list(advertised.values())
    if any(tokens <= have for have in hosts):
        return frozenset()
    return tokens


#: Executors whose claim passes ``parent_not_paused=True`` — the parent
#: gate binds their queued jobs, so a non-terminal paused parent is a
#: real claim block worth surfacing. Keep in sync with the
#: ``claim_executor_jobs(parent_not_paused=True)`` call sites
#: (``ssh_node``, ``claude_docker``, ``devin_api``).
PARENT_GATED_EXECUTORS: tuple[str, ...] = ("ssh_node", "claude_docker", "devin_api")


def parent_pause_gaps(conn: Connection) -> dict[int, frozenset[str]]:
    """``ref_id -> the parent's blocking open tags`` for every claimable
    (``STATUS:queued``/``yielded``) job on a parent-gated executor whose
    parent is live and paused.

    The operator-visible twin of the claim gate's parent exclusion
    (gr575883): the sweeper's scheduler alert names these tags so a
    parked job says WHY it waits — the same contract ``placement_gap``
    gives a placement-blocked job. The read IS the shared
    :func:`~precis.store._todo_sql.parent_gate_exclusion_sql` predicate,
    so the alert fires exactly when (and only when) the claim gate
    blocks; a terminal parent's stale tags appear in no value, and
    neither does the wait a job itself resolves (gr738684)."""
    rows = conn.execute(
        f"""
        SELECT r.ref_id, pt.value
          FROM refs r
          JOIN ref_tags prt ON prt.ref_id = r.parent_id
          JOIN tags pt ON pt.tag_id = prt.tag_id
         WHERE r.kind = 'job' AND r.retired_at IS NULL
           AND r.meta->>'executor' = ANY(%s)
           AND pt.namespace = 'OPEN'
           AND {_doable_exclusion_clause("pt", "prt")}
           AND {not_served_wait_sql("pt", "r")}
           AND {parent_gate_exclusion_sql("r")}
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %s AND t.value = ANY(%s)
               )
        """,
        (
            list(PARENT_GATED_EXECUTORS),
            sorted(TERMINAL_TODO_STATUSES),
            STATUS_NAMESPACE,
            list(CLAIMABLE),
        ),
    ).fetchall()
    gaps: dict[int, set[str]] = {}
    for ref_id, value in rows:
        gaps.setdefault(int(ref_id), set()).add(str(value))
    return {ref_id: frozenset(v) for ref_id, v in gaps.items()}


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


def dark_advertised_by_host(conn: Connection) -> dict[str, set[str]]:
    """``host -> {resources it last advertised}`` for hosts whose
    ``host_heartbeat`` is stale — the dark complement of
    :func:`_advertised_by_host` (gr681494).

    A dark host's ``resource_slots`` rows are fossil, not retracted — only
    a *live* heartbeat pass retracts them — so they still describe what the
    host would offer were it alive. ``plan_dispatch._step_unplaced`` reads
    the map to collapse its "job unplaceable — provision a host?" asks: a
    job whose placement tokens fit only a dark host waits on the ONE
    ``host-dark`` alert instead of fanning out a per-job ask.

    The heartbeat predicate mirrors ``nursery._detect_host_dark`` exactly
    (stale ``ts``, not ``meta.retired``, not ``meta.ephemeral``, not a
    12-hex container id, not an empty-probe dev checkout): an ask is only
    safe to suppress for a host that alert will actually page. That
    includes its coverage cap — the detector pages the 50 stalest hosts
    (``ORDER BY ts, host LIMIT 50``), so this map holds the same 50
    (td681892): suppressing the ask of a host past the cap would hide a
    gap no alert names. The ``host`` tie-break keeps the two windows
    identical on equal timestamps — without it Postgres could return
    different boundary rows for each query, suppressing an ask for a
    host the alert never named.
    """
    rows = conn.execute(
        "SELECT rs.host, rs.resource FROM resource_slots rs "
        "JOIN ("
        "SELECT hh.host FROM host_heartbeat hh "
        "WHERE hh.ts < now() - %(silence)s::interval "
        "AND (hh.meta->>'ephemeral') IS DISTINCT FROM 'true' "
        "AND (hh.meta->>'retired') IS NULL "
        "AND hh.host !~ '^[0-9a-f]{12}$' "
        "AND NOT ("
        "hh.meta ? 'worker_units' "
        "AND (hh.meta->'worker_units' = '{}'::jsonb "
        "OR hh.meta->'worker_units'->'units' = '{}'::jsonb) "
        "AND COALESCE(hh.meta->'activity', '{}'::jsonb) = '{}'::jsonb"
        ") "
        "ORDER BY hh.ts ASC, hh.host LIMIT 50"
        ") hh ON hh.host = rs.host",
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


def _plan_reader(conn: Connection) -> Any:
    """A ``store``-shaped borrower for plan_dispatch's reads: they run
    inside the claim transaction, so earlier claims in a batch are
    visible as holders too."""
    return SimpleNamespace(pool=SimpleNamespace(connection=lambda: nullcontext(conn)))


def _queued_build_plan(
    conn: Connection, parent_ids: list[int] | None
) -> tuple[Any | None, set[int]]:
    """``(_Plan, wanted)`` — the in-memory plans above the queued
    ``run_slice`` parents (every queued build's when ``parent_ids`` is
    ``None``); ``plan`` is ``None`` when no live plan root covers them,
    with ``wanted`` still resolved for the fallback read. One load,
    shared by the overlap and sequencing holds — it covers only the
    plan roots above those queued builds, not every plan subtree."""
    from precis.workers.job_types import plan_dispatch as pd

    if parent_ids is None:
        parent_ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT DISTINCT parent_id FROM refs j"
                " WHERE j.kind = 'job' AND j.retired_at IS NULL"
                " AND j.parent_id IS NOT NULL"
                " AND j.meta->>'job_type' = 'run_slice'"
                " AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)"
                "  WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS'"
                "  AND t.value = 'queued')"
            ).fetchall()
        ]
    if not parent_ids:
        return None, set()
    ancestors = {
        int(r[0])
        for r in conn.execute(
            "WITH RECURSIVE up(ref_id, parent_id) AS ("
            " SELECT ref_id, parent_id FROM refs WHERE ref_id = ANY(%s)"
            " UNION SELECT r.ref_id, r.parent_id FROM refs r"
            "  JOIN up ON r.ref_id = up.parent_id)"
            " SELECT ref_id FROM up",
            (parent_ids,),
        ).fetchall()
    }
    roots = [r for r in pd._candidate_roots(conn) if r in ancestors]
    if not roots:
        return None, set(parent_ids)
    return pd._load(_plan_reader(conn), roots), set(parent_ids)


def build_overlap_holds(
    conn: Connection,
    parent_ids: list[int] | None = None,
    *,
    shared: tuple[Any | None, set[int]] | None = None,
) -> set[int]:
    """Build todo ids held by started work in the claim transaction."""
    return set(_build_overlap_details(conn, parent_ids, shared=shared))


def _build_overlap_details(
    conn: Connection,
    parent_ids: list[int] | None = None,
    *,
    shared: tuple[Any | None, set[int]] | None = None,
) -> dict[int, tuple[int, frozenset[str]]]:
    """Build todo ids held by started work, read in the claim transaction.

    Reusing the planner's file and repo resolution keeps pending pre-land
    reviews and branch diffs in the same exclusion set. The reader borrows
    this connection without committing it, so earlier claims in a batch
    are visible as holders too. Cheap on the claim path: no started build
    or no queued ``run_slice`` job (or none in ``parent_ids``) returns
    before any plan load, and the load covers only the plan roots above
    those queued builds, not every plan subtree. ``shared`` is an
    already-loaded :func:`_queued_build_plan` result on this conn — the
    claim pass loads once and hands it to both hold reads (gr656970
    review).
    """
    from precis.workers.job_types import plan_dispatch as pd

    reader = _plan_reader(conn)
    started = pd._started_slices(reader)
    if not started:
        return {}
    plan, wanted = shared or _queued_build_plan(conn, parent_ids)
    if plan is None:
        return {}
    return pd._overlap_holds(
        plan, reader, started, [n for n in plan.todos() if n.id in wanted]
    )


def _blocked_parents_sql(conn: Connection, parent_ids: list[int]) -> set[int]:
    """``parent_ids`` — or an ancestor of one — carrying a ``blocked-by``
    link to a live target whose STATUS is not blocker-terminal. The
    plain sequencing read for a build parent no loaded plan covers (the
    land-held nuance needs the plan model, so it is the fallback only)."""
    if not parent_ids:
        return set()
    rows = conn.execute(
        f"""
        WITH RECURSIVE up(origin, ref_id, parent_id) AS (
            SELECT ref_id, ref_id, parent_id FROM refs
             WHERE ref_id = ANY(%s)
            UNION
            SELECT up.origin, r.ref_id, r.parent_id FROM refs r
              JOIN up ON r.ref_id = up.parent_id
        )
        SELECT DISTINCT origin FROM up
         WHERE EXISTS (
            SELECT 1 FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
             WHERE l.src_ref_id = up.ref_id AND l.relation = 'blocked-by'
               AND b.retired_at IS NULL
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt
                        JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'open'
                   ) NOT IN {blocker_terminal_sql()})
        """,
        (parent_ids,),
    ).fetchall()
    return {int(r[0]) for r in rows}


def source_gripe_holds(conn: Connection, parent_ids: list[int]) -> dict[int, str]:
    """Name one live source-gripe blocker per held build parent.

    The source sits outside the plan: ``templates._run`` hangs it off a
    ``related-to`` edge from the root, so neither ``plan.blocked`` nor
    the ancestors read above reaches it — a blocker linked on the gripe
    after the chain minted is rechecked here instead, on the parent and
    every ancestor (gr656970 review)."""
    if not parent_ids:
        return {}
    rows = conn.execute(
        f"""
        WITH RECURSIVE up(origin, ref_id, parent_id) AS (
            SELECT ref_id, ref_id, parent_id FROM refs
             WHERE ref_id = ANY(%s)
            UNION
            SELECT up.origin, r.ref_id, r.parent_id FROM refs r
              JOIN up ON r.ref_id = up.parent_id
        )
        SELECT DISTINCT ON (up.origin)
               up.origin, g.ref_id, b.ref_id, b.kind, bs.status
          FROM up
          JOIN links rel ON rel.src_ref_id = up.ref_id
                        AND rel.relation = 'related-to'
          JOIN refs g ON g.ref_id = rel.dst_ref_id
          JOIN links l ON l.src_ref_id = g.ref_id AND l.relation = 'blocked-by'
          JOIN refs b ON b.ref_id = l.dst_ref_id
          CROSS JOIN LATERAL (
              SELECT COALESCE(
                  (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                    WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS'
                    LIMIT 1), 'open') AS status
          ) bs
         WHERE g.kind = 'gripe' AND g.retired_at IS NULL
           AND b.retired_at IS NULL AND bs.status NOT IN {blocker_terminal_sql()}
         ORDER BY up.origin, g.ref_id, b.ref_id
        """,
        (parent_ids,),
    ).fetchall()
    return {
        int(parent): (
            f"source gripe gr{source} is blocked-by "
            f"{handle_registry.try_format(kind, blocker) or f'{kind}:{blocker}'} "
            f"({status})"
        )
        for parent, source, blocker, kind, status in rows
    }


def source_todo_holds(
    conn: Connection, parent_ids: list[int], *, blockers_only: bool = False
) -> dict[int, str]:
    """Name one reason per build parent whose chain's source todo holds it.

    The source of a todo chain is the todo named by ``meta.source_todo``
    on the plan root (the parent or an ancestor), read here after mint —
    the chain inventory's validation, not metadata alone: the meta-carrying
    node must be a live plan root with a ``related-to`` edge to the todo
    its key names. The source holds the build while it carries an open
    ``blocked-by``, one of the dispatcher's hold tags, ``needs-spec`` or
    ``factory:skip``, no longer carries ``factory:build``, or is terminal
    or retired. When the source is a split part (``meta.todo_split_part``)
    the blocker read is stricter: the part stays held until its
    predecessor is live and ``done`` — a dropped or retired one holds too.
    ``blockers_only`` keeps the open-blocker reason alone: the
    stalled-chain census shields a chain only for a blocker — a withdrawn
    tag is a stall to ask about."""
    if not parent_ids:
        return {}
    from precis.plans.todo_chain import (
        FACTORY_BUILD_TAG,
        NEEDS_SPEC_TAG,
        SKIP_TAG,
        SOURCE_TODO_KEY,
        SPLIT_PART_KEY,
    )
    from precis.workers.gripe_dispatch import _TERMINAL_STATUSES, HOLD_TAGS

    rows = conn.execute(
        f"""
        WITH RECURSIVE up(origin, ref_id, parent_id) AS (
            SELECT ref_id, ref_id, parent_id FROM refs
             WHERE ref_id = ANY(%s)
            UNION
            SELECT up.origin, r.ref_id, r.parent_id FROM refs r
              JOIN up ON r.ref_id = up.parent_id
        )
        SELECT DISTINCT ON (up.origin)
               up.origin, g.ref_id, g.retired_at IS NOT NULL,
               (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                 WHERE rt.ref_id = g.ref_id AND t.namespace = 'STATUS'
                 LIMIT 1),
               EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                        WHERE rt.ref_id = g.ref_id
                          AND t.namespace IN ('OPEN', 'FLAG')
                          AND t.value = %s),
               (SELECT min(t.value) FROM ref_tags rt JOIN tags t USING (tag_id)
                 WHERE rt.ref_id = g.ref_id AND t.namespace IN ('OPEN', 'FLAG')
                   AND t.value = ANY(%s)),
               bl.ref_id, bl.kind, bl.status
          FROM up
          JOIN refs u ON u.ref_id = up.ref_id
                     AND u.kind = 'todo' AND u.retired_at IS NULL
                     AND EXISTS (SELECT 1 FROM ref_tags put JOIN tags pn
                                           ON pn.tag_id = put.tag_id
                                  WHERE put.ref_id = u.ref_id
                                    AND pn.namespace = 'OPEN'
                                    AND pn.value = 'plan:' || u.ref_id)
                     AND NOT EXISTS (SELECT 1 FROM ref_tags ust JOIN tags usn
                                             ON usn.tag_id = ust.tag_id
                                      WHERE ust.ref_id = u.ref_id
                                        AND usn.namespace = 'STATUS'
                                        AND usn.value = ANY(%s))
          JOIN refs g ON g.kind = 'todo'
                     AND g.ref_id = CASE
                         WHEN u.meta->>%s ~ '^[0-9]{{1,15}}$'
                         THEN (u.meta->>%s)::bigint END
                     AND EXISTS (SELECT 1 FROM links sl
                                  WHERE sl.src_ref_id = u.ref_id
                                    AND sl.relation = 'related-to'
                                    AND sl.dst_ref_id = g.ref_id)
          LEFT JOIN LATERAL (
              SELECT b.ref_id, b.kind, bs.status
                FROM links l
                JOIN refs b ON b.ref_id = l.dst_ref_id
                CROSS JOIN LATERAL (
                    SELECT COALESCE(
                        (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                          WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS'
                          LIMIT 1), 'open') AS status
                ) bs
               WHERE l.src_ref_id = g.ref_id AND l.relation = 'blocked-by'
                 AND ((g.meta ? %s
                         AND (b.retired_at IS NOT NULL OR bs.status <> 'done'))
                      OR (NOT (g.meta ? %s)
                          AND b.retired_at IS NULL
                          AND bs.status NOT IN {blocker_terminal_sql()}))
               ORDER BY b.ref_id LIMIT 1
          ) bl ON TRUE
         ORDER BY up.origin, g.ref_id
        """,
        (
            parent_ids,
            FACTORY_BUILD_TAG,
            [*HOLD_TAGS, NEEDS_SPEC_TAG, SKIP_TAG],
            list(_TERMINAL_STATUSES),
            SOURCE_TODO_KEY,
            SOURCE_TODO_KEY,
            SPLIT_PART_KEY,
            SPLIT_PART_KEY,
        ),
    ).fetchall()
    held: dict[int, str] = {}
    for parent, source, retired, status, tagged, hold, blocker, kind, bstatus in rows:
        reason = None
        if not blockers_only:
            if retired or status in TERMINAL_TODO_STATUSES:
                reason = f"source todo td{source} is {'retired' if retired else status}"
            elif not tagged:
                reason = f"source todo td{source} no longer carries {FACTORY_BUILD_TAG}"
            elif hold is not None:
                reason = f"source todo td{source} carries hold tag {hold}"
        if reason is None and blocker is not None:
            reason = (
                f"source todo td{source} is blocked-by "
                f"{handle_registry.try_format(kind, blocker) or f'{kind}:{blocker}'} "
                f"({bstatus})"
            )
        if reason is not None:
            held[int(parent)] = reason
    return held


def source_holds(conn: Connection, parent_ids: list[int]) -> dict[int, str]:
    """:func:`source_gripe_holds` and :func:`source_todo_holds` together:
    one reason per build parent whose gripe or todo source holds it."""
    return {
        **source_todo_holds(conn, parent_ids),
        **source_gripe_holds(conn, parent_ids),
    }


def build_blocked_holds(
    conn: Connection,
    parent_ids: list[int] | None = None,
    *,
    shared: tuple[Any | None, set[int]] | None = None,
) -> dict[int, str]:
    """Queued ``run_slice`` parents a ``blocked-by`` edge still holds,
    read in the claim transaction (gr656970 review).

    Dispatch's ``_Plan.blocked`` check decides at mint time; a blocker
    linked AFTER the job queued never passed it, so the claim re-runs
    the same read — the build stays queued while any blocked-by target
    on the todo or an ancestor is live, while a ``done`` blocker's
    land is still held (the gr659981 hold dispatch applies), and while
    the chain's source gripe — a ``related-to`` hop off the ancestor
    chain, outside the plan model — carries an open blocked-by of its
    own (:func:`source_gripe_holds`), or its source todo holds it
    (:func:`source_todo_holds`). A parent no live plan covers — a
    ``run_slice`` minted outside plan_dispatch — falls back to the
    direct ancestors read, which lacks the land nuance but still
    honours the edge. ``shared`` is the claim pass's already-loaded
    :func:`_queued_build_plan` result — one load feeds both holds.
    """
    plan, wanted = shared or _queued_build_plan(conn, parent_ids)
    covered = {n.id for n in plan.todos()} if plan is not None else set()
    held = (
        {n.id for n in plan.todos() if n.id in wanted and plan.blocked(n)}
        if plan is not None
        else set()
    )
    uncovered = wanted - covered
    if uncovered:
        held |= _blocked_parents_sql(conn, sorted(uncovered))
    return {
        **dict.fromkeys(held, "build todo or its ancestor has a blocked-by hold"),
        **source_holds(conn, sorted(wanted)),
    }


def queued_build_hold_reasons(conn: Connection) -> dict[int, str]:
    """The claim pass's sequencing and overlap holds for the queue view."""
    shared = _queued_build_plan(conn, None)
    if not shared[1]:
        return {}
    return {
        **{
            parent: f"files overlap td{holder}"
            for parent, (holder, _) in _build_overlap_details(
                conn, shared=shared
            ).items()
        },
        **build_blocked_holds(conn, shared=shared),
    }


def _factory_pref_sql(class_rank: bool) -> str:
    if not class_rank:
        return "0"
    return f"CASE WHEN r.meta->>'job_type' IN {_FACTORY_TYPES_SQL} THEN 0 ELSE 1 END"


# Keep pre-stamp lands at the same score as newly minted lands.
_DISPATCH_SCORE_SQL = (
    f"COALESCE((r.meta->>'{_dscore.SCORE_META_KEY}')::float8,"
    f" CASE WHEN r.meta->>'job_type' = 'land_slice'"
    f" THEN {_dscore.FINISH_FIRST_SCORE} ELSE 0 END)"
)

_CLAIM_ORDER_PREFIX_SQL = (
    "claim_rank ASC, factory_pref ASC, aged_past_class_age DESC, dispatch_score DESC"
)
_CLAIM_AGE_BUCKET_SQL = "b.age_s > %s"

_BUILD_HOLD_FILTER_SQL = (
    " AND (COALESCE({alias}.meta->>'job_type', '') <> 'run_slice'"
    " OR {alias}.parent_id IS NULL OR {alias}.parent_id <> ALL(%s))"
)


def _claim_order_sql(
    age_step_s: float,
    class_age_s: float,
    *,
    class_rank: bool = True,
    release_freeze: bool = False,
) -> tuple[str, str, tuple[Any, ...]]:
    """The D2/D3 claim order: ``(claim_rank_sql, aged_prio_sql, params)``.

    The expressions read ``b.age_s`` / ``b.base_class`` from the LATERALs
    in :func:`claim_executor_jobs` (queued-tag age and base origin class,
    computed once per row — :data:`_CLASS_RANK_CASE`; missing origin
    reads as the old cron band, 2, at prio <= 2, else background 3;
    :data:`RELEASE_LEG_JOB_TYPES` read as the release band, 2.5).
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

    ``release_freeze=True`` (gr584136) adds the lane cap to the
    promotion floor: while any release leg is claimable
    (``t.has_release``), every row's floor is its own base class — no
    lower class promotes, so an aged background backlog can neither
    reach the release band (tie it) nor cross it toward recurring.
    It requires the surrounding ``t`` LATERAL to select
    ``bool_or(base = 2.5) AS has_release``; a caller whose ``t`` lateral
    predates the band must pass nothing — release rows still self-pin
    at 2.5, only the freeze term drops.
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
    #: A release-band row is self-pinned (``base = 2.5`` → its floor is its
    #: own rank — emitted unconditionally since it only reads
    #: ``b.base_class``): it never class-promotes, so it can never outrank
    #: recurring. ``release_freeze`` adds ``t.has_release``: while a
    #: release leg is claimable every row's floor is its own base — no
    #: lower class promotes into or across the band.
    _cap_sql = (
        f"CASE WHEN b.base_class = {_RELEASE_LEG_RANK} THEN b.base_class ELSE 0 END"
    )
    if release_freeze:
        _cap_sql += ", CASE WHEN t.has_release THEN b.base_class ELSE 0 END"
    _eff_floor = (
        "LEAST(b.base_class, GREATEST(COALESCE(t.top_class, b.base_class),"
        f" 2, {_cap_sql}))"
    )
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
    reclaim_counts_toward_limit: bool = False,
    exclude_job_types: frozenset[str] = frozenset(),
    include_job_types: tuple[str, ...] | None = None,
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
    Opt out with ``reclaim_counts_toward_limit=True`` (claude_inproc's
    slot-boundary claim, gr551639): then the reclaims consume part of
    ``limit`` — the fresh-work pass below gets only the remainder, so
    the WHOLE call returns at most ``limit`` rows (one per free pool
    slot — an over-return would stamp a job's lease with no slot to
    start it).

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

    ``meta.last_claimed_at`` is the every-claim twin: re-stamped on EVERY
    successful claim — fresh, re-queued (a yield → wake → re-claim keeps
    the first ``started_at`` and appends no ``reclaims``) or a running-row
    reclaim — so the newest claim instant is always durable
    (gr625325 review; ``factory_rescue._queue_moved_at`` reads it as the
    queue-drain clock).

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

    **Background share (td656098).** The order never lets a background
    row beat agent work, so a saturated agent queue would starve it:
    about one pick in :data:`_BG_SHARE_EVERY` goes to the oldest queued
    background row instead, unless a human-class or prio-1 row waits.
    The share is also the bulk-progress floor under the factory term
    (gr657333 review, td657493): it ignores ``factory_pref`` on purpose —
    a factory-first share under a continuous factory stream would starve
    the bulk backlog it exists to protect, while a factory op loses the
    reserved pick to an older bulk row and still claims on the ordinary
    order.

    **Claim ordering (slice 6a, FIFO fix 2026-09-25, ageing 2026-10-15;
    class rank 2026-10-23, job-scheduling-fairness D2/D3; share term
    2026-10-24, D4 share half; release band 2026-11-07, gr584136;
    factory sub-priority 2026-11-13, gr657333; model affinity
    2026-11-13, gr595542; lane deficit, gr595541; dispatch_score,
    gr702610).**
    ``ORDER BY claim_rank ASC, factory_pref ASC, aged_past_class_age DESC,
    dispatch_score DESC, lane_deficit DESC, aged_prio ASC, queued_since ASC,
    ref_id ASC``
    (the age bucket puts jobs past :func:`claim_class_age_s` ahead of
    fresher peers in the same class and factory band, gr736421;
    ``dispatch_score`` = the ``meta.dispatch_score`` stamp, hot-first;
    an unstamped ``land_slice`` floors at
    :data:`~precis.utils.dispatch_score.FINISH_FIRST_SCORE` — see
    ``_DISPATCH_SCORE_SQL``; lane deficit = the lane's weighted share — its
    ``claim.share.<lane>``
    weight over the eligible lanes' weights — minus the fraction of the
    sliding window's lane-seconds this host gave it, gr595541; the same
    deficit orders the pre-LIMIT window so truncation can't exclude the
    owed lane, td655978)
    where ``claim_rank`` is the row's class (human 0 < agent 1 <
    recurring 2 < release 2.5 < background 3 — the release band from
    :data:`RELEASE_LEG_JOB_TYPES` ``job_type``s, not ``origin``; a
    missing origin reads as the old cron
    band at prio <= 2, else background) stepped down one class per
    :func:`claim_class_age_s` of wait until it ties the top class
    queued, ``-1`` for a prio-1 preempt (absolute across classes),
    ``factory_pref`` is 0 for a :data:`FACTORY_PIPELINE_JOB_TYPES` row
    (within a class, the dark-factory ops outrank bulk seed/drain work —
    the bulk side's progress floor is the oldest-first background share
    above, so a factory stream defers bulk but cannot starve it)
    and 1 otherwise, and
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

    The Python re-rank over the locked candidates adds, in key order:
    capability scarcity (6d, ``-scarcity`` first, ahead of the SQL's own
    ``claim_rank``/``factory_pref`` order the key then mirrors), the D4
    share half
    (the lane deficit, re-read per pick as each claim charges its lane
    ``_BATCH_CLAIM_CHARGE_S`` so a batch round-robins), and —
    gr595542 — **model affinity**, a tiebreak between ``aged_prio`` and
    ``queued_since``: a row whose effective model (``params.model`` else
    the job_type's declared default) matches the
    reservation host's fresh last-used-model stamp
    (:func:`~precis.workers.footprint_admission.last_models`, stamped by
    :func:`~precis.workers.footprint_admission.note_model_use` at claim
    time) sorts ahead of rows the fairness keys tied with it. It never
    overrides class, share, prio or age — warm weights win the reload
    race only among equals, and the stamp decays after
    ``PRECIS_MODEL_AFFINITY_TTL_MIN`` (default 30 min) so a cold host
    steers nothing.

    **Reserve-at-claim (slices 6c/6d; footprint admission gr595542).** A
    job's resource requirements — explicit ``meta.requires`` or *derived*
    from its ``job_type`` via the registry (:func:`effective_requires`;
    ``struct_relax``/``fold`` → ``{"gpu": 1}``) — are reserved in this
    transaction before the job is handed back: the conditional decrement
    is the lock. The reservation set is merged with the footprint units
    :func:`precis.workers.footprint_admission.admission_units` computes —
    a GPU job whose *effective* model (``params.model``, else the
    job_type's declared default — ``struct_relax`` → ``mace_mp``)
    reserves the model's learned ``vram_mb`` (footprint + margin) and a
    science-lane (``ssh_node``) job with enough measured runs reserves
    its predicted peak RSS as ``mem_mb`` — one all-or-nothing decrement,
    so a job never holds its ``gpu`` token without its VRAM headroom.
    Right after the decrement (refunding it on a veto),
    :func:`~precis.workers.footprint_admission.admission_block` vets the
    units against live state the static ledger can't see (td655172): the
    footprint must fit the host's largest single card (``vram_card_mb`` —
    the host-wide sum would admit models no card can hold), and the
    host's latest measured *admittable* free headroom
    (``host_heartbeat.meta``'s ``vram_free_mb``/``mem_free_mb``, per beat
    — available less the measured host's own reserve, the same
    subtraction the static rows carry, td655171) must cover the units less
    the still-pending holds the beat can't have captured, so untracked
    consumers (LLM serving, interactive use) can't over-admit or spend
    the reserve. Holds count in
    full until their reservations release: the measured free is
    aggregate, so a drop can't be attributed to the reserved job over an
    untracked consumer — releasing on drops could free a hold whose job
    never allocated (td655171 review), and the residual under-admission
    is the safe direction.
    When the beat carries the per-card list (``vram_free_cards_mb`` —
    raw free per card) the
    vetoes become a pick: the claim admits onto one card with headroom
    under its concurrency cap, still net of the host's
    ``PRECIS_HOST_VRAM_RESERVE_MB`` reserve off the card under test —
    read from the beat's own ``vram_reserve_mb`` stamp, since the
    reservation host can be remote (``target_node``) and the claimer's
    env would subtract the wrong host's (td655172 review) — and stamps
    ``meta.reserved.gpu_card``, so
    the dispatch binds execution to the admitted card rather than
    exposing every card.
    Both dimensions are dark until the footprint data and the host's
    capacity rows exist; a claim that can't fit them is dropped this
    cycle the same way a full token slot drops it. The reservation
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

    A successful claim whose job has an effective model (explicit or by
    job_type default) also stamps the reservation host's last-used model
    (:func:`~precis.workers.footprint_admission.note_model_use`) — the
    write half of the pull's model-affinity sort key (gr595542; see
    "Claim ordering" below).

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
    is True (``coordinator``, ``job_inproc``), a row whose job_type declares a
    :func:`job_type_requires` token not advertised on the host that would run
    it (its ``target_node`` pin, else this claiming host) is dropped from
    THIS claim via :func:`_coordinator_capability_ok` — check-only, no
    reservation (see that function's docstring for why the reservation path
    above is unsafe for a Yield-heavy coordinator job_type like
    ``quest_tick``). Off by default: every other caller is unaffected.

    **Process-local exclusion (gr623024).** ``exclude_job_types`` is the
    claiming PROCESS's own "cannot run" list (``job_inproc`` passes the
    job_types whose needed capability, e.g. ``repo_checkout``, this process
    lacks). The host-level gate above reads ``resource_slots``, which is
    keyed per HOST: a sibling worker process with the capability makes the
    host advertise it for every process on it. The names join the operator
    suspend list in the candidate SQL, so the row stays queued, untouched,
    for a capable process — never claimed-then-released.

    **Placement gate (td543917).** Always on: a row whose job_type's
    ``ServiceSpec.placement`` (:func:`placement_requires`) names a token the
    running host does not advertise is dropped the same way
    (:func:`_placement_ok`) — how the ``ssh_node`` dark-factory ops
    (``run_slice``, ``land_slice``, ``release_*``) reach a host with the
    repo checkout, ``claude`` and ``git``.

    **Executor lane cap + class reservation (gr603792).** When
    :func:`executor_lane_limits` resolves a running cap for this executor
    (``ssh_node`` only, code default 8; ``claim.executor_cap.<executor>``
    overrides), the fresh-work pick loop counts every
    ``STATUS:running`` row stamped with this ``lease_host`` and admits a
    candidate of type ``jt`` only while
    ``running_total < cap - (reserve_total - reserve[jt])`` —
    a non-reserved type stops at ``cap - reserve_total``, a reserved type
    may also fill its own allotment (``claim.reserve.<executor>``, a JSON
    ``{job_type: slots}`` map, code default
    ``{"run_slice": 1, "land_slice": 1}`` on ``ssh_node``). A deep science
    queue therefore can never hold every executor slot against a factory
    op, and the factory allotment is small enough that the science class
    keeps its band. Reclaims of already-running rows are unaffected —
    they continue a running row, they don't take a fresh slot. The
    count+claim pair is serialized per ``(executor, lease_host)`` by a
    ``pg_try_advisory_xact_lock`` — the loser skips fresh claims for the
    pass (same rule as the per-type caps) instead of reading the same
    occupancy and over-claiming into the reserved slots. Types already
    blocked by the reservation at count time are excluded from the
    candidate query itself, so a blocked backlog can't fill the
    ``limit × _CLAIM_OVERFETCH`` window and hide a reserved-class job
    queued behind it (ssh_node's ``limit=1`` fetches three candidates).

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
    compute until the owner unblocks it. The gate reads the shared
    :func:`~precis.store._todo_sql.parent_gate_exclusion_sql` predicate,
    and a TERMINAL parent (``done`` / ``won't-do`` / ``auto-timeout``)
    is exempt: a finished todo's stale pause tags (a leftover
    ``waiting-for:fleet``) must not pin its queued jobs forever
    (gr575883). So is the one ``waiting-for:`` tag a job declares it
    resolves (``meta.serves_parent_wait`` — the ``ci_rerun`` a parked
    release fix waits for, gr738684).
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
    parent_params: tuple[Any, ...] = ()
    if parent_not_paused:
        # gr575883: the gate exists so a paused LIVE project doesn't burn
        # compute — a terminal parent (done / won't-do / auto-timeout) is
        # finished, so its stale pause tags (a leftover
        # ``waiting-for:fleet``) must not pin the parent's queued jobs
        # forever. The shared predicate carries the terminal carve-out.
        parent_sql = f"""
           AND NOT {parent_gate_exclusion_sql("r")}"""
        parent_params = (sorted(TERMINAL_TODO_STATUSES),)

    # Operator hold switch (``PRECIS_SUSPENDED_JOB_TYPES``): a suspended
    # job_type is never claimed — fresh or reclaimed — so its queued rows
    # wait in place; in-flight rows keep polling/finishing normally. The
    # COALESCE keeps a job with no ``meta.job_type`` claimable (it can't
    # match any suspended name).
    suspended = suspended_job_types() | exclude_job_types
    suspend_sql = ""
    suspend_params: tuple[list[str], ...] = ()
    if suspended:
        suspend_sql = """
           AND COALESCE(r.meta->>'job_type', '') <> ALL(%s)"""
        suspend_params = (sorted(suspended),)

    # Restrict both fresh claims and stale reclaims on dedicated lanes.
    if include_job_types is not None:
        suspend_sql += " AND r.meta->>'job_type' = ANY(%s)"
        suspend_params += (list(include_job_types),)

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
    # Footprint-admission lookup cache, one per claim pass: dedupes the
    # model-VRAM and predicted-RSS reads across the pick loop's candidates
    # (gr595542 — same key asked again for the next row hits the dict).
    fp_cache: dict[tuple[Any, ...], Any] = {}
    # The lane→adapter map, read once per claim pass and only when a
    # lane-named run_slice row actually reaches the placement gate
    # (gr676054) — most passes carry no such row.
    _lane_adapters_cache: dict[str, str] | None = None

    def lane_adapters() -> dict[str, str]:
        nonlocal _lane_adapters_cache
        if _lane_adapters_cache is None:
            try:
                _lane_adapters_cache = lane_adapter_map(conn)
            except Exception:
                # The map is a refinement, not a gate: an unreadable lane
                # table falls back to the ``builder`` floor — same as a
                # lane pin that resolves to nothing.
                log.warning("claim: lane→adapter map read failed", exc_info=True)
                _lane_adapters_cache = {}
        return _lane_adapters_cache

    def lane_adapters_for(meta: dict[str, Any]) -> dict[str, str] | None:
        """The lane→adapter map, only when THIS row's placement consults
        it — a ``run_slice`` carrying ``params.lane`` and no stamped
        ``meta.adapter`` (the adapter stamp resolves the token without
        the table, and an unpinned row takes the ``builder`` floor)."""
        if meta.get("job_type") != "run_slice" or meta.get("adapter"):
            return None
        if not (meta.get("params") or {}).get("lane"):
            return None
        return lane_adapters()

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

        now_iso = datetime.now(UTC).isoformat()
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
            "started_at": meta.get("started_at") or now_iso,
            # Wall-clock instant of THIS claim — re-stamped on EVERY
            # successful claim, unlike ``started_at`` (gr625325 review).
            # A re-claim from ``queued``/``yielded`` — the wake_runner's
            # re-queue keeps the first ``started_at`` and drops only
            # ``lease_until`` — appends no ``reclaims`` entry either, so
            # without this key a second claim leaves no durable mark and
            # a queue that just drained reads as stalled to
            # ``factory_rescue._queue_moved_at``.
            "last_claimed_at": now_iso,
        }
        if reclaim_stale_running and status_value == RUNNING:
            why = _reclaim_reason(meta, advertised_boot_ids)
            attempts = int(meta.get("attempts") or 0)
            attempts += 1
            reclaims = list(meta.get("reclaims") or [])
            reclaims.append({"at": now_iso, "why": why})
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
        meta["last_claimed_at"] = fields["last_claimed_at"]
        # gr595536: open the run's footprint segment — the dispatch
        # thread re-baselines it at bind() and the terminal STATUS stamp
        # reads it into ``meta.footprint``.
        job_footprint.begin(ref_id)

    def _finish_claim(
        ref_id: int,
        title: str,
        meta: dict[str, Any],
        status_value: str | None,
        created_at: datetime | None = None,
    ) -> tuple[int, str, dict[str, Any]] | None:
        """Reserve resources (if any) + stamp lease identity for one row
        already locked by the caller's ``FOR UPDATE`` select. Returns the
        claimable tuple, or ``None`` if a live resource slot is full —
        counted token or gr595542 footprint headroom, the decrement is
        all-or-nothing over the merged set — / the reservation host is
        memory-pressured / the row is still within its
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
        if meta.get("job_type") == "run_slice" and status_value != RUNNING:
            # The claim stamps no STATUS:running (dispatch does, after the
            # batch), so a second build picked in this batch could not see
            # the first as a holder: one fresh build per claim batch. Cheap
            # first — it saves the lock and the overlap read below.
            if fresh_builds:
                return None
            # Serialize overlap read + running stamp across hosts. A loser
            # leaves work queued; no lease or resource reservation was made.
            locked = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)::bigint)",
                ("factory:build-overlap",),
            ).fetchone()
            if not (locked and locked[0]):
                return None
            parent = conn.execute(
                "SELECT parent_id FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()
            shared = _shared_build_plan()
            if parent and parent[0] in (
                build_overlap_holds(conn, shared=shared)
                # gr656970: a blocked-by edge minted after the job queued
                # holds it here too — dispatch's check saw it once, the
                # claim re-runs it so a late blocker still sequences,
                # on the todo's ancestors and on the source gripe the
                # chain is related-to.
                | build_blocked_holds(conn, shared=shared).keys()
            ):
                return None
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
        if not _placement_ok(meta, advertised, default_host, lane_adapters_for(meta)):
            # Placement gate (td543917): a dark-factory op on a host without
            # its checkout / lane binary / git stays queued for a capable host.
            log.debug(
                "claim: skip job %s on %s — placement %s not advertised",
                ref_id,
                default_host,
                sorted(effective_placement(meta, advertised, lane_adapters_for(meta))),
            )
            return None
        if reclaim_stale_running and meta.get("reserved"):
            # A stolen (crash-recovered) job still carries the dead worker's
            # reservation — refund it before this claim re-reserves, so slots
            # don't leak on the reserving host. No-op for fresh queued work.
            release_job_reservation(conn, ref_id)
            meta.pop("reserved", None)
        if meta.get("job_type") == "run_slice" and status_value != RUNNING:
            from precis.workers.admission import launch_fits
            from precis.workers.job_types.run_slice import work_footprint

            # Launch runs locally, even if params carry a target_node.
            # Check the exact spawn ledger, not learned RSS or executor cap.
            # The footprint honours the live claim.footprint.slice.cpu_milli
            # override, the same one the launch's spawn admits (gr741695).
            slice_fp = work_footprint(conn, "slice")
            if not launch_fits(conn, default_host, slice_fp):
                _log_launch_refusal(conn, ref_id, default_host, "slice", slice_fp)
                return None
        requires = effective_requires(meta)
        # The resource lives where the job runs: its target_node (an
        # ssh_node GPU job reserves on the GPU box, not the claimer), else
        # this host. Self-gate to what that host actually advertises — an
        # unadvertised requirement falls back to the node-gate/pin (no
        # stall in the window before the probe populates the slot map).
        params = meta.get("params") or {}
        res_host = str(params.get("target_node") or default_host)
        advertised_here = advertised.get(res_host, set())
        # Footprint admission (gr595542): extra mem_mb/vram_mb units the
        # claim must fit alongside its counted tokens — a GPU job whose
        # effective model (``params.model``, else the job_type's declared
        # default — ``struct_relax`` → ``mace_mp``) has a learned
        # footprint reserves footprint + margin; a science-lane
        # (ssh_node) job with enough measured runs reserves its predicted
        # peak RSS × margin. Dark (no units) until the model row /
        # rollup key / host capacity rows exist, so a bare queue is
        # byte-identical to the token-only claim. The conditional
        # decrement below refuses the claim when the headroom isn't
        # there; ``admission_block`` vets the units against live state
        # (per-card fit, measured free headroom) after the decrement,
        # refunding it on a veto.
        footprint = _footprint_admission.admission_units(
            conn,
            executor=executor,
            meta=meta,
            res_host=res_host,
            requires=requires,
            advertised_here=advertised_here,
            cache=fp_cache,
        )
        if not requires and not footprint:
            _footprint_admission.note_model_use(conn, meta=meta, res_host=res_host)
            _stamp_lease_identity(ref_id, meta, status_value)
            return (ref_id, title, meta)
        # Soft memory-pressure veto (6d-deferred): a heavy job's reservation
        # host is out of RAM headroom → skip it this round (the lock frees at
        # commit; it retries once pressure clears). Dark until a ``mem`` row
        # with free=0 exists. Covers footprint-bearing jobs too — a predicted
        # RSS is exactly the "heavy" signal the veto exists for.
        if res_host in pressured:
            return None
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
            # local-serving node in 25s, all pinned to the same cpuset; the
            # 5400s-wall pair was deadline-killed while their second-DGX-node twins finished in
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
        to_reserve = {**reservable, **footprint}
        if to_reserve and not reserve_resource_slots(conn, res_host, to_reserve):
            # A live slot is full (token or footprint headroom) → drop it;
            # the lock frees at commit and the job waits for capacity on
            # that host. All-or-nothing: a GPU job never holds its token
            # without its VRAM, nor its VRAM without its token.
            return None
        gpu_card: int | None = None
        if footprint:
            # Vetted after the decrement: it row-locks the ledger, so a
            # concurrent claimer's committed admissions are visible to the
            # pending-headroom sum (td655172). A veto refunds in this txn;
            # an admitted VRAM claim also carries the card index the veto
            # picked onto the reservation below, binding the dispatch.
            blocked, gpu_card = _footprint_admission.admission_block(
                conn, host=res_host, units=footprint, cache=fp_cache
            )
            if blocked is not None:
                release_resource_slots(conn, res_host, to_reserve)
                # td655172: the ledger rows are static capacity minus
                # reservations — a per-card misfit (footprint > any single
                # card), a live-free shortfall (untracked consumers the
                # ledger can't see, plus holds the last beat can't have
                # captured and that stay pending until their reservations
                # release — td655171) or every eligible card at its job
                # cap would over-admit past the decrement. Skip this
                # round; the lock frees at commit and the claim retries.
                log.debug(
                    "claim: skip job %s on %s — footprint admission "
                    "blocked (%s, units %s)",
                    ref_id,
                    res_host,
                    blocked,
                    footprint,
                )
                return None
        if to_reserve:
            reserved: dict[str, Any] = {"host": res_host, "slots": to_reserve}
            if gpu_card is not None:
                # The admitted card — dispatch binds execution to it
                # (struct_relax --gpus device=<i> / cuda:<i> in-proc).
                reserved["gpu_card"] = gpu_card
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
        _footprint_admission.note_model_use(conn, meta=meta, res_host=res_host)
        _stamp_lease_identity(ref_id, meta, status_value)
        return (ref_id, title, meta)

    claimed: list[tuple[int, str, dict[str, Any]]] = []
    fresh_builds: list[int] = []
    # One ``_queued_build_plan`` load per claim pass, shared by the
    # overlap and sequencing holds — the ssh_node queue filter below and
    # the per-job ``_finish_claim`` gate (gr656970 review: each hold used
    # to load its own copy, so the same plan subtrees were fetched twice
    # per pass, and twice again under the claim lock). Loaded lazily — a
    # pass that claims no ``run_slice`` (and isn't ssh_node's) never pays
    # for it. The load covers every queued build's parent, so
    # ``_finish_claim`` needs no narrower read of its own.
    _build_plan_shared: tuple[Any | None, set[int]] | None = None

    def _shared_build_plan() -> tuple[Any | None, set[int]]:
        nonlocal _build_plan_shared
        if _build_plan_shared is None:
            _build_plan_shared = _queued_build_plan(conn, None)
        return _build_plan_shared

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
                *parent_params,
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
    # Under ``reclaim_counts_toward_limit`` the pre-pass consumed part of
    # ``limit`` — the fresh window shrinks to the remainder so the call's
    # total return stays ≤ ``limit`` (default stays additive).
    fresh_limit = max(0, limit - len(claimed)) if reclaim_counts_toward_limit else limit
    # ``queued_since`` is needed twice — as a column and inside the aged
    # Per-job_type running caps (D4 cap half): code-shipped
    # :data:`_MAX_RUNNING_DEFAULTS` plus ``app_state``
    # ``claim.max_running.<job_type>`` overrides bound how many rows of a
    # type may be RUNNING on this host — the derived_drain band can never
    # occupy every ``job_inproc`` process. At-cap types are EXCLUDED from
    # the candidate set, so a capped band can't starve rows queued behind
    # it. A per-type advisory lock serializes the count+claim pair, so
    # two concurrent claimers can't both pass the cap — a TRY lock, never
    # a blocking one: the loser treats the type as at-cap for this pass
    # (the cap can never be exceeded by the loser) instead of queueing on
    # the holder's open transaction until ``lock_timeout`` kills the whole
    # claim pass — a concurrent claim is the normal case on a multi-worker
    # host (test_job_claim_holds_a_real_row_lock_skip_locked).
    #
    # "This host" is ``default_host`` — the ``reserve_host()`` namespace
    # ``_stamp_lease_identity`` writes into ``meta.lease_host`` — NOT
    # ``node`` (``PRECIS_NODE``): the node gate names a capability lane,
    # not a machine, and where the two diverge a node-keyed count reads 0
    # running rows forever, so the cap silently never binds and a bulk
    # band can occupy every in-proc slot on the host (gr592211: 49-queued
    # ``derived_drain`` starved release_poll/factory_rescue_tick off
    # the agent host's lanes for 15+ min per turnover).
    cap_sql = ""
    cap_params: tuple[Any, ...] = ()
    reserve_block_sql = ""
    reserve_block_params: tuple[Any, ...] = ()
    caps: dict[str, int] = {}
    running_by_type: dict[str, int] = {}
    lane_usage: dict[str, float] = {}
    lane_weights: dict[str, float] = {}
    claim_host = default_host
    executor_cap: int | None = None
    executor_reserve: dict[str, int] = {}
    reserve_total = 0
    running_total = 0
    if claim_host is not None:
        caps = job_type_running_caps(conn)
        executor_cap, executor_reserve = executor_lane_limits(
            conn, executor, host=claim_host
        )
        reserve_total = sum(executor_reserve.values())
        if executor_cap is not None:
            # Serialize this lane's count+claim (td604572 review of
            # gr603792): the per-type caps lock per job_type below, but
            # the executor pool's own count-and-claim pair raced — two
            # concurrent claimers read the same ``running_total`` and
            # each admitted a row, so the cap was exceeded into the
            # reserved slots. Same TRY-lock rule as the per-type caps:
            # the loser treats the lane as contested and its fresh work
            # re-gates next cycle.
            held = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)::bigint)",
                (_executor_lane_lock_key(executor, claim_host),),
            ).fetchone()
            if not (held and held[0]):
                return claimed
        contested: set[str] = set()
        for jt in sorted(caps):
            held = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)::bigint)",
                (f"claim-cap:{jt}",),
            ).fetchone()
            if not (held and held[0]):
                contested.add(jt)
        # One grouped read feeds the per-type cap counts; the lane
        # deficit reads its own windowed usage below (gr595541).
        running_by_type = {}
        for jt, _ps, cnt in conn.execute(
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
        running_total = sum(running_by_type.values())
        if claim_class_rank_enabled():
            lane_usage = lane_seconds_in_window(
                conn, executor, claim_host, claim_share_window_s()
            )
            lane_weights = lane_share_weights(conn)
        if executor_cap is not None and running_total >= executor_cap:
            # Lane already full under the lane lock — no fresh claim can
            # pass this cycle, so the candidate query is skipped too.
            return claimed
        at_cap = sorted(
            jt
            for jt, cap in caps.items()
            if jt in contested or running_by_type.get(jt, 0) >= cap
        )
        if at_cap:
            cap_sql = " AND COALESCE(r.meta->>'job_type', '') <> ALL(%s)"
            cap_params = (at_cap,)
        if executor_cap is not None:
            # Reservation-blocked types are excluded BEFORE the LIMIT
            # (td604572 review): the window below is only
            # ``fresh_limit × _CLAIM_OVERFETCH`` rows — ssh_node's
            # ``limit=1`` fetched three, so three blocked computes ahead
            # of a factory job hid it every pass while its reserved slot
            # sat free. Once the shared band fills (``running_total >=
            # cap - reserve_total``) only the reserved set can still
            # claim — expressible in SQL as "job_type IS one of the
            # reserved set"; a reserved type blocks once its own
            # allotment is used and only peers' slots remain. The
            # pick-loop check below stays — it re-fires as this pass's
            # own claims fill the band.
            reserve_clauses: list[str] = []
            if running_total >= executor_cap - reserve_total:
                reserve_clauses.append("COALESCE(r.meta->>'job_type', '') = ANY(%s)")
                reserve_block_params += (sorted(executor_reserve),)
            blocked_reserved = sorted(
                jt
                for jt, n in executor_reserve.items()
                if running_by_type.get(jt, 0) >= n
                and running_total >= executor_cap - (reserve_total - n)
            )
            if blocked_reserved:
                reserve_clauses.append("COALESCE(r.meta->>'job_type', '') <> ALL(%s)")
                reserve_block_params += (blocked_reserved,)
            if reserve_clauses:
                reserve_block_sql = " AND " + " AND ".join(
                    f"({c})" for c in reserve_clauses
                )

    # ── Fresh-work pass (unchanged ordering/semantics) ──────────────────
    # ``q.qs`` (the queued-tag's stamp) and ``b.age_s``/``b.base_class``
    # are computed once per row by LATERALs — the claim-order expressions
    # read the aliases instead of repeating correlated subqueries.
    class_rank_on = claim_class_rank_enabled()
    age_step = claim_age_step_s()
    class_age = claim_class_age_s()
    claim_rank_sql, aged_prio_sql, order_params = _claim_order_sql(
        age_step,
        class_age,
        class_rank=class_rank_on,
        release_freeze=True,
    )
    # The full eligibility gate, shared by the eligible-lane read (the
    # deficit denominator below) and the candidate query — one definition,
    # so the two can never disagree.
    where_sql = f"""
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
               ){exclusion_sql}{parent_sql}{suspend_sql}{cap_sql}{reserve_block_sql}"""
    where_params: tuple[Any, ...] = (
        executor,
        node,
        STATUS_NAMESPACE,
        list(CLAIMABLE),
        STATUS_NAMESPACE,
        list(TERMINAL),
        *parent_params,
        *suspend_params,
        *cap_params,
        *reserve_block_params,
    )
    # Filter existing holds before LIMIT so held builds cannot fill the window.
    hold_params: tuple[list[int], ...] = ()
    if executor == "ssh_node":
        shared = _shared_build_plan()
        held_builds = (
            build_overlap_holds(conn, shared=shared)
            | build_blocked_holds(conn, shared=shared).keys()
        )
        if held_builds:
            hold_params = (sorted(held_builds),)
    hold_sql = _BUILD_HOLD_FILTER_SQL if hold_params else ""
    where_sql += hold_sql.format(alias="r")
    where_params = (*where_params, *hold_params)
    # D4 share half, deficit form (gr595541): inside the winning class the
    # lane (``meta.share_key``, else ``job_type[:pass]`` —
    # :func:`_share_key`) this host has served LEAST of its weighted share
    # over the sliding window goes first, then aged-prio/queued_since/
    # ref_id. The SQL emits the SAME ``w/W - frac`` deficit the Python
    # re-rank applies, ordered before the LIMIT so the overfetch window
    # always contains the owed lane's rows: ``W`` is the weight total
    # over the eligible lanes, read with a DISTINCT pass over the same
    # gates (td655978 review — a ``frac/weight`` key tied at zero usage,
    # so three older weight-1 rows could fill a ``limit=1`` window while
    # the weight-3 lane owed 75% never reached the re-rank). The window's
    # lane-seconds arrive as a JSON map; a lane absent from both maps
    # reads as weight 1 / no service, the same formula a raced-in row
    # gets. Runs only under the class order — the
    # ``PRECIS_CLAIM_CLASS_RANK=0`` kill switch keeps the pre-slice-2
    # ORDER BY verbatim. Skipped when weights are all default AND the
    # window is empty — every deficit is then identical.
    share_col_sql = "0"
    share_order_sql = ""
    share_params: tuple[Any, ...] = ()
    eligible_lanes: set[str] = set()
    if class_rank_on and claim_host is not None and (lane_weights or lane_usage):
        lane_key = _LANE_KEY_SQL.format(a="r")
        eligible_lanes = {
            str(lane)
            for (lane,) in conn.execute(
                f"SELECT DISTINCT {lane_key} FROM refs r {where_sql}",
                where_params,
            ).fetchall()
        }
        lane_w_total = (
            sum(lane_weights.get(lane, 1.0) for lane in eligible_lanes) or 1.0
        )
        total_s = sum(lane_usage.values())
        fracs = {k: v / total_s for k, v in lane_usage.items()} if total_s > 0 else {}
        share_col_sql = (
            f"COALESCE((%s::jsonb->>{lane_key})::float8, 1) / %s"
            f" - COALESCE((%s::jsonb->>{lane_key})::float8, 0)"
        )
        share_order_sql = "lane_deficit DESC, "
        share_params = (json.dumps(lane_weights), lane_w_total, json.dumps(fracs))
    fresh_sql = f"""
        SELECT r.ref_id, r.title, r.meta, r.prio, r.created_at,
               q.qs AS queued_since,
               {claim_rank_sql} AS claim_rank,
               {aged_prio_sql} AS aged_prio,
               {share_col_sql} AS lane_deficit,
               {_factory_pref_sql(class_rank_on)} AS factory_pref,
               {_CLAIM_AGE_BUCKET_SQL} AS aged_past_class_age,
               {_DISPATCH_SCORE_SQL} AS dispatch_score
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
                SELECT min({_CLASS_RANK_CASE.replace("r.", "r3.")}) AS top_class,
                       bool_or({_CLASS_RANK_CASE.replace("r.", "r3.")}
                               = {_RELEASE_LEG_RANK}) AS has_release
                  FROM refs r3
                 WHERE r3.kind = 'job' AND r3.retired_at IS NULL
                   AND r3.meta->>'executor' = %s
                   AND EXISTS (
                         SELECT 1 FROM ref_tags rt3 JOIN tags t3 USING (tag_id)
                          WHERE rt3.ref_id = r3.ref_id
                            AND t3.namespace = %s AND t3.value = ANY(%s)
                       )
                   AND COALESCE(r3.prio, %s)::float8 <> 1
                   {hold_sql.format(alias="r3")}
              ) t
          {where_sql}@@BG_WHERE@@
         ORDER BY @@ORDER_BY@@
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
        """
    fresh_params: tuple[Any, ...] = (
        *order_params,
        *share_params,
        class_age,
        STATUS_NAMESPACE,
        list(CLAIMABLE),
        _DEFAULT_JOB_PRIO,
        _DEFAULT_JOB_PRIO,
        _DEFAULT_JOB_PRIO,
        executor,
        STATUS_NAMESPACE,
        list(CLAIMABLE),
        _DEFAULT_JOB_PRIO,
        *hold_params,
        *where_params,
    )
    rows = conn.execute(
        fresh_sql.replace("@@BG_WHERE@@", "").replace(
            "@@ORDER_BY@@",
            f"{_CLAIM_ORDER_PREFIX_SQL}, {share_order_sql}"
            "aged_prio ASC, q.qs ASC, r.ref_id ASC",
        ),
        (*fresh_params, fresh_limit * _CLAIM_OVERFETCH),
    ).fetchall()
    # Cross-class starvation guard (td656098): the order above never lets
    # a background row beat agent work, so under an always-busy agent
    # queue it never claims. About 1 claim in ``_BG_SHARE_EVERY`` is
    # reserved for the OLDEST waiting background row — strictly
    # oldest-first, ignoring the factory term (td657493 review): it is
    # also the bulk-progress floor under a factory stream, so letting a
    # factory op take it would starve the very backlog it guards —
    # fetched into the window explicitly (the overfetch window is
    # agent-ordered and may not contain it).
    bg_ticks = _bg_share_ticks.get(executor, 0)
    bg_share_due = (
        class_rank_on
        and fresh_limit > 0
        and any(
            (bg_ticks + i) % _BG_SHARE_EVERY == 0 for i in range(1, fresh_limit + 1)
        )
    )
    if bg_share_due:
        have = {int(r[0]) for r in rows}
        rows = [
            *rows,
            *(
                r
                for r in conn.execute(
                    fresh_sql.replace(
                        "@@BG_WHERE@@",
                        " AND b.base_class = 3 AND COALESCE(r.prio, %s)::float8 <> 1",
                    ).replace(
                        "@@ORDER_BY@@",
                        "q.qs ASC, r.ref_id ASC",
                    ),
                    (*fresh_params, _DEFAULT_JOB_PRIO, _CLAIM_OVERFETCH),
                ).fetchall()
                if int(r[0]) not in have
            ),
        ]

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
    now_utc = datetime.now(UTC)

    def _row_base(r: Any) -> float:
        meta = dict(r[2] or {})
        if (
            str(meta.get("job_type") or "") in RELEASE_LEG_JOB_TYPES
            or meta.get(_dscore.CLAIM_BAND_META_KEY) == _dscore.RELEASE_CLAIM_BAND
        ):
            return _RELEASE_LEG_RANK
        origin = meta.get("origin")
        if isinstance(origin, str) and origin in _BASE_CLASS_RANK:
            return _BASE_CLASS_RANK[origin]
        p = float(r[3] if r[3] is not None else _DEFAULT_JOB_PRIO)
        return 2 if p <= 2 else 3

    def _row_prio(r: Any) -> float:
        return float(r[3] if r[3] is not None else _DEFAULT_JOB_PRIO)

    top_class = min((_row_base(r) for r in rows if _row_prio(r) != 1.0), default=0)
    # Release-band freeze (gr584136): while a release leg is among the
    # claimable non-prio-1 candidates, no row class-promotes — mirrors the
    # ``t.has_release`` term the SQL emits under ``release_freeze=True``.
    has_release = any(
        _row_base(r) == _RELEASE_LEG_RANK and _row_prio(r) != 1.0 for r in rows
    )

    def _row_age_s(r: Any) -> float:
        qs = r[5] or r[4]
        if qs is None:
            return 0.0
        if getattr(qs, "tzinfo", None) is None:
            qs = qs.replace(tzinfo=UTC)
        return max(0.0, (now_utc - qs).total_seconds())

    # The deficit denominator is the eligible lane set the SQL used when
    # it ordered before LIMIT (td655978); empty when the share path
    # didn't run — fall back to the fetched lanes.
    cand_lanes = eligible_lanes or {_share_key(dict(r[2] or {})) for r in rows}
    lane_w_total = sum(lane_weights.get(k, 1.0) for k in cand_lanes) or 1.0

    def _lane_deficit(lane: str) -> float:
        """``share - fraction`` of the window's lane-seconds, the share
        being this lane's weight over the eligible lanes' weights."""
        used = sum(lane_usage.values())
        frac = lane_usage.get(lane, 0.0) / used if used > 0 else 0.0
        return lane_weights.get(lane, 1.0) / lane_w_total - frac

    # Model affinity (gr595542): the host each model-carrying candidate
    # would load its model on (its target_node pin, else this host — the
    # ``_finish_claim`` reservation-host derivation), narrowed to hosts
    # whose last-used-model stamp is still fresh. The model is the row's
    # EFFECTIVE model — ``params.model`` else the job_type's declared
    # default (``struct_relax`` → ``mace_mp``) — so an implicit default
    # steers exactly like an explicit one (td655172). Read once per pass;
    # a queue with no model-carrying rows costs no query.
    _pref_hosts = {
        str((dict(r[2] or {}).get("params") or {}).get("target_node") or default_host)
        for r in rows
        if _footprint_admission.effective_model(dict(r[2] or {}))
    }
    model_pref = _footprint_admission.last_models(conn, _pref_hosts)

    def _row_affinity(r: Any) -> int:
        """``0`` when the job's effective model matches the host's fresh
        last-used-model stamp (warm weights — claim it first), else ``1``.
        A pure tiebreak: it lands after aged prio and before queue age, so
        it reorders only rows the fairness keys already tied."""
        meta = dict(r[2] or {})
        model = _footprint_admission.effective_model(meta)
        if not model:
            return 1
        params = meta.get("params") or {}
        host = str(params.get("target_node") or default_host)
        return 0 if model_pref.get(host) == str(model) else 1

    def _order_key(
        r: Any,
    ) -> tuple[float, float, int, int, float, float, float, int, Any, int]:
        prio = _row_prio(r)
        age = _row_age_s(r)
        if class_rank_on and prio == 1.0:
            rank, floor = -1.0, 5.0
        elif class_rank_on:
            base = _row_base(r)
            # May tie the top class queued, never reaches agent/human —
            # except while a release leg is claimable: then every row's
            # floor is its own base, so an aged background row can never
            # reach or cross the release band (gr584136).
            floor_rank = (
                float(base) if has_release else float(min(base, max(top_class, 2)))
            )
            rank = float(max(base - int(age // class_age), floor_rank))
            floor = 5.0 if rank < 2 else 2.0
        else:
            # Kill switch: the pre-slice-2 order — aged prio, 1.5 floor.
            rank, floor = 0.0, 1.5
        aged = max(prio - age / age_step, min(prio, floor)) if age_step > 0 else prio
        scarcity = _scarcity(effective_requires(dict(r[2] or {})), host_count)
        # D4 share half: the lane's deficit as it stands NOW (the pick loop
        # charges each claim to its lane), larger deficit first.
        deficit = _lane_deficit(_share_key(dict(r[2] or {}))) if class_rank_on else 0.0
        # ``queued_since`` (r[5]) before ``ref_id``: the same FIFO-within-band
        # rule the SQL ORDER BY applies — see the "Claim ordering" note in
        # :func:`claim_executor_jobs`. Never NULL for a row the EXISTS above
        # admitted, but ``or r[4]`` keeps the key total if a race retags it.
        return (
            -scarcity,
            rank,
            r[9],  # factory_pref
            -int(r[10]),  # aged_past_class_age
            -r[11],  # dispatch_score
            -deficit,
            aged,
            _row_affinity(r),
            r[5] or r[4],
            int(r[0]),
        )

    def _oldest_background(cands: list[Any]) -> int | None:
        """Index of the oldest queued background-class row — the reserved
        share's pick. Strictly oldest-first, ignoring the factory term
        (td657493 review): the share is the bulk-progress floor under a
        factory stream — a factory-first pick would let continuous
        factory work starve the backlog the guard exists for, while a
        factory op loses only this one pick and still claims on the
        ordinary order. ``None`` when a human-class or prio-1 row waits
        (they preempt the share) or no background row is queued."""
        if any(_row_prio(c) == 1.0 or _row_base(c) == 0 for c in cands):
            return None
        bg = [
            i
            for i, c in enumerate(cands)
            if _row_base(c) == _BASE_CLASS_RANK["background"]
        ]
        if not bg:
            return None
        return min(bg, key=lambda i: (cands[i][5] or cands[i][4], int(cands[i][0])))

    fresh_claimed = 0
    remaining = sorted(rows, key=_order_key)
    while remaining and fresh_claimed < fresh_limit:
        # Pick the best row under the CURRENT share counts — popping
        # argmin each time keeps a ``limit``-sized batch interleaving
        # shares rather than claiming a statically-sorted prefix.
        pick = min(range(len(remaining)), key=lambda i: _order_key(remaining[i]))
        if class_rank_on:
            bg_ticks += 1
            if bg_ticks % _BG_SHARE_EVERY == 0:
                oldest = _oldest_background(remaining)
                if oldest is not None:
                    pick = oldest
        r = remaining.pop(pick)
        ref_id, title, meta = int(r[0]), str(r[1]), dict(r[2] or {})
        job_type = str(meta.get("job_type") or "")
        cap = caps.get(job_type)
        if cap is not None and running_by_type.get(job_type, 0) >= cap:
            continue  # hit the cap mid-claim — the SQL exclusion only sees the count at query time
        if executor_cap is not None:
            # Per-class slot reservation (gr603792). ``jt`` claims into
            # the shared band while ``running_total < cap - (reserve_total
            # - reserve[jt])`` — a non-reserved type stops at
            # ``cap - reserve_total``, so the last slots stay free — and
            # into its OWN reserved allotment while ``running[jt] <
            # reserve[jt]`` (a peer's reserved slot is never borrowable:
            # once another reserved class holds its slot the pool counts
            # it, and ``jt`` must still reach its own). The hard ceiling
            # ``running_total >= cap`` binds every type — cap 0 suspends
            # the lane entirely.
            own = executor_reserve.get(job_type, 0)
            allowed = executor_cap - (reserve_total - own)
            if running_total >= executor_cap or (
                running_total >= allowed and running_by_type.get(job_type, 0) >= own
            ):
                continue  # only slots reserved for other types remain
        claim = _finish_claim(ref_id, title, meta, QUEUED, r[4])
        if claim is None:
            continue
        claimed.append(claim)
        if job_type == "run_slice":
            fresh_builds.append(ref_id)
        fresh_claimed += 1
        running_total += 1
        running_by_type[job_type] = running_by_type.get(job_type, 0) + 1
        share_key = _share_key(meta)
        lane_usage[share_key] = lane_usage.get(share_key, 0.0) + _BATCH_CLAIM_CHARGE_S
    _bg_share_ticks[executor] = bg_ticks
    return claimed


# ── Status / chunk / meta helpers ─────────────────────────────────


def set_status(
    store: Store, ref_id: int, value: str, *, conn: Connection | None = None
) -> None:
    """Replace the current ``STATUS:`` tag with ``value`` on ``ref_id``.

    A terminal value also refunds any resource reservation the job holds
    (slice 6c) — release rides the same status write so a job's slots come
    back the instant it stops running, in the caller's transaction. A
    ``run_slice`` job's terminal write also wakes the oldest job parked for
    its lane's capacity (:func:`_wake_lane_waiter`, gr611299).
    """
    from precis.store import Tag

    tag = Tag.parse_strict(f"STATUS:{value}")
    lane = _freed_lane(store, ref_id, conn) if value in TERMINAL else None
    store.add_tag(
        ref_id,
        tag,
        set_by="agent",
        replace_prefix=True,
        conn=conn,
    )
    if value in LEAVE_RUNNING:
        # Segment close short of terminal (yield / park / requeue): the
        # rusage spent so far folds into the accumulator the terminal
        # stamp drains (gr595536).
        job_footprint.fold(ref_id)
    if value in TERMINAL:
        if conn is not None:
            release_job_reservation(conn, ref_id)
        else:
            with store.pool.connection() as c:
                with c.transaction():
                    release_job_reservation(c, ref_id)
        if lane is not None:
            _wake_lane_waiter(store, lane, conn)


def _freed_lane(store: Store, ref_id: int, conn: Connection | None) -> str | None:
    """The lane a live ``run_slice`` job is about to free, read before its
    terminal write (see :func:`precis.plans.lanes.freed_lane`; one PK
    SELECT, empty for any other job type). Never raises:
    a missed wake only leaves the job on its ``LANE_FULL_BACKOFF`` timer."""
    from precis.plans import lanes

    # A plain read-only SELECT by primary key: no savepoint needed.
    try:
        if conn is not None:
            return lanes.freed_lane(conn, ref_id)
        with store.pool.connection() as c:
            return lanes.freed_lane(c, ref_id)
    except Exception:  # pragma: no cover — the status write still lands
        log.warning(
            "lane-free wake: lane lookup failed for ref %s", ref_id, exc_info=True
        )
        return None


def _wake_lane_waiter(store: Store, lane: str, conn: Connection | None) -> None:
    """Pull the oldest job parked for ``lane`` forward to now (gr611299).
    Rides the terminal status write's transaction when there is one (a
    savepoint, so a failed wake cannot abort the caller's write). Never
    raises: the parked job's ``LANE_FULL_BACKOFF`` timer is the fallback."""
    from precis.plans import lanes

    try:
        if conn is not None:
            with conn.transaction():
                woken = lanes.wake_lane_waiter(conn, lane)
        else:
            with store.pool.connection() as c:
                with c.transaction():
                    woken = lanes.wake_lane_waiter(c, lane)
    except Exception:  # pragma: no cover — the status write still lands
        log.warning("lane-free wake: wake on lane %r failed", lane, exc_info=True)
        return
    if woken is not None:
        log.info("lane-free wake: lane %r freed a slot, woke jo%s", lane, woken)


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
    meta: dict[str, Any] | None = None,
) -> None:
    """Append a chunk at the next ``ord`` for the ref.

    When ``conn`` is provided we count via that connection so back-to-
    back appends inside the same tx see each other's INSERTs. The
    previous implementation called ``store.list_chunks_for_ref`` which
    opens its own pool connection — uncommitted INSERTs in ``conn``
    were invisible, leading to two calls computing the same
    ``next_pos`` and a unique-constraint violation on ``(ref_id, ord)``.

    ``meta`` merges under ``chunk_kind`` (e.g. the gripe pipeline's
    ``stage`` mark, gr595551) — the caller's keys may never retag the
    row's kind.
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
                meta={**(meta or {}), "chunk_kind": chunk_kind},
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
    (gr614723). ``None`` is
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

#: Head/tail spans :func:`_error_meta` keeps when a ``reason`` outgrows
#: :data:`_ERROR_META_CAP` (gr635381): a traceback's cause is its LAST
#: line, which a bare head-truncation always cut — head for context,
#: the tail for the actual exception. 200 + " … " + 300 = 503 chars.
_ERROR_META_HEAD = 200
_ERROR_META_TAIL = 300


def _error_meta(reason: str) -> str:
    """The ``refs.meta.error`` breadcrumb for a failure ``reason`` —
    the reason itself under :data:`_ERROR_META_CAP`; head
    :data:`_ERROR_META_HEAD` + ``" … "`` + tail :data:`_ERROR_META_TAIL`
    past it, so a long traceback's final line (the exception class and
    message, the exit reason) survives the cap instead of being the
    first thing dropped (gr635381). Every executor ``meta.error``
    stamp routes through here — ``record_failure``, ``poison_guard``,
    and the hand-rolled failure arms in ``claude_inproc``/
    ``claude_docker``/``job_inproc``/``ssh_node``/``devin_api`` — so no
    caller re-introduces a head-only cut. (The sweeper's own stamps are
    short one-line reasons it composes itself, not captured text, so
    they stay direct.)"""
    if len(reason) <= _ERROR_META_CAP:
        return reason
    return f"{reason[:_ERROR_META_HEAD]} … {reason[-_ERROR_META_TAIL:]}"


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
    *,
    submit_attempt: str | None = None,
    temporary_submit_pin: bool = False,
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

    ``note`` also lands on ``wake_when.payload.why`` (first 200 chars) so
    the wake_runner's re-queued/cancelled audit line names the pause the
    job was waiting on (gr592789).
    """
    row = conn.execute(
        "SELECT meta FROM refs WHERE ref_id = %s FOR UPDATE", (ref_id,)
    ).fetchone()
    meta = row[0] if row else {}
    if submit_attempt is not None and meta.get("submit_attempt") != submit_attempt:
        return False
    parks = int(meta.get("pause_parks") or 0)
    if parks >= MAX_PAUSE_PARKS:
        return False
    at = wake_at if wake_at is not None else datetime.now(UTC) + PAUSE_BACKOFF
    append_chunk(store, ref_id, JOB_EVENT_KIND, note, conn=conn)
    set_meta(
        conn,
        ref_id,
        wake_when={
            "kind": "at_time",
            # ``why`` echoes on the wake_runner's re-queued/cancelled
            # audit line — the parked job's health trail names the pause
            # it waited on, not just "(at_time)" (gr592789).
            "payload": {"ts": int(at.timestamp()), "why": note[:200]},
        },
        pause_parks=parks + 1,
    )
    if submit_attempt is not None:
        # The wake can start another submit immediately after this commit.
        # Remove this attempt's launch intent and pin before exposing the park.
        conn.execute(
            "UPDATE refs SET meta = (CASE WHEN %s"
            " AND meta->'params'->>'target_node' = %s"
            " THEN meta #- '{params,target_node}' ELSE meta END)"
            " - ARRAY['submit_started_at', 'submit_attempt', 'submit_host']"
            " WHERE ref_id = %s",
            (temporary_submit_pin, os.environ.get("PRECIS_NODE"), ref_id),
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
    conn: Connection | None = None,
) -> None:
    """Tag a job ``STATUS:failed`` with a reason event chunk.

    ``failure_class`` (optional) distinguishes *why* the job failed — e.g.
    ``"infra"`` (the runner/container/executor itself died: subprocess
    exception, non-zero container exit, malformed/missing output) vs
    ``"non-convergence"`` (the compute actually ran and reported a genuine
    physical/numeric failure) — stamped onto ``refs.meta.failure_class`` so a
    downstream harvest can tell "couldn't run" apart from "ran and failed"
    instead of laundering both into the same bare ``STATUS:failed``. When
    ``failure_class`` is given, ``reason`` (through :func:`_error_meta`)
    is also stamped onto ``refs.meta.error`` alongside it — the full,
    untruncated ``reason`` always lands in the ``job_event`` chunk
    either way; the meta copy exists so a downstream consumer that only
    reads ``meta`` (e.g. an auto-filed infra gripe, or ``view='raw'``'s
    leading ``failure:`` line — gr635381) isn't stuck with a bare
    ``failure_class`` and no idea what actually happened. gr635381 made
    the stamp unconditional: an unclassified failure deserves the same
    visible reason, and past ``_ERROR_META_CAP`` the crumb keeps the
    reason's *tail* — the actual exception — not just its head.

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

    Use ``conn`` to keep a disposition and its evidence in the caller's
    transaction.

    A ``reason`` matching :func:`classify_transient_backoff_hours`
    (rate/spend limits, transient API faults) additionally stamps
    ``meta.retry_after`` on the job — the sweeper's unpark phase re-arms
    the parked parent at that time instead of the 12h·2ᴺ cool-down.
    """
    if conn is None:
        with store.pool.connection() as owned:
            record_failure(
                store,
                ref_id,
                reason,
                gripe_rollback=gripe_rollback,
                failure_class=failure_class,
                open_tag=open_tag,
                conn=owned,
            )
        return

    if open_tag is not None:
        store.add_tag(ref_id, Tag.open(open_tag), set_by="system", conn=conn)
    append_chunk(store, ref_id, JOB_EVENT_KIND, reason, conn=conn)
    set_status(store, ref_id, FAILED, conn=conn)
    # gr635381: ``meta.error`` is stamped for EVERY failure, not only
    # classified ones — a bare ``STATUS:failed`` with no breadcrumb
    # forced the reader into the full job_event chunk for the cause.
    meta_fields: dict[str, Any] = {"error": _error_meta(reason)}
    if failure_class is not None:
        meta_fields["failure_class"] = failure_class
    set_meta(conn, ref_id, **meta_fields)
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
        set_meta(conn, ref_id, **transient_fields)
    if gripe_rollback is not None:
        set_status(store, gripe_rollback, "open", conn=conn)
    # Slice-5 failure bubble.
    from precis.utils.job_bubble import bubble_job_failure

    bubble_job_failure(store, ref_id, conn=conn)


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
    Reclaims an executor marked :data:`RECLAIM_WHY_READOPT` (a restart
    re-adopting a live detached compute) count toward neither.

    **Infra tag (gr692015).** The failed row also carries the open tag
    ``infra:child-killed``, stamped before the bubble in the same
    transaction. :func:`~precis.utils.job_bubble.bubble_job_failure`
    classifies by tag and never reads ``meta.failure_class``; without the
    tag a crash-loop latched ``child-failed:`` on the parent at once
    instead of taking the bounded infra retry.
    """
    cap = MAX_ATTEMPTS if max_attempts is None else max_attempts
    attempts = int(meta.get("attempts") or 0)
    reclaims = meta.get("reclaims") or []
    # re-adoptions of a live detached compute are restarts, not churn
    counted = [r for r in reclaims if r.get("why") != RECLAIM_WHY_READOPT]
    reclaim_churn = len(counted) >= _RECLAIMS_CAP
    if attempts < cap and not reclaim_churn:
        return False
    if reclaim_churn and attempts < cap:
        reason = (
            f"runner: {len(counted)} reclaims recorded (reclaim-churn cap "
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
    store.add_tag(ref_id, Tag.open("infra:child-killed"), set_by="system", conn=conn)
    append_chunk(store, ref_id, JOB_EVENT_KIND, reason, conn=conn)
    set_meta(conn, ref_id, failure_class="infra", error=_error_meta(reason))
    set_status(store, ref_id, FAILED, conn=conn)
    from precis.utils.job_bubble import bubble_job_failure

    bubble_job_failure(store, ref_id, conn=conn)
    return True


#: The memory-kill requeue policy's constants (gr595538).
#: Requeue bump multiplier — the next attempt's footprint estimate is
#: ×1.5 over the best available source (never a smaller cap than before).
MEMORY_KILL_BUMP = 1.5
#: The OPEN tag a memory-killed requeue stamps — the searchable flag.
MEMORY_KILL_FLAG = "killed:memory"
#: Requeues allowed before a second kill parks the job: exactly one
#: bigger retry, then triage decides (never an unbounded bump ladder).
MEMORY_KILL_MAX_REQUEUES = 1


def handle_memory_kill(
    store: Store,
    ref_id: int,
    *,
    cap_mb: float | None = None,
    conn: Connection | None = None,
) -> bool:
    """The memory-kill policy's ACTION (gr595538) — the executor-side
    half of §H's attempt-cap story, next to :func:`poison_guard`.

    A job whose run died to the per-task memory cap deserves a bigger
    retry, not a counted crash attempt: the kill sites stamp
    ``meta.outcome = KILLED_FOR_MEMORY`` (``precis.utils.container_limits``)
    immediately before calling this. First kill → the job is re-queued
    once with its footprint estimate bumped (``meta.footprint_req``,
    ×:data:`MEMORY_KILL_BUMP` over the best of the previous estimate, the
    measured p95 for its ``(job_type, size)`` key and the killed attempt's
    own peak and cap, or 8 GiB when none are known). It stamps the
    :data:`MEMORY_KILL_FLAG` open tag. A second kill →
    ``STATUS:parked`` (reason memory) and one ``memory_triage`` job minted
    (idem ``memory_triage:<ref_id>``) to decide the disposition from the
    run logs — never a bare failure, never a human wait.

    ``meta`` persists across the requeue, so a stale outcome stamp must
    not retrigger: this pops ``meta.outcome`` whenever it acts and counts
    kills from the ``meta.killed_memory`` history it appends. Returns
    ``True`` (and transitions the row, in ``conn``'s transaction when
    given — else in one it opens and commits) when the outcome stamp was
    present and acted on; ``False`` when the caller should proceed with
    its normal failure path. The requeue never bubbles to the parent and
    never touches ``meta.attempts`` — a poison-guard crash-loop count it
    is explicitly not.
    """

    def _act(c: Connection) -> bool:
        from precis.store._jobs_ops import predicted_rss_mb
        from precis.utils.container_limits import KILLED_FOR_MEMORY, job_estimate_mb

        row = c.execute(
            "SELECT meta, prio FROM refs WHERE ref_id = %s FOR UPDATE",
            (ref_id,),
        ).fetchone()
        if row is None:
            return False
        meta = dict(row[0] or {})
        if meta.get("outcome") != KILLED_FOR_MEMORY:
            return False
        status_now = current_status(c, ref_id)
        if status_now in TERMINAL:
            # A stale stamp on a terminal row (the kill-site call raced a
            # terminal write) must not resurrect it — forensics stay.
            return False
        prio = row[1]
        raw_hist = meta.get("killed_memory")
        history = (
            [dict(h) for h in raw_hist if isinstance(h, dict)]
            if isinstance(raw_hist, list)
            else []
        )
        requeue = len(history) < MEMORY_KILL_MAX_REQUEUES
        peak = job_footprint.take(ref_id).get("ru_maxrss_mb")
        previous = job_estimate_mb(meta)
        fields: dict[str, Any] = {"killed_memory": history}
        entry: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(),
            "peak_mb": peak,
            "prev_req_mb": previous,
            "cap_mb": cap_mb,
        }
        history.append(entry)
        if requeue:
            raw_size = meta.get("size")
            size = raw_size if isinstance(raw_size, dict) else {}
            sources = (
                cap_mb,
                previous,
                predicted_rss_mb(
                    c,
                    str(meta.get("job_type") or ""),
                    size.get("name"),
                    size.get("value"),
                    min_samples=_footprint_admission.min_samples(),
                ),
                float(peak) if peak is not None else None,
            )
            best = max(
                (s for s in sources if s is not None and math.isfinite(s) and s > 0),
                default=8192,
            )
            bumped = math.ceil(best * MEMORY_KILL_BUMP)
            entry["bumped_req_mb"] = bumped
            fields["footprint_req"] = bumped
        else:
            fields["park_reason"] = "memory"
        release_job_reservation(c, ref_id)
        c.execute(
            "UPDATE refs SET meta = (meta - 'lease_until' - 'outcome' "
            "- 'container' - 'container_id' - 'deadline' - 'read_mcp_pid' "
            "- 'memory_cap' "
            "- 'compute_handle' - 'submit_started_at') || %s::jsonb "
            "WHERE ref_id = %s",
            (Jsonb(fields), ref_id),
        )

        if requeue:
            # Requeue once, to the back of the queue at unchanged prio —
            # the fresh STATUS:queued tag stamp is the re-queue (the
            # yield_claim shape). Never counts toward poison_guard's
            # meta.attempts: the next claim is a fresh-work claim, and
            # nothing here touches the attempt counter. Always a restart,
            # never a resume — these sessions are per task and short.
            store.add_tag(ref_id, Tag.open(MEMORY_KILL_FLAG), set_by="system", conn=c)
            note = (
                f"runner: killed for memory (peak {peak} MiB) — requeueing "
                f"with the footprint estimate bumped to {bumped} MiB "
                f"(×{MEMORY_KILL_BUMP} over the recorded sizing baseline)"
            )
            append_chunk(store, ref_id, JOB_EVENT_KIND, note, conn=c)
            set_status(store, ref_id, QUEUED, conn=c)
            return True

        # Killed again after the requeue: park (STATUS:parked, reason
        # memory) and hand the row + its run logs to the memory_triage
        # job_type — the big-model pass that decides too-big (drop),
        # bug/leak (gripe) or unexplained (ask-user todo). Never the
        # user directly: only what triage cannot explain reaches a human.
        append_chunk(
            store,
            ref_id,
            JOB_EVENT_KIND,
            "runner: killed for memory a second time — parking "
            "(STATUS:parked, reason memory); a memory_triage job decides "
            "the disposition",
            conn=c,
        )
        set_status(store, ref_id, PARKED, conn=c)
        from precis.workers._mint import mint_job_once

        mint_job_once(
            store,
            conn=c,
            job_type="memory_triage",
            executor="claude_inproc",
            title=f"memory triage: job {ref_id} killed for memory twice",
            params={"job_id": ref_id},
            parent_id=None,
            prio=int(prio) if prio is not None else None,
            idem_key=f"memory_triage:{ref_id}",
        )
        return True

    if conn is not None:
        return _act(conn)
    with store.pool.connection() as c:
        with c.transaction():
            return _act(c)


__all__ = [
    "CANCELLED",
    "CANCEL_REQUESTED",
    "FAILED",
    "JOB_EVENT_KIND",
    "JOB_SUMMARY_KIND",
    "MAX_ATTEMPTS",
    "MAX_PAUSE_PARKS",
    "MEMORY_KILL_BUMP",
    "MEMORY_KILL_FLAG",
    "PARENT_GATED_EXECUTORS",
    "PARKED",
    "PAUSE_BACKOFF",
    "QUEUED",
    "RECLAIM_WHY_EPOCH",
    "RECLAIM_WHY_EXPIRY",
    "RECLAIM_WHY_READOPT",
    "RUNNING",
    "STATUS_NAMESPACE",
    "SUCCEEDED",
    "TERMINAL",
    "TIMED_OUT",
    "WAITING",
    "WAITING_ASK_USER",
    "WAITING_CHILDREN",
    "WAITING_MANUAL_KICK",
    "WAITING_TIME",
    "advertised_by_host",
    "append_chunk",
    "claim_executor_jobs",
    "current_status",
    "dark_advertised_by_host",
    "effective_requires",
    "handle_memory_kill",
    "is_cancel_requested",
    "job_type_requires",
    "lane_adapter_map",
    "max_job_attempts",
    "maybe_reset_gpu_after_kill",
    "parent_pause_gaps",
    "park_job_until",
    "placement_gap",
    "placement_requires",
    "poison_guard",
    "record_failure",
    "release_job_reservation",
    "renew_lease_if_mine",
    "reserve_host",
    "set_meta",
    "set_status",
]
