"""The review-and-sign context for one claim hub — state header + frozen
ladder, contradicted/open-questions panels, publish-row panel, one action
per state, withheld evidence + sign-off doors, the approve-form prefill,
and the DAG. D1 (docs/backlog/disputes-edge-nonblocking-disagreement.md):
"blocked" and "contradicted" key on a live ``contradicts`` edge (either
direction, any counterpart kind — :func:`~precis.nanopub.evidence.
live_contradicts`); "open questions" is the non-blocking ``disputes``
complement (:func:`~precis.nanopub.evidence.open_disputes`) — never the
red banner, never a demerit.

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
import logging
import re
import threading
from collections.abc import Callable
from typing import Any

from precis.taproot.canon import MergeCandidate, Verdict, dedup_judge, nearest_hubs
from precis.utils import handle_registry
from precis_web.timefmt import abs_ts, utc_date

log = logging.getLogger(__name__)

#: Injectable signatures for the "Nearest claims" panel (slice 3,
#: the read-for-question loop (skill precis-read-for-question)) — :func:`hub_context` defaults
#: to the real :func:`~precis.taproot.canon.nearest_hubs`/``dedup_judge``,
#: tests inject stubs so a unit test never opens a DB connection or an LLM
#: call.
NearestFn = Callable[..., list[MergeCandidate]]
JudgeFn = Callable[[str, str], Verdict]

#: "Nearest claims" panel size — five is plenty for an at-a-glance
#: same-claim check; the agent-facing ``view='similar'`` door
#: (``handlers/_finding_hub_mint.py``) uses a bigger k for a fuller scan.
_NEAREST_K = 5

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


def hub_context(
    store: Any,
    hub_id: int,
    *,
    embedder: Any = None,
    nearest_fn: NearestFn = nearest_hubs,
    judge_fn: JudgeFn = dedup_judge,
) -> dict[str, Any] | None:
    """Assemble the review-and-sign context for claim hub ``hub_id``, or
    ``None`` when it isn't a live ``TAPROOT:claim`` hub. See the module
    docstring — the caller merges this under one namespaced context key
    (``ctx['np']``) rather than splatting it flat, so its keys can never
    silently shadow the reader-evidence context's own.

    ``embedder``/``nearest_fn``/``judge_fn`` feed the "Nearest claims"
    panel (:func:`_nearest_claims`, slice 3 of docs/backlog/read-for-
    question-loop.md) — injected so a unit test can stub the ANN
    retrieval and the LLM verdict without a DB or model call. The caller
    (``routes/claim.py``'s ``claim_page_context``) is the one that
    resolves the real embedder off the runtime; ``hub_context`` itself
    never reaches for one."""
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
    # Gate parity (D1, docs/backlog/disputes-edge-nonblocking-
    # disagreement.md): the same definition `check_contradicts` blocks
    # mint on — a live `contradicts` edge touching the hub, either
    # direction, any counterpart kind — not `bundle.contradicts` (the
    # narrower inbound-paper-evidence-only shape the graph render below
    # still uses).
    contradicted = evidence.live_contradicts(store, hub_id)
    disputed = bool(contradicted)
    action, action_label = _STATE_ACTION.get(state, (None, ""))
    if disputed and action is not None:
        # No forward transition is offered while the edge stands — spec
        # publish-time gate #6; covers a contradicts edge arriving AFTER
        # anchoring too (the server-side gates refuse regardless).
        action, action_label = None, "Blocked — unresolved contradicts edge"

    withheld = withheld_edges(store, hub_id)
    preflight = publish_preflight(store, hub_id, row=row) if state is not None else []
    suggested_payload = _suggested_payload(store, row, bundle, hub_meta)
    open_disputes = _dispute_panel(store, hub_id)
    # Pre-approve (unminted/candidate) is also this hub's own eligibility
    # to be a merge WINNER (slice 3: a hub past 'candidate' has a frozen
    # identity — merging into it would retroactively re-identify a
    # reviewed/signed artifact, taproot-merge-mcp-surface.md) — shared by
    # the mint-gates dry-run below and the nearest-claims panel.
    pre_approve = state in (None, "candidate")
    # Pre-approve: the mint gates haven't run for real yet, but they are
    # pure reads — dry-run them against the live sentence + the prefilled
    # grounding so the gates panel shows how the claim stacks up NOW, not
    # a wall of "pending".
    dryrun = (
        _mint_dryrun(store, hub_id, bundle, hub_meta, suggested_payload)
        if pre_approve
        else None
    )
    # "Nearest claims" (slice 3): skip the ANN + per-row LLM judge entirely
    # once past candidate — the panel is hidden there anyway (frozen
    # identity, nothing to merge into), so there's no point spending the
    # judge calls.
    nearest, nearest_note = (
        _nearest_claims(
            store,
            hub_id,
            bundle.sentence,
            hub_meta.get("scope"),
            embedder=embedder,
            nearest_fn=nearest_fn,
            judge_fn=judge_fn,
            own_mergeable=pre_approve,
        )
        if pre_approve
        else ([], None)
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
        "contradicted": _contradicted_panel(contradicted),
        "disputes": open_disputes,
        "withheld": _withheld_rows(store, withheld),
        "preflight": preflight,
        "action": action,
        "action_label": action_label,
        "suggested_payload": suggested_payload,
        "graph": _graph(
            store,
            bundle,
            row,
            display_artifact_type,
            blocked=disputed,
            open_disputes_count=len(open_disputes),
        ),
        "ladder": _ladder(state, row, disputed=disputed),
        "gates": _gate_report(state, preflight, dryrun=dryrun),
        "nearest": nearest,
        "nearest_note": nearest_note,
    }


def _nearest_claims(
    store: Any,
    hub_ref_id: int,
    sentence: str,
    scope: Any,
    *,
    embedder: Any,
    nearest_fn: NearestFn,
    judge_fn: JudgeFn,
    own_mergeable: bool,
    k: int = _NEAREST_K,
) -> tuple[list[dict[str, Any]], str | None]:
    """The "Nearest claims" panel data (slice 3, docs/backlog/read-for-
    question-loop.md): the ``k`` nearest OTHER live claim hubs to this
    hub's own sentence (:func:`~precis.taproot.canon.nearest_hubs`, the
    same ANN retrieval the mint-door cascade and ``view='similar'`` use —
    :func:`~precis.handlers._finding_hub_mint.render_similar_view`
    excludes self the same way), each carrying the
    :func:`~precis.taproot.canon.dedup_judge` verdict against this hub's
    sentence, computed fresh on every render (no cache — this is one
    hub's page at a time, never a batch scan).

    ``can_merge`` on every row is this hub's OWN eligibility to be a merge
    winner (``own_mergeable``, the same 'pre-candidate' test the mint-gate
    dry-run above uses) — not a per-neighbour :func:`~precis.taproot.hub.
    merge_hubs` dry-run plan, which would cost one more DB round-trip per
    row just to show a button; the confirm POST computes the real,
    authoritative plan (including the *neighbour's* own eligibility)
    before writing anything.

    ``embedder is None`` (no query embedder configured on this runtime)
    degrades to an empty list + an explanatory note, never an exception —
    the panel is advisory, not a page-breaking dependency.
    """
    if embedder is None:
        return [], "nearest claims unavailable (no embedder)"
    candidates = nearest_fn(sentence, scope, store, embedder, k=k + 1)
    others = [c for c in candidates if c.hub_ref_id != hub_ref_id][:k]
    rows = [
        {
            "hub_ref_id": c.hub_ref_id,
            "handle": handle_registry.format_handle("finding", c.hub_ref_id),
            "claim": c.claim,
            "distance": c.distance,
            "verdict": (v := judge_fn(sentence, c.claim))["verdict"],
            "confidence": v["confidence"],
            "can_merge": own_mergeable,
        }
        for c in others
    ]
    return rows, None


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
    # gr245768: the grounding passage(s), when this hub has any resolved --
    # sharpens `all-caps-artifact` from allowlist-only to also clearing a
    # token genuinely capitalized in the source ("The GLYMPHATIC system...").
    # Only chunks of real evidence sources count — supporters AND
    # contradictors (`bundle.sources` + `bundle.contradicts` are both
    # restricted to evidence kinds; role is not the criterion, kind is). A
    # sibling finding's prose is not a source: letting it in cleared
    # `unsupported-term`'s `C60` for fi191121 out of another finding's text
    # (docs/backlog/claim-terms-absent-from-quotes.md).
    evidence_sources = [*bundle.sources, *bundle.contradicts]
    evidence_ref_ids = {s.ref_id for s in evidence_sources}
    evidence_chunks = [
        c for c in bundle.grounding_chunks if c.ref_id in evidence_ref_ids
    ]
    source_text = (
        "\n".join(c.text for c in evidence_chunks) if evidence_chunks else None
    )
    source_title = "\n".join(s.title for s in evidence_sources if s.title) or None
    return {
        "violations": violations,
        "advisories": gates.advisory_lint(
            bundle.sentence,
            artifact_type=artifact_type,
            source_text=source_text,
            source_title=source_title,
        ),
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
    when = abs_ts(row.updated_at) if row is not None else None
    steps = []
    for i, (name, tip) in enumerate(_LADDER):
        current = i == idx
        if current and when:
            tip = f"{tip} In this state since {when}."
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
    ("contradicts", "still no live contradicts edge at publish time"),
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
    ("Introduction**\n\nThe debate…") out of the candidate pool.

    Candidates are tried in relevance order until one yields a unique
    snip. Body chunks overlap at their seams (a chunk's first sentence is
    routinely the previous chunk's last), so the most relevant sentence
    can be one that occurs verbatim in two chunks — every 8-token window
    of it then matches twice and no snip exists inside it. Falling through
    to the next sentence keeps the prefill submittable; the old
    best-sentence-only pick handed the reviewer an empty snip that the
    gate refused on submit (first seen on fi191121, 2026-09-16). Only when
    no candidate carries a unique window does the top sentence go out with
    an empty snip."""
    from precis.nanopub import evidence as ev
    from precis.nanopub import snip as sniplib

    claim_tokens = set(sniplib.tokens(claim))
    candidates = [
        s.strip()
        for s in _SENTENCE_SPLIT.split(chunk.text or "")
        if len(sniplib.tokens(s)) >= 6 and "**" not in s and not ev.citation_markers(s)
    ]
    haystacks = [c.text for c in ev.paper_body_chunks(store, chunk.ref_id)]
    if not candidates:
        whole = (chunk.text or "").strip()
        return whole, _unique_snip(whole, haystacks)
    ranked = sorted(
        candidates,
        key=lambda s: (
            len(claim_tokens & set(sniplib.tokens(s))),
            len(sniplib.tokens(s)),
        ),
        reverse=True,
    )
    for quote in ranked:
        snip = _unique_snip(quote, haystacks)
        if snip:
            return quote, snip
    return ranked[0], ""


def _unique_snip(quote: str, haystacks: list[str]) -> str:
    """The first 8-token window of ``quote`` that occurs exactly once
    across ``haystacks`` (the paper's live body chunks), or ``""``."""
    from precis.nanopub import snip as sniplib

    toks = sniplib.tokens(quote)
    for i in range(max(1, len(toks) - 7)):
        candidate = " ".join(toks[i : i + 8])
        if sniplib.count_matches(candidate, haystacks) == 1:
            return candidate
    return ""


#: ref_ids whose lazy generation thread is still running. A prefill is
#: rendered on every approve-form AND read-only dry-run-panel view, so an
#: unguarded enqueue would spawn a fresh thread — and a fresh billed LLM
#: call — per refresh for the whole window before the sentence lands.
_CONTEXT_SENTENCE_INFLIGHT: set[int] = set()
_CONTEXT_SENTENCE_LOCK = threading.Lock()


def _run_context_sentence_enqueue(store: Any, ref_id: int) -> None:
    """The actual paper-context-sentence generation for one ref (``docs/
    backlog/paper-context-sentence.md``) — run off the request thread by
    :func:`_enqueue_context_sentence`. Swallows every error: this is
    best-effort background work, never something a page render depends on."""
    try:
        from precis.utils.llm.router import DispatchClient, Tier
        from precis.workers.context_sentence import run_context_sentence_pass

        client = DispatchClient(tier=Tier.SMALL, source="context_sentence")
        run_context_sentence_pass(store, client=client, ref_ids=[ref_id])
    except Exception:
        log.warning(
            "context_sentence: lazy enqueue failed for ref_id=%s",
            ref_id,
            exc_info=True,
        )


def _context_sentence_thread_body(store: Any, ref_id: int) -> None:
    """Thread target: do the work, then always release the in-flight slot.
    The release lives here rather than in :func:`_run_context_sentence_enqueue`
    so it still happens when a test monkeypatches that function out."""
    try:
        _run_context_sentence_enqueue(store, ref_id)
    finally:
        with _CONTEXT_SENTENCE_LOCK:
            _CONTEXT_SENTENCE_INFLIGHT.discard(ref_id)


def _enqueue_context_sentence(store: Any, ref_id: int) -> threading.Thread | None:
    """Fire-and-forget: generate + write the paper-context sentence for
    ``ref_id`` in a background thread — never blocks or raises into the
    prefill render. Returns ``None`` when a thread for this ref is already
    in flight (:data:`_CONTEXT_SENTENCE_INFLIGHT`), else the started thread
    (tests join it; callers otherwise ignore the return value)."""
    with _CONTEXT_SENTENCE_LOCK:
        if ref_id in _CONTEXT_SENTENCE_INFLIGHT:
            return None
        _CONTEXT_SENTENCE_INFLIGHT.add(ref_id)
    thread = threading.Thread(
        target=_context_sentence_thread_body,
        args=(store, ref_id),
        name=f"context-sentence-{ref_id}",
        daemon=True,
    )
    try:
        thread.start()
    except Exception:
        # A thread that never started will never run its finally-clause,
        # so release the slot here or this ref is wedged for the process.
        with _CONTEXT_SENTENCE_LOCK:
            _CONTEXT_SENTENCE_INFLIGHT.discard(ref_id)
        log.warning("context_sentence: thread start failed ref_id=%s", ref_id)
        return None
    return thread


def _lazy_enqueue_context_sentences(store: Any, sources: list[Any]) -> None:
    """Best-effort: for every grounding source paper in ``sources`` missing
    ``refs.meta['context_sentence']``, fire off the generation pass
    (:func:`_enqueue_context_sentence`) — the lazy half of the population
    policy (docs/backlog/paper-context-sentence.md); the batch-backfill
    half runs off ``context_sentence.backfill_candidate_ref_ids``. Never
    raises: a lookup hiccup just skips the enqueue for this render."""
    ref_ids = [s.ref_id for s in sources]
    if not ref_ids:
        return
    try:
        refs = store.fetch_refs_by_ids(ref_ids)
    except Exception:
        log.warning("context_sentence: lazy lookup failed", exc_info=True)
        return
    for ref_id in ref_ids:
        ref = refs.get(ref_id)
        meta = (getattr(ref, "meta", None) or {}) if ref is not None else {}
        if not meta.get("context_sentence"):
            _enqueue_context_sentence(store, ref_id)


def _suggested_payload(
    store: Any, row: Any, bundle: Any, hub_meta: dict[str, Any]
) -> str:
    """The approve form's prefill: the frozen payload when one exists;
    else the prepared payload an agent's hypothesis proposal parked on
    ``refs.meta`` (`handlers/_finding_hypothesis.py::META_PROPOSED_PAYLOAD`),
    so a human opening a proposed hub finds the form already filled in;
    else per-passage candidates derived from the grounding chunks — quote +
    unique snip suggested, for the reviewer to trim and attest.

    Also fires the lazy half of the paper-context-sentence population
    policy (:func:`_lazy_enqueue_context_sentences`) for every grounding
    source paper missing a sentence — best-effort, never blocking."""
    _lazy_enqueue_context_sentences(store, bundle.sources)
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


def _dispute_panel(store: Any, hub_ref_id: int) -> list[dict[str, Any]]:
    """The NON-blocking "open questions" surface (D1, docs/backlog/
    disputes-edge-nonblocking-disagreement.md): one entry per live
    `disputes` edge touching this hub, either direction — counterpart
    ref/kind/title, direction, and (when the edge names one) the
    disputing passage's text, resolved via ``ev.fetch_chunks`` so the
    reviewer sees the actual conflict without hunting (fi189542
    precedent, now generalized past the paper-only shape). Never the
    blocking banner's data — see :func:`_contradicted_panel` for that."""
    from precis.nanopub import evidence as ev

    edges = ev.open_disputes(store, hub_ref_id)
    if not edges:
        return []

    # A ``disputes`` edge is directed (filer's subject → the thing it
    # questions) and carries no inverse slug: the disputing passage sits
    # on whichever side names it — the filer's own pin (``src_chunk_id``)
    # when the edge points AT this hub (``direction == 'in'``), or the
    # target's pin (``dst_chunk_id``) when this hub is the one filing the
    # question (``direction == 'out'``). The automated writers
    # (``workers/hub_refine.py``) don't set the chunk columns at all —
    # their pointer is ``links.meta['source_handle']`` (``pc<id>``), same
    # as evidence edges — so that is the fallback pin.
    def _pin(e: Any) -> int | None:
        col = e.src_chunk_id if e.direction == "in" else e.dst_chunk_id
        if col is not None:
            return col
        handle = str((e.meta or {}).get("source_handle") or "")
        if handle.startswith("pc") and handle[2:].isdigit():
            return int(handle[2:])
        return None

    pins = {(e.ref_id, e.direction): _pin(e) for e in edges}
    chunk_ids = [cid for cid in pins.values() if cid is not None]
    chunks = (
        {c.chunk_id: c for c in ev.fetch_chunks(store, chunk_ids)} if chunk_ids else {}
    )
    out = []
    for e in edges:
        pin = pins[(e.ref_id, e.direction)]
        chunk = chunks.get(pin) if pin is not None else None
        out.append(
            {
                "ref_id": e.ref_id,
                "kind": e.kind,
                "title": e.title,
                "direction": e.direction,
                "passage": chunk.text if chunk is not None else "",
            }
        )
    return out


def _contradicted_panel(contradicted: list[Any]) -> list[dict[str, Any]]:
    """The BLOCKING banner's data: one entry per live ``contradicts``
    edge from :func:`~precis.nanopub.evidence.live_contradicts` — just
    enough to name the counterpart (kind, ref_id, direction); the panel
    exists to say "adjudicate this," not to relitigate the conflict, so
    it carries no passage."""
    return [
        {"ref_id": e.ref_id, "kind": e.kind, "title": e.title, "direction": e.direction}
        for e in contradicted
    ]


def _withheld_rows(store: Any, withheld: list[Any]) -> list[dict[str, Any]]:
    """:func:`~precis.nanopub.preflight.withheld_edges`' rows, enriched
    with the source paper's human-verification stamp
    (``refs.human_verified_at``/``_by``, the Meta tab's "Mark reviewed"
    sign-off) so the withheld-evidence list's paper titles carry the same
    ✓ every other paper-link surface shows (gr351830). One batched
    ``fetch_refs_by_ids`` over the distinct ``paper_ref_id`` set, not a
    per-row fetch."""
    paper_ids = {w.paper_ref_id for w in withheld}
    paper_refs = store.fetch_refs_by_ids(list(paper_ids)) if paper_ids else {}
    rows: list[dict[str, Any]] = []
    for w in withheld:
        paper_ref = paper_refs.get(w.paper_ref_id)
        rows.append(
            {
                "link_id": w.link_id,
                "paper_ref_id": w.paper_ref_id,
                "paper_title": w.paper_title,
                "relation": w.relation,
                "stale": w.stale,
                "chunk_handle": w.chunk_handle,
                "passage": w.passage,
                "reviewed_at": (
                    utc_date(getattr(paper_ref, "human_verified_at", None)) or None
                ),
                "reviewed_by": getattr(paper_ref, "human_verified_by", None) or None,
            }
        )
    return rows


def _graph(
    store: Any,
    bundle: Any,
    row: Any,
    display_artifact_type: str,
    *,
    blocked: bool,
    open_disputes_count: int = 0,
) -> dict[str, Any]:
    """The per-hub neighborhood as positioned SVG nodes + edges (layered:
    papers → atoms → hub → anchor), with a detail dict per node for the
    click pane — the viewer.html prototype's NODES shape, served live.

    ``display_artifact_type`` labels the hub node instead of
    ``bundle.artifact_type``: :func:`~precis.nanopub.evidence.load_bundle`
    can only ever set the bundle's own field to ``claim``/``compound``, so
    it alone can never say ``hypothesis`` — see :func:`hub_context`.

    ``blocked`` is the D1 gate-parity flag (live
    :func:`~precis.nanopub.evidence.live_contradicts`, either direction —
    see :func:`hub_context`'s ``disputed``), NOT ``bundle.contradicts``:
    the hub node's red outline and "⚠ CONTRADICTED" label key on it.
    ``open_disputes_count`` rides along as a non-red annotation only —
    open questions never touch the node's color."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    width = 940
    row_h = 118

    def _spread(n: int) -> list[int]:
        if n == 0:
            return []
        gap = width // (n + 1)
        return [gap * (i + 1) for i in range(n)]

    papers = list(bundle.sources) + list(bundle.contradicts)
    hub_paper_ids = {p.ref_id for p in papers}
    # A compound's grounding lives on its conjunct atoms (the compound-shape
    # gate: "a compound cites its conjunct atoms' artifacts, not papers"),
    # so the hub's own evidence list is empty by design and the papers row
    # rendered nothing (fi211522). Aggregate the atoms' evidence instead —
    # deduped across atoms — and remember which atom(s) each paper grounds
    # so its edge lands on the atom, not the hub.
    paper_atom_ids: dict[int, list[int]] = {}
    if bundle.conjunct_atoms:
        from precis.errors import BadInput
        from precis.nanopub import evidence as ev

        seen_paper_ids = {p.ref_id for p in papers}
        for atom_id, _sentence in bundle.conjunct_atoms:
            try:
                atom_bundle = ev.load_bundle(store, atom_id)
            except BadInput:
                continue
            for src in list(atom_bundle.sources) + list(atom_bundle.contradicts):
                paper_atom_ids.setdefault(src.ref_id, []).append(atom_id)
                if src.ref_id not in seen_paper_ids:
                    seen_paper_ids.add(src.ref_id)
                    papers.append(src)
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
            "cls": "hub" + (" disputed" if blocked else ""),
            "x": width // 2,
            "y": hub_y,
            "label": bundle.sentence[:44],
            "sub": f"{display_artifact_type} · {state}"
            + (" · ⚠ CONTRADICTED" if blocked else "")
            + (
                f" · open questions: {open_disputes_count}"
                if open_disputes_count
                else ""
            ),
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
        # A paper aggregated from the atoms draws its edge to each atom it
        # grounds; a paper on the hub's own evidence list keeps its hub edge
        # (both, when it grounds both).
        dsts = ["hub"] if src.ref_id in hub_paper_ids else []
        dsts += [f"fi{a}" for a in paper_atom_ids.get(src.ref_id, [])]
        for dst in dsts:
            edges.append(
                {
                    "src": f"pc{src.ref_id}",
                    "dst": dst,
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
