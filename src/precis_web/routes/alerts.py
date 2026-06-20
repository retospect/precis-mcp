"""Alerts tab — open operational / health conditions.

A thin read over ``kind='alert'`` rows raised by background passes
(nursery's spin-loop / orphan / stale-claim / … detectors today; more
producers over time). The heavy lifting — detection, dedup, lifecycle
— lives in :mod:`precis.alerts` and the producing workers; this route
just lists what's currently open, grouped by source, severity-sorted.

* ``GET /alerts`` — open alerts (default).
* ``GET /alerts?state=resolved`` — recently-resolved alerts (history).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis.alerts import STATE_OPEN, STATE_RESOLVED
from precis_web.deps import get_store, templates
from precis_web.timefmt import ago as _ago

router = APIRouter(tags=["alerts"])

log = logging.getLogger(__name__)

#: How many resolved alerts the history view shows. Open alerts are
#: unbounded (you want to see them all); resolved is just a recent tail.
_RESOLVED_LIMIT = 100

_SEVERITY_RANK = {"critical": 0, "warn": 1, "info": 2}


def _rows(store: Any, *, state_tag: str, limit: int | None) -> list[dict[str, Any]]:
    """Alerts carrying ``state_tag``, severity- then recency-ordered."""
    sql = """
        SELECT r.ref_id,
               r.title,
               r.meta->>'alert_source'   AS source,
               r.meta->>'severity'       AS severity,
               r.meta->>'detail'         AS detail,
               r.meta->>'subject_ref_id' AS subject_ref_id,
               sr.kind                   AS subject_kind,
               COALESCE((r.meta->>'seen_count')::int, 1) AS seen_count,
               r.created_at,
               r.updated_at
          FROM refs r
          JOIN ref_tags rt ON rt.ref_id = r.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
          LEFT JOIN refs sr
                 ON sr.ref_id = NULLIF(r.meta->>'subject_ref_id', '')::int
         WHERE r.kind = 'alert'
           AND r.deleted_at IS NULL
           AND t.namespace = 'OPEN'
           AND t.value = %s
         ORDER BY CASE r.meta->>'severity'
                    WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                  r.updated_at DESC
    """
    params: list[Any] = [state_tag]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with store.pool.connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "ref_id": int(r[0]),
                "title": r[1],
                "source": r[2] or "unknown",
                "severity": r[3] or "warn",
                "detail": r[4] or "",
                "subject_ref_id": int(r[5]) if r[5] is not None else None,
                "subject_kind": r[6],
                "seen_count": int(r[7]),
                "first_seen": _ago(r[8]),
                "last_seen": _ago(r[9]),
            }
        )
    return out


def _group_by_source(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group open alerts by source for a sectioned display."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for a in alerts:
        groups.setdefault(a["source"], []).append(a)

    # Order groups by their worst (lowest-rank) severity, then name.
    def _key(item: tuple[str, list[dict[str, Any]]]) -> tuple[int, str]:
        source, rows = item
        worst = min(_SEVERITY_RANK.get(r["severity"], 1) for r in rows)
        return (worst, source)

    return [
        {"source": source, "alerts": rows, "count": len(rows)}
        for source, rows in sorted(groups.items(), key=_key)
    ]


@router.get("/alerts", response_class=HTMLResponse)
async def alerts(request: Request, state: str = "open") -> HTMLResponse:
    """List open (default) or recently-resolved alerts."""
    store = get_store(request)
    if state == "resolved":
        rows = _rows(store, state_tag=STATE_RESOLVED, limit=_RESOLVED_LIMIT)
        ctx = {
            "active_tab": "alerts",
            "state": "resolved",
            "groups": _group_by_source(rows),
            "total": len(rows),
        }
    else:
        rows = _rows(store, state_tag=STATE_OPEN, limit=None)
        ctx = {
            "active_tab": "alerts",
            "state": "open",
            "groups": _group_by_source(rows),
            "total": len(rows),
        }
    return templates.TemplateResponse(request, "alerts/list.html.j2", ctx)
