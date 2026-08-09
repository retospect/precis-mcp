"""Factory console — the window onto what the factory runs.

The System-page merge folded the read/write
console formerly served at ``GET /factory`` into the "Services" sub-tab
of the merged System page (``/status?tab=services``); ``GET /factory``
now just redirects there. The SQL helpers below (host strip, category
tables, quests, ``service_config`` reads) still live here and are
imported by ``status.py`` to build that sub-tab's context — only the
*route* moved, not the logic. The ``POST /factory/{prio,model,clear}``
write endpoints stay mounted at their original paths (their redirect
target is now the Services sub-tab).

Host strip (load / worker-alive per machine) over one list per category
of services — every pass / job-type / compute / daemon / serving row from
the one `ServiceSpec` registry, joined to its live `service_config` prio
and its last-success / last-failure from `worker_logs`
(docs/backlog/factory-console-and-scheduling.md, slices 3–4).

* **Slice 3 (read):** the host strip + the total service list + last
  activity, all degrading to empty on a schema surprise (status-tab
  pattern).
* **Slice 4 (write):** a host selector scopes the page; each row's prio
  is editable (0 = off, 1..10 = claim weight) and model-using rows get a
  model_pref dropdown populated from the `llm` catalog. The writes go
  straight to `service_config`; the worker picks them up next cycle.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from precis import health_checks
from precis.budget import settings as budget_settings
from precis.utils.llm import live_config
from precis.utils.llm.router import Tier
from precis.workers.service_config import (
    RESERVE_SERVICE,
    clear_reserve,
    clear_service_config,
    set_reserve,
    set_service_model,
    set_service_prio,
)
from precis_web.deps import get_store
from precis_web.timefmt import abs_ts as _abs_ts
from precis_web.timefmt import age_seconds as _age_seconds
from precis_web.timefmt import ago as _ago

router = APIRouter(prefix="/factory", tags=["factory"])

log = logging.getLogger(__name__)

#: A host silent longer than this reads as "dead worker" in the strip.
_STALE_AFTER_S = 600

#: How far back the per-host "recent errors" chip looks.
_ERROR_WINDOW = "6 hours"

#: The soft verified-capability gauge (mirrors ``capability_probe``); rendered
#: as a green/red "agent" chip rather than the ``mem`` RAM-pressure copy.
_CONTAINER_AGENT_RESOURCE = "container_agent"

#: Human-readable mouseover copy per ``resource_slots.resource``. Keyed by
#: resource name; ``_slot_desc`` falls back to a generic line for unknowns so a
#: newly-probed resource still gets *a* tooltip rather than none.
_RESOURCE_DESC = {
    "podman": (
        "Container-runtime capacity — how many agent/sandbox containers this "
        "host can run in parallel (free/total). 'podman' is the generic slot "
        "name; on the Macs the runtime is colima/Docker. free drops as a "
        "container is reserved at claim, and returns when it exits."
    ),
    "gpu": (
        "GPU capacity — parallel GPU jobs this host offers (DFT / ML / "
        "embeddings), free/total. free drops while a GPU job holds a slot."
    ),
    "mem": (
        "Memory-pressure headroom — a live gauge, NOT a fixed slot count. "
        "Higher free = more RAM headroom; 0 = under pressure (jetsam risk on a "
        "Mac). Watch this on any host running a container runtime."
    ),
    "container_agent": (
        "Agent-container capability — a verified 0/1 gauge, shown only where "
        "the operator opted in (PRECIS_AGENT_CONTAINER). 1 = the runtime, image "
        "and auth token all check out; 0 = degraded (opted in but can't launch, "
        "so agentic passes fall back in-process rather than failing)."
    ),
}


def _slot_desc(resource: str, free: int, capacity: int, kind: str) -> str:
    """The mouseover text for one capability/pressure chip."""
    base = _RESOURCE_DESC.get(
        resource,
        f"'{resource}' capacity on this host ({'headroom gauge' if kind == 'soft' else 'parallel slots'}).",
    )
    return f"{resource}: {free}/{capacity} — {base}"


def _soft_gauge_render(
    resource: str, host: str, free: int, capacity: int, pressure: str
) -> tuple[str, str]:
    """Label + mouseover for a soft gauge chip (``mem`` / ``container_agent``).

    Each soft gauge renders as a coloured chip (crit=red … ok=green) but reads
    differently: ``mem`` is RAM-pressure headroom (0 = jetsam risk), while
    ``container_agent`` is a verified-capability flag (0 = degraded, opted in but
    can't launch). Both share the colour ramp; only the label + copy differ."""
    if resource == _CONTAINER_AGENT_RESOURCE:
        state = (
            "verified — can launch agent containers"
            if free > 0
            else "degraded — opted in (PRECIS_AGENT_CONTAINER) but can't launch; "
            "agentic passes fall back in-process"
        )
        return "agent", f"Agent-container capability on {host}: {state}."
    word = (
        "under pressure"
        if pressure == "crit"
        else ("low" if pressure == "warn" else "plenty")
    )
    return "RAM", (
        f"Memory-pressure headroom on {host}: {free}/{capacity} ({word}). "
        "0 = jetsam risk."
    )


#: All-hosts wildcard shown first in the host selector.
_ALL = "*"

#: Category display order — grinders/health first, heavy tail last.
_CATEGORY_ORDER = [
    "ingest",
    "discovery",
    "acquisition",
    "jobs",
    "health",
    "review",
    "audio",
    "compute",
    "serving",
    "daemon",
]


def _hosts(store: Any) -> list[dict[str, Any]]:
    """Per-host load + liveness from ``host_heartbeat`` (empty on error)."""
    try:
        with store.pool.connection() as conn:
            cur = conn.execute(
                "SELECT host, ts, temp_c, load1, load5, load15, meta, "
                "       EXTRACT(EPOCH FROM (now() - ts)) AS age_s "
                "FROM host_heartbeat ORDER BY host"
            )
            rows = cur.fetchall()
    except Exception:
        log.warning("factory: host_heartbeat read failed", exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for host, ts, temp_c, load1, load5, load15, meta, age_s in rows:
        age = float(age_s) if age_s is not None else None
        # top_cpu is a heartbeat nicety (top processes by CPU%); tolerate its
        # absence on old rows / hosts that couldn't probe.
        top_cpu = (meta or {}).get("top_cpu") or []
        out.append(
            {
                "host": host,
                "alive": age is not None and age <= _STALE_AFTER_S,
                "ago": _ago(ts),
                "temp_c": temp_c,
                "load1": load1,
                "load5": load5,
                "load15": load15,
                "top_cpu": top_cpu,
            }
        )
    return out


def _slots_by_host(store: Any) -> dict[str, list[dict[str, Any]]]:
    """host -> its advertised ``resource_slots`` rows (empty on error).

    The heartbeat self-probe (slice 6b) writes what each machine can do +
    how many parallel slots it offers; the strip renders it as capability
    chips. ``free``/``capacity`` differ only once slice 6c reserves at claim.
    """
    try:
        with store.pool.connection() as conn:
            cur = conn.execute(
                "SELECT host, resource, capacity, free, kind "
                "FROM resource_slots ORDER BY host, resource"
            )
            rows = cur.fetchall()
    except Exception:
        log.warning("factory: resource_slots read failed", exc_info=True)
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for host, resource, capacity, free, kind in rows:
        cap_i, free_i = int(capacity), int(free)
        # Soft gauges (6d memory-pressure + the container_agent capability
        # flag) render as a coloured indicator, not a plain capability chip:
        # free is measured headroom / a 0-1 verified flag (0 = under pressure or
        # degraded … capacity = plenty / verified). Colour ok/warn/crit; the
        # per-gauge label + tooltip come from ``_soft_gauge_render``.
        pressure: str | None = None
        label: str | None = None
        ptitle: str | None = None
        if kind == "soft" and cap_i > 0:
            ratio = free_i / cap_i
            pressure = "crit" if free_i == 0 else ("warn" if ratio < 0.5 else "ok")
            label, ptitle = _soft_gauge_render(resource, host, free_i, cap_i, pressure)
        out.setdefault(host, []).append(
            {
                "resource": resource,
                "capacity": cap_i,
                "free": free_i,
                "kind": kind,
                "pressure": pressure,
                "label": label,
                "ptitle": ptitle,
                "desc": _slot_desc(resource, free_i, cap_i, kind),
            }
        )
    return out


#: gr162694 #2 wants "level >= warning", not just ERROR/CRITICAL — a host
#: quietly emitting WARNINGs (no hard failure yet) should still surface.
_ERROR_LEVELS = ("WARNING", "ERROR", "CRITICAL")

#: The visible truncated line's length — the spec's "~80 chars"; the full
#: line + timestamp still carries in the chip's ``title=`` mouseover.
_LATEST_ERROR_LINE_CHARS = 80


def _errors_by_host(store: Any) -> dict[str, dict[str, Any]]:
    """host -> {count, samples[], latest} of recent WARNING+ ``worker_logs``.

    A per-machine health readout for the host strip: how many warning-or-
    worse log lines each host emitted in the last :data:`_ERROR_WINDOW`,
    plus the newest few (time-ago + pass + trimmed message) for the
    mouseover, plus ``latest`` — the single most recent line, truncated to
    :data:`_LATEST_ERROR_LINE_CHARS` for direct display (gr162694 #2: the
    strip should show *what* broke, not just a count behind a hover),
    carrying the full line + absolute timestamp in its own ``title``. Empty
    on error (the console degrades rather than 500s, same as every other
    reader here).
    """
    try:
        with store.pool.connection() as conn:
            cur = conn.execute(
                "SELECT host, ts, level, pass, message FROM worker_logs "
                "WHERE level = ANY(%s) "
                "  AND ts > now() - %s::interval "
                "ORDER BY ts DESC LIMIT 300",
                (list(_ERROR_LEVELS), _ERROR_WINDOW),
            )
            rows = cur.fetchall()
    except Exception:
        log.warning("factory: worker_logs error read failed", exc_info=True)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for host, ts, level, pass_, message in rows:
        entry = out.setdefault(host, {"count": 0, "samples": [], "latest": None})
        entry["count"] += 1
        full = (message or "").strip().replace("\n", " ")
        if entry["latest"] is None:  # rows arrive newest-first
            entry["latest"] = {
                "line": full[:_LATEST_ERROR_LINE_CHARS],
                "title": f"{_abs_ts(ts)} · {level} · {pass_ or '?'} · {full}",
            }
        if len(entry["samples"]) < 5:
            entry["samples"].append(
                {
                    "ago": _ago(ts),
                    "pass": pass_ or "?",
                    "msg": full[:160],
                }
            )
    return out


def _quests(store: Any) -> dict[str, Any]:
    """Active quests with prio + windowed usage in characters vs proportional share (§9).

    The quests tab is the same mental model as services — set a priority, the
    system allocates proportionally — on the striving substrate. Each row
    carries its trailing-window usage in characters (the tote, metered in
    chars per gripe 162594 — ``cost_usd`` is null for the free/quota-bound
    quest-tick lane) against its priority-weighted share of the budget,
    rendered as a bar. A quest at/over 100% is what the allocator's
    ``over_budget`` skips. Read-only for now (prio + enable/disable reuse the
    quest handler); empty on error.
    """
    try:
        from precis.quest import allocator as alloc
        from precis.quest import reweight

        active = alloc.active_quest_ids(store)
        budget = alloc._char_budget_total()
        window = alloc.BUDGET_WINDOW_DAYS
        if not active:
            return {"window_days": window, "budget": budget, "rows": []}
        weights = {
            q: reweight.base_weight(store.get_ref(kind="quest", id=q).prio)
            for q in active
        }
        denom = sum(weights.values()) or 1.0
        rows: list[dict[str, Any]] = []
        for q in active:
            ref = store.get_ref(kind="quest", id=q)
            spend = alloc.weekly_chars(store, q, days=window)
            share = (budget * weights[q] / denom) if budget else None
            pct = min(100.0, 100.0 * spend / share) if share and share > 0 else None
            rows.append(
                {
                    "id": q,
                    "title": (ref.title if ref else f"quest {q}") or f"quest {q}",
                    "prio": ref.prio if ref else None,
                    "spend": round(spend),
                    "share": round(share) if share is not None else None,
                    "pct": round(pct, 1) if pct is not None else None,
                    "over": bool(share is not None and spend >= share),
                }
            )
        # Heaviest share-consumers first — the fair-share story reads top-down.
        rows.sort(key=lambda r: (r["pct"] is None, -(r["pct"] or 0.0)))
        return {"window_days": window, "budget": budget, "rows": rows}
    except Exception:
        log.warning("factory: quests read failed", exc_info=True)
        return {"window_days": 7, "budget": None, "rows": []}


def _config_rows(store: Any) -> list[tuple[str, str, int, str | None]]:
    """All ``service_config`` rows as ``(service, host, prio, model_pref)``."""
    try:
        with store.pool.connection() as conn:
            cur = conn.execute(
                "SELECT service, host, prio, model_pref FROM service_config "
                "ORDER BY service, host"
            )
            return [(s, h, int(p), m) for s, h, p, m in cur.fetchall()]
    except Exception:
        log.warning("factory: service_config read failed", exc_info=True)
        return []


def _activity(store: Any) -> dict[str, dict[str, Any]]:
    """handler -> {last_ok, last_fail} from ``worker_logs`` BatchResult rows.

    Thin wrapper over :func:`precis.health_checks.activity_by_handler` (the
    shared registry × worker_logs join truth — §D, ``health_digest``'s
    Layer-2 "intended-on but silent" coherence check reads the same
    function over a shorter window). See that function's docstring for the
    exact shape.
    """
    return health_checks.activity_by_handler(store)


def _reserves(store: Any) -> dict[str, dict[str, Any]]:
    """host -> its active (unexpired) reserve row (§B-2, gr162694 §K).

    ``service_config``'s ``reserve`` pseudo-service (``workers/
    service_config.py``) is otherwise invisible outside the claim
    transaction it gates — an operator who reserved a host and forgot
    would only discover it days later via a starved queue. Keyed by the
    literal ``host`` column, so a wildcard (``ALL_HOSTS``) reserve shows
    up under that key; the caller decides how to fan it out to every
    host's row. Empty on error, same as every other reader here.
    """
    try:
        with store.pool.connection() as conn:
            cur = conn.execute(
                "SELECT host, expires_at, actor FROM service_config "
                "WHERE service = %s AND expires_at IS NOT NULL AND expires_at > now()",
                (RESERVE_SERVICE,),
            )
            rows = cur.fetchall()
    except Exception:
        log.warning("factory: reserve read failed", exc_info=True)
        return {}
    return {
        host: {"expires_at": expires_at, "actor": actor}
        for host, expires_at, actor in rows
    }


#: Executor passes are named ``job_<executor>`` (``job_ssh_node``,
#: ``job_claude_inproc``, ``job_claude_docker``) — they drain ``kind='job'``
#: rows reactively (fires only when work exists), not on a fixed cadence, so
#: "next run" is blank for them (gr162694 #1's "pull/claim executor passes").
_JOB_EXECUTOR_PREFIX = "job_"


def _scheduler_leases(store: Any) -> dict[str, dict[str, Any]]:
    """cadence name -> its ``scheduler_leases`` row (empty on error).

    Backs the Services sub-tab's "next run" column (gr162694 #1):
    ``workers/scheduler.py``'s ``CADENCES`` each claim a lease per fire,
    ``next_fire_at`` being the wall-clock next attempt. Everything else
    either runs every worker loop cycle or drains a job queue reactively
    (see :func:`_next_run`).
    """
    try:
        with store.pool.connection() as conn:
            cur = conn.execute(
                "SELECT name, next_fire_at, interval_s, last_fired_at, last_host "
                "FROM scheduler_leases"
            )
            rows = cur.fetchall()
    except Exception:
        log.warning("factory: scheduler_leases read failed", exc_info=True)
        return {}
    return {
        name: {
            "next_fire_at": next_fire_at,
            "interval_s": int(interval_s),
            "last_fired_at": last_fired_at,
            "last_host": last_host,
        }
        for name, next_fire_at, interval_s, last_fired_at, last_host in rows
    }


def _duration(secs: float) -> str:
    """Compact duration text ('45s', '3m', '2h', '1d') — mirrors ``_ago``'s
    buckets minus the trailing 'ago' (used for both past and future deltas)."""
    secs = max(0.0, secs)
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs / 60)}m"
    if secs < 172800:
        return f"{int(secs / 3600)}h"
    return f"{int(secs / 86400)}d"


