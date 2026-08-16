"""``/nanopub`` — the review-and-sign surface (spec: Web view; slice 4).

Three surfaces, one invariant: the UI only *renders* the state machine —
integrity is enforced in :mod:`precis.nanopub` (gates, freeze-at-review,
append-only proof store), so every action here is a thin interactive
door onto those functions with ``interactive=True`` (a person clicked).

* ``GET /nanopub`` — "see all the things": every claim hub by publish
  state; the **disputed** bucket first, sorted by dispute age (disputes
  must not rot invisibly); drifted rows flagged; OTS batches pending
  past threshold.
* ``GET /nanopub/fi<id>`` — per-hub review page: the claim DAG as
  clickable SVG (papers → atoms → hub → anchor; ``contradicts`` dashed,
  disputed hubs marked by shape+colour, never colour alone), the publish
  row side panel, the publish preflight, and one action per state.
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

router = APIRouter(tags=["nanopub"])


def _error(request: Request, title: str, detail: str, status: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {"title": title, "detail": detail, "status": status},
        status_code=status,
    )


@router.get("/nanopub", response_class=HTMLResponse)
async def nanopub_index(request: Request) -> HTMLResponse:
    from datetime import UTC, datetime, timedelta

    from precis.nanopub import overview
    from precis.nanopub.ots import STUCK_PENDING_DAYS

    store = get_store(request)
    rows = overview.hub_rows(store)
    disputed = [r for r in rows if r.disputed]
    live = [r for r in rows if not r.disputed and r.state is not None]
    unminted = [r for r in rows if not r.disputed and r.state is None]

    batches = store.nanopub_batches()
    threshold = datetime.now(UTC) - timedelta(days=STUCK_PENDING_DAYS)
    stuck = [b for b in store.nanopub_pending_batches() if b.created_at < threshold]

    # Same click-to-inspect detail shape as /nanopub/tree.
    detail: dict[str, Any] = {}
    for r in rows:
        fields = [["state", r.state or "unminted"], ["frozen", r.frozen or "—"]]
        if r.disputed_since:
            fields.append(["disputed since", r.disputed_since.strftime("%Y-%m-%d")])
        if r.withheld_count:
            fields.append(["withheld edges", str(r.withheld_count)])
        if r.drifted:
            fields.append(["drifted", "frozen string ≠ live title"])
        if r.approved_title and r.approved_title != r.title:
            fields.append(["approved", r.approved_title])
        if r.trusty_uri:
            fields.append(["trusty", r.trusty_uri])
        if r.batch_id:
            fields.append(["batch", str(r.batch_id)])
        if r.updated_at:
            fields.append(["updated", r.updated_at.strftime("%Y-%m-%d")])
        links = [
            ["open claim page →", f"/nanopub/fi{r.ref_id}"],
            ["tree view", "/nanopub/tree"],
        ]
        if r.trusty_uri:
            links.append(["TriG bytes", f"/np/{r.trusty_uri.rsplit('/', 1)[-1]}"])
        detail[f"h{r.ref_id}"] = {
            "kind": "claim hub",
            "title": r.title,
            "fields": fields,
            "links": links,
        }

    return templates.TemplateResponse(
        request,
        "nanopub/index.html.j2",
        {
            "active_tab": "nanopub",
            "disputed": disputed,
            "live": live,
            "unminted": unminted,
            "batches": batches,
            "stuck": stuck,
            "detail": detail,
        },
    )


@router.get("/nanopub/tree", response_class=HTMLResponse)
async def nanopub_tree(request: Request) -> HTMLResponse:
    """The claim forest: compounds nest their conjunct atoms, refined
    claims nest under what they refine, evidence sources hang as leaves.
    Same data family as the queue table — publish rows + links, no new
    state. A ``detail`` dict (same shape as the hub page's graph pane)
    backs the click-to-inspect side panel."""
    from precis.nanopub import overview

    store = get_store(request)
    roots = overview.hub_tree(store)

    def _count(nodes: list[Any]) -> int:
        return sum(1 + _count(n.children) for n in nodes)

    detail: dict[str, Any] = {}

    def _collect(n: Any) -> None:
        r = n.row
        hub_key = f"h{r.ref_id}"
        if hub_key not in detail:
            fields = [["state", r.state or "unminted"]]
            if r.disputed:
                fields.append(["disputed", "yes — blocked"])
            if r.withheld_count:
                fields.append(["withheld edges", str(r.withheld_count)])
            if r.drifted:
                fields.append(["drifted", "frozen string ≠ live title"])
            if r.approved_title and r.approved_title != r.title:
                fields.append(["approved", r.approved_title])
            if r.trusty_uri:
                fields.append(["trusty", r.trusty_uri])
            if n.children:
                fields.append(["nested claims", str(len(n.children))])
            if n.evidence:
                fields.append(["evidence", str(len(n.evidence))])
            links = [["open claim page →", f"/nanopub/fi{r.ref_id}"]]
            if r.trusty_uri:
                links.append(["TriG bytes", f"/np/{r.trusty_uri.rsplit('/', 1)[-1]}"])
            detail[hub_key] = {
                "kind": "claim hub",
                "title": r.title,
                "fields": fields,
                "links": links,
            }
        for e in n.evidence:
            ev_key = f"e{e.kind}-{e.ref_id}-{e.relation}"
            if ev_key not in detail:
                if e.kind == "finding":
                    url = f"/nanopub/fi{e.ref_id}"
                elif e.kind == "paper":
                    url = f"/papers/{e.ref_id}"
                else:
                    url = f"/refs/{e.kind}/{e.ref_id}"
                detail[ev_key] = {
                    "kind": e.kind,
                    "title": e.title,
                    "fields": [["relation", e.relation]],
                    "links": [["open →", url]],
                }
        for c in n.children:
            _collect(c)

    for root in roots:
        _collect(root)

    return templates.TemplateResponse(
        request,
        "nanopub/tree.html.j2",
        {
            "active_tab": "nanopub",
            "roots": roots,
            "n_nodes": _count(roots),
            "detail": detail,
        },
    )


@router.get("/nanopub/fi{hub_id}", response_class=HTMLResponse)
async def nanopub_hub(request: Request, hub_id: int) -> HTMLResponse:
    store = get_store(request)
    ctx = _hub_context(store, hub_id)
    if ctx is None:
        return _error(request, "No claim hub", f"fi{hub_id} is not a claim hub", 404)
    ctx["active_tab"] = "nanopub"
    return templates.TemplateResponse(request, "nanopub/hub.html.j2", ctx)


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
        return _error(request, "Approve refused", str(exc), 400)
    return RedirectResponse(url=f"/nanopub/fi{hub_id}", status_code=303)


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
    return RedirectResponse(url=f"/nanopub/fi{hub_id}", status_code=303)


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
    return RedirectResponse(url=f"/nanopub/fi{hub_id}", status_code=303)


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
    return RedirectResponse(url=f"/nanopub/fi{hub_id}", status_code=303)


# ── page assembly ────────────────────────────────────────────────────

_STATE_ACTION = {
    None: ("approve", "Approve (freeze this exact string)"),
    "candidate": ("approve", "Approve (freeze this exact string)"),
    "reviewed": ("sign", "Sign"),
    "signed": ("reopen", "Reopen (discard artifact pointer, re-mint)"),
    "anchored": ("publish-cli", "Publish via CLI (point of no return)"),
    "published": (None, "Published — change = supersede/retract"),
}


def _hub_context(store: Any, hub_id: int) -> dict[str, Any] | None:
    from precis.errors import BadInput
    from precis.nanopub import evidence
    from precis.nanopub.preflight import publish_preflight, withheld_edges

    try:
        bundle = evidence.load_bundle(store, hub_id)
    except BadInput:
        return None

    row = store.nanopub_publish_row(hub_id)
    state = row.state if row else None
    artifact = (
        store.nanopub_artifact(row.artifact_id)
        if row and row.artifact_id is not None
        else None
    )
    proof = (
        store.nanopub_latest_proof(row.batch_id)
        if row and row.batch_id is not None
        else None
    )
    disputed = bool(bundle.contradicts)
    action, action_label = _STATE_ACTION.get(state, (None, ""))
    if disputed and action is not None:
        # No forward transition is offered while the edge stands — spec
        # publish-time gate #6; covers a contradicts edge arriving AFTER
        # anchoring too (the server-side gates refuse regardless).
        action, action_label = None, "Blocked — unresolved contradicts edge"

    withheld = withheld_edges(store, hub_id)
    preflight = publish_preflight(store, hub_id, row=row) if state is not None else []
    return {
        "hub_id": hub_id,
        "bundle": bundle,
        "row": row,
        "state": state or "unminted",
        "frozen": _frozen_rung(state),
        "artifact": artifact,
        "proof_state": proof[0] if proof else None,
        "disputed": disputed,
        "disputes": _dispute_panel(store, bundle),
        "withheld": withheld,
        "preflight": preflight,
        "action": action,
        "action_label": action_label,
        "suggested_payload": _suggested_payload(row, bundle),
        "graph": _graph(store, bundle, row),
    }


def _frozen_rung(state: str | None) -> str:
    from precis.nanopub.overview import HubOverviewRow

    return HubOverviewRow(
        ref_id=0,
        title="",
        state=state,
        publish_row_id=None,
        approved_title=None,
        claim_sha=None,
        trusty_uri=None,
        batch_id=None,
        updated_at=None,
        disputed=False,
        disputed_since=None,
        withheld_count=0,
    ).frozen


def _suggested_payload(row: Any, bundle: Any) -> str:
    """The approve form's prefill: the frozen payload when one exists,
    else a passage skeleton derived from the bundle (quote/snip left for
    the reviewer — freeze-at-review means a human fills exactly what the
    signature will cover)."""
    if row is not None and row.grounding:
        return json.dumps(row.grounding, indent=2)
    by_ref = {s.ref_id: s for s in bundle.sources}
    passages = []
    for chunk in bundle.grounding_chunks:
        src = by_ref.get(chunk.ref_id)
        if src is None:
            continue
        passages.append(
            {
                "doi": src.doi or "",
                "pdf_sha256": src.pdf_sha256 or "",
                "quote": "",
                "snip": "",
                "chunk_id": chunk.chunk_id,
                "role": src.role,
            }
        )
    if not passages:
        passages = [
            {
                "doi": s.doi or "",
                "pdf_sha256": s.pdf_sha256 or "",
                "quote": "",
                "snip": "",
                "role": s.role,
            }
            for s in bundle.sources[:3]
        ]
    return json.dumps({"passages": passages, "fields": {}}, indent=2)


def _dispute_panel(store: Any, bundle: Any) -> list[dict[str, Any]]:
    """Symmetric dispute rendering: the hub's claim beside each
    contradicting passage's text, so the reviewer sees the actual
    conflict without hunting (fi189542 precedent)."""
    if not bundle.contradicts:
        return []
    from precis.nanopub import evidence as ev
    from precis.taproot import seniority

    hub_evidence = seniority.derive_evidence(store, bundle.hub_ref_id)
    chunks_by_paper: dict[int, str] = {}
    contradict_refs = [g for g in hub_evidence.grounding if g.relation == "contradicts"]
    chunk_ids = []
    for g in contradict_refs:
        handle = g.source_handle or ""
        if handle.startswith("pc") and handle[2:].isdigit():
            chunk_ids.append((g.paper_ref_id, int(handle[2:])))
    if chunk_ids:
        infos = {
            c.chunk_id: c for c in ev.fetch_chunks(store, [cid for _, cid in chunk_ids])
        }
        for paper_id, cid in chunk_ids:
            if cid in infos and paper_id not in chunks_by_paper:
                chunks_by_paper[paper_id] = infos[cid].text
    return [
        {
            "paper_ref_id": s.ref_id,
            "paper_title": s.title,
            "doi": s.doi,
            "has_pdf": bool(s.pdf_sha256),
            "passage": chunks_by_paper.get(s.ref_id, ""),
        }
        for s in bundle.contradicts
    ]


def _graph(store: Any, bundle: Any, row: Any) -> dict[str, Any]:
    """The per-hub neighborhood as positioned SVG nodes + edges (layered:
    papers → atoms → hub → anchor), with a detail dict per node for the
    click pane — the viewer.html prototype's NODES shape, served live."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    width = 940
    row_h = 118

    def _spread(n: int) -> list[int]:
        if n == 0:
            return []
        gap = width // (n + 1)
        return [gap * (i + 1) for i in range(n)]

    papers = bundle.sources + bundle.contradicts
    paper_xs = _spread(len(papers))
    for x, src in zip(paper_xs, papers):
        nodes.append(
            {
                "id": f"pc{src.ref_id}",
                "cls": "paper" + (" contradicts" if src.role == "contradicts" else ""),
                "x": x,
                "y": 40,
                "label": (src.title or f"pc{src.ref_id}")[:38],
                "sub": f"{src.kind} · {src.year or '—'} · {src.role}",
                "detail": {
                    "kind": src.kind,
                    "title": src.title,
                    "fields": [
                        ["role", src.role],
                        ["via", src.via],
                        ["doi", src.doi or "—"],
                        ["pdf sha256", (src.pdf_sha256 or "—")[:24]],
                    ],
                    "links": (
                        [["DOI", f"https://doi.org/{src.doi}"]] if src.doi else []
                    )
                    + [["paper page", f"/papers/{src.ref_id}"]],
                },
            }
        )

    has_atoms = bool(bundle.conjunct_atoms)
    atom_xs = _spread(len(bundle.conjunct_atoms))
    for x, (atom_id, sentence) in zip(atom_xs, bundle.conjunct_atoms):
        atom_row = store.nanopub_publish_row(atom_id)
        nodes.append(
            {
                "id": f"fi{atom_id}",
                "cls": "atom",
                "x": x,
                "y": 40 + row_h,
                "label": sentence[:38],
                "sub": f"atom · {atom_row.state if atom_row else 'unminted'}",
                "detail": {
                    "kind": "atomic claim",
                    "title": sentence,
                    "fields": [
                        ["publish state", atom_row.state if atom_row else "unminted"],
                        ["trusty", (atom_row.trusty_uri or "—") if atom_row else "—"],
                    ],
                    "links": [["review page", f"/nanopub/fi{atom_id}"]],
                },
            }
        )
        edges.append(
            {"src": f"fi{atom_id}", "dst": "hub", "label": "conjunct-of", "cls": ""}
        )

    hub_y = 40 + row_h * (2 if has_atoms else 1)
    state = row.state if row else "unminted"
    nodes.append(
        {
            "id": "hub",
            "cls": "hub" + (" disputed" if bundle.contradicts else ""),
            "x": width // 2,
            "y": hub_y,
            "label": bundle.sentence[:44],
            "sub": f"{bundle.artifact_type} · {state}"
            + (" · ⚠ DISPUTED" if bundle.contradicts else ""),
            "detail": {
                "kind": f"{bundle.artifact_type} hub",
                "title": bundle.sentence,
                "fields": [
                    ["publish state", state],
                    ["aida", (row.aida_uri or "—") if row else "—"],
                    ["trusty", (row.trusty_uri or "—") if row else "—"],
                ],
                "links": [["claim page", f"/claim/fi{bundle.hub_ref_id}"]],
            },
        }
    )
    for src in papers:
        edges.append(
            {
                "src": f"pc{src.ref_id}",
                # Evidence edges land on the atoms' hub only when there
                # are no atoms; with atoms, papers ground the atoms —
                # but inbound edges are stored per-hub, so draw to hub.
                "dst": "hub",
                "label": src.role,
                "cls": "contradicts" if src.role == "contradicts" else "",
            }
        )

    if row and row.batch_id is not None:
        nodes.append(
            {
                "id": "ots",
                "cls": "anchor",
                "x": width // 2,
                "y": hub_y + row_h,
                "label": f"OTS batch {row.batch_id}",
                "sub": "Merkle leaf → daily root → Bitcoin",
                "detail": {
                    "kind": "timestamp anchor",
                    "title": f"OTS batch {row.batch_id}",
                    "fields": [["batch", str(row.batch_id)]],
                    "links": [],
                },
            }
        )
        edges.append({"src": "hub", "dst": "ots", "label": "leaf", "cls": "merkle"})

    # Resolve edge endpoints from node centers (nodes are 200×56 rects
    # centered on x): source bottom edge → destination top edge.
    pos = {n["id"]: (n["x"], n["y"]) for n in nodes}
    drawn = []
    for e in edges:
        if e["src"] not in pos or e["dst"] not in pos:
            continue
        sx, sy = pos[e["src"]]
        dx, dy = pos[e["dst"]]
        drawn.append({**e, "x1": sx, "y1": sy + 28, "x2": dx, "y2": dy - 30})

    height = (max(n["y"] for n in nodes) if nodes else 40) + 90
    return {
        "nodes": nodes,
        "edges": drawn,
        "width": width,
        "height": height,
        # Rendered with Jinja's |tojson (script-safe escaping of </, <, >,
        # &) — never json.dumps + |safe: titles/DOIs are DB content.
        "detail": {n["id"]: n["detail"] for n in nodes},
    }
