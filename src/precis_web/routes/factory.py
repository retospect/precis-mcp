"""Factory console (``/factory``) — the window onto what the factory runs.

Host strip (load / worker-alive per machine) over one list per category
of services — every pass / job-type / compute / daemon / serving row from
the one `ServiceSpec` registry, joined to its live `service_config` prio
and its last-success / last-failure from `worker_logs`
(docs/design/factory-console-and-scheduling.md, slices 3–4).

* **Slice 3 (read):** the host strip + the total service list + last
  activity, all degrading to empty on a schema surprise (status-tab
  pattern).
* **Slice 4 (write):** a host selector scopes the page; each row's prio
  is editable (0 = off, 1..10 = claim weight) and model-using rows get a
  model_pref dropdown populated from the `llm` catalog. The writes go
  straight to `service_config`; the worker picks them up next cycle.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.workers.registry import SERVICES, ServiceKind
from precis.workers.service_config import (
    DEFAULT_PRIO,
    clear_service_config,
    set_service_model,
    set_service_prio,
)
from precis_web.deps import get_store, templates
from precis_web.timefmt import ago as _ago

router = APIRouter(prefix="/factory", tags=["factory"])

log = logging.getLogger(__name__)

#: A host silent longer than this reads as "dead worker" in the strip.
_STALE_AFTER_S = 600

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
                "SELECT host, ts, temp_c, load1, load5, load15, "
                "       EXTRACT(EPOCH FROM (now() - ts)) AS age_s "
                "FROM host_heartbeat ORDER BY host"
            )
            rows = cur.fetchall()
    except Exception:
        log.warning("factory: host_heartbeat read failed", exc_info=True)
        return []
    out: list[dict[str, Any]] = []
    for host, ts, temp_c, load1, load5, load15, age_s in rows:
        age = float(age_s) if age_s is not None else None
        out.append(
            {
                "host": host,
                "alive": age is not None and age <= _STALE_AFTER_S,
                "ago": _ago(ts),
                "temp_c": temp_c,
                "load1": load1,
                "load5": load5,
                "load15": load15,
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
        # Soft gauges (the 6d memory-pressure signal) render as a coloured
        # pressure indicator, not a plain capability chip: free is measured
        # headroom (0 = under pressure … capacity = plenty). RAM pressure is
        # the thing to watch when a host runs a container runtime (OrbStack /
        # podman VMs eat memory), so surface it as ok/warn/crit.
        pressure: str | None = None
        if kind == "soft" and cap_i > 0:
            ratio = free_i / cap_i
            pressure = "crit" if free_i == 0 else ("warn" if ratio < 0.5 else "ok")
        out.setdefault(host, []).append(
            {
                "resource": resource,
                "capacity": cap_i,
                "free": free_i,
                "kind": kind,
                "pressure": pressure,
            }
        )
    return out


def _quests(store: Any) -> dict[str, Any]:
    """Active quests with prio + windowed spend vs proportional share (§9).

    The quests tab is the same mental model as services — set a priority, the
    system allocates proportionally — on the striving substrate. Each row
    carries its trailing-window spend (the tote, now honest per gripe 162594)
    against its priority-weighted share of the budget, rendered as a bar. A
    quest at/over 100% is what the allocator's ``over_budget`` skips. Read-only
    for now (prio + enable/disable reuse the quest handler); empty on error.
    """
    try:
        from precis.quest import allocator as alloc
        from precis.quest import reweight

        active = alloc.active_quest_ids(store)
        budget = alloc._budget_total()
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
            spend = alloc.weekly_spend(store, q, days=window)
            share = (budget * weights[q] / denom) if budget else None
            pct = min(100.0, 100.0 * spend / share) if share and share > 0 else None
            rows.append(
                {
                    "id": q,
                    "title": (ref.title if ref else f"quest {q}") or f"quest {q}",
                    "prio": ref.prio if ref else None,
                    "spend": round(spend, 4),
                    "share": round(share, 4) if share is not None else None,
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

    Keyed by the ``payload.handler`` string (a pass's ``BatchResult.handler``,
    which is what actually lands — not the logger-derived ``pass`` column),
    so callers look it up via ``ServiceSpec.log_handler``. The numeric-guard
    regex keeps the cast safe on non-BatchResult payloads.
    """
    try:
        with store.pool.connection() as conn:
            cur = conn.execute(
                "SELECT payload->>'handler' AS h, "
                "  MAX(ts) FILTER ("
                "    WHERE payload->>'ok' ~ '^[0-9]+$' "
                "      AND (payload->>'ok')::int > 0) AS last_ok, "
                "  MAX(ts) FILTER ("
                "    WHERE payload->>'failed' ~ '^[0-9]+$' "
                "      AND (payload->>'failed')::int > 0) AS last_fail "
                "FROM worker_logs "
                "WHERE payload ? 'handler' AND ts > now() - interval '7 days' "
                "GROUP BY payload->>'handler'"
            )
            rows = cur.fetchall()
    except Exception:
        log.warning("factory: worker_logs activity read failed", exc_info=True)
        return {}
    return {h: {"last_ok": ok, "last_fail": fail} for h, ok, fail in rows}


