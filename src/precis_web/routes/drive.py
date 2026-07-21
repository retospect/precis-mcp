"""Drive tab — browse and place authored artifacts in folders (ADR 0045).

A thin surface over ``kind='folder'`` containment: the folder tree in a
sidebar, a folder's contents (folders first, then artifacts with
per-kind deep links into their readers), breadcrumbs, and the write
actions — create / rename / move / unfile / delete. Every mutation
dispatches a verb through the runtime (``put`` / ``edit`` / ``link
rel='parent'`` / ``delete``) so the placement guards stay
single-sourced in the handlers, mirroring the Tasks tab's move route
(ADR 0027's no-surface-drift rule).

* ``GET  /drive``                 — folder tree + Unfiled artifacts.
* ``GET  /drive/{ref_id}``        — one folder: path, contents, actions.
* ``POST /drive/create``          — new folder (optionally inside one).
* ``POST /drive/{ref_id}/rename`` — rename a folder.
* ``POST /drive/move``            — place / unfile any artifact.
* ``POST /drive/{ref_id}/delete`` — delete (handler refuses non-empty).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from precis_web.deps import get_runtime, get_store, redirect_or_error, templates
from precis_web.routes.drafts import _DOC_TYPES
from precis_web.timefmt import ago as _ago

router = APIRouter(prefix="/drive", tags=["drive"])

log = logging.getLogger(__name__)

#: Per-kind reader deep links. Kinds without a dedicated reader render
#: as plain rows (the handle still tells the operator what to `get`).
_READER_URL = {
    "draft": "/drafts/{ident}",
    "structure": "/structure/{ident}",
    "cad": "/cad/{ident}",
    "datasheet": "/datasheets/{ident}",
    "todo": "/tasks?focus={ref_id}",
}

_KIND_ICON = {
    "folder": "📁",
    "draft": "📝",
    "structure": "⚛️",
    "cad": "🧊",
    "todo": "☑️",
}


def _artifact_kinds(request: Request) -> list[str]:
    """Kinds declared ``role='artifact'`` in this build (minus folder).

    Read from the live hub so a future placeable kind (pcb, …) joins
    the Drive surface by declaration, with no route edit.
    """
    try:
        hub = get_runtime(request).hub
        out = []
        for k in sorted(hub.kinds):
            handler = hub.handler_for(k)
            spec = getattr(handler, "spec", None)
            if spec is not None and getattr(spec, "role", None) == "artifact":
                if k != "folder":
                    out.append(k)
        return out
    except Exception:
        log.debug("drive: hub artifact-kind introspection failed", exc_info=True)
        return ["draft", "structure", "cad", "todo"]


def _doctypes() -> list[dict[str, Any]]:
    """Draft genres for the "+ New" dropdown's ``doctype`` picker, from the
    single-source ``_DOC_TYPES`` list the ``/drafts`` page also renders (so
    adding a genre there lands here too). ``default`` marks the pre-selected
    option, matching ``/drafts/new``'s ``doctype`` form default."""
    return [
        {"value": d["value"], "label": d["label"], "default": d["value"] == "paper"}
        for d in _DOC_TYPES
    ]


def _folder_tree(store: Any) -> list[dict[str, Any]]:
    """Every live folder as a nested tree (name-sorted per level)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT f.ref_id, f.title, f.parent_id,
                   (SELECT count(*) FROM refs c
                     WHERE c.parent_id = f.ref_id AND c.deleted_at IS NULL),
                   (SELECT p.kind FROM refs p WHERE p.ref_id = f.parent_id)
              FROM refs f
             WHERE f.kind = 'folder' AND f.deleted_at IS NULL
             ORDER BY lower(f.title)
            """
        ).fetchall()
    nodes: dict[int, dict[str, Any]] = {
        int(r[0]): {
            "ref_id": int(r[0]),
            "title": r[1] or "",
            "parent_id": int(r[2]) if r[2] is not None and r[4] == "folder" else None,
            "n_children": int(r[3]),
            "children": [],
        }
        for r in rows
    }
    roots: list[dict[str, Any]] = []
    for node in nodes.values():
        pid = node["parent_id"]
        parent = nodes.get(pid) if isinstance(pid, int) else None
        (parent["children"] if parent is not None else roots).append(node)
    return roots


