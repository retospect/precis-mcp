"""``/nanopub`` — the claim-forest workbench (spec: Web view; slice 4).

One invariant: the UI only *renders* the state machine — integrity is
enforced in :mod:`precis.nanopub` (gates, freeze-at-review, append-only
proof store), so every action here is a thin interactive door onto those
functions with ``interactive=True`` (a person clicked).

* ``GET /nanopub`` — the one working surface: the claim forest
  (compounds nest conjunct atoms, evidence as leaves) beside a review
  pane (``/claim/fi<id>`` framed with ``?embed=1`` — the review-and-sign
  section lives on the claim page now, see below) and a paper pane, with
  draggable dividers. The **disputed** strip sits on top sorted by
  dispute age (disputes must not rot invisibly); OTS batches + the
  stuck-pending alert live under the tree. ``/nanopub/tree`` redirects
  here. ``?draft=dr<id>`` (or bare/``fi``-less numeric) scopes the forest
  + tally to the hubs that draft's chunks cite outbound
  (:func:`precis.nanopub.overview.draft_cited_hub_ids`) — "did I review
  everything this draft cites?" — with a clear-filter chip; an
  unresolvable draft id degrades to a friendly notice, not a 500.
* ``GET /nanopub/fi<id>`` — **redirects** (307, query string preserved)
  to ``/claim/fi<id>``: the nanopub-light-up merge folded the per-hub
  review page (claim DAG, publish row, action box, preflight) into the
  claim page's reader view as one page, one URL, one way to look at a
  claim and sign it. The POST doors below stay at this path (only their
  post-action redirects now land on ``/claim/fi<id>``); the review
  context itself is assembled by :func:`precis_web.nanopub_render.
  hub_context`, shared with :func:`precis_web.routes.claim.
  claim_page_context` so the two entry points (a fresh GET, this
  module's approve-error re-render) render byte-identical pages.
* ``GET /np/<code>`` — the exact frozen artifact bytes as
  ``application/trig``, served by artifact code (during embargo the
  w3id name resolves nowhere public; this is the local mirror).

The **registry POST is deliberately absent** — publishing is CLI-only
(``precis nanopub publish --live``), so the one true point of no return
never sits behind a web button.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from precis_web.deps import get_store, get_web_config, templates
from precis_web.routes.claim import claim_page_context

router = APIRouter(tags=["nanopub"])


def _error(request: Request, title: str, detail: str, status: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {"title": title, "detail": detail, "status": status},
        status_code=status,
    )


def _parse_draft_ref_id(value: str) -> int | None:
    """``dr173020`` / bare ``173020`` → a draft ref_id, leniently — or
    ``None`` when ``value`` doesn't parse as an id at all (the caller then
    shows a friendly notice, never a 500)."""
    v = value.strip().lower().removeprefix("dr")
    return int(v) if v.isdigit() else None


@router.get("/nanopub", response_class=HTMLResponse)
async def nanopub_index(request: Request) -> HTMLResponse:
    """The one nanopub working surface: the claim forest (compounds nest
    conjunct atoms, refined claims nest under what they refine, evidence
    as leaves) beside a review pane (the claim page, review section
    included, framed) and a paper pane. The old queue table folded in as
    the disputed-first strip + the OTS section under the tree.

    ``?draft=`` scopes the forest + tally to one draft's cited hubs (see
    module docstring) — the author's "did I check and sign everything my
    paper cites?" readout."""
    from collections import Counter
    from datetime import UTC, datetime, timedelta

    from precis.nanopub import overview
    from precis.nanopub.ots import STUCK_PENDING_DAYS

    store = get_store(request)

    draft_param = request.query_params.get("draft") or ""
    draft_filter: dict[str, Any] | None = None
    draft_notice: str | None = None
    cited: set[int] | None = None
    if draft_param:
        draft_ref_id = _parse_draft_ref_id(draft_param)
        draft_ref = (
            store.fetch_refs_by_ids([draft_ref_id]).get(draft_ref_id)
            if draft_ref_id is not None
            else None
        )
        if draft_ref_id is None or draft_ref is None or draft_ref.kind != "draft":
            draft_notice = f"{draft_param!r} isn't a live draft id — showing all claims"
        else:
            cited = overview.draft_cited_hub_ids(store, draft_ref_id)
            draft_filter = {"ref_id": draft_ref_id, "label": f"dr{draft_ref_id}"}

    roots = overview.hub_tree(store)
    rows = overview.hub_rows(store)
    if cited is not None:
        # Tally over the DISPLAYED set (pruned subtrees), not the literal
        # cite targets: a cited compound's conjunct atoms are real sign
        # work (atoms publish first), so the "N claims" chip and the
        # state strip must count what the tree shows.
        roots = overview.prune_tree(roots, cited)
        shown = overview.tree_ids(roots)
        rows = [r for r in rows if r.ref_id in shown]
    if draft_filter is not None:
        draft_filter["n"] = len(rows)
    disputed = [r for r in rows if r.disputed]
    # Pipeline-ordered per-state tally for the header strip (zeros
    # dropped) — the at-a-glance "what moved" readout, scoped to the draft
    # filter when one's active (the "am I done signing?" readout).
    tally = Counter(r.state or "unminted" for r in rows)
    state_counts = [
        (s, tally[s])
        for s in (
            "candidate",
            "reviewed",
            "signed",
            "anchored",
            "published",
            "unminted",
        )
        if tally[s]
    ]

    batches = store.nanopub_batches()
    threshold = datetime.now(UTC) - timedelta(days=STUCK_PENDING_DAYS)
    stuck = [b for b in store.nanopub_pending_batches() if b.created_at < threshold]

    return templates.TemplateResponse(
        request,
        "nanopub/index.html.j2",
        {
            "active_tab": "nanopub",
            "roots": roots,
            "n_nodes": len(rows),
            "state_counts": state_counts,
            "disputed": disputed,
            "batches": batches,
            "stuck": stuck,
            "draft_filter": draft_filter,
            "draft_notice": draft_notice,
        },
    )


