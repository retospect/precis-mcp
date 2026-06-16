"""Tags browser — every tag in the cluster, ordered by usage count.

Single-page table. Each row carries the namespace:value, a per-tag
usage count, and a one-click pivot to the Tasks dashboard filtered
to refs carrying that tag. Useful when the operator wants to see
what conventions have crept into the corpus (which ``DREAM:*`` and
``tier:*`` and ``waiting-for:*`` tags exist, who's using
``user:asa`` vs ``user:hermes``, what `OPEN` vocabulary is in play).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis_web.deps import get_store, templates

router = APIRouter(prefix="/tags", tags=["tags"])


def _list_tags(store: object, q: str, limit: int) -> list[dict[str, object]]:
    """Top-N tags by usage. ``q=`` is a case-insensitive substring filter on
    ``namespace:value``.
    """
    sql = """
        SELECT t.namespace, t.value, count(rt.ref_id) AS n
          FROM tags t LEFT JOIN ref_tags rt USING(tag_id)
         WHERE %s = ''
            OR (t.namespace || ':' || t.value) ILIKE %s
         GROUP BY t.namespace, t.value
         ORDER BY n DESC, t.namespace, t.value
         LIMIT %s
    """
    pattern = f"%{q.strip()}%"
    with store.pool.connection() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(sql, (q.strip(), pattern, limit)).fetchall()
    return [
        {
            "namespace": str(r[0]),
            "value": str(r[1]),
            "label": f"{r[0]}:{r[1]}",
            "count": int(r[2]),
        }
        for r in rows
    ]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request, q: str | None = None) -> HTMLResponse:
    """Render the tag table. ``?q=`` narrows by substring; default top 200."""
    store = get_store(request)
    query = (q or "").strip()
    rows = _list_tags(store, query, limit=200)
    return templates.TemplateResponse(
        request,
        "tags/index.html.j2",
        {
            "active_tab": "tags",
            "q": query,
            "rows": rows,
        },
    )