def _flatten_tree(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Depth-first flatten with a ``depth`` key — for indent rendering
    and the move-target dropdown."""
    out: list[dict[str, Any]] = []

    def walk(nodes: list[dict[str, Any]], depth: int) -> None:
        for n in nodes:
            out.append({**n, "depth": depth})
            walk(n["children"], depth + 1)

    walk(roots, 0)
    return out


def _row(r: tuple, *, kinds_with_slug: bool = True) -> dict[str, Any]:
    ref_id, kind, title, slug, updated_at = (
        int(r[0]),
        str(r[1]),
        str(r[2] or ""),
        r[3],
        r[4],
    )
    meta = r[5] if len(r) > 5 and isinstance(r[5], dict) else {}
    ident = slug if slug is not None else str(ref_id)
    url = _READER_URL.get(kind)
    # A cast draft (morning brief / evening meditation) carries its published
    # episode id in meta once narrated — surface the mp3 + compiled PDF as
    # download links so the audio "shows up in the Drive" beside its text.
    episode_id = meta.get("audio_episode_id") if kind == "draft" else None
    is_cast = kind == "draft" and bool(meta.get("cast"))
    return {
        "ref_id": ref_id,
        "kind": kind,
        "icon": _KIND_ICON.get(kind, "▫️"),
        "title": title,
        "ident": ident,
        # link() addresses slug kinds by slug, numeric kinds by int id.
        "handler_id": ident,
        "url": url.format(ident=ident, ref_id=ref_id) if url else None,
        "audio_url": f"/podcast/audio/{episode_id}" if episode_id else None,
        "pdf_url": f"/drafts/{ident}/pdf" if is_cast else None,
        "updated": _ago(updated_at) if updated_at is not None else "",
    }


_CHILD_COLS = """
    r.ref_id, r.kind, r.title,
    (SELECT ri.id_value FROM ref_identifiers ri
      WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key'
      LIMIT 1) AS slug,
    r.updated_at, r.meta
"""


def _children(store: Any, folder_id: int) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_CHILD_COLS}
              FROM refs r
             WHERE r.parent_id = %s AND r.deleted_at IS NULL
             ORDER BY (r.kind != 'folder'), r.kind, lower(r.title)
            """,
            (folder_id,),
        ).fetchall()
    return [_row(r) for r in rows]


