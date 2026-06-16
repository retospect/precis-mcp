"""Status tab — corpus / ingest / worker health.

Direct SQL summaries off the live DB: ref counts per kind, the paper
corpus (held vs stub), todo status breakdown, finding-chase status,
and the most recent ``ref_events`` (ingests, status flips, worker
activity). Each section is computed defensively so a schema surprise
in one query degrades to an empty panel instead of a 500.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis_web.deps import get_store, get_web_config, templates

router = APIRouter(prefix="/status", tags=["status"])

log = logging.getLogger(__name__)

#: A host that hasn't reported (heartbeat) or logged (worker_logs)
#: within this many seconds is flagged stale in the UI. Generous
#: enough to ride out a missed reporter tick on a few-minute cadence.
_STALE_AFTER_S = 600


def _safe(fn) -> Any:  # type: ignore[no-untyped-def]
    """Run a query closure, returning its result or a sentinel on error."""
    try:
        return fn()
    except Exception:
        log.exception("precis web status: section query failed")
        return None


def _age_seconds(ts: Any) -> float | None:
    """Seconds since ``ts`` (a tz-aware datetime), or ``None``."""
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds()


def _ago(ts: Any) -> str:
    """Compact relative-time string ('3m ago', '2h ago')."""
    secs = _age_seconds(ts)
    if secs is None:
        return ""
    secs = max(0.0, secs)
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs / 60)}m ago"
    if secs < 172800:
        return f"{int(secs / 3600)}h ago"
    return f"{int(secs / 86400)}d ago"


def _kind_counts(store: Any) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT kind, count(*)::int FROM refs WHERE deleted_at IS NULL "
            "GROUP BY kind ORDER BY count(*) DESC"
        ).fetchall()
    return [{"kind": r[0], "count": int(r[1])} for r in rows]


def _paper_summary(store: Any) -> dict[str, int]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*)::int AS total, "
            "count(*) FILTER (WHERE pdf_sha256 IS NOT NULL)::int AS held "
            "FROM refs WHERE kind = 'paper' AND deleted_at IS NULL"
        ).fetchone()
    total, held = (int(row[0]), int(row[1])) if row else (0, 0)
    return {"total": total, "held": held, "stub": total - held}


def _todo_status(store: Any) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(st.value, 'open') AS status, count(*)::int
              FROM refs r
              LEFT JOIN LATERAL (
                SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                 WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1
              ) st ON TRUE
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
             GROUP BY COALESCE(st.value, 'open')
             ORDER BY count(*) DESC
            """
        ).fetchall()
    return [{"status": r[0], "count": int(r[1])} for r in rows]


def _recent_events(store: Any, limit: int = 20) -> list[dict[str, Any]]:
    # NB: ``ref_events`` stamps its timestamp in column ``ts`` (see
    # 0001_initial.sql), not ``created_at`` — the earlier name made
    # this query raise and the panel silently rendered empty under
    # the ``_safe`` wrapper.
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ts, source, event, ref_id FROM ref_events "
            "ORDER BY ts DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [
        {
            "ts": r[0].strftime("%Y-%m-%d %H:%M") if r[0] else "",
            "source": r[1] or "",
            "event": r[2] or "",
            "ref_id": r[3],
        }
        for r in rows
    ]


def _claude_usage(store: Any) -> dict[str, Any]:
    """Roll up Claude spend from ``ref_events.cost_usd``.

    Every agentic call (dream / reviewers / plan_tick via
    ``claude_agent``) logs an ``agent:done`` event carrying
    ``cost_usd`` and a payload with ``model`` / ``turns_used``. We
    sum cost + count calls over a 24h and 7d window, plus a 7d
    per-model breakdown. Rows without a cost (non-LLM events) are
    excluded by the ``cost_usd IS NOT NULL`` filter.
    """
    with store.pool.connection() as conn:
        totals = conn.execute(
            "SELECT "
            "count(*) FILTER (WHERE ts > now() - interval '24 hours')::int, "
            "COALESCE(sum(cost_usd) FILTER "
            "(WHERE ts > now() - interval '24 hours'), 0)::float, "
            "count(*)::int, "
            "COALESCE(sum(cost_usd), 0)::float "
            "FROM ref_events "
            "WHERE cost_usd IS NOT NULL AND ts > now() - interval '7 days'"
        ).fetchone()
        by_model = conn.execute(
            "SELECT COALESCE(payload->>'model', source) AS label, "
            "count(*)::int, COALESCE(sum(cost_usd), 0)::float "
            "FROM ref_events "
            "WHERE cost_usd IS NOT NULL AND ts > now() - interval '7 days' "
            "GROUP BY COALESCE(payload->>'model', source) "
            "ORDER BY 3 DESC LIMIT 12"
        ).fetchall()
    cd, cost_d, cw, cost_w = (
        (int(totals[0]), float(totals[1]), int(totals[2]), float(totals[3]))
        if totals
        else (0, 0.0, 0, 0.0)
    )
    return {
        "day": {"calls": cd, "cost": cost_d},
        "week": {"calls": cw, "cost": cost_w},
        "by_model": [
            {"label": r[0] or "\u2014", "calls": int(r[1]), "cost": float(r[2])}
            for r in by_model
        ],
    }


