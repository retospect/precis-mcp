"""Condition registry — declarative health probes with a bounded heal arm
(self-healing-spine Layer 2, slice 3).

One condition = one :class:`Condition` row: a SQL probe that returns
per-instance findings (empty = green), a severity, and optionally a
whitelisted heal request per finding. The hourly lane
(``health_digest``) evaluates the registry: findings become
:class:`~precis.workers.health_digest.CheckResult` rows (group
``condition``) and ride the digest's existing alert-sync / router /
push machinery unchanged; heals run through
:func:`~precis.workers.bounded_heal.run_bounded_heal`.

First rows (the gr204385 family — a handler dead inside a live worker
was invisible for 4 days):

* ``pass-dead-on-host`` — a handler that logged steadily on a
  ``(host, process)`` over the last 7 days has gone silent past budget
  while that host still heartbeats. Exact because ``runner.py`` logs
  every registered pass EVERY cycle, idle or not — silence means the
  handler is dead/wedged, not merely quiet. Names nothing still
  registers (a renamed/removed pass's leftover history — gr501991's
  ``_orcid_enrich_pass`` ghost) are excluded via
  :func:`_liveness_handler_names`. Handlers switched
  off live via ``service_config`` (prio 0) are excluded, as are worker
  units freshly observed inactive AND disabled by the host heartbeat.
  Missing/stale unit observations and failed/enabled units still alert.
* ``rescue-pass-cadence`` — the rescue-critical passes (sweeper,
  nursery, quest_loop_reconcile) have a fleet-wide cadence SLO. For
  sweeper/nursery (system-profile, fast rotation, where a gap IS
  starvation) a gap alone means the rescuers themselves are starved
  (the 2026-08-12 bib_parse starvation: quest_loop_reconcile stretched
  to ~30 min). ``quest_loop_reconcile`` runs in the agent-profile
  rotation instead, where cycles legitimately stretch hours behind a
  long ``claude_inproc`` run — gap alone false-positived on 34% of its
  normal run intervals (gr204708) — so it also needs a demand probe:
  a gap only fires when an active quest has actually waited the whole
  budget with no live loop.
* ``pass-wedged`` — ``host_heartbeat.meta.activity`` shows a pass
  running for hours (fresh heartbeat, stale ``since`` — the dedicated
  heartbeat thread keeps ``ts`` fresh through a wedge, so both signals
  are needed; caspar sat 9 h in ``_bib_parse_pass`` this way).
* ``llm-degraded`` — error-rate per (model, transport, placement) over
  the last hour (``llm_call_log`` has the columns; nothing read them as
  health).
* ``dead-generation-claims`` — claims whose holder generation is
  provably replaced are persisting past the claim reaper's window: the
  reclaim lane itself is broken (the watcher's watcher).
* ``limiter-cap-full`` — the auto-seeded ``host:<netloc>`` limiter rows
  are at ``rate_limit.MAX_SEEDED_HOST_ROWS`` with none idle enough to
  evict, so a newly seen host runs unpaced (gr507345).
* ``limiter-refusing`` — a provider's
  ``external_rate_limits`` row has refused calls past a daily floor
  (``refused_today``/``last_refused_at``, migration 0233 — written by
  every counted refusal: a starved bucket, a spent daily cap, a full
  ``llm-api`` slot hold, or a live Retry-After park). Demand the shared
  limiter could not admit; self-clears once refusals stop or at the day
  rollover.
* ``local-serving-pressure`` — llm-lane-scheduler slice S0: sustained
  KV-cache use over threshold, retractions ticking, or host swap-in
  rising on a serving box, read from the ``serving_*`` columns the
  heartbeat pass scrapes into ``host_heartbeat_log``.
* ``settings-env-shadowed`` — info-only (db-resident-settings.md slice 4):
  a host still has a registered setting's env var set locally (self-
  reported via ``host_heartbeat.meta.settings_env_present``) after a DB
  row has taken over resolution — cleanup visibility for the ansible-diet
  pass, not a failure.
* ``deploy-fleet-coverage`` — gr507870: a heartbeating host that the
  overlay puts in no deploy group keeps running its old release with
  deploys silently skipping it (spark ran r82 through r86). Deploy-group
  membership lives only in the gitignored overlay, which a fleet host
  cannot read, so ``scripts/deploy`` publishes it into
  ``app_settings.deploy.fleet`` on every successful deploy; this probe
  diffs that ledger against ``host_heartbeat`` — uncovered live hosts and
  hosts whose ``meta.code_sha`` is not the deployed sha are findings.

Heal arm: ``pass-dead-on-host`` / ``pass-wedged`` findings on the
``precis-worker`` process request **restart-once** — ssh + a sudoers
grant scoped to exactly one command per platform (``launchctl
kickstart -k system/com.precis.worker`` / ``systemctl restart
precis-worker``; provisioned by ``deploy/redeploy-precis.yml``),
``cap=1`` then gripe. Dark until ``PRECIS_RESTART_ONCE_ENABLED=1``:
the ssh mesh (deploy→deploy) must be verified per host before arming.
The healing host may be the sick host (a wedged *pass* leaves the
rotation live enough to run this) — that's fine: the attempt is burned
before the action, so a self-kill can't loop.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial

from precis.store import Store
from precis.workers.bounded_heal import HealSpec, run_bounded_heal
from precis.workers.health_digest import CheckResult
from precis.workers.runner import RESCUE_SERVICES

log = logging.getLogger(__name__)

_GROUP = "condition"

#: Fresh-heartbeat gate shared by the host-scoped probes: a probe about a
#: host only fires while that host is provably alive (a dead host is the
#: nursery host-dark detector's business, not a per-pass condition).
_HOST_FRESH_MIN = 10


def _env_f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


# ── finding / row shapes ─────────────────────────────────────────────────


@dataclass(frozen=True)
class HealRequest:
    """A whitelisted heal a finding asks for. ``action`` names an entry in
    :data:`_HEAL_ACTIONS` — never a raw command."""

    action: str
    host: str
    process: str


@dataclass(frozen=True)
class ConditionFinding:
    """One fired instance of a condition. ``key`` is the per-instance
    fingerprint suffix (drives per-instance alert auto-open/auto-close)."""

    key: str
    detail: str
    heal: HealRequest | None = None


@dataclass(frozen=True)
class Condition:
    """One registry row. ``probe`` returns fired instances (empty = green)
    and must only raise for a genuinely broken probe — that surfaces as one
    ``unknown`` check (never a false alarm)."""

    name: str
    severity: str  # "info" | "warn" — CheckResult's vocabulary
    probe: Callable[[Store], list[ConditionFinding]]
    detail_green: str = ""
    # kept on the row for the doc/report; the fast cadence wires up when the
    # first fast row lands (all current rows are hourly).
    cadence: str = field(default="hourly")


# ── probes ───────────────────────────────────────────────────────────────

#: Slow-lane pass-dead (gr500868). A rotation pass on a busy host logs a
#: claim line every cycle, but on an agent-profile host (melchior's
#: fetch_oa / bib_parse: ~15 lines/day) it logs rarely enough to sit under
#: the 150-rows/7d floor, and an outage longer than the 7d window empties
#: its history entirely -- it went 8 days unalerted. So a second tier looks
#: back ``_PASS_DEAD_SLOW_LOOKBACK_DAYS`` and accepts a lower row floor, with
#: a looser silence budget (``_PASS_DEAD_SLOW_BUDGET_S``, > 7x the observed
#: ~1.6h inter-line gap, so ordinary quiet stretches don't fire). The tier
#: stops firing once a pass has been silent ``_PASS_DEAD_SLOW_MAX_SILENT_DAYS``
#: so a handler removed or renamed in code doesn't alert for the whole
#: lookback; two weeks of alerting is ample to have been seen.
_PASS_DEAD_SLOW_LOOKBACK_DAYS = 30
_PASS_DEAD_SLOW_BUDGET_S = 12 * 3600.0
_PASS_DEAD_SLOW_MIN_ROWS = 40
_PASS_DEAD_SLOW_MAX_SILENT_DAYS = 14

_PASS_DEAD_SQL = """
WITH live AS (
    SELECT host FROM host_heartbeat
     WHERE ts > now() - make_interval(mins => %(fresh_min)s)
), hist AS (
    SELECT host, process, payload->>'handler' AS handler,
           max(ts) AS last_seen,
           count(*) FILTER (WHERE ts > now() - interval '7 days') AS n7d,
           count(*) AS nlong
      FROM worker_logs
     WHERE ts > now() - make_interval(days => %(lookback_d)s)
       AND payload ? 'handler'
     GROUP BY 1, 2, 3
)
SELECT h.host, h.process, h.handler,
       EXTRACT(EPOCH FROM (now() - h.last_seen)) / 3600.0 AS silent_h
  FROM hist h JOIN live l ON l.host = h.host
 WHERE (h.last_seen < now() - make_interval(secs => %(budget_s)s)
        AND h.n7d >= %(min_rows)s)
    OR (h.last_seen < now() - make_interval(secs => %(slow_budget_s)s)
        AND h.last_seen > now() - make_interval(days => %(slow_max_silent_d)s)
        AND h.nlong >= %(slow_min_rows)s)
 ORDER BY 1, 2, 3
