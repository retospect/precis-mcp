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
* ``GET /nanopub/fi<id>`` — the workbench **deep link**: the full
  workbench with the review pane preloaded on that claim (the tree's
  anchor hrefs point here, so cmd-click / copy-link keeps the navigation
  context; client-side ``history.replaceState`` keeps the URL in sync as
  the pane navigates). ``?embed=1`` still redirects (307) to
  ``/claim/fi<id>?embed=1`` — a pane must never nest the workbench. The
  claim page itself carries the review-and-sign section (nanopub-light-up
  merge); the POST doors below stay at this path (their post-action
  redirects land on ``/claim/fi<id>``); the review context is assembled
  by :func:`precis_web.nanopub_render.hub_context`, shared with
  :func:`precis_web.routes.claim.claim_page_context` so the two entry
  points (a fresh GET, this module's approve-error re-render) render
  byte-identical pages.
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

from precis_web.auth import current_user
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


def _index_context(request: Request) -> dict[str, Any]:
    """The workbench page context: the claim forest (compounds nest
    conjunct atoms, refined claims nest under what they refine, evidence
    as leaves) beside a review pane (the claim page, review section
    included, framed) and a paper pane. The old queue table folded in as
    the disputed-first strip + the OTS section under the tree. Shared by
    the bare index and the ``/nanopub/fi<id>`` deep link (which adds a
    ``preload_src`` for the review pane on top).

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

    return {
        "active_tab": "nanopub",
        "roots": roots,
        "n_nodes": len(rows),
        "state_counts": state_counts,
        "disputed": disputed,
        "batches": batches,
        "stuck": stuck,
        "draft_filter": draft_filter,
        "draft_notice": draft_notice,
    }


@router.get("/nanopub", response_class=HTMLResponse)
async def nanopub_index(request: Request) -> HTMLResponse:
    """The one nanopub working surface — see :func:`_index_context`."""
    return templates.TemplateResponse(
        request, "nanopub/index.html.j2", _index_context(request)
    )


@router.get("/nanopub/tree", response_model=None)
async def nanopub_tree() -> RedirectResponse:
    """The tree IS the index now — kept as a redirect for old links."""
    return RedirectResponse(url="/nanopub", status_code=307)


@router.get("/nanopub/fi{hub_id}", response_model=None)
async def nanopub_hub(request: Request, hub_id: int) -> HTMLResponse:
    """The workbench **deep link**: the full workbench (nav, tree, three
    panes) with the review pane preloaded on ``fi<hub_id>`` — what a
    cmd-click / copied link from the tree lands on, so a shared claim URL
    always arrives with its navigation context, never the bare claim page.

    The panes are divs, so ``preload_src`` is the URL the shell swaps into
    the review pane on load, not an iframe ``src``. There is no longer an
    ``?embed=1`` escape hatch here: nothing nests the workbench, so nothing
    needs telling that it is already inside a pane."""
    ctx = _index_context(request)
    ctx["preload_src"] = f"/claim/fi{hub_id}"
    return templates.TemplateResponse(request, "nanopub/index.html.j2", ctx)


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


def _back_to_hub(hub_id: int) -> RedirectResponse:
    """Post-action redirect. Lands on the claim page (nanopub-light-up
    merge) — never the old ``/nanopub/fi<id>`` URL, which would just bounce
    through the redirect a second time.

    A submit from inside the workbench's review pane needs no marker on
    this URL any more: the pane posts with ``HX-Request``, the browser
    replays that header across this 303, and ``claim_view`` answers with
    the chrome-less fragment the pane swaps in."""
    return RedirectResponse(url=f"/claim/fi{hub_id}", status_code=303)


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
    return _back_to_hub(hub_id)


@router.post("/nanopub/fi{hub_id}/sign", response_model=None)
async def nanopub_sign(
    request: Request, hub_id: int, attest: str = Form("")
) -> Response:
    from precis.errors import BadInput
    from precis.nanopub import mint

    store = get_store(request)
    attesting = bool(attest)
    # An attestation is a person's, so it is signed under the person's own
    # ORCID (``web_users.orcid``, set on /account) — not under a
    # deployment-wide identity that would make every attester the box.
    # No iD on the account means there is nobody to attribute the claim
    # to; that is a stop, not a default.
    signer = current_user(request)
    if attesting and (signer is None or not signer.orcid):
        return _error(
            request,
            "Sign refused",
            "an attestation is attributed to an ORCID iD, and there is no "
            "signed-in account carrying one — add yours on /account (with "
            "the gate on, so the server knows who you are), then sign. The "
            "bot signature below needs no identity and stays available.",
            400,
        )
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
            signer_orcid=signer.orcid if attesting and signer else None,
            signer_name=(signer.full_name or signer.login)
            if attesting and signer
            else None,
        )
    except (BadInput, PermissionError) as exc:
        return _error(request, "Sign refused", str(exc), 400)
    return _back_to_hub(hub_id)


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
    return _back_to_hub(hub_id)


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
    return _back_to_hub(hub_id)


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
    return _back_to_hub(hub_id)


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
    return _back_to_hub(hub_id)
