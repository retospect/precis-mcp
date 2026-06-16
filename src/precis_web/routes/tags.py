"""Tags browser — every tag in the cluster, ordered by usage count.

Single-page table. Each row carries the namespace:value, a per-tag
usage count, and a one-click pivot to the Tasks dashboard filtered
to refs carrying that tag. Useful when the operator wants to see
what conventions have crept into the corpus (which ``DREAM:*`` and
``tier:*`` and ``waiting-for:*`` tags exist, who's using
``user:asa`` vs ``user:hermes``, what `OPEN` vocabulary is in play).
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis_web.deps import get_store, templates

router = APIRouter(prefix="/tags", tags=["tags"])

#: Namespaces the delete button never offers. Structural / closed-vocab
#: tags (``STATUS``, ``LLM``, ``DREAM``, ``PRIO``, ``SRC``, ``CACHE``,
#: ``EMBED``, …) carry semantic meaning the handlers rely on. Blanket-
#: deleting them across the cluster would break things in obscure ways
#: (a job whose ``STATUS:running`` vanished would never appear failed,
#: a todo with no ``LLM:`` tag would silently fall off the dispatcher).
#: Per-ref removal via the standard ``tag(remove=[...])`` verb is still
#: available for those.
_PROTECTED_NAMESPACES: frozenset[str] = frozenset(
    {
        "STATUS",
        "LLM",
        "DREAM",
        "PRIO",
        "SRC",
        "CACHE",
        "EMBED",
    }
)


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
            "deletable": str(r[0]) not in _PROTECTED_NAMESPACES,
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


@router.get("/refs", response_class=HTMLResponse)
async def refs_by_tag(
    request: Request,
    namespace: str | None = None,
    value: str | None = None,
    kind: str | None = None,
) -> HTMLResponse:
    """List refs matching a tag, a kind, or both, grouped by kind.

    Three call shapes — all hit the same template:

    * ``?namespace=NS&value=V`` — every ref carrying the tag (the
      original "Tags table → view N refs" pivot)
    * ``?kind=K`` — every live ref of one kind (the Status page
      "Refs by kind" chip pivot; DRY with the tag-pivot view above)
    * ``?namespace=NS&value=V&kind=K`` — intersection: refs of one
      kind that also carry the tag

    Page groups by kind and links each ref to its native detail view
    (``/refs/{kind}/{ref_id}`` for browsable kinds, ``/papers/{ref_id}``
    for papers, ``/tasks?focus=N`` for todos).
    """
    if namespace is None and value is None and kind is None:
        raise HTTPException(
            status_code=400,
            detail="needs ?namespace=NS&value=V, ?kind=K, or both",
        )
    has_tag = namespace is not None and value is not None
    store = get_store(request)
    where_parts: list[str] = []
    params: list[object] = []
    if has_tag:
        where_parts.append("t.namespace = %s AND t.value = %s")
        params.extend([namespace, value])
    if kind:
        where_parts.append("r.kind = %s")
        params.append(kind)
    where_parts.append("r.deleted_at IS NULL")  # default: hide soft-deleted
    if has_tag:
        sql = (
            "SELECT r.kind, r.ref_id, r.title, r.deleted_at IS NOT NULL AS dropped "
            "FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t USING(tag_id) "
            f"WHERE {' AND '.join(where_parts)} "
            "ORDER BY r.kind, r.ref_id "
            "LIMIT 500"
        )
    else:
        sql = (
            "SELECT r.kind, r.ref_id, r.title, FALSE AS dropped "
            "FROM refs r "
            f"WHERE {' AND '.join(where_parts)} "
            "ORDER BY r.kind, r.ref_id "
            "LIMIT 500"
        )
    with store.pool.connection() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(sql, tuple(params)).fetchall()
    # Group by kind, preserving the SQL order.
    by_kind: dict[str, list[dict[str, object]]] = {}
    for r in rows:
        row_kind = str(r[0])
        ref_id = int(r[1])
        title = (r[2] or "").split("\n", 1)[0]
        if len(title) > 80:
            title = title[:80].rstrip() + "…"
        by_kind.setdefault(row_kind, []).append(
            {
                "id": ref_id,
                "title": title or "(untitled)",
                "deleted": bool(r[3]),
                "url": _ref_url(row_kind, ref_id),
            }
        )
    if has_tag and kind:
        label = f"{namespace}:{value} on kind={kind}"
    elif has_tag:
        label = f"{namespace}:{value}"
    else:
        label = f"kind={kind}"
    return templates.TemplateResponse(
        request,
        "tags/refs.html.j2",
        {
            "active_tab": "tags" if has_tag else "status",
            "namespace": namespace or "",
            "value": value or "",
            "kind_filter": kind or "",
            "label": label,
            "by_kind": by_kind,
            "total": sum(len(v) for v in by_kind.values()),
        },
    )


#: Per-kind URL shape for the native detail viewer. Falls back to a
#: generic ``/refs/{kind}/{id}`` for kinds that don't have their own
#: tab; the refs router rejects with a friendly 404 if that kind isn't
#: browsable, which is acceptable — the row is still readable in the
#: list above.
_KIND_URLS: dict[str, str] = {
    "paper": "/papers/{id}",
    "todo": "/tasks?focus={id}",
    "job": "/tasks?focus={id}",
}


def _ref_url(kind: str, ref_id: int) -> str:
    template = _KIND_URLS.get(kind, "/refs/{kind}/{id}")
    return template.format(kind=kind, id=ref_id)


@router.post("/delete")
async def delete_tag(
    request: Request,
    namespace: str = Form(...),
    value: str = Form(...),
    q: str = Form(""),
) -> RedirectResponse:
    """Wipe one tag from the entire cluster.

    Single ``DELETE FROM tags`` — the FK on ``ref_tags.tag_id`` is
    ``ON DELETE CASCADE``, so every ref carrying the tag is unhooked
    in the same statement. Protected (closed-vocab) namespaces refuse
    the operation; per-ref removal via the standard ``tag`` verb is
    still available for those.

    ``q`` is the search-box query — preserved across the redirect so
    the operator returns to the same filtered view they just cleaned
    up. Empty string drops the param.
    """
    if namespace in _PROTECTED_NAMESPACES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"namespace {namespace!r} is protected; use the standard "
                f"tag(remove=[...]) verb to drop it from a specific ref"
            ),
        )
    store = get_store(request)
    with store.pool.connection() as conn:  # type: ignore[attr-defined]
        with conn.transaction():
            conn.execute(
                "DELETE FROM tags WHERE namespace = %s AND value = %s",
                (namespace, value),
            )
    target = "/tags"
    if q.strip():
        target = f"/tags?q={q.strip()}"
    return RedirectResponse(url=target, status_code=303)