def _next_run(
    service_name: str, kind: str, leases: dict[str, dict[str, Any]]
) -> dict[str, str] | None:
    """The Services sub-tab "next run" cell for one row (gr162694 #1).

    A cadence claimed via ``scheduler_leases`` shows its wall-clock next
    fire (overdue reads "overdue Xm", pending reads "in Xm"); every other
    ``pass``-kind row runs every idle worker-loop cycle ("every cycle")
    EXCEPT the ``job_*`` executor passes, which drain ``kind='job'`` rows
    reactively — blank, same as a daemon/serving/compute row. ``None`` →
    the template renders "—".
    """
    lease = leases.get(service_name)
    if lease is not None:
        secs_until = -(_age_seconds(lease["next_fire_at"]) or 0.0)
        when, mag = ("overdue", -secs_until) if secs_until <= 0 else ("in", secs_until)
        last = (
            f"last fired {_ago(lease['last_fired_at'])} on {lease['last_host']}"
            if lease["last_fired_at"]
            else "never fired yet"
        )
        title = (
            f"Cadence lease — every {lease['interval_s']}s; {last}; "
            f"next attempt ~{_abs_ts(lease['next_fire_at'])}."
        )
        return {"text": f"{when} {_duration(mag)}", "title": title}
    if kind != "pass" or service_name.startswith(_JOB_EXECUTOR_PREFIX):
        return None
    return {
        "text": "every cycle",
        "title": "Runs every worker loop cycle — no fixed schedule, "
        "checks for due work each tick.",
    }