"""


def _disabled_services(store: Store) -> set[tuple[str, str]]:
    """``(host, service)`` pairs switched off live (prio 0). ``'*'`` host
    rows apply to every host — returned with the literal ``'*'`` and
    matched by the caller."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT host, service FROM service_config WHERE prio = 0"
        ).fetchall()
    return {(str(r[0]), str(r[1])) for r in rows}


def _handler_service(handler: str) -> str:
    """Map a ``worker_logs`` payload handler back to its service name
    (``ServiceSpec.log_handler`` is ``log_name or name``)."""
    from precis.workers.registry import SERVICES

    for spec in SERVICES:
        if spec.log_handler == handler:
            return spec.name
    return handler


def _liveness_handler_names(store: Store) -> set[str]:
    """The ``worker_logs`` ``payload.handler`` values a live pass can still
    emit: every :class:`ServiceSpec` ``name`` and ``log_handler``, plus the
    dynamic services registered in ``service_config`` (``axis:*`` /
    ``topic:*`` — wired per-boot, never in the static registry).

    Silence on a name outside this set is leftover history from a renamed
    or removed pass — not a dead handler. gr501991: ``_orcid_enrich_pass``
    was minted only by the stuck-pass warning's ``__name__`` keying while
    the pass logged ``orcid_enrich``; when the pass stopped hanging the
    ghost name went silent and pass-dead fired on a name nothing writes.

    Membership here is the coarse "still registers" gate; a name in this
    set can still be unemittable — :func:`_shadowed_service_names` filters
    those at the probe."""
    from precis.workers.registry import SERVICES

    names = {s.name for s in SERVICES} | {s.log_handler for s in SERVICES}
    with store.pool.connection() as conn:
        rows = conn.execute("SELECT DISTINCT service FROM service_config").fetchall()
    names.update(str(r[0]) for r in rows)
    return names


def _shadowed_service_names() -> set[str]:
    """Registry ``name`` tokens a divergent ``log_name`` shadows
    (``fetch`` → ``fetch_oa``, ``chase`` → ``finding_chase``, …).

    The runner mints a pass's ``payload.handler`` rows under its *log*
    identity on every path — success (``BatchResult.handler``), the
    deadline-abandon and crash rows, and ``activity``'s stuck warning —
    so the bare service token can never be emitted; its silence is the
    ``log_name`` row's business. Before that fix the error paths minted
    the token: ``fetch`` ended up a name only failure rows ever wrote,
    and when the deadline crashes stopped the token went silent and
    pass-dead fired on a live pass (gr506756, the gr501991 ghost shape on
    the opposite axis).
    """
    from precis.workers.registry import SERVICES

    return {s.name for s in SERVICES if s.log_name}


def _parked_workers(store: Store) -> set[tuple[str, str]]:
    """Fresh systemd observations of deliberate parking, not crashes.

    A sibling process can keep the host heartbeat fresh, so the unit
    snapshot needs its own timestamp. Unknown state never suppresses an
    expectation learned from cycle logs. Disabled alone is insufficient:
    a manually started disabled unit may still be serving work.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT host, meta->'worker_units' FROM host_heartbeat "
            "WHERE ts > now() - make_interval(mins => %s) "
            "AND meta ? 'worker_units'",
            (_HOST_FRESH_MIN,),
        ).fetchall()
    now = datetime.now(UTC)
    parked: set[tuple[str, str]] = set()
    for host, snapshot in rows:
        if not isinstance(snapshot, dict):
            continue
        try:
            observed = datetime.fromisoformat(snapshot["observed_at"])
            age_s = (now - observed).total_seconds()
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= age_s < _HOST_FRESH_MIN * 60:
            continue
        units = snapshot.get("units")
        if not isinstance(units, dict):
            continue
        for process, state in units.items():
            if (
                isinstance(state, dict)
                and state.get("active_state") == "inactive"
                and state.get("unit_file_state") == "disabled"
            ):
                parked.add((str(host), str(process)))
    return parked


def _fresh_unit_lists(store: Store) -> dict[str, dict[str, object]]:
    """Installed-unit maps from fresh, well-formed heartbeat snapshots.

    A host-lanes retirement removes the lane's unit file, so its process is
    absent here while the host's other workers keep the heartbeat fresh.
    Absence is evidence only from a fresh snapshot that lists at least one
    unit (the heartbeat worker's own); anything else is unknown and left out.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT host, meta->'worker_units' FROM host_heartbeat "
            "WHERE ts > now() - make_interval(mins => %s) "
            "AND meta ? 'worker_units'",
            (_HOST_FRESH_MIN,),
        ).fetchall()
    now = datetime.now(UTC)
    out: dict[str, dict[str, object]] = {}
    for host, snapshot in rows:
        if not isinstance(snapshot, dict):
            continue
        try:
            observed = datetime.fromisoformat(snapshot["observed_at"])
            age_s = (now - observed).total_seconds()
        except (KeyError, TypeError, ValueError):
            continue
        units = snapshot.get("units")
        if 0 <= age_s < _HOST_FRESH_MIN * 60 and (isinstance(units, dict) and units):
            out[str(host)] = units
    return out


