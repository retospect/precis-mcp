"""``health_digest`` — the slow-rot liveness net.

Design: ``docs/backlog/self-healing-spine.md`` Layer 2. Nursery owns the
*critical* page-immediately lane; this pass owns the slower "quiet for
suspiciously long" lane — info/warn only, pushed (not polled).

**SQL-first, zero LLM** — deterministic queries + a pure-template body (an
LLM-fleet outage is one of the things this must still report); no ``llm``
import anywhere, enforced by ``tests/workers/test_health_digest.py``.

:func:`run_health_digest_pass`, per fire:

1. **Evaluate every check** (:func:`_evaluate_checks`), three sources:
   * **Curated Layer-1 outcome checks** (:data:`_FRESHNESS_CHECKS` + bespoke
     functions) — ~15 end-to-end outcomes with budgets. ``chunks_embedded``/
     ``chunks_keyworded`` are **idle-aware** (empty backlog = healthy
     regardless of last-run age; a non-draining one past budget isn't),
     reusing :func:`precis.health_checks.compute_backlog_counts` — same
     computation ``/status`` renders. ``chunks_classified`` is idle-aware on
     the ``service_config`` gate instead: :func:`_classify_gate_enabled`
     (via :func:`_resolve_enabled_somewhere`, shared with Layer-2) reads
     "disabled by config" rather than stale-forever when the default-OFF
     ``classify`` pass is off. Gated-on but past budget, it's ALSO
     idle-aware on the eligible pool (gr331492): :func:`_classify_pool_empty`
     mirrors ``classify.py``'s own claim predicate inline and reads
     "backlog drained" once the corpus backfill completes, rather than
     re-filing a pipeline-stuck gripe against a pass that finished its work.
     ``lanes_idle_beside_queue`` flags a job executor with old queued work
     and no claim in an hour (:func:`precis.health_checks.idle_lanes`).
     ``tool_latency`` / ``tool_latency_regressed`` read the ``tool_calls``
     ledger (:func:`precis.health_checks.tool_latency`, the rollup
     ``precis-status`` shows): a verb/kind p95 over its budget, or more
     than double its trailing-week p95.
   * **Cadence staleness** (:func:`_cadence_staleness_checks`, derived) —
     every ``scheduler_leases`` row overdue past its *live*-resolved
     interval + margin; zero per-cadence config, so a new
     ``workers/scheduler.py`` cadence is watched automatically. Plus
     **never-seeded**: a registry cadence whose ``eligible`` gate
     short-circuits :func:`~precis.workers.scheduler.run_scheduler_pass`
     before ``claim_scheduler_lease`` never seeds a lease row — any
     :data:`CADENCES` entry (outside the ``materialize`` exemption) with no
     lease row anywhere flags ``<name>/never-seeded``.
   * **Layer-2 coherence** (:func:`_layer2_checks`, derived) — every
     registered ``PASS``/``ref_pass`` :class:`~precis.workers.registry.ServiceSpec`
     resolving enabled (structural ``default_profiles`` or a live
     ``service_config`` prio override) with zero ``worker_logs`` in 24h
     reads "intended-on but silent". Derived from registry ×
     ``service_config`` × ``worker_logs`` — a new pass needs zero edits here.
2. **Findings → alerts** (:func:`_sync_alerts`) — each non-``ok`` check
   raises ``kind='alert'`` under ``alert_source="watchdog:<group>"``, capped
   info/warn (nursery keeps critical); ``resolve_stale_alerts`` auto-closes
   whatever went fresh again, including a group with zero findings this eval.
   The fingerprint is the check's *pinned* failure id
   (:data:`_PINNED_FINGERPRINTS` → :func:`check_fingerprint`), registered
   in :mod:`precis.alert_ids`, not ``CheckResult.name`` — so renaming a
   check never mints a new id. The full check set is also persisted as a
   JSON snapshot (:data:`CHECKS_SNAPSHOT_KEY`) that :func:`health_panel`
   — rendered by ``get(kind='alert', id='/health')`` and the ``/status``
   Health sub-tab alike — reads with its
   age instead of re-running the ~30 s backlog aggregates, overlaying the
   cheap freshness probes live.
3. **Remediation router** (:func:`_route_findings`) — a still-open
   ``watchdog:<group>`` alert older than its class's self-heal budget gets
   one condition-linked ``kind='gripe'`` (dedup key: a
   ``watchdog-condition: <source>/<fingerprint>`` marker line, re-scanned
   every eval), auto-closed when the alert clears. Budgets by ``group``:
   ``cadence``→6h, ``coherence``→24h, ``discovery``→12h,
   else→24h-at-warn/never-at-info; ``meta`` (``alert_backlog_rot``) never
   gripes. Flood-capped at 3 new gripes/eval, over-cap retried next eval.
   No auto-restart/auto-fix — filing is the whole act;
   ``backlog_groom`` grooms gripes into ``fix_gripe`` todos.
4. **Embed-pipeline culprit diagnosis** (:func:`_diagnose_embed_pipeline`) —
   pure SQL, only when the ``embed`` backlog check is stale: names the first
   stuck stage in materializer → ``embed_batch`` jobs → ``job_inproc``
   slot-gated claim chain, or "all stages nominal". Feeds the check's
   detail line and the router's gripe body.
5. **Push policy** (:func:`_maybe_push`) — templated digest to
   ``PRECIS_OPS_ALERT_TARGET`` (dark if unset) when the daily heartbeat is
   due (``app_settings['health_digest:last_push']`` > 24h) or a check just
   went ``ok``→not-``ok``. An all-green daily push is the dead-man's proof
   the watchdog itself is alive; a routed finding gets a
   ``⛳ gripe filed:`` prefix.
6. **Dead-man ping** (:func:`_ping_deadman`) — GET
   ``PRECIS_DEADMAN_PING_URL`` (when set, via ``safe_fetch.safe_get``) after
   a successful eval — covers a total fleet/DB outage. SSRF-blocked for a
   private/loopback/LAN target (the guard targets agent-supplied URLs, not
   this operator-set constant) unless ``PRECIS_DEADMAN_ALLOW_PRIVATE=1``
   (:func:`_ping_deadman_private`). See ``docs/runbooks/dead-mans-switch.md``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.workers.doctor_report import DoctorReport

from precis import health_checks
from precis.alerts import (
    ACKED_SQL,
    STATE_OPEN,
    queue_ops_message,
    raise_alert,
    resolve_stale_alerts,
)

# precis.nanopub.stale is the shared llm-free staleness predicate for a
# staged nanopub candidate (:func:`_check_nanopub_candidates_fresh` below);
# see that module's docstring for why ``nanopub.gates`` itself can't be
# imported here.
from precis.nanopub.stale import candidate_stale_reason
from precis.store import Store
from precis.workers.registry import SERVICES, ServiceKind, ServiceSpec
from precis.workers.runner import BatchResult
from precis.workers.scheduler import CADENCES

log = logging.getLogger(__name__)

#: App-settings key holding the ISO-8601 UTC timestamp of the last digest
#: push (heartbeat OR degradation) — the whole "reuse app_settings, don't
#: invent a new throttle table" ask from the spec.
LAST_PUSH_KEY = "health_digest:last_push"

#: App-settings key holding the JSON snapshot of the last eval's full check
#: set (:func:`_store_checks_snapshot`) — what the agent-facing health
#: panel (:func:`health_panel`) reads instead of re-running the ~30 s of
#: backlog aggregates (``docs/backlog/alert-failure-id-registry.md`` D3).
CHECKS_SNAPSHOT_KEY = "health_digest:checks_snapshot"

#: The daily green-heartbeat cadence: push at least this often even when
#: every check is ``ok`` (the dead-man's-switch the push itself IS).
HEARTBEAT_INTERVAL_HOURS = 24.0

#: When set, GET this URL (via ``safe_fetch.safe_get``) after every
#: successful eval — a healthchecks.io-style external dead-man's-switch that
#: survives a total fleet/DB outage (nothing DB-mediated can report that).
#: Dark by default. See ``docs/runbooks/dead-mans-switch.md``.
DEADMAN_PING_URL_ENV = "PRECIS_DEADMAN_PING_URL"

#: Opt-in: set to ``"1"`` to permit a private/loopback/LAN
#: ``PRECIS_DEADMAN_PING_URL`` target — the default SSRF guard is built for
#: agent-supplied (untrusted) URLs and blocks those ranges outright, but a
#: self-hosted dead-man's-switch check often lives on the LAN. Dark by
#: default (blocked, not silently bypassed). See
#: ``docs/runbooks/dead-mans-switch.md``.
DEADMAN_ALLOW_PRIVATE_ENV = "PRECIS_DEADMAN_ALLOW_PRIVATE"

#: Layer-1/2/cadence severities are capped to these — nursery keeps the
#: ``critical`` page lane; this pass is the slow-rot digest.
_INFO = "info"
_WARN = "warn"

#: The ``links.relation`` values a findings→claims taproot evidence edge
#: carries (mirrors ``precis.taproot.hub.HUB_ROLES`` as a local literal, not
#: an import — same reasoning ``precis_web/routes/status.py`` gives for its
#: own ``_SPIN_LOOP_EVENTS_24H`` mirror: this pass must not grow a
#: dependency on the subsystems it watches just to read one threshold/tuple).
_TAPROOT_HUB_ROLES = ("establishes", "corroborates", "contradicts")


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One evaluated check — the common shape every check source returns."""

    group: str
    name: str
    status: str  # "ok" | "stale" | "unknown"
    detail: str
    severity: str  # "info" | "warn" — only meaningful when status != "ok"
    age_hours: float | None = None

    @property
    def is_finding(self) -> bool:
        """``unknown`` (a check's own SQL failed) never raises an alert —
        the digest must not become its own false-alarm source."""
        return self.status == "stale"