def _hosts(store: Any) -> list[dict[str, Any]]:
    """Per-host liveness from ``worker_logs``: last-seen + recent errors.

    A host that logged anything in the last 7 days appears; its
    ``last_seen`` is the newest log line and ``problems`` counts
    WARNING/ERROR rows in the last 24h. ``stale`` flags hosts quiet
    for longer than ``_STALE_AFTER_S``.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT host, max(ts) AS last_seen, "
            "count(*) FILTER (WHERE level IN ('WARNING','ERROR') "
            "AND ts > now() - interval '24 hours')::int AS problems "
            "FROM worker_logs WHERE ts > now() - interval '7 days' "
            "GROUP BY host ORDER BY max(ts) DESC"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        age = _age_seconds(r[1])
        out.append(
            {
                "host": r[0],
                "ago": _ago(r[1]),
                "stale": age is None or age > _STALE_AFTER_S,
                "problems": int(r[2]),
            }
        )
    return out


def _heartbeats(store: Any) -> list[dict[str, Any]]:
    """Per-host sensor snapshot from ``host_heartbeat`` (temp + load).

    Read via raw SQL (not the ``HeartbeatMixin``) so the fake-store
    route tests need no method. ``temp`` / ``load`` are ``None`` when
    the reporting host couldn't read them; ``stale`` flags a missed
    reporter cadence.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT host, ts, temp_c, load1, load5, load15 "
            "FROM host_heartbeat ORDER BY host"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        age = _age_seconds(r[1])
        out.append(
            {
                "host": r[0],
                "ago": _ago(r[1]),
                "stale": age is None or age > _STALE_AFTER_S,
                "temp_c": float(r[2]) if r[2] is not None else None,
                "load1": float(r[3]) if r[3] is not None else None,
                "load5": float(r[4]) if r[4] is not None else None,
                "load15": float(r[5]) if r[5] is not None else None,
            }
        )
    return out


def _app_version() -> str:
    """Installed ``precis-mcp`` version, for stale-server detection."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("precis-mcp")
    except PackageNotFoundError:  # pragma: no cover - editable/source runs
        return "unknown"


_WINDOW_LABEL: dict[str, str] = {
    "five_hour": "5-hour session",
    "seven_day": "Week (all models)",
    "seven_day_sonnet": "Week (Sonnet only)",
    "seven_day_opus": "Week (Opus only)",
    "overage": "Overage / pay-per-use",
}


def _claude_quota(store: Any) -> dict[str, Any]:
    """Render the OAuth utilisation snapshot for the Status panel.

    Reads the singleton ``claude_quota_snapshot`` row written by the
    agent-worker ``quota_check`` pass. Returns ``{}`` when nothing has
    been written yet (free tier, or first run before the pass has
    fired). The template renders "snapshot unavailable" in that case.
    """
    row = store.read_claude_quota(scope="unified")
    if row is None:
        return {}
    data = row.data or {}
    windows = data.get("windows") or {}
    rendered: list[dict[str, Any]] = []
    for key, payload in windows.items():
        if not isinstance(payload, dict):
            continue
        rendered.append(
            {
                "key": key,
                "label": _WINDOW_LABEL.get(key, key),
                "used_percentage": payload.get("used_percentage"),
                "resets_at": payload.get("resets_at"),
            }
        )
    # Show 5h first, then weeklies, then overage; alphabetical within
    # each tier matches Claude Code's own ordering in `/usage`.
    _ORDER = ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus", "overage")
    rendered.sort(key=lambda w: _ORDER.index(w["key"]) if w["key"] in _ORDER else 99)
    return {
        "ts": row.ts.isoformat() if row.ts else None,
        "representative_claim": data.get("representative_claim"),
        "windows": rendered,
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    store = get_store(request)
    cfg = get_web_config(request)
    return templates.TemplateResponse(
        request,
        "status.html.j2",
        {
            "active_tab": "status",
            "kind_counts": _safe(lambda: _kind_counts(store)) or [],
            "papers": _safe(lambda: _paper_summary(store)) or {},
            "todo_status": _safe(lambda: _todo_status(store)) or [],
            "events": _safe(lambda: _recent_events(store)) or [],
            "usage": _safe(lambda: _claude_usage(store)) or {},
            "quota": _safe(lambda: _claude_quota(store)) or {},
            "hosts": _safe(lambda: _hosts(store)) or [],
            "heartbeats": _safe(lambda: _heartbeats(store)) or [],
            "corpus_dir": "  ".join(str(p) for p in cfg.corpus_dirs),
            "app_version": _app_version(),
        },
    )
