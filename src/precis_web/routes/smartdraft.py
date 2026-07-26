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
from fastapi.responses import HTMLResponse, JSONResponse, Response

from precis.store.types import Tag
from precis.utils.embed_query import embed_query
from precis_web import draft_eyes, smartdraft
from precis_web.deps import get_runtime, get_store, templates
from precis_web.routes.drafts import (
    _DOC_TYPES,
    _draft_author_lines,
    _draft_ref,
    _owner_workspace,
)

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
    debug: str = "",
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
    sview_mode = "toc" if sview == "toc" else "list"

    # Build the nodes once; search + view share them.
    nodes = smartdraft.build_nodes(store, ref.id, marks=marks)

    # Search (RRF over the active signals). Embed the query once, degrading to
    # lexical-only if the embedder is down; surface that so it isn't silent.
    query = q.strip()
    active = {c for c in sig.lower() if c in _SIGNALS}
    hits: list[smartdraft.SearchHit] = []
    sem_degraded = False
    if query:
        sranks: dict[int, int] = {}
        if "s" in active:
            embedder = getattr(
                getattr(get_runtime(request), "hub", None), "embedder", None
            )
            qvec = embed_query(embedder, query)  # None if no embedder / failure
            sem_degraded = qvec is None
            # top-N nearest via the HNSW index — no full-vector scan.
            sranks = smartdraft.semantic_ranks(store, ref.id, qvec)
        hits = smartdraft.search_chunks(
            nodes, query, active=active, semantic_ranks=sranks
        )

    # The in-TOC search view keeps every hit visible (uncollapsed).
    keep_dcs = {h.node.dc for h in hits} if (query and sview_mode == "toc") else None
    view = smartdraft.assemble_view(
        nodes,
        ref_id=ref.id,
        focus_dc=focus or None,
        relevance=rel_on,
        keep_dcs=keep_dcs,
    )

    # "occurs in N places" backlinks for a focused registry/glossary term
    # (gripe 56690) — computed from the already-loaded node set, no DB scan.
    term_occurrences = (
        smartdraft.term_occurrences(nodes, view.focus)
        if view.focus is not None and view.focus.is_term
        else []
    )

    # "Cited sources" (gripe 56635) — the focus block's paper citations,
    # each a new-tab hover-preview chip. Scoped to the focus only (cheap —
    # its text is already in hand), not the whole draft.
    cited_sources = (
        _cited_sources(store, view.focus.text) if view.focus is not None else []
    )

    # Tools pane (right-rail bottom): the export/metadata/lifecycle controls
    # ported from the classic reader. All reuse the classic /drafts/{ident}/…
    # endpoints, so this only needs the same context the classic route computes
    # (drafts.detail): reMarkable readiness, the byline editor's prefilled
    # lines, the genre picker's options + current genre/brief, and the
    # auto-author toggle state.
    from precis.export.remarkable import remarkable_configured

    _, owner_ws = _owner_workspace(store, ref)

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
            "sview": sview_mode,
            "hits": hits,
            "sem_degraded": sem_degraded,
            "needs": _needs_items(store, ref.id),
            "term_occurrences": term_occurrences,
            "cited_sources": cited_sources,
            "debug": debug.strip().lower() in ("1", "true", "on", "yes"),
            # ── Tools pane (right-rail bottom) — classic-reader parity ──
            "remarkable_ready": remarkable_configured(store),
            "author_lines": _draft_author_lines(ref),
            "doctypes": _DOC_TYPES,
            "cur_doctype": str(owner_ws.get("doc_type") or ""),
            "cur_brief": str(owner_ws.get("brief") or ""),
            "authoring_enabled": store.draft_authoring_enabled(ref.id),
        },
    )


def _cited_sources(store: Any, text: str) -> list[Any]:
    """The paper (``pc``/``pa``) sources the focus block cites, as
    hover-preview chips (gripe 56635) — reuses the classic ``/drafts``
    reader's block-scoped cite parser (:func:`precis_web.routes.drafts.
    _ref_chips`) rather than re-implementing cite parsing, then narrows its
    output to just the paper citations (that parser also yields intra-draft
    ``¶`` xrefs / other-kind mentions, which this rail doesn't want). Each
    chip is a :func:`precis_web.linkify.popover_chip` — its anchor already
    carries ``target=\"_blank\" rel=\"noopener\"`` (no ``data-dc``), so it
    opens the paper reader in a new tab and the no-reload nav interceptor
    leaves it alone."""
    from precis_web.routes.drafts import _paper_pdf_missing, _ref_chips

    def is_missing(kind: str, ident: str) -> bool:
        return kind == "paper" and _paper_pdf_missing(store, ident)

    chips = _ref_chips(text or "", is_missing=is_missing)
    return [c for c in chips if 'href="/r/paper/' in str(c)]


