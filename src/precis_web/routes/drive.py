"""Drive — the unified seek+manage surface (WS1a of
``docs/proposals/web-ui-rationalization.md``; ADR 0045).

Grafts Drive's folder tree + CRUD onto the Items cross-kind search/facet/
presenter engine (``routes/items.py``, kept as the reusable
query/row-building layer, now redirecting its own ``/items`` path here).
One page: the search box + kind/tag/date/state facets, a persistent
folder-tree sidebar (``folder=`` facet, driving the same
``store.recent_refs``/``search_chunks_across_kinds`` the search bar
uses), per-row quick actions (move / delete / tag — the
``ItemPresenter.actions()`` seam, ``item_view.py``), a "show deleted"
state toggle, and the cluster's watch-dir drop-zone info (reused from
``routes/papers_needed.py``). ``state=stub`` rows (WS1b — the folded
``/papers-needed`` queue) also carry the acquisition-provenance flag
group (``ACQUIRE_FLAG_DEFS``, ``routes/flags.py``) alongside the usual
reading-intent flags. Every mutation still dispatches a verb through the
runtime — no direct SQL, no surface drift (ADR 0027).

* ``GET  /drive``                        — the merged list + sidebar.
* ``GET  /drive/tags/suggest``            — tag-filter autocomplete.
* ``POST /drive/new``                     — create a cad/structure/figure
  artifact (draft creation stays on ``/drafts/new``).
* ``POST /drive/create``                  — new folder (optionally nested).
* ``POST /drive/{ref_id}/rename``         — rename a folder.
* ``POST /drive/move``                    — place / unfile any artifact.
* ``POST /drive/{ref_id}/delete``         — delete a folder (refuses
  non-empty).
* ``POST /drive/item/{kind}/{id}/delete`` — per-row delete (any kind).
* ``POST /drive/item/{kind}/{id}/tag``    — per-row tag-add (any kind).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from precis_web.deps import get_runtime, get_store, redirect_or_error, templates
from precis_web.item_view import artifact_kinds, display_title
from precis_web.routes.drafts import _DOC_TYPES
from precis_web.routes.flags import (
    ACQUIRE_FLAG_DEFS,
    FLAG_DEFS,
    _safe_local_redirect,
)
from precis_web.routes.items import (
    _DEFAULT_SOURCE_KINDS,
    _PAGE_SIZE,
    _folder_options,
    _parse_date,
    _recent_rows,
    _run_search,
)
from precis_web.routes.items import tags_suggest as _tags_suggest
from precis_web.routes.papers_needed import _KIND_DROPZONES, _watch_dir_from_plist
from precis_web.timefmt import ago as _ago

router = APIRouter(prefix="/drive", tags=["drive"])

log = logging.getLogger(__name__)

#: Same autocomplete backend as the legacy ``/items/tags/suggest`` — one
#: function, two mounted paths (no logic fork).
router.add_api_route("/tags/suggest", _tags_suggest, methods=["GET"])

#: Per-kind reader deep links. Kinds without a dedicated reader render
#: as plain rows (the handle still tells the operator what to `get`).
_READER_URL = {
    "draft": "/smartdraft/{ident}",
    "structure": "/structure/{ident}",
    "cad": "/cad/{ident}",
    "figure": "/figure/{ident}",
    "mermaid": "/mermaid/{ident}",
    "datasheet": "/datasheets/{ident}",
    "todo": "/tasks?focus={ref_id}",
}

_KIND_ICON = {
    "folder": "📁",
    "draft": "📝",
    "structure": "⚛️",
    "cad": "🧊",
    "todo": "☑️",
    "quest": "🧭",
    "pathway": "🐈",  # a cat on a (reaction) path — gr161575
}

#: The "Work" facet — agenda / work-item kinds, browsed as a flat,
#: paginated list (a third chip row beside "Source" and "Author"). These
#: kinds carry no embedded body chunks, so they surface in the no-query
#: browse view; a *text* query still matches only the chunked Source/
#: Author kinds (harmless — a chunkless kind contributes nothing). ``todo``
#: is declared ``role='artifact'`` (it can live in folders), so it is
#: pulled out of the Author facet below to sit here instead — one home,
#: not two. "Schedules" (``level:recurring`` todos) is not a kind but a
#: tag preset the template links to, riding the existing tag facet.
_WORK_KINDS: tuple[str, ...] = ("quest", "todo")


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
        "title": display_title(title),
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
@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    sort: str = "relevance",
    since: str = "",
    until: str = "",
    k: list[str] = Query(default_factory=list),
    tag: list[str] = Query(default_factory=list),
    state: str = "all",
    paper_chunks: str = "both",
    folder: str = "",
    page: int = 1,
    submitted: str = "",
) -> HTMLResponse:
    """The merged Drive surface: Items' cross-kind search/facet engine
    plus Drive's folder-tree sidebar, per-row quick actions, a "show
    deleted" state, and the watch-dir drop-zone info.

    ``q=`` runs the search; ``k=`` (repeated) narrows the kind set —
    the "Source" chips and the "Author" facet (``role='artifact'``
    kinds); ``tag=`` (repeated) are the tag-filter chips; ``state=stub``
    shows only paper stubs (awaiting fetch), ``state=deleted`` shows
    soft-deleted refs instead of live ones (a lightweight trash view —
    no undelete surface yet, just visibility); ``paper_chunks=with``/
    ``=without`` (the grouped "paper" chip's popover) filters on
    whether a ref has a body chunk (the ingested-vs-not split) — this
    is a **global** browse filter (it reuses ``recent_refs(has_chunks=
    …)``, not scoped to the ``paper`` kind, by design: chunk presence
    is meaningful for any source kind, not just papers); ``folder=`` (a
    folder ``ref_id``) narrows the no-query landing to one folder's
    direct children — the same facet the sidebar tree's links drive;
    ``sort=recency`` orders newest-first; ``since=``/``until=`` bound
    the date window; ``page=`` pages past the ``_PAGE_SIZE`` cap. With
    no ``q`` the landing shows the recent list.

    Kind selection persists in an ``items_kinds`` cookie (unchanged
    name — pre-dates the merge, no reason to churn a cookie key): an
    explicit submit (``submitted=1``) sets it; a fresh visit reads it
    (or defaults to every source kind).
    """
    store = get_store(request)
    q = (q or "").strip()

    if submitted:
        selected_kinds = [x.strip() for x in k if x.strip()]
    else:
        cookie = request.cookies.get("items_kinds", "")
        selected_kinds = [x for x in cookie.split(",") if x] or list(
            _DEFAULT_SOURCE_KINDS
        )
    tags = [t.strip() for t in tag if t.strip()]
    sort = "recency" if (sort or "").strip().lower() == "recency" else "relevance"
    state = (state or "all").strip().lower()
    # ``state=stub`` → only PDF-less papers (the "to get" queue);
    # ``state=deleted`` → soft-deleted refs (the "show deleted" toggle).
    # Both shape only the recent/browse view, not search (a search hit
    # matched a live chunk, so neither filter is meaningful there).
    has_pdf = False if state == "stub" else None
    show_deleted = state == "deleted"
    # ``paper_chunks`` (the grouped "paper" chip's ▾ popover) drives the
    # ingested-vs-not split. It's a **global** browse chunk-filter by
    # design — it reuses ``recent_refs(has_chunks=…)`` unscoped to any
    # one kind, not a paper-only facet, despite riding the "paper" chip.
    pc = (paper_chunks or "both").strip().lower()
    has_chunks = True if pc == "with" else False if pc == "without" else None
    since_dt = _parse_date(since)
    until_dt = _parse_date(until)
    folder_raw = (folder or "").strip()
    folder_id = int(folder_raw) if folder_raw.isdigit() else None
    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE

    runtime = get_runtime(request)
    hub = getattr(runtime, "hub", None)
    artifact_kind_defs = artifact_kinds(hub)
    # Third chip row: keep the Work kinds out of the Author facet so a
    # role='artifact' work kind (``todo``) lists once, under "Work".
    work_kind_defs = list(_WORK_KINDS)
    artifact_kind_defs = [k for k in artifact_kind_defs if k not in _WORK_KINDS]

    rows: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    has_next = False
    result_total: int | None = None
    if q:
        embedder = getattr(hub, "embedder", None)
        rows, has_next = await asyncio.to_thread(
            _run_search,
            store,
            embedder,
            kinds=selected_kinds,
            q=q,
            sort=sort,
            since=since_dt,
            until=until_dt,
            tags=tags,
            offset=offset,
        )
        # Total-match count for the "showing N of ~K" header — a lexical
        # approximation (the fused semantic+lexical ranking that actually
        # populates ``rows`` has no cheap exact total), but it's the
        # difference between finding 5 of 50 and 5 of 50,000.
        result_total = await asyncio.to_thread(
            store.count_refs_matching_lexical,
            kinds=selected_kinds,
            q=q,
            tags=tags,
            since=since_dt,
            until=until_dt,
        )
        # Floor to what's actually on the page: the fused ranking can surface
        # semantic-only hits the lexical count misses, so without this the
        # header could read the absurd "showing 30 of ~5".
        result_total = max(result_total, offset + len(rows))
    else:
        recent, has_next = await asyncio.to_thread(
            _recent_rows,
            store,
            selected_kinds,
            tags,
            has_pdf,
            folder_id,
            offset,
            has_chunks=has_chunks,
            deleted=show_deleted,
        )

    # Where a flag toggle / row action bounces back to — this exact search.
    return_to = request.url.path + (
        f"?{request.url.query}" if request.url.query else ""
    )

    # Pager links preserve every filter, only ``page`` changes.
    _pager_params: list[tuple[str, str]] = [("submitted", "1")]
    if q:
        _pager_params.append(("q", q))
    _pager_params.append(("sort", sort))
    if since:
        _pager_params.append(("since", since))
    if until:
        _pager_params.append(("until", until))
    if state != "all":
        _pager_params.append(("state", state))
    if pc != "both":
        _pager_params.append(("paper_chunks", pc))
    if folder_raw:
        _pager_params.append(("folder", folder_raw))
    for kk in selected_kinds:
        _pager_params.append(("k", kk))
    for t in tags:
        _pager_params.append(("tag", t))

    def _page_url(n: int) -> str:
        return "/drive?" + urlencode([*_pager_params, ("page", n)])

    # The folder-tree sidebar + (when one is selected) its breadcrumb.
    roots = _folder_tree(store)
    flat = _flatten_tree(roots)
    current = (
        next((f for f in flat if f["ref_id"] == folder_id), None)
        if folder_id is not None
        else None
    )
    crumbs = _breadcrumb(store, folder_id) if current and folder_id else []
    # A stale bookmark to a deleted folder shouldn't dead-end the operator
    # — fall back to the unfiltered landing with a soft notice.
    notice = (
        f"folder #{folder_id} not found (deleted?)"
        if folder_raw and current is None
        else None
    )

    # Watch-dir drop-zone info (papers_needed.py:15-19's second gap) — the
    # cluster's manual-ingest paths, when the web host can read the plist.
    watch_dir = _watch_dir_from_plist()
    dropzones: list[dict[str, str]] = []
    if watch_dir:
        for label, sub, description in _KIND_DROPZONES:
            dropzones.append(
                {
                    "label": label,
                    "path": str(Path(watch_dir) / sub),
                    "description": description,
                }
            )

    resp = templates.TemplateResponse(
        request,
        "drive/index.html.j2",
        {
            "active_tab": "drive",
            "q": q,
            "kind_defs": list(_DEFAULT_SOURCE_KINDS),
            "artifact_kind_defs": artifact_kind_defs,
            "work_kind_defs": work_kind_defs,
            "selected_kinds": selected_kinds,
            "tags": tags,
            "sort": sort,
            "since": since,
            "until": until,
            "state": state,
            "paper_chunks": pc,
            "folder": folder_raw,
            "folder_options": await asyncio.to_thread(_folder_options, store),
            "folders": flat,
            "current": current,
            "crumbs": crumbs,
            "notice": notice,
            "rows": rows,
            "recent": recent,
            "result_total": result_total,
            "flag_defs": FLAG_DEFS,
            "acquire_flag_defs": ACQUIRE_FLAG_DEFS,
            "return_to": return_to,
            "page": page,
            "has_next": has_next,
            "prev_url": _page_url(page - 1) if page > 1 else None,
            "next_url": _page_url(page + 1) if has_next else None,
            "doctypes": _doctypes(),
            "watch_dir": watch_dir,
            "dropzones": dropzones,
        },
    )
    if submitted:
        # Remember the kind selection for the next visit (90 days).
        resp.set_cookie("items_kinds", ",".join(selected_kinds), max_age=90 * 24 * 3600)
    return resp


#: Starter sources for the "+ New" dropdown (kind → put args builder). Draft
#: has its own richer flow (``/drafts/new``); this covers cad / structure /
#: figure / mermaid so a fresh artifact lands the operator straight in its
#: editor.
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
    # Mermaid is likewise born empty (id_required=False, text optional); the
    # operator writes the diagram source in the /mermaid turn loop.
    "mermaid": lambda slug: ("mermaid", {"id": slug}, f"/mermaid/{slug}"),
}


@router.post("/new")
async def create_artifact(
    request: Request,
    kind: str = Form(...),
    title: str = Form(""),
) -> Response:
    """Create a new cad / structure / figure / mermaid artifact from the
    Drive "+ New" dropdown.

    Slugifies ``title`` → slug, dispatches the kind's ``put`` with a valid
    *starter* source, and redirects into its editor (where the operator edits
    by prompt). Draft creation is handled by ``/drafts/new``, not here. A kind
    with no starter falls through to the handler's canonical BadInput rather
    than silently landing on ``/drive``."""
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
    # The merged Drive page navigates folders via the ``folder=`` query
    # facet, not a path segment (WS1a) — same destination the sidebar
    # tree's own links use.
    redirect = f"/drive?folder={int(pid)}" if pid else "/drive"
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
        redirect=f"/drive?folder={ref_id}",
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
    redirect = _safe_local_redirect(back, "/drive")
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
        request, "link", args, redirect=redirect, error_title="Move"
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


#: Per-row quick actions (``ItemPresenter.actions()``, WS1a) — generic
#: over every kind, so these two routes take ``kind`` on the path rather
#: than living per-kind. Mirrors ``move_artifact`` above: any id that's
#: all digits is treated as a numeric ref_id, else a slug (drafts/cad/…
#: address by slug).


@router.post("/item/{kind}/{ref_id}/delete")
async def delete_item(
    request: Request,
    kind: str,
    ref_id: str,
    back: str = Form("/drive"),
) -> Response:
    """Delete any ref via its own kind's ``delete`` verb. A kind with no
    delete support surfaces the rejection through the same error page
    every other write route uses — a clean stop, not a crash."""
    redirect = _safe_local_redirect(back, "/drive")
    handler_id: str | int = int(ref_id) if ref_id.isdigit() else ref_id
    return await redirect_or_error(
        request,
        "delete",
        {"kind": kind, "id": handler_id},
        redirect=redirect,
        error_title="Delete",
    )


@router.post("/item/{kind}/{ref_id}/tag")
async def tag_item(
    request: Request,
    kind: str,
    ref_id: str,
    value: str = Form(""),
    back: str = Form("/drive"),
) -> Response:
    """Add one tag to any ref via the ``tag`` verb (closed axes still
    round-trip their ``NAMESPACE:value`` string through the handler's own
    vocabulary guard — this route doesn't parse it). A blank box is a
    no-op redirect rather than a validation error."""
    redirect = _safe_local_redirect(back, "/drive")
    value = value.strip()
    if not value:
        return RedirectResponse(url=redirect, status_code=303)
    handler_id: str | int = int(ref_id) if ref_id.isdigit() else ref_id
    return await redirect_or_error(
        request,
        "tag",
        {"kind": kind, "id": handler_id, "add": [value]},
        redirect=redirect,
        error_title="Tag",
    )
