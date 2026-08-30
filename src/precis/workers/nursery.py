"""Nursery worker — ``docs/backlog/todo-tree-plan.md`` Slice 3: SQL-only
detectors over the todo tree + worker fleet, one ``kind='alert'`` per
condition (:mod:`precis.alerts`) — no LLM call, despite the plan's stale
"Nursery model = sonnet" decision; the rules are deterministic.

Todo-tree detectors (each one SQL query → finding rows):

* **orphans** — open todos with no ``meta.rotation_root`` ancestor; every
  open leaf must root under some strategic.
* **stale claims** — ``claimed-by:<x>`` held > ``STALE_CLAIM_HOURS=3``;
  age from ``ref_tags.created_at`` (same source the ingest lock TTL uses).
* **long waits** — ``waiting-for:*`` held > ``LONG_WAIT_DAYS=7``.
* **stuck doable** — a dispatch candidate (``dispatch.py::_candidate_parent_ids``'s
  signal: ``meta.executor``/``llm_tier``/``OPEN:executor:*``) with none of
  the doable-exclusion tags
  (:func:`precis.handlers._todo_views._doable_exclusion_clause`) and
  ``created_at`` older than ``STUCK_DOABLE_HOURS=24``. Skip the candidacy
  gate and every non-dispatchable leaf reads as stuck.
* **stalled recurrings** — a recurring ref's (``meta.schedule``) latest
  spawned child open longer than the schedule period.
* **spin loops** — a ``(ref_id, source)`` emitting > ``SPIN_LOOP_EVENTS_24H``
  ``ref_events``/24h — a derived-queue worker re-claiming the same ref
  every pass.
* **plan-tick spins** — a planner parent minting > ``PLAN_TICK_REMINT_24H``
  ``plan_tick`` jobs/24h — succeeding each tick without converging.
* **quest-loop failing** — a ``quest_tick`` coordinator resting
  ``STATUS:failed`` > ``QUEST_LOOP_FAIL_24H`` times/24h despite the RC1
  backoff — a persistent break failing at the ceiling cadence. Re-mint-spin
  sibling of ``plan-tick-spin``.
* **orphaned coordinator** (``critical``) — a coordinator's newest loop
  rested ``STATUS:failed`` past the RC1 ceiling with nothing newer minted
  since — the zero-retry silent-outage case ``quest-loop-failing``/
  ``plan-tick-spin`` miss (both need a *repeated* re-mint to fire).
* **child-failed parked** — an open ``child-failed:*`` tag
  (:mod:`precis.handlers._job_bubble`) held > ``CHILD_FAILED_PARKED_HOURS=6``;
  ``stuck-doable`` excludes tagged leaves, so this is the only surfacing
  path once auto-decompose has had its one shot. **Excludes**
  ``child-failed-final`` leaves (below).
* **child-failed-final** — count of leaves that exhausted the sweeper's
  ``unpark`` phase (:data:`precis.workers.sweeper.UNPARK_CAP`); ONE
  aggregate finding, not per-leaf.

Worker-health detectors (daemon liveness, not the todo graph) — all
``critical``, a *new* one pages once via :func:`notify_critical_alert`:

* **worker-restart** — ``(host, process)`` booting > ``WORKER_RESTART_STORM_1H``
  times/1h.
* **dead-worker** — a continuous daemon silent > ``DEAD_WORKER_SILENCE_MIN``
  while its host is otherwise alive.
* **dispatch-stall** — ``claude_inproc`` jobs queued past
  ``DISPATCH_STALL_MINUTES`` with nothing running — symptom-level, so it
  catches both a stalled and a never-started agent-profile executor,
  freezing the planner cluster-wide either way.
* **nas-denied** — a fresh ``host_heartbeat`` reports the NAS EPERM from
  its own launchd context — that host's launchd/cron daemons are all
  locked out of ``/opt/nas``.
* **host-dark** — freshest ``host_heartbeat`` stale past
  ``HOST_DARK_SILENCE_MIN``, bounded to hosts with recent ``worker_logs``
  (``HOST_DARK_LOOKBACK_DAYS``, so a decommissioned host ages out). Since
  heartbeat runs inside the per-host worker it reports on, a dead
  single-worker host's own heartbeat dies too and ``dead-worker``
  self-suppresses — a fleet-mate reports ``host-dark`` instead.
* **embed-lane-stalled** — >= 1 ``embed_batch`` job ``STATUS:queued`` while
  zero succeeded in :data:`EMBED_LANE_STALL_WINDOW_MIN`
  (``docs/backlog/embedder-wedge-hardening.md``) — job *outcome* tags are
  the only truthful probe; process/``/readyz`` checks pass through a wedge.

Each finding → an ``alert`` under ``alert_source = nursery:<category>``,
deduped on ``fingerprint = "<category>:<ref_id>"`` (:mod:`precis.alerts`);
repeat sightings bump ``seen_count``, a disappeared finding auto-resolves
via ``resolve_stale_alerts``. Surfaced by the ``/alerts`` web tab, not
semantic search.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from precis.alerts import notify_critical_alert, raise_alert, resolve_stale_alerts
from precis.handlers._todo_guards import todo_root_sql
from precis.handlers._todo_views import _doable_exclusion_clause
from precis.store import Store
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


# Threshold knobs (hours / days). Mirrored in the skill so the
# operator can find the canonical values without reading code.
STALE_CLAIM_HOURS = 3
LONG_WAIT_DAYS = 7
STUCK_DOABLE_HOURS = 24

#: A single (ref_id, source) emitting more than this many ``ref_events``
#: in 24h is almost certainly a worker spin loop — a derived-queue claim
#: re-picking the same ref every pass because a no-op / terminal-but-
#: retryable outcome never clears the claim predicate (the fetcher
#: retry-window-on-disabled-provider bug and the chase chunk-less-stub
#: loop were both ~150–1300/day per ref). A healthy ref sees a handful
#: of events a day, so 200 is comfortably above the noise floor.
SPIN_LOOP_EVENTS_24H = 200

#: A planner parent minting more than this many ``plan_tick`` jobs in 24h is
#: re-ticking without converging — the coroutine "succeeds" each pass but the
#: task never resolves (its deliverable keeps failing), so dispatch re-mints
#: forever with no resume-streak / ``child-failed`` bubble to stop it. A
#: healthy planner resolves and stops well under this; ~1 tick / 90-min lease
#: sustained across a day (≈16) is already pathological.
PLAN_TICK_REMINT_24H = 16

#: A quest whose ``quest_tick`` coordinator loop has rested ``STATUS:failed``
#: more than this many times in 24h is stuck on a persistent break — the
#: reconciler's RC1 backoff throttles the re-mint to a 30 min → 6 h
#: cadence, so a quest still crossing this threshold is failing at (or near) the
#: ceiling and needs a human. A healthy quest loop rests ``succeeded`` (dry /
#: punt), never ``failed``; a couple of transient failures in a day is
#: tolerable, so 3 keeps it above the noise while catching the standing break.
QUEST_LOOP_FAIL_24H = 3

#: An **active** coordinator (an active quest's ``quest_tick`` loop, or a
#: planner parent's ``plan_tick`` loop) whose *newest* coordinator job rests
#: terminal ``STATUS:failed`` for longer than this, with nothing newer minted
#: since, is dark — not spinning (``quest-loop-failing`` / ``plan-tick-spin``
#: need repeated re-mints to fire), just silent. That's the blind spot the
#: 2026-07-26→30 incident exposed: the melchior worker-agent daemon died, so
#: the reconciler that re-mints coordinator loops never ran at all — one
#: failed row, zero retry attempts, invisible to every count-based detector.
#: Sized to the quest lane's RC1 backoff ceiling (``quest/loop.py``'s
#: ``_DEFAULT_FAIL_BACKOFF_MAX_S`` = 6h): a healthy reconciler always re-mints
#: within that window, so a coordinator still dark past it means the
#: reconciler itself isn't running, not merely backing off.
ORPHANED_COORDINATOR_STALE_HOURS = 6

#: A todo carrying an open ``child-failed:<job_id>`` tag longer than this is
#: parked behind a failure bubble with nothing acting on it. ``stuck-doable``
#: excludes anything with an open tag by construction, so without this
#: detector such a parent is invisible on ``/alerts`` — the blind spot
#: gripe 168886 was filed for. Short relative to ``STALE_CLAIM_HOURS`` /
#: ``LONG_WAIT_DAYS``: the executor's auto-decompose guardrail (tier 2)
#: resolves the common case (a resumable-exhaustion streak) within one
#: tick, so a parent still parked past a few hours is either a hard
#: crash/infra failure or a repeat streak-exhaustion post-decompose —
#: both need a human, promptly.
CHILD_FAILED_PARKED_HOURS = 6

#: A daemon relaunching more than this many times in an hour is in a
#: restart storm — abnormal churn, whatever the cause. The motivating
#: incident was macOS jetsam culling the agent worker ~50-200x/day under
#: memory pressure, orphaning every in-flight plan_tick — invisible for 1.5
#: days because nothing watched daemon health. But a *deploy* storm trips it
#: too (a burst of `/go` ship+deploys each bounce every worker), so the
#: alert can't assert the cause from the count alone — it hedges (see
#: :func:`_restart_storm_detail`). A healthy daemon boots once and runs;
#: even a busy deploy day is a handful of bounces.
WORKER_RESTART_STORM_1H = 8

#: A *continuously-running* daemon silent (no ``worker_logs`` row) for longer
#: than this is dead or wedged — a healthy worker logs every loop iteration
#: (~2s idle cadence) and the DB handler flushes at least every 5s, so a
#: multi-minute gap while the host is otherwise alive means the process died.
DEAD_WORKER_SILENCE_MIN = 10

#: The ``worker_logs.process`` values that run as long-lived loops and so must
#: never fall silent while their host is up. Periodic one-shots (cron-tick,
#: dream) are excluded — their silence between runs is normal, not a fault.
#:
#: ``precis-worker-agent`` was retired 2026-08-04 when the fleet consolidated
#: onto a single ``--profile all`` worker per host (the agent profile folded
#: into ``precis-worker``, and ``com.precis.worker-agent.plist`` was removed).
#: It must NOT stay listed: a decommissioned daemon can never log again, so it
#: matched the dead-worker predicate forever and pinned two false *critical*
#: alerts (melchior + spark) for the full
#: :data:`DEAD_WORKER_LOOKBACK_DAYS` window — noise that buried the real
#: cast/recurring stalls underneath it.
WORKER_CONTINUOUS_PROCESSES = ("precis-worker",)

#: How far back ``dead-worker`` will still consider a (host, process) it once
#: saw. It exists only to stop a *decommissioned* daemon alarming forever — but
#: set to 24h it became the blind spot behind a 4-day silent agent-worker outage
#: (gr176223): once a critical daemon had been dead > 24h its last log row aged
#: out of the window, the finding vanished, and ``resolve_stale_alerts``
#: AUTO-RESOLVED the critical alert — so a still-broken worker read as *fixed*
#: after day one. Widened to match the ``worker_logs`` retention the sweeper
#: keeps (30d): a silent critical daemon stays flagged for as long as we retain
#: any evidence it ran, then natural log pruning (not an arbitrary floor) drops
#: a genuinely-gone one. A real outage never self-silences inside this window.
DEAD_WORKER_LOOKBACK_DAYS = 30

#: A host's freshest ``host_heartbeat`` row older than this is dark — gr186752
#: (``docs/backlog/self-healing-spine.md`` Layer 2). Slightly wider than
#: ``DEAD_WORKER_SILENCE_MIN`` (10 vs the daemon-level 10) since a host-level
#: verdict should not trip on the same jitter a single-daemon check would;
#: kept equal for now (no observed need to separate them) but named
#: independently so they can diverge later without coupling the two detectors.
HOST_DARK_SILENCE_MIN = 10

#: How far back ``host-dark`` will still consider a host it once saw —
#: mirrors :data:`DEAD_WORKER_LOOKBACK_DAYS` (the same ``worker_logs``
#: retention floor): ``host_heartbeat`` is a latest-snapshot-per-host UPSERT,
#: so a decommissioned host's row lingers forever without this bound.
HOST_DARK_LOOKBACK_DAYS = 30

#: A ``claude_inproc`` job (plan_tick / fix_gripe / news / briefing) sitting
#: ``STATUS:queued`` longer than this while **nothing** is running is a stalled
#: planner: minting is cluster-wide but *execution* is agent-profile-only
#: (melchior), so a jetsam-cull / OAuth-401 / never-started agent worker
#: freezes every minted job with no bubble — the 2026-07 "45 min dark"
#: incident (gripe 55748). A healthy executor claims within a worker loop
#: (~seconds), so a multi-minute queued age with zero live claims means the
#: single executor is gone. Complements ``dead-worker`` (process silence):
#: this is symptom-level (work not flowing) and so also catches an agent
#: worker that never started — which has no log rows for dead-worker to age.
DISPATCH_STALL_MINUTES = 15

#: A window with at least one ``embed_batch`` job ``STATUS:queued`` and
#: zero ``STATUS:succeeded`` transitions is a stalled embed lane —
#: ``embedder-wedge-hardening.md``. ``embed_batch`` jobs are bounded
#: (minutes each, not hours), so a healthy lane drains a queued backlog
#: well inside an hour; this is a job-outcome probe, deliberately
#: independent of the embedder process's own ``/readyz`` (which the
#: motivating 2026-08-08→10 wedge proved can lie — HF-Hub-dial hangs read
#: as "alive" for stretches while nothing was actually completing).
EMBED_LANE_STALL_WINDOW_MIN = 60

#: Per-category alert severity (drives sort + colour on the /alerts
#: tab, and — for ``critical`` — a one-shot Discord push via
#: :func:`notify_critical_alert`). Spin loops and stuck claims/recurrings
#: burn resources or block progress → ``warn``; orphans / long-waits /
#: stuck-doable are hygiene nudges → ``info``. The worker-health detectors
#: plus ``orphaned-coordinator`` are ``critical`` — a dead/thrashing worker,
#: or a coordinator loop nothing is re-minting, is an outage (the planner or
#: a quest stalls silently), not drift.
_SEVERITY: dict[str, str] = {
    "spin-loop": "warn",
    "plan-tick-spin": "warn",
    "quest-loop-failing": "warn",
    "orphan": "info",
    "stale-claim": "warn",
    "long-wait": "info",
    "stuck-doable": "info",
    "child-failed-parked": "warn",
    "child-failed-final": "warn",
    "stalled-recurring": "warn",
    "worker-restart": "critical",
    "dead-worker": "critical",
    "dispatch-stall": "critical",
    "orphaned-coordinator": "critical",
    "nas-denied": "critical",
    "host-dark": "critical",
    "embed-lane-stalled": "critical",
}


@dataclass(frozen=True, slots=True)
class Symptom:
    """One nursery hit.

    ``ref_id`` + ``category`` is the dedup key for the graph detectors
    (each finding is a specific todo/job ref). The worker-health detectors
    are not ref-scoped — they set ``ref_id=None`` and supply an explicit
    ``fingerprint_key`` (e.g. ``"worker-restart:melchior:precis-worker-agent"``)
    so dedup / auto-resolve still work per (host, process).

    ``total`` is the category's true pre-``LIMIT`` count (a window-function
    read, same value on every ``Symptom`` of that pass) for the handful of
    ``LIMIT 50``-capped detectors that compute it; ``None`` for every other
    detector.
    """

    category: str
    ref_id: int | None
    title: str
    detail: str  # one-line human summary for the alert
    fingerprint_key: str | None = None
    total: int | None = None


#: Detectors in catalogue order, each paired with its category. The
#: category is both the alert sub-source (``nursery:<category>``) and
#: the dedup-fingerprint prefix. Each detector self-limits to 50 hits.
_DETECTORS: tuple[tuple[str, Callable[[Store], list[Symptom]]], ...] = (
    ("spin-loop", lambda s: _detect_spin_loops(s)),
    ("plan-tick-spin", lambda s: _detect_plan_tick_spins(s)),
    ("quest-loop-failing", lambda s: _detect_quest_loop_failures(s)),
    ("orphaned-coordinator", lambda s: _detect_orphaned_coordinator(s)),
    ("orphan", lambda s: _detect_orphans(s)),
    ("stale-claim", lambda s: _detect_stale_claims(s)),
    ("long-wait", lambda s: _detect_long_waits(s)),
    ("stuck-doable", lambda s: _detect_stuck_doable(s)),
    ("child-failed-parked", lambda s: _detect_child_failed_parked(s)),
    ("child-failed-final", lambda s: _detect_child_failed_final(s)),
    ("stalled-recurring", lambda s: _detect_stalled_recurrings(s)),
    ("worker-restart", lambda s: _detect_worker_restart_storms(s)),
    ("dead-worker", lambda s: _detect_dead_workers(s)),
    ("nas-denied", lambda s: _detect_nas_denied(s)),
    ("host-dark", lambda s: _detect_host_dark(s)),
    ("dispatch-stall", lambda s: _detect_dispatch_stalls(s)),
    ("embed-lane-stalled", lambda s: _detect_embed_lane_stalled(s)),
)


def run_nursery_pass(store: Store, *, limit: int = 50) -> BatchResult:
    """Detect; raise/refresh an alert per finding; auto-resolve cleared.

    Counters in the returned ``BatchResult``:

    * ``claimed`` = number of findings surfaced this pass (raised or
      refreshed alerts)
    * ``ok`` = number of alerts auto-resolved this pass (conditions
      that cleared)
    * ``failed`` = 0 (no failure mode in the SQL detectors)

    Per detector: raise an ``alert`` for every current finding (deduped
    on ``"<category>:<ref_id>"`` so a repeat just bumps ``seen_count``),
    then resolve any open alert of that source whose fingerprint is no
    longer present. Empty findings still run the resolve sweep, so a
    fixed problem disappears from the open list on the next pass.
    """
    raised = 0
    resolved = 0
    for category, detect in _DETECTORS:
        source = f"nursery:{category}"
        severity = _SEVERITY.get(category, "warn")
        findings = detect(store)
        surfaced = len(findings)
        live: list[str] = []
        for f in findings:
            fp = f.fingerprint_key or f"{f.category}:{f.ref_id}"
            live.append(fp)
            title = f"[{f.category}] {f.title}"
            detail = f.detail
            extra_meta: dict[str, Any] | None = None
            if f.total is not None:
                if f.total > surfaced:
                    detail += f" · showing oldest {surfaced} of {f.total}"
                extra_meta = {"total": f.total}
            _ref_id, is_new = raise_alert(
                store,
                source=source,
                fingerprint=fp,
                title=title,
                detail=detail,
                severity=severity,
                subject_ref_id=f.ref_id,
                extra_meta=extra_meta,
            )
            # A *new* critical condition pages once (dead / thrashing
            # worker → planner stall). Bumps of an already-open alert
            # don't re-push, so a standing outage doesn't spam.
            if is_new and severity == "critical":
                notify_critical_alert(store, title, detail, fingerprint=fp)
        raised += len(findings)
        resolved += resolve_stale_alerts(store, source=source, live_fingerprints=live)
    if raised or resolved:
        log.info("nursery: %d alerts raised/refreshed, %d resolved", raised, resolved)
    return BatchResult(handler="nursery", claimed=raised, ok=resolved, failed=0)


# ── orphans ────────────────────────────────────────────────────────


def _detect_orphans(store: Store) -> list[Symptom]:
    """Open todos whose ancestor chain has no ``meta.rotation_root`` root.

    Walks ``parent_id`` to the topmost ancestor. If that ancestor
    doesn't carry ``meta.rotation_root=true``, the todo is an orphan.
    Recurring subtrees are excluded — they're scheduled work, not
    strategic work, and the plan explicitly carves them out of the
    strategic invariant. A subtree is "recurring" when its root either
    carries ``meta.schedule`` directly, or is a builtin structural
    anchor (``meta.builtin`` set — e.g. the seeded Watches umbrella,
    which is itself a schedule-less folder that every individual
    recurring parents under).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            WITH RECURSIVE walk(ref_id, parent_id, root_id) AS (
                SELECT ref_id, parent_id, ref_id
                  FROM refs
                 WHERE kind = 'todo' AND deleted_at IS NULL
                UNION ALL
                SELECT w.ref_id, r.parent_id, r.ref_id
                  FROM walk w
                  JOIN refs r ON r.ref_id = w.parent_id
                 WHERE r.kind = 'todo' AND r.deleted_at IS NULL
            ),
            roots AS (
                -- folder placement roles: the root is the topmost *todo* — a folder
                -- parent above it is placement, not tree membership.
                SELECT DISTINCT ON (w.ref_id) w.ref_id AS leaf_id,
                       w.root_id,
                       COALESCE((r.meta->>'rotation_root')::boolean, false) AS root_is_strategic,
                       ((r.meta ? 'schedule') OR (r.meta->>'builtin') IS NOT NULL)
                         AS root_is_recurring
                  FROM walk w
                  JOIN refs r ON r.ref_id = w.root_id
                 WHERE {todo_root_sql("r")}
                 ORDER BY w.ref_id, w.root_id
            )
            SELECT r.ref_id, r.title, COUNT(*) OVER () AS total_count
              FROM refs r
              JOIN roots rt ON rt.leaf_id = r.ref_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rtg JOIN tags t ON t.tag_id = rtg.tag_id
                       WHERE rtg.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do', 'auto-timeout')
               -- Root is not strategic
               AND NOT rt.root_is_strategic
               -- And not in a recurring subtree (root is not recurring either)
               AND NOT rt.root_is_recurring
             ORDER BY r.ref_id
             LIMIT 50
            """,
        ).fetchall()
    return [
        Symptom(
            category="orphan",
            ref_id=int(r[0]),
            title=_first_line(r[1]),
            detail=(
                "open todo with no strategic ancestor — root needs "
                "``meta.rotation_root=true`` or this leaf needs to be "
                "re-parented under one"
            ),
            total=int(r[-1]),
        )
        for r in rows
    ]


# ── stale claims ──────────────────────────────────────────────────


def _detect_stale_claims(store: Store) -> list[Symptom]:
    """Leaves with ``claimed-by:<x>`` older than ``STALE_CLAIM_HOURS``.

    The claim's age is ``ref_tags.created_at`` on the open tag row.
    A claim older than the threshold without a STATUS change probably
    means the worker died mid-task (process crash, network split, OOM)
    — the leaf is stuck under a phantom claim.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, t.value AS claim, rt.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value LIKE 'claimed-by:%%'
               AND rt.created_at < now() - %s::interval
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do', 'auto-timeout')
             ORDER BY r.ref_id
             LIMIT 50
            """,
            (f"{STALE_CLAIM_HOURS} hours",),
        ).fetchall()
    out: list[Symptom] = []
    for r in rows:
        claim = str(r[2])
        hours = _hours_since(r[3])
        out.append(
            Symptom(
                category="stale-claim",
                ref_id=int(r[0]),
                title=_first_line(r[1]),
                detail=(
                    f"claimed {hours:.0f}h ago by {claim.removeprefix('claimed-by:')}; "
                    f"if the worker died mid-task, release the claim or "
                    f"mark STATUS:auto-timeout"
                ),
            )
        )
    return out


# ── long waits ────────────────────────────────────────────────────


def _detect_long_waits(store: Store) -> list[Symptom]:
    """Leaves with ``waiting-for:*`` tagged more than ``LONG_WAIT_DAYS``.

    The wait may still be legitimate (a slow API, a paper that takes
    weeks to ingest) but past the threshold the operator probably
    wants to know about it. The detail line names the wait target so
    triage doesn't require an extra ``get``.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, t.value AS wait, rt.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value LIKE 'waiting-for:%%'
               AND rt.created_at < now() - %s::interval
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do', 'auto-timeout')
             ORDER BY r.ref_id
             LIMIT 50
            """,
            (f"{LONG_WAIT_DAYS} days",),
        ).fetchall()
    out: list[Symptom] = []
    for r in rows:
        wait = str(r[2])
        days = _days_since(r[3])
        out.append(
            Symptom(
                category="long-wait",
                ref_id=int(r[0]),
                title=_first_line(r[1]),
                detail=(
                    f"waiting {days:.0f}d on {wait.removeprefix('waiting-for:')}; "
                    f"check whether the dependency is still alive"
                ),
            )
        )
    return out


# ── stuck doable ──────────────────────────────────────────────────


def _detect_stuck_doable(store: Store) -> list[Symptom]:
    """Open leaves that are genuine dispatch *candidates*, with no claim,
    no wait, created >24h ago.

    Two gates keep this from over-firing (gripe 204308 — a live
    firing decomposed as ~100% false positives against these two
    exact gaps):

    * **Candidacy gate** — mirrors the very first eligibility check
      in ``dispatch.py::_candidate_parent_ids``: no
      ``meta.executor`` / ``meta.llm_tier`` / ``OPEN:executor:*``
      auto-run signal means dispatch would never touch this leaf
      regardless of anything else — it isn't "stuck", it was never
      doable to begin with (the ``OPEN:ephemeral`` autocatpath
      compute-lane internals from the audit were exactly this: no
      run signal of their own, dispatch only ever mints their child
      job). This mirrors dispatch.py rather than importing it — the
      two must move together if the candidacy signal's shape ever
      changes.
    * **Exclusion-registry gate** — reuses
      :func:`precis.handlers._todo_views._doable_exclusion_clause`
      (the same "robot stay away" registry the doable view and the
      dispatch worker share) instead of a hand-copied tag list, so a
      self-halted leaf (``OPEN:halt`` / ``OPEN:halt:*``) or one
      already parked behind ``child-failed:*`` reads as
      working-as-designed rather than stuck. ``claimed-by:*`` is
      checked alongside it but isn't part of the shared registry — a
      fresh claim is legitimate in-progress work, not a "robot stay
      away" marker, and a *stale* one already has its own detector
      (:func:`_detect_stale_claims`).

    What's left after both gates are leaves the doable rotation
    *could* be picking but isn't. Causes are usually: PRIO 10 buried
    by louder strategics, a paused ancestor the operator forgot
    about, or a tag mistake. The digest can't diagnose; it just
    surfaces the existence so the operator notices.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, r.created_at, COUNT(*) OVER () AS total_count
              FROM refs r
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND r.created_at < now() - %s::interval
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) IN ('open', 'doing')
               -- Leaf (no children)
               AND NOT EXISTS (
                   SELECT 1 FROM refs c
                    WHERE c.parent_id = r.ref_id
                      AND c.deleted_at IS NULL
               )
               -- Dispatch-candidacy gate — mirrors dispatch.py::
               -- _candidate_parent_ids' first eligibility check.
               AND (
                   r.meta ? 'executor'
                   OR r.meta ? 'llm_tier'
                   OR EXISTS (
                       SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id
                          AND t.namespace = 'OPEN' AND t.value LIKE 'executor:%%'
                   )
               )
               -- Not a recurring root
               AND NOT (r.meta ? 'schedule')
               -- No claim, and none of the shared doable-exclusion tags
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND (
                          t.value LIKE 'claimed-by:%%'
                          OR """
            + _doable_exclusion_clause()
            + """
                      )
               )
               -- Not blocked
               AND NOT EXISTS (
                   SELECT 1 FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
                    WHERE l.src_ref_id = r.ref_id
                      AND l.relation = 'blocked-by'
                      AND b.deleted_at IS NULL
                      AND COALESCE(
                            (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                              WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                            'open'
                          ) NOT IN ('done', 'won''t-do')
               )
             ORDER BY r.ref_id
             LIMIT 50
            """,
            (f"{STUCK_DOABLE_HOURS} hours",),
        ).fetchall()
    return [
        Symptom(
            category="stuck-doable",
            ref_id=int(r[0]),
            title=_first_line(r[1]),
            detail=(
                f"doable for {_hours_since(r[2]):.0f}h with no claim, no wait, "
                f"no blocker — check the strategic rotation or its PRIO"
            ),
            total=int(r[-1]),
        )
        for r in rows
    ]


# ── child-failed parked ────────────────────────────────────────────


def _detect_child_failed_parked(store: Store) -> list[Symptom]:
    """Todos carrying an open ``child-failed:<job_id>`` tag longer than
    ``CHILD_FAILED_PARKED_HOURS``.

    ``bubble_job_failure`` (:mod:`precis.handlers._job_bubble`) tags the
    parent and pulls it out of dispatch/doable rotation on any job
    failure; nothing else automated ever clears it (except, since
    2026-08-10, the sweeper's bounded ``unpark`` phase — see
    :mod:`precis.workers.sweeper`). A parent can carry more than one
    ``child-failed:<job_id>`` tag over time (each failed child job adds
    its own), so this groups by ``ref_id`` and reports the *oldest* one —
    how long the parent has been parked overall.

    **Excludes ``child-failed-final`` leaves** (parked-leaf-recovery, docs/
    backlog/parked-leaf-recovery.md) — those have exhausted the unpark
    phase's bounded retries and are surfaced instead, in aggregate, by
    :func:`_detect_child_failed_final` — repeating them here too would be
    per-leaf noise for a condition the sweep will never touch again.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT sub.ref_id, sub.title, sub.tag, sub.created_at,
                   COUNT(*) OVER () AS total_count
              FROM (
                SELECT DISTINCT ON (r.ref_id)
                       r.ref_id, r.title, t.value AS tag, rt.created_at
                  FROM refs r
                  JOIN ref_tags rt ON rt.ref_id = r.ref_id
                  JOIN tags t ON t.tag_id = rt.tag_id
                 WHERE r.kind = 'todo' AND r.deleted_at IS NULL
                   AND t.namespace = 'OPEN'
                   AND t.value LIKE 'child-failed:%%'
                   AND rt.created_at < now() - %s::interval
                   AND COALESCE(
                         (SELECT t2.value FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                           WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS' LIMIT 1),
                         'open'
                       ) NOT IN ('done', 'won''t-do', 'auto-timeout')
                   AND NOT EXISTS (
                         SELECT 1 FROM ref_tags rt3 JOIN tags t3 ON t3.tag_id = rt3.tag_id
                          WHERE rt3.ref_id = r.ref_id
                            AND t3.namespace = 'OPEN'
                            AND t3.value = 'child-failed-final'
                       )
                 ORDER BY r.ref_id, rt.created_at ASC
              ) sub
             ORDER BY sub.ref_id
             LIMIT 50
            """,
            (f"{CHILD_FAILED_PARKED_HOURS} hours",),
        ).fetchall()
    out: list[Symptom] = []
    for r in rows:
        tag = str(r[2])
        hours = _hours_since(r[3])
        out.append(
            Symptom(
                category="child-failed-parked",
                ref_id=int(r[0]),
                title=_first_line(r[1]),
                detail=(
                    f"parked {hours:.0f}h behind {tag} — a hard failure, a "
                    f"streak-exhaustion past its one auto-decompose attempt "
                    f"(gripe 168886 tier 2), or a decompose tick still "
                    f"queued/running behind a busy dispatch backlog; check "
                    f"the job's job_result / job_event chunks or "
                    f"view='attention' for the recovery options"
                ),
                total=int(r[-1]),
            )
        )
    return out


def _detect_child_failed_final(store: Store) -> list[Symptom]:
    """ONE aggregate finding for every live todo carrying an open
    ``child-failed-final`` tag (parked-leaf-recovery, docs/backlog/
    parked-leaf-recovery.md) — not per-leaf, since a terminal park is a
    condition only a human can now clear (the sweeper's ``unpark`` phase
    never touches it again) and per-leaf findings would just repeat the
    same "go look" message N times. ``ref_id=None`` + an explicit
    ``fingerprint_key`` (mirrors the worker-health detectors) since this
    finding isn't scoped to any one ref. Returns ``[]`` (no alert) when
    the count is zero — the ordinary "cleared" case.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(*)
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value = 'child-failed-final'
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do', 'auto-timeout')
            """
        ).fetchone()
    count = int(row[0]) if row else 0
    if count == 0:
        return []
    return [
        Symptom(
            category="child-failed-final",
            ref_id=None,
            title=f"{count} leaf/leaves stuck at child-failed-final",
            detail=(
                f"{count} todo(s) exhausted the sweeper's unpark phase "
                f"(bounded autonomous retry) and are terminally parked — "
                f"only a human `tag(remove=…)` unparks these now; see "
                f"search(kind='todo', tags=['child-failed-final']) for the list"
            ),
            fingerprint_key="child-failed-final:aggregate",
        )
    ]


# ── stalled recurrings ────────────────────────────────────────────


def _detect_stalled_recurrings(store: Store) -> list[Symptom]:
    """Recurring refs (``meta.schedule`` set) whose most recent spawned
    child has been open more than ~1.5x the recurring's natural cadence.

    The Slice-4 collision-skip leaves the prior tick on the queue
    when it stalls; without nursery surfacing the operator can't
    see why ticks have stopped piling up. We approximate the
    "1.5x cadence" as: child has been open for at least 1h, or
    since the recurring's previous spawn event — whichever is
    longer. The 1h floor catches near-immediate stalls (a daily
    recurring that crashed on its first tick).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT
              rec.ref_id AS rec_id,
              rec.title AS rec_title,
              child.ref_id AS child_id,
              child.title AS child_title,
              child.created_at AS child_created
              FROM refs rec
              JOIN refs child ON child.parent_id = rec.ref_id
                              AND child.deleted_at IS NULL
                              AND child.meta ? 'spawned_for_tick'
             WHERE rec.kind = 'todo' AND rec.deleted_at IS NULL
               AND (rec.meta ? 'schedule')
               AND child.created_at < now() - interval '1 hour'
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = child.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do', 'auto-timeout')
               AND child.created_at = (
                   SELECT max(c2.created_at) FROM refs c2
                    WHERE c2.parent_id = rec.ref_id
                      AND c2.deleted_at IS NULL
                      AND c2.meta ? 'spawned_for_tick'
               )
             ORDER BY rec.ref_id
             LIMIT 50
            """,
        ).fetchall()
    return [
        Symptom(
            category="stalled-recurring",
            ref_id=int(r[0]),
            title=_first_line(r[1]),
            detail=(
                f"recurring #{int(r[0])} stalled: last spawn "
                f"(child #{int(r[2])}) has been open "
                f"{_hours_since(r[4]):.0f}h — collision-skip will keep "
                f"new ticks from piling up; resolve or auto-timeout"
            ),
        )
        for r in rows
    ]


# ── spin loops ────────────────────────────────────────────────────


def _detect_spin_loops(store: Store) -> list[Symptom]:
    """Refs a background worker is hammering — >N events/24h, one source.

    Catches the failure mode where a derived-queue worker re-claims the
    same ref every pass because its no-op / retryable outcome never
    clears the claim predicate. The detail names the source + event +
    rate so triage starts at the worker, not the ref. ``category`` is
    ``spin-loop`` and the dedup key is ``(ref_id, source)`` collapsed
    onto the ref — a loop on the same ref from the same source is one
    finding regardless of how the count drifts pass-to-pass.

    Cheap: a single grouped scan of the last 24h of ``ref_events``,
    which is GIN/btree-indexed on ``ts``. Capped at 50 like the others.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, source,
                   (array_agg(event ORDER BY ts DESC))[1] AS last_event,
                   count(*)::int AS n
              FROM ref_events
             WHERE ts > now() - interval '24 hours'
             GROUP BY ref_id, source
            HAVING count(*) > %s
             ORDER BY count(*) DESC
             LIMIT 50
            """,
            (SPIN_LOOP_EVENTS_24H,),
        ).fetchall()
    out: list[Symptom] = []
    for r in rows:
        ref_id, source, last_event, n = int(r[0]), str(r[1]), r[2], int(r[3])
        out.append(
            Symptom(
                category="spin-loop",
                ref_id=ref_id,
                title=f"{source} on #{ref_id}",
                detail=(
                    f"{n} {source} events in 24h (last: {last_event or '?'}) "
                    f"— a worker is re-claiming this ref every pass; check "
                    f"the {source} claim predicate's backoff/retry window"
                ),
            )
        )
    return out


# ── plan-tick spin (planner re-minting without converging) ──────────


def _detect_plan_tick_spins(store: Store) -> list[Symptom]:
    """Planner parents re-minting many ``plan_tick`` jobs in 24h.

    The resume-streak cap (``meta.plan_tick_resume_streak``) only bubbles an
    *exhaustion* loop (max-turns / timeout). A tick that runs to a clean
    ``STATUS:succeeded`` every pass (verdict: continue) but never resolves the
    task — because its deliverable keeps failing — re-mints forever with no
    streak and no ``child-failed`` bubble (observed: ``nanotrans_auto``
    authoring tex it couldn't address, ~47 ticks/48h). This count-based net
    catches that, mirroring the ``ref_events`` spin detector: a parent minting
    more than :data:`PLAN_TICK_REMINT_24H` plan_tick jobs in 24h is almost
    certainly stuck — usually a repo bug blocking the deliverable, or a task
    that needs splitting.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.parent_id, p.title, count(*)::int AS n
              FROM refs j
              JOIN refs p ON p.ref_id = j.parent_id
             WHERE j.kind = 'job'
               AND j.meta->>'job_type' = 'plan_tick'
               AND j.created_at > now() - interval '24 hours'
               AND j.parent_id IS NOT NULL
               AND p.deleted_at IS NULL
             GROUP BY j.parent_id, p.title
            HAVING count(*) > %s
             ORDER BY count(*) DESC
             LIMIT 50
            """,
            (PLAN_TICK_REMINT_24H,),
        ).fetchall()
    return [
        Symptom(
            category="plan-tick-spin",
            ref_id=int(r[0]),
            title=_first_line(r[1]),
            detail=(
                f"planner minted {int(r[2])} plan_tick jobs in 24h without "
                f"converging (> {PLAN_TICK_REMINT_24H}); each tick 'succeeds' "
                "but the task never resolves — likely a repo bug blocking the "
                "deliverable, or the task needs splitting"
            ),
        )
        for r in rows
    ]


# ── quest-loop failing (coordinator re-mint spin on real failure) ──


def _detect_quest_loop_failures(store: Store) -> list[Symptom]:
    """Quests whose ``quest_tick`` loop keeps resting ``STATUS:failed`` (RC1).

    A quest's coordinator loop rests ``failed`` only after ``_max_tick_failures``
    consecutive hard failures (``workers/job_types/quest_tick.py``) — a
    persistent break, not a transient blip (``paused`` states don't count). The
    reconciler's RC1 backoff (``quest/loop.py``) now spaces the re-mint
    out to a 30 min → 6 h cadence instead of every worker pass, but it cannot
    *fix* the break — so a genuinely-broken quest keeps failing at the ceiling
    cadence. This counts distinct ``quest_tick`` loops per quest that became
    ``STATUS:failed`` in the last 24h; more than :data:`QUEST_LOOP_FAIL_24H`
    surfaces the quest so a human resolves or abandons it. Mirrors
    ``_detect_plan_tick_spins`` (the same "coordinator succeeds/fails every tick
    but the work never resolves" shape, on the quest lane).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.parent_id, q.title, count(*)::int AS n,
                   max(rt.created_at) AS last_failed
              FROM refs j
              JOIN refs q ON q.ref_id = j.parent_id
              JOIN ref_tags rt ON rt.ref_id = j.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE j.kind = 'job'
               AND j.deleted_at IS NULL
               AND j.meta->>'job_type' = 'quest_tick'
               AND j.parent_id IS NOT NULL
               AND q.deleted_at IS NULL
               AND t.namespace = 'STATUS'
               AND t.value = 'failed'
               AND rt.created_at > now() - interval '24 hours'
             GROUP BY j.parent_id, q.title
            HAVING count(*) > %s
             ORDER BY count(*) DESC
             LIMIT 50
            """,
            (QUEST_LOOP_FAIL_24H,),
        ).fetchall()
    return [
        Symptom(
            category="quest-loop-failing",
            ref_id=int(r[0]),
            title=_first_line(r[1]),
            detail=(
                f"quest #{int(r[0])}'s quest_tick loop rested STATUS:failed "
                f"{int(r[2])}× in 24h (> {QUEST_LOOP_FAIL_24H}, last "
                f"{_hours_since(r[3]):.0f}h ago) — a persistent break the RC1 "
                "backoff is throttling but can't fix: check the "
                "loop's job_summary / job_event chunks for the failing tick "
                "(bad tier/endpoint config, a code error, or spark down), then "
                "fix the cause or pause the quest"
            ),
        )
        for r in rows
    ]


# ── orphaned coordinator (dark: zero re-mint attempts) ─────────────


def _detect_orphaned_coordinator(store: Store) -> list[Symptom]:
    """Active coordinators whose newest loop failed and nothing re-minted.

    ``_detect_quest_loop_failures`` / ``_detect_plan_tick_spins`` both need
    *repeated* re-mints (a count over a threshold) to fire — they catch a
    spin, not a silence. A reconciler that stopped running entirely (the
    2026-07-26→30 melchior worker-agent outage) produces exactly one failed
    row and then nothing, forever — invisible to either counter. This
    detector instead asks, per **active** coordinator lineage: is the
    *newest* loop terminal ``STATUS:failed``, and has it been that way
    longer than :data:`ORPHANED_COORDINATOR_STALE_HOURS`? Since "newest"
    is definitionally the most recent job in the lineage, a failed newest
    row already means no live/queued replacement exists — a fresh mint
    would itself be the newest row instead.

    Two coordinator lineages, unioned:

    * **quest lane** — an active quest (``STATUS:active``) grouped by its
      ``quest_tick`` / ``executor='coordinator'`` loop history.
    * **planner lane** — an open/doing todo with ``meta.llm_tier`` set (the
      planner-coroutine signature ``_candidate_parent_ids`` also keys on)
      grouped by its ``plan_tick`` child-job history. Parents already
      carrying a hard-block tag (``halt`` / ``halt:*`` / ``child-failed:*``)
      are excluded — those are already surfaced (``child-failed-parked``,
      ``halt:*`` on ``view='attention'``); this detector is for the case
      where *nothing* flagged it at all, e.g. an infra-class failure inside
      the bounded-retry window (:mod:`precis.handlers._job_bubble`) whose
      retry never got a chance to run because dispatch itself is dark.

    Degrade-safe: the two-lineage query is more involved than the sibling
    single-table scans, so a read failure here (bad state, a schema drift)
    is caught and logged rather than raised — ``run_pass`` (``workers/
    runner.py``) wraps a whole ref-pass in try/except, so an uncaught
    exception here would silently skip *every other* detector this cycle
    too; catching locally keeps this detector's blast radius to itself.
    """
    try:
        return _detect_orphaned_coordinator_unsafe(store)
    except Exception:
        log.warning("nursery: _detect_orphaned_coordinator raised", exc_info=True)
        return []


def _detect_orphaned_coordinator_unsafe(store: Store) -> list[Symptom]:
    with store.pool.connection() as conn:
        quest_rows = conn.execute(
            """
            WITH newest AS (
                SELECT DISTINCT ON (j.parent_id)
                       j.parent_id AS coord_id, j.ref_id AS job_id,
                       t.value AS status, rt.created_at AS status_at
                  FROM refs j
                  JOIN ref_tags rt ON rt.ref_id = j.ref_id
                  JOIN tags t ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
                 WHERE j.kind = 'job' AND j.deleted_at IS NULL
                   AND j.meta->>'job_type' = 'quest_tick'
                   AND j.meta->>'executor' = 'coordinator'
                   AND j.parent_id IS NOT NULL
                 ORDER BY j.parent_id, j.ref_id DESC
            )
            SELECT n.coord_id, q.title, n.job_id, n.status_at
              FROM newest n
              JOIN refs q ON q.ref_id = n.coord_id
             WHERE q.kind = 'quest' AND q.deleted_at IS NULL
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = q.ref_id
                      AND t.namespace = 'STATUS' AND t.value = 'active'
               )
               AND n.status = 'failed'
               AND n.status_at < now() - %(stale)s::interval
             ORDER BY n.coord_id
             LIMIT 50
            """,
            {"stale": f"{ORPHANED_COORDINATOR_STALE_HOURS} hours"},
        ).fetchall()
        planner_rows = conn.execute(
            """
            WITH newest AS (
                SELECT DISTINCT ON (j.parent_id)
                       j.parent_id AS coord_id, j.ref_id AS job_id,
                       t.value AS status, rt.created_at AS status_at
                  FROM refs j
                  JOIN ref_tags rt ON rt.ref_id = j.ref_id
                  JOIN tags t ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
                 WHERE j.kind = 'job' AND j.deleted_at IS NULL
                   AND j.meta->>'job_type' = 'plan_tick'
                   AND j.parent_id IS NOT NULL
                 ORDER BY j.parent_id, j.ref_id DESC
            )
            SELECT n.coord_id, p.title, n.job_id, n.status_at
              FROM newest n
              JOIN refs p ON p.ref_id = n.coord_id
             WHERE p.kind = 'todo' AND p.deleted_at IS NULL
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = p.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) IN ('open', 'doing')
               AND (p.meta ? 'llm_tier')
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = p.ref_id AND t.namespace = 'OPEN'
                      AND (
                           t.value = 'halt'
                        OR t.value LIKE 'halt:%%'
                        OR t.value LIKE 'child-failed:%%'
                      )
               )
               AND n.status = 'failed'
               AND n.status_at < now() - %(stale)s::interval
             ORDER BY n.coord_id
             LIMIT 50
            """,
            {"stale": f"{ORPHANED_COORDINATOR_STALE_HOURS} hours"},
        ).fetchall()

    out: list[Symptom] = []
    for r in quest_rows:
        coord_id, title, job_id, status_at = int(r[0]), r[1], int(r[2]), r[3]
        dark_h = _hours_since(status_at)
        out.append(
            Symptom(
                category="orphaned-coordinator",
                ref_id=coord_id,
                title=_first_line(title),
                detail=(
                    f"quest #{coord_id} is active but its quest_tick loop's "
                    f"newest job (#{job_id}) rested STATUS:failed "
                    f"{dark_h:.1f}h ago (> {ORPHANED_COORDINATOR_STALE_HOURS}h "
                    "RC1 backoff ceiling) with no newer loop minted — the "
                    "reconciler that should have re-armed it isn't running "
                    "(check the worker-agent daemon), not merely backing off"
                ),
            )
        )
    for r in planner_rows:
        coord_id, title, job_id, status_at = int(r[0]), r[1], int(r[2]), r[3]
        dark_h = _hours_since(status_at)
        out.append(
            Symptom(
                category="orphaned-coordinator",
                ref_id=coord_id,
                title=_first_line(title),
                detail=(
                    f"todo #{coord_id} is an active planner but its plan_tick "
                    f"lineage's newest job (#{job_id}) rested STATUS:failed "
                    f"{dark_h:.1f}h ago (> {ORPHANED_COORDINATOR_STALE_HOURS}h) "
                    "with no newer child minted and no child-failed/halt tag — "
                    "likely an infra retry (handlers/_job_bubble.py) that "
                    "never got a dispatch pass to act on it; check whether "
                    "dispatch is running"
                ),
            )
        )
    return out


# ── worker health (daemon liveness, not the todo graph) ───────────


def _restart_storm_detail(process: str, host: str, n: int, platform: str | None) -> str:
    """Hedged, OS-aware body for a worker-restart finding.

    The detector counts boot rows; it cannot see *why* a daemon restarted,
    so it must not assert a cause. A restart storm is one of two things and
    the count alone can't tell them apart: (a) repeated **deploy bounces** (a
    burst of `/go` ship+deploys, each of which restarts every worker
    unconditionally — the spark 2026-07-06 case), or (b) a **crash / OOM
    cull** (the melchior jetsam case). The old text hardcoded "not a deploy
    bounce … macOS jetsam" + a `launchctl` command, which was flatly wrong on
    a Linux host. ``platform`` (from the boot row payload; NULL on pre-fix
    boots) selects the right diagnostic command.
    """
    base = (
        f"{process} on {host} relaunched {n} times in the last hour "
        f"(> {WORKER_RESTART_STORM_1H}) — a restart storm. Each kill mid-job "
        "orphans in-flight work (swept + re-run at the stuck-job clock). The "
        "boot count can't tell the cause apart: either (a) repeated deploy "
        "bounces (a burst of ship+deploys restarting every worker), or (b) a "
        "crash / OOM cull. "
    )
    if platform == "darwin":
        how = (
            "Diagnose (macOS): `launchctl print system/<label>` for "
            "`immediate reason = inefficient` (jetsam) + wired-RAM pressure; "
            "correlate the boot timestamps with recent deploys."
        )
    elif platform and platform.startswith("linux"):
        how = (
            "Diagnose (Linux): `journalctl -u <unit> -n50` — a clean external "
            "`Stopping→Stopped→Started` cycle is a deploy bounce; a signal / "
            "non-zero exit (or an oom-killer line in `dmesg`) is a crash. "
            "Correlate the boot timestamps with recent deploys."
        )
    else:
        how = (
            "Diagnose: correlate the boot timestamps with recent deploys; on "
            "macOS check `launchctl print` for jetsam + wired-RAM pressure, on "
            "Linux `journalctl -u <unit>` for an external stop vs a signal/OOM "
            "exit."
        )
    return base + how


def _detect_worker_restart_storms(store: Store) -> list[Symptom]:
    """Daemons relaunching abnormally often in the last hour.

    Counts explicit ``worker: started`` boot rows (emitted at
    :func:`precis.cli.worker.run` startup) per ``(host, process)``. A
    count over :data:`WORKER_RESTART_STORM_1H` is a restart storm — either a
    jetsam/OOM cull loop or a deploy-bounce burst; the message hedges the
    cause and tailors the diagnostic command to the host's ``platform`` (read
    from the boot row payload). Forward-looking: only fires once the
    boot-row-emitting build is deployed and a worker actually thrashes.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT host, process, count(*)::int AS n,
                   max(payload->>'platform') AS platform
              FROM worker_logs
             WHERE message = 'worker: started'
               AND process IS NOT NULL
               AND ts > now() - interval '1 hour'
             GROUP BY host, process
            HAVING count(*) > %s
             ORDER BY count(*) DESC
             LIMIT 50
            """,
            (WORKER_RESTART_STORM_1H,),
        ).fetchall()
    return [
        Symptom(
            category="worker-restart",
            ref_id=None,
            fingerprint_key=f"worker-restart:{r[0]}:{r[1]}",
            title=f"{r[1]} on {r[0]} restarted {int(r[2])}× in 1h",
            detail=_restart_storm_detail(str(r[1]), str(r[0]), int(r[2]), r[3]),
        )
        for r in rows
    ]


def _platform_recovery_hint(platform: str | None) -> str:
    """OS-aware daemon-restart command, shared by dead-worker + host-dark.

    ``platform`` comes from ``host_heartbeat.meta->>'platform'``
    (``platform.system()`` — ``"Darwin"``/``"Linux"``, title-cased),
    normalized to lower-case before comparing so both naming conventions
    land the same branch. Factored out of :func:`_dead_worker_detail`
    (gr180078) so :func:`_host_dark_detail` (gr186752) doesn't duplicate
    the OS hedge.
    """
    norm = (platform or "").lower()
    if norm == "darwin":
        return "Recover (macOS): `launchctl kickstart -k system/<label>`."
    if norm.startswith("linux"):
        return "Recover (Linux): `systemctl restart <unit>`."
    return (
        "Recover: `launchctl kickstart -k system/<label>` on macOS, "
        "`systemctl restart <unit>` on Linux."
    )


def _dead_worker_detail(
    process: str, host: str, silent_h: float, platform: str | None
) -> str:
    """Hedged, OS-aware body for a dead-worker finding (gr180078).

    Mirrors :func:`_restart_storm_detail`'s platform hedge — the old text
    hardcoded ``launchctl kickstart -k``, which is meaningless on the
    Linux/systemd GPU node. ``platform`` here comes from
    ``host_heartbeat.meta->>'platform'`` (``platform.system()`` —
    ``"Darwin"``/``"Linux"``, title-cased, unlike the boot row's
    ``sys.platform``), normalized to lower-case before comparing so both
    naming conventions land the same branch.
    """
    base = (
        f"{process} on {host} has written no log for {silent_h:.1f}h "
        f"(> {DEAD_WORKER_SILENCE_MIN}min) while the host is otherwise alive "
        "— the daemon is dead or wedged. If it is the agent worker, "
        "plan_tick / claude_inproc jobs stall cluster-wide. "
    )
    return (
        base
        + _platform_recovery_hint(platform)
        + " Or: `scripts/restart-worker-and-watch` (OS-agnostic)."
    )


def _host_dark_detail(host: str, silent_h: float, platform: str | None) -> str:
    """Hedged, OS-aware body for a host-dark finding (gr186752).

    The complement of :func:`_dead_worker_detail`: since the heartbeat pass
    runs *inside* the same per-host worker it reports on, a dead
    single-writer host takes its own liveness signal down with it and drops
    out of ``dead-worker``'s ``host_alive`` gate (by design, so one dead
    host doesn't fan out into one alert per daemon it ran) — this is the one
    alert that case still needs. ``platform`` comes from the stale
    heartbeat row's own ``meta->>'platform'``, which persists even though
    the row itself has gone stale.
    """
    base = (
        f"{host}'s host_heartbeat has been silent {silent_h:.1f}h "
        f"(> {HOST_DARK_SILENCE_MIN}min) — the host itself looks dark, not "
        "just one daemon: the heartbeat pass runs inside the same worker "
        "process it reports on, so a dead single-writer host takes its own "
        "liveness signal down with it (gr186752). Every daemon on this host "
        "is presumed dead until the heartbeat resumes. "
    )
    return (
        base
        + _platform_recovery_hint(platform)
        + " Or: `scripts/restart-worker-and-watch` (OS-agnostic)."
    )


def _detect_dead_workers(store: Store) -> list[Symptom]:
    """Continuous daemons that have gone silent while their host is up.

    A worker in :data:`WORKER_CONTINUOUS_PROCESSES` that has written no
    ``worker_logs`` row for :data:`DEAD_WORKER_SILENCE_MIN` minutes is
    dead or wedged (a live one logs every loop). Gated on the host still
    being alive — some other process on it logged recently, or its
    ``host_heartbeat`` is fresh — so a whole-host / DB outage doesn't
    fan out into one false "dead worker" per daemon (that is a different,
    single failure). The :data:`DEAD_WORKER_LOOKBACK_DAYS` floor scopes it
    to daemons seen within log retention so a decommissioned worker
    eventually stops alarming — but is wide enough (30d) that a real
    multi-day outage never ages out and self-resolves (gr176223).

    ``platform`` (gr180078) prefers the freshest ``host_heartbeat.meta`` for
    the host — heartbeat is its own self-throttled pass, independent of the
    dead daemon, so it's the most reliable live signal for "what OS is this
    host" even while the daemon in question is silent.

    ``host_alive`` (gr186752): since §A/§L, heartbeat is written by the
    heartbeat worker *pass* (``workers/heartbeat.py``, system profile, 60s
    in-proc throttle) running inside the very worker process it reports
    on — it is NOT an independent watchdog on a separate lease. So on a
    single-worker host, a dead worker takes ``host_heartbeat`` down with it
    too: the host drops out of this CTE and every ``dead-worker`` finding
    for it self-suppresses — by design, one host-level fact shouldn't fan
    out into N per-daemon alerts. :func:`_detect_host_dark` is the deliberate
    complement that still raises exactly one critical for that case (a stale
    ``host_heartbeat`` row itself, not gated on any *other* liveness signal).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH last_seen AS (
                SELECT host, process, max(ts) AS last_ts
                  FROM worker_logs
                 WHERE process = ANY(%(procs)s)
                   AND ts > now() - (%(lookback_days)s || ' days')::interval
                 GROUP BY host, process
            ),
            host_alive AS (
                SELECT host FROM worker_logs
                 WHERE ts > now() - interval '3 minutes'
                 GROUP BY host
                UNION
                SELECT host FROM host_heartbeat
                 WHERE ts > now() - interval '3 minutes'
            ),
            host_platform AS (
                SELECT DISTINCT ON (host) host, meta->>'platform' AS platform
                  FROM host_heartbeat
                 ORDER BY host, ts DESC
            )
            SELECT ls.host, ls.process, ls.last_ts, hp.platform
              FROM last_seen ls
              LEFT JOIN host_platform hp ON hp.host = ls.host
             WHERE ls.last_ts < now() - (%(silence_min)s || ' minutes')::interval
               AND ls.host IN (SELECT host FROM host_alive)
             ORDER BY ls.last_ts ASC
             LIMIT 50
            """,
            {
                "procs": list(WORKER_CONTINUOUS_PROCESSES),
                "silence_min": DEAD_WORKER_SILENCE_MIN,
                "lookback_days": DEAD_WORKER_LOOKBACK_DAYS,
            },
        ).fetchall()
    out: list[Symptom] = []
    for host, process, last_ts, platform in rows:
        silent = _hours_since(last_ts)
        out.append(
            Symptom(
                category="dead-worker",
                ref_id=None,
                fingerprint_key=f"dead-worker:{host}:{process}",
                title=f"{process} on {host} silent {silent:.1f}h",
                detail=_dead_worker_detail(str(process), str(host), silent, platform),
            )
        )
    return out


def _detect_nas_denied(store: Store) -> list[Symptom]:
    """Hosts whose latest heartbeat reports the NAS unreadable from the
    launchd context — every launchd/cron daemon there is locked out of
    /opt/nas. Gated on a fresh (<5 min) heartbeat so a stale row (host/DB
    outage — a different failure) doesn't linger as a false NAS alert.
    Root cause is almost always a Full Disk Access grant broken by a brew
    python upgrade; see OPEN-ITEMS 'melchior daemon NAS lockout'.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT host, meta->>'nas_path' AS path, meta->>'nas_errno' AS errno
              FROM host_heartbeat
             WHERE ts > now() - interval '5 minutes'
               AND meta->>'nas_ok' = 'false'
             ORDER BY host
             LIMIT 50
            """
        ).fetchall()
    out: list[Symptom] = []
    for host, path, _errno in rows:
        out.append(
            Symptom(
                category="nas-denied",
                ref_id=None,
                fingerprint_key=f"nas-denied:{host}",
                title=f"{host} launchd context locked out of the NAS",
                detail=(
                    f"{host}: EPERM reading {path or '/opt/nas/botshome'} from the "
                    "heartbeat's launchd context — every launchd/cron daemon on "
                    f"{host} is denied NAS access (ingest/fetch stalled). Almost "
                    "always a Full Disk Access grant that broke when `brew upgrade` "
                    "changed the venv python's cdhash. Re-grant FDA to the resolved "
                    "python in System Settings > Full Disk Access, then "
                    "`launchctl kickstart -k` the precis daemons. "
                    "See OPEN-ITEMS 'melchior daemon NAS lockout'."
                ),
            )
        )
    return out


def _detect_host_dark(store: Store) -> list[Symptom]:
    """Hosts whose ``host_heartbeat`` itself has gone stale (gr186752).

    The complement of ``dead-worker``'s ``host_alive`` gate (see that
    function's docstring): a stale heartbeat row IS the host-dark signal —
    no additional "is anything else alive" gate, since heartbeat going
    silent while its own host is up would mean the heartbeat pass alone
    died, which ``dead-worker`` already can't see either (heartbeat isn't in
    ``WORKER_CONTINUOUS_PROCESSES``) and is the exact scenario this
    detector exists to surface. Bounded to hosts with any ``worker_logs``
    row in the last :data:`HOST_DARK_LOOKBACK_DAYS` so a decommissioned
    host — whose ``host_heartbeat`` UPSERT row lingers forever — ages out
    instead of alarming critical forever.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT hh.host, hh.ts, hh.meta->>'platform' AS platform
              FROM host_heartbeat hh
             WHERE hh.ts < now() - (%(silence)s || ' minutes')::interval
               AND EXISTS (
                   SELECT 1 FROM worker_logs wl
                    WHERE wl.host = hh.host
                      AND wl.ts > now() - (%(lookback)s || ' days')::interval
               )
             ORDER BY hh.ts ASC
             LIMIT 50
            """,
            {
                "silence": HOST_DARK_SILENCE_MIN,
                "lookback": HOST_DARK_LOOKBACK_DAYS,
            },
        ).fetchall()
    out: list[Symptom] = []
    for host, ts, platform in rows:
        silent = _hours_since(ts)
        out.append(
            Symptom(
                category="host-dark",
                ref_id=None,
                fingerprint_key=f"host-dark:{host}",
                title=f"{host} host_heartbeat silent {silent:.1f}h",
                detail=_host_dark_detail(str(host), silent, platform),
            )
        )
    return out


def _detect_dispatch_stalls(store: Store) -> list[Symptom]:
    """The single agent-profile executor stopped claiming — planner dark.

    ``dispatch`` mints ``plan_tick`` (and other ``claude_inproc``) jobs on
    every node, but only melchior's ``--profile=agent`` worker can *execute*
    them (it needs the hermes ``~/.claude`` OAuth / MCP state). So the
    executor is a cluster-wide single point of failure: if it is culled,
    401s, or never starts, every minted job sits ``STATUS:queued`` forever
    with no ``child-failed`` bubble, and other nodes' dispatch skips those
    parents (they already have a live child) — silent freeze (gripe 55748).

    Fire iff **work is waiting and nothing is running**: at least one
    ``claude_inproc`` job queued past :data:`DISPATCH_STALL_MINUTES` while
    zero jobs hold a live lease. The "nothing running" gate is what
    distinguishes a dead executor from a healthy-but-backlogged one — a
    live executor grinding a long tick shows a running job with an unexpired
    ``meta.lease_until``, so a deep queue behind it does **not** alarm. A
    single non-ref-scoped critical alert (one page, not one per stuck job);
    it auto-resolves the moment the queue drains.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            WITH inproc AS (
                SELECT j.created_at, j.meta,
                       (SELECT t.value
                          FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                         WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS'
                         LIMIT 1) AS status
                  FROM refs j
                 WHERE j.kind = 'job'
                   AND j.deleted_at IS NULL
                   AND j.meta->>'executor' = 'claude_inproc'
            )
            SELECT
                count(*) FILTER (
                    WHERE status = 'queued'
                      AND created_at < now() - (%(mins)s || ' minutes')::interval
                )::int AS n_stalled,
                min(created_at) FILTER (
                    WHERE status = 'queued'
                      AND created_at < now() - (%(mins)s || ' minutes')::interval
                ) AS oldest,
                count(*) FILTER (
                    WHERE status = 'running'
                      AND (meta->>'lease_until') IS NOT NULL
                      AND (meta->>'lease_until')::timestamptz > now()
                )::int AS n_running
              FROM inproc
            """,
            {"mins": DISPATCH_STALL_MINUTES},
        ).fetchone()

    if row is None:
        return []
    n_stalled, oldest, n_running = int(row[0]), row[1], int(row[2])
    # Work is waiting (queued past threshold) AND nothing is executing.
    if n_stalled == 0 or n_running > 0:
        return []
    oldest_h = _hours_since(oldest)
    return [
        Symptom(
            category="dispatch-stall",
            ref_id=None,
            fingerprint_key="dispatch-stall",
            title=f"planner stalled — {n_stalled} job(s) queued, none running",
            detail=(
                f"{n_stalled} claude_inproc job(s) stuck STATUS:queued (oldest "
                f"{oldest_h:.1f}h, > {DISPATCH_STALL_MINUTES}min) with zero live "
                "claims — the agent-profile executor (melchior) is not claiming: "
                "dead/culled worker, OAuth-401, or the agent profile never "
                "started on any node. Minting is cluster-wide but execution is "
                "melchior-only, so the planner is frozen cluster-wide. Recover: "
                "check melchior's agent worker (`launchctl kickstart -k "
                "system/com.precis.worker.agent`) + `claude` OAuth; longer term, "
                "run the agent profile on a second host or wire local-model "
                "tool-calling so execution isn't single-homed."
            ),
        )
    ]


def _detect_embed_lane_stalled(store: Store) -> list[Symptom]:
    """``embed_batch`` jobs queued with nothing completing — the embed
    lane is down (``docs/backlog/embedder-wedge-hardening.md``).

    The 2026-08-08→10 caspar incident: ``serve-embeddings`` startup dials
    HuggingFace Hub for revision metadata even with weights cached; when
    HF is slow/rate-limited the load hangs forever, and the process stays
    alive throughout — a restart-based watchdog just kicks it into the
    same hang again. Process-exists and even ``/readyz`` checks can pass
    right through this class of wedge, so job *outcomes* are the only
    truthful probe. State is read from ``STATUS:*`` ``ref_tags`` (never
    ``meta->>'last_status'`` — that's a compat mirror, not the source of
    truth).

    Fire iff **work is waiting and nothing is completing**: at least one
    ``embed_batch`` job ``STATUS:queued`` while zero ``embed_batch`` jobs
    transitioned to ``STATUS:succeeded`` in the last
    :data:`EMBED_LANE_STALL_WINDOW_MIN` minutes. ``embed_batch`` jobs are
    bounded (minutes each), so a healthy lane drains a queued backlog well
    inside the window — this does not fire on a merely deep-but-draining
    queue. A single non-ref-scoped critical alert (one page, not one per
    stuck job); auto-resolves the moment a job succeeds.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT
                count(*) FILTER (WHERE t.value = 'queued')::int AS n_queued,
                count(*) FILTER (
                    WHERE t.value = 'succeeded'
                      AND rt.created_at > now() - (%(mins)s || ' minutes')::interval
                )::int AS n_recent_succeeded
              FROM refs j
              JOIN ref_tags rt ON rt.ref_id = j.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE j.kind = 'job'
               AND j.deleted_at IS NULL
               AND j.meta->>'job_type' = 'embed_batch'
               AND t.namespace = 'STATUS'
            """,
            {"mins": EMBED_LANE_STALL_WINDOW_MIN},
        ).fetchone()

    if row is None:
        return []
    n_queued, n_recent_succeeded = int(row[0]), int(row[1])
    if n_queued == 0 or n_recent_succeeded > 0:
        return []
    return [
        Symptom(
            category="embed-lane-stalled",
            ref_id=None,
            fingerprint_key="embed-lane-stalled",
            title=f"embed lane stalled — {n_queued} embed_batch job(s) queued, 0 succeeded",
            detail=(
                f"{n_queued} embed_batch job(s) STATUS:queued with zero "
                f"STATUS:succeeded transitions in the last "
                f"{EMBED_LANE_STALL_WINDOW_MIN} minutes — the embed lane is "
                "down even though the embedder process (and possibly "
                "/readyz) can look fine, e.g. a load wedged dialing "
                "HuggingFace Hub. Check com.precis.embedder / "
                "precis-embedder's recent logs for a hung model load; "
                "restart alone may not clear it if the network condition "
                "persists."
            ),
        )
    ]


# ── small helpers ─────────────────────────────────────────────────


def _first_line(title: str | None) -> str:
    """Trim to one line for digest readability."""
    if not title:
        return "(no title)"
    head = title.split("\n", 1)[0]
    if len(head) > 80:
        head = head[:80].rstrip() + "…"
    return head


def _hours_since(ts: datetime | None) -> float:
    if ts is None:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds() / 3600.0


def _days_since(ts: datetime | None) -> float:
    return _hours_since(ts) / 24.0


__all__ = [
    "DISPATCH_STALL_MINUTES",
    "HOST_DARK_LOOKBACK_DAYS",
    "HOST_DARK_SILENCE_MIN",
    "LONG_WAIT_DAYS",
    "QUEST_LOOP_FAIL_24H",
    "SPIN_LOOP_EVENTS_24H",
    "STALE_CLAIM_HOURS",
    "STUCK_DOABLE_HOURS",
    "Symptom",
    "run_nursery_pass",
]