@router.get("/nanopub/tree", response_model=None)
async def nanopub_tree() -> RedirectResponse:
    """The tree IS the index now — kept as a redirect for old links."""
    return RedirectResponse(url="/nanopub", status_code=307)


@router.get("/nanopub/fi{hub_id}", response_model=None)
async def nanopub_hub(request: Request, hub_id: int) -> RedirectResponse:
    """Legacy per-hub review URL — the claim page now carries the
    review-and-sign section too (nanopub-light-up merge). Query string
    preserved (notably ``?embed=1``, the workbench iframe's framing flag)."""
    suffix = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/claim/fi{hub_id}{suffix}", status_code=307)


@router.get("/np/{code}")
async def nanopub_trig(request: Request, code: str) -> Response:
    """The exact frozen artifact bytes — never a re-serialization."""
    artifact = get_store(request).nanopub_artifact_by_trusty(code)
    if artifact is None:
        return Response(
            f"no artifact {code!r}\n", status_code=404, media_type="text/plain"
        )
    return Response(
        artifact.trig_bytes,
        media_type="application/trig",
        headers={"X-Trusty-URI": artifact.trusty_uri},
    )


# ── interactive doors (a person clicked; integrity lives below) ──────


def _back_to_hub(request: Request, hub_id: int) -> RedirectResponse:
    """Post-action redirect, keeping ``?embed=1`` when the form was
    submitted from inside the /nanopub review pane. Lands on the claim
    page (nanopub-light-up merge) — never the old ``/nanopub/fi<id>`` URL,
    which would just bounce through the redirect a second time."""
    suffix = "?embed=1" if request.query_params.get("embed") == "1" else ""
    return RedirectResponse(url=f"/claim/fi{hub_id}{suffix}", status_code=303)


@router.post("/nanopub/fi{hub_id}/approve", response_model=None)
async def nanopub_approve(
    request: Request,
    hub_id: int,
    title: str = Form(""),
    payload: str = Form("{}"),
) -> Response:
    from precis.errors import BadInput
    from precis.nanopub import mint

    store = get_store(request)
    try:
        parsed = json.loads(payload or "{}")
        if not isinstance(parsed, dict):
            raise BadInput("grounding payload must be a JSON object")
        mint.approve(
            store,
            hub_id,
            payload=parsed,
            title=title.strip() or None,
            interactive=True,  # the review surface IS the human act
        )
    except (BadInput, json.JSONDecodeError) as exc:
        # Re-render the claim page with the reviewer's edits intact — a
        # gate refusal is feedback on a draft, not a dead end that eats
        # the hand-trimmed payload. Shares claim_page_context with the
        # plain GET so both render byte-identical pages (see module
        # docstring) — head is the canonical fi<id> form, always
        # resolvable regardless of what cite head the reviewer arrived by.
        ctx = claim_page_context(store, f"fi{hub_id}")
        if ctx.get("np") is None:
            return _error(request, "Approve refused", str(exc), 400)
        ctx["np"] = {
            **ctx["np"],
            "approve_error": str(exc),
            "submitted_title": title,
            "submitted_payload": payload,
        }
        return templates.TemplateResponse(
            request, "claim/view.html.j2", ctx, status_code=400
        )
    return _back_to_hub(request, hub_id)