def _needs_items(store: Any, ref_id: int) -> list[dict[str, Any]]:
    """Open change-requests / LLM jobs on this draft for the right pane — the
    '✋ needs you' + in-flight status. Reuses the classic reader's walk; a bare
    list of ``{todo_id, title, blocked, asks, status}`` degrades to [] on error."""
    try:
        from precis_web.routes.drafts import _work_items

        items = _work_items(store, ref_id)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for w in items or []:
        # ``_work_items`` returns dicts (jobs = ``[{"id", "status", "reason"}]``,
        # asks = ``[{"tag", "question"}]``) — read keys, not attributes, or every
        # field silently defaults and the pane shows a blank row (todo → None).
        jobs = w.get("jobs") or ()
        status = (
            jobs[-1]["status"] if jobs else ("blocked" if w.get("blocked") else "open")
        )
        out.append(
            {
                "todo_id": w.get("todo_id"),
                "title": w.get("title") or "",
                "blocked": bool(w.get("blocked")),
                "asks": [
                    (a.get("question") or a.get("tag") or "")
                    for a in (w.get("asks") or [])
                ],
                "status": status,
            }
        )
    return out


def _ref_view(ref: Any) -> dict[str, Any]:
    return {"id": ref.id, "title": getattr(ref, "title", None) or f"draft {ref.id}"}


def _chunk_tags(store: Any, chunk_id: int) -> list[str]:
    """The tag values on one chunk (for the write path's echo-back)."""
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT t.value FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id "
                "WHERE ct.chunk_id = %s ORDER BY t.value",
                (chunk_id,),
            ).fetchall()
    except Exception:
        return []
    return [str(r[0]) for r in rows]


@router.post("/smartdraft/{ident}/chunk-tag")
async def chunk_tag(request: Request, ident: str) -> JSONResponse:
    """Add / remove a **chunk-level** tag (the ``T`` search signal). Body
    ``{handle: 'dc<id>', add?: str, remove?: str}``. Tags are free-form (OPEN
    namespace, lowercased). Returns the chunk's current tag values."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad body"}, status_code=400)
    store = get_store(request)
    ref = _draft_ref(store, ident)
    if ref is None:
        return JSONResponse({"ok": False, "error": "draft not found"}, status_code=404)
    rh = store.resolve_handle(str(payload.get("handle") or ""))
    if rh is None or rh.chunk_ord is None or int(rh.ref_id) != ref.id:
        return JSONResponse({"ok": False, "error": "chunk not found"}, status_code=404)
    add = str(payload.get("add") or "").strip()
    remove = str(payload.get("remove") or "").strip()
    if add:
        store.add_tag(ref.id, Tag.open(add), pos=rh.chunk_ord)
    if remove:
        store.remove_tag(ref.id, Tag.open(remove), pos=rh.chunk_ord)
    # Tag edits don't mint a new chunk_id, so bust the base-node cache directly
    # (a text edit self-invalidates via the content version) — the T signal +
    # the chip row then reflect it on the very next render.
    smartdraft.invalidate(ref.id)
    return JSONResponse({"ok": True, "tags": _chunk_tags(store, int(rh.chunk_id))})


@router.get("/smartdraft/{ident}/tag-suggest")
async def tag_suggest(request: Request, ident: str, q: str = "") -> JSONResponse:
    """Type-ahead: existing tag values on this draft matching ``q`` — so you
    reuse tags you've already applied rather than re-inventing them."""
    store = get_store(request)
    ref = _draft_ref(store, ident)
    query = q.strip()
    if ref is None or len(query) < 2:
        return JSONResponse({"tags": []})
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT t.value FROM tags t "
                "JOIN chunk_tags ct ON ct.tag_id = t.tag_id "
                "JOIN chunks c ON c.chunk_id = ct.chunk_id "
                "WHERE c.ref_id = %s AND t.value ILIKE %s ORDER BY t.value LIMIT 10",
                (ref.id, f"%{query}%"),
            ).fetchall()
    except Exception:
        return JSONResponse({"tags": []})
    return JSONResponse({"tags": [str(r[0]) for r in rows]})
