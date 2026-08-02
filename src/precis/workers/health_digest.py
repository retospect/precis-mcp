"""``health_digest`` — the §D liveness net (Phase 1 of
``docs/proposals/health-watchdog.md``).

A periodic, outcome-based digest that reaches out (push, not pull) so a
scheduled producer never rots silently for days: nursery already owns the
*critical*, page-immediately lane (dead workers, dispatch stalls, …); this
pass owns the slower "hasn't this been quiet for suspiciously long" lane —
info/warn only, no critical alerts here.

**SQL-first, zero LLM.** Every check is a deterministic query, exactly like
nursery — the watchdog must not depend on the subsystems it monitors (an
LLM-fleet outage is one of the things it most needs to be able to report),
and the digest body is a **pure template**. There is no ``llm`` import
anywhere in this module — enforced by ``tests/workers/test_health_digest.py``.

One fire (:func:`run_health_digest_pass`) does four things:

1. **Evaluate every check**, from three sources (:func:`_evaluate_checks`):

   * **Curated Layer-1 outcome checks** (:data:`_FRESHNESS_CHECKS` +
     the handful of bespoke functions below) — ~15 end-to-end outcomes the
     factory exists to produce, budgets seeded from the design doc's
     pulse-probe observations. The two checks with a natural backlog
     concept (``chunks_embedded`` / ``chunks_keyworded``) are **idle-aware**:
     an empty backlog is healthy no matter how long ago the last batch ran;
     a non-empty, non-draining backlog past budget is not — the
     idle-vs-stuck distinction the design doc calls "the master's
     idle-vs-stuck criterion". They reuse
     :func:`precis.health_checks.compute_backlog_counts` — the exact
     computation ``/status`` renders, so there is one liveness truth, not
     two. No curated cadence-freshness rows live here — see the next
     bullet.
   * **Cadence staleness** (:func:`_cadence_staleness_checks`, derived) —
     every ``scheduler_leases`` row where ``next_fire_at`` is overdue past
     its own interval + a margin. Zero per-cadence config: a newly-added
     cadence in ``workers/scheduler.py`` (including ``dream_agent`` /
     ``anki_sync``) is watched automatically, against the *live*-resolved
     interval — a curated Layer-1 row with its own fixed budget would
     contradict this the moment an operator raises the interval (e.g.
     dream's DB-overridable knob), so it is deliberately not duplicated
     there.
   * **Layer-2 coherence** (:func:`_layer2_checks`, derived) — every
     registered ``PASS`` + ``ref_pass`` :class:`~precis.workers.registry.ServiceSpec`
     that resolves enabled (structural default_profiles/enable_env, or a
     live ``service_config`` prio override) with zero ``worker_logs`` rows
     in 24h reads "intended-on but silent". Derived straight from the
     registry + ``service_config`` + ``worker_logs`` — a new pass needs
     **zero** edits here (``tests/workers/test_health_digest.py`` proves it
     by minting a fake spec).

2. **Findings → alerts** (:func:`_sync_alerts`) — each non-``ok`` check
   raises a ``kind='alert'`` via :func:`precis.alerts.raise_alert` under
   ``alert_source="watchdog:<group>"``, severity capped to info/warn
   (nursery keeps the critical lane); :func:`precis.alerts.resolve_stale_alerts`
   auto-closes whatever went fresh again. No gripe filing in this slice —
   the remediation router is a later phase.
3. **Push policy** (:func:`_maybe_push`) — a templated digest to
   ``PRECIS_OPS_ALERT_TARGET`` (dark if unset) when the daily heartbeat is
   due (``app_settings['health_digest:last_push']`` older than 24h) or the
   finding set just degraded (a check that was ``ok`` last eval is not
   ``ok`` now). An all-green daily push ("✅ all green") IS the internal
   dead-man's proof that the watchdog itself is alive.
4. **Dead-man ping** (:func:`_ping_deadman`) — after a successful eval,
   GET ``PRECIS_DEADMAN_PING_URL`` (when set) via ``safe_fetch.safe_get``.
   Covers the one failure mode nothing DB-mediated can: a total fleet/DB
   outage. A private/loopback/LAN target is blocked by the SSRF guard
   unless the operator opts in with ``PRECIS_DEADMAN_ALLOW_PRIVATE=1``
   (:func:`_ping_deadman_private`) — the guard is built for
   agent-supplied URLs, not this operator-set env constant. See
   ``docs/runbooks/dead-mans-switch.md``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from precis import health_checks
from precis.alerts import queue_ops_message, raise_alert, resolve_stale_alerts
from precis.store import Store
from precis.workers.registry import SERVICES, ServiceKind, ServiceSpec
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: App-settings key holding the ISO-8601 UTC timestamp of the last digest
#: push (heartbeat OR degradation) — the whole "reuse app_settings, don't
#: invent a new throttle table" ask from the spec.
LAST_PUSH_KEY = "health_digest:last_push"

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
#: doc's pulse-probe-seeded values (``docs/proposals/health-watchdog.md``
#: §A "Layer 1 — Outcomes"), not guesses.
_FRESHNESS_CHECKS: tuple[tuple[str, str, str, float, str, str], ...] = (
    (
        "papers_ingested",
        "ingest",
        "SELECT max(created_at) FROM refs WHERE kind = 'paper' AND deleted_at IS NULL",
        6.0,
        _WARN,
        "a new paper landing",
    ),
    (
        "chunks_classified",
        "discovery",
        "SELECT max(ct.created_at) FROM chunk_tags ct "
        "JOIN tags t ON t.tag_id = ct.tag_id WHERE t.namespace = 'role3'",
        12.0,
        _WARN,
        "a chunk classified (role3)",
    ),
    (
        "news",
        "ingest",
        "SELECT max(created_at) FROM refs WHERE kind = 'news' AND deleted_at IS NULL",
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
        "cast_audio",
        "reading",
        "SELECT max(ts) FROM worker_logs WHERE pass = 'cast_audio'",
        26.0,
        _WARN,
        "cast_audio narrating a cast",
    ),
    (
        "taproot_edges",
        "knowledge",
        "SELECT max(created_at) FROM links WHERE relation = ANY(ARRAY"
        f"{list(_TAPROOT_HUB_ROLES)!r}::text[])",
        6.0,
        _WARN,
        "a findings→claims taproot edge",
    ),
)


def _freshness_layer1(conn: Any) -> list[CheckResult]:
    probes = health_checks.fetch_freshness_timestamps(
        conn, [(key, sql) for key, _grp, sql, *_ in _FRESHNESS_CHECKS]
    )
    out: list[CheckResult] = []
    for key, group, _sql, budget_hours, severity, what in _FRESHNESS_CHECKS:
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
                    f"{what}: {age:.1f}h ago (budget {budget_hours:.0f}h)",
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
                f"{what}: last seen {seen} (budget {budget_hours:.0f}h)",
                severity,
                age,
            )
        )
    return out


# ── Layer 1: idle-aware backlog checks (embed / chunk_keywords) ─────────

#: ``(backlog_key, group, budget_hours, severity, what)``
_BACKLOG_CHECKS: tuple[tuple[str, str, float, str, str], ...] = (
    ("embed", "discovery", 2.0, _WARN, "chunks embedded"),
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
        out.append(
            CheckResult(
                group,
                key,
                "stale",
                f"{what}: {pending} pending, NOT draining "
                f"(last batch {last_seen}, budget {budget_hours:.0f}h)",
                severity,
                age,
            )
        )
    return out


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
              WHERE r.kind = 'paper' AND r.deleted_at IS NULL
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


def _check_card_forge(conn: Any) -> CheckResult:
    """Last successful ``card_forge`` job (a ``job_type``, not a worker
    pass — no ``worker_logs`` pass name to key off, so read the job
    lifecycle directly)."""
    sql = """
        SELECT max(rt.created_at)
          FROM refs j
          JOIN ref_tags rt ON rt.ref_id = j.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE j.kind = 'job' AND j.deleted_at IS NULL
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
    age = _hours_since(ts)
    if age is not None and age <= budget_hours:
        return CheckResult(
            "reading",
            "card_forge",
            "ok",
            f"card-forge: {age:.1f}h ago (budget {budget_hours:.0f}h)",
            _WARN,
            age,
        )
    seen = f"{age:.1f}h ago" if age is not None else "never"
    return CheckResult(
        "reading",
        "card_forge",
        "stale",
        f"card-forge: last succeeded {seen} (budget {budget_hours:.0f}h)",
        _WARN,
        age,
    )


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
         WHERE j.kind = 'job' AND j.deleted_at IS NULL
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
    sql = """
        SELECT hh.host, hh.ts
          FROM host_heartbeat hh
         WHERE hh.ts < now() - (%(silence)s || ' minutes')::interval
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
        "(nursery host-dark already paging; see its alert for detail)",
        _WARN,
    )


def _check_alert_backlog_rot(conn: Any) -> CheckResult:
    from precis.alerts import STATE_OPEN

    sql = """
        SELECT count(*)::int
          FROM refs r
          JOIN ref_tags rt ON rt.ref_id = r.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE r.kind = 'alert' AND r.deleted_at IS NULL
           AND t.namespace = 'OPEN' AND t.value = %s
           AND r.created_at < now() - interval '7 days'
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


