"""Nursery worker — Slice 3 of ``docs/design/todo-tree-plan.md``.

Pattern-matches the todo tree (and the worker fleet) for local
incoherence and raises a ``kind='alert'`` per condition through
:mod:`precis.alerts`. The detectors are SQL-only — no LLM call, no
opus / sonnet budget; Settled decision #5 in the plan ("Nursery model
= sonnet") was written assuming an LLM tier, but the actual detection
rules are deterministic pattern matches that don't need reasoning.

Detector catalogue (each is one SQL query, returns a list of
finding rows):

* **orphans** — open todos that have no ``level:strategic`` ancestor
  (knob #6: strategic invariant; every open leaf must root under
  *some* strategic).
* **stale claims** — leaves carrying ``claimed-by:<x>`` for more
  than ``STALE_CLAIM_HOURS=3`` without a status change. The
  claim's age is read from ``ref_tags.created_at`` — the same
  source ADR 0016 uses for the ingest claim TTL.
* **long waits** — leaves carrying ``waiting-for:*`` for more than
  ``LONG_WAIT_DAYS=7``.
* **stuck doable** — open leaves with no claim, no waiting tag, and
  ``created_at`` older than ``STUCK_DOABLE_HOURS=24``. The
  rotation should have picked these up; if they're still here, the
  doable filter is rejecting them for a reason worth surfacing.
* **stalled recurrings** — ``level:recurring`` refs whose most
  recent spawned child has been open more than the schedule's
  period. The Slice-4 collision-skip leaves the prior tick on the
  queue; without nursery surfacing, the operator can't see why
  ticks have stopped piling up.
* **spin loops** — any ``(ref_id, source)`` emitting more than
  ``SPIN_LOOP_EVENTS_24H`` ``ref_events`` in 24h (a derived-queue
  worker re-claiming the same ref every pass).

Each finding becomes an ``alert`` under ``alert_source =
nursery:<category>``, deduped on ``fingerprint = "<category>:<ref_id>"``
(see :mod:`precis.alerts`). A repeat sighting bumps the alert's
``seen_count``; a finding that disappears auto-resolves its alert on
the next pass (``resolve_stale_alerts`` per source). This replaced the
old per-minute ``kind='memory'`` digest, which conflated ops telemetry
with reflective thought and — because the spin-loop finding set churns
every second — spun on itself writing thousands of near-dup memories a
day. Alerts dedup per *condition* instead, and are surfaced by the
``/alerts`` web tab, not semantic search.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from precis.alerts import notify_critical_alert, raise_alert, resolve_stale_alerts
from precis.handlers._todo_guards import todo_root_sql
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

#: A daemon relaunching more than this many times in an hour is in a
#: restart storm, not a normal deploy bounce (which is one relaunch). The
#: motivating incident: macOS jetsam culling the agent worker ~50-200x/day
#: under memory pressure, orphaning every in-flight plan_tick — invisible
#: for 1.5 days because nothing watched daemon health. A healthy daemon
#: boots once and runs; even a busy deploy day is a handful of bounces.
WORKER_RESTART_STORM_1H = 8

#: A *continuously-running* daemon silent (no ``worker_logs`` row) for longer
#: than this is dead or wedged — a healthy worker logs every loop iteration
#: (~2s idle cadence) and the DB handler flushes at least every 5s, so a
#: multi-minute gap while the host is otherwise alive means the process died.
DEAD_WORKER_SILENCE_MIN = 10

#: The ``worker_logs.process`` values that run as long-lived loops and so must
#: never fall silent while their host is up. Periodic one-shots (cron-tick,
#: dream) are excluded — their silence between runs is normal, not a fault.
WORKER_CONTINUOUS_PROCESSES = ("precis-worker", "precis-worker-agent")

#: Per-category alert severity (drives sort + colour on the /alerts
#: tab, and — for ``critical`` — a one-shot Discord push via
#: :func:`notify_critical_alert`). Spin loops and stuck claims/recurrings
#: burn resources or block progress → ``warn``; orphans / long-waits /
#: stuck-doable are hygiene nudges → ``info``. The worker-health detectors
#: are the only ``critical`` ones — a dead or thrashing worker is an
#: outage (the planner stalls cluster-wide), not drift.
_SEVERITY: dict[str, str] = {
    "spin-loop": "warn",
    "plan-tick-spin": "warn",
    "orphan": "info",
    "stale-claim": "warn",
    "long-wait": "info",
    "stuck-doable": "info",
    "stalled-recurring": "warn",
    "worker-restart": "critical",
    "dead-worker": "critical",
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One nursery hit.

    ``ref_id`` + ``category`` is the dedup key for the graph detectors
    (each finding is a specific todo/job ref). The worker-health detectors
    are not ref-scoped — they set ``ref_id=None`` and supply an explicit
    ``fingerprint_key`` (e.g. ``"worker-restart:melchior:precis-worker-agent"``)
    so dedup / auto-resolve still work per (host, process).
    """

    category: str
    ref_id: int | None
    title: str
    detail: str  # one-line human summary for the alert
    fingerprint_key: str | None = None


