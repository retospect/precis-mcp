"""Smartdraft tab — the fisheye-rail reader (design:
``docs/proposals/draft-reader-fisheye-rail.md``).

A **parallel** surface to `/drafts`: same draft data, a different lens. Three
panes — left (fisheye TOC nav), middle (the focus + its neighbourhood), right
(collaboration: the working set + a request box). It reuses the shipped
`/drafts/{ident}/marks` + `/request-ws` endpoints, and touches nothing in the
working reader, so it ships dark by construction.

Slice 1 is **server-rendered**: the focus is a query param (`?focus=dc<id>`), a
TOC click reloads at that focus. The relevance overlay (fisheye-collapse of quiet
runs) toggles via `?relevance=0`. Smoothing this to a client-side no-reload fisheye
+ hover-expand is a later slice.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from precis.utils.embed_query import embed_query
from precis_web import draft_eyes, smartdraft
from precis_web.deps import get_runtime, get_store, templates
from precis_web.routes.drafts import _draft_ref

router = APIRouter(tags=["smartdraft"])


@router.get("/smartdraft", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    """List drafts, linking each into the smartdraft reader."""
    store = get_store(request)
    refs = store.list_refs(kind="draft", order_by="viewed_desc", limit=200)
    drafts = [
        {
            "id": r.id,
            "slug": r.slug,
            "title": (r.title or r.slug or "untitled").split("\n", 1)[0],
        }
        for r in refs
    ]
    return templates.TemplateResponse(
        request,
        "smartdraft/index.html.j2",
        {"active_tab": "smartdraft", "drafts": drafts},
    )


#: Search signal letters, in display order.
_SIGNALS = "vkts"


@router.get("/smartdraft/{ident}", response_class=HTMLResponse)
async def reader(
    request: Request,
    ident: str,
    focus: str = "",
    relevance: str = "1",
    q: str = "",
    sig: str = _SIGNALS,
    sview: str = "list",
) -> Response:
    """The three-pane fisheye reader. ``q`` runs multi-signal search (RRF); ``sig``
    is the active-signal letter set (e.g. ``vkts``); ``sview`` is ``list``/``toc``."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "active_tab": "smartdraft",
                "title": "Draft not found",
                "status": 404,
                "detail": f"no draft {ident!r}",
            },
            status_code=404,
        )
    marks = draft_eyes.load_marks(store, ref.id)
    rel_on = relevance.strip().lower() not in ("0", "false", "off", "no")
    view = smartdraft.build_view(
        store, ref.id, focus_dc=focus or None, relevance=rel_on, marks=marks
    )

    # Search (RRF over the active signals). Embed the query once, degrading to
    # lexical-only if the embedder is down (the search-embed guard).
    query = q.strip()
    active = {c for c in sig.lower() if c in _SIGNALS}
    hits: list[smartdraft.SearchHit] = []
    if query:
        qvec = None
        if "s" in active:
            embedder = getattr(
                getattr(get_runtime(request), "hub", None), "embedder", None
            )
            qvec = embed_query(embedder, query)  # None if no embedder / failure
        hits = smartdraft.search_chunks(
            view.nodes, query, active=active, query_embedding=qvec
        )

    return templates.TemplateResponse(
        request,
        "smartdraft/view.html.j2",
        {
            "active_tab": "smartdraft",
            "ident": ident,
            "ref": _ref_view(ref),
            "view": view,
            "relevance": rel_on,
            "focus_dc": view.focus.dc if view.focus else "",
            "focus_pinned": bool(view.focus and view.focus.pinned),
            "eye_count": len(marks.get("eyes") or {}),
            "q": query,
            "active_sig": "".join(c for c in _SIGNALS if c in active),
            "sview": "toc" if sview == "toc" else "list",
            "hits": hits,
        },
    )


def _ref_view(ref: Any) -> dict[str, Any]:
    return {"id": ref.id, "title": getattr(ref, "title", None) or f"draft {ref.id}"}