def _llm_models(store: Any) -> list[str]:
    """Model ids from the ``llm`` catalog for the model_pref dropdown."""
    try:
        cards = store.list_refs(kind="llm", limit=200)
    except Exception:
        return []
    ids = sorted(
        {
            (c.meta or {}).get("model_id")
            for c in cards
            if (c.meta or {}).get("model_id")
        }
    )
    return [str(i) for i in ids]


def _host_options(hosts: list[dict[str, Any]], config: list[Any]) -> list[str]:
    """The host selector options: ``*`` then every host we know of."""
    known = {h["host"] for h in hosts} | {row[1] for row in config if row[1] != _ALL}
    return [_ALL, *sorted(known)]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request, host: str = _ALL) -> HTMLResponse:
    """Render the factory overview, scoped to ``?host=`` (default all)."""
    store = get_store(request)
    hosts = _hosts(store)
    slots_by_host = _slots_by_host(store)
    for h in hosts:
        h["slots"] = slots_by_host.get(h["host"], [])
    config = _config_rows(store)
    activity = _activity(store)
    models = _llm_models(store)
    host_options = _host_options(hosts, config)
    if host not in host_options:
        host = _ALL

    # Explicit rows for the selected host, and the cross-host override hints.
    exact: dict[str, tuple[int, str | None]] = {
        s: (p, m) for (s, h, p, m) in config if h == host
    }
    others: dict[str, list[str]] = {}
    for s, h, p, _m in config:
        if h != host:
            others.setdefault(s, []).append(f"{h}={p}")

    by_category: dict[str, list[dict[str, Any]]] = {}
    for spec in SERVICES:
        act = activity.get(spec.log_handler, {})
        ex = exact.get(spec.name)
        row = {
            "name": spec.name,
            "label": spec.label,
            "kind": spec.kind.value,
            "one_line": spec.one_line,
            "profiles": ", ".join(sorted(spec.default_profiles)) or "—",
            "enable_env": spec.enable_env,
            "requires": sorted(spec.requires),
            "uses_model": spec.uses_model,
            "external": list(spec.uses_external),
            "has_agent": spec.introspect is not None,
            "prio": ex[0] if ex is not None else None,  # None → "default"
            "model_pref": ex[1] if ex is not None else None,
            "others": ", ".join(others.get(spec.name, [])),
            "last_ok": _ago(act["last_ok"]) if act.get("last_ok") else None,
            "last_fail": _ago(act["last_fail"]) if act.get("last_fail") else None,
        }
        by_category.setdefault(spec.category, []).append(row)

    ordered = [c for c in _CATEGORY_ORDER if c in by_category]
    ordered += sorted(c for c in by_category if c not in _CATEGORY_ORDER)
    categories = [{"name": c, "services": by_category[c]} for c in ordered]

    return templates.TemplateResponse(
        request,
        "factory/index.html.j2",
        {
            "active_tab": "factory",
            "hosts": hosts,
            "categories": categories,
            "default_prio": DEFAULT_PRIO,
            "selected_host": host,
            "host_options": host_options,
            "models": models,
            "service_kinds": [k.value for k in ServiceKind],
            "quests": _quests(store),
        },
    )


def _redirect(host: str) -> RedirectResponse:
    return RedirectResponse(url=f"/factory?host={host}", status_code=303)


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