def _llm_models(store: Any) -> list[str]:
    """Model ids from the ``llm`` catalog for the model_pref dropdown."""
    try:
        cards = store.list_refs(kind="llm", limit=200)
    except Exception:
        return []
    ids = sorted(
        {model_id for c in cards if (model_id := (c.meta or {}).get("model_id"))}
    )
    return [str(i) for i in ids]


def _host_options(hosts: list[dict[str, Any]], config: list[Any]) -> list[str]:
    """The host selector options: ``*`` then every host we know of."""
    known = {h["host"] for h in hosts} | {row[1] for row in config if row[1] != _ALL}
    return [_ALL, *sorted(known)]


@router.get("", response_model=None)
@router.get("/", response_model=None)
async def index(host: str = _ALL) -> RedirectResponse:
    """``/factory`` is retired (WS3) — redirect to the Services sub-tab.

    All the compute above (host strip, category tables, quests) is now
    invoked from ``status.py``'s ``_services_ctx`` to build
    ``/status?tab=services``; this route only preserves the old URL.
    """
    return RedirectResponse(url=f"/status?tab=services&host={host}", status_code=307)


def _redirect(host: str) -> RedirectResponse:
    return RedirectResponse(url=f"/status?tab=services&host={host}", status_code=303)


@router.post("/prio", response_model=None)
async def set_prio(
    request: Request,
    host: str = Form(...),
    service: str = Form(...),
    prio: int = Form(...),
) -> RedirectResponse:
    """Set a service's prio for ``host`` (0 = off, 1..10 = claim weight)."""
    store = get_store(request)
    try:
        set_service_prio(store, host, service, max(0, min(10, prio)), actor="web")
    except Exception:
        log.warning("factory: set_prio failed", exc_info=True)
    return _redirect(host)


