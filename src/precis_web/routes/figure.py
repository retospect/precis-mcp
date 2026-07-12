"""Figure tab — the interactive SVG canvas you draw *with* the model.

The ``figure`` kind is otherwise a text/MCP surface (put/get/edit). This
route is the *human* affordance on the same data: a 3-pane canvas —

* left: the rendered SVG **inlined** into the page DOM (so declarative
  SMIL/CSS animation plays natively — an ``<img>`` embed would freeze it),
  made safe by :func:`precis.figure.svg.sanitize_svg` (the trust boundary),
  with a light coordinate grid overlay (the shared spatial frame);
* right: the shared **vocabulary** above a **chat** that drives the
  draw-with-me turn loop.

Routes:

* ``GET  /figure`` — the figure list.
* ``GET  /figure/{slug}`` — the 3-pane editor.
* ``GET  /figure/{slug}/source.svg`` — the sanitized SVG (the ``<img>`` src).
* ``POST /figure/{slug}/turn`` — run one :func:`precis.figure.turn.run_turn`
  (form ``message=``) and return ``{reply, svg, findings, changed}`` JSON.

The turn runs in a worker thread (``asyncio.to_thread``) — a ``claude``
subprocess is slow, and the event loop must stay responsive for other tabs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.responses import Response as RawResponse

from precis.errors import NotFound
from precis.figure.svg import DEFAULT_VIEWBOX, default_svg, lint_svg, sanitize_svg
from precis.figure.turn import run_turn
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis_web.deps import get_store, templates

router = APIRouter(tags=["figure"])

log = logging.getLogger(__name__)

_LIST_LIMIT = 100


def _viewbox(ref: Any, svg: str) -> tuple[float, float, float, float]:
    raw = (getattr(ref, "meta", None) or {}).get("viewbox")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        except (TypeError, ValueError):
            pass
    from precis.figure.svg import read_viewbox

    return read_viewbox(svg) or DEFAULT_VIEWBOX


def _docs(store: Any, ref_id: int) -> tuple[str, str, str, list[str]]:
    """Return ``(svg_source, vocab, notes, turn_texts)`` for a figure ref."""
    svg = ""
    vocab = ""
    notes = ""
    turns: list[str] = []
    for c in store.reading_order(ref_id, kind="figure"):
        if c.chunk_kind == "figure_node" and not svg:
            svg = c.text
        elif c.chunk_kind == "figure_vocab" and not vocab:
            vocab = c.text
        elif c.chunk_kind == "figure_notes" and not notes:
            notes = c.text
        elif c.chunk_kind == "figure_turn":
            turns.append(c.text)
    return svg or default_svg(), vocab, notes, turns


@router.get("/figure", response_class=HTMLResponse)
async def figure_list(request: Request) -> HTMLResponse:
    store = get_store(request)
    refs = store.list_refs(kind="figure", limit=_LIST_LIMIT)
    rows = [
        {"slug": r.slug, "title": r.title or r.slug, "handle": f"fg{r.id}"}
        for r in refs
    ]
    return templates.TemplateResponse(
        request,
        "figure/list.html.j2",
        {"active_tab": "figure", "figures": rows},
    )


@router.get("/figure/{slug}", response_class=HTMLResponse)
async def figure_detail(request: Request, slug: str) -> HTMLResponse:
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="figure", id=slug)
    except NotFound:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Figure not found",
                "detail": f"no live figure with slug {slug!r}",
                "status": 404,
            },
            status_code=404,
        )
    svg, vocab, notes, turns = _docs(store, ref.id)
    box = _viewbox(ref, svg)
    findings = lint_svg(svg, box)
    ctx = {
        "active_tab": "figure",
        "slug": ref.slug,
        "title": ref.title or ref.slug,
        # Inlined into the canvas via ``| safe`` — sanitize is the trust boundary.
        "svg": _safe_svg(svg),
        "vocab": vocab,
        "notes": notes,
        # Only the last couple of turns — the memory is the vocab + notes, not
        # the chat log; showing more is noise (and the log persists for search).
        "turns": turns[-2:],
        "viewbox": " ".join(_num(v) for v in box),
        "vb_w": _num(box[2]),
        "vb_h": _num(box[3]),
        "findings": [{"kind": f.kind, "message": f.message} for f in findings],
    }
    return templates.TemplateResponse(request, "figure/detail.html.j2", ctx)


@router.get("/figure/{slug}/source.svg")
async def figure_source(request: Request, slug: str) -> RawResponse:
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="figure", id=slug)
    except NotFound:
        return RawResponse(status_code=404, content="not found")
    svg, _vocab, _notes, _turns = _docs(store, ref.id)
    safe = _safe_svg(svg)  # defense-in-depth; storage is already clean
    return RawResponse(
        content=safe,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/figure/{slug}/turn")
async def figure_turn(
    request: Request, slug: str, message: str = Form(...)
) -> JSONResponse:
    store = get_store(request)
    message = message.strip()
    try:
        ref = resolve_live_slug_ref(store, kind="figure", id=slug)
    except NotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    result = await asyncio.to_thread(run_turn, store, ref, message)
    return JSONResponse(
        {
            "reply": result.reply,
            # Re-sanitized: the client injects this inline (innerHTML).
            "svg": _safe_svg(result.svg) if result.svg else result.svg,
            "changed": result.changed,
            "healed": result.healed,
            "vocab": result.vocab,
            "notes": result.notes,
            "findings": [
                {"kind": f.kind, "node": f.node, "message": f.message}
                for f in result.findings
            ],
        }
    )


def _num(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


def _safe_svg(svg: str) -> str:
    """Sanitize an SVG for inline rendering; empty canvas on any parse error."""
    try:
        return sanitize_svg(svg)
    except Exception:
        return default_svg()
