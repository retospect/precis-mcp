"""LLM routing pages + the selector preview's data source.

``GET /api/llm/resolve`` mirrors :func:`~precis.utils.llm.router.resolve_selection`
over HTTP: given ``(model=alias, placement?, reasoning?, temperature?)`` it
returns what :func:`~precis.utils.llm.router.route` would actually pick,
without making a call. The shared ``_llm_selector.html.j2`` macro (used by
smartdraft's ask toolbar, and any future structured picker) fetches this on
every control change to render its live "→ model · placement" preview line.
Read-only, never raises — a bad/unknown alias comes back 200 with the row's
``error`` field set rather than a 4xx, so the widget can render the message
inline instead of special-casing a fetch failure.

``GET /llm`` / ``/llm/chain`` / ``/llm/op`` are the human read-only
baseline the operator reaches for first (gr484946): the only ``/llm/*``
paths that ever existed were the ``/factory`` editor's POST endpoints,
so the guessed GETs 404'd. ``/llm/chain`` renders the same live rung
table as the Models tab header (``_active_routing_ctx`` — resolved via
the same ``resolve_chain``/``resolve_model`` dispatch walks, so it can't
drift) plus the operator-override JSON; ``/llm/op`` the per-operation
routing rollup (``_llm_ops_ctx``). Editing stays on
``/status?tab=services`` — these pages link there.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from precis.utils.llm.router import resolve_selection
from precis_web.deps import get_store, templates
from precis_web.routes.status import (
    _active_routing_ctx,
    _llm_chain_ctx,
    _llm_ops_ctx,
)

router = APIRouter(prefix="/api/llm", tags=["llm"])

#: The read-only ``/llm`` section — see the module docstring.
pages = APIRouter(prefix="/llm", tags=["llm"])


def _parse_temperature(raw: str | None) -> float | None:
    """Best-effort float parse — junk or absent both degrade to ``None``
    rather than 400ing the preview fetch (mirrors
    :func:`~precis.utils.llm.router.llm_select_from_payload`'s leniency)."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@router.get("/resolve")
async def resolve(
    request: Request,
    model: str = "big",
    placement: str | None = None,
    reasoning: str | None = None,
    temperature: str | None = None,
) -> JSONResponse:
    """Preview a structured ``(model=alias, placement, reasoning,
    temperature)`` selection — the JSON body is
    :func:`~precis.utils.llm.router.resolve_selection`'s return dict verbatim
    (``tier`` is rendered as its plain string value, already JSON-safe)."""
    row = resolve_selection(
        model,
        placement=placement or None,
        reasoning=reasoning or None,
        temperature=_parse_temperature(temperature),
    )
    return JSONResponse(row)


@pages.get("", response_class=HTMLResponse)
@pages.get("/", response_class=HTMLResponse)
async def llm_index(request: Request) -> RedirectResponse:
    """``/llm`` lands on the chain view — the routing baseline (gr484946)."""
    return RedirectResponse(url="/llm/chain", status_code=302)


@pages.get("/chain", response_class=HTMLResponse)
async def llm_chain(request: Request) -> HTMLResponse:
    """Per-tier live placement chains: every rung's slot, model,
    transport and placement, as ``dispatch`` would walk them right now
    (``_active_routing_ctx``) — plus the cloud dial and any operator
    ``llm.chain.<tier>`` override JSON (``_llm_chain_ctx``)."""
    store = get_store(request)
    ctx: dict[str, Any] = {"active_tab": "llm"}
    ctx.update(await asyncio.to_thread(_active_routing_ctx, store))
    ctx.update(await asyncio.to_thread(_llm_chain_ctx, store))
    return templates.TemplateResponse(request, "llm/chain.html.j2", ctx)


@pages.get("/op", response_class=HTMLResponse)
async def llm_op(request: Request) -> HTMLResponse:
    """Per-operation routing rollup — default vs effective model for
    every registered/observed call site (``_llm_ops_ctx``). Read-only;
    the Services tab owns the edits."""
    store = get_store(request)
    ctx: dict[str, Any] = {"active_tab": "llm"}
    ctx.update(await asyncio.to_thread(_llm_ops_ctx, store))
    return templates.TemplateResponse(request, "llm/op.html.j2", ctx)
