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
    from precis.handlers._finding_hypothesis import (
        ARTIFACT_HYPOTHESIS,
        META_ARTIFACT_TYPE,
    )
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
    # `load_bundle` fetches this same ref internally but doesn't carry its
    # meta out on the bundle (`bundle.artifact_type` is claim/compound only —
    # see `_suggested_payload`/`_graph`), so one extra fetch here is what
    # both the prefill and the state-header branches need.
    hub_ref = store.fetch_refs_by_ids([hub_id]).get(hub_id)
    hub_meta = (hub_ref.meta or {}) if hub_ref is not None else {}
    # A hypothesis never has a `claim`/`compound` bundle.artifact_type
    # (load_bundle can only ever set one of those) — the durable meta marker
    # is what actually says "hypothesis", and a publish row's own frozen
    # artifact_type (set at approve) always wins once one exists.
    display_artifact_type = (
        row.artifact_type
        if row is not None
        else (
            ARTIFACT_HYPOTHESIS
            if hub_meta.get(META_ARTIFACT_TYPE) == ARTIFACT_HYPOTHESIS
            else bundle.artifact_type
        )
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
    suggested_payload = _suggested_payload(store, row, bundle, hub_meta)
    # Pre-approve (unminted/candidate): the mint gates haven't run for
    # real yet, but they are pure reads — dry-run them against the live
    # sentence + the prefilled grounding so the gates panel shows how the
    # claim stacks up NOW, not a wall of "pending".
    dryrun = (
        _mint_dryrun(store, hub_id, bundle, hub_meta, suggested_payload)
        if state in (None, "candidate")
        else None
    )
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
        "suggested_payload": suggested_payload,
        "graph": _graph(store, bundle, row, display_artifact_type),
        "ladder": _ladder(state, row, disputed=disputed),
        "gates": _gate_report(state, preflight, dryrun=dryrun),
    }


def _mint_dryrun(
    store: Any,
    hub_id: int,
    bundle: Any,
    hub_meta: dict[str, Any],
    payload_json: str,
) -> dict[str, Any] | None:
    """Read-only rehearsal of the Layer-A mint gates for a pre-approve
    hub: the same :func:`precis.nanopub.gates.run_mint_gates` call approve
    makes, against the live sentence and the approve form's prefilled
    payload (what a reviewer clicking Approve right now would submit).
    Returns ``{"violations": {gate: [messages]}, "advisories": [...]}``
    plus the same pre-reword ``provenance_body`` snapshot approve reads —
    or ``None`` when the prefill doesn't parse (the panel then degrades
    back to "pending", never a 500)."""
    from precis.nanopub import evidence, gates

    try:
        payload = json.loads(payload_json or "{}")
        if not isinstance(payload, dict):
            return None
    except json.JSONDecodeError:
        return None
    violations: dict[str, list[str]] = {}
    for v in gates.run_mint_gates(
        store,
        bundle,
        payload,
        hub_meta=hub_meta,
        provenance_body=evidence.hub_body(store, hub_id),
    ):
        violations.setdefault(v.gate, []).append(v.message)
    artifact_type = gates.resolve_artifact_type(bundle, payload)
    return {
        "violations": violations,
        "advisories": gates.advisory_lint(bundle.sentence, artifact_type=artifact_type),
    }


#: The maturity ladder, left → right, with the "what happened here / what
#: was checked" hover text the claim page's stepper renders. Each rung's
#: tip describes the checks that gated ENTERING it — hovering a lit rung
#: answers "what has been verified so far".
_LADDER: list[tuple[str, str]] = [
    (
        "candidate",
        "Entered the publish pipeline: the chase built and verified its "
        "evidence upstream (grounding passages pinned, refine verification "
        "on the edges). Nothing is frozen yet — the sentence is still "
        "editable, and the Gates panel below shows a live dry-run of every "
        "check approve will make.",
    ),
    (
        "reviewed",
        "Approved by a human: the exact claim sentence was frozen "
        "(sha-pinned) and EVERY Layer-A mint gate ran and passed at that "
        "moment — grounding, verbatim quotes, primary source in corpus, "
        "sentence lint, no contradicting edge (see the gates list below).",
    ),
    (
        "signed",
        "Signed: the artifact bytes were minted and cryptographically "
        "signed — immutable in the append-only proof store from here on.",
    ),
    (
        "anchored",
        "Anchored: an OpenTimestamps batch committed the signature's hash "
        "to the Bitcoin timeline — the artifact provably existed by then.",
    ),
    (
        "published",
        "Published to the nanopub registry — public and immutable forever; "
        "a change is a supersede or retract, never an edit.",
    ),
]


