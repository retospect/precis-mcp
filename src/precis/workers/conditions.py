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
  handler is dead/wedged/removed, not merely quiet. Handlers switched
  off live via ``service_config`` (prio 0) are excluded.
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
* ``settings-env-shadowed`` — info-only (db-resident-settings.md slice 4):
  a host still has a registered setting's env var set locally (self-
  reported via ``host_heartbeat.meta.settings_env_present``) after a DB
  row has taken over resolution — cleanup visibility for the ansible-diet
  pass, not a failure.

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
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

from precis.store import Store
from precis.workers.bounded_heal import HealSpec, run_bounded_heal
from precis.workers.health_digest import CheckResult

log = logging.getLogger(__name__)

_GROUP = "condition"

#: Fresh-heartbeat gate shared by the host-scoped probes: a probe about a
#: host only fires while that host is provably alive (a dead host is the
#: nursery host-dark detector's business, not a per-pass condition).
_HOST_FRESH_MIN = 10


def _env_f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw not in (None, "") else default
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
    # kept on the row for the doc/report; the fast lane wires up when the
    # first fast row lands (all current rows are hourly).
    lane: str = field(default="hourly")


# ── probes ───────────────────────────────────────────────────────────────

_PASS_DEAD_SQL = """
WITH live AS (
    SELECT host FROM host_heartbeat
     WHERE ts > now() - make_interval(mins => %(fresh_min)s)
), hist AS (
    SELECT host, process, payload->>'handler' AS handler,
           max(ts) AS last_seen,
           count(*) AS n7d
      FROM worker_logs
     WHERE ts > now() - interval '7 days'
       AND payload ? 'handler'
     GROUP BY 1, 2, 3
)
SELECT h.host, h.process, h.handler,
       EXTRACT(EPOCH FROM (now() - h.last_seen)) / 3600.0 AS silent_h
  FROM hist h JOIN live l ON l.host = h.host
 WHERE h.last_seen < now() - make_interval(secs => %(budget_s)s)
   AND h.n7d >= %(min_rows)s
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


def _probe_pass_dead(store: Store) -> list[ConditionFinding]:
    budget_s = _env_f("PRECIS_COND_PASS_DEAD_BUDGET_S", 4 * 3600.0)
    # ≥ ~1 row/hour over 7d — excludes one-off/manual handlers so a
    # deliberately-rare pass can't false-alarm.
    min_rows = int(_env_f("PRECIS_COND_PASS_DEAD_MIN_ROWS", 150))
    with store.pool.connection() as conn:
        rows = conn.execute(
            _PASS_DEAD_SQL,
            {"fresh_min": _HOST_FRESH_MIN, "budget_s": budget_s, "min_rows": min_rows},
        ).fetchall()
    disabled = _disabled_services(store)
    out: list[ConditionFinding] = []
    for host, process, handler, silent_h in rows:
        service = _handler_service(str(handler))
        if (str(host), service) in disabled or ("*", service) in disabled:
            continue  # switched off on purpose — not a death
        out.append(
            ConditionFinding(
                key=f"pass-dead:{host}/{process}/{handler}",
                detail=(
                    f"handler '{handler}' on {host}/{process} silent "
                    f"{float(silent_h):.1f}h (logged every cycle for 7d before; "
                    "host still heartbeats) — dead handler inside a live "
                    "worker (the gr204385 class)"
                ),
                heal=HealRequest("restart-worker", str(host), str(process)),
            )
        )
    return out


#: Rescue-critical handlers: the passes other recovery depends on. A
#: fleet-wide gap here means the rescuers themselves are starved — much
#: tighter budget than the per-host pass-dead row.
_RESCUE_HANDLERS = ("sweeper", "nursery", "quest_loop_reconcile")

_QUEST_RECONCILE_DEMAND_SQL = """
WITH active_quests AS (
    SELECT r.ref_id AS quest_id, rt.created_at AS active_since
      FROM refs r
      JOIN ref_tags rt ON rt.ref_id = r.ref_id
      JOIN tags t ON t.tag_id = rt.tag_id
     WHERE r.kind = 'quest' AND r.deleted_at IS NULL
       AND t.namespace = 'STATUS' AND t.value = 'active'
), latest_loop AS (
    SELECT DISTINCT ON (aq.quest_id)
           aq.quest_id, aq.active_since,
           j.ref_id AS job_id, j.meta->>'rest_reason' AS rest_reason,
           jt.value AS job_status, jrt.created_at AS status_since
      FROM active_quests aq
      LEFT JOIN refs j
        ON j.kind = 'job' AND j.deleted_at IS NULL
       AND j.meta->>'idem_key' = 'quest_tick:' || aq.quest_id
       AND j.meta->>'executor' = 'coordinator'
      LEFT JOIN ref_tags jrt ON jrt.ref_id = j.ref_id
      LEFT JOIN tags jt ON jt.tag_id = jrt.tag_id AND jt.namespace = 'STATUS'
     -- A job's non-STATUS tags (e.g. reaped:reboot-orphan) also match jrt,
     -- landing extra rows with jt NULL; the NULLS LAST tie-break makes
     -- DISTINCT ON deterministically keep the row carrying the STATUS value.
     ORDER BY aq.quest_id, j.ref_id DESC, jt.value NULLS LAST
)
SELECT 1
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


def _probe_pass_wedged(store: Store) -> list[ConditionFinding]:
    budget_s = _env_f("PRECIS_COND_PASS_WEDGED_BUDGET_S", 2 * 3600.0)
    with store.pool.connection() as conn:
        rows = conn.execute(
            _PASS_WEDGED_SQL, {"fresh_min": _HOST_FRESH_MIN, "budget_s": budget_s}
        ).fetchall()
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
    ]


_LLM_DEGRADED_SQL = """
SELECT model, transport, COALESCE(placement, 'cloud') AS placement,
       count(*) AS n, avg(errored::int) AS err_rate
  FROM llm_call_log
 WHERE ts > now() - interval '60 minutes'
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


CONDITIONS: tuple[Condition, ...] = (
    Condition("pass-dead-on-host", "warn", _probe_pass_dead),
    Condition("rescue-pass-cadence", "warn", _probe_rescue_cadence),
    Condition("pass-wedged", "warn", _probe_pass_wedged),
    Condition("llm-degraded", "warn", _probe_llm_degraded),
    Condition("agent-ticks-toolless", "warn", _probe_toolless_agent_spend),
    Condition("dead-generation-claims", "warn", _probe_dead_generation_claims),
    Condition("settings-env-shadowed", "info", _probe_settings_env_shadowed),
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