@router.post("/model", response_model=None)
async def set_model(
    request: Request,
    host: str = Form(...),
    service: str = Form(...),
    model: str = Form(""),
) -> RedirectResponse:
    """Pin (or clear, with an empty value) a service's model_pref for ``host``."""
    store = get_store(request)
    try:
        set_service_model(store, host, service, model or None, actor="web")
    except Exception:
        log.warning("factory: set_model failed", exc_info=True)
    return _redirect(host)


@router.post("/clear", response_model=None)
async def clear(
    request: Request,
    host: str = Form(...),
    service: str = Form(...),
) -> RedirectResponse:
    """Delete the ``(host, service)`` row — revert to the env/profile default."""
    store = get_store(request)
    try:
        clear_service_config(store, host, service)
    except Exception:
        log.warning("factory: clear failed", exc_info=True)
    return _redirect(host)


@router.post("/reserve", response_model=None)
async def reserve(
    request: Request,
    host: str = Form(...),
    hours: float = Form(4.0),
) -> RedirectResponse:
    """Put ``host`` (or ``*`` for every host) into reserve mode for ``hours``
    (§B-2, gr162694 §K) — the ONE door is :func:`set_reserve`; an out-of-
    range ``hours`` (<= 0 or > 168) is its ``ValueError``, logged and
    refused rather than silently clamped (unlike ``/prio``'s clamp)."""
    store = get_store(request)
    try:
        set_reserve(store, host, hours=hours, actor="web")
    except ValueError:
        log.warning("factory: reserve refused — bad hours=%r for host=%s", hours, host)
    except Exception:
        log.warning("factory: reserve failed", exc_info=True)
    return _redirect(host)