def _probe_pass_dead(store: Store) -> list[ConditionFinding]:
    budget_s = _env_f("PRECIS_COND_PASS_DEAD_BUDGET_S", 4 * 3600.0)
    # ≥ ~1 row/hour over 7d — excludes one-off/manual handlers so a
    # deliberately-rare pass can't false-alarm.
    min_rows = int(_env_f("PRECIS_COND_PASS_DEAD_MIN_ROWS", 150))
    with store.pool.connection() as conn:
        rows = conn.execute(
            _PASS_DEAD_SQL,
            {
                "fresh_min": _HOST_FRESH_MIN,
                "budget_s": budget_s,
                "min_rows": min_rows,
                "lookback_d": _PASS_DEAD_SLOW_LOOKBACK_DAYS,
                "slow_budget_s": _env_f(
                    "PRECIS_COND_PASS_DEAD_SLOW_BUDGET_S", _PASS_DEAD_SLOW_BUDGET_S
                ),
                "slow_min_rows": int(
                    _env_f(
                        "PRECIS_COND_PASS_DEAD_SLOW_MIN_ROWS", _PASS_DEAD_SLOW_MIN_ROWS
                    )
                ),
                "slow_max_silent_d": _PASS_DEAD_SLOW_MAX_SILENT_DAYS,
            },
        ).fetchall()
    disabled = _disabled_services(store)
    parked = _parked_workers(store)
    installed = _fresh_unit_lists(store)
    live_names = _liveness_handler_names(store)
    shadowed = _shadowed_service_names()
    out: list[ConditionFinding] = []
    for host, process, handler, silent_h in rows:
        if str(handler) not in live_names:
            continue  # renamed/removed pass — stale history, not a death
        if str(handler) in shadowed:
            # A service token shadowed by its own ``log_name`` is never a
            # ``payload.handler`` emitter — the log name's row carries
            # this pass's liveness (gr506756).
            continue
        if (str(host), str(process)) in parked:
            continue
        service = _handler_service(str(handler))
        if (str(host), service) in disabled or ("*", service) in disabled:
            continue  # switched off on purpose — not a death
        if (
            str(process).startswith("precis-worker")
            and str(host) in installed
            and str(process) not in installed[str(host)]
        ):
            # The lane's unit is gone from a fresh unit listing: retired on
            # purpose (host-lanes). Report that; a restart has no unit to
            # restart (gr495419).
            out.append(
                ConditionFinding(
                    key=f"pass-dead:{host}/{process}/{handler}",
                    detail=(
                        f"lane retired: {process} is not installed on {host} "
                        f"(fresh unit listing), so handler '{handler}' has been "
                        f"silent {float(silent_h):.1f}h by design, not a dead "
                        "handler. Clears when its log history ages out "
                        f"(7d, or {_PASS_DEAD_SLOW_MAX_SILENT_DAYS}d of silence)"
                    ),
                )
            )
            continue
        out.append(
            ConditionFinding(
                key=f"pass-dead:{host}/{process}/{handler}",
                detail=(
                    f"handler '{handler}' on {host}/{process} silent "
                    f"{float(silent_h):.1f}h (logged steadily before; "
                    "host still heartbeats) — dead handler inside a live "
                    "worker (the gr204385 class)"
                ),
                heal=HealRequest("restart-worker", str(host), str(process)),
            )
        )
    return out


#: Rescue-critical handlers: the passes other recovery depends on. A
#: fleet-wide gap here means the rescuers themselves are starved — much
#: tighter budget than the per-host pass-dead row. The same set
#: ``run_loop`` ages to the front of a saturated rotation.
_RESCUE_HANDLERS = RESCUE_SERVICES

_LATEST_QUEST_LOOP_CTE = """
WITH active_quests AS (
    SELECT r.ref_id AS quest_id, rt.created_at AS active_since
      FROM refs r
      JOIN ref_tags rt ON rt.ref_id = r.ref_id
      JOIN tags t ON t.tag_id = rt.tag_id
     WHERE r.kind = 'quest' AND r.retired_at IS NULL
       AND t.namespace = 'STATUS' AND t.value = 'active'
), latest_loop AS (
    SELECT DISTINCT ON (aq.quest_id)
           aq.quest_id, aq.active_since,
           j.ref_id AS job_id, j.meta->>'rest_reason' AS rest_reason,
           jt.value AS job_status, jrt.created_at AS status_since,
           j.meta->'wake_when'->>'kind' AS wake_kind,
           j.meta->'wake_when'->'payload'->>'ts' AS wake_ts
      FROM active_quests aq
      LEFT JOIN refs j
        ON j.kind = 'job' AND j.retired_at IS NULL
       AND j.meta->>'idem_key' = 'quest_tick:' || aq.quest_id
       AND j.meta->>'executor' = 'coordinator'
      LEFT JOIN ref_tags jrt ON jrt.ref_id = j.ref_id
      LEFT JOIN tags jt ON jt.tag_id = jrt.tag_id AND jt.namespace = 'STATUS'
     -- A job's non-STATUS tags (e.g. reaped:reboot-orphan) also match jrt,
     -- landing extra rows with jt NULL; the NULLS LAST tie-break makes
     -- DISTINCT ON deterministically keep the row carrying the STATUS value.
     ORDER BY aq.quest_id, j.ref_id DESC, jt.value NULLS LAST
)
"""

_QUEST_RECONCILE_DEMAND_SQL = (
    _LATEST_QUEST_LOOP_CTE
    + """SELECT 1
  FROM latest_loop
 WHERE (
         job_id IS NULL
         AND active_since < now() - make_interval(secs => %(budget_s)s)
       )
    OR (
         job_status IN ('succeeded', 'cancelled')
         AND NOT (job_status = 'succeeded' AND rest_reason = 'dry')
         AND status_since < now() - make_interval(secs => %(budget_s)s)
       )
 LIMIT 1
"""
)


def _quest_reconcile_demand(store: Store, budget_s: float) -> bool:
    """True iff some active quest has gone ``budget_s`` without a live
    ``quest_tick`` coordinator loop — an existence probe, not a finding
    list (the cadence probe only needs a yes/no per fired handler).

    A quest counts as "waiting" when its most-recent coordinator loop is
    terminal-and-remintable (``succeeded``/``cancelled``, excluding a dry
    rest) or missing entirely, and that state has held for the full
    budget. Deliberately excluded: a live loop (non-terminal STATUS), a
    ``failed`` most-recent loop (the escalating backoff in
    ``precis.quest.loop._failed_rest_cooldown_active`` is on purpose), and
    a dry rest (``succeeded`` + ``meta.rest_reason == 'dry'`` —
    ``_dry_rest_cooldown_active``'s cooldown, also on purpose). Alerting
    on either recreates the gr204708 false positive: the reconciler is
    deliberately resting, not starved.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            _QUEST_RECONCILE_DEMAND_SQL, {"budget_s": budget_s}
        ).fetchone()
    return row is not None


#: gr204708 — a gap alone false-positives on ``quest_loop_reconcile`` (its
#: agent-profile rotation legitimately stretches across hour-long
#: ``claude_inproc`` runs). A per-handler DEMAND probe answers "did work
#: actually wait the whole budget window", not just "was the pass quiet";
#: handlers absent from this table (sweeper, nursery — system-profile, fast
#: rotation, where a gap IS starvation) stay pure-cadence.
_RESCUE_DEMAND: dict[str, Callable[[Store, float], bool]] = {
    "quest_loop_reconcile": _quest_reconcile_demand,
}

_RESCUE_GAP_SQL = """
SELECT payload->>'handler' AS handler,
       EXTRACT(EPOCH FROM (now() - max(ts))) / 60.0 AS gap_min
  FROM worker_logs
 WHERE ts > now() - interval '7 days'
   AND payload->>'handler' = ANY(%(handlers)s)
 GROUP BY 1