def _unfiled(store: Any, artifact_kinds: list[str]) -> list[dict[str, Any]]:
    """Live artifact refs with no parent. Todos are exempt — an
    unfoldered strategic root is normal, not 'unfiled' (ADR 0045 §5)."""
    kinds = [k for k in artifact_kinds if k != "todo"]
    if not kinds:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_CHILD_COLS}
              FROM refs r
             WHERE r.kind = ANY(%s) AND r.parent_id IS NULL
               AND r.deleted_at IS NULL
             ORDER BY r.kind, r.updated_at DESC
            """,
            (kinds,),
        ).fetchall()
    return [_row(r) for r in rows]


def _breadcrumb(store: Any, folder_id: int) -> list[dict[str, Any]]:
    """(ref_id, title) pairs root→here, walking up folder parents."""
    crumbs: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: int | None = folder_id
    while current is not None and current not in seen:
        seen.add(current)
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT kind, title, parent_id FROM refs "
                "WHERE ref_id = %s AND deleted_at IS NULL",
                (current,),
            ).fetchone()
        if row is None or row[0] != "folder":
            break
        crumbs.append({"ref_id": current, "title": row[1] or ""})
        current = row[2]
    crumbs.reverse()
    return crumbs


@router.get("", response_class=HTMLResponse)
async def drive_index(request: Request) -> HTMLResponse:
    store = get_store(request)
    roots = _folder_tree(store)
    flat = _flatten_tree(roots)
    ctx = {
        "active_tab": "drive",
        "folders": flat,
        "current": None,
        "crumbs": [],
        "children": [],
        "unfiled": _unfiled(store, _artifact_kinds(request)),
        "doctypes": _doctypes(),
    }
    return templates.TemplateResponse(request, "drive/index.html.j2", ctx)


@router.get("/{ref_id}", response_class=HTMLResponse)
async def drive_folder(request: Request, ref_id: int) -> HTMLResponse:
    store = get_store(request)
    roots = _folder_tree(store)
    flat = _flatten_tree(roots)
    current = next((f for f in flat if f["ref_id"] == ref_id), None)
    ctx: dict[str, Any]
    if current is None:
        # Render the index with a soft notice rather than a bare 404 —
        # a stale bookmark shouldn't dead-end the operator.
        ctx = {
            "active_tab": "drive",
            "folders": flat,
            "current": None,
            "crumbs": [],
            "children": [],
            "unfiled": _unfiled(store, _artifact_kinds(request)),
            "doctypes": _doctypes(),
            "notice": f"folder #{ref_id} not found (deleted?)",
        }
        return templates.TemplateResponse(request, "drive/index.html.j2", ctx)
    ctx = {
        "active_tab": "drive",
        "folders": flat,
        "current": current,
        "crumbs": _breadcrumb(store, ref_id),
        "children": _children(store, ref_id),
        "unfiled": [],
        "doctypes": _doctypes(),
    }
    return templates.TemplateResponse(request, "drive/index.html.j2", ctx)


#: Starter sources for the "+ New" dropdown (kind → put args builder). Draft
#: has its own richer flow (``/drafts/new``); this covers cad + structure so a
#: fresh artifact lands the operator straight in its editor.
_NEW_STARTERS = {
    "cad": lambda slug: (
        "cad",
        {"id": slug, "text": "part add box:w40d40h10"},
        f"/cad/{slug}",
    ),
    "structure": lambda slug: (
        "structure",
        {
            "id": slug,
            "text": '{"cell":{"a":10,"b":10,"c":10,"pbc":[true,true,true]},"ops":[]}',
        },
        f"/structure/{slug}",
    ),
    # A figure is born with a default empty canvas (no starter source needed);
    # the operator then draws it in the /figure turn loop.
    "figure": lambda slug: ("figure", {"id": slug}, f"/figure/{slug}"),
}


@router.post("/new")
async def create_artifact(
    request: Request,
    kind: str = Form(...),
    title: str = Form(""),
) -> Response:
    """Create a new cad / structure artifact from the Drive "+ New" dropdown.

    Slugifies ``title`` → slug, dispatches the kind's ``put`` with a valid
    *starter* source, and redirects into its editor (where the operator edits
    by prompt). Draft creation is handled by ``/drafts/new``, not here."""
    from precis.utils.slug import slug_from_text

    builder = _NEW_STARTERS.get(kind)
    if builder is None:
        return await redirect_or_error(
            request,
            "put",
            {"kind": kind},  # let the handler raise the canonical BadInput
            redirect="/drive",
            error_title="New artifact",
        )
    slug = slug_from_text(title) or f"{kind}-design"
    put_kind, args, redirect = builder(slug)
    return await redirect_or_error(
        request,
        "put",
        {"kind": put_kind, **args},
        redirect=redirect,
        error_title="New artifact",
    )


@router.post("/create")
async def create_folder(
    request: Request,
    name: str = Form(""),
    parent_id: str = Form(""),
) -> Response:
    """Create a folder via ``put``; nest via ``link`` when a parent is set.

    Two dispatches because the handler's put is create-only (the
    ``parent`` relation is virtual, so it can't ride the D3
    put-shortcut). The insert lands first; a failed nesting leaves a
    top-level folder rather than nothing — visible, recoverable.
    """
    store = get_store(request)
    pid = parent_id.strip()
    redirect = f"/drive/{int(pid)}" if pid else "/drive"
    if not name.strip():
        return await redirect_or_error(
            request,
            "put",
            {"kind": "folder"},  # handler raises the canonical BadInput
            redirect=redirect,
            error_title="Create folder",
        )
    resp = await redirect_or_error(
        request,
        "put",
        {"kind": "folder", "text": name.strip()},
        redirect=redirect,
        error_title="Create folder",
    )
    if pid and resp.status_code < 400:
        # Find the folder we just created (newest with this title) and
        # nest it through the guarded link surface.
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id FROM refs WHERE kind = 'folder' "
                "AND deleted_at IS NULL AND title = %s "
                "ORDER BY ref_id DESC LIMIT 1",
                (name.strip(),),
            ).fetchone()
        if row is not None:
            return await redirect_or_error(
                request,
                "link",
                {
                    "kind": "folder",
                    "id": int(row[0]),
                    "target": f"folder:{int(pid)}",
                    "rel": "parent",
                    "mode": "add",
                },
                redirect=redirect,
                error_title="Nest folder",
            )
    return resp


@router.post("/{ref_id}/rename")
async def rename_folder(
    request: Request, ref_id: int, name: str = Form("")
) -> Response:
    return await redirect_or_error(
        request,
        "edit",
        {"kind": "folder", "id": ref_id, "text": name},
        redirect=f"/drive/{ref_id}",
        error_title="Rename folder",
    )


@router.post("/move")
async def move_artifact(
    request: Request,
    kind: str = Form(...),
    id: str = Form(...),
    target_folder: str = Form(""),
    back: str = Form("/drive"),
) -> Response:
    """Place (or unfile) any artifact via the guarded ``link`` surface."""
    tf = target_folder.strip()
    handler_id: str | int = int(id) if id.isdigit() else id
    if tf:
        args: dict[str, Any] = {
            "kind": kind,
            "id": handler_id,
            "target": f"folder:{int(tf)}",
            "rel": "parent",
            "mode": "add",
        }
    else:
        args = {"kind": kind, "id": handler_id, "rel": "parent", "mode": "remove"}
    return await redirect_or_error(
        request, "link", args, redirect=back, error_title="Move"
    )


@router.post("/{ref_id}/delete")
async def delete_folder(request: Request, ref_id: int) -> Response:
    """Delete via the handler — it refuses while the folder has contents."""
    return await redirect_or_error(
        request,
        "delete",
        {"kind": "folder", "id": ref_id},
        redirect="/drive",
        error_title="Delete folder",
    )