@router.post("/release", response_model=None)
async def release(request: Request, host: str = Form(...)) -> RedirectResponse:
    """Clear ``host``'s reserve row (§B-2) — the ONE door is
    :func:`clear_reserve`."""
    store = get_store(request)
    try:
        clear_reserve(store, host)
    except Exception:
        log.warning("factory: release failed", exc_info=True)
    return _redirect(host)


#: The four ADR 0066 capability tiers an operator placement-chain can target
#: (Phase B step 2; Phase C made these the only ``Tier`` members) — keyed by
#: the ``Tier`` string values.
_CHAIN_TIERS: dict[str, Tier] = {
    Tier.FRONTIER.value: Tier.FRONTIER,
    Tier.BIG.value: Tier.BIG,
    Tier.MEDIUM.value: Tier.MEDIUM,
    Tier.SMALL.value: Tier.SMALL,
}


def _set_chain_override(store: Any, tier: Tier, chain_json: str) -> None:
    """Write (or, for blank text, clear) one tier's placement-chain override.

    Blank ``chain_json`` reverts the tier to the compiled default ladder.
    Non-blank text must parse as JSON *and* be a list — a malformed row can't
    dark a tier, so a decode failure or a non-list value is logged and
    dropped without writing anything (the previous override, if any, is left
    untouched rather than clobbered by garbage).
    """
    text = chain_json.strip()
    if not text:
        budget_settings.clear_setting(store, live_config.chain_key(tier))
        live_config.bust_cache()
        return
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("factory: ignoring malformed chain JSON for tier=%s", tier.value)
        return
    if not isinstance(parsed, list):
        log.warning(
            "factory: ignoring non-list chain JSON for tier=%s (got %s)",
            tier.value,
            type(parsed).__name__,
        )
        return
    budget_settings.set_setting(store, live_config.chain_key(tier), chain_json)
    live_config.bust_cache()