#: Detectors in catalogue order, each paired with its category. The
#: category is both the alert sub-source (``nursery:<category>``) and
#: the dedup-fingerprint prefix. Each detector self-limits to 50 hits.
_DETECTORS: tuple[tuple[str, Callable[[Store], list[Finding]]], ...] = (
    ("spin-loop", lambda s: _detect_spin_loops(s)),
    ("plan-tick-spin", lambda s: _detect_plan_tick_spins(s)),
    ("orphan", lambda s: _detect_orphans(s)),
    ("stale-claim", lambda s: _detect_stale_claims(s)),
    ("long-wait", lambda s: _detect_long_waits(s)),
    ("stuck-doable", lambda s: _detect_stuck_doable(s)),
    ("stalled-recurring", lambda s: _detect_stalled_recurrings(s)),
    ("worker-restart", lambda s: _detect_worker_restart_storms(s)),
    ("dead-worker", lambda s: _detect_dead_workers(s)),
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
        live: list[str] = []
        for f in findings:
            fp = f.fingerprint_key or f"{f.category}:{f.ref_id}"
            live.append(fp)
            title = f"[{f.category}] {f.title}"
            _ref_id, is_new = raise_alert(
                store,
                source=source,
                fingerprint=fp,
                title=title,
                detail=f.detail,
                severity=severity,
                subject_ref_id=f.ref_id,
            )
            # A *new* critical condition pages once (dead / thrashing
            # worker → planner stall). Bumps of an already-open alert
            # don't re-push, so a standing outage doesn't spam.
            if is_new and severity == "critical":
                notify_critical_alert(title, f.detail)
        raised += len(findings)
        resolved += resolve_stale_alerts(store, source=source, live_fingerprints=live)
    if raised or resolved:
        log.info("nursery: %d alerts raised/refreshed, %d resolved", raised, resolved)
    return BatchResult(handler="nursery", claimed=raised, ok=resolved, failed=0)


# ── orphans ────────────────────────────────────────────────────────


def _detect_orphans(store: Store) -> list[Finding]:
    """Open todos whose ancestor chain has no ``level:strategic`` root.

    Walks ``parent_id`` to the topmost ancestor. If that ancestor
    doesn't carry the ``level:strategic`` open tag, the todo is an
    orphan. Recurring subtrees (under ``level:recurring`` roots) are
    excluded — they're scheduled work, not strategic work, and the
    plan explicitly carves them out of the strategic invariant.
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
                -- ADR 0045: the root is the topmost *todo* — a folder
                -- parent above it is placement, not tree membership.
                SELECT DISTINCT ON (w.ref_id) w.ref_id AS leaf_id,
                       w.root_id
                  FROM walk w
                  JOIN refs r ON r.ref_id = w.root_id
                 WHERE {todo_root_sql("r")}
                 ORDER BY w.ref_id, w.root_id
            )
            SELECT r.ref_id, r.title
              FROM refs r
              JOIN roots rt ON rt.leaf_id = r.ref_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rtg JOIN tags t ON t.tag_id = rtg.tag_id
                       WHERE rtg.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do', 'auto-timeout')
               -- Root is not strategic
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rtg JOIN tags t ON t.tag_id = rtg.tag_id
                    WHERE rtg.ref_id = rt.root_id
                      AND t.namespace = 'OPEN'
                      AND t.value = 'level:strategic'
               )
               -- And not in a recurring subtree (root is not recurring either)
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rtg JOIN tags t ON t.tag_id = rtg.tag_id
                    WHERE rtg.ref_id = rt.root_id
                      AND t.namespace = 'OPEN'
                      AND t.value = 'level:recurring'
               )
             ORDER BY r.ref_id
             LIMIT 50
            """,
        ).fetchall()
    return [
        Finding(
            category="orphan",
            ref_id=int(r[0]),
            title=_first_line(r[1]),
            detail=(
                "open todo with no strategic ancestor — root needs "
                "a ``level:strategic`` tag or this leaf needs to be "
                "re-parented under one"
            ),
        )
        for r in rows
    ]


# ── stale claims ──────────────────────────────────────────────────


