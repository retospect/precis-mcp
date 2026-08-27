"""Hypothesis-proposal mode for ``put(kind='finding', hypothesis=True, …)``.

A `hypothesis` is one of the three nanopub artifact types
(`nanopub/vocab.py::HYPOTHESIS`), and the only one an agent can honestly
originate. It asserts a conjecture, so it carries **no evidence**:
`nanopub/gates.py::run_mint_gates` rejects a hypothesis that arrives with
grounding passages — *"a hypothesis has no supporting passage by definition
(motivation, not evidence)"* — and demands `testable_by` (*"the
discriminating experiment is what separates a conjecture from vibes"*) plus
`motivation` prose naming the inferential leap.

That shape is exactly what a dream cycle produces when it lands on something
solid, and until now there was no way to write one down. The canonical worked
example is `docs/reference/nanopub-example/qi-hypothesis-scaled-switching.trig`,
minted from a compound that *failed* its commensurability gate: the transfer
was unproven but well-motivated, so it was restated as a typed Hypothesis. Its
own comment names the payoff — *"a signed, timestamped hypothesis is a priority
claim on an idea."*

**This door prepares; it never approves.** It mints the hub, writes the
motivation edges, and parks the prepared mint payload on `refs.meta` so the
`/claim/fi<id>` approve form comes pre-filled. `approve` / `sign` / `signoff`
/ `anchor` / `publish` stay human-only, unchanged (`cli/nanopub.py`, and the
`interactive=True` guard in `nanopub/mint.py::approve`). Nothing here creates
a `nanopub_publish` row.

Three guards stand in for the grounding invariant that `seed_claim_hub`
enforces for ordinary hubs (*"this door can never mint a thin-air hub"*).
The first runs **before** anything is written, because a proposing agent
cannot delete what it minted — a hub refused after the fact would sit in
the human queue forever:

* **The sentence lints clean, under HYPOTHESIS rules.** `gates.py::
  check_claim_sentence` is a pure function, so unlike the rest of
  `run_mint_gates` (which needs an evidence bundle, and therefore a hub) it
  can run pre-mint. The call passes `artifact_type=ARTIFACT_HYPOTHESIS`, so
  the epistemic pair does not apply (`gates.py::
  _ARTIFACT_LINT_EXEMPTIONS`) — a conjecture has no measurement to name,
  and `testable_by=` carries the discriminating experiment instead. Passing
  nothing would silently re-impose the strict `claim` set, which is the
  default. The remaining mint gates are satisfied here by construction — no
  passages, no fields — so this is the whole of what can fail.

* **≥2 motivators across ≥2 distinct independent sources.** A source is a
  distinct source paper (a claim hub motivator resolves through its live
  evidence edges) or a distinct `structure` ref, counted directly — a
  measured structure is its own observation. A conjecture that leaps from a
  single source is a restatement of that source, not a cross-binding, and
  the whole point of the type is the unearned transfer between two things.
* **No hub that already carries a publish row.** `nanopub_reopen` clears
  `grounding` but keeps `artifact_type`, so a hub that ever held a `claim`
  row would assemble as an `AtomicClaim` — silently dropping `testableBy`,
  `motivatedBy` and `motivation` from the signed TriG while the gates
  applied hypothesis rules to it.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.errors import BadInput
from precis.response import Response
from precis.store import Store
from precis.store.types import Tag
from precis.taproot import hub as hublib
from precis.taproot.canon import CanonicalClaim
from precis.utils import handle_registry

log = logging.getLogger(__name__)

#: Open tag marking a hub as awaiting human triage of an agent-authored
#: hypothesis. Lowercase/open, matching the `needs-triage` (paper) and
#: `needs-experiment` (quest graduation) convention — the closed `TAPROOT`
#: axis is `select: one` over `{claim, review}` and cannot take a third
#: value without the reclassifier evicting it.
PROPOSED_TAG = "hypothesis-proposed"

#: `refs.meta` key holding the prepared `nanopub approve` payload. Read by
#: `precis_web/nanopub_render.py::_suggested_payload` to pre-fill the approve
#: form, and by the `view='mint-preflight'` door as its default payload.
META_PROPOSED_PAYLOAD = "proposed_payload"

#: `refs.meta` key recording that this hub asserts a conjecture, not a
#: finding. The *durable* marker — :data:`PROPOSED_TAG` is dropped once a
#: human triages the proposal, and `nanopub_publish.artifact_type` only
#: exists after approve, but a hypothesis is a hypothesis from the moment
#: it is minted. `taproot/canon.py::hypothesis_hub_predicate_sql` reads it,
#: which is what keeps `hub_refine`/`chase_trigger` from widening a guess.
META_ARTIFACT_TYPE = "artifact_type"

#: Its value. Matches `nanopub_publish.artifact_type` / the
#: `nanopub/assemble.py` vocabulary, so the two agree on the word.
ARTIFACT_HYPOTHESIS = "hypothesis"

#: Minimum distinct independent sources (papers and/or structures) behind a
#: proposal — see the module docstring's first guard.
MIN_SOURCE_PAPERS = 2


def _resolve_motivator(store: Store, token: str) -> tuple[int, str | None]:
    """``(ref_id, source_handle)`` for one motivator token.

    Accepts a ref handle (``pa5``, ``pt9``, ``fi1234``) or a chunk handle
    (``pc293``). A chunk handle keeps its token as ``source_handle`` so
    :func:`precis.taproot.hub.attach_motivation` can ground the edge at that
    passage; a ref handle grounds ref-level.
    """
    resolved = store.resolve_handle(token)
    if resolved is None:
        raise BadInput(
            f"motivator {token!r} doesn't resolve to a live ref",
            next=(
                "name papers/patents/claim hubs by universal handle — pa5, "
                "pt9, fi1234, or a passage as pc293"
            ),
        )
    # A chunk handle keeps its token as `source_handle` so `attach_motivation`
    # grounds the edge at that passage; a record handle grounds ref-level.
    if resolved.chunk_id is not None:
        return int(resolved.ref_id), token
    return int(resolved.ref_id), None


def _source_papers(store: Store, ref_id: int, kind: str) -> set[int]:
    """The distinct source papers a motivator stands on.

    A paper/patent is its own source. A claim hub contributes the papers its
    live evidence edges come from, so "two hubs grounded in the same single
    paper" correctly reads as one source rather than two — that pair is a
    restatement, not the cross-binding the hypothesis type is for.
    """
    if kind in ("paper", "patent"):
        return {ref_id}
    with store.pool.connection() as conn:
        return {h.src_ref_id for h in hublib.live_evidence_handles(conn, ref_id)}


def put_hypothesis(
    store: Store,
    *,
    sentence: str,
    scope: dict[str, Any] | None,
    motivation: str | None,
    testable_by: str | None,
    motivated_by: list[str] | None,
    llm_models: list[str] | None = None,
    from_memory: str | None = None,
    set_by: str = "agent",
) -> Response:
    """Mint a hypothesis claim hub and park its prepared mint payload."""
    # Local import: `nanopub.gates` pulls the evidence/mint stack, which
    # imports handlers back. Same reason `_finding_mint_preflight` does it.
    from precis.nanopub.gates import check_claim_sentence

    sentence = (sentence or "").strip()
    if not sentence:
        raise BadInput(
            "a hypothesis needs its claim sentence — pass title=<sentence>.",
            next=(
                "the sentence is read alone, years later: it needs an evidence "
                "verb (predicts/shows/measures/observes/demonstrates/finds) AND "
                "the technique that would test it (DFT, Raman, TEM, …) — see "
                "get(kind='skill', id='precis-nanopub-help')"
            ),
        )
    if not (motivation or "").strip():
        raise BadInput(
            "a hypothesis needs motivation= prose naming the inferential leap.",
            next="motivation='Both systems attribute X to the same mechanism; "
            "the transfer to Y is untested.'",
        )
    if not (testable_by or "").strip():
        raise BadInput(
            "a hypothesis needs testable_by= — the discriminating experiment "
            "is what separates a conjecture from vibes.",
            next="testable_by='conductance modulation under electrode "
            "displacement in junctions of …'",
        )
    # Required at THIS door, not just at the sign-time gate, because the
    # proposing agent is the only party who knows what model authored the
    # conjecture — a human hitting the llm-attribution refusal at approve
    # has no way to recover the id (fi211520 shipped unattributed exactly
    # this way).
    models = [str(m).strip() for m in llm_models or [] if str(m).strip()]
    if not models:
        raise BadInput(
            "a proposed hypothesis needs llm_models= naming the authoring "
            "model id(s) — an agent-prepared artifact attributes its "
            "machine author.",
            next="llm_models=['claude-fable-5'] — the model id you are "
            "running as; add any other model that co-authored the sentence",
        )
    if scope is not None and not isinstance(scope, dict):
        raise BadInput(
            f"scope must be a dict, got {type(scope).__name__}",
            next="scope={'material': 'graphene', ...}",
        )

    tokens = list(motivated_by or [])
    if len(tokens) < MIN_SOURCE_PAPERS:
        raise BadInput(
            f"a hypothesis needs motivated_by= naming at least "
            f"{MIN_SOURCE_PAPERS} artifacts, got {len(tokens)}",
            next=(
                "a conjecture that leaps from one source is a restatement of "
                "that source — name the two things whose binding is unearned, "
                "e.g. motivated_by=['pc293', 'fi1234']"
            ),
        )

    resolved: list[tuple[int, str | None]] = []
    papers: set[int] = set()
    seen: set[int] = set()
    for token in tokens:
        ref_id, source_handle = _resolve_motivator(store, token)
        ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
        if ref is None or ref.kind not in hublib.MOTIVATION_SRC_KINDS:
            kind_desc = "unknown" if ref is None else ref.kind
            raise BadInput(
                f"motivator {token!r} is a {kind_desc!r} ref",
                options=sorted(hublib.MOTIVATION_SRC_KINDS),
                next=(
                    "a hypothesis is motivated by a paper, a patent, another "
                    "claim hub, or a measured structure (an instrument "
                    "observation) — a memory is something you thought with, "
                    "not a source you can cite"
                ),
            )
        if ref_id not in seen:
            seen.add(ref_id)
            resolved.append((ref_id, source_handle))
        if ref.kind == "structure":
            # A structure is its own distinct source — counted directly by
            # ref id, not resolved through evidence edges like a claim hub.
            papers.add(ref_id)
        else:
            papers |= _source_papers(store, ref_id, ref.kind)

    if len(papers) < MIN_SOURCE_PAPERS:
        raise BadInput(
            f"motivated_by= resolves to {len(papers)} distinct source paper(s), "
            f"need {MIN_SOURCE_PAPERS}",
            next=(
                "two claim hubs grounded in the same paper are one source, not "
                "two — the type exists for a binding between separate findings "
                "(a measured structure also counts as its own source)"
            ),
        )

    # Lint the sentence BEFORE minting. `run_mint_gates` needs an evidence
    # bundle and so can only run on a hub that already exists — but the
    # sentence lint is a pure function, and it is the gate that actually
    # bites (`no-epistemic-mode` alone blocks 1,419 of 1,524 live hubs).
    # Minting first and checking after would strand a hub the proposer has
    # no permission to delete, sitting in the human queue tagged
    # `hypothesis-proposed` forever. The remaining mint gates are satisfied
    # by construction here (no passages, no fields), so this is the whole
    # of the check that can fail.
    #
    # `artifact_type` must be passed: the blocking set is scoped by it, and
    # the default is the strict `claim` set. Without it this door demanded
    # the epistemic pair from a conjecture — the category error td244962
    # names, and the reason the old `next=` hint here told proposers to
    # name "the technique that would test it" inside the sentence. That
    # experiment belongs in `testable_by=`, which is separately mandatory.
    lint = check_claim_sentence(sentence, artifact_type=ARTIFACT_HYPOTHESIS)
    if lint:
        raise BadInput(
            "the claim sentence fails the mint gates:\n"
            + "\n".join(f"  - [{v.gate}] {v.message}" for v in lint),
            next=(
                "reword and re-propose — nothing was written. A hypothesis "
                "is exempt from the epistemic pair (no measurement exists "
                "yet — name the discriminating experiment in testable_by= "
                "instead), but the admissibility, grammar and notation "
                "rules still apply: one falsifiable assertion, no author "
                "names, no dangling reference, terminal period, UTF-8 canon"
            ),
        )

    claim = CanonicalClaim(sentence=sentence, scope=dict(scope or {}))
    payload: dict[str, Any] = {
        "hypothesis": True,
        # A hypothesis has no supporting passage by definition, and with no
        # passages any structured field trips the gates' field-containment
        # check (containment is verified against quotes that don't exist).
        "passages": [],
        "fields": {},
        "motivation": (motivation or "").strip(),
        "testable_by": (testable_by or "").strip(),
        # `mint.py::_mint_input` requires every motivated_by_refs entry to
        # already carry a SIGNED artifact, so this stays empty and the handles
        # ride as a hint the reviewer promotes once its motivators are signed.
        "motivated_by_refs": [],
        "motivated_by_hint": list(tokens),
        # Frozen into `grounding` at approve, folded into the pubinfo
        # software node at sign; the llm-attribution gate refuses an
        # agent-parked payload without it.
        "llm_models": models,
    }

    # `mint_hub` converges on the sentence's pub_id, and applies `extra_meta`
    # only on a real insert — so re-proposing the same conjecture is a no-op
    # rather than an overwrite. That is the behaviour we want: the parked
    # payload may already be open in front of a reviewer, and a re-run must
    # not rewrite what they are reading. Reword the sentence to propose
    # something genuinely different.
    hub_ref_id = hublib.mint_hub(
        store,
        claim,
        set_by=set_by,  # type: ignore[arg-type]
        extra_meta={
            META_ARTIFACT_TYPE: ARTIFACT_HYPOTHESIS,
            META_PROPOSED_PAYLOAD: payload,
        },
    )

    # Converged onto a hub that is already in the publish pipeline: refuse
    # before writing anything else. `nanopub_reopen` keeps `artifact_type`,
    # so re-typing an existing claim row as a hypothesis assembles the wrong
    # artifact (see the module docstring's second guard).
    existing_row = store.nanopub_publish_row(hub_ref_id)
    if existing_row is not None:
        raise BadInput(
            f"fi{hub_ref_id} already has a nanopub publish row "
            f"({existing_row.state}) as artifact_type="
            f"{existing_row.artifact_type!r}",
            next=(
                "a hub's artifact type is frozen at its first publish row and "
                "survives reopen — reword the hypothesis into its own hub"
            ),
        )

    # Converged onto a hub that is not ours. `mint_hub`'s pub_id is derived
    # from sentence+scope alone, and on a collision it returns the existing
    # hub WITHOUT applying `extra_meta` — the same property that makes a
    # re-proposal a safe no-op makes a collision with an ordinary claim hub
    # silent. Without this check we would hang motivation edges and the
    # `hypothesis-proposed` triage tag on somebody's real, evidence-bearing
    # claim, drop it into the human queue mislabelled, and still not set the
    # marker (so the approve form would prefill from passage candidates as
    # if nothing had happened). Checking the marker rather than the evidence
    # edges catches the evidence-free collision too, and lets a genuine
    # re-proposal of our own hypothesis through.
    hub_ref = store.fetch_refs_by_ids([hub_ref_id]).get(hub_ref_id)
    if hub_ref is None or (hub_ref.meta or {}).get(META_ARTIFACT_TYPE) != (
        ARTIFACT_HYPOTHESIS
    ):
        raise BadInput(
            f"fi{hub_ref_id} already exists as an ordinary claim hub with "
            f"this sentence",
            next=(
                "a hub is identified by its sentence and scope, so this "
                f"proposal collided with a claim somebody already holds — "
                f"read fi{hub_ref_id} and reword into a distinct conjecture"
            ),
        )

    for ref_id, source_handle in resolved:
        meta = {"source_handle": source_handle} if source_handle else None
        hublib.attach_motivation(
            store,
            hub_ref_id=hub_ref_id,
            motivator_ref_id=ref_id,
            meta=meta,
            set_by=set_by,
        )

    # Provenance both ways: the dream memory that reasoned its way here, and
    # (via `PRECIS_CURRENT_AGENTLOG`) the tick that produced it. Written here
    # rather than left to the memory autolinker, which only fires when the
    # memory is written *after* the hub and names its handle.
    _link_origin(store, hub_ref_id=hub_ref_id, from_memory=from_memory)

    store.add_tag(hub_ref_id, Tag.open(PROPOSED_TAG))

    handle = handle_registry.format_handle("finding", hub_ref_id)
    return Response(
        body=(
            f"hypothesis hub {handle} minted (candidate for review)\n"
            f"claim: {sentence[:120]}\n"
            # Live edge count, not newly-written: `attach_motivation` is
            # idempotent, so a re-proposal would otherwise report zero.
            f"motivated by: {len(resolved)} edge(s) across {len(papers)} "
            f"independent source(s)\n"
            f"check it before leaving it: get(kind='finding', id='{handle}', "
            "view='mint-preflight')\n"
            "a human approves/signs it — this door never does"
        )
    )


def hypothesis_prose(store: Store, ref: Any) -> dict[str, str] | None:
    """The falsification prose — ``{"motivation": …, "testable_by": …}`` —
    for a hypothesis hub's already-fetched ``ref``. ``None`` unless ``ref``
    is marked :data:`ARTIFACT_HYPOTHESIS`; either value may be the empty
    string if the payload is present but that field wasn't (degrades
    gracefully rather than raising — see below).

    Reads whichever of the two homes this prose currently lives in
    (docs/backlog/hypothesis-cites-render-not-stored.md): the **approved**
    ``nanopub_publish.grounding`` envelope once a human approves (frozen at
    review time — :func:`precis.nanopub.mint.approve` — and it survives a
    later reword/reopen), else the still-**proposed**
    ``refs.meta[META_PROPOSED_PAYLOAD]`` an agent parked at
    :func:`put_hypothesis` mint time. The approved copy wins when both
    exist (it is the reviewed, frozen wording). ``store`` may lack the
    nanopub mixin (a bare ``FakeStore`` in a reader test) — ``getattr``
    degrades to the proposed payload only, never raises.
    """
    meta = getattr(ref, "meta", None) or {}
    if meta.get(META_ARTIFACT_TYPE) != ARTIFACT_HYPOTHESIS:
        return None
    payload: dict[str, Any] = {}
    row_fn = getattr(store, "nanopub_publish_row", None)
    if row_fn is not None:
        try:
            row = row_fn(ref.id)
        except Exception:
            row = None
        if row is not None and row.grounding:
            payload = row.grounding
    if not payload:
        proposed = meta.get(META_PROPOSED_PAYLOAD)
        if isinstance(proposed, dict):
            payload = proposed
    return {
        "motivation": str(payload.get("motivation") or "").strip(),
        "testable_by": str(payload.get("testable_by") or "").strip(),
    }


def _link_origin(store: Store, *, hub_ref_id: int, from_memory: str | None) -> None:
    """Best-effort ``origin --related-to--> hub`` edges: the dream memory that
    reasoned its way here, and the agentlog tick that ran it.

    Written here rather than left to the memory autolinker
    (:meth:`precis.handlers._numeric_ref.NumericRefHandler._sync_mention_links`),
    which only fires when the memory is written *after* the hub and happens to
    name its handle. A provenance miss must never sink a proposal that is
    otherwise sound, so every failure is logged and swallowed.
    """
    import os

    from precis import agentlog

    origins: list[int] = []
    if from_memory:
        try:
            resolved = store.resolve_handle(from_memory)
            if resolved is not None:
                origins.append(int(resolved.ref_id))
        except Exception:
            log.warning("hypothesis: unresolvable from_memory=%r", from_memory)
    raw_log_id = os.environ.get(agentlog.ENV_VAR)
    if raw_log_id and raw_log_id.isdigit():
        origins.append(int(raw_log_id))

    for src_id in origins:
        try:
            store.add_link(
                src_ref_id=src_id,
                dst_ref_id=hub_ref_id,
                relation="related-to",
                meta={"auto": "hypothesis-origin"},
            )
        except Exception:
            log.warning(
                "hypothesis: failed to link origin %s to fi%s",
                src_id,
                hub_ref_id,
                exc_info=True,
            )


__all__ = [
    "ARTIFACT_HYPOTHESIS",
    "META_ARTIFACT_TYPE",
    "META_PROPOSED_PAYLOAD",
    "PROPOSED_TAG",
    "hypothesis_prose",
    "put_hypothesis",
]