@router.post("/llm/chain", response_model=None)
async def set_llm_chain(
    request: Request,
    tier: str = Form(...),
    chain_json: str = Form(""),
) -> RedirectResponse:
    """Write an operator placement-chain override for one capability tier
    (ADR 0066 Phase B step 2) — blank ``chain_json`` clears it back to the
    default ladder. An unrecognized ``tier`` or malformed JSON is a no-op."""
    store = get_store(request)
    try:
        resolved = _CHAIN_TIERS.get(tier)
        if resolved is not None:
            _set_chain_override(store, resolved, chain_json)
    except Exception:
        log.warning("factory: set_llm_chain failed", exc_info=True)
    return RedirectResponse(url="/status?tab=services", status_code=303)


def _set_op_override(store: Any, source: str, tier: str, model: str) -> None:
    """Write (or, for blank/"default" ``tier``, clear) one operation's
    override (the per-operation routing editor).

    Guarded to the steerable allow-list — an excluded or unregistered
    ``source`` is a no-op (the template renders those rows read-only; this
    is defense-in-depth, not the only guard).

    ``tier`` and ``model`` are **mutually exclusive** — a single combined
    control on the form — so ``model`` is read ONLY when ``tier ==
    "pinned"``; a plain capability tier (``frontier``/``big``/``medium``/
    ``small``) always wins and ignores any stale ``model`` field, so
    changing only the tier dropdown on a pinned op can never be silently
    discarded:

    - blank or ``"default"`` → clear the row (registry default).
    - a recognized capability tier (in :data:`_CHAIN_TIERS`) → ``{"tier": ...}``.
    - ``"pinned"`` → ``{"model": ...}`` if ``model`` is non-blank, else a
      no-op (leaves the prior override, if any, untouched).
    - anything else (an unrecognized tier) → a no-op.
    """
    from precis.utils.llm import operations

    if not operations.is_steerable(source):
        log.warning(
            "factory: ignoring llm/op override for non-steerable source=%s", source
        )
        return

    tier = tier.strip()
    model = model.strip()

    payload: dict[str, str]
    if not tier or tier == "default":
        budget_settings.clear_setting(store, live_config.op_key(source))
        live_config.bust_cache()
        return
    elif tier in _CHAIN_TIERS:
        payload = {"tier": tier}
    elif tier == "pinned":
        if not model:
            log.warning(
                "factory: ignoring pinned llm/op override for source=%s "
                "with no model selected",
                source,
            )
            return
        payload = {"model": model}
    else:
        log.warning("factory: ignoring unknown tier=%r for op source=%s", tier, source)
        return

    budget_settings.set_setting(store, live_config.op_key(source), json.dumps(payload))
    live_config.bust_cache()


