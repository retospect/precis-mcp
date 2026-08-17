"""``/claim/<head>`` full-page view + ``/preview/claim/<head>`` hover fragment
for a Taproot claim hub (turn-taking persona threads-adjacent). Both resolve the cite head via
:func:`precis_web.claim_render.render_claim_evidence`, which returns ``None``
when the head isn't a live ``TAPROOT:claim`` hub — rendered as a friendly
"no claim hub" stub rather than a 404, since a stray ``[fi123]`` cite is an
ordinary finding, not an error.

``POST /claim/<head>/unacquirable`` is the **claim-level** unacquirable-
override write door (:mod:`precis.taproot.trust`'s only softener) — the
twin of, but semantically distinct from, ``POST /papers/<id>/unacquirable``
(a pure acquirability fact about the paper that never softens a claim).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from precis_web.claim_render import (
    claim_citers,
    claim_full_sentence,
    render_claim_evidence,
)
from precis_web.deps import get_store, get_web_config, templates
from precis_web.routes.refs import _followup_discussions

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
        hub_ref_id = data["hub_ref_id"]
        # Full-page-only enrichments — kept OUT of render_claim_evidence so the
        # shared evidence shape stays identical between the singular and bulk
        # (smartdraft rail) paths:
        #   • citers  — the "Used by" inbound-cites section.
        #   • claim   — the full sentence from the finding_body chunk,
        #               falling back to refs.title when absent (titles are
        #               full-length since the [:200] cap was dropped, but
        #               legacy hubs may still carry a truncated one).
        #   • discussions — the "Ask & think" follow-up threads, the same
        #               affordance the generic finding detail carried before
        #               /refs/finding/<hub> started redirecting here.
        # getattr: reader tests drive this route with FakeStores that
        # predate the nanopub mixin — degrade the chip, not the page.
        _publish_row_fn = getattr(store, "nanopub_publish_row", None)
        publish_row = _publish_row_fn(hub_ref_id) if _publish_row_fn else None
        ctx = {
            **data,
            "missing": False,
            "citers": claim_citers(store, hub_ref_id),
            "claim": claim_full_sentence(store, hub_ref_id) or data["claim"],
            "discussions": _followup_discussions(store, hub_ref_id),
            # The review-and-sign surface's chip (slice 4) — state or None.
            "publish_state": publish_row.state if publish_row else None,
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


def _claim_error(
    request: Request, title: str, detail: str, status: int
) -> HTMLResponse:
    """Render the shared error page for a claim-route failure — mirrors
    ``precis_web.routes.papers._paper_error``."""
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {"title": title, "detail": detail, "status": status},
        status_code=status,
    )


@router.post("/claim/{head}/unacquirable", response_model=None)
async def claim_unacquirable(
    request: Request,
    head: str,
    mode: str = Form(""),
    note: str = Form(""),
) -> Response:
    """Set / clear a **claim-level** unacquirable-source declaration on this
    hub — :mod:`precis.taproot.trust`'s only softener: an explicit author
    assertion that Ⓐ (``mode='abstract'``) the abstract on file backs THIS
    claim, or ✍ (``mode='vouched'``) the author vouches for it, source
    unobtainable. Writes ``meta.unacquirable_override = {mode, note, by,
    at}`` on the hub's own ref — distinct from, and never inherited from,
    a source paper's Meta-tab acquirability declaration (``POST
    /papers/<id>/unacquirable``), which never softens a claim.

    ``mode`` empty or ``'clear'`` drops the override. ``note`` is required
    when setting: a silent override defeats the audit purpose (mirrors the
    finding handler's own guard and ``papers.unacquirable``)."""
    store = get_store(request)
    data = render_claim_evidence(store, head)
    if data is None:
        return _claim_error(
            request, "Unacquirable error", f"no claim hub for {head!r}", 400
        )
    hub_ref_id = data["hub_ref_id"]
    redirect = f"/claim/{head}"
    mode = (mode or "").strip().lower()
    if mode in ("", "clear"):
        store.update_ref(hub_ref_id, meta_patch={"unacquirable_override": None})
        return RedirectResponse(url=redirect, status_code=303)
    if mode not in ("abstract", "vouched"):
        return _claim_error(
            request, "Unacquirable error", f"unknown mode {mode!r}", 400
        )
    if not note.strip():
        return _claim_error(
            request,
            "Unacquirable error",
            "a note is required — say why the source can't be obtained",
            400,
        )
    override = {
        "mode": mode,
        "note": note.strip(),
        "by": get_web_config(request).source,
        "at": datetime.now(UTC).isoformat(),
    }
    store.update_ref(hub_ref_id, meta_patch={"unacquirable_override": override})
    return RedirectResponse(url=redirect, status_code=303)
