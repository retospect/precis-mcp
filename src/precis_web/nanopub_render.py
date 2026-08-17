"""The review-and-sign context for one claim hub — state header + frozen
ladder, dispute panel, publish-row panel, one action per state, withheld
evidence + sign-off doors, the approve-form prefill, and the DAG.

Moved out of ``routes/nanopub.py`` in the nanopub-light-up UX
consolidation: the reader evidence page (``/claim/fi<id>``,
:mod:`precis_web.routes.claim`) and the review-and-sign surface used to be
two pages sharing one hub. They're now one page — :func:`hub_context` is
the shared assembly both :func:`precis_web.routes.claim.claim_page_context`
(the merged GET) and ``routes/nanopub.py``'s approve-error re-render (the
one POST door that still re-renders a full page on a gate refusal) call,
so the review section renders identically wherever it appears. Living here
(not in either routes module) means ``routes/nanopub.py`` can import
:func:`~precis_web.routes.claim.claim_page_context` from ``routes/claim.py``
without a routes-module import cycle back the other way.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: One action per publish state (the state → next-transition map the
#: action box renders from).
_STATE_ACTION = {
    None: ("approve", "Approve (freeze this exact string)"),
    "candidate": ("approve", "Approve (freeze this exact string)"),
    "reviewed": ("sign", "Sign"),
    "signed": ("reopen", "Reopen (discard artifact pointer, re-mint)"),
    "anchored": ("publish-cli", "Publish via CLI (point of no return)"),
    "published": (None, "Published — change = supersede/retract"),
}


def hub_context(store: Any, hub_id: int) -> dict[str, Any] | None:
    """Assemble the review-and-sign context for claim hub ``hub_id``, or
    ``None`` when it isn't a live ``TAPROOT:claim`` hub. See the module
    docstring — the caller merges this under one namespaced context key
    (``ctx['np']``) rather than splatting it flat, so its keys can never
    silently shadow the reader-evidence context's own."""
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
        "suggested_payload": _suggested_payload(store, row, bundle),
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


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _suggest_quote_snip(store: Any, chunk: Any, claim: str) -> tuple[str, str]:
    """A gate-passing starting point from the grounding chunk: the
    citation-marker-free sentence most lexically relevant to the claim as
    the quote candidate (the reviewer trims it to the assertion —
    freeze-at-review still means a human decides exactly what the
    signature covers), and a snip validated unique-within-paper with the
    same helpers the mint gates run. Newlines split too, and ``**`` spans
    are disqualified outright — both keep markdown heading residue
    ("Introduction**\\n\\nThe debate…") out of the candidate pool."""
    from precis.nanopub import evidence as ev
    from precis.nanopub import snip as sniplib

    claim_tokens = set(sniplib.tokens(claim))
    candidates = [
        s.strip()
        for s in _SENTENCE_SPLIT.split(chunk.text or "")
        if len(sniplib.tokens(s)) >= 6 and "**" not in s and not ev.citation_markers(s)
    ]
    quote = (
        max(
            candidates,
            key=lambda s: (
                len(claim_tokens & set(sniplib.tokens(s))),
                len(sniplib.tokens(s)),
            ),
        )
        if candidates
        else (chunk.text or "").strip()
    )
    haystacks = [c.text for c in ev.paper_body_chunks(store, chunk.ref_id)]
    toks = sniplib.tokens(quote)
    snip = ""
    for i in range(max(1, len(toks) - 7)):
        candidate = " ".join(toks[i : i + 8])
        if sniplib.count_matches(candidate, haystacks) == 1:
            snip = candidate
            break
    return quote, snip


def _suggested_payload(store: Any, row: Any, bundle: Any) -> str:
    """The approve form's prefill: the frozen payload when one exists,
    else per-passage candidates derived from the grounding chunks —
    quote + unique snip suggested, for the reviewer to trim and attest."""
    if row is not None and row.grounding:
        return json.dumps(row.grounding, indent=2)
    by_ref = {s.ref_id: s for s in bundle.sources}
    passages = []
    for chunk in bundle.grounding_chunks:
        src = by_ref.get(chunk.ref_id)
        if src is None:
            continue
        quote, snip = _suggest_quote_snip(
            store, chunk, f"{bundle.sentence} {bundle.body}"
        )
        passages.append(
            {
                "doi": src.doi or "",
                "pdf_sha256": src.pdf_sha256 or "",
                "quote": quote,
                "snip": snip,
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
                    "links": [["claim page", f"/claim/fi{atom_id}"]],
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
                # No aida field: the URI is just the sentence URL-encoded —
                # unreadable here; the publish-row panel has a copy button.
                "fields": [
                    ["publish state", state],
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