def _detect_stale_claims(store: Store) -> list[Finding]:
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
    out: list[Finding] = []
    for r in rows:
        claim = str(r[2])
        hours = _hours_since(r[3])
        out.append(
            Finding(
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


def _detect_long_waits(store: Store) -> list[Finding]:
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
    out: list[Finding] = []
    for r in rows:
        wait = str(r[2])
        days = _days_since(r[3])
        out.append(
            Finding(
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


def _detect_stuck_doable(store: Store) -> list[Finding]:
    """Open leaves with no claim, no wait, created >24h ago.

    These are leaves the doable rotation *could* be picking but isn't.
    Causes are usually: PRIO 10 buried by louder strategics, paused
    ancestor that the operator forgot about, or a tag mistake. The
    digest can't diagnose; it just surfaces the existence so the
    operator notices.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, r.created_at
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
               -- No claim, no wait, no asking
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND (t.value LIKE 'claimed-by:%%'
                           OR t.value LIKE 'waiting-for:%%'
                           OR t.value = 'ask-user'
                           OR t.value LIKE 'ask-user:%%'
                           OR t.value = 'level:recurring')
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
        Finding(
            category="stuck-doable",
            ref_id=int(r[0]),
            title=_first_line(r[1]),
            detail=(
                f"doable for {_hours_since(r[2]):.0f}h with no claim, no wait, "
                f"no blocker — check the strategic rotation or its PRIO"
            ),
        )
        for r in rows
    ]


# ── stalled recurrings ────────────────────────────────────────────


def _detect_stalled_recurrings(store: Store) -> list[Finding]:
    """``level:recurring`` refs whose most recent spawned child has been
    open more than ~1.5x the recurring's natural cadence.

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
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = rec.ref_id
                      AND t.namespace = 'OPEN'
                      AND t.value = 'level:recurring'
               )
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
        Finding(
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


def _detect_spin_loops(store: Store) -> list[Finding]:
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
    out: list[Finding] = []
    for r in rows:
        ref_id, source, last_event, n = int(r[0]), str(r[1]), r[2], int(r[3])
        out.append(
            Finding(
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


def _detect_plan_tick_spins(store: Store) -> list[Finding]:
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
        Finding(
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


# ── worker health (daemon liveness, not the todo graph) ───────────


def _detect_worker_restart_storms(store: Store) -> list[Finding]:
    """Daemons relaunching abnormally often in the last hour.

    Counts explicit ``worker: started`` boot rows (emitted at
    :func:`precis.cli.worker.run` startup) per ``(host, process)``. A
    count over :data:`WORKER_RESTART_STORM_1H` is a restart storm — the
    signature of the jetsam-cull loop that orphaned plan_ticks for 1.5
    days with nothing watching. Forward-looking: only fires once the
    boot-row-emitting build is deployed and a worker actually thrashes.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT host, process, count(*)::int AS n
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
        Finding(
            category="worker-restart",
            ref_id=None,
            fingerprint_key=f"worker-restart:{r[0]}:{r[1]}",
            title=f"{r[1]} on {r[0]} restarted {int(r[2])}× in 1h",
            detail=(
                f"{r[1]} on {r[0]} relaunched {int(r[2])} times in the last "
                f"hour (> {WORKER_RESTART_STORM_1H}) — a restart storm, not a "
                "deploy bounce. Likely macOS jetsam / OOM culling it under "
                "memory pressure; each kill mid-job orphans in-flight work. "
                "Check `launchctl print` for `immediate reason = inefficient` "
                "and the host's wired-RAM pressure."
            ),
        )
        for r in rows
    ]


def _detect_dead_workers(store: Store) -> list[Finding]:
    """Continuous daemons that have gone silent while their host is up.

    A worker in :data:`WORKER_CONTINUOUS_PROCESSES` that has written no
    ``worker_logs`` row for :data:`DEAD_WORKER_SILENCE_MIN` minutes is
    dead or wedged (a live one logs every loop). Gated on the host still
    being alive — some other process on it logged recently, or its
    ``host_heartbeat`` is fresh — so a whole-host / DB outage doesn't
    fan out into one false "dead worker" per daemon (that is a different,
    single failure). The 24h floor scopes it to daemons seen recently, so
    a decommissioned worker doesn't alarm forever.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH last_seen AS (
                SELECT host, process, max(ts) AS last_ts
                  FROM worker_logs
                 WHERE process = ANY(%(procs)s)
                   AND ts > now() - interval '24 hours'
                 GROUP BY host, process
            ),
            host_alive AS (
                SELECT host FROM worker_logs
                 WHERE ts > now() - interval '3 minutes'
                 GROUP BY host
                UNION
                SELECT host FROM host_heartbeat
                 WHERE ts > now() - interval '3 minutes'
            )
            SELECT ls.host, ls.process, ls.last_ts
              FROM last_seen ls
             WHERE ls.last_ts < now() - (%(silence_min)s || ' minutes')::interval
               AND ls.host IN (SELECT host FROM host_alive)
             ORDER BY ls.last_ts ASC
             LIMIT 50
            """,
            {
                "procs": list(WORKER_CONTINUOUS_PROCESSES),
                "silence_min": DEAD_WORKER_SILENCE_MIN,
            },
        ).fetchall()
    out: list[Finding] = []
    for host, process, last_ts in rows:
        silent = _hours_since(last_ts)
        out.append(
            Finding(
                category="dead-worker",
                ref_id=None,
                fingerprint_key=f"dead-worker:{host}:{process}",
                title=f"{process} on {host} silent {silent:.1f}h",
                detail=(
                    f"{process} on {host} has written no log for "
                    f"{silent:.1f}h (> {DEAD_WORKER_SILENCE_MIN}min) while the "
                    "host is otherwise alive — the daemon is dead or wedged. "
                    "If it is the agent worker, plan_tick / claude_inproc jobs "
                    "stall cluster-wide; `launchctl kickstart -k` to recover."
                ),
            )
        )
    return out


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
    "LONG_WAIT_DAYS",
    "SPIN_LOOP_EVENTS_24H",
    "STALE_CLAIM_HOURS",
    "STUCK_DOABLE_HOURS",
    "Finding",
    "run_nursery_pass",
]