@router.post("/llm/op", response_model=None)
async def set_llm_op(
    request: Request,
    source: str = Form(...),
    tier: str = Form(""),
    model: str = Form(""),
) -> RedirectResponse:
    """Write (or clear) an operator override for one steerable operation
    (the per-operation routing editor) — blank/
    ``"default"`` ``tier`` reverts to the registry default; ``model`` is
    only honoured when ``tier == "pinned"``, so a plain capability tier
    can never be discarded by a stale sticky model selection. A
    non-steerable ``source`` (excluded or unregistered) is a no-op."""
    store = get_store(request)
    try:
        _set_op_override(store, source, tier, model)
    except Exception:
        log.warning("factory: set_llm_op failed", exc_info=True)
    return RedirectResponse(url="/status?tab=services", status_code=303)


def _set_cloud_enabled(store: Any, enabled: bool) -> None:
    """Write the ADR 0066 §5 cloud-throttle dial. ``True`` clears the row
    (back to default-on); only an explicit ``False`` writes ``"false"``."""
    if enabled:
        budget_settings.clear_setting(store, live_config.CLOUD_ENABLED_KEY)
    else:
        budget_settings.set_setting(store, live_config.CLOUD_ENABLED_KEY, "false")
    live_config.bust_cache()


@router.post("/llm/cloud", response_model=None)
async def set_llm_cloud(request: Request, enabled: str = Form(...)) -> RedirectResponse:
    """Flip the fleet-wide cloud-throttle dial — ``enabled=false`` pauses
    every tier's cloud rungs; anything else clears back to default-on."""
    store = get_store(request)
    try:
        _set_cloud_enabled(store, enabled.strip().lower() != "false")
    except Exception:
        log.warning("factory: set_llm_cloud failed", exc_info=True)
    return RedirectResponse(url="/status?tab=services", status_code=303)