HAVING max(ts) < now() - make_interval(secs => %(budget_s)s)
"""


def _probe_rescue_cadence(store: Store) -> list[ConditionFinding]:
    budget_s = _env_f("PRECIS_COND_RESCUE_GAP_BUDGET_S", 45 * 60.0)
    with store.pool.connection() as conn:
        alive = conn.execute(
            "SELECT 1 FROM host_heartbeat "
            " WHERE ts > now() - make_interval(mins => %s) LIMIT 1",
            (_HOST_FRESH_MIN,),
        ).fetchone()
        if alive is None:
            return []  # whole fleet dark — the dead-man's-switch class, not ours
        rows = conn.execute(
            _RESCUE_GAP_SQL,
            {"handlers": list(_RESCUE_HANDLERS), "budget_s": budget_s},
        ).fetchall()
    out: list[ConditionFinding] = []
    for r in rows:
        handler = str(r[0])
        gap_min = float(r[1])
        demand = _RESCUE_DEMAND.get(handler)
        if demand is not None and not demand(store, budget_s):
            continue  # idle gap, not starvation — nothing was waiting
        detail = (
            f"rescue-critical pass '{handler}' has not completed anywhere in "
            f"{gap_min:.0f} min (fleet-wide; hosts are alive) — "
            "pass-loop starvation (the 2026-08-12 bib_parse class)"
        )
        if demand is not None:
            detail += (
                " and work is waiting (an active quest has sat without a "
                "live loop past budget)"
            )
        out.append(ConditionFinding(key=f"rescue-gap:{handler}", detail=detail))
    return out


_PASS_WEDGED_SQL = """
SELECT hb.host, act.key AS process,
       act.value->>'pass' AS pass,
       EXTRACT(EPOCH FROM (now() - (act.value->>'since')::timestamptz)) / 3600.0
  FROM host_heartbeat hb, jsonb_each(hb.meta->'activity') AS act
 WHERE hb.ts > now() - make_interval(mins => %(fresh_min)s)
   AND NOT (act.value ? 'idle')
   AND (act.value->>'since') IS NOT NULL
   AND (act.value->>'since')::timestamptz
       < now() - make_interval(secs => %(budget_s)s)
"""


_QUEST_TICK_STALLED_SQL = (
    _LATEST_QUEST_LOOP_CTE
    + """SELECT quest_id, job_id, job_status,
       EXTRACT(EPOCH FROM (now() - CASE
           WHEN job_status = 'queued' THEN status_since
           ELSE to_timestamp(wake_ts::bigint)
       END)) / 60.0 AS stalled_min
  FROM latest_loop
 WHERE (
         job_status = 'queued'
         AND status_since < now() - make_interval(secs => %(budget_s)s)
       )
    OR (
         job_status = 'waiting_time' AND wake_kind = 'at_time'
         AND wake_ts ~ '^[0-9]+$'
         AND to_timestamp(wake_ts::bigint)
             < now() - make_interval(secs => %(budget_s)s)
       )
 ORDER BY quest_id
"""
)


def _probe_quest_tick_stalled(store: Store) -> list[ConditionFinding]:
    """gr465967 — a quest's live ``quest_tick`` loop is due but nothing
    picked it up. Two shapes, each held past budget (default 3h): the loop
    sits ``STATUS:queued`` (the coordinator never claimed it), or sits
    ``waiting_time`` with its ``at_time`` wake long past (``wake_runner``
    never re-queued it). Neither is a deliberate rest — a dry rest is
    ``succeeded`` and a quota/breaker pause parks the wake in the FUTURE —
    so this never fires on a quest that is merely resting. The 2026-10-04
    fleet-wide 6.5 h tick silence left no per-slice trail to root-cause;
    this names the quest and loop job while it is happening."""
    budget_s = _env_f("PRECIS_COND_QUEST_TICK_STALL_S", 3 * 3600.0)
    with store.pool.connection() as conn:
        alive = conn.execute(
            "SELECT 1 FROM host_heartbeat "
            " WHERE ts > now() - make_interval(mins => %s) LIMIT 1",
            (_HOST_FRESH_MIN,),
        ).fetchone()
        if alive is None:
            return []  # fleet dark — the dead-man's-switch class, not ours
        rows = conn.execute(_QUEST_TICK_STALLED_SQL, {"budget_s": budget_s}).fetchall()
    return [
        ConditionFinding(
            key=f"quest-tick-stalled:{qid}",
            detail=(
                f"quest qu{qid}: loop job {jid} is {status} and due "
                f"{float(stalled_min):.0f} min ago with no slice run "
                "(coordinator not claiming / wake_runner not waking it) — "
                "ticks silent for this quest"
            ),
        )
        for qid, jid, status, stalled_min in rows
    ]


def _probe_pass_wedged(store: Store) -> list[ConditionFinding]:
    budget_s = _env_f("PRECIS_COND_PASS_WEDGED_BUDGET_S", 2 * 3600.0)
    with store.pool.connection() as conn:
        rows = conn.execute(
            _PASS_WEDGED_SQL, {"fresh_min": _HOST_FRESH_MIN, "budget_s": budget_s}
        ).fetchall()
    parked = _parked_workers(store)
    return [
        ConditionFinding(
            key=f"pass-wedged:{r[0]}/{r[1]}",
            detail=(
                f"{r[0]}/{r[1]} has been inside pass '{r[2]}' for "
                f"{float(r[3]):.1f}h (heartbeat fresh — the dedicated thread "
                "beats through a wedge; the rotation is stuck)"
            ),
            heal=HealRequest("restart-worker", str(r[0]), str(r[1])),
        )
        for r in rows
        if (str(r[0]), str(r[1])) not in parked
    ]


#: ``features->>'gated'`` excludes router gate-outs (a breaker trip / cloud
#: throttle / saturated slot that never ran, gr516941): they log
#: ``errored`` lite rows, but a pause is demand queued, not a degraded wire —
#: a multi-hour quota pause would otherwise fire this warn alert at 100%
#: ``err_rate`` on every poll.
_LLM_DEGRADED_SQL = """
SELECT model, transport, COALESCE(placement, 'cloud') AS placement,
       count(*) AS n, avg(errored::int) AS err_rate
  FROM llm_call_log
 WHERE ts > now() - interval '60 minutes'
   AND (features->>'gated') IS DISTINCT FROM 'true'
 GROUP BY 1, 2, 3
HAVING count(*) >= %(min_calls)s AND avg(errored::int) >= %(err_rate)s
"""


def _probe_llm_degraded(store: Store) -> list[ConditionFinding]:
    min_calls = int(_env_f("PRECIS_COND_LLM_MIN_CALLS", 20))
    err_rate = _env_f("PRECIS_COND_LLM_ERR_RATE", 0.5)
    with store.pool.connection() as conn:
        rows = conn.execute(
            _LLM_DEGRADED_SQL, {"min_calls": min_calls, "err_rate": err_rate}
        ).fetchall()
    return [
        ConditionFinding(
            key=f"llm-degraded:{r[0]}/{r[1]}/{r[2]}",
            detail=(
                f"model {r[0]} via {r[1]} ({r[2]}) erroring at "
                f"{float(r[4]) * 100:.0f}% over the last hour ({int(r[3])} calls)"
            ),
        )
        for r in rows
    ]


#: Agentic ticks that ran to completion without a single *successful* precis
#: tool call — the 2026-08-15 zombie-loop signature: claude-tier spend with
#: zero store writes (broken tick-env DB credential; the MCP registered but
#: every verb died ``fe_sendauth``). The runner marks such ticks with a
#: ``no-precis-tools`` job_event; a burst of them means the tick environment
#: is dead, not that the tasks are hard. Detected here so the outage costs
#: minutes, not a silent $13/day.
#:
#: The leading-wildcard LIKE can't use an index — it row-filters the
#: ``chunk_kind = 'job_event'`` index subset, which the sweeper GCs by age,
#: so the scan stays bounded. If it ever surfaces in pg_stat, switch the
#: predicate to the existing ``tsv`` GIN index.
_TOOLLESS_TICKS_SQL = """
SELECT count(*) AS n, count(DISTINCT r.parent_id) AS parents
  FROM chunks c
  JOIN refs r ON r.ref_id = c.ref_id
 WHERE c.chunk_kind = 'job_event'
   AND c.created_at > now() - make_interval(secs => %(window_s)s)
   AND c.text LIKE '%%no-precis-tools%%'
