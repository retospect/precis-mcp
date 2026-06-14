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
* ``POST /tasks/{id}/move``             — reparent (link rel='parent')
* ``POST /tasks/{id}/delete``           — soft-delete

The move route is a thin shell over the reserved virtual relation
``link(kind='todo', rel='parent')`` so the cycle / depth / owner
guards stay single-sourced in the handler — the web layer never
touches ``parent_id`` directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
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


def _child_jobs(store: Any, todo_ids: list[int]) -> list[dict[str, Any]]:
    """Return ``kind='job'`` children of the given todos.

    Jobs are where processing actually happens — a worker claims a
    job ref and writes ``meta.lease_until`` for the run window. We
    surface them under their parent todo so the lock/lease badges have
    a node to attach to. Degrades to ``[]`` cleanly when the query
    returns nothing (and under the test fake's empty cursor).
    """
    if not todo_ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, parent_id, title, meta->>'lease_until' "
            "FROM refs WHERE kind = 'job' AND deleted_at IS NULL "
            "AND parent_id = ANY(%s)",
            (todo_ids,),
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "parent_id": int(r[1]) if r[1] is not None else None,
            "title": r[2],
            "lease_until": r[3],
        }
        for r in rows
    ]


def _lease_active(lease_until: str | None) -> bool:
    """True when ``lease_until`` parses and lies in the future."""
    if not lease_until:
        return False
    try:
        ts = datetime.fromisoformat(lease_until)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts > datetime.now(UTC)


def _build_rows(store: Any) -> list[dict[str, Any]]:
    """Flatten the todo tree (with child jobs) into DFS-ordered rows.

    Each row carries ``id, kind, title, status, level, depth, done,
    total`` plus processing signals ``locked`` (a live ``pg_locks``
    row lock) and ``lease_until`` / ``lease_active`` (the durable
    marker a worker writes). Roots are ``parent_id IS NULL``; orphans
    (parent missing) surface as roots so nothing silently disappears.
    """
    todos = store.list_refs(kind="todo", limit=5000)
    by_id = {r.id: r for r in todos}
    todo_ids = [r.id for r in todos]
    jobs = _child_jobs(store, todo_ids)

    all_ids = todo_ids + [j["id"] for j in jobs]
    tags = _load_tags(store, all_ids)
    locked = store.locked_ref_ids(all_ids)

    # Normalise todos + jobs into a single node dict so the walk is
    # kind-agnostic.
    nodes: dict[int, dict[str, Any]] = {}
    for r in todos:
        nodes[r.id] = {
            "id": r.id,
            "kind": "todo",
            "title": r.title,
            "parent_id": r.parent_id if r.parent_id in by_id else None,
            "lease_until": None,
        }
    for j in jobs:
        # A job whose parent todo vanished is dropped (no orphan jobs).
        if j["parent_id"] not in by_id:
            continue
        nodes[j["id"]] = {
            "id": j["id"],
            "kind": "job",
            "title": j["title"],
            "parent_id": j["parent_id"],
            "lease_until": j["lease_until"],
        }

    children: dict[int | None, list[dict[str, Any]]] = {}
    for n in nodes.values():
        children.setdefault(n["parent_id"], []).append(n)
    for kids in children.values():
        kids.sort(key=lambda n: n["id"])

    rows: list[dict[str, Any]] = []

    def walk(node: dict[str, Any], depth: int) -> None:
        kids = children.get(node["id"], [])
        done = sum(1 for k in kids if tags[k["id"]]["status"] in _CLOSED)
        lease_until = node["lease_until"]
        rows.append(
            {
                "id": node["id"],
                "kind": node["kind"],
                "title": node["title"],
                "status": tags[node["id"]]["status"],
                "level": tags[node["id"]]["level"],
                "depth": depth,
                "done": done,
                "total": len(kids),
                "is_leaf": not kids,
                "locked": node["id"] in locked,
                "lease_until": lease_until,
                "lease_active": _lease_active(lease_until),
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


@router.post("/{ref_id}/move")
async def move_task(
    request: Request,
    ref_id: int,
    new_parent_id: str = Form(""),
) -> RedirectResponse:
    """Reparent a todo via the reserved ``link(rel='parent')`` surface.

    An empty / blank ``new_parent_id`` detaches the todo to a root
    (``mode='remove'``); otherwise the todo moves under that parent
    (``mode='add'``). All tree guards (cycle / depth / owner) fire in
    the handler — a rejected move returns the handler's BadInput.
    """
    npid = new_parent_id.strip()
    if npid:
        dispatch(
            request,
            "link",
            {
                "kind": "todo",
                "id": ref_id,
                "target": f"todo:{int(npid)}",
                "rel": "parent",
                "mode": "add",
            },
        )
    else:
        dispatch(
            request,
            "link",
            {"kind": "todo", "id": ref_id, "rel": "parent", "mode": "remove"},
        )
    return RedirectResponse(url="/tasks", status_code=303)


@router.post("/{ref_id}/delete")
async def delete_task(request: Request, ref_id: int) -> RedirectResponse:
    """Soft-delete a todo (children re-parent to NULL via FK)."""
    dispatch(request, "delete", {"kind": "todo", "id": ref_id})
    return RedirectResponse(url="/tasks", status_code=303)