def _hours_since(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds() / 3600.0


# ── Layer 1: curated outcome checks (simple freshness) ──────────────────

#: ``(key, group, sql, budget_hours, severity, what)`` — a plain "last
#: activity" probe run in one batch via
#: :func:`precis.health_checks.fetch_freshness_timestamps`. ``what`` is the
#: human phrase used in the rendered detail line. Budgets are the design
#: doc's pulse-probe-seeded values (§A "Layer 1 — Outcomes" of the folded
#: design — see module docstring), not guesses.
_FRESHNESS_CHECKS: tuple[tuple[str, str, str, float, str, str], ...] = (
    (
        "papers_ingested",
        "ingest",
        "SELECT max(created_at) FROM refs WHERE kind = 'paper' AND retired_at IS NULL",
        6.0,
        _WARN,
        "a new paper landing",
    ),
    (
        # gr204385: overridden to a non-finding "disabled by config" result
        # in _layer1_checks() when _classify_gate_enabled() is False — this
        # SQL/budget pair only governs the reading while the classify PASS
        # is actually turned on somewhere. Namespace is 'ROLE3' uppercase —
        # mirrors classify.py's OUTPUT_NAMESPACE (not imported, per this
        # module's don't-depend-on-what-it-watches convention, see
        # _TAPROOT_HUB_ROLES above); `tags.namespace` is CHECK-constrained
        # to `namespace = upper(namespace)`, so a lowercase literal here
        # could never match a row regardless of the gate — a second,
        # independent cause of "stale forever" gr204385 also fixes.
        "chunks_classified",
        "discovery",
        "SELECT max(ct.created_at) FROM chunk_tags ct "
        "JOIN tags t ON t.tag_id = ct.tag_id WHERE t.namespace = 'ROLE3'",
        12.0,
        _WARN,
        "a chunk classified (role3)",
    ),
    (
        "news",
        "ingest",
        "SELECT max(created_at) FROM refs WHERE kind = 'news' AND retired_at IS NULL",
        8.0,
        _WARN,
        "a news ref landing",
    ),
    (
        "morning_brief_cast",
        "reading",
        "SELECT max(ts) FROM worker_logs WHERE pass = 'briefing'",
        26.0,
        _WARN,
        "the morning briefing pass running",
    ),
    (
        # Probe the runner's structured BatchResult rows, not the
        # module's log lines: ``pass='cast_audio'`` is logger-derived, so
        # *every* ``precis.workers.cast_audio`` line refreshes it —
        # including the ``render failed`` warnings a failing draft emits
        # at least hourly for its whole 48h selection window. On the
        # weekly cast cadence that failure noise alone could keep the
        # check green indefinitely with zero narrations. ``ok>0`` lands
        # only on a publish (``cli/_worker_passes._cast_audio_pass``) —
        # same "last real output" shape the backlog checks' last-done
        # probe uses (``health_checks.compute_backlog_counts``).
        "cast_audio",
        "reading",
        "SELECT ts FROM worker_logs "
        "WHERE pass = 'runner' "
        "AND split_part(payload->>'handler', ':', 1) = 'cast_audio' "
        "AND COALESCE((payload->>'ok')::int, 0) > 0 "
        "ORDER BY ts DESC LIMIT 1",
        26.0,
        _WARN,
        "cast_audio narrating a cast",
    ),
)
# ``taproot_edges`` used to be a row here (bare ``max(links.created_at)``
# over the hub roles, 6h budget). It is now :func:`_check_taproot_edges`,
# input-aware like ``chunks_extracted`` — see that function for why.


#: Slack added to a recurring's longest cron gap: the schedule pass,
#: dispatch and the claude_inproc lane each add minutes between a tick
#: and the first output landing.
_SCHEDULE_SLACK_H = 2.0


def _recurring_budget(
    conn: Any, job_types: tuple[str, ...], floor_hours: float
) -> tuple[float, str]:
    """A freshness budget that follows the live recurring schedules driving
    ``job_types``: the longest gap between consecutive fires of ANY matching
    recurring over the past week plus slack, never below ``floor_hours``.

    The fires are UNIONED across recurrings rather than minimised
    per-schedule, because a lane can have more than one producer —
    ``cast_audio`` narrates both the Monday ``reading_brief`` and the
    Sunday ``meditation`` cast, so its output cadence is the union of the
    two schedules' fires, not either alone. This is the same bug class as
    gr479694 (news_poll moved 30m→daily, its fixed 8h budget then read a
    healthy cadence as an outage) and the 2026-10-12 weekly-cast
    reconcile (al481399/al481520: the reading-loop watches went
    daily→weekly and the fixed 26h budgets read a weekly lane as stale).

    No live (unpaused) recurring keeps the floor, so a removed or paused
    schedule still trips the check. Returns ``(budget, note)``; ``note``
    names the schedules in the detail line.
    """
    from precis.workers.schedule.parse import parse_schedule, ticks_since

    rows = conn.execute(
        """
        SELECT r.meta->'schedule'->>'cron'
          FROM refs r
         WHERE r.kind = 'todo' AND r.retired_at IS NULL
           AND r.meta->>'job_type' = ANY(%s)
           AND r.meta->'schedule' ? 'cron'
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                  WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                    AND t.value = 'paused')
        """,
        (list(job_types),),
    ).fetchall()
    now = datetime.now(UTC)
    fires: list[datetime] = []
    crons: list[str] = []
    for (cron,) in rows:
        try:
            sched = parse_schedule({"cron": cron, "backfill_missed": True})
            fires.extend(ticks_since(now - timedelta(days=7), sched, now=now))
            # The wrap-around gap — last fire to the NEXT fire — is the
            # one that matters for a weekly-or-slower lane: two weekly
            # producers firing close together (the Sunday/Monday casts)
            # would otherwise read their small inter-fire gap as the
            # cadence. ticks_since's ~7-day walk cap bounds both calls.
            nxt = ticks_since(now, sched, now=now + timedelta(days=7))
            if nxt:
                fires.append(min(nxt))
        except Exception:
            log.warning(
                "health_digest: unparseable %s cron %r", "/".join(job_types), cron
            )
            continue
        crons.append(cron)
    if not crons:
        return floor_hours, f", no live {'/'.join(job_types)} schedule"
    fires.sort()
    if len(fires) < 2:
        # Slower than weekly: fewer than two fires inside the 14-day
        # combined walk, so the gap is at least a week.
        gap_h = 7 * 24.0
    else:
        gap_h = max((b - a).total_seconds() for a, b in pairwise(fires)) / 3600
    budget = max(floor_hours, gap_h + _SCHEDULE_SLACK_H)
    which = "/".join(job_types)
    label = "schedule" if len(crons) == 1 else "schedules"
    named = ", ".join(repr(c) for c in sorted(crons))
    return budget, f", {which} {label} {named}"


#: Freshness keys whose budget follows live recurring schedules instead
#: of the fixed floor — the job_types whose recurrings produce the lane's
#: output. ``card_forge`` is not a row in ``_FRESHNESS_CHECKS`` (it probes
#: the job lifecycle, not worker_logs): ``_check_card_forge`` calls
#: :func:`_recurring_budget` with its own job_type directly.
_SCHEDULE_BUDGET_JOBS: dict[str, tuple[str, ...]] = {
    "news": ("news_poll",),
    "cast_audio": ("reading_brief", "meditation"),
}


def _freshness_layer1(conn: Any) -> list[CheckResult]:
    probes = health_checks.fetch_freshness_timestamps(
        conn, [(key, sql) for key, _grp, sql, *_ in _FRESHNESS_CHECKS]
    )
    out: list[CheckResult] = []
    for key, group, _sql, budget_hours, severity, what in _FRESHNESS_CHECKS:
        note = ""
        budget_jobs = _SCHEDULE_BUDGET_JOBS.get(key)
        if budget_jobs is not None:
            try:
                # Savepoint: a failed lookup must not abort the shared
                # connection the remaining Layer-1 checks run on.
                with conn.transaction():
                    budget_hours, note = _recurring_budget(
                        conn, budget_jobs, budget_hours
                    )
            except Exception:
                log.exception("health_digest: %s budget lookup failed", key)
        probe = probes[key]
        if not probe.ok:
            out.append(
                CheckResult(group, key, "unknown", f"{what}: probe failed", severity)
            )
            continue
        age = _hours_since(probe.ts)
        if age is not None and age <= budget_hours:
            out.append(
                CheckResult(
                    group,
                    key,
                    "ok",
                    f"{what}: {age:.1f}h ago (budget {budget_hours:.0f}h{note})",
                    severity,
                    age,
                )
            )
            continue
        seen = f"{age:.1f}h ago" if age is not None else "never"
        out.append(
            CheckResult(
                group,
                key,
                "stale",
                f"{what}: last seen {seen} (budget {budget_hours:.0f}h{note})",
                severity,
                age,
            )
        )
    return out


# ── Layer 1: idle-aware backlog checks (embed / chunk_keywords) ─────────

#: The backlog key naming the embed queue specifically — the only backlog
#: check with a §F culprit diagnosis (:func:`_diagnose_embed_pipeline`)
#: behind it; ``chunk_keywords`` has no analogous stage chain.
_EMBED_BACKLOG_KEY = "embed"

#: ``(backlog_key, group, budget_hours, severity, what)``
_BACKLOG_CHECKS: tuple[tuple[str, str, float, str, str], ...] = (
    (_EMBED_BACKLOG_KEY, "discovery", 2.0, _WARN, "chunks embedded"),
    ("chunk_keywords", "discovery", 6.0, _WARN, "chunks keyworded"),
)


def _idle_aware_backlog_checks(conn: Any) -> list[CheckResult]:
    """Idle-vs-stuck: an empty backlog is healthy no matter how stale the
    last batch is; a non-empty, non-draining one past budget is not.

    Reuses :func:`precis.health_checks.compute_backlog_counts` — the exact
    ``/status`` panel computation, so this is the same "one liveness truth"
    the web UI shows, not a second implementation that could drift.
    """
    backlog = health_checks.compute_backlog_counts(conn)
    out: list[CheckResult] = []
    for key, group, budget_hours, severity, what in _BACKLOG_CHECKS:
        row = backlog.get(key)
        if row is None or row.get("pending", -1) < 0:
            out.append(
                CheckResult(
                    group, key, "unknown", f"{what}: backlog query failed", severity
                )
            )
            continue
        pending = int(row["pending"])
        last_ts = row.get("last_ts")
        age = _hours_since(last_ts)
        if pending <= 0:
            out.append(
                CheckResult(
                    group, key, "ok", f"{what}: 0 pending (caught up)", severity, age
                )
            )
            continue
        if age is not None and age <= budget_hours:
            out.append(
                CheckResult(
                    group,
                    key,
                    "ok",
                    f"{what}: {pending} pending, draining (last batch {age:.1f}h ago)",
                    severity,
                    age,
                )
            )
            continue
        last_seen = f"{age:.1f}h ago" if age is not None else "never"
        detail = (
            f"{what}: {pending} pending, NOT draining "
            f"(last batch {last_seen}, budget {budget_hours:.0f}h)"
        )
        if key == _EMBED_BACKLOG_KEY:
            # §F coupling: name the first stuck stage in the materializer →
            # embed_batch → job_inproc chain right in the check's own
            # detail line (also reused verbatim by the router's
            # pipeline-stuck gripe body — see _router_nudge).
            detail += " — " + _diagnose_embed_pipeline(conn)
        out.append(CheckResult(group, key, "stale", detail, severity, age))
    return out


# ── §F coupling: embed-pipeline culprit diagnosis ────────────────────────

#: ``embed_batch`` is the only job_type this diagnosis reasons about — the
#: materializer → job → job_inproc chain §F actually built.
_DIAGNOSE_JOB_TYPE = "embed_batch"

#: The "recent activity" window for stage 1 (materializer minting) and
#: stage 3 (jobs succeeding/failing) — generous relative to the
#: materializer's 300s cadence and the embed check's own 2h budget, so a
#: single missed tick never misreads as "materializer silent".
_DIAGNOSE_WINDOW_HOURS = 6.0

#: A ``resource_slots`` row for ``embedder`` counts as "live" within this
#: many minutes of ``updated_at`` — mirrors the heartbeat's own 60s cadence
#: with the same order-of-magnitude margin ``_HOSTS_ALIVE_SILENCE_MIN``
#: uses for a host's heartbeat row (a probe hiccup must not misread as "no
#: capacity" the instant it's a few cycles stale).
_DIAGNOSE_SLOT_FRESH_MINUTES = 15


def _diagnose_embed_pipeline(conn: Any) -> str:
    """Name the first stuck stage in the §F materialize → ``embed_batch``
    jobs → ``job_inproc`` slot-gated claim chain — pure SQL, ~one short
    paragraph. Called only when the ``embed`` backlog check is already
    stale (:func:`_idle_aware_backlog_checks`), and by the router
    (:func:`_router_nudge`) when it files the pipeline-stuck gripe for
    that same finding — same string, both places.

    1. **Materializer minting?** Zero ``embed_batch`` jobs minted in the
       window ⇒ "materializer silent" (never logged) or "materializer
       failing: <error>" (its last ``worker_logs`` row was an ERROR).
    2. **Jobs claimable?** Jobs ARE being minted — queued but zero fresh
       ``resource_slots`` row for ``embedder`` fleet-wide ⇒ "no embedder
       capacity advertised (probe/daemon down?)".
    3. **Jobs succeeding?** Claimed jobs all failing, or running with none
       completing in the window ⇒ names that, with the last failure's
       error snippet when available.
    4. None of the above explains it ⇒ "all stages nominal — backlog may
       simply exceed drain rate" (still stale, still filed — honesty over
       false precision).

    Defensive like every other bespoke check in this module: a probe
    failure degrades to a short "unavailable" string rather than raising
    (never mind that its caller's own check has ALREADY gone stale by the
    time this runs — a broken diagnosis must not become a second alarm).
    """
    try:
        return _diagnose_embed_pipeline_inner(conn)
    except Exception:
        log.exception("health_digest: embed-pipeline diagnosis probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception(
                "health_digest: rollback after embed-pipeline diagnosis failed"
            )
        return "embed-pipeline diagnosis unavailable (probe failed)"


def _diagnose_embed_pipeline_inner(conn: Any) -> str:
    # Stage 1 — materializer minting?
    minted_row = conn.execute(
        "SELECT count(*) FROM refs WHERE kind = 'job' AND retired_at IS NULL "
        "AND meta->>'job_type' = %(jt)s "
        "AND created_at > now() - (%(w)s || ' hours')::interval",
        {"jt": _DIAGNOSE_JOB_TYPE, "w": _DIAGNOSE_WINDOW_HOURS},
    ).fetchone()
    minted_n = int(minted_row[0]) if minted_row else 0

    if minted_n == 0:
        last_log = conn.execute(
            "SELECT ts, level, message FROM worker_logs "
            "WHERE pass = 'materialize' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if last_log is None:
            return (
                "materializer silent — no worker_logs rows for pass=materialize "
                f"ever, and 0 {_DIAGNOSE_JOB_TYPE} jobs minted in "
                f"{_DIAGNOSE_WINDOW_HOURS:.0f}h (check the materialize "
                "scheduler cadence / PRECIS_MATERIALIZE_EMBED)"
            )
        ts, level, message = last_log
        age = _hours_since(ts)
        age_s = f"{age:.1f}h ago" if age is not None else "unknown"
        if level in ("ERROR", "CRITICAL"):
            return f"materializer failing: {(message or '')[:300]}"
        return (
            f"materializer silent {age_s} (last logged: "
            f"{(message or '')[:160]!r}), 0 {_DIAGNOSE_JOB_TYPE} jobs minted "
            f"in {_DIAGNOSE_WINDOW_HOURS:.0f}h"
        )

    # Stage 2 — jobs claimable? (jobs ARE minting — is a slot advertised?)
    queued_row = conn.execute(
        """
        SELECT count(*), min(r.created_at)
          FROM refs r
         WHERE r.kind = 'job' AND r.retired_at IS NULL
           AND r.meta->>'job_type' = %(jt)s
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = 'STATUS' AND t.value = 'queued'
               )
        """,
        {"jt": _DIAGNOSE_JOB_TYPE},
    ).fetchone()
    queued_n = int(queued_row[0]) if queued_row and queued_row[0] else 0
    oldest_queued = queued_row[1] if queued_row else None

    slots_row = conn.execute(
        "SELECT "
        "count(*) FILTER (WHERE updated_at > now() - (%(m)s || ' minutes')::interval), "
        "count(*) "
        "FROM resource_slots WHERE resource = 'embedder'",
        {"m": _DIAGNOSE_SLOT_FRESH_MINUTES},
    ).fetchone()
    fresh_slots = int(slots_row[0]) if slots_row and slots_row[0] else 0
    any_slots = int(slots_row[1]) if slots_row and slots_row[1] else 0

    if queued_n > 0 and fresh_slots == 0:
        stale_note = (
            f" ({any_slots} stale row(s) — last probe went unanswered)"
            if any_slots
            else " (no resource_slots row for embedder at all)"
        )
        oldest_age = _hours_since(oldest_queued)
        oldest_s = f"{oldest_age:.1f}h" if oldest_age is not None else "unknown"
        return (
            "no embedder capacity advertised (probe/daemon down?)"
            f"{stale_note} — {queued_n} {_DIAGNOSE_JOB_TYPE} job(s) queued, "
            f"oldest {oldest_s} old"
        )

    # Stage 3 — jobs succeeding?
    status_row = conn.execute(
        """
        SELECT
            count(*) FILTER (WHERE t.value = 'running')::int,
            count(*) FILTER (
                WHERE t.value = 'succeeded'
                  AND rt.created_at > now() - (%(w)s || ' hours')::interval
            )::int,
            count(*) FILTER (
                WHERE t.value = 'failed'
                  AND rt.created_at > now() - (%(w)s || ' hours')::interval
            )::int
          FROM refs r
          JOIN ref_tags rt ON rt.ref_id = r.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE r.kind = 'job' AND r.retired_at IS NULL
           AND r.meta->>'job_type' = %(jt)s
           AND t.namespace = 'STATUS'
        """,
        {"jt": _DIAGNOSE_JOB_TYPE, "w": _DIAGNOSE_WINDOW_HOURS},
    ).fetchone()
    running_n, succeeded_n, failed_n = (
        (int(status_row[0]), int(status_row[1]), int(status_row[2]))
        if status_row
        else (0, 0, 0)
    )

    if failed_n > 0 and succeeded_n == 0:
        err_row = conn.execute(
            """
            SELECT c.text
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
              LEFT JOIN chunks c
                     ON c.ref_id = r.ref_id AND c.chunk_kind = 'job_event'
             WHERE r.kind = 'job' AND r.retired_at IS NULL
               AND r.meta->>'job_type' = %(jt)s
               AND t.namespace = 'STATUS' AND t.value = 'failed'
               AND rt.created_at > now() - (%(w)s || ' hours')::interval
             ORDER BY rt.created_at DESC, c.ord DESC
             LIMIT 1
            """,
            {"jt": _DIAGNOSE_JOB_TYPE, "w": _DIAGNOSE_WINDOW_HOURS},
        ).fetchone()
        err = err_row[0][:300] if err_row and err_row[0] else "no error detail recorded"
        return f"jobs claimed but failing: {err}"

    if running_n > 0 and succeeded_n == 0:
        return (
            f"jobs running but none completing in {_DIAGNOSE_WINDOW_HOURS:.0f}h "
            f"({running_n} running now)"
        )

    return "all stages nominal — backlog may simply exceed drain rate"


# ── Layer 1: bespoke checks that don't fit the simple/backlog shapes ────

#: Budget for :func:`_check_chunks_extracted` — mirrors the retired
#: ``chunks_extracted`` freshness-check row's 6h budget.
_CHUNKS_EXTRACTED_BUDGET_HOURS = 6.0


def _check_chunks_extracted(conn: Any) -> CheckResult:
    """Extraction pipeline liveness: body rows only, input-aware.

    The original check read the newest ``chunks.created_at`` unfiltered —
    but ``card_forge``'s ``ord < 0`` card rewrites and the concept/glossary
    chunk writers also touch ``chunks`` on their own schedule, so one of
    *those* landing inside the budget window masked a genuinely stalled
    extraction pipeline (a missed alarm). Two fixes, both required:

    * **Body-row only** (``ord >= 0``) — the extraction pipeline's own
      output, not a synthesis pass's variant rows.
    * **Input-aware.** A bare "last body chunk was N hours ago" is
      indistinguishable from "no new paper has landed since" (healthy
      idle) without knowing whether there's been fresh input. So this is
      stale only when a paper landed more than budget ago that is
      *newer* than the newest body chunk (input arrived with nothing
      extracted for it since) — which, since that paper is both newer
      than the chunk and older than budget, already implies the chunk
      itself is older than budget too (checked once, not as two
      independent conditions). Quiet when no such paper exists — a quiet
      ingest pipeline is healthy, not stuck.
    """
    sql = """
        SELECT
            (SELECT max(created_at) FROM chunks WHERE ord >= 0) AS newest_chunk_ts,
            (SELECT max(r.created_at)
               FROM refs r
              WHERE r.kind = 'paper' AND r.retired_at IS NULL
                AND r.created_at < now() - (%(budget)s || ' hours')::interval
                AND r.created_at > COALESCE(
                      (SELECT max(created_at) FROM chunks WHERE ord >= 0),
                      '-infinity'::timestamptz)
            ) AS stale_input_ts
    """
    try:
        row = conn.execute(sql, {"budget": _CHUNKS_EXTRACTED_BUDGET_HOURS}).fetchone()
        newest_chunk_ts, stale_input_ts = (row[0], row[1]) if row else (None, None)
    except Exception:
        log.exception("health_digest: chunks_extracted probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception("health_digest: rollback after chunks_extracted probe failed")
        return CheckResult(
            "ingest",
            "chunks_extracted",
            "unknown",
            "a chunk being extracted: probe failed",
            _WARN,
        )
    age = _hours_since(newest_chunk_ts)
    if stale_input_ts is None:
        seen = f"{age:.1f}h ago" if age is not None else "never"
        return CheckResult(
            "ingest",
            "chunks_extracted",
            "ok",
            f"a chunk being extracted: {seen} "
            "(idle — no new paper input pending extraction)",
            _WARN,
            age,
        )
    return CheckResult(
        "ingest",
        "chunks_extracted",
        "stale",
        f"a chunk being extracted: last body chunk {age:.1f}h ago "
        f"(budget {_CHUNKS_EXTRACTED_BUDGET_HOURS:.0f}h) while a paper landed "
        f"{_hours_since(stale_input_ts):.1f}h ago with nothing extracted since",
        _WARN,
        age,
    )


#: Budget for :func:`_check_taproot_edges` — the retired freshness row's
#: own 6h, kept for overdue eligible work with no recent edge progress.
_TAPROOT_EDGES_BUDGET_HOURS = 6.0


def _check_taproot_edges(conn: Any) -> CheckResult:
    """Overdue chase work without edge progress; quiet empty queues are healthy.

    Mirrors ``chase.claim_tracing_findings`` and its default 60/1440-minute
    backoff, without row locks or importing the worker's model hooks. Queue
    parity is fixture-tested. Neither output silence nor a new canonical
    hub proves chase has work: real hubs are explicitly not chase inputs.
    Age from queue entry / last progress / backoff expiry, not just ref
    creation, so intentional cooldowns do not become false stalls. Failed
    or unknown activity never resets age; historical waiting expiries
    survive subsequent failures even though membership uses the last event.
    """
    sql = """
        WITH candidates AS (
            SELECT GREATEST(
                       r.created_at, queue.entered_at, progress.eligible_since
                   ) AS eligible_since,
                   last_chase.event,
                   last_chase.ts + LEAST(60 * POWER(2,
                       LEAST(GREATEST(wait_run.waits - 1, 0), 60)),
                       1440) * INTERVAL '1 minute' AS waiting_until
              FROM refs r
              JOIN LATERAL (
                  SELECT max(rt.created_at) AS entered_at
                    FROM ref_tags rt JOIN tags t USING (tag_id)
                   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                     AND t.value IN ('tracing', 'acquiring')
              ) queue ON queue.entered_at IS NOT NULL
              LEFT JOIN LATERAL (
                  SELECT e.event, e.ts FROM ref_events e
                   WHERE e.ref_id = r.ref_id AND e.source = 'chase'
                   ORDER BY e.ts DESC LIMIT 1
              ) last_chase ON TRUE
              LEFT JOIN LATERAL (
                  SELECT count(*)::int AS waits FROM ref_events e
                   WHERE e.ref_id = r.ref_id AND e.source = 'chase'
                     AND e.event = 'waiting'
                     AND e.ts > COALESCE(
                         (SELECT max(e2.ts) FROM ref_events e2
                           WHERE e2.ref_id = r.ref_id AND e2.source = 'chase'
                             AND e2.event <> 'waiting'),
                         '-infinity'::timestamptz)
              ) wait_run ON TRUE
              LEFT JOIN LATERAL (
                  -- Age is separate from membership: only semantic progress
                  -- or an actual cooldown expiry opens a fresh age budget.
                  SELECT max(transition_at) AS eligible_since
                    FROM (
                        SELECT CASE WHEN event = 'advanced' THEN ts
                                    WHEN event = 'waiting' THEN
                                        ts + LEAST(60 * POWER(2,
                                            LEAST(GREATEST(waits - 1, 0), 60)),
                                            1440) * INTERVAL '1 minute'
                               END AS transition_at
                          FROM (
                              SELECT event, ts,
                                     count(*) FILTER (WHERE event = 'waiting')
                                         OVER (PARTITION BY run ORDER BY ts) AS waits
                                FROM (
                                    SELECT event, ts,
                                           max(ts) FILTER (WHERE event <> 'waiting')
                                               OVER (ORDER BY ts) AS run
                                      FROM ref_events
                                     WHERE ref_id = r.ref_id AND source = 'chase'
                                ) history
                               -- Match the worker's strict ts > last non-wait
                               -- rule, including equal-timestamp peers.
                               WHERE run IS NULL OR ts > run
                                  OR event <> 'waiting'
                          ) waiting_runs
                    ) transitions
                   WHERE transition_at <= now()
              ) progress ON TRUE
             WHERE r.kind = 'finding' AND r.retired_at IS NULL
               AND NOT (
                   EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                            WHERE rt.ref_id = r.ref_id AND t.namespace = 'TAPROOT'
                              AND t.value = 'claim')
                   AND NOT EXISTS (
                       SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                        WHERE rt.ref_id = r.ref_id AND t.namespace = 'TAPROOTCASCADE')
               )
        ), eligible AS (
            SELECT eligible_since FROM candidates
             WHERE NOT COALESCE(event = 'waiting' AND waiting_until > now(), FALSE)
        )
        SELECT
            (SELECT max(created_at) FROM links
              WHERE relation = ANY(%(roles)s::text[])) AS newest_edge_ts,
            count(*) AS eligible_count, min(eligible_since) AS oldest_eligible
          FROM eligible
    """
    params = {"roles": list(_TAPROOT_HUB_ROLES)}
    try:
        row = conn.execute(sql, params).fetchone()
        newest_edge_ts, eligible_count, oldest_eligible = row
    except Exception:
        log.exception("health_digest: taproot_edges probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception("health_digest: rollback after taproot_edges probe failed")
        return CheckResult(
            "knowledge",
            "taproot_edges",
            "unknown",
            "a findings→claims taproot edge: probe failed",
            _WARN,
        )
    age = _hours_since(newest_edge_ts)
    seen = f"{age:.1f}h ago" if age is not None else "never"
    if eligible_count == 0:
        return CheckResult(
            "knowledge",
            "taproot_edges",
            "ok",
            f"a findings→claims taproot edge: {seen} (idle: 0 eligible)",
            _WARN,
            age,
        )
    oldest_age = _hours_since(oldest_eligible)
    stale = (
        oldest_age is not None
        and oldest_age > _TAPROOT_EDGES_BUDGET_HOURS
        and (age is None or age > _TAPROOT_EDGES_BUDGET_HOURS)
    )
    return CheckResult(
        "knowledge",
        "taproot_edges",
        "stale" if stale else "ok",
        f"a findings→claims taproot edge: last edge {seen}; "
        f"{eligible_count} eligible, oldest waiting {oldest_age:.1f}h "
        f"(budget {_TAPROOT_EDGES_BUDGET_HOURS:.0f}h)",
        _WARN,
        age,
    )


def _check_card_forge(conn: Any) -> CheckResult:
    """Last successful ``card_forge`` job (a ``job_type``, not a worker
    pass — no ``worker_logs`` pass name to key off, so read the job
    lifecycle directly)."""
    sql = """
        SELECT max(rt.created_at)
          FROM refs j
          JOIN ref_tags rt ON rt.ref_id = j.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE j.kind = 'job' AND j.retired_at IS NULL
           AND j.meta->>'job_type' = 'card_forge'
           AND t.namespace = 'STATUS' AND t.value = 'succeeded'
    """
    try:
        row = conn.execute(sql).fetchone()
        ts = row[0] if row else None
    except Exception:
        log.exception("health_digest: card_forge probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception("health_digest: rollback after card_forge probe failed")
        return CheckResult(
            "reading", "card_forge", "unknown", "card-forge: probe failed", _WARN
        )
    budget_hours = 26.0
    note = ""
    try:
        # Savepoint: a failed lookup must not poison the caller's
        # connection the remaining checks run on.
        with conn.transaction():
            budget_hours, note = _recurring_budget(conn, ("card_forge",), budget_hours)
    except Exception:
        log.exception("health_digest: card_forge budget lookup failed")
    age = _hours_since(ts)
    if age is not None and age <= budget_hours:
        return CheckResult(
            "reading",
            "card_forge",
            "ok",
            f"card-forge: {age:.1f}h ago (budget {budget_hours:.0f}h{note})",
            _WARN,
            age,
        )
    seen = f"{age:.1f}h ago" if age is not None else "never"
    return CheckResult(
        "reading",
        "card_forge",
        "stale",
        f"card-forge: last succeeded {seen} (budget {budget_hours:.0f}h{note})",
        _WARN,
        age,
    )


def _check_claim_hub_dedup_index(conn: Any) -> CheckResult:
    """Every live claim hub (``TAPROOT:claim`` **and** ``STATUS:canonical``
    — see :func:`precis.taproot.canon.claim_hub_predicate_sql`; a
    ``finding`` carrying ``TAPROOT:claim`` alone is a chase-tree finding,
    not a hub) is reachable by claim dedup.

    :func:`precis.taproot.canon.block` ANN-retrieves candidate duplicates
    over hubs' ``finding_body`` (``ord=0``) embeddings. A hub missing that
    chunk — or holding one the embed pass hasn't vectorized — is *invisible*
    to dedup: it can never be returned as a near-duplicate, so a fresh mint
    of the same claim silently becomes a second hub. Nothing else surfaces
    that; the mint just succeeds.

    This is the invariant the 2026-08-19 audit lacked. Dedup then retrieved
    over ``card_combined`` (``ord=-1``), which no code path ever wrote for a
    hub, so coverage sat at 187/1,524 (12.3%) and every "no duplicates
    found" verdict in the corpus was vacuous — undetected for the whole life
    of the feature. **That 1,524 was the permissive ``TAPROOT:claim``-alone
    population** — 280 of those rows were chase-tree findings
    (``STATUS:established``/``dead_chain``/``multi_candidate``), never
    hubs; this check denominated against them too until 2026-08-20
    (``docs/backlog/claim-hub-definition-divergence.md``). The strict
    claim-hub count is 1,244. Coverage is the thing to watch, not the
    freshness of any one pass: the body chunk is written in ``mint_hub``'s
    own transaction, so a shortfall means either an embed backlog or a hub
    minted off-door.

    A small non-zero count is usually just embed lag on a fresh mint, hence
    ``warn`` rather than error; a persistent or growing count is a real
    correctness hole.

    Not imported from :mod:`precis.taproot.canon` — this module asserts
    (``tests/workers/test_health_digest.py::test_module_imports_no_llm``)
    that it carries no ``llm`` import, direct or transitive, and
    ``taproot.canon`` imports :mod:`precis.utils.llm.router`; the two tag
    literals are duplicated here instead (this module's existing
    convention — every other tag check here inlines its namespace/value
    literals rather than importing a constant, e.g. the ``STATUS`` checks
    above).
    """
    sql = """
        SELECT count(*) AS hubs,
               count(*) FILTER (WHERE ce.chunk_id IS NULL) AS unindexed
          FROM refs r
          LEFT JOIN chunks c ON c.ref_id = r.ref_id AND c.ord = 0
                            AND c.chunk_kind = 'finding_body'
                            AND c.retired_at IS NULL
          LEFT JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
                                       AND ce.embedder = %s
                                       AND ce.status = 'ok'
         WHERE r.kind = 'finding' AND r.retired_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND (rt.expires_at IS NULL OR rt.expires_at > now())
                    AND t.namespace = 'TAPROOT' AND t.value = 'claim'
               )
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = 'STATUS' AND t.value = 'canonical'
               )
    """
    try:
        row = conn.execute(sql, (health_checks._EMBED_ARTIFACT,)).fetchone()
    except Exception:
        log.exception("health_digest: claim-hub dedup-index probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception("health_digest: rollback after dedup-index probe failed")
        return CheckResult(
            "taproot",
            "claim_hub_dedup_index",
            "unknown",
            "claim-hub dedup index: probe failed",
            _WARN,
        )
    hubs = int(row[0]) if row else 0
    unindexed = int(row[1]) if row else 0
    if hubs == 0:
        return CheckResult(
            "taproot",
            "claim_hub_dedup_index",
            "ok",
            "claim-hub dedup index: no hubs yet",
            _WARN,
        )
    if unindexed == 0:
        return CheckResult(
            "taproot",
            "claim_hub_dedup_index",
            "ok",
            f"claim-hub dedup index: {hubs}/{hubs} indexed",
            _WARN,
        )
    pct = 100.0 * (hubs - unindexed) / hubs
    return CheckResult(
        "taproot",
        "claim_hub_dedup_index",
        "stale",
        f"claim-hub dedup index: {unindexed} of {hubs} hubs unindexed "
        f"({pct:.1f}% covered) — those hubs cannot be returned as "
        f"duplicate candidates, so mints of the same claim will fork",
        _WARN,
    )


def _check_nanopub_candidates_fresh(conn: Any) -> CheckResult:
    """Staged nanopub candidates re-gated against the CURRENT mint rules.

    A ``nanopub_publish`` row is staged as ``candidate`` and then the world
    moves: new blocking lint codes land (llm-attribution 2026-08-24, the
    review-source arm 2026-08-27), or the hub picks up a live
    ``contradicts`` edge — and nothing re-gates the staged row, so the
    ``/nanopub`` queue keeps offering dead work. The 2026-08-27 audit found
    13 of 135 staged rows already unmintable.

    Deliberately narrower than ``run_mint_gates``: only the sentence lints
    (code-drift-sensitive) and the contradicts edge (world-drift-sensitive)
    are re-checked. The payload gates (quote/snip/containment) have nothing
    to bite on — a candidate carries no grounding envelope yet — and the
    structural arms barely move under a code release.

    The row's hub must also still *be* a strict claim hub — live
    ``TAPROOT:claim`` **and** ``STATUS:canonical`` (the tag literals are
    inlined, not imported; see :func:`_check_claim_hub_dedup_index`). A row
    failing that reports ``noncanonical`` regardless of lint: the
    2026-08-27 review found 3 staged rows carrying ``TAPROOT:claim`` alone
    — chase-tree findings, never hubs. The remedy is routing them back to
    the finding lifecycle; this check only surfaces them.
    """
    sql = """
        SELECT r.ref_id, r.title, p.artifact_type,
               EXISTS (
                   SELECT 1 FROM links l
                    JOIN refs pr ON pr.ref_id = l.src_ref_id
                                AND pr.retired_at IS NULL
                   WHERE l.dst_ref_id = r.ref_id AND l.relation = 'contradicts'
               ) AS disputed,
               (EXISTS (
                    SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = r.ref_id
                       AND (rt.expires_at IS NULL OR rt.expires_at > now())
                       AND t.namespace = 'TAPROOT' AND t.value = 'claim'
                )
                AND EXISTS (
                    SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = r.ref_id
                       AND (rt.expires_at IS NULL OR rt.expires_at > now())
                       AND t.namespace = 'STATUS' AND t.value = 'canonical'
                )) AS canonical
          FROM nanopub_publish p
          JOIN refs r ON r.ref_id = p.claim_ref_id AND r.retired_at IS NULL
         WHERE p.state = 'candidate'
         ORDER BY p.id
    """
    try:
        rows = conn.execute(sql).fetchall()
    except Exception:
        log.exception("health_digest: nanopub-candidates probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception(
                "health_digest: rollback after nanopub-candidates probe failed"
            )
        return CheckResult(
            "nanopub",
            "staged_candidates_fresh",
            "unknown",
            "staged nanopub candidates: probe failed",
            _WARN,
        )
    if not rows:
        return CheckResult(
            "nanopub",
            "staged_candidates_fresh",
            "ok",
            "staged nanopub candidates: none staged",
            _WARN,
        )
    stale: list[str] = []
    for ref_id, title, artifact_type, disputed, canonical in rows:
        reason = candidate_stale_reason(
            canonical=bool(canonical),
            disputed=bool(disputed),
            title=str(title or ""),
            artifact_type=str(artifact_type),
        )
        if reason is not None:
            stale.append(f"fi{int(ref_id)}({reason.label})")
    if not stale:
        return CheckResult(
            "nanopub",
            "staged_candidates_fresh",
            "ok",
            f"staged nanopub candidates: {len(rows)}/{len(rows)} pass "
            "current mint gates",
            _WARN,
        )
    shown = "; ".join(stale[:4]) + ("; …" if len(stale) > 4 else "")
    return CheckResult(
        "nanopub",
        "staged_candidates_fresh",
        "stale",
        f"staged nanopub candidates: {len(stale)} of {len(rows)} staged "
        f"rows no longer pass the current mint gates — the /nanopub queue "
        f"is offering dead work ({shown})",
        _WARN,
    )


def _check_lanes_idle_beside_queue(conn: Any) -> CheckResult:
    """A job executor has old queued work but claimed nothing for an hour.

    Reads :func:`precis.health_checks.idle_lanes` (``claude_inproc`` is
    excluded there; nursery's dispatch-stall detector owns that lane)."""
    try:
        lanes = health_checks.idle_lanes(conn)
    except Exception:
        log.exception("health_digest: lanes_idle_beside_queue probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception("health_digest: rollback after lanes_idle probe failed")
        return CheckResult(
            "autonomy",
            "lanes_idle_beside_queue",
            "unknown",
            "job lanes: probe failed",
            _WARN,
        )
    if not lanes:
        return CheckResult(
            "autonomy",
            "lanes_idle_beside_queue",
            "ok",
            "job lanes: no executor idle beside queued work",
            _WARN,
        )
    detail = "; ".join(
        f"{lane.executor}: {lane.queued} queued, "
        f"oldest {lane.oldest_queued_minutes}m, no claim in 1h"
        for lane in lanes
    )
    return CheckResult("autonomy", "lanes_idle_beside_queue", "stale", detail, _WARN)


def _check_agent_jobs_completing(conn: Any) -> CheckResult:
    """Agent (``claude_inproc``) jobs completing, not all-failing, in 6h.

    Idle-aware: zero completions in the window (nothing minted/ran) is
    ``ok`` — a quiet planner is not a broken one. Only "activity happened
    but every single completion failed" is stale."""
    sql = """
        SELECT
            count(*) FILTER (WHERE t.value = 'succeeded')::int AS ok_n,
            count(*) FILTER (WHERE t.value = 'failed')::int    AS failed_n
          FROM refs j
          JOIN ref_tags rt ON rt.ref_id = j.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE j.kind = 'job' AND j.retired_at IS NULL
           AND j.meta->>'executor' = 'claude_inproc'
           AND t.namespace = 'STATUS' AND t.value IN ('succeeded', 'failed')
           AND rt.created_at > now() - interval '6 hours'
    """
    try:
        row = conn.execute(sql).fetchone()
        ok_n, failed_n = int(row[0] or 0), int(row[1] or 0)
    except Exception:
        log.exception("health_digest: agent_jobs_completing probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception("health_digest: rollback after agent_jobs probe failed")
        return CheckResult(
            "autonomy",
            "agent_jobs_completing",
            "unknown",
            "agent jobs: probe failed",
            _WARN,
        )
    if ok_n == 0 and failed_n == 0:
        return CheckResult(
            "autonomy",
            "agent_jobs_completing",
            "ok",
            "agent jobs: none completed in 6h (idle)",
            _WARN,
        )
    if failed_n > 0 and ok_n == 0:
        return CheckResult(
            "autonomy",
            "agent_jobs_completing",
            "stale",
            f"agent jobs: {failed_n} completed in 6h — ALL failing, zero successes",
            _WARN,
        )
    return CheckResult(
        "autonomy",
        "agent_jobs_completing",
        "ok",
        f"agent jobs: {ok_n} ok / {failed_n} failed in 6h",
        _WARN,
    )


#: Digest-line mirror of nursery's ``host-dark`` critical (5min narrower
#: window there; this is the softer, non-paging summary of the same fact —
#: nursery already pages, this just keeps it visible in the daily digest).
_HOSTS_ALIVE_SILENCE_MIN = 15
_HOSTS_ALIVE_LOOKBACK_DAYS = 30


def _check_hosts_alive(conn: Any) -> CheckResult:
    # Excludes ephemeral job-container identities (gr333066), mirroring
    # nursery._detect_host_dark's two-layer guard (gr306275 + gr331348): the
    # cooperative `meta.ephemeral` stamp, plus a non-cooperative belt on the
    # identity *shape* itself (a 12-hex host is a Docker container ID
    # regardless of who wrote it). The regex is inlined rather than shared
    # from `heartbeat._CONTAINER_ID_RE` — this check must not depend on the
    # module it watches, same rationale as nursery's copy.
    sql = """
        SELECT hh.host, hh.ts
          FROM host_heartbeat hh
         WHERE hh.ts < now() - (%(silence)s || ' minutes')::interval
           AND (hh.meta->>'ephemeral') IS DISTINCT FROM 'true'
           AND hh.host !~ '^[0-9a-f]{12}$'
           AND EXISTS (
               SELECT 1 FROM worker_logs wl
                WHERE wl.host = hh.host
                  AND wl.ts > now() - (%(lookback)s || ' days')::interval
           )
         ORDER BY hh.ts ASC
         LIMIT 50
    """
    try:
        rows = conn.execute(
            sql,
            {
                "silence": _HOSTS_ALIVE_SILENCE_MIN,
                "lookback": _HOSTS_ALIVE_LOOKBACK_DAYS,
            },
        ).fetchall()
    except Exception:
        log.exception("health_digest: hosts_alive probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception("health_digest: rollback after hosts_alive probe failed")
        return CheckResult(
            "infra", "hosts_alive", "unknown", "hosts alive: probe failed", _WARN
        )
    if not rows:
        return CheckResult(
            "infra", "hosts_alive", "ok", "hosts alive: all fresh", _WARN
        )
    names = ", ".join(str(r[0]) for r in rows)
    return CheckResult(
        "infra",
        "hosts_alive",
        "stale",
        f"hosts alive: {names} dark >{_HOSTS_ALIVE_SILENCE_MIN}min "
        "per heartbeat (see nursery's host-dark alert if one is open)",
        _WARN,
    )


def _check_alert_backlog_rot(conn: Any) -> CheckResult:
    """Open alerts nobody has closed in a week — "the response loop is
    rotting".

    Excludes ``watchdog:meta``, which is this check's own alert source. Once
    it fires, its alert ages past seven days and then counts itself, so the
    condition can never clear no matter what an operator does: ``al187588``
    sat open from 2026-08-02 with ``seen_count`` 1053 for exactly that
    reason. A watchdog must not be its own subject.
    """
    sql = f"""
        SELECT count(*)::int
          FROM refs r
          JOIN ref_tags rt ON rt.ref_id = r.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE r.kind = 'alert' AND r.retired_at IS NULL
           AND t.namespace = 'OPEN' AND t.value = %s
           AND r.created_at < now() - interval '7 days'
           AND coalesce(r.meta->>'alert_source', '') <> 'watchdog:meta'
           AND NOT {ACKED_SQL}
    """
    try:
        row = conn.execute(sql, (STATE_OPEN,)).fetchone()
        n = int(row[0] or 0) if row else 0
    except Exception:
        log.exception("health_digest: alert_backlog_rot probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception(
                "health_digest: rollback after alert_backlog_rot probe failed"
            )
        return CheckResult(
            "meta", "alert_backlog_rot", "unknown", "alert backlog: probe failed", _INFO
        )
    if n == 0:
        return CheckResult(
            "meta", "alert_backlog_rot", "ok", "alert backlog: none open >7d", _INFO
        )
    return CheckResult(
        "meta",
        "alert_backlog_rot",
        "stale",
        f"alert backlog: {n} open alert(s) older than 7 days — the response loop is rotting",
        _INFO,
    )


#: Trailing window the ``tool_latency_regressed`` check compares the last
#: hour against. The DB keeps no deploy timestamp, so "since the last
#: deploy" (gr479849) reads as "against the week before this hour"; a
#: deploy that slows a pair shows the same doubling.
_TOOL_LATENCY_BASELINE_S = 7 * 86400

#: A pair whose p95 doubled but is still under this is not reported:
#: 40 ms → 90 ms is noise, not a slow MCP.
_TOOL_LATENCY_REGRESSION_FLOOR_MS = 1000


def _fmt_pair(t: health_checks.ToolLatency, *, with_baseline: bool = False) -> str:
    s = f"{t.verb}/{t.kind} p95 {t.p95_ms / 1000:.1f}s"
    if with_baseline and t.baseline_p95_ms is not None:
        return f"{s} (week {t.baseline_p95_ms / 1000:.1f}s, n={t.calls})"
    return f"{s} > {t.budget_ms / 1000:.0f}s budget (n={t.calls})"


def _two_over(t: health_checks.ToolLatency, line_ms: int) -> bool:
    """p95 and the second-slowest call both over ``line_ms``."""
    s2 = t.second_slowest_ms
    return t.p95_ms > line_ms and s2 is not None and s2 > line_ms


def _tool_latency_checks(conn: Any) -> list[CheckResult]:
    """MCP tool-call latency over the last hour from the ``tool_calls``
    ledger (gr479849): ``tool_latency`` goes stale when a verb/kind's p95
    exceeds its budget
    (:func:`precis.health_checks.tool_latency_budget_ms`);
    ``tool_latency_regressed`` when it more than doubled against the
    trailing week. Pairs under
    :data:`~precis.health_checks.TOOL_LATENCY_MIN_CALLS` calls are not
    judged, and a pair needs its second-slowest call over the line too:
    one cold call after a restart is not a slow MCP.
    """
    try:
        rows = health_checks.tool_latency(conn, baseline_s=_TOOL_LATENCY_BASELINE_S)
    except Exception:
        log.exception("health_digest: tool_latency probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception("health_digest: rollback after tool_latency probe failed")
        return [
            CheckResult(
                "infra", "tool_latency", "unknown", "tool latency: probe failed", _WARN
            ),
            CheckResult(
                "infra",
                "tool_latency_regressed",
                "unknown",
                "tool latency trend: probe failed",
                _INFO,
            ),
        ]
    floor = health_checks.TOOL_LATENCY_MIN_CALLS
    judged = [t for t in rows if t.calls >= floor]
    over = [t for t in judged if _two_over(t, t.budget_ms)]
    regressed = [
        t
        for t in judged
        if t.baseline_calls >= floor
        and t.baseline_p95_ms is not None
        and t.p95_ms >= _TOOL_LATENCY_REGRESSION_FLOOR_MS
        and _two_over(t, 2 * t.baseline_p95_ms)
    ]
    budget = (
        CheckResult(
            "infra",
            "tool_latency",
            "stale",
            "tool latency 1h: " + "; ".join(_fmt_pair(t) for t in over),
            _WARN,
        )
        if over
        else CheckResult(
            "infra",
            "tool_latency",
            "ok",
            f"tool latency 1h: {len(judged)} verb/kind pair(s) within budget",
            _WARN,
        )
    )
    trend = (
        CheckResult(
            "infra",
            "tool_latency_regressed",
            "stale",
            "tool latency 1h more than doubled: "
            + "; ".join(_fmt_pair(t, with_baseline=True) for t in regressed),
            _INFO,
        )
        if regressed
        else CheckResult(
            "infra",
            "tool_latency_regressed",
            "ok",
            "tool latency 1h: no pair doubled against the past week",
            _INFO,
        )
    )
    return [budget, trend]


#: The registry service name gating the role3 classifier (see
#: ``workers/registry.py``'s ``classify`` :class:`ServiceSpec`) — the name
#: :func:`_classify_gate_enabled` looks up.
_CLASSIFY_SERVICE_NAME = "classify"


def _classify_gate_enabled(store: Store) -> bool:
    """Does ``classify`` resolve enabled on at least one host right now?

    Reuses :func:`_resolve_enabled_somewhere` — the exact registry ×
    ``service_config`` predicate :func:`_layer2_checks` already uses for
    this same spec — rather than a second, possibly-drifting config read
    (gr204385's ask). Best-effort: a missing spec (registry edit gone
    wrong) or a ``service_config`` read failure (handled inside the
    helper, which logs and falls back to structural default) both read as
    "not enabled", matching ``classify``'s own default-OFF posture rather
    than risking a false "on".
    """
    spec = next((s for s in SERVICES if s.name == _CLASSIFY_SERVICE_NAME), None)
    if spec is None:
        return False
    return _resolve_enabled_somewhere(store, [spec]).get(_CLASSIFY_SERVICE_NAME, False)


def _idle_classify_check() -> CheckResult:
    """The ``chunks_classified`` reading when :func:`_classify_gate_enabled`
    is False — ``status="ok"`` (a non-finding, per :attr:`CheckResult.is_finding`)
    so a fleet where ``classify`` has simply never been turned on can never
    alert, mirroring the idle-aware backlog checks' own "empty backlog is
    healthy" non-finding shape rather than inventing a parallel status."""
    return CheckResult(
        "discovery",
        "chunks_classified",
        "ok",
        "a chunk classified (role3): classify is disabled by service_config "
        "(default-off) — idle by design, not stale",
        _WARN,
    )


#: Mirrors ``classify.py``'s ``ARTIFACT`` (``f"classify:cascade-v{CLASSIFY_VERSION}"``,
#: ``CLASSIFY_VERSION="1"``) and ``_CLAIM_COOLDOWN_MIN`` — kept as literals
#: here rather than imported, per this module's don't-depend-on-what-it-
#: watches convention (see the ``chunks_classified`` tuple's comment
#: above). A ``classify.py`` version bump must update
#: :data:`_CLASSIFY_ARTIFACT` too, or this probe under-counts the pending
#: pool after the bump (a stale artifact string never matches the new
#: claims, so every chunk looks perpetually unclaimed — still correct for
#: "is anything pending", just imprecise about the cooldown).
_CLASSIFY_ARTIFACT = "classify:cascade-v1"
_CLASSIFY_RECLAIM_COOLDOWN_MIN = 20


def _classify_pool_empty(conn: Any) -> bool:
    """True when no chunk is currently eligible for the classify cascade's
    claim — mirrors ``classify.py``'s ``_claim_slice``/``_reclaim_slice``
    predicate INLINE (this module deliberately does not import from what
    it watches, see the ``chunks_classified`` tuple's comment above).

    A chunk is pending iff it's an unretired paper's body ``paragraph``
    chunk over 120 chars with no ``ROLE3`` tag yet, AND either never
    claimed under :data:`_CLASSIFY_ARTIFACT` or claimed more than
    :data:`_CLASSIFY_RECLAIM_COOLDOWN_MIN` minutes ago — a poison chunk
    (gr331492) cycling under a fresh claim still counts as pending, so a
    hijacked chunk mid-retry never makes the pool read empty. ``EXISTS ...
    LIMIT 1`` shape, cheap even against the 1.3M-row corpus — only called
    when the ``chunks_classified`` marker is already over budget (see
    :func:`_layer1_checks`), never on the healthy path.
    """
    row = conn.execute(
        """
        SELECT NOT EXISTS (
            SELECT 1
              FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
             WHERE r.kind = 'paper' AND r.retired_at IS NULL
               AND c.ord >= 0 AND c.chunk_kind = 'paragraph' AND length(c.text) > 120
               AND NOT EXISTS (
                     SELECT 1 FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id
                      WHERE ct.chunk_id = c.chunk_id AND t.namespace = 'ROLE3'
                   )
               AND NOT EXISTS (
                     SELECT 1 FROM chunk_claims cl
                      WHERE cl.chunk_id = c.chunk_id AND cl.artifact = %(art)s
                        AND cl.claimed_at >= now() - (%(cooldown)s * interval '1 minute')
                   )
             LIMIT 1
        )
        """,
        {"art": _CLASSIFY_ARTIFACT, "cooldown": _CLASSIFY_RECLAIM_COOLDOWN_MIN},
    ).fetchone()
    # A missing row should never happen for `SELECT NOT EXISTS(...)`; fail
    # conservatively toward "not empty" so a probe hiccup can never silence
    # a genuinely stuck backlog.
    return bool(row[0]) if row else False


def _idle_classify_pool_empty_check() -> CheckResult:
    """The ``chunks_classified`` reading when the classify gate IS enabled
    but :func:`_classify_pool_empty` finds nothing left to claim —
    ``status="ok"`` (non-finding), same idle-by-design shape as
    :func:`_idle_classify_check`. gr331492: the classify backfill reached
    fresh-eligible=0 / reclaimable=0 in prod, so the ``ROLE3`` marker
    legitimately goes stale once the backlog drains — without this, the
    router re-files the same pipeline-stuck gripe forever against a pass
    that has simply finished its work."""
    return CheckResult(
        "discovery",
        "chunks_classified",
        "ok",
        "a chunk classified (role3): backlog drained — idle by design, not stale",
        _WARN,
    )


def _check_doctor_report_fresh(conn: Any) -> CheckResult:
    """The doctor is still filing reports.

    Every other consumer of the report treats its absence as a reason to
    fall back QUIETLY — this module's own push-body selector drops to the
    template (:func:`_select_push_body`) and ``briefing_cast`` drops its
    health line — so a doctor that stops reporting is invisible by
    construction. That is how the 2026-09-10 melchior OAuth expiry ran two
    days with no report and no signal. This is the one check that reads the
    absence as a fault rather than a fallback.

    Freshness follows the newest body CHUNK, not the ref's ``created_at``:
    a same-day re-tick appends a paragraph without touching the ref (same
    reason :func:`precis.workers.doctor_report.latest_report` uses
    ``_last_tick_evidence``). Budget is the report's own
    ``FRESH_WINDOW`` (12h) doubled — one whole missed 8h cadence window is
    a blip, two consecutive ones is a fault.
    """
    sql = """
        SELECT GREATEST(
                 r.created_at,
                 COALESCE(MAX(c.created_at), r.created_at)
               )
          FROM refs r
          LEFT JOIN chunks c
            ON c.ref_id = r.ref_id AND c.retired_at IS NULL AND c.ord >= 0
         WHERE r.kind = 'draft' AND r.retired_at IS NULL
           AND r.meta->>'author' = 'doctor'
         GROUP BY r.ref_id, r.created_at
         ORDER BY r.created_at DESC
         LIMIT 1
    """
    # Lazy import, matching _lookup_fresh_doctor_report below: doctor_report
    # is read at call time so the budget has ONE home (its FRESH_WINDOW)
    # rather than a second copy of the number here.
    from precis.workers.doctor_report import FRESH_WINDOW

    budget_hours = 2 * FRESH_WINDOW.total_seconds() / 3600.0
    try:
        row = conn.execute(sql).fetchone()
    except Exception:
        log.exception("health_digest: doctor_report_fresh probe failed")
        try:
            conn.rollback()
        except Exception:
            log.exception("health_digest: rollback after doctor_report probe failed")
        return CheckResult(
            "autonomy",
            "doctor_report_fresh",
            "unknown",
            "doctor report: probe failed",
            _WARN,
        )
    if row is None or row[0] is None:
        return CheckResult(
            "autonomy",
            "doctor_report_fresh",
            "stale",
            "doctor report: none has ever been filed",
            _WARN,
        )
    latest = row[0]
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    age_h = (datetime.now(UTC) - latest).total_seconds() / 3600.0
    if age_h > budget_hours:
        return CheckResult(
            "autonomy",
            "doctor_report_fresh",
            "stale",
            f"doctor report: last tick wrote {age_h:.1f}h ago "
            f"(budget {budget_hours:.0f}h) — the doctor is not reporting; "
            f"check doctor_tick job failures",
            _WARN,
            age_hours=age_h,
        )
    return CheckResult(
        "autonomy",
        "doctor_report_fresh",
        "ok",
        f"doctor report: last tick wrote {age_h:.1f}h ago",
        _WARN,
        age_hours=age_h,
    )


def _layer1_checks(store: Store) -> list[CheckResult]:
    """All curated Layer-1 outcome checks, one connection, best-effort."""
    with store.pool.connection() as conn:
        out = _freshness_layer1(conn)
        out.append(_check_chunks_extracted(conn))
        out.append(_check_taproot_edges(conn))
        out += _idle_aware_backlog_checks(conn)
        out.append(_check_card_forge(conn))
        out.append(_check_claim_hub_dedup_index(conn))
        out.append(_check_nanopub_candidates_fresh(conn))
        out.append(_check_agent_jobs_completing(conn))
        out.append(_check_lanes_idle_beside_queue(conn))
        out.append(_check_doctor_report_fresh(conn))
        out.append(_check_hosts_alive(conn))
        out.append(_check_alert_backlog_rot(conn))
        out += _tool_latency_checks(conn)
    if not _classify_gate_enabled(store):
        out = [
            _idle_classify_check() if c.name == "chunks_classified" else c for c in out
        ]
    elif any(c.name == "chunks_classified" and c.status != "ok" for c in out):
        # Only pay for the (heavier) eligible-pool probe once the marker is
        # already over budget — the healthy path never runs this query.
        with store.pool.connection() as conn:
            pool_empty = _classify_pool_empty(conn)
        if pool_empty:
            out = [
                _idle_classify_pool_empty_check()
                if c.name == "chunks_classified"
                else c
                for c in out
            ]
    return out


# ── derived: cadence staleness (scheduler_leases) ────────────────────────

#: gr194430 §D blind spot: a cadence gated by ``eligible`` (§A) never seeds
#: a ``scheduler_leases`` row when its eligibility is false on *every*
#: host — the gate short-circuits ``run_scheduler_pass`` before
#: ``claim_scheduler_lease`` (see ``workers/scheduler.py``'s docstring), so
#: the row-per-lease loop above never sees it at all. That's invisible by
#: omission, not by an overdue row, so it needs its own check below.
#: ``materialize`` is exempt: it carries no ``eligible``/``host_affinity``
#: gate (any live worker always attempts the claim), it's flag-gated
#: *inside* the run instead (``PRECIS_MATERIALIZE_EMBED``,
#: ``workers/materialize.py``) — its lease is always seeded regardless of
#: the flag, so it can never legitimately land here, and listing it
#: defensively documents that a future refactor adding a pre-claim gate to
#: it must not make this check false-positive.
_CADENCE_NEVER_SEEDED_EXEMPT = frozenset({"materialize"})


#: Floor on the cadence-staleness margin (seconds) — see
#: :func:`_cadence_staleness_checks` for the measured rotation lengths that
#: set it. A short-interval cadence is judged against this, not its own
#: interval.
_CADENCE_MARGIN_FLOOR_S = 900


def _cadence_staleness_checks(store: Store) -> list[CheckResult]:
    """Every ``scheduler_leases`` row overdue past ``interval_s + margin``,
    plus (gr194430) every registry cadence with **no** lease row at all.

    Zero per-cadence config — a cadence added to ``workers/scheduler.py``
    is watched the moment it seeds its first lease row, no digest edit.
    ``margin = max(interval_s, _CADENCE_MARGIN_FLOOR_S)`` — generous enough
    that a normal rotation never trips it, tight enough that a
    genuinely-stopped cadence still trips within a fraction of the 6h
    self-heal budget its ``watchdog:cadence`` alert carries.

    Why the floor is 15 min and not the old 300 s: a cadence fires from
    the ``scheduler`` pass of whichever worker's rotation reaches it, and a
    rotation is as long as its slowest pass. Measured 2026-09-25 (12h,
    prod): melchior's system worker cycles every ~34 min (``fetch_oa`` up
    to 43 min, ``fetch_google_patents`` 32 min, ``orcid_enrich`` 12 min,
    all by their own pacing), balthazar's every ~7.5 min with 9 gaps over
    10 min in 6h. A 300 s cadence with a 300 s margin therefore read
    "stale" every time balthazar's rotation ran long — ``materialize``
    flapped 5× in one morning while firing every 5–13 min, never stopped.
    The check's job is "stopped firing", a days-scale rot; 15 min of
    lateness is the worker architecture, not a finding.

    The never-seeded half closes §D's blind spot: an ``eligible`` gate
    (``dream_agent``/``anki_sync``/``structural``/``deep_review``) that's
    false on every host — e.g. its enabling env lost fleet-wide in a
    deploy-template regression — means the cadence never wins its first
    claim, so no ``scheduler_leases`` row ever exists for it to go stale.
    The lease-row loop above is blind to that by construction (it only
    iterates rows that exist); the absence itself, sustained past this
    eval, is the only signal available without reaching into remote hosts'
    env, so any :data:`CADENCES` entry outside
    :data:`_CADENCE_NEVER_SEEDED_EXEMPT` with no lease row at all is
    flagged — distinctly named (``<name>/never-seeded``) so it never
    collides with that same cadence's ordinary overdue-lease finding above.
    """
    out: list[CheckResult] = []
    try:
        leases = store.scheduler_leases()
    except Exception:
        log.exception("health_digest: scheduler_leases read failed")
        return [
            CheckResult(
                "cadence",
                "scheduler_leases",
                "unknown",
                "cadence staleness: read failed",
                _WARN,
            )
        ]
    seeded = {lease.name for lease in leases}
    for cad in CADENCES:
        if cad.name in seeded or cad.name in _CADENCE_NEVER_SEEDED_EXEMPT:
            continue
        out.append(
            CheckResult(
                "cadence",
                f"{cad.name}/never-seeded",
                "stale",
                f"{cad.name}: expected cadence has no scheduler_leases row "
                "on any host — its eligibility gate may be false fleet-wide "
                "(enabling env lost in a deploy-template regression), so it "
                "has never won its first claim",
                _WARN,
            )
        )
    now = datetime.now(UTC)
    for lease in leases:
        margin_s = max(lease.interval_s, _CADENCE_MARGIN_FLOOR_S)
        next_fire = lease.next_fire_at
        if next_fire.tzinfo is None:
            next_fire = next_fire.replace(tzinfo=UTC)
        overdue_s = (now - next_fire).total_seconds()
        if overdue_s <= margin_s:
            out.append(
                CheckResult(
                    "cadence",
                    lease.name,
                    "ok",
                    f"{lease.name}: on schedule (next due in {-overdue_s / 60:.0f}min)"
                    if overdue_s < 0
                    else f"{lease.name}: fired within interval+margin",
                    _WARN,
                )
            )
            continue
        last_fired = (
            f"{_hours_since(lease.last_fired_at):.1f}h ago"
            if lease.last_fired_at
            else "never"
        )
        out.append(
            CheckResult(
                "cadence",
                lease.name,
                "stale",
                f"{lease.name}: stopped firing — overdue "
                f"{overdue_s / 60:.0f}min past interval+margin "
                f"({lease.interval_s}s + {margin_s}s), last fired {last_fired} "
                f"on {lease.last_host or 'unknown'}",
                _WARN,
            )
        )
    return out


# ── derived: Layer-2 registry × service_config × worker_logs coherence ──


def _resolve_enabled_somewhere(
    store: Store, specs: list[ServiceSpec]
) -> dict[str, bool]:
    """For each ``spec``, does it resolve enabled on at least one host?

    Mirrors :class:`precis.workers.service_config.ServiceConfigResolver`'s
    semantics (exact-host row beats the ``*`` wildcard, which beats the
    structural default) without needing a specific host in hand: an exact
    ``prio > 0`` row on ANY host means "enabled somewhere"; otherwise the
    ``*`` wildcard row (if any) decides; otherwise fall back to the
    structural default, which mirrors the live gate
    ``cli/worker.py::_profile_default_on`` — profile membership
    (``default_profiles`` non-empty and on this host's profile). §L retired
    ``enable_env`` as a default source: an ``enable_env``-only spec defaults
    OFF absent an explicit ``service_config`` row.
    """
    structural = {s.name: bool(s.default_profiles) for s in specs}
    if not structural:
        return {}
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT service, host, prio FROM service_config WHERE service = ANY(%s)",
                (list(structural),),
            ).fetchall()
    except Exception:
        log.warning("health_digest: service_config read failed", exc_info=True)
        rows = []
    by_service: dict[str, list[tuple[str, int]]] = {}
    for service, host, prio in rows:
        by_service.setdefault(str(service), []).append((str(host), int(prio)))

    out: dict[str, bool] = {}
    for name, default_on in structural.items():
        service_rows = by_service.get(name, [])
        if any(host != "*" and prio > 0 for host, prio in service_rows):
            out[name] = True
            continue
        wildcard = next((prio for host, prio in service_rows if host == "*"), None)
        out[name] = (wildcard > 0) if wildcard is not None else default_on
    return out


def _layer2_checks(
    store: Store, *, specs: list[ServiceSpec] | None = None
) -> list[CheckResult]:
    """Registered PASS + ``ref_pass`` services that resolve enabled
    somewhere but show zero ``worker_logs`` activity in 24h.

    ``specs`` defaults to the real registry (``workers/registry.py``); a
    test passes a synthetic list to prove a newly-minted
    :class:`~precis.workers.registry.ServiceSpec` is picked up with zero
    edits to this module.
    """
    candidates = specs if specs is not None else list(SERVICES)
    pass_specs = [s for s in candidates if s.kind == ServiceKind.PASS and s.ref_pass]
    enabled = _resolve_enabled_somewhere(store, pass_specs)
    activity = health_checks.activity_by_handler(store, window_hours=24)

    out: list[CheckResult] = []
    for spec in pass_specs:
        if not enabled.get(spec.name, False):
            continue
        act = activity.get(spec.log_handler)
        # last_seen is UNFILTERED (any row, not just ok>0/failed>0) — a
        # healthy-idle pass logs claimed=0 every cycle (workers/runner.py),
        # which has neither a last_ok nor a last_fail. Keying off those
        # alone misread "ran all day, nothing to do" as "never ran",
        # violating the idle-vs-stuck acceptance gate.
        if act and act.get("last_seen") is not None:
            continue  # has recent worker_logs activity — not silent
        out.append(
            CheckResult(
                "coherence",
                spec.name,
                "stale",
                f"{spec.name}: registered PASS resolves enabled but zero "
                f"worker_logs rows (ok or failed) in 24h — intended-on but silent",
                _WARN,
            )
        )
    return out


def _evaluate_checks(
    store: Store, *, specs: list[ServiceSpec] | None = None
) -> list[CheckResult]:
    """Run every check from all three sources. Best-effort per source —
    one source's own defensive degrade-to-unknown never blocks another."""
    out: list[CheckResult] = []
    try:
        out += _layer1_checks(store)
    except Exception:
        log.exception("health_digest: layer-1 checks raised")
    try:
        out += _cadence_staleness_checks(store)
    except Exception:
        log.exception("health_digest: cadence-staleness checks raised")
    try:
        out += _layer2_checks(store, specs=specs)
    except Exception:
        log.exception("health_digest: layer-2 checks raised")
    return out


# ── findings → alerts ─────────────────────────────────────────────────

#: ``(group, check name) → alert fingerprint`` for every fixed-name check
#: this module emits. The fingerprint is the check's *failure id* (its
#: subject in :mod:`precis.alert_ids`), pinned here rather than derived
#: from ``CheckResult.name`` so renaming a check — a pure refactor — does
#: not mint a new id and silently auto-resolve the old one. Derived
#: checks (``cadence``, ``coherence``, ``condition``) are keyed by their
#: data (lease name, spec name, finding key) and fall through unchanged.
#: ``tests/test_alert_ids.py`` asserts every constant ``(group, name)``
#: pair in this module is listed here and resolves to a registered rule.
_PINNED_FINGERPRINTS: dict[tuple[str, str], str] = {
    ("ingest", "papers_ingested"): "papers_ingested",
    ("ingest", "news"): "news",
    ("ingest", "chunks_extracted"): "chunks_extracted",
    ("discovery", "chunks_classified"): "chunks_classified",
    ("discovery", "embed"): "embed",
    ("discovery", "chunk_keywords"): "chunk_keywords",
    ("reading", "morning_brief_cast"): "morning_brief_cast",
    ("reading", "cast_audio"): "cast_audio",
    ("reading", "card_forge"): "card_forge",
    ("knowledge", "taproot_edges"): "taproot_edges",
    ("taproot", "claim_hub_dedup_index"): "claim_hub_dedup_index",
    ("nanopub", "staged_candidates_fresh"): "staged_candidates_fresh",
    ("autonomy", "agent_jobs_completing"): "agent_jobs_completing",
    ("autonomy", "lanes_idle_beside_queue"): "lanes_idle_beside_queue",
    ("autonomy", "doctor_report_fresh"): "doctor_report_fresh",
    ("infra", "hosts_alive"): "hosts_alive",
    ("meta", "alert_backlog_rot"): "alert_backlog_rot",
    ("infra", "tool_latency"): "tool_latency",
    ("infra", "tool_latency_regressed"): "tool_latency_regressed",
}


def check_fingerprint(check: CheckResult) -> str:
    """The alert fingerprint a check raises under — pinned when the check
    has a fixed name (:data:`_PINNED_FINGERPRINTS`), else its data-derived
    name."""
    return _PINNED_FINGERPRINTS.get((check.group, check.name), check.name)


def _sync_alerts(store: Store, checks: list[CheckResult]) -> tuple[int, int, bool]:
    """Raise/refresh a ``watchdog:<group>`` alert per stale finding; resolve
    whatever went fresh again. Mirrors ``nursery.run_nursery_pass``'s
    per-source raise-then-resolve-stale sweep. A source that emitted zero
    checks this eval (finding-only groups, e.g. ``_layer2_checks``, go
    silent when everything's healthy — no group key at all) is still
    swept: any currently-open ``watchdog:<source>`` alert with no
    corresponding entry in ``by_group`` is resolved outright, so an
    all-quiet group doesn't strand its alerts (and downstream marker
    gripes) open forever.

    Returns ``(raised, resolved, degraded)`` — ``degraded`` is ``True`` iff
    at least one finding is a *first* sighting (``raise_alert``'s
    ``is_new``): a check that was ``ok`` (or didn't exist) last eval and is
    ``stale`` now. That, not "is anything currently stale", is the push
    policy's degradation signal — a standing, already-alerted condition
    must not re-trigger an off-cycle push every hour.
    """
    by_group: dict[str, list[CheckResult]] = {}
    for c in checks:
        by_group.setdefault(c.group, []).append(c)

    raised = 0
    resolved = 0
    degraded = False
    for group, group_checks in by_group.items():
        source = f"watchdog:{group}"
        live: list[str] = []
        for c in group_checks:
            if not c.is_finding:
                continue
            fp = check_fingerprint(c)
            live.append(fp)
            _ref_id, is_new = raise_alert(
                store,
                source=source,
                fingerprint=fp,
                title=f"[{group}] {c.name} stale",
                detail=c.detail,
                severity=c.severity,
            )
            raised += 1
            degraded = degraded or is_new
        resolved += resolve_stale_alerts(store, source=source, live_fingerprints=live)

    covered = {f"watchdog:{group}" for group in by_group}
    with store.pool.connection() as conn:
        quiet_sources = conn.execute(
            """
            SELECT DISTINCT r.alert_source
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert' AND r.retired_at IS NULL
               AND r.alert_source LIKE 'watchdog:%%'
               AND t.namespace = 'OPEN' AND t.value = %s
            """,
            (STATE_OPEN,),
        ).fetchall()
    for (source,) in quiet_sources:
        if source in covered:
            continue
        resolved += resolve_stale_alerts(store, source=source, live_fingerprints=[])
    return raised, resolved, degraded


# ── remediation router: condition-linked gripe filing (§D Phase 2) ──────

#: Marker line the router's gripe body starts with — the exactly-once /
#: auto-close dedup key. A fresh scan of open gripes' body chunks for this
#: exact prefix (not an in-memory or app_settings map) is the single
#: source of truth every eval, so it survives a restart/crash between
#: evals. Keep the trailing space — the payload is ``<source>/<fingerprint>``.
_ROUTER_MARKER_PREFIX = "watchdog-condition: "

#: check-group → routing class. Everything not listed here falls through
#: to :data:`_ROUTER_DEFAULT_CLASS` ("outcome") — the catch-all for the
#: curated Layer-1 outcome groups (``ingest``, ``reading``, ``knowledge``,
#: ``autonomy``, ``infra``, …), so a newly-added outcome check needs zero
#: router edits, mirroring the rest of this module's zero-per-check-config
#: discipline.
_ROUTER_CLASS: dict[str, str] = {
    "cadence": "transient",
    "coherence": "config-drift",
    "discovery": "pipeline-stuck",
    "nursery-backlog": "backlog-nondraining",
    "tracked-source": "tracked-source",
}
_ROUTER_DEFAULT_CLASS = "outcome"

#: check-group → self-heal budget hours (age of the open watchdog alert,
#: i.e. time since ``created_at`` — the alert IS the "how long has this
#: persisted" clock; the router doesn't reinvent it). Groups not listed
#: fall through to the "outcome" class's warn/info split below.
_ROUTER_BUDGETS: dict[str, float] = {
    "cadence": 6.0,  # catch_up/self-heal gets first shot
    "coherence": 24.0,  # slow rot, exactly the class that rots
    "discovery": 12.0,
}

#: The "outcome" class's default budget for a warn-severity finding; an
#: info-severity outcome finding never gripes (``None``, see
#: :func:`_router_budget_hours`).
_ROUTER_OUTCOME_BUDGET_HOURS = 24.0

#: check-groups that NEVER route to a gripe regardless of age. Today just
#: ``meta`` — home of ``alert_backlog_rot`` (the "the response loop itself
#: is rotting" finding): a gripe about gripe-rot would feed the very rot
#: it reports, so the digest itself is the rail for this one.
_ROUTER_NEVER: frozenset[str] = frozenset({"meta"})

#: check-NAME (``CheckResult.name`` / alert fingerprint) → budget-hours
#: override, for the rare check that needs a different budget than its
#: group's default. Empty today — exceptions land here, not by editing
#: :data:`_ROUTER_BUDGETS` (which would move the whole group).
_ROUTER_OVERRIDES: dict[str, float] = {}

#: Max NEW gripes filed in one eval. Over-cap over-budget findings are
#: left for the next eval (logged, not lost) — a burst of simultaneous
#: conditions (e.g. a shared dependency going down) must not dump a wall
#: of gripes on one pass.
_ROUTER_FLOOD_CAP = 3

#: Nursery capped-backlog alert sources (each detector ends LIMIT 50 and
#: carries a true `total` in alert meta). These get ONE aggregate,
#: auto-closing marker gripe per category when the backlog is not
#: draining — a human hand-off, never an alert auto-resolve. Critical
#: nursery categories are intentionally excluded (they page in real-time).
#: A surfaced alert bound to a live gripe is dispositioned — someone owns
#: it — so it doesn't count toward "not draining" (gr488682).
_NURSERY_BACKLOG_SOURCES: frozenset[str] = frozenset(
    {"nursery:orphan", "nursery:stuck-doable", "nursery:child-failed-parked"}
)
#: Aggregate fingerprint for a non-draining nursery backlog (one per source).
_NURSERY_BACKLOG_FP = "backlog"
#: Synthetic router group for nursery backlog gripes (drives class/nudge).
_NURSERY_BACKLOG_GROUP = "nursery-backlog"
#: Slow-rot self-heal budget before a non-draining nursery backlog gripes.
_NURSERY_BACKLOG_BUDGET_HOURS = 24.0

#: Registry of non-watchdog alert sources that get a find-or-file tracker
#: gripe per ``(source, fingerprint)`` — source → self-heal budget hours.
#: Adding a source is one entry here, not a router branch. The marker scan
#: counts only live (non-retired, non-terminal) gripes, so a tracker that
#: was soft-deleted is simply not found and the next eval re-binds the
#: still-open alert to a fresh one; repeats dedup on the marker.
#: ``mode`` is ``"per-fingerprint"`` (one tracker per fingerprint — e.g.
#: one per OTS batch) or ``"aggregate"`` (one tracker per source, listing
#: the live fingerprints; closes only when every fingerprint has cleared —
#: e.g. N blocked quests are one gripe, not N).
_TRACKED_SOURCES: dict[str, tuple[float, str]] = {
    "nanopub_ots": (6.0, "per-fingerprint"),
    "quest_tick": (6.0, "aggregate"),
}
#: Marker fingerprint of an aggregate-mode tracker (one per source).
_TRACKED_AGG_FP = "all"
#: Synthetic router group for tracked-source gripes (drives class/nudge).
_TRACKED_GROUP = "tracked-source"


def _router_class(group: str) -> str:
    return _ROUTER_CLASS.get(group, _ROUTER_DEFAULT_CLASS)


def _router_budget_hours(name: str, group: str, severity: str) -> float | None:
    """Self-heal budget (hours) before ``name`` routes to a gripe, or
    ``None`` if it never should. Precedence: per-check override → the
    group's budget → the "outcome" class's warn/info split — mirrors
    :func:`_router_class`'s same group-first, catch-all-last shape."""
    if name in _ROUTER_OVERRIDES:
        return _ROUTER_OVERRIDES[name]
    if group in _ROUTER_NEVER:
        return None
    if group in _ROUTER_BUDGETS:
        return _ROUTER_BUDGETS[group]
    return _ROUTER_OUTCOME_BUDGET_HOURS if severity == _WARN else None


def _cadence_last_host(conn: Any, name: str) -> str:
    row = conn.execute(
        "SELECT last_host FROM scheduler_leases WHERE name = %s", (name,)
    ).fetchone()
    return str(row[0]) if row and row[0] else "unknown"


def _router_nudge(store: Store, cls: str, fingerprint: str) -> str:
    """The class-specific line appended after ``Class: <cls>.`` in a filed
    gripe's body (module docstring's Piece 1 template). Opens its own
    read-only connection for the (rare, gripe-filing-only) lookups it
    needs — deliberately never shares the caller's write transaction, so a
    defensive rollback inside :func:`_diagnose_embed_pipeline` can never
    abort the gripe INSERT alongside it."""
    if cls == "config-drift":
        return (
            "intended-on but silent — check service_config prio rows for "
            f"{fingerprint} and the host units' env; the exact toggle is "
            "`precis service …` / the /factory console."
        )
    if cls == "transient":
        with store.pool.connection() as conn:
            last_host = _cadence_last_host(conn, fingerprint)
        return (
            f"cadence {fingerprint} stopped firing past its own "
            "interval+margin and outlived catch_up — check scheduler_leases "
            f"+ worker_logs on {last_host}."
        )
    if cls == "pipeline-stuck":
        if fingerprint == _EMBED_BACKLOG_KEY:
            with store.pool.connection() as conn:
                return _diagnose_embed_pipeline(conn)
        return (
            "backlog not draining past budget — check the corresponding "
            f"worker pass's worker_logs for {fingerprint}."
        )
    if cls == "backlog-nondraining":
        return (
            "capped backlog not draining past budget — this is prod-ops "
            "disposition (triage/close the underlying todos), not a code "
            "fix. The count is in the gripe body; nursery auto-resolves "
            "each item as it's fixed and this gripe auto-closes when the "
            "category fully clears."
        )
    if cls == "tracked-source":
        return (
            "this alert source has no other owner — read the alert "
            "(get(kind='alert', id='<source>/<fingerprint>')) for the "
            "cause. Repeat raises dedup onto this gripe, and it auto-closes "
            "when the alert clears."
        )
    return (
        f"pulse-probe outcome {fingerprint!r} missed its freshness window — "
        "check the underlying pass's worker_logs."
    )


# STATUS values the marker scan below treats as "still live" — every
# non-terminal stage of the gripe lifecycle. Mirrors the canonical
# vocabulary in ``precis_web.routes.gripes`` (``STATUS_VALUES`` /
# ``TERMINAL_VALUES = ("done", "wontfix")``); duplicated locally rather
# than imported because ``precis.workers`` -> ``precis_web`` is a
# forbidden edge under the import-linter contracts. A human/agent
# triaging a router-filed marker gripe (``open`` -> ``triaged`` etc.) is
# a normal backlog action and must not make the scan blind to it — only
# ``done``/``wontfix`` (deliberately closed) drop out.
_MARKER_GRIPE_LIVE_STATUSES: tuple[str, ...] = (
    "open",
    "triaged",
    "ready_for_fix",
    "in_review",
)


def _open_marker_gripes(store: Store) -> dict[tuple[str, str], int]:
    """``{(source, fingerprint): gripe_ref_id}`` for every currently live
    (:data:`_MARKER_GRIPE_LIVE_STATUSES`, not deleted) gripe whose body
    starts with the :data:`_ROUTER_MARKER_PREFIX` marker line — the
    single scan both the dedup-before-file check and the auto-close
    sweep read, so the two can't drift (one query, one truth, every
    eval). "Live" is any non-terminal STATUS, not just ``open``: a
    marker gripe that's been triaged (or further along) still tracks a
    real condition and must keep suppressing duplicate filing / stay
    eligible for auto-close; only ``done``/``wontfix`` (a deliberate
    resolution) drop out, so a legitimate re-file is still possible if
    the condition recurs after one of those."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, c.text
              FROM refs r
              JOIN chunks c
                     ON c.ref_id = r.ref_id
                    AND c.chunk_kind = 'gripe_body' AND c.ord = 0
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL
               AND t.namespace = 'STATUS' AND t.value = ANY(%s)
               AND c.text LIKE %s
            """,
            (list(_MARKER_GRIPE_LIVE_STATUSES), f"{_ROUTER_MARKER_PREFIX}%"),
        ).fetchall()
    out: dict[tuple[str, str], int] = {}
    for gripe_ref_id, text in rows:
        first_line = (text or "").splitlines()[0] if text else ""
        condition = first_line[len(_ROUTER_MARKER_PREFIX) :].strip()
        source, _, fingerprint = condition.partition("/")
        if source and fingerprint:
            out[(source, fingerprint)] = int(gripe_ref_id)
    return out


def _tracked_alert_ids(store: Store, alert_ids: list[int]) -> set[int]:
    """Alert ref ids already bound to a live gripe — any link in either
    direction to a non-retired, non-terminal (not ``done``/``wontfix``)
    gripe. A dispositioned condition is owned work, not unhandled backlog:
    it must not feed the nursery-backlog aggregate's "not draining"
    verdict (gr488682 — a parked leaf with a live tracker kept the
    category's marker gripe open forever). A dead tracker releases the
    binding, exactly like the marker scan's live-status rule."""
    if not alert_ids:
        return set()
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT a.ref_id
              FROM refs a
              JOIN links l
                ON a.ref_id IN (l.src_ref_id, l.dst_ref_id)
              JOIN refs g
                ON g.ref_id IN (l.src_ref_id, l.dst_ref_id)
               AND g.ref_id <> a.ref_id
               AND g.kind = 'gripe'
               AND g.retired_at IS NULL
             WHERE a.ref_id = ANY(%s)
               AND NOT EXISTS (
                     SELECT 1 FROM ref_tags rt
                      JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = g.ref_id
                       AND t.namespace = 'STATUS'
                       AND t.value = ANY(%s))
            """,
            (list(alert_ids), ["done", "wontfix"]),
        ).fetchall()
    return {int(r[0]) for r in rows}


def _auto_close_marker_gripe(
    store: Store, gripe_id: int, source: str, fingerprint: str
) -> None:
    """One marker gripe whose condition cleared (no live open watchdog
    alert for its ``(source, fingerprint)`` — ``resolve_stale_alerts``
    already closed the alert this eval): append the resolution comment,
    then soft-delete. Mirrors ``handlers/gripe.py``'s ``_append_comment``
    (next_pos computed before the write tx) + delete path at the SQL
    level."""
    from precis.store.types import ChunkInsert

    existing = store.chunks.list_chunks_for_ref(gripe_id)
    next_pos = len(existing)
    comment = (
        f"auto-closed by health_digest: condition {fingerprint} returned "
        f"fresh at {datetime.now(UTC).isoformat()}"
    )
    with store.tx() as conn:
        store.chunks.insert_chunks(
            gripe_id,
            [
                ChunkInsert(
                    ord=next_pos, text=comment, meta={"chunk_kind": "gripe_comment"}
                )
            ],
            conn=conn,
        )
        store.retire_ref(gripe_id, conn=conn)
    log.info(
        "health_digest: router auto-closed gripe id=%d (%s/%s fresh)",
        gripe_id,
        source,
        fingerprint,
    )


def _file_router_gripe(
    store: Store,
    *,
    source: str,
    fingerprint: str,
    group: str,
    title: str,
    detail: str,
    created_at: datetime,
    budget_hours: float,
) -> int:
    """Insert one condition-linked gripe (marker-line protocol — module
    docstring's Piece 1 template). Direct store insert-ref + body chunk +
    ``STATUS:open`` tag, mirroring ``workers/backlog_groom.py``'s
    worker-side ref-creation pattern (not ``file_gripe_readonly`` — that
    SQL function exists so an ``agent_ro`` connection can still file a
    gripe; this background pass already runs write-capable)."""
    from precis.store.types import ChunkInsert, Tag

    cls = _router_class(group)
    nudge = _router_nudge(store, cls, fingerprint)
    body = (
        f"{_ROUTER_MARKER_PREFIX}{source}/{fingerprint}\n"
        f"{title}\n{detail}\n"
        f"First seen: {created_at.isoformat()}  ·  filed after "
        f"{budget_hours:.0f}h self-heal budget.\n"
        f"Class: {cls}. {nudge}\n"
        "This gripe auto-closes when the check returns fresh (health_digest router)."
    )
    tag = "alert:" + source if group == _TRACKED_GROUP else "watchdog"
    gripe_title = f"[{tag}] {source}/{fingerprint}: {title}"
    if len(gripe_title) > 200:
        gripe_title = gripe_title[:197].rstrip() + "…"
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="gripe", slug=None, title=gripe_title, meta={}, conn=conn
        )
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "gripe_body"})],
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.add_tag(
            ref.id, Tag.open("origin:health-digest-router"), set_by="system", conn=conn
        )
    log.info(
        "health_digest: router filed gripe id=%d for %s/%s (age > %.0fh budget)",
        ref.id,
        source,
        fingerprint,
        budget_hours,
    )
    return int(ref.id)


def _route_findings(store: Store) -> frozenset[tuple[str, str]]:
    """The remediation router: condition-linked gripe filing with
    exactly-once + auto-close, self-heal-budget suppression, and a
    per-eval flood cap (module docstring's Piece 1). Runs after
    :func:`_sync_alerts`, so it reads the just-synced open
    ``watchdog:<group>`` alert set — an alert's ``created_at`` is already
    "how long has this condition persisted", so the router reads it
    rather than keeping its own clock.

    Returns the ``(group, name)`` pairs that carry an open marker gripe
    after this eval (auto-closes removed, new files added) — used only to
    prefix a routed finding louder in the push-rendered digest
    (``⛳ gripe filed:``); the marker scan itself, not this return value,
    is the actual source of truth (see :func:`_open_marker_gripes`).
    """
    with store.pool.connection() as conn:
        alert_rows = conn.execute(
            f"""
            SELECT r.ref_id, r.alert_source, r.fingerprint, r.created_at,
                   r.title, r.meta->>'detail' AS detail,
                   r.meta->>'severity' AS severity, r.meta->>'total' AS total
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'alert' AND r.retired_at IS NULL
               AND (r.alert_source LIKE 'watchdog:%%'
                    OR r.alert_source = ANY(%s)
                    OR r.alert_source = ANY(%s))
               AND r.alert_source IS NOT NULL AND r.fingerprint IS NOT NULL
               AND t.namespace = 'OPEN' AND t.value = %s
               AND NOT {ACKED_SQL}
            """,
            (list(_NURSERY_BACKLOG_SOURCES), list(_TRACKED_SOURCES), STATE_OPEN),
        ).fetchall()

    marker_gripes = _open_marker_gripes(store)

    # ``live_conditions`` is the auto-close sweep's truth set. A nursery
    # backlog row aggregates onto its category's single ``(source,
    # "backlog")`` marker slot (never a per-ref fingerprint) — everything
    # else keeps its per-(source, fingerprint) identity as before. Also
    # accumulate, per nursery source, the surfaced count / true total /
    # oldest ``created_at`` needed to file (or skip) the one aggregate
    # gripe below.
    live_conditions: set[tuple[str, str]] = set()
    nursery_agg: dict[str, dict[str, Any]] = {}
    # aggregate-mode tracked sources: source -> [(created_at, fingerprint)]
    tracked_agg: dict[str, list[tuple[datetime, str]]] = {}
    # Nursery per-ref alerts bound to a live tracker gripe are
    # dispositioned, not unhandled backlog: they don't count toward the
    # aggregate's live/surfaced/oldest, so a category whose every surfaced
    # condition is owned reads clear (and its marker gripe auto-closes).
    nursery_tracked = _tracked_alert_ids(
        store,
        [int(r[0]) for r in alert_rows if str(r[1]) in _NURSERY_BACKLOG_SOURCES],
    )
    for _rid, source, fp, created_at, _title, _detail, _severity, total in alert_rows:
        source = str(source)
        fp = str(fp)
        if source in _NURSERY_BACKLOG_SOURCES:
            if int(_rid) in nursery_tracked:
                continue
            live_conditions.add((source, _NURSERY_BACKLOG_FP))
            agg = nursery_agg.setdefault(
                source, {"surfaced": 0, "total": 0, "oldest": created_at}
            )
            agg["surfaced"] += 1
            agg["total"] = max(agg["total"], int(total) if total is not None else 0)
            if agg["oldest"] is None or (
                created_at is not None and created_at < agg["oldest"]
            ):
                agg["oldest"] = created_at
        elif _TRACKED_SOURCES.get(source, (0.0, ""))[1] == "aggregate":
            live_conditions.add((source, _TRACKED_AGG_FP))
            tracked_agg.setdefault(source, []).append((created_at, fp))
        else:
            live_conditions.add((source, fp))

    # Auto-close sweep: a marker gripe whose condition is no longer a live
    # open watchdog alert — the check went fresh, resolve_stale_alerts
    # already closed the alert above in _sync_alerts.
    for (source, fingerprint), gripe_id in list(marker_gripes.items()):
        if (source, fingerprint) in live_conditions:
            continue
        try:
            _auto_close_marker_gripe(store, gripe_id, source, fingerprint)
        except Exception:
            log.exception(
                "health_digest: router failed to auto-close gripe id=%d (%s/%s)",
                gripe_id,
                source,
                fingerprint,
            )
            continue
        del marker_gripes[(source, fingerprint)]

    # File: over-budget open alerts not already carrying a marker gripe,
    # oldest condition first, bounded by the flood cap.
    filed = 0
    routed: set[tuple[str, str]] = set()
    for (
        _ref_id,
        source,
        fingerprint,
        created_at,
        title,
        detail,
        severity,
        _total,
    ) in sorted(alert_rows, key=lambda row: row[3]):
        source = str(source)
        fingerprint = str(fingerprint)
        # Nursery backlog rows never file per-ref — handled in aggregate,
        # below, after this watchdog loop.
        if source in _NURSERY_BACKLOG_SOURCES:
            continue
        if source in tracked_agg:
            continue  # aggregate mode — filed once per source, below
        budget: float | None
        if source in _TRACKED_SOURCES:
            group = _TRACKED_GROUP
            budget = _TRACKED_SOURCES[source][0]
        else:
            group = source.removeprefix("watchdog:")
            budget = _router_budget_hours(fingerprint, group, severity or _WARN)
        if budget is None:
            continue
        age = _hours_since(created_at)
        if age is None or age <= budget:
            continue
        routed_key = (
            (source, fingerprint) if group == _TRACKED_GROUP else (group, fingerprint)
        )
        if (source, fingerprint) in marker_gripes:
            routed.add(routed_key)
            continue
        if filed >= _ROUTER_FLOOD_CAP:
            log.warning(
                "health_digest: router flood cap (%d) hit — %s/%s over "
                "budget (%.1fh > %.0fh) held for next eval",
                _ROUTER_FLOOD_CAP,
                source,
                fingerprint,
                age,
                budget,
            )
            continue
        try:
            _file_router_gripe(
                store,
                source=source,
                fingerprint=fingerprint,
                group=group,
                title=title,
                detail=detail or "",
                created_at=created_at,
                budget_hours=budget,
            )
        except Exception:
            log.exception(
                "health_digest: router failed to file gripe for %s/%s",
                source,
                fingerprint,
            )
            continue
        filed += 1
        routed.add(routed_key)

    # Aggregate-mode tracked sources: ONE tracker per source, listing the
    # live fingerprints; closes (auto-close sweep above) only when none
    # of its alerts is open any more.
    for source, items in tracked_agg.items():
        budget = _TRACKED_SOURCES[source][0]
        oldest = min(c for c, _ in items if c is not None)
        age = _hours_since(oldest)
        if age is None or age <= budget:
            continue
        if (source, _TRACKED_AGG_FP) in marker_gripes:
            routed.add((source, _TRACKED_AGG_FP))
            continue
        if filed >= _ROUTER_FLOOD_CAP:
            log.warning(
                "health_digest: router flood cap (%d) hit — %s held for next eval",
                _ROUTER_FLOOD_CAP,
                source,
            )
            continue
        fps = sorted(fp for _, fp in items)
        try:
            _file_router_gripe(
                store,
                source=source,
                fingerprint=_TRACKED_AGG_FP,
                group=_TRACKED_GROUP,
                title=f"{len(fps)} open alert(s) from {source}",
                detail="Live fingerprints when filed:\n"
                + "\n".join(f"- {fp}" for fp in fps),
                created_at=oldest,
                budget_hours=budget,
            )
        except Exception:
            log.exception("health_digest: router failed to file gripe for %s", source)
            continue
        filed += 1
        routed.add((source, _TRACKED_AGG_FP))

    # Nursery backlog aggregates: ONE marker gripe per non-draining
    # category, never per ref — the exact per-leaf noise this design
    # forbids (each capped detector already surfaces up to 50 individual
    # per-ref alerts of its own; those stay per-ref, only the "is this
    # category draining" verdict is aggregated here).
    for source, agg in nursery_agg.items():
        oldest = agg["oldest"]
        age = _hours_since(oldest)
        if age is None or age <= _NURSERY_BACKLOG_BUDGET_HOURS:
            continue
        if (source, _NURSERY_BACKLOG_FP) in marker_gripes:
            routed.add((_NURSERY_BACKLOG_GROUP, source))
            continue
        if filed >= _ROUTER_FLOOD_CAP:
            log.warning(
                "health_digest: router flood cap (%d) hit — %s/%s over "
                "budget (%.1fh > %.0fh) held for next eval",
                _ROUTER_FLOOD_CAP,
                source,
                _NURSERY_BACKLOG_FP,
                age,
                _NURSERY_BACKLOG_BUDGET_HOURS,
            )
            continue
        total = int(agg["total"])
        surfaced = int(agg["surfaced"])
        title = f"{source}: backlog not draining ({total} live, {surfaced} surfaced)"
        detail = (
            f"{total} live conditions ({surfaced} surfaced, oldest {age:.0f}h) "
            f"and not draining past the {_NURSERY_BACKLOG_BUDGET_HOURS:.0f}h "
            "budget — prod-ops disposition (triage/close the underlying todos)."
        )
        try:
            _file_router_gripe(
                store,
                source=source,
                fingerprint=_NURSERY_BACKLOG_FP,
                group=_NURSERY_BACKLOG_GROUP,
                title=title,
                detail=detail,
                created_at=oldest,
                budget_hours=_NURSERY_BACKLOG_BUDGET_HOURS,
            )
        except Exception:
            log.exception(
                "health_digest: router failed to file nursery-backlog gripe for %s",
                source,
            )
            continue
        filed += 1
        routed.add((_NURSERY_BACKLOG_GROUP, source))
    return frozenset(routed)


# ── checks snapshot + agent-facing health panel ─────────────────────────


def _check_to_json(c: CheckResult) -> dict[str, Any]:
    return {
        "group": c.group,
        "name": c.name,
        "status": c.status,
        "detail": c.detail,
        "severity": c.severity,
        "age_hours": c.age_hours,
        "id": f"watchdog:{c.group}/{check_fingerprint(c)}",
    }


def _store_checks_snapshot(store: Store, checks: list[CheckResult]) -> None:
    """Persist this eval's full check set as JSON under
    :data:`CHECKS_SNAPSHOT_KEY` — one ``app_settings`` row both the MCP
    and web processes read (no cross-process cache). The backlog checks
    cost ~30 s of full-table scans (D3), so the panel reads the snapshot
    with its age rather than recomputing."""
    import json

    from precis.budget import settings as app_settings

    payload = {
        "computed_at": datetime.now(UTC).isoformat(),
        "checks": [_check_to_json(c) for c in checks],
    }
    app_settings.set_setting(store, CHECKS_SNAPSHOT_KEY, json.dumps(payload))


def _load_checks_snapshot(store: Store) -> dict[str, Any] | None:
    import json

    from precis.budget import settings as app_settings

    raw = app_settings.get_setting(store, CHECKS_SNAPSHOT_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) and "checks" in data else None


@contextmanager
def _conn_or_pool(store: Store, conn: Any | None) -> Iterator[Any]:
    """Yield ``conn`` when the caller already holds one (``/status``'s
    request-shared connection), else a fresh pooled connection."""
    if conn is not None:
        yield conn
    else:
        with store.pool.connection() as pooled:
            yield pooled


#: Open critical alerts the panel names by id (the count covers the rest).
_PANEL_CRITICAL_CAP = 10

#: :data:`_FRESHNESS_CHECKS` whose verdict :func:`_layer1_checks` overrides
#: (``chunks_classified``: the ``service_config`` gate and the eligible-pool
#: probe). :func:`health_panel`'s live re-probe sees only the raw marker age,
#: so it may refresh these rows to ``ok`` but never overwrite the snapshot's
#: verdict with a raw ``stale``. A pool that refills mid-hour therefore
#: reads ``ok`` on the panel until the next eval (at most one cadence).
#: With no snapshot row yet, the raw live reading is shown.
_EVAL_GATED_FRESHNESS = frozenset({"chunks_classified"})


def health_panel(store: Store, conn: Any | None = None) -> dict[str, Any]:
    """The per-subsystem health read — agent (``id='/health'``) and web
    (``/status`` Health sub-tab) render literally this object.

    Same states ``/status`` and the digest show, in one call, without the
    expensive part: every check comes from the last eval's snapshot
    (hourly; ``snapshot_age_hours`` says how old), except the cheap
    :data:`_FRESHNESS_CHECKS` probes and ``hosts_alive``, which are re-run
    live and overlay their snapshot rows. ``checks`` is a list of the snapshot's JSON
    shape (``group``/``name``/``status``/``detail``/``severity``/
    ``age_hours``/``id``) with a ``live`` flag; the ``id`` is the
    registered failure id (``watchdog:<group>/<fingerprint>``), so a
    stale row can be followed straight to its rule and its open alert.
    ``open_alerts`` is the open-alert count by severity; ``open_critical``
    names the open critical alerts (``ref_id``/``title``, newest first,
    capped at :data:`_PANEL_CRITICAL_CAP`) so a count is never the only
    trace of a page (gr489194). Returns
    ``snapshot_age_hours=None`` and an empty ``checks`` list when no
    eval has run yet (a fresh install) — never raises.

    ``conn`` lets a caller with an already-open connection (the
    ``/status`` route's request-shared one) run the live reads on it
    instead of checking out two more pooled connections.
    """
    snap = _load_checks_snapshot(store)
    checks: list[dict[str, Any]] = []
    computed_at: datetime | None = None
    if snap is not None:
        try:
            computed_at = datetime.fromisoformat(str(snap.get("computed_at")))
            if computed_at.tzinfo is None:
                computed_at = computed_at.replace(tzinfo=UTC)
        except ValueError:
            computed_at = None
        checks = [dict(c, live=False) for c in snap["checks"] if isinstance(c, dict)]

    try:
        with _conn_or_pool(store, conn) as c:
            live = _freshness_layer1(c)
            # gr489195: one indexed heartbeat read (sub-ms on prod) — a
            # host back from dark must not read dark for up to an hour.
            live.append(_check_hosts_alive(c))
    except Exception:
        log.exception("health_panel: live freshness probes raised")
        live = []
    by_id = {c.get("id"): i for i, c in enumerate(checks)}
    for c in live:
        row = dict(_check_to_json(c), live=True)
        idx = by_id.get(row["id"])
        if idx is None:
            checks.append(row)
        elif c.name in _EVAL_GATED_FRESHNESS and c.status != "ok":
            # gr489780: the raw marker age is only half this check; the
            # eval's gate/pool override lives in the snapshot row, so a
            # live "stale" must not overwrite its "backlog drained".
            continue
        else:
            checks[idx] = row

    open_by_sev: dict[str, int] = {}
    try:
        with _conn_or_pool(store, conn) as c:
            rows = c.execute(
                """
                SELECT r.meta->>'severity', count(*)
                  FROM refs r
                  JOIN ref_tags rt ON rt.ref_id = r.ref_id
                  JOIN tags t ON t.tag_id = rt.tag_id
                 WHERE r.kind = 'alert' AND r.retired_at IS NULL
                   AND t.namespace = 'OPEN' AND t.value = %s
                 GROUP BY 1
                """,
                (STATE_OPEN,),
            ).fetchall()
        open_by_sev = {str(sev or "warn"): int(n) for sev, n in rows}
    except Exception:
        log.exception("health_panel: open-alert count raised")

    open_critical: list[dict[str, Any]] = []
    if open_by_sev.get("critical"):
        try:
            with _conn_or_pool(store, conn) as c:
                crit = c.execute(
                    """
                    SELECT r.ref_id, r.title
                      FROM refs r
                      JOIN ref_tags rt ON rt.ref_id = r.ref_id
                      JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE r.kind = 'alert' AND r.retired_at IS NULL
                       AND t.namespace = 'OPEN' AND t.value = %s
                       AND r.meta->>'severity' = 'critical'
                     ORDER BY r.updated_at DESC
                     LIMIT %s
                    """,
                    (STATE_OPEN, _PANEL_CRITICAL_CAP),
                ).fetchall()
            open_critical = [{"ref_id": int(i), "title": t or ""} for i, t in crit]
        except Exception:
            log.exception("health_panel: open-critical list raised")

    return {
        "computed_at": computed_at,
        "snapshot_age_hours": _hours_since(computed_at),
        "checks": checks,
        "open_alerts": open_by_sev,
        "open_critical": open_critical,
    }


# ── push policy: pure template, daily heartbeat + on-degradation ────────


def _render_digest(
    checks: list[CheckResult],
    *,
    routed: frozenset[tuple[str, str]] = frozenset(),
) -> str:
    """Pure-template digest body — grouped, worst/oldest first, age shown.

    ``routed`` (``(group, name)`` pairs, from :func:`_route_findings`)
    prefixes a stale line ``⛳ gripe filed:`` — the age-escalation
    observability the router's proposal asks for: a routed finding must
    stand out in the push body, not just exist as a gripe an operator
    has to go looking for.

    No LLM anywhere in this module (asserted by
    ``tests/workers/test_health_digest.py``) — the digest must still send
    when the LLM/agent fleet is down."""
    stale = [c for c in checks if c.status == "stale"]
    unknown = [c for c in checks if c.status == "unknown"]
    if not stale and not unknown:
        return "✅ health_digest: all green (" + str(len(checks)) + " checks)"

    lines: list[str] = []
    if stale:
        # Worst/oldest first: warn before info, then oldest age first.
        ordered = sorted(
            stale,
            key=lambda c: (
                0 if c.severity == _WARN else 1,
                -(c.age_hours if c.age_hours is not None else 1e9),
            ),
        )
        lines.append(f"⚠️ {len(stale)} stale check(s):")
        for c in ordered:
            age = f" [{c.age_hours:.1f}h]" if c.age_hours is not None else ""
            marker = "⛳ gripe filed: " if (c.group, c.name) in routed else ""
            lines.append(
                f"  - {marker}({c.severity}) {c.group}/{c.name}{age}: {c.detail}"
            )
    if unknown:
        lines.append(f"❓ {len(unknown)} check(s) could not run (probe error):")
        for c in unknown:
            lines.append(f"  - {c.group}/{c.name}: {c.detail}")
    ok_n = len(checks) - len(stale) - len(unknown)
    lines.append(f"({ok_n}/{len(checks)} checks green)")
    return "\n".join(lines)


def _select_push_body(
    checks: list[CheckResult],
    *,
    routed: frozenset[tuple[str, str]] = frozenset(),
    doctor_report: DoctorReport | None = None,
) -> str:
    """Pure push-body selection (``docs/backlog/doctor-tick-report.md``
    item 3) — no DB, no I/O, so the fallback law is provable without a
    store.

    The doctor-authored report replaces the template body only when a
    fresh report was found (the caller has already applied
    ``doctor_report.FRESH_WINDOW`` and failure-isolated the lookup) AND
    it has a non-empty body. The all-green heartbeat stays
    template-only **regardless** of any doctor report — it's the
    dead-man proof, not a report, so it must never be suppressed or
    replaced. Every other path (no report, stale report, empty report,
    lookup failure surfaced as ``None``) falls through to the exact same
    ``_render_digest`` call the pre-cutover code made, byte-identical."""
    template = _render_digest(checks, routed=routed)
    all_green = not any(c.status in ("stale", "unknown") for c in checks)
    if all_green or doctor_report is None or not doctor_report.body.strip():
        return template
    return (
        f"🩺 doctor report — {doctor_report.headline}\n\n"
        f"{doctor_report.body}\n\n"
        "(doctor-authored report; template digest below was suppressed "
        "while this report is fresh)"
    )


def _lookup_fresh_doctor_report(store: Store) -> DoctorReport | None:
    """Best-effort fresh doctor report for the push-body selector.

    Failure-isolated on purpose: an import error, a DB blip, a schema
    surprise on the doctor's own report draft — none of it may block the
    digest send. Any exception here degrades to ``None``, which
    :func:`_select_push_body` treats exactly like "no report exists"
    (design law 9: the digest must send when the LLM/fleet is down)."""
    try:
        from precis.workers import doctor_report

        return doctor_report.latest_report(store, max_age=doctor_report.FRESH_WINDOW)
    except Exception:
        log.exception(
            "health_digest: doctor report lookup failed, falling back to template"
        )
        return None


def _last_push_at(store: Store) -> datetime | None:
    from precis.budget import settings as app_settings

    raw = app_settings.get_setting(store, LAST_PUSH_KEY)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _stamp_pushed(store: Store) -> None:
    from precis.budget import settings as app_settings

    app_settings.set_setting(store, LAST_PUSH_KEY, datetime.now(UTC).isoformat())


def _maybe_push(
    store: Store,
    checks: list[CheckResult],
    *,
    degraded: bool,
    routed: frozenset[tuple[str, str]] = frozenset(),
) -> bool:
    """Push the digest when (a) the daily heartbeat is due, or (b) the
    finding set just degraded. Returns ``True`` iff a push was queued."""
    last_push = _last_push_at(store)
    heartbeat_due = (
        last_push is None
        or (datetime.now(UTC) - last_push).total_seconds()
        >= HEARTBEAT_INTERVAL_HOURS * 3600
    )
    if not (heartbeat_due or degraded):
        return False
    # Belt-and-suspenders: _lookup_fresh_doctor_report already isolates its
    # own failures, but the push must go out even if that isolation itself
    # somehow doesn't — the digest send is the one thing that may never be
    # blocked by the doctor-report read.
    try:
        doctor_report = _lookup_fresh_doctor_report(store)
    except Exception:
        log.exception("health_digest: doctor report lookup raised past its own guard")
        doctor_report = None
    body = _select_push_body(checks, routed=routed, doctor_report=doctor_report)
    # Keep the "degraded" marker whenever the finding set degraded, even
    # when the daily heartbeat also happens to be due at the same moment —
    # dropping it just because the heartbeat coincided would hide the more
    # important fact in the title.
    title = "health_digest: degraded" if degraded else "health_digest"
    pushed = queue_ops_message(store, title, body, reason="health_digest")
    # Only stamp last_push when a push actually went out — queue_ops_message
    # returns False (no-op) when PRECIS_OPS_ALERT_TARGET is unset, and a dark
    # target must not silently arm the 24h heartbeat clock while nothing
    # ever gets pushed. NOTE: queue_ops_message currently returns True
    # unconditionally once a target IS configured, even if the DB write
    # inside its try/except raised — that pre-existing quirk (a push can
    # read as "sent" when it wasn't) is out of scope here.
    if pushed:
        _stamp_pushed(store)
    return pushed


# ── dead-man ping ─────────────────────────────────────────────────────


def _ping_deadman() -> None:
    """Fire-and-forget GET of ``PRECIS_DEADMAN_PING_URL`` (SSRF-guarded by
    default; opt-in bypass for a LAN target via
    :data:`DEADMAN_ALLOW_PRIVATE_ENV`, see :func:`_ping_deadman_private`).

    Dark unless the operator sets the env var. A failing ping only logs a
    warning — it must never fail the pass (the external service alarming
    on missed pings, not this call succeeding, is the whole point)."""
    url = os.environ.get(DEADMAN_PING_URL_ENV, "").strip()
    if not url:
        return
    allow_private = os.environ.get(DEADMAN_ALLOW_PRIVATE_ENV, "").strip() == "1"
    try:
        if allow_private:
            _ping_deadman_private(url)
        else:
            from precis.utils.http import http_client
            from precis.utils.safe_fetch import safe_get

            with http_client(
                timeout=10.0, user_agent="precis-mcp/health_digest"
            ) as client:
                safe_get(client, url)
    except Exception as exc:
        from precis.utils.safe_fetch import SsrfBlocked

        if isinstance(exc, SsrfBlocked):
            log.warning(
                "health_digest: dead-man ping to %s blocked by the SSRF guard "
                "(private/loopback/LAN target) — set %s=1 to allow a LAN "
                "dead-man's-switch target (see docs/runbooks/dead-mans-switch.md)",
                url,
                DEADMAN_ALLOW_PRIVATE_ENV,
            )
        else:
            log.warning("health_digest: dead-man ping to %s failed", url, exc_info=True)


def _ping_deadman_private(url: str) -> None:
    """Direct, unguarded GET for an operator-opted-in LAN dead-man target.

    ``safe_fetch``'s SSRF guard exists to stop an *agent-supplied* URL — an
    input we do not control — from reaching a private/loopback/metadata
    address; see its module docstring. ``PRECIS_DEADMAN_PING_URL`` is the
    opposite trust case: an **operator-set env constant**, the same
    footing ``PRECIS_OPS_ALERT_TARGET`` already sits on, not attacker-
    influenced input. ``safe_fetch`` has no "trust this one URL" seam
    today, so this opt-in path — behind :data:`DEADMAN_ALLOW_PRIVATE_ENV`
    (``PRECIS_DEADMAN_ALLOW_PRIVATE=1``, dark by default) — issues a plain
    ``httpx`` GET that bypasses the pinning transport, which would
    otherwise reject a LAN/private target outright.
    """
    from precis.utils.http import require_httpx

    httpx = require_httpx()
    # Raw httpx.Client, not http_client: operator-opted-in LAN target, so the
    # pinning transport (which rejects private addresses) must not apply.
    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        client.get(url, headers={"User-Agent": "precis-mcp/health_digest"})


# ── entrypoint ───────────────────────────────────────────────────────


def run_health_digest_pass(
    store: Store, *, specs: list[ServiceSpec] | None = None
) -> BatchResult:
    """One digest eval: check → alert-sync → remediation router →
    push-policy → dead-man ping.

    ``specs`` is test-only (see :func:`_layer2_checks`). Returns
    ``BatchResult(handler="health_digest", claimed=<checks evaluated>,
    ok=<checks ok>, failed=<checks stale>)``.
    """
    checks = _evaluate_checks(store, specs=specs)

    # Condition registry (self-healing-spine Layer 2, slice 3) — declarative
    # probes evaluated on this hourly lane; their findings ride the same
    # alert-sync / router / push machinery as every other check (group
    # `condition`, source `watchdog:condition`). Lazy import: conditions.py
    # imports CheckResult from this module. Best-effort like every sibling
    # stage — a broken registry must not take the digest down with it.
    cond_findings: list[Any] = []
    try:
        from precis.workers.conditions import run_condition_checks

        cond_checks, cond_findings = run_condition_checks(store)
        checks = checks + cond_checks
    except Exception:
        log.exception("health_digest: condition registry raised")

    stale_n = sum(1 for c in checks if c.status == "stale")
    ok_n = sum(1 for c in checks if c.status == "ok")

    try:
        _store_checks_snapshot(store, checks)
    except Exception:
        log.exception("health_digest: checks snapshot write raised")

    degraded = False
    try:
        _raised, _resolved, degraded = _sync_alerts(store, checks)
    except Exception:
        log.exception("health_digest: alert sync raised")

    # The bounded heal arm (restart-once et al) — dark until
    # PRECIS_RESTART_ONCE_ENABLED=1; runs AFTER the alert sync so the
    # finding is on record before anything is bounced.
    try:
        from precis.workers.conditions import run_condition_heals

        run_condition_heals(store, cond_findings)
    except Exception:
        log.exception("health_digest: condition heal arm raised")

    routed: frozenset[tuple[str, str]] = frozenset()
    try:
        routed = _route_findings(store)
    except Exception:
        log.exception("health_digest: remediation router raised")

    try:
        _maybe_push(store, checks, degraded=degraded, routed=routed)
    except Exception:
        log.exception("health_digest: push policy raised")

    _ping_deadman()

    log.info(
        "health_digest: %d checks (%d ok, %d stale, %d unknown)",
        len(checks),
        ok_n,
        stale_n,
        len(checks) - ok_n - stale_n,
    )
    return BatchResult(
        handler="health_digest", claimed=len(checks), ok=ok_n, failed=stale_n
    )


__all__ = [
    "CHECKS_SNAPSHOT_KEY",
    "DEADMAN_ALLOW_PRIVATE_ENV",
    "DEADMAN_PING_URL_ENV",
    "HEARTBEAT_INTERVAL_HOURS",
    "LAST_PUSH_KEY",
    "CheckResult",
    "check_fingerprint",
    "health_panel",
    "run_health_digest_pass",
]