def _layer1_checks(store: Store) -> list[CheckResult]:
    """All curated Layer-1 outcome checks, one connection, best-effort."""
    with store.pool.connection() as conn:
        out = _freshness_layer1(conn)
        out.append(_check_chunks_extracted(conn))
        out += _idle_aware_backlog_checks(conn)
        out.append(_check_card_forge(conn))
        out.append(_check_agent_jobs_completing(conn))
        out.append(_check_hosts_alive(conn))
        out.append(_check_alert_backlog_rot(conn))
    return out


# ── derived: cadence staleness (scheduler_leases) ────────────────────────


def _cadence_staleness_checks(store: Store) -> list[CheckResult]:
    """Every ``scheduler_leases`` row overdue past ``interval_s + margin``.

    Zero per-cadence config — a cadence added to ``workers/scheduler.py``
    is watched the moment it seeds its first lease row, no digest edit.
    ``margin = max(interval_s, 300s)`` — generous enough that a normal
    scheduling jitter never trips it, tight enough that a genuinely-stopped
    cadence trips within about one missed interval.
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
    now = datetime.now(UTC)
    for lease in leases:
        margin_s = max(lease.interval_s, 300)
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
    structural default (``default_profiles`` non-empty, or ``enable_env``
    set — the same condition ``cli/worker.py::_should_register`` uses).
    """
    structural = {s.name: bool(s.default_profiles) or bool(s.enable_env) for s in specs}
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


