"""Tasks tab — the hierarchical todo tree.

Reads assemble a structured tree directly off the DB (the
"work-off-the-db" principle); writes route through the in-process
runtime (``dispatch``) so the todo handler's level-gradient guard,
depth check, and STATUS vocabulary stay single-sourced.

Routes:

* ``GET  /tasks``                       — dashboard (tree + doable)
* ``POST /tasks/roots``                 — create a strategic root
* ``POST /tasks/{parent_id}/children``  — create a child leaf
* ``POST /tasks/{id}/status``           — set STATUS
* ``POST /tasks/{id}/delete``           — soft-delete
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from precis_web.deps import dispatch, get_store, templates

router = APIRouter(prefix="/tasks", tags=["tasks"])

#: STATUS values the UI offers as a dropdown. Mirrors the todo
#: handler's closed vocabulary (``precis.store.types._CLOSED_VOCAB``).
STATUS_CHOICES: tuple[str, ...] = (
    "open",
    "doing",
    "blocked",
    "paused",
    "done",
    "won't-do",
)

_CLOSED = {"done", "won't-do"}


def _load_tags(store: Any, ref_ids: list[int]) -> dict[int, dict[str, str]]:
    """Bulk-fetch STATUS + ``level:`` for each todo in one query.

    Returns ``{ref_id: {'status': ..., 'level': ...}}`` with sensible
    defaults (``status='open'``, ``level=''``).
    """
    out: dict[int, dict[str, str]] = {
        rid: {"status": "open", "level": ""} for rid in ref_ids
    }
    if not ref_ids:
        return out
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT rt.ref_id, t.namespace, t.value
              FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = ANY(%s)
               AND (t.namespace = 'STATUS'
                    OR (t.namespace = 'OPEN' AND t.value LIKE 'level:%%'))
            """,
            (ref_ids,),
        ).fetchall()
    for ref_id, namespace, value in rows:
        rid = int(ref_id)
        if namespace == "STATUS":
            out[rid]["status"] = str(value)
        elif str(value).startswith("level:"):
            out[rid]["level"] = str(value).split(":", 1)[1]
    return out


def _build_rows(store: Any) -> list[dict[str, Any]]:
    """Flatten the todo tree into DFS-ordered rows for the template.

    Each row carries ``id, title, status, level, depth, done, total``
    where ``done/total`` count direct children (the branch progress
    readout). Roots are ``parent_id IS NULL``; orphans (parent missing)
    are surfaced as roots so nothing silently disappears.
    """
    refs = store.list_refs(kind="todo", limit=5000)
    by_id = {r.id: r for r in refs}
    tags = _load_tags(store, [r.id for r in refs])

    children: dict[int | None, list[Any]] = {}
    for r in refs:
        # Treat a parent that isn't a live todo as a root (orphan).
        parent = r.parent_id if r.parent_id in by_id else None
        children.setdefault(parent, []).append(r)
    for kids in children.values():
        kids.sort(key=lambda r: r.id)

    rows: list[dict[str, Any]] = []

    def walk(node: Any, depth: int) -> None:
        kids = children.get(node.id, [])
        done = sum(1 for k in kids if tags[k.id]["status"] in _CLOSED)
        rows.append(
            {
                "id": node.id,
                "title": node.title,
                "status": tags[node.id]["status"],
                "level": tags[node.id]["level"],
                "depth": depth,
                "done": done,
                "total": len(kids),
                "is_leaf": not kids,
            }
        )
        for k in kids:
            walk(k, depth + 1)

    for root in children.get(None, []):
        walk(root, 0)
    return rows


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Strategic tree + doable queue."""
    store = get_store(request)
    rows = _build_rows(store)
    doable_body, _ = dispatch(
        request, "search", {"kind": "todo", "view": "doable", "page_size": 20}
    )
    return templates.TemplateResponse(
        request,
        "tasks/dashboard.html.j2",
        {
            "active_tab": "tasks",
            "rows": rows,
            "doable_body": doable_body,
            "status_choices": STATUS_CHOICES,
        },
    )


@router.post("/roots")
async def create_root(
    request: Request,
    text: str = Form(...),
    level: str = Form("strategic"),
) -> RedirectResponse:
    """Create a top-level (strategic) root."""
    tags = [f"level:{level}"] if level else None
    dispatch(request, "put", {"kind": "todo", "text": text, "tags": tags})
    return RedirectResponse(url="/tasks", status_code=303)


@router.post("/{parent_id}/children")
async def create_child(
    request: Request,
    parent_id: int,
    text: str = Form(...),
    level: str = Form("subtask"),
) -> RedirectResponse:
    """Create a child under ``parent_id``."""
    tags = [f"level:{level}"] if level else None
    dispatch(
        request,
        "put",
        {"kind": "todo", "text": text, "parent_id": parent_id, "tags": tags},
    )
    return RedirectResponse(url="/tasks", status_code=303)


@router.post("/{ref_id}/status")
async def set_status(
    request: Request,
    ref_id: int,
    status: str = Form(...),
) -> RedirectResponse:
    """Set a todo's STATUS via the tag verb (closed-prefix replace)."""
    dispatch(
        request,
        "tag",
        {"kind": "todo", "id": ref_id, "add": [f"STATUS:{status}"]},
    )
    return RedirectResponse(url="/tasks", status_code=303)


@router.post("/{ref_id}/delete")
async def delete_task(request: Request, ref_id: int) -> RedirectResponse:
    """Soft-delete a todo (children re-parent to NULL via FK)."""
    dispatch(request, "delete", {"kind": "todo", "id": ref_id})
    return RedirectResponse(url="/tasks", status_code=303)