"""


def _probe_toolless_agent_spend(store: Store) -> list[ConditionFinding]:
    window_s = _env_f("PRECIS_COND_TOOLLESS_WINDOW_S", 2 * 3600.0)
    min_ticks = int(_env_f("PRECIS_COND_TOOLLESS_MIN_TICKS", 3))
    with store.pool.connection() as conn:
        row = conn.execute(_TOOLLESS_TICKS_SQL, {"window_s": window_s}).fetchone()
    n = int(row[0] or 0) if row else 0
    if n < min_ticks:
        return []
    parents = int(row[1] or 0) if row else 0
    return [
        ConditionFinding(
            key="agent-ticks-toolless",
            detail=(
                f"{n} agentic tick(s) across {parents} parent todo(s) made "
                f"zero successful precis tool calls in the last "
                f"{window_s / 3600:.0f}h — claude-tier spend with no store "
                "writes. Check the tick env's DB credential / MCP "
                "registration (the 2026-08-15 fe_sendauth outage signature)."
            ),
        )
    ]


#: Claims (slot holds / zombie agentlogs) whose holder generation is
#: provably replaced but that are OLDER than the reaper's age floor plus a
#: full sweeper cycle — the epoch arm should have reclaimed them, so their
#: persistence means the reclaim lane itself is broken (sweeper dead, or
#: the reaper erroring every pass). The claims-vs-liveness panel, as a row.
_DEAD_GEN_HOLDS_SQL = """
SELECT h.holder_host, h.holder_process, count(*),
       EXTRACT(EPOCH FROM (now() - min(h.acquired_at))) / 60.0
  FROM resource_slot_holds h
 WHERE h.holder_boot_id IS NOT NULL
   AND h.acquired_at < now() - make_interval(secs => %(age_s)s)
   AND NOT EXISTS (
         SELECT 1 FROM host_heartbeat hb
          WHERE hb.host = h.holder_host
            AND hb.meta -> 'boot_ids' ->> h.holder_process = h.holder_boot_id
       )
 GROUP BY 1, 2
"""


def _probe_dead_generation_claims(store: Store) -> list[ConditionFinding]:
    age_s = _env_f("PRECIS_COND_DEAD_GEN_AGE_S", 30 * 60.0)
    with store.pool.connection() as conn:
        rows = conn.execute(_DEAD_GEN_HOLDS_SQL, {"age_s": age_s}).fetchall()
    return [
        ConditionFinding(
            key=f"dead-gen-claims:{r[0]}/{r[1]}",
            detail=(
                f"{int(r[2])} slot hold(s) held by a replaced generation of "
                f"{r[0]}/{r[1]} for {float(r[3]):.0f} min — the claim reaper "
                "should have reclaimed these within one sweeper pass; the "
                "reclaim lane looks broken"
            ),
        )
        for r in rows
    ]


_SETTINGS_ENV_SHADOWED_SQL = """
SELECT host, meta->'settings_env_present' AS keys
  FROM host_heartbeat
 WHERE ts > now() - make_interval(mins => %(fresh_min)s)
   AND meta ? 'settings_env_present'
"""


def _probe_settings_env_shadowed(store: Store) -> list[ConditionFinding]:
    """Visibility row, not a fault (db-resident-settings.md slice 4): a host
    still advertises (via ``host_heartbeat.meta.settings_env_present``,
    :func:`precis.settings.advertised_env_presence`) that a registered
    setting's env var is set locally, while the key now resolves from a DB
    row on this same host — the ansible template still sets the var, but it
    no longer does anything, and is safe to drop. Deliberately excludes
    unregistered/never-DB keys (``resolve`` only reports ``"db"`` for a
    registry hit with a live row)."""
    from precis.settings import REGISTRY, resolve

    with store.pool.connection() as conn:
        rows = conn.execute(
            _SETTINGS_ENV_SHADOWED_SQL, {"fresh_min": _HOST_FRESH_MIN}
        ).fetchall()
    out: list[ConditionFinding] = []
    for host, keys in rows:
        for key in keys or []:
            if key not in REGISTRY:
                continue
            _value, layer = resolve(key, store=store)
            if layer != "db":
                continue
            out.append(
                ConditionFinding(
                    key=f"settings-env-shadowed:{host}/{key}",
                    detail=(
                        f"{host} still has the env var for setting {key!r} "
                        "set locally, but a DB row now wins fleet-wide — "
                        "safe to drop from the ansible template"
                    ),
                )
            )
    return out


_ANALYSIS_STALE_SQL = """
SELECT COALESCE((SELECT id_value FROM ref_identifiers
                  WHERE ref_id = src.ref_id AND id_kind = 'cite_key'
                  ORDER BY created_at DESC LIMIT 1),
                src.ref_id::text) AS slug,
       src.title, l.dst_ref_id,
       l.meta->>'sha' AS pinned, l.meta->>'at' AS pinned_at, e.sha AS current
  FROM links l
  JOIN refs src ON src.ref_id = l.src_ref_id AND src.retired_at IS NULL
  JOIN refs dst ON dst.ref_id = l.dst_ref_id AND dst.retired_at IS NULL
  JOIN LATERAL (
      SELECT payload->>'sha' AS sha
        FROM ref_events
       WHERE ref_id = l.src_ref_id AND source = 'cad' AND event = 'saved'
       ORDER BY event_id DESC
       LIMIT 1
  ) e ON TRUE
 WHERE l.relation = 'analyzed-by'
   AND COALESCE(l.meta->>'sha', '') <> ''
   AND e.sha IS DISTINCT FROM l.meta->>'sha'
 ORDER BY slug, l.dst_ref_id
