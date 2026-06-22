"""Agent logs tab — run-attribution records.

A thin read over ``kind='agentlog'`` rows opened by run machinery
(``plan_tick`` today; operator / chat edits over time). The write side —
opening a log, attaching ``touched`` links, GC — lives in
:mod:`precis.agentlog` and the sweeper; this route just lists recent
runs and renders one run's assembled prompt + touched chunks for
debugging "why does this chunk look like that?".

* ``GET /agentlogs``      — recent runs, newest-first, grouped by source.
* ``GET /agentlogs/{id}`` — one run: prompt + model/source + the chunks
  it touched + a link to the full LLM transcript on its job.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis.agentlog import list_recent
from precis.errors import NotFound
from precis_web.deps import get_store, templates
from precis_web.timefmt import ago as _ago

router = APIRouter(tags=["agentlogs"])

log = logging.getLogger(__name__)


def _group_by_source(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group recent runs by source (plan_tick / operator / chat / …)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        groups.setdefault(r.get("source") or "unknown", []).append(r)
    return [
        {"source": source, "runs": rows, "count": len(rows)}
        for source, rows in sorted(groups.items())
    ]


@router.get("/agentlogs", response_class=HTMLResponse)
async def agentlogs(request: Request) -> HTMLResponse:
    """Recent agentic runs, grouped by source."""
    store = get_store(request)
    runs = list_recent(store, limit=300)
    for r in runs:
        r["created"] = _ago(r["created_at"])
    ctx = {
        "active_tab": "agentlogs",
        "groups": _group_by_source(runs),
        "total": len(runs),
    }
    return templates.TemplateResponse(request, "agentlogs/list.html.j2", ctx)


def _touched_chunks(store: Any, log_id: int) -> list[dict[str, Any]]:
    """Draft chunks this run wrote/moved — slug + handle + a text clip,
    linking back to the block in the draft reader."""
    sql = """
        SELECT d.ref_id,
               (SELECT ri.id_value FROM ref_identifiers ri
                 WHERE ri.ref_id = d.ref_id AND ri.id_kind = 'cite_key'
                 LIMIT 1) AS slug,
               d.kind,
               c.handle,
               left(c.text, 200) AS clip
          FROM links l
          JOIN chunks c ON c.chunk_id = l.dst_chunk_id
          JOIN refs  d ON d.ref_id = c.ref_id
         WHERE l.src_ref_id = %s
           AND l.relation = 'touched'
           AND c.retired_at IS NULL
           AND d.deleted_at IS NULL
         ORDER BY d.ref_id, c.ord
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (log_id,)).fetchall()
    out: list[dict[str, Any]] = []
    for ref_id, slug, kind, handle, clip in rows:
        ident = slug or str(ref_id)
        href = (
            f"/drafts/{ident}#c-{handle}" if kind == "draft" else f"/r/{kind}/{ident}"
        )
        out.append(
            {
                "kind": kind,
                "ident": ident,
                "handle": handle,
                "clip": (clip or "").strip(),
                "href": href,
            }
        )
    return out


@router.get("/agentlogs/{ref_id}", response_class=HTMLResponse)
async def agentlog_detail(request: Request, ref_id: int) -> HTMLResponse:
    """One run: assembled prompt, model/source, touched chunks, transcript."""
    store = get_store(request)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title, meta, created_at FROM refs "
            "WHERE ref_id = %s AND kind = 'agentlog' AND deleted_at IS NULL",
            (ref_id,),
        ).fetchone()
    if row is None:
        raise NotFound(f"no agentlog {ref_id}")
    title, meta, created_at = row
    meta = dict(meta or {})
    ctx = {
        "active_tab": "agentlogs",
        "ref_id": ref_id,
        "title": title,
        "source": meta.get("source"),
        "model": meta.get("model"),
        "status": meta.get("status"),
        "prompt": meta.get("prompt") or "",
        "parent_ref_id": meta.get("parent_ref_id"),
        "job_ref_id": meta.get("job_ref_id"),
        "started_at": meta.get("started_at"),
        "ended_at": meta.get("ended_at"),
        "created": _ago(created_at),
        "touched": _touched_chunks(store, ref_id),
    }
    return templates.TemplateResponse(request, "agentlogs/detail.html.j2", ctx)
