"""``/claim/<head>`` full-page view + ``/preview/claim/<head>`` hover fragment
for a Taproot claim hub (turn-taking persona threads-adjacent). Both resolve the cite head via
:func:`precis_web.claim_render.render_claim_evidence`, which returns ``None``
when the head isn't a live ``TAPROOT:claim`` hub — rendered as a friendly
"no claim hub" stub rather than a 404, since a stray ``[fi123]`` cite is an
ordinary finding, not an error.

The **one** claim page (nanopub-light-up UX consolidation): the full page is
the reader evidence view (sentence, ★ print set, corroborating/contradicting
evidence, citers, discussions) *and*, when the store carries the nanopub
mixin, the review-and-sign surface merged in under ``ctx['np']``
(:func:`~precis_web.nanopub_render.hub_context` — namespaced rather than
splatted flat so its keys can never silently shadow the reader context's
own). ``/nanopub/fi<id>`` is the workbench deep link (full workbench with
this page framed in the review pane); :func:`claim_page_context` is the
shared builder ``routes/nanopub.py``'s approve-error re-render also calls,
so both paths render byte-identical pages.

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

from precis.utils import handle_registry
from precis_web import ask
from precis_web.claim_render import (
    claim_citers,
    claim_full_sentence,
    render_claim_evidence,
)
from precis_web.deps import get_store, get_web_config, templates
from precis_web.nanopub_render import hub_context
from precis_web.routes.refs import _followup_discussions

router = APIRouter(tags=["claim"])


def _refuted_ruling(store: Any, hub_ref_id: int) -> dict[str, Any] | None:
    """``None`` unless ``hub_ref_id`` carries the ``STATUS:refuted`` control
    tag (the do-not-repropose ledger, docs/backlog/quest-dossier-dialectic.md
    §"Refuted lifecycle") — then the superseding-ruling shape the claim page
    banner needs: ``ruling_id``/``ruling_title``/``ruling_url``, all ``None``
    when refuted but the ruling link is missing (banner still shows, just
    says "ruling unknown" rather than erroring).

    Looks for an outbound ``superseded-by`` link first, then ``retracted-by``
    (the two relation shapes the refuted-lifecycle write path uses), and
    takes the first link target that's itself a finding — the negative
    ruling is minted as an established finding, per the design doc."""
    if not store.has_tag(hub_ref_id, "STATUS", "refuted"):
        return None
    for relation in ("superseded-by", "retracted-by"):
        links = store.links_for(hub_ref_id, direction="out", relation=relation)
        if not links:
            continue
        targets = store.fetch_refs_by_ids([link.dst_ref_id for link in links])
        ruling = next(
            (
                targets[link.dst_ref_id]
                for link in links
                if targets.get(link.dst_ref_id) is not None
                and targets[link.dst_ref_id].kind == "finding"
            ),
            None,
        )
        if ruling is not None:
            return {
                "ruling_id": ruling.id,
                "ruling_title": ruling.title,
                "ruling_url": f"/claim/{handle_registry.format_handle('finding', ruling.id)}",
            }
    return {"ruling_id": None, "ruling_title": None, "ruling_url": None}


def claim_page_context(store: Any, head: str) -> dict[str, Any]:
    """The full ``/claim/<head>`` page context: the reader evidence shape
    plus, when the store carries the nanopub mixin, the review-and-sign
    context merged in under ``ctx['np']``. Shared by :func:`claim_view`
    (the GET) and ``routes/nanopub.py``'s approve-error re-render (the one
    POST door that still needs to re-render a full page on a gate refusal)
    so both render byte-identical pages."""
    data = render_claim_evidence(store, head)
    if data is None:
        return {"head": head, "missing": True}
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
    #   • np      — the review-and-sign section (state header, DAG, dispute
    #               panel, action box, …). getattr: reader tests drive this
    #               route with FakeStores that predate the nanopub mixin —
    #               degrade by dropping the section, not the page.
    #   • refuted — the red banner shape (see :func:`_refuted_ruling`), None
    #               unless the hub carries STATUS:refuted.
    _publish_row_fn = getattr(store, "nanopub_publish_row", None)
    hub_ref = store.fetch_refs_by_ids([hub_ref_id]).get(hub_ref_id)
    return {
        **data,
        "missing": False,
        "citers": claim_citers(store, hub_ref_id),
        "claim": claim_full_sentence(store, hub_ref_id) or data["claim"],
        "discussions": _followup_discussions(store, hub_ref_id),
        "ask_model": ask.answer_model_label(),
        "passages_by_paper": _passages_by_paper(data["chunks"]),
        "np": hub_context(store, hub_ref_id) if _publish_row_fn else None,
        "refuted": _refuted_ruling(store, hub_ref_id),
        "hypothesis": _hypothesis_fields(store, hub_ref),
    }


def _hypothesis_fields(store: Any, hub_ref: Any) -> dict[str, str] | None:
    """``{"motivation": …, "testable_by": …}`` for the claim page's
    falsification-prose fields — real fields, not the raw ``json.dumps`` the
    review textarea shows (:func:`~precis_web.nanopub_render._suggested_payload`,
    left untouched). ``None`` unless ``hub_ref`` is marked a hypothesis
    (docs/backlog/hypothesis-cites-render-not-stored.md). ``hub_ref`` may be
    ``None`` (a store hiccup) — degrades to no section rather than raising."""
    if hub_ref is None:
        return None
    from precis.handlers._finding_hypothesis import hypothesis_prose

    return hypothesis_prose(store, hub_ref)


def _passages_by_paper(chunks: list[dict[str, Any]]) -> dict[str, list[dict]]:
    """Group the grounding passages under the paper row each one grounds,
    so the template nests every quote directly below its source instead of
    a separate "Grounding passages" section repeating the pc handles. A
    passage grounding several papers appears once, under its highest-
    ranked paper (``entry["papers"]`` is already role-rank ordered by
    ``_grounding_chunks``) — the entry keeps the full papers list, so the
    nested line still names the other roles it grounds."""
    by_paper: dict[str, list[dict]] = {}
    for c in chunks:
        first = next((p["handle"] for p in c["papers"]), None)
        if first is not None:
            by_paper.setdefault(first, []).append(c)
    return by_paper


@router.get("/claim/{head}", response_class=HTMLResponse)
async def claim_view(request: Request, head: str) -> HTMLResponse:
    """The claim hub's one page: the sentence, the ★ print set, the fuller
    corroborating/contradicting evidence, and — when the store carries the
    nanopub mixin — the review-and-sign section (state, DAG, approve/sign
    action)."""
    ctx = claim_page_context(get_store(request), head)
    # htmx-aware (the ``flags.py`` pattern): the /nanopub workbench swaps
    # this claim straight into its review pane, so an htmx request gets the
    # body WITHOUT page chrome — the same ``claim/_body.html.j2`` the full
    # page includes, off the same context, so a swapped pane is
    # byte-identical to a server-rendered one.
    #
    # Branching on the header rather than on a distinct /fragment URL is what
    # makes the permalink resolvers work: /c/<handle> and /r/paper/<id> 303
    # INTO this route, and the browser replays HX-Request across a
    # same-origin redirect. A separate fragment URL would be unreachable
    # through those chains without re-introducing the ?embed=1-style query
    # threading this refactor removes.
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "claim/_body.html.j2", ctx)
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