def _ladder(state: str | None, row: Any, *, disputed: bool) -> list[dict[str, Any]]:
    """The left-to-right flow-graph steps for the claim's maturity. Each
    step: ``name``, ``tip`` (hover: what was checked/what happened),
    ``done`` (rung climbed), ``current``. An unminted hub lights nothing;
    a disputed hub carries ``blocked`` on its current rung (no forward
    transition while the contradicts edge stands)."""
    names = [n for n, _ in _LADDER]
    idx = names.index(state) if state in names else -1
    when = (
        row.updated_at.strftime("%Y-%m-%d %H:%M")
        if row is not None and row.updated_at
        else None
    )
    steps = []
    for i, (name, tip) in enumerate(_LADDER):
        current = i == idx
        if current and when:
            tip = f"{tip} In this state since {when}Z."
        if current and disputed:
            tip = f"{tip} BLOCKED: a live contradicts edge stands — adjudicate first."
        steps.append(
            {
                "name": name,
                "tip": tip,
                "done": i <= idx,
                "current": current,
                "blocked": current and disputed,
            }
        )
    return steps


#: Layer-A mint gates (``precis.nanopub.gates``) — the mechanical checks
#: ``approve`` runs against the exact sentence+payload it freezes. One
#: (slug, what-passing-means) line per gate; every gate listed here PASSED
#: for any hub whose state is reviewed or beyond (approve refuses
#: otherwise — the queue only holds strings that can mint).
_MINT_GATES: list[tuple[str, str]] = [
    # Phrased as an at-approve statement: a dispute can arrive AFTER the
    # freeze, and then the ladder + the preflight "contradicts" row (the
    # live re-check) show blocked while this row stays truthfully ✓.
    ("contradicts", "no unresolved contradicting edge stood at approve"),
    (
        "primary-source",
        "the primary source is in the corpus — not hearsay from a citing paper",
    ),
    (
        "claim-sentence",
        "the sentence passes the admissibility/grammar lint (blocking subset)",
    ),
    ("schema-lint", "the grounding payload matches the envelope schema"),
    ("grounding", "grounded passages carry DOI + verbatim quote + snip"),
    ("quote-verbatim", "each quote is found verbatim in its pinned source chunk"),
    ("snip", "each snip is a well-formed subrange of its quote"),
    ("field-containment", "structured field values appear inside a quoted passage"),
    ("quantity-bound", "quantities sit within the vocabulary's physical bounds"),
    ("pdf-sha", "each passage pins the exact source PDF by sha256"),
    ("llm-attribution", "an agent-prepared payload names its authoring model(s)"),
    ("compound-shape", "a compound cites its conjunct atoms' artifacts, not papers"),
    ("mint-order", "conjunct atoms carry signed artifacts before their compound mints"),
    ("rejected-memo", "the sentence is not a previously rejected claim string"),
]

#: Publish-time preflight checks (``precis.nanopub.preflight``) — what
#: must hold for the registry POST. Slugs mirror ``PreflightIssue.check``;
#: a check absent from the live issue list passed.
_PREFLIGHT_CHECKS: list[tuple[str, str]] = [
    ("state", "the state machine reached the publish door (anchored)"),
    ("hanging", "not a hanging claim — a grounded passage exists"),
    ("contradicts", "still no unresolved dispute at publish time"),
    ("drift", "the live hub sentence still hashes to the frozen sha"),
    ("withheld-edge", "every evidence edge is refine-verified or human-signed-off"),
    ("dependency-drift", "dependency artifacts are unchanged since this one signed"),
    ("dependency-unpublished", "every dependency artifact is already published"),
    ("trust", "signer + key fingerprint are allowlisted and attesting"),
    ("ots-pending", "the OTS proof reached a Bitcoin attestation"),
]

#: States that imply every ``_MINT_GATES`` entry passed — approve refuses
#: on any violation, so a row past candidate mechanically cleared them
#: all. (``drift`` is deliberately absent from the mint list: it can only
#: fire after a freeze, so it reports under the preflight group.)
_GATES_PASSED_STATES = ("reviewed", "signed", "anchored", "published")


