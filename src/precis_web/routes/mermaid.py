"""Mermaid tab — the diagram you draw *with* the model (ADR 0057, slice 4).

The ``mermaid`` kind is otherwise a text/MCP surface (put/get/edit). This route
is the human affordance on the same data: a 2-pane editor — the rendered
diagram on the left (server-rendered via the pure-Python ``mermaidx`` engine,
sanitized through :func:`precis.figure.svg.sanitize_svg` and inlined), the
shared vocabulary + a chat on the right that drives the shared
:func:`precis.diagram.turn.run_turn` loop.

Routes:

* ``GET  /mermaid`` — the diagram list.
* ``GET  /mermaid/{slug}`` — the editor.
* ``GET  /mermaid/{slug}/render.svg`` — the sanitized rendered SVG (``<img>`` src).
* ``POST /mermaid/{slug}/turn`` — run one turn (form ``message=``) → JSON.

The turn runs in a worker thread (``asyncio.to_thread``) — the model call is
slow, and the event loop must stay responsive. When ``mermaidx`` is not
installed the render degrades to showing the source text (the kind is dark
then anyway).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.responses import Response as RawResponse

from precis.errors import NotFound
from precis.figure.svg import sanitize_svg
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.mermaid.mermaid import MERMAID_LANG, render_svg
from precis.mermaid.turn import run_turn
from precis_web.deps import get_store, templates

router = APIRouter(tags=["mermaid"])

log = logging.getLogger(__name__)

_LIST_LIMIT = 100


def _docs(store: Any, ref_id: int) -> tuple[str, str, str, list[str]]:
    """Return ``(source, vocab, notes, turn_texts)`` for a mermaid ref."""
    source = vocab = notes = ""
    turns: list[str] = []
    for c in store.reading_order(ref_id, kind="mermaid"):
        if c.chunk_kind == "mermaid_node" and not source:
            source = c.text
        elif c.chunk_kind == "mermaid_vocab" and not vocab:
            vocab = c.text
        elif c.chunk_kind == "mermaid_notes" and not notes:
            notes = c.text
        elif c.chunk_kind == "mermaid_turn":
            turns.append(c.text)
    return source, vocab, notes, turns


def _bindings(store: Any, ref_id: int) -> list[dict[str, Any]]:
    for c in store.reading_order(ref_id, kind="mermaid"):
        if c.chunk_kind == "mermaid_node":
            return store.element_bindings(c.chunk_id)
    return []


def _rendered_svg(source: str) -> str:
    """Render mermaid → sanitized SVG for inline display, or ``""`` when the
    engine is absent or the source doesn't render (the canvas falls back to
    the source text)."""
    if not source.strip():
        return ""
    try:
        return sanitize_svg(render_svg(source))
    except Exception:
        return ""


@router.get("/mermaid", response_class=HTMLResponse)
async def mermaid_list(request: Request) -> HTMLResponse:
    store = get_store(request)
    refs = store.list_refs(kind="mermaid", limit=_LIST_LIMIT)
    rows = [
        {"slug": r.slug, "title": r.title or r.slug, "handle": f"mm{r.id}"}
        for r in refs
    ]
    return templates.TemplateResponse(
        request,
        "mermaid/list.html.j2",
        {"active_tab": "mermaid", "diagrams": rows},
    )


@router.get("/mermaid/{slug}", response_class=HTMLResponse)
async def mermaid_detail(request: Request, slug: str) -> HTMLResponse:
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="mermaid", id=slug)
    except NotFound:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Mermaid diagram not found",
                "detail": f"no live mermaid diagram with slug {slug!r}",
                "status": 404,
            },
            status_code=404,
        )
    source, vocab, notes, turns = _docs(store, ref.id)
    findings = MERMAID_LANG.lint(source, None)
    ctx = {
        "active_tab": "mermaid",
        "slug": ref.slug,
        "title": ref.title or ref.slug,
        "source": source,
        "svg": _rendered_svg(source),  # inlined; sanitize is the trust boundary
        "vocab": vocab,
        "notes": notes,
        "turns": turns[-2:],
        "findings": [{"kind": f.kind, "message": f.message} for f in findings],
        "bindings": _bindings(store, ref.id),
    }
    return templates.TemplateResponse(request, "mermaid/detail.html.j2", ctx)


@router.get("/mermaid/{slug}/render.svg")
async def mermaid_render(request: Request, slug: str) -> RawResponse:
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="mermaid", id=slug)
    except NotFound:
        return RawResponse(status_code=404, content="not found")
    source, _v, _n, _t = _docs(store, ref.id)
    svg = _rendered_svg(source)
    if not svg:
        return RawResponse(status_code=422, content="could not render mermaid")
    return RawResponse(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/mermaid/{slug}/turn")
async def mermaid_turn(
    request: Request, slug: str, message: str = Form(...)
) -> JSONResponse:
    store = get_store(request)
    message = message.strip()
    try:
        ref = resolve_live_slug_ref(store, kind="mermaid", id=slug)
    except NotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    result = await asyncio.to_thread(run_turn, store, ref, message)
    return JSONResponse(
        {
            "reply": result.reply,
            "source": result.svg,  # TurnResult.svg holds the mermaid source
            "svg": _rendered_svg(result.svg) if result.svg else "",
            "changed": result.changed,
            "healed": result.healed,
            "vocab": result.vocab,
            "notes": result.notes,
            "findings": [
                {"kind": f.kind, "node": f.node, "message": f.message}
                for f in result.findings
            ],
            "bindings": [
                {
                    "element": b["element"],
                    "handle": b["handle"],
                    "relation": b["relation"],
                    "title": b.get("title", ""),
                }
                for b in result.bindings
            ],
        }
    )
