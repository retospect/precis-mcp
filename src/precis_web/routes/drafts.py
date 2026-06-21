"""Drafts tab — a read-first viewer/editor for the ``draft`` kind (ADR 0033).

Tier-A surface (the document is *steered*, not hand-typed):

* ``GET /drafts`` — list drafts.
* ``GET /drafts/{ident}`` — the reader: TOC-left + chunks in DFS reading
  order, each rendered as **raw source** through ``linkify_refs`` (so
  ``¶`` / ``§`` / ``[[…]]`` / ``kind:ref`` references become hover-preview
  + click-navigate anchors), anchored ``#c-<handle>``. A links/backlinks
  panel and a per-chunk change-request box complete the steering loop.
* ``POST /drafts/{ident}/request`` — file a change request: a ``todo``
  anchored at a chunk handle, parented on the draft's project (so it
  flows into the todo tree → dispatch → jobs).
* ``GET /c/{handle}`` — resolve an opaque chunk handle → redirect into
  the reader at ``#c-<handle>`` (the target of every ``¶`` anchor).
* ``GET /preview/chunk/{handle}`` — the hover-popover fragment for a
  ``¶`` chunk anchor (peer of ``/preview/{kind}/{id}``).

Rendering is **raw source** (Tier A); the resolution pass that computes
§-numbers / resolves cross-refs / KaTeX math is the export engine (Tier
B), shared across HTML/LaTeX/Word targets.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from precis_web.deps import get_store, redirect_or_error, templates

router = APIRouter(tags=["drafts"])

log = logging.getLogger(__name__)


def _draft_ref(store: Any, ident: str) -> Any:
    """Resolve a draft by slug or numeric ref_id (``get_ref`` handles
    both). Returns the live ``Ref`` or ``None``."""
    key: int | str = int(ident) if ident.lstrip("#").isdigit() else ident
    if isinstance(key, str) and key.startswith("#"):
        key = int(key[1:])
    return store.get_ref(kind="draft", id=key)


def _project_id(store: Any, ref_id: int) -> int | None:
    """The draft's owning project todo (the ``draft-of`` target)."""
    for link in store.links_for(ref_id, direction="out", relation="draft-of"):
        return int(link.dst_ref_id)
    return None


def _link_rows(store: Any, ref_id: int) -> dict[str, list[dict[str, Any]]]:
    """Outgoing (this draft → X) and incoming (X → this draft) edges for
    the links panel, with the other endpoint's kind/title resolved."""
    out: list[dict[str, Any]] = []
    inc: list[dict[str, Any]] = []
    links = store.links_for(ref_id, direction="both")
    other_ids = {
        (link.dst_ref_id if link.src_ref_id == ref_id else link.src_ref_id)
        for link in links
    }
    refs = store.fetch_refs_by_ids(list(other_ids)) if other_ids else {}
    for link in links:
        outgoing = link.src_ref_id == ref_id
        other = refs.get(link.dst_ref_id if outgoing else link.src_ref_id)
        row = {
            "relation": link.relation,
            "kind": getattr(other, "kind", "?"),
            "ref_id": getattr(other, "id", None),
            "title": (getattr(other, "title", "") or "").split("\n", 1)[0][:80],
        }
        (out if outgoing else inc).append(row)
    return {"out": out, "in": inc}


@router.get("/drafts", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    store = get_store(request)
    refs = store.list_refs(kind="draft", limit=200)
    drafts = [
        {
            "ident": r.slug or r.id,
            "title": (r.title or r.slug or "untitled").split("\n", 1)[0],
            "slug": r.slug,
        }
        for r in refs
    ]
    return templates.TemplateResponse(
        request,
        "drafts/index.html.j2",
        {"active_tab": "drafts", "drafts": drafts},
    )


@router.get("/drafts/{ident}", response_class=HTMLResponse)
async def reader(request: Request, ident: str) -> Response:
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "active_tab": "drafts",
                "title": "Draft not found",
                "status": 404,
                "detail": f"no draft {ident!r}",
            },
            status_code=404,
        )
    chunks = [
        {
            "handle": c.handle,
            "chunk_kind": c.chunk_kind,
            "text": c.text,
            "depth": c.depth,
        }
        for c in store.reading_order(ref.id)
    ]
    toc = [
        {
            "handle": e.handle,
            "depth": e.depth,
            "title": e.title,
            "gist": e.gist or (", ".join(e.keywords[:6]) if e.keywords else ""),
        }
        for e in store.draft_toc(ref.id)
    ]
    return templates.TemplateResponse(
        request,
        "drafts/detail.html.j2",
        {
            "active_tab": "drafts",
            "ref": {
                "ident": ref.slug or ref.id,
                "slug": ref.slug,
                "title": ref.title,
                "id": ref.id,
            },
            "chunks": chunks,
            "toc": toc,
            "project_id": _project_id(store, ref.id),
            "links": _link_rows(store, ref.id),
        },
    )


@router.post("/drafts/{ident}/request")
async def request_change(
    request: Request,
    ident: str,
    handle: str = Form(...),
    text: str = Form(...),
) -> Response:
    """File a change request anchored at a chunk: a ``todo`` parented on
    the draft's project, carrying ``meta.anchor='¶<handle>'``. Flows
    through the normal todo tree → dispatch → jobs."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    back = f"/drafts/{ident}#c-{handle}"
    if ref is None or not text.strip():
        return RedirectResponse(url=back, status_code=303)
    project = _project_id(store, ref.id)
    args: dict[str, Any] = {
        "kind": "todo",
        "text": text.strip(),
        "meta": {"anchor": f"¶{handle}"},
    }
    if project is not None:
        args["parent_id"] = project
    return await redirect_or_error(
        request, "put", args, redirect=back, error_title="Change request error"
    )


@router.get("/c/{handle}")
async def goto_chunk(request: Request, handle: str) -> Response:
    """Resolve an opaque ``¶`` handle → redirect into its draft reader,
    anchored at the chunk. The click target of every ``¶`` anchor."""
    store = get_store(request)
    chunk = store.get_draft_chunk(handle)
    if chunk is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "active_tab": "drafts",
                "title": "Chunk not found",
                "status": 404,
                "detail": f"no chunk ¶{handle}",
            },
            status_code=404,
        )
    ref = store.get_ref(kind="draft", id=int(chunk.ref_id))
    ident = ref.slug if ref and ref.slug else chunk.ref_id
    return RedirectResponse(url=f"/drafts/{ident}#c-{handle}", status_code=303)


@router.get("/preview/chunk/{handle}", response_class=HTMLResponse)
async def preview_chunk(request: Request, handle: str) -> HTMLResponse:
    """Hover-popover fragment for a ``¶`` chunk anchor — peer of the
    ``/preview/{kind}/{id}`` route, reusing the same popover template."""
    store = get_store(request)
    chunk = store.get_draft_chunk(handle)
    if chunk is None:
        return templates.TemplateResponse(
            request,
            "preview/popover.html.j2",
            {"kind": "chunk", "label": f"¶{handle}", "missing": True},
        )
    flat = " ".join((chunk.text or "").split())
    title = flat[:100] + ("…" if len(flat) > 100 else "")
    return templates.TemplateResponse(
        request,
        "preview/popover.html.j2",
        {
            "kind": chunk.chunk_kind,
            "label": f"¶{handle}",
            "ref_id": handle,
            "title": title or "(empty)",
            "body_preview": "",
            "deleted": False,
            "missing": False,
        },
    )
