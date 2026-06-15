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

from fastapi import APIRouter, Form, Request, Response
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


def _redirect_or_error(
    request: Request,
    verb: str,
    args: dict[str, Any],
    *,
    redirect: str = "/tasks",
) -> Response:
    """Dispatch one verb; redirect on success, render the error on failure.

    The write routes used to discard the handler result and redirect
    unconditionally, so a rejected mutation (an invalid tag, a guard
    veto) failed silently — the operator typed something, hit submit,
    and the page reloaded unchanged with no explanation. Surfacing the
    handler's own message (its ``next=`` recovery hint included) makes
    these self-diagnosing.
    """
    body, is_error = dispatch(request, verb, args)
    if is_error:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Request error", "detail": body, "status": 400},
            status_code=400,
        )
    return RedirectResponse(url=redirect, status_code=303)


def _split_tags(raw: str) -> list[str]:
    """Split a comma/space separated tag string into a clean list."""
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    return [p for p in parts if p]


def _load_freeform_tags(store: Any, ref_ids: list[int]) -> dict[int, list[str]]:
    """Return removable free tags per ref (canonical strings).

    Excludes ``STATUS:`` (dedicated dropdown) and ``level:`` (dedicated
    badge) since those have their own controls. ``OPEN`` namespace tags
    store the full ``key:value`` in ``value``; closed namespaces render
    as ``NAMESPACE:value``.
    """
    out: dict[int, list[str]] = {rid: [] for rid in ref_ids}
    if not ref_ids:
        return out
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT rt.ref_id, t.namespace, t.value FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id WHERE rt.ref_id = ANY(%s)",
            (ref_ids,),
        ).fetchall()
    for ref_id, namespace, value in rows:
        rid = int(ref_id)
        tag_str = str(value) if namespace == "OPEN" else f"{namespace}:{value}"
        if tag_str.startswith(("STATUS:", "level:")):
            continue
        out[rid].append(tag_str)
    for tags in out.values():
        tags.sort()
    return out


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
    freeform = _load_freeform_tags(store, todo_ids)
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
                "tags": freeform.get(node["id"], []) if node["kind"] == "todo" else [],
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
) -> Response:
    """Create a top-level (strategic) root."""
    tags = [f"level:{level}"] if level else None
    return _redirect_or_error(
        request, "put", {"kind": "todo", "text": text, "tags": tags}
    )


@router.post("/{parent_id}/children")
async def create_child(
    request: Request,
    parent_id: int,
    text: str = Form(...),
    level: str = Form("subtask"),
) -> Response:
    """Create a child under ``parent_id``."""
    tags = [f"level:{level}"] if level else None
    return _redirect_or_error(
        request,
        "put",
        {"kind": "todo", "text": text, "parent_id": parent_id, "tags": tags},
    )


@router.post("/{ref_id}/status")
async def set_status(
    request: Request,
    ref_id: int,
    status: str = Form(...),
) -> Response:
    """Set a todo's STATUS via the tag verb (closed-prefix replace)."""
    return _redirect_or_error(
        request,
        "tag",
        {"kind": "todo", "id": ref_id, "add": [f"STATUS:{status}"]},
    )


@router.post("/{ref_id}/move")
async def move_task(
    request: Request,
    ref_id: int,
    new_parent_id: str = Form(""),
) -> Response:
    """Reparent a todo via the reserved ``link(rel='parent')`` surface.

    An empty / blank ``new_parent_id`` detaches the todo to a root
    (``mode='remove'``); otherwise the todo moves under that parent
    (``mode='add'``). All tree guards (cycle / depth / owner) fire in
    the handler — a rejected move returns the handler's BadInput.
    """
    npid = new_parent_id.strip()
    if npid:
        return _redirect_or_error(
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
    return _redirect_or_error(
        request,
        "link",
        {"kind": "todo", "id": ref_id, "rel": "parent", "mode": "remove"},
    )


@router.post("/{ref_id}/edit")
async def edit_text(
    request: Request,
    ref_id: int,
    text: str = Form(""),
) -> Response:
    """Rewrite a todo's text in place via the ``edit`` verb.

    Same id, parent, links, and tags survive; the old body is audited
    in ``ref_events``. Multiline text is preserved verbatim. An empty /
    whitespace ``text`` is a no-op. Owner-only on strategic / tactical
    nodes — the web process runs as owner, so the guard passes here.
    """
    if not text.strip():
        return RedirectResponse(url="/tasks", status_code=303)
    return _redirect_or_error(
        request,
        "edit",
        {"kind": "todo", "id": ref_id, "mode": "replace", "text": text.strip()},
    )


@router.post("/{ref_id}/tags")
async def edit_tags(
    request: Request,
    ref_id: int,
    add: str = Form(""),
    remove: str = Form(""),
) -> Response:
    """Add and/or remove free tags on a todo via the ``tag`` verb.

    ``add`` is a comma/space separated tag string the operator typed;
    ``remove`` is a single tag (from a chip's remove button). Both flow
    through the handler so tag-vocabulary validation stays single-
    sourced — an invalid tag now renders the handler's BadInput inline
    instead of silently redirecting (the operator was typing tags that
    failed validation with no feedback).
    """
    add_list = _split_tags(add)
    remove_list = _split_tags(remove)
    args: dict[str, Any] = {"kind": "todo", "id": ref_id}
    if add_list:
        args["add"] = add_list
    if remove_list:
        args["remove"] = remove_list
    if not add_list and not remove_list:
        return RedirectResponse(url="/tasks", status_code=303)
    return _redirect_or_error(request, "tag", args)


@router.get("/{ref_id}/history", response_class=HTMLResponse)
async def history(request: Request, ref_id: int) -> HTMLResponse:
    """Lazy (htmx) history fragment for one todo.

    Two strands the tree itself doesn't surface inline:

    * **Attempts** — every child ``kind='job'`` (one execution attempt
      each), newest first, with its STATUS so succeeded/failed/running
      runs are all legible in one place.
    * **Event log** — ``ref_events`` for this todo (e.g. ``status:done``
      with its timestamp + source).

    Rendered as a bare fragment so htmx can swap it into the row's
    expander without a full page reload.
    """
    store = get_store(request)
    jobs = _child_jobs(store, [ref_id])
    job_status = _load_tags(store, [j["id"] for j in jobs])
    attempts = [
        {
            "id": j["id"],
            "title": j["title"],
            "status": job_status.get(j["id"], {}).get("status", "open"),
        }
        for j in jobs
    ]
    attempts.sort(key=lambda a: a["id"], reverse=True)

    events: list[dict[str, Any]] = []
    for e in store.events_for(ref_id, limit=50):
        events.append(
            {
                "ts": e.ts.strftime("%Y-%m-%d %H:%M") if e.ts else "",
                "event": e.event,
                "source": e.source,
            }
        )
    return templates.TemplateResponse(
        request,
        "tasks/_history.html.j2",
        {"ref_id": ref_id, "attempts": attempts, "events": events},
    )


@router.post("/{ref_id}/delete")
async def delete_task(request: Request, ref_id: int) -> RedirectResponse:
    """Soft-delete a todo (children re-parent to NULL via FK)."""
    dispatch(request, "delete", {"kind": "todo", "id": ref_id})
    return RedirectResponse(url="/tasks", status_code=303)