"""


def _probe_analysis_stale(store: Store) -> list[ConditionFinding]:
    """Attached-models staleness (the closed attached-models-layer spec): an
    ``analyzed-by`` link pins the content sha of the design version the
    analysis ran against; ``cad_save`` records the current sha in a
    ``ref_events`` row. A mismatch means the design changed under the
    analysis — the number is stale and must be flagged loudly (a
    declared-but-drifted result is worse than none). Auto-closes when the
    analysis is re-attached (fresh pin) or either side is retired."""
    with store.pool.connection() as conn:
        rows = conn.execute(_ANALYSIS_STALE_SQL).fetchall()
    return [
        ConditionFinding(
            key=f"analysis-stale:{slug}/fi{dst_id}",
            detail=(
                f"analysis fi{dst_id} of cad {slug!r} ({title}) predates the "
                f"current design (pinned {pinned} at {pinned_at}, design now "
                f"{current}) — re-run the analysis and re-attach, or detach it"
            ),
        )
        for slug, title, dst_id, pinned, pinned_at, current in rows
    ]


#: ``external_rate_limits`` provider -> the ``ref_events.source`` of the
#: fetch_oa leg that must take a token from it before every call.
_LIMITER_FETCHERS: dict[str, str] = {
    "unpaywall": "fetcher:unpaywall",
    "openalex": "fetcher:openalex",
    "arxiv": "fetcher:arxiv",
    "crossref": "fetcher:crossref",
    "s2": "fetcher:s2",
    "core": "fetcher:core",
    "europepmc": "fetcher:europepmc",
    "openalex-content": "fetcher:openalex_content",
}

#: Events that prove the leg actually called out (``no_oa_version`` /
#: ``circuit_open`` / ``provider_wait`` can happen without a request).
_LIMITER_TRAFFIC_EVENTS = ("fetch_ok", "fetch_failed", "api_error")

_LIMITER_BYPASS_SQL = """
SELECT l.provider, l.last_refill, t.n
  FROM external_rate_limits l
  JOIN (SELECT source, count(*) AS n
          FROM ref_events
         WHERE ts > now() - interval '24 hours'
           AND event = ANY(%(events)s)
         GROUP BY source) t
    ON t.source = (%(fetchers)s::jsonb ->> l.provider)
 WHERE l.last_refill < now() - interval '24 hours'
"""

#: S1 variant: "last consumed" = the later of the token-lane's
#: ``last_refill`` and the rate-less ``last_used_at`` touch.
_LIMITER_BYPASS_SQL_S1 = _LIMITER_BYPASS_SQL.replace(
    "l.last_refill < now() - interval '24 hours'",
    "GREATEST(l.last_refill, COALESCE(l.last_used_at, l.last_refill)) "
    "< now() - interval '24 hours'",
)


def _probe_limiter_bypassed(store: Store) -> list[ConditionFinding]:
    """A fetcher is calling its provider but the provider's shared limiter
    row has not been consumed in 24h: the limiter is failing open silently
    (gr503435 — a scrubbed DSN made ``rate_limit`` see no database_url), so
    politeness and the daily quota are not in force."""
    import json

    from precis.utils import rate_limit

    with store.pool.connection() as conn:
        sql = (
            _LIMITER_BYPASS_SQL_S1
            if rate_limit.s1_columns_present(conn)
            else _LIMITER_BYPASS_SQL
        )
        rows = conn.execute(
            sql,
            {
                "events": list(_LIMITER_TRAFFIC_EVENTS),
                "fetchers": json.dumps(_LIMITER_FETCHERS),
            },
        ).fetchall()
    return [
        ConditionFinding(
            key=f"limiter-bypassed:{provider}",
            detail=(
                f"{n} {_LIMITER_FETCHERS[str(provider)]} call events in 24h but "
                f"external_rate_limits[{provider}] last consumed {last_refill:%Y-%m-%d %H:%MZ} "
                "- the limiter is failing open (check the worker's database_url "
                "/ PRECIS_RATE_LIMIT and its 'runs unpaced' WARNING)"
            ),
        )
        for provider, last_refill, n in rows
    ]


def _probe_limiter_host_cap(store: Store) -> list[ConditionFinding]:
    """The ``host:<netloc>`` limiter rows are at their seeding cap and none
    is idle enough to evict (gr507345): every host first seen from now on
    gets no row, so ``rate_limit.acquire`` fails open and the host runs
    unpaced — no politeness, no Retry-After sharing — with only a once-an-
    hour worker WARNING to show for it. Green while there is room, or
    while the eviction can make room."""
    from precis.utils import rate_limit

    with store.pool.connection() as conn:
        occupancy_sql = (
            rate_limit.HOST_ROW_OCCUPANCY_SQL_S1
            if rate_limit.s1_columns_present(conn)
            else rate_limit.HOST_ROW_OCCUPANCY_SQL
        )
        row = conn.execute(
            occupancy_sql,
            {"idle_s": float(rate_limit.HOST_ROW_IDLE_EVICT_S)},
        ).fetchone()
    if row is None:
        return []
    n, idle = int(row[0]), int(row[1])
    cap = rate_limit.MAX_SEEDED_HOST_ROWS
    if n < cap or idle > 0:
        return []
    return [
        ConditionFinding(
            key="limiter-cap-full",
            detail=(
                f"external_rate_limits holds {n}/{cap} host: rows and none is "
                f"idle over {rate_limit.HOST_ROW_IDLE_EVICT_S // 3600}h — a host "
                "first seen now gets no limiter row and runs unpaced (raise "
                "rate_limit.MAX_SEEDED_HOST_ROWS or shorten HOST_ROW_IDLE_EVICT_S)"
            ),
        )
    ]


_LIMITER_REFUSING_SQL = """
SELECT provider, refused_today, last_refused_at
  FROM external_rate_limits
 WHERE refused_today >= %(min_refusals)s
   AND last_refused_at > now() - make_interval(secs => %(recent_s)s)
 ORDER BY refused_today DESC, provider