@router.post("/nanopub/fi{hub_id}/sign", response_model=None)
async def nanopub_sign(
    request: Request, hub_id: int, attest: str = Form("")
) -> Response:
    from precis.errors import BadInput
    from precis.nanopub import mint

    store = get_store(request)
    attesting = bool(attest)
    try:
        mint.sign(
            store,
            hub_id,
            role="attesting" if attesting else "bot",
            # The sign button signs for real (spec: Web view) — the guard
            # is invocation: only this interactive route and the CLI may
            # open the attesting-key door.
            interactive=attesting,
            llm_models=[],
        )
    except (BadInput, PermissionError) as exc:
        return _error(request, "Sign refused", str(exc), 400)
    return _back_to_hub(request, hub_id)


@router.post("/nanopub/fi{hub_id}/reopen", response_model=None)
async def nanopub_reopen(request: Request, hub_id: int) -> Response:
    store = get_store(request)
    row = store.nanopub_publish_row(hub_id)
    if row is None or not store.nanopub_reopen(row.id):
        return _error(
            request,
            "Reopen refused",
            "only a reviewed/signed (pre-anchor) row reopens",
            400,
        )
    return _back_to_hub(request, hub_id)


@router.post("/nanopub/fi{hub_id}/signoff/{link_id}", response_model=None)
async def nanopub_signoff(
    request: Request, hub_id: int, link_id: int, note: str = Form("")
) -> Response:
    from precis.errors import BadInput
    from precis.nanopub.preflight import signoff_edge

    store = get_store(request)
    try:
        ok = signoff_edge(
            store,
            link_id,
            by=get_web_config(request).source,
            note=note,
            interactive=True,  # this form IS the human attestation
        )
    except BadInput as exc:
        return _error(request, "Sign-off refused", str(exc), 400)
    if not ok:
        return _error(request, "Sign-off refused", f"no evidence edge {link_id}", 400)
    return _back_to_hub(request, hub_id)


def _parse_ref(value: str, prefixes: tuple[str, ...]) -> int:
    """``pa4185`` / ``pc518151`` / bare ``4185`` → int ref/chunk id."""
    from precis.errors import BadInput

    v = value.strip().lower()
    for p in prefixes:
        v = v.removeprefix(p)
    if not v.isdigit():
        raise BadInput(f"not an id: {value!r} — want e.g. {prefixes[0]}123 or bare 123")
    return int(v)


@router.post("/nanopub/fi{hub_id}/evidence/add", response_model=None)
async def nanopub_evidence_add(
    request: Request,
    hub_id: int,
    source: str = Form(""),
    chunk: str = Form(""),
    relation: str = Form("corroborates"),
) -> Response:
    """Human curation door: attach one paper/patent evidence edge to the
    hub. The new edge starts withheld (no ``support`` verdict), so it
    still needs sign-off or refine-verification before publish."""
    from precis.errors import BadInput
    from precis.nanopub import evidence as ev
    from precis.taproot.hub import attach_evidence

    store = get_store(request)
    try:
        paper_ref_id = _parse_ref(source, ("pa", "pt"))
        meta: dict[str, Any] = {}
        if chunk.strip():
            chunk_id = _parse_ref(chunk, ("pc",))
            chunks = ev.fetch_chunks(store, [chunk_id])
            if not chunks or chunks[0].ref_id != paper_ref_id:
                raise BadInput(
                    f"pc{chunk_id} is not a chunk of pa{paper_ref_id} — the "
                    "grounding passage must live in the cited source"
                )
            meta["source_handle"] = f"pc{chunk_id}"
        attach_evidence(
            store,
            hub_ref_id=hub_id,
            paper_ref_id=paper_ref_id,
            role=relation,
            meta=meta,
            set_by="user",  # the actors vocab's direct-human slug
        )
    except BadInput as exc:
        return _error(request, "Add refused", str(exc), 400)
    return _back_to_hub(request, hub_id)


@router.post("/nanopub/fi{hub_id}/evidence/{link_id}/remove", response_model=None)
async def nanopub_evidence_remove(
    request: Request, hub_id: int, link_id: int
) -> Response:
    from precis.nanopub.preflight import remove_evidence_edge

    store = get_store(request)
    if not remove_evidence_edge(store, hub_id, link_id, interactive=True):
        return _error(
            request,
            "Remove refused",
            f"no evidence edge {link_id} on fi{hub_id}",
            400,
        )
    return _back_to_hub(request, hub_id)
