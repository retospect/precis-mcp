"""``/claim/<head>`` full-page view + ``/preview/claim/<head>`` hover fragment
for a Taproot claim hub (ADR 0051-adjacent). Both resolve the cite head via
:func:`precis_web.claim_render.render_claim_evidence`, which returns ``None``
when the head isn't a live ``TAPROOT:claim`` hub — rendered as a friendly
"no claim hub" stub rather than a 404, since a stray ``[fi123]`` cite is an
ordinary finding, not an error.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis_web.claim_render import claim_citers, render_claim_evidence
from precis_web.deps import get_store, templates

router = APIRouter(tags=["claim"])


@router.get("/claim/{head}", response_class=HTMLResponse)
async def claim_view(request: Request, head: str) -> HTMLResponse:
    """The claim hub's evidence page: the sentence, the ★ print set, and the
    fuller corroborating/contradicting evidence for context."""
    store = get_store(request)
    data = render_claim_evidence(store, head)
    if data is None:
        ctx: dict[str, Any] = {"head": head, "missing": True}
    else:
        # "Used by" (inbound cites) is a full-page-only section — added here,
        # not in render_claim_evidence, so the shared evidence shape stays
        # identical between the singular and bulk (smartdraft rail) paths.
        ctx = {
            **data,
            "missing": False,
            "citers": claim_citers(store, data["hub_ref_id"]),
        }
    return templates.TemplateResponse(request, "claim/view.html.j2", ctx)


@router.get("/preview/claim/{head}", response_class=HTMLResponse)
async def claim_preview(request: Request, head: str) -> HTMLResponse:
    """Compact hover card for a ``[fi123]`` / ``[<pub_id>]`` claim-hub cite."""
    data = render_claim_evidence(get_store(request), head)
    ctx = (
        {"head": head, "missing": True} if data is None else {**data, "missing": False}
    )
    return templates.TemplateResponse(request, "claim/popover.html.j2", ctx)