def _sync_alerts(store: Store, checks: list[CheckResult]) -> tuple[int, int, bool]:
    """Raise/refresh a ``watchdog:<group>`` alert per stale finding; resolve
    whatever went fresh again. Mirrors ``nursery.run_nursery_pass``'s
    per-source raise-then-resolve-stale sweep.

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
            fp = c.name
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
    return raised, resolved, degraded


# ── push policy: pure template, daily heartbeat + on-degradation ────────


def _render_digest(checks: list[CheckResult]) -> str:
    """Pure-template digest body — grouped, worst/oldest first, age shown.

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
            lines.append(f"  - ({c.severity}) {c.group}/{c.name}{age}: {c.detail}")
    if unknown:
        lines.append(f"❓ {len(unknown)} check(s) could not run (probe error):")
        for c in unknown:
            lines.append(f"  - {c.group}/{c.name}: {c.detail}")
    ok_n = len(checks) - len(stale) - len(unknown)
    lines.append(f"({ok_n}/{len(checks)} checks green)")
    return "\n".join(lines)


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


def _maybe_push(store: Store, checks: list[CheckResult], *, degraded: bool) -> bool:
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
    body = _render_digest(checks)
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
    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        client.get(url, headers={"User-Agent": "precis-mcp/health_digest"})


# ── entrypoint ───────────────────────────────────────────────────────


def run_health_digest_pass(
    store: Store, *, specs: list[ServiceSpec] | None = None
) -> BatchResult:
    """One digest eval: check → alert-sync → push-policy → dead-man ping.

    ``specs`` is test-only (see :func:`_layer2_checks`). Returns
    ``BatchResult(handler="health_digest", claimed=<checks evaluated>,
    ok=<checks ok>, failed=<checks stale>)``.
    """
    checks = _evaluate_checks(store, specs=specs)
    stale_n = sum(1 for c in checks if c.status == "stale")
    ok_n = sum(1 for c in checks if c.status == "ok")

    degraded = False
    try:
        _raised, _resolved, degraded = _sync_alerts(store, checks)
    except Exception:
        log.exception("health_digest: alert sync raised")

    try:
        _maybe_push(store, checks, degraded=degraded)
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
    "DEADMAN_ALLOW_PRIVATE_ENV",
    "DEADMAN_PING_URL_ENV",
    "HEARTBEAT_INTERVAL_HOURS",
    "LAST_PUSH_KEY",
    "CheckResult",
    "run_health_digest_pass",
]
