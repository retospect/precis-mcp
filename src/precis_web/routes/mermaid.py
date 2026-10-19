"""Mermaid tab — the diagram you draw *with* the model.

The ``mermaid`` kind is otherwise a text/MCP surface (put/get/edit). This route
is the human affordance on the same data: a 2-pane editor — the rendered
diagram on the left (server-rendered via the pure-Python ``mermaidx`` engine,
sanitized through :func:`precis.figure.svg.sanitize_svg` and inlined), the
shared vocabulary + a chat on the right that drives the shared
:func:`precis.diagram.turn.run_turn` loop.

Routes:

* ``GET  /mermaid`` — retired into the unified Drive surface; redirects to
  the ``kind=mermaid`` facet preset.
* ``GET  /mermaid/{slug}`` — the editor.
* ``GET  /mermaid/{slug}/render.svg`` — the sanitized rendered SVG (``<img>`` src).
* ``GET  /mermaid/{slug}/export.{fmt}`` — download the diagram
  (``mermaid`` / ``svg`` / ``png`` / ``pdf``, Content-Disposition attachment).
* ``POST /mermaid/{slug}/turn`` — run one turn (form ``message=``) → JSON.

The turn runs in a worker thread (``asyncio.to_thread``) — the model call is
slow, and the event loop must stay responsive. When ``mermaidx`` is not
installed the render degrades to showing the source text (the kind is dark
then anyway).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.responses import Response as RawResponse

from precis.errors import NotFound
from precis.figure.svg import sanitize_svg
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.mermaid.mermaid import MERMAID_LANG, render_pdf, render_png, render_svg
from precis.mermaid.turn import run_turn
from precis_web.deps import get_store, templates

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(tags=["mermaid"])

log = logging.getLogger(__name__)


def _docs(store: Store, ref_id: int) -> tuple[str, str, str, list[str]]:
    """Return ``(source, vocab, notes, turn_texts)`` for a mermaid ref."""
    source = vocab = notes = ""
    turns: list[str] = []
    for c in store.drafts.reading_order(ref_id, kind="mermaid"):
        if c.chunk_kind == "mermaid_node" and not source:
            source = c.text
        elif c.chunk_kind == "mermaid_vocab" and not vocab:
            vocab = c.text
        elif c.chunk_kind == "mermaid_notes" and not notes:
            notes = c.text
        elif c.chunk_kind == "mermaid_turn":
            turns.append(c.text)
    return source, vocab, notes, turns


def _bindings(store: Store, ref_id: int) -> list[dict[str, Any]]:
    for c in store.drafts.reading_order(ref_id, kind="mermaid"):
        if c.chunk_kind == "mermaid_node":
            return store.drafts.element_bindings(c.chunk_id)
    return []


def _rendered_svg(source: str) -> str:
    """Render mermaid → sanitized SVG for inline display, or ``""`` when the
    engine is absent or the source doesn't render (the canvas falls back to
    the source text)."""
    if not source.strip():
        return ""
    try:
        return sanitize_svg(render_svg(source))
    except Exception as exc:
        log.warning("mermaid render failed (%s): %s", type(exc).__name__, exc)
        return ""


@router.get("/mermaid", response_class=HTMLResponse)
async def mermaid_list() -> RedirectResponse:
    """Retired into the unified Drive surface — redirects to the
    ``kind=mermaid`` facet preset (same target as ``_drive_back.html.j2``'s
    back-link). The workbench (``/mermaid/{slug}`` and everything under it)
    is unaffected.
    """
    return RedirectResponse(url="/drive?k=mermaid&folder=*&sort=recency")


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


#: Download formats the workbench offers — ``mermaid`` is the raw source;
#: the rest are rendered server-side by mermaidx, ``svg`` through the same
#: sanitized render the canvas shows.
_EXPORT_FORMATS = ("mermaid", "svg", "png", "pdf")

#: Header-unsafe characters are scrubbed out of the download filename — a
#: stored slug should never contain them, but Content-Disposition is a raw
#: response header and must not be injectable through one (branch-desk
#: review of gr498614).
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@router.get("/mermaid/{slug}/export.{fmt}")
async def mermaid_export(request: Request, slug: str, fmt: str) -> RawResponse:
    """Download the diagram as ``<slug>.<fmt>`` — the same mermaidx render
    the page displays (``svg`` is the sanitized inline render, ``png`` /
    ``pdf`` rasterize/typeset the same source), or the raw source for
    ``mermaid``."""
    if fmt not in _EXPORT_FORMATS:
        return RawResponse(f"unknown export format {fmt!r}\n", status_code=404)
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="mermaid", id=slug)
    except NotFound:
        return RawResponse(status_code=404, content="not found")
    source, _v, _n, _t = _docs(store, ref.id)
    try:
        if fmt == "mermaid":
            body: str | bytes = source
            media = "text/plain; charset=utf-8"
        elif fmt == "svg":
            body = _rendered_svg(source)
            media = "image/svg+xml"
        elif fmt == "png":
            body = render_png(source)
            media = "image/png"
        else:
            body = render_pdf(source)
            media = "application/pdf"
    except Exception as exc:
        log.warning("mermaid export render failed (%s): %s", type(exc).__name__, exc)
        return RawResponse(status_code=422, content="could not render mermaid")
    if not body:
        return RawResponse(status_code=422, content="could not render mermaid")
    filename = _FILENAME_UNSAFE.sub("_", ref.slug or slug) or "diagram"
    return RawResponse(
        content=body,
        media_type=media,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}.{fmt}"',
        },
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