"""


def _probe_limiter_refusing(store: Store) -> list[ConditionFinding]:
    """A provider row refusing past the daily floor is
    demand the shared limiter could not admit — a starved token bucket, a
    spent daily cap, a full ``llm-api`` slot hold, or a live Retry-After
    park (every counted refusal writes ``refused_today``/
    ``last_refused_at``). The recency arm keeps a morning burst from
    holding the check red all day; ``refused_today``'s own day rollover
    bounds staleness regardless. Green on a pre-S1 schema (the columns
    don't exist yet)."""
    from precis.utils import rate_limit

    min_refusals = int(_env_f("PRECIS_COND_LIMITER_REFUSALS_MIN", 25))
    recent_s = _env_f("PRECIS_COND_LIMITER_REFUSAL_RECENT_S", 2 * 3600.0)
    with store.pool.connection() as conn:
        if not rate_limit.s1_columns_present(conn):
            return []
        rows = conn.execute(
            _LIMITER_REFUSING_SQL,
            {"min_refusals": min_refusals, "recent_s": recent_s},
        ).fetchall()
    return [
        ConditionFinding(
            key=f"limiter-refusing:{provider}",
            detail=(
                f"external_rate_limits[{provider}] refused {int(n)} call(s) "
                f"today (last {last:%Y-%m-%d %H:%MZ}) — the shared limiter "
                "could not admit the demand: the bucket, daily cap, or "
                "llm-api hold is undersized, or a caller is looping (the "
                "worker's rate-limited WARNING names the reason)"
            ),
        )
        for provider, n, last in rows
    ]


#: ``app_settings`` key ``scripts/deploy`` refreshes after every green
#: deploy: JSON ``{"sha": <deployed sha>, "at": <isoZ>, "covered":
#: [<host>, …], "skipped": {<host>: <sha it still runs>}}``. Host names
#: only — never an address (the repo is public).
_DEPLOY_LEDGER_KEY = "deploy.fleet"

#: A host heartbeating within this window counts as live for the coverage
#: question — generous on purpose: an hourly eval that caught a host
#: mid-bounce would otherwise drop and re-fire the alert every deploy.
_DEPLOY_FLEET_FRESH_MIN = 60

_DEPLOY_FLEET_SQL = """
SELECT host, meta->>'code_sha' AS code_sha
  FROM host_heartbeat
 WHERE ts > now() - make_interval(mins => %(fresh_min)s)
   AND (meta->>'ephemeral') IS DISTINCT FROM 'true'
   AND host !~ '^[0-9a-f]{12}$'
 ORDER BY host
"""

#: ``meta.code_sha`` shapes worth comparing (a full sha or "unknown"/absent
#: — anything else is an unidentifiable build the lag arm can't judge).
_DEPLOY_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _probe_deploy_fleet(store: Store) -> list[ConditionFinding]:
    """gr507870 — diff the deploy-published fleet ledger against live
    heartbeats. Two finding shapes under one rule:

    * ``deploy-uncovered:<host>`` — a live host in no deploy group.
      Deploys never reach it; it keeps serving the last sha it got.
    * ``deploy-behind:<host>`` — a covered host whose ``meta.code_sha``
      (heartbeat.py's served-commit stamp) is not the deployed sha.

    Both need the ledger: written by ``scripts/deploy`` at each success
    marker because the overlay's group membership never leaves the deploy
    machine. ``deploy-ledger:unpublished`` fires while no deploy has
    written one yet (a missing row means this check is blind, which is
    itself the gr507870 shape). An unreadable payload is
    ``deploy-ledger:invalid``.
    """
    fresh_min = int(
        _env_f("PRECIS_COND_DEPLOY_FLEET_FRESH_MIN", _DEPLOY_FLEET_FRESH_MIN)
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = %s", (_DEPLOY_LEDGER_KEY,)
        ).fetchone()
        beats = conn.execute(_DEPLOY_FLEET_SQL, {"fresh_min": fresh_min}).fetchall()
    if row is None:
        return [
            ConditionFinding(
                key="deploy-ledger:unpublished",
                detail=(
                    "no deploy fleet ledger on record (app_settings."
                    f"{_DEPLOY_LEDGER_KEY} — a successful scripts/deploy "
                    "writes it); host↔deploy-group coverage is unjudgeable "
                    "until the next deploy publishes"
                ),
            )
        ]
    import json

    try:
        ledger = json.loads(str(row[0]))
        assert isinstance(ledger, dict)
        covered = ledger.get("covered")
        assert isinstance(covered, list) and all(isinstance(h, str) for h in covered)
    except (json.JSONDecodeError, AssertionError):
        return [
            ConditionFinding(
                key="deploy-ledger:invalid",
                detail=(
                    f"app_settings.{_DEPLOY_LEDGER_KEY} is not the expected "
                    "{'sha', 'covered': [host…]} payload — re-run a deploy "
                    "or inspect the row"
                ),
            )
        ]
    covered_set = {str(h) for h in covered}
    deployed_sha = ledger.get("sha")
    deployed_sha = (
        str(deployed_sha)
        if isinstance(deployed_sha, str) and _DEPLOY_SHA_RE.match(deployed_sha)
        else None
    )
    skipped = ledger.get("skipped")
    skipped_set = {str(h) for h in skipped} if isinstance(skipped, dict) else set()
    out: list[ConditionFinding] = []
    for host, code_sha in beats:
        host = str(host)
        if host not in covered_set:
            out.append(
                ConditionFinding(
                    key=f"deploy-uncovered:{host}",
                    detail=(
                        f"{host} heartbeats but the overlay puts it in no "
                        "deploy group — every deploy silently skips it and "
                        "it keeps serving the last sha it ever got "
                        "(gr507870: re-add it to a deploy group, or retire "
                        "the host's daemons)"
                    ),
                )
            )
            continue
        if (
            deployed_sha
            and isinstance(code_sha, str)
            and _DEPLOY_SHA_RE.match(code_sha)
            and code_sha != deployed_sha
        ):
            skipped_note = (
                " (the deploy recorded it skipped — it still runs the pre-deploy sha)"
                if host in skipped_set
                else ""
            )
            out.append(
                ConditionFinding(
                    key=f"deploy-behind:{host}",
                    detail=(
                        f"{host} serves {code_sha[:12]} but the deployed "
                        f"release is {deployed_sha[:12]}{skipped_note} — "
                        "running stale code behind a fleet it was left out of"
                    ),
                )
            )
    return out


#: A gap past ~2 beats (~2 min at the per-minute cadence) breaks a
#: "consecutive beats" run — otherwise a dark scrape glues two ordinary
#: bursts into one sustained one.
_MAX_BEAT_GAP_S = 150.0

_SERVING_PRESSURE_SQL = """
SELECT host, ts, serving_running, serving_queued, serving_kv_usage,
       serving_retractions_total, swap_in_pages_total
  FROM host_heartbeat_log
 WHERE ts > now() - make_interval(secs => %(lookback_s)s)
   AND (serving_running IS NOT NULL
        OR serving_queued IS NOT NULL
        OR serving_kv_usage IS NOT NULL
        OR serving_retractions_total IS NOT NULL
        OR swap_in_pages_total IS NOT NULL)
 ORDER BY host, ts
"""


def _probe_local_serving_pressure(store: Store) -> list[ConditionFinding]:
    """llm-lane-scheduler S0: sustained local-serving pressure on a host —
    sustained KV-cache use over threshold, retractions held > 0, or host
    swap-in rising for N beats (one beat ≈ one minute). Reads the
    ``serving_*``/``swap_in_pages_total`` columns the heartbeat scrape
    writes. Swap-in counts only on a host with a successful serving
    scrape in the window — any Linux worker can swap; only a serving
    box's swap is *serving* pressure. ``serving_retractions_total`` is a
    GAUGE despite the name (SGLang ``num_retracted_reqs``, requests
    currently retracted) — it fires on the level; swap-in is the one
    monotonic counter, deltas clamped at zero so a restart reads as
    "rebased", not negative. Runs break on a scrape gap past
    ``_MAX_BEAT_GAP_S``.

    Thresholds (env-tunable): KV >= ``PRECIS_COND_SERVING_KV_MAX`` (0.90)
    or retractions > 0, each for ``PRECIS_COND_SERVING_SUSTAIN_MIN`` (5)
    consecutive beats, or swap-in rising for that many beats — over
    ``PRECIS_COND_SERVING_LOOKBACK_MIN`` (60, matching the hourly lane).
    """
    lookback_min = _env_f("PRECIS_COND_SERVING_LOOKBACK_MIN", 60.0)
    kv_max = _env_f("PRECIS_COND_SERVING_KV_MAX", 0.90)
    sustain = int(_env_f("PRECIS_COND_SERVING_SUSTAIN_MIN", 5.0))
    with store.pool.connection() as conn:
        rows = conn.execute(
            _SERVING_PRESSURE_SQL, {"lookback_s": lookback_min * 60.0}
        ).fetchall()

    # (ts, served, kv, retractions, swap)
    by_host: dict[str, list[tuple]] = defaultdict(list)
    for host, ts, running, queued, kv, retractions, swap in rows:
        served = any(v is not None for v in (running, queued, kv, retractions))
        by_host[str(host)].append((ts, served, kv, retractions, swap))

    findings: list[ConditionFinding] = []
    for host, beats in sorted(by_host.items()):
        signals: list[str] = []
        served_in_window = any(served for _, served, *_ in beats)

        prev_ts: datetime | None = None
        run = longest = 0
        for ts, _, kv, _, _ in beats:
            if prev_ts is not None and (ts - prev_ts).total_seconds() > _MAX_BEAT_GAP_S:
                run = 0
            prev_ts = ts
            run = run + 1 if kv is not None and kv >= kv_max else 0
            longest = max(longest, run)
        if longest >= sustain:
            signals.append(f"KV usage >= {kv_max:.0%} for {longest} consecutive beats")

        prev_ts = None
        run = longest = 0
        for ts, _, _, r, _ in beats:
            if prev_ts is not None and (ts - prev_ts).total_seconds() > _MAX_BEAT_GAP_S:
                run = 0
            prev_ts = ts
            run = run + 1 if r is not None and r > 0 else 0
            longest = max(longest, run)
        if longest >= sustain:
            signals.append(f"retractions > 0 for {longest} consecutive beats")

        if served_in_window:
            prev_ts = None
            prev = None
            run = longest = 0
            for ts, _, _, _, s in beats:
                if s is None or (
                    prev_ts is not None
                    and (ts - prev_ts).total_seconds() > _MAX_BEAT_GAP_S
                ):
                    prev_ts = ts
                    prev = None
                    run = 0
                    continue
                prev_ts = ts
                run = run + 1 if prev is not None and float(s) > prev else 0
                longest = max(longest, run)
                prev = float(s)
            if longest >= sustain:
                signals.append(f"host swap-in rising for {longest} consecutive beats")

        if signals:
            last_ts = beats[-1][0]
            findings.append(
                ConditionFinding(
                    key=f"local-serving-pressure:{host}",
                    detail=(
                        f"{host}: {'; '.join(signals)} in the last "
                        f"{lookback_min:.0f} min (last beat "
                        f"{last_ts:%Y-%m-%d %H:%MZ}) — KV thrash / unified-"
                        "memory pressure on the serving box"
                    ),
                )
            )
    return findings


CONDITIONS: tuple[Condition, ...] = (
    Condition("limiter-bypassed", "warn", _probe_limiter_bypassed),
    Condition("limiter-cap-full", "warn", _probe_limiter_host_cap),
    Condition("limiter-refusing", "warn", _probe_limiter_refusing),
    Condition("local-serving-pressure", "warn", _probe_local_serving_pressure),
    Condition("analysis-stale", "warn", _probe_analysis_stale),
    Condition("pass-dead-on-host", "warn", _probe_pass_dead),
    Condition("rescue-pass-cadence", "warn", _probe_rescue_cadence),
    Condition("quest-tick-stalled", "warn", _probe_quest_tick_stalled),
    Condition("pass-wedged", "warn", _probe_pass_wedged),
    Condition("llm-degraded", "warn", _probe_llm_degraded),
    Condition("agent-ticks-toolless", "warn", _probe_toolless_agent_spend),
    Condition("dead-generation-claims", "warn", _probe_dead_generation_claims),
    Condition("settings-env-shadowed", "info", _probe_settings_env_shadowed),
    Condition("deploy-fleet-coverage", "warn", _probe_deploy_fleet),
)


# ── evaluation (hourly lane entrypoint) ──────────────────────────────────


def run_condition_checks(
    store: Store, *, conditions: tuple[Condition, ...] | None = None
) -> tuple[list[CheckResult], list[ConditionFinding]]:
    """Evaluate the registry → digest-shaped checks + the raw findings.

    Green condition → one ``ok`` row (digest greens table); each fired
    instance → one ``stale`` row named by its finding key (per-instance
    alert lifecycle); broken probe → one ``unknown`` row (never a false
    alarm — the digest discipline).
    """
    checks: list[CheckResult] = []
    findings: list[ConditionFinding] = []
    for cond in conditions if conditions is not None else CONDITIONS:
        try:
            fired = cond.probe(store)
        except Exception:
            log.warning("conditions: probe %s raised", cond.name, exc_info=True)
            checks.append(
                CheckResult(
                    group=_GROUP,
                    name=cond.name,
                    status="unknown",
                    detail="probe failed (see worker log)",
                    severity=cond.severity,
                )
            )
            continue
        if not fired:
            checks.append(
                CheckResult(
                    group=_GROUP,
                    name=cond.name,
                    status="ok",
                    detail=cond.detail_green or "clear",
                    severity=cond.severity,
                )
            )
            continue
        findings.extend(fired)
        checks.extend(
            CheckResult(
                group=_GROUP,
                name=f.key,
                status="stale",
                detail=f.detail,
                severity=cond.severity,
            )
            for f in fired
        )
    return checks, findings


# ── the heal arm ─────────────────────────────────────────────────────────

RESTART_ONCE_ENABLED_ENV = "PRECIS_RESTART_ONCE_ENABLED"

#: process name → the exact unit the sudoers grant covers. Only listed
#: processes are healable; anything else is report-only by construction.
_RESTARTABLE_UNITS = {"precis-worker": "com.precis.worker"}


def _host_platform(store: Store, host: str) -> str:
    """``Darwin`` / ``Linux`` from the host's own heartbeat (defaults to
    Darwin — the fleet majority — when unadvertised)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'platform' FROM host_heartbeat WHERE host = %s", (host,)
        ).fetchone()
    return str(row[0]) if row and row[0] else "Darwin"


def _restart_worker_cmd(host: str, platform: str) -> list[str]:
    """The ONE vetted remote restart — must stay byte-aligned with the
    sudoers grant redeploy-precis.yml provisions (scoped to exactly this
    command; anything else prompts for a password and fails BatchMode)."""
    ssh = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        f"deploy@{host}",
    ]
    if platform.lower().startswith("darwin"):
        return [
            *ssh,
            "sudo",
            "/bin/launchctl",
            "kickstart",
            "-k",
            "system/com.precis.worker",
        ]
    return [*ssh, "sudo", "/usr/bin/systemctl", "restart", "precis-worker"]


def _run_restart(cmd: list[str]) -> bool:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("conditions: restart-once ssh failed: %s", exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "conditions: restart-once rc=%d stderr=%s",
            proc.returncode,
            proc.stderr.decode("utf-8", "replace")[-500:],
        )
    return proc.returncode == 0


def run_condition_heals(
    store: Store,
    findings: list[ConditionFinding],
    *,
    runner: Callable[[list[str]], bool] = _run_restart,
) -> int:
    """Execute the whitelisted heal arm for fired findings (restart-once).

    Dark unless ``PRECIS_RESTART_ONCE_ENABLED=1``. One bounded_heal key
    per (host, process) — two findings against the same worker share one
    budget. ``cap=1``: one autonomous bounce per incident, then the gripe.
    Returns the number of successful heal actions. Never raises.
    """
    if os.environ.get(RESTART_ONCE_ENABLED_ENV, "") != "1":
        return 0
    healed = 0
    seen: set[str] = set()
    for f in findings:
        h = f.heal
        if h is None or h.action != "restart-worker":
            continue
        if h.process not in _RESTARTABLE_UNITS:
            continue
        key = f"restart-worker:{h.host}:{h.process}"
        if key in seen:
            continue
        seen.add(key)
        try:
            # Findings can predate a role change. Recheck before consuming
            # the heal budget, including findings from stale activity maps.
            if (h.host, h.process) in _parked_workers(store):
                continue
            platform = _host_platform(store, h.host)
            cmd = _restart_worker_cmd(h.host, platform)
            spec = HealSpec(
                key=key,
                cap=1,
                base_cooldown_s=3600.0,
                title=f"worker on {h.host} needed a restart and the one "
                "autonomous bounce is spent",
                detail=f.detail,
            )
            outcome = run_bounded_heal(store, spec, partial(runner, cmd))
            log.warning("conditions: heal %s -> %s", key, outcome)
            if outcome == "healed":
                healed += 1
        except Exception:
            log.warning("conditions: heal %s errored", key, exc_info=True)
    return healed


__all__ = [
    "CONDITIONS",
    "RESTART_ONCE_ENABLED_ENV",
    "Condition",
    "ConditionFinding",
    "HealRequest",
    "run_condition_checks",
    "run_condition_heals",
]