def _gate_report(
    state: str | None,
    preflight: list[Any],
    *,
    dryrun: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Every gate the claim faces, with what its status IS — not just the
    failures. ``mint`` gates all passed the moment approve succeeded
    (state ≥ reviewed); before that, ``dryrun`` (:func:`_mint_dryrun`)
    supplies a live rehearsal — per-gate passing/failing against the
    current sentence + prefilled grounding, with the advisory lint codes
    riding the claim-sentence row as a note ("passed, with
    considerations") — and only a missing/unparseable dry-run degrades to
    "pending". ``preflight`` checks read the live issue list: a slug with
    a blocking issue failed, a non-blocking one is a note, anything else
    passed (or is pending while the hub is unminted/candidate)."""
    minted = state in _GATES_PASSED_STATES
    issues_by_check: dict[str, Any] = {}
    for i in preflight:
        issues_by_check.setdefault(i.check, i)
    mint: list[dict[str, Any]] = []
    if minted or dryrun is None:
        mint = [
            {
                "name": name,
                "desc": desc,
                "status": "passed" if minted else "pending",
                "message": None,
            }
            for name, desc in _MINT_GATES
        ]
    else:
        violations: dict[str, list[str]] = dryrun["violations"]
        advisories: list[str] = dryrun["advisories"]
        for name, desc in _MINT_GATES:
            broke = violations.get(name)
            if broke:
                extra = f" (+{len(broke) - 1} more)" if len(broke) > 1 else ""
                status, message = "failed", broke[0] + extra
            elif name == "claim-sentence" and advisories:
                codes = ", ".join(w.split(":", 1)[0].strip() for w in advisories)
                status = "note"
                message = (
                    f"{desc} — passing, with {len(advisories)} advisory "
                    f"consideration(s): {codes}"
                )
            else:
                status, message = "passed", None
            mint.append(
                {"name": name, "desc": desc, "status": status, "message": message}
            )
        # A violation slug outside the vocabulary (a new gate) must not
        # vanish — append it raw rather than hide it.
        for name in violations:
            if name not in {n for n, _ in _MINT_GATES}:
                mint.append(
                    {
                        "name": name,
                        "desc": "",
                        "status": "failed",
                        "message": violations[name][0],
                    }
                )
    pre = []
    for name, desc in _PREFLIGHT_CHECKS:
        issue = issues_by_check.get(name)
        if issue is not None:
            status = "failed" if issue.blocking else "note"
            message = issue.message
        elif state in (None, "unminted", "candidate"):
            status, message = "pending", None
        else:
            status, message = "passed", None
        pre.append({"name": name, "desc": desc, "status": status, "message": message})
    return {"mint": mint, "preflight": pre, "dryrun": not minted and dryrun is not None}


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


def _suggested_payload(
    store: Any, row: Any, bundle: Any, hub_meta: dict[str, Any]
) -> str:
    """The approve form's prefill: the frozen payload when one exists;
    else the prepared payload an agent's hypothesis proposal parked on
    ``refs.meta`` (`handlers/_finding_hypothesis.py::META_PROPOSED_PAYLOAD`),
    so a human opening a proposed hub finds the form already filled in;
    else per-passage candidates derived from the grounding chunks — quote +
    unique snip suggested, for the reviewer to trim and attest."""
    if row is not None and row.grounding:
        return json.dumps(row.grounding, indent=2)
    from precis.handlers._finding_hypothesis import META_PROPOSED_PAYLOAD

    proposed = hub_meta.get(META_PROPOSED_PAYLOAD)
    if proposed is not None:
        return json.dumps(proposed, indent=2)
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


def _graph(
    store: Any, bundle: Any, row: Any, display_artifact_type: str
) -> dict[str, Any]:
    """The per-hub neighborhood as positioned SVG nodes + edges (layered:
    papers → atoms → hub → anchor), with a detail dict per node for the
    click pane — the viewer.html prototype's NODES shape, served live.

    ``display_artifact_type`` labels the hub node instead of
    ``bundle.artifact_type``: :func:`~precis.nanopub.evidence.load_bundle`
    can only ever set the bundle's own field to ``claim``/``compound``, so
    it alone can never say ``hypothesis`` — see :func:`hub_context`."""
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
            "sub": f"{display_artifact_type} · {state}"
            + (" · ⚠ DISPUTED" if bundle.contradicts else ""),
            "detail": {
                "kind": f"{display_artifact_type} hub",
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
