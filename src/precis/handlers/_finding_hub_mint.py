"""``put(kind='finding', supporters=...)`` — the semantic-dedup gate at the
agent hub-mint door — plus ``get(kind='finding', view='similar')``, the
read-only sibling that surfaces the same nearest-hubs query on demand
(the read-for-question loop (skill precis-read-for-question) slice 2).

Split out of ``finding.py`` (same rationale as the sibling ``_finding_*``
modules): this pass only ever touches ``self.store``/``self.hub.embedder``,
never other handler state, so it moves cleanly as a free function.
``FindingHandler.put``'s ``supporters=`` branch calls :func:`put_hub`.

Before this slice, the hub-mint door (:func:`~precis.taproot.authoring.
seed_claim_hub`) converged only on an **identical** ``(sentence, scope)`` —
a reworded near-duplicate minted a second hub. :func:`put_hub` runs the
SAME cascade :func:`precis.taproot.directed.directed_mint` runs before
minting — :func:`~precis.taproot.canon.block` (ANN over existing hubs) ->
:func:`~precis.taproot.canon.dedup_judge` per candidate ->
:func:`~precis.taproot.canon.place` — and branches on the verdict:

* ``attach`` — the claim already exists under different wording: the
  supporters attach as evidence on the MATCHED hub, no second hub minted
  (:func:`_attach_to_existing`). Never routes through
  :func:`~precis.taproot.authoring.seed_claim_hub` — that mints on
  ``(sentence, scope)`` identity, which is exactly the identity this
  branch is bypassing.
* ``new`` / ``new_contradicts`` — no matching claim (or a contradicting
  one): mints via :func:`~precis.taproot.authoring.seed_claim_hub` as
  before; a contradiction additionally gets the hub<->hub ``disputes``
  link :func:`~precis.taproot.hub.apply_placement` writes for the
  automated callers (:func:`_mint_new`) — reusing that shape rather than
  re-deriving it.
* ``needs_review`` — a risky, unconfirmed ``same`` verdict. **Deviation
  from every automated cascade caller** (``directed_mint``/backfill/chase,
  whose ``apply_placement`` attaches nothing and relies on a re-run):
  this door mints anyway (:func:`_mint_with_review_todo`) and files ONE
  ``kind='todo'`` naming both hubs, because a hand-authored claim with
  real grounding must never be silently dropped — an agent's ``put`` has
  no re-run to fall back on the way a batch pass does.

**Near-duplicate refusal** (docs/backlog/hub-mint-gate-fixes.md section 3,
this door only — directed/backfill/chase never call it): the ANN candidates
are merged with *same-passage neighbours* (live hubs already holding an
evidence edge on a supporter's ``source_handle`` chunk — same-burst siblings
with no embedding yet). When :func:`place` says ``new``, a second, bounded
MEDIUM judge (:func:`~precis.taproot.canon.containment_judge`, one call per
neighbour: <=3 within the distance cutoff + same-passage ones) classifies
same / component / generalization / different. Anything but ``different``
raises :class:`BadInput` naming the neighbour, with options ``attach``
(``dedup='attach:fi<N>'``), ``refine`` (reword the neighbour; not offered once
its publish row is past ``candidate``) and ``force`` (``dedup='force'`` mints
and stamps ``meta.dedup_override``). ``containment_fn`` is injectable.

``dedup=False`` skips the whole cascade (bulk re-puts, tests without a
model/embedder) and mints unconditionally via ``seed_claim_hub``, same as
before this slice — the response says so explicitly, never silently.
Dedup needs an embedder (:func:`block`'s ANN retrieval); with
``dedup=True`` and none configured, :func:`put_hub` raises
:class:`~precis.errors.Unsupported` rather than silently skipping the
cascade — a caller that can't afford the embedder should say so with
``dedup=False``, not get a degrade it never asked for.

``block_fn``/``judge_fn``/``merge_confirm_fn`` are injectable (mirrors
:func:`~precis.taproot.directed.directed_mint`) so a test can stub the
cascade without a model or embedder — see
``tests/test_finding_hub_mint.py``.

The response never carries ``pub_id=`` (docs/backlog/read-for-question-
loop.md slice 2): the column stays (it's ``mint_hub``'s own race-
convergence key), but skills already tell authors to cite ``[fi<id>]``,
and surfacing it here only invited an agent to try citing it directly.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.embedder import EmbedderUnavailable
from precis.errors import BadInput, Unsupported, Upstream
from precis.handlers._finding_common import multi_article_advisory
from precis.identity import make_pub_id, make_taproot_hub_paper_id
from precis.response import Response
from precis.store.types import ActorSlug, Tag
from precis.taproot import authoring
from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    CONTAINMENT_MAX_CANDIDATES,
    CONTAINMENT_MAX_DISTANCE,
    TAPROOT_CLAIM,
    TAPROOT_NAMESPACE,
    CanonicalClaim,
    ContainmentVerdict,
    MergeCandidate,
    Placement,
    Verdict,
    block,
    claim_hub_predicate_sql,
    claim_sha,
    containment_judge,
    dedup_judge,
    judge_candidates,
    judge_containment,
    merge_confirm,
    nearest_hubs,
    place,
)
from precis.taproot.hub import (
    _grounding_chunk_ord,
    attach_evidence,
    run_retraction_checks,
)
from precis.utils import handle_registry
from precis.utils.link_tag_ops import validate_relation

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.store.types import Ref

BlockFn = Callable[[CanonicalClaim, Any, Any], list[MergeCandidate]]
JudgeFn = Callable[[str, str], Verdict]
MergeConfirmFn = Callable[[str, str], Verdict]
ContainmentFn = Callable[[str, str], ContainmentVerdict]

#: Same-passage neighbours judged per put (besides the ANN ones).
_SAME_PASSAGE_MAX = 3

__all__ = ["put_hub", "render_similar_view"]


def put_hub(
    store: Store,
    *,
    sentence: str,
    scope: dict[str, Any],
    supporters: list[dict[str, Any]],
    set_by: ActorSlug = "agent",
    dedup: bool | str = True,
    embedder: Any = None,
    extra_meta: dict[str, Any] | None = None,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = merge_confirm,
    containment_fn: ContainmentFn = containment_judge,
) -> Response:
    """``supporters=`` hub-mint, gated by the semantic dedup cascade — see
    the module docstring for the branching contract. Appends the
    ``multi-article`` advisory (gr463414) for any supporter whose paper is
    tagged so; the write has already succeeded by then.

    ``extra_meta`` merges into the minted hub's ``refs.meta`` on a real
    mint only — an ``attach``/converge outcome writes no hub row, so the
    keys are dropped there (same rule as :func:`mint_hub`'s own
    ``extra_meta``)."""
    resp = _put_hub(
        store,
        sentence=sentence,
        scope=scope,
        supporters=supporters,
        set_by=set_by,
        dedup=dedup,
        embedder=embedder,
        extra_meta=extra_meta,
        block_fn=block_fn,
        judge_fn=judge_fn,
        merge_confirm_fn=merge_confirm_fn,
        containment_fn=containment_fn,
    )
    advisory = _multi_article_advisories(store, supporters)
    if not advisory:
        return resp
    return replace(resp, body=resp.body + advisory)


def _multi_article_advisories(store: Store, supporters: list[dict[str, Any]]) -> str:
    """Advisory lines for supporters citing a ``multi-article`` paper — one
    per distinct paper, resolved the way ``link`` does (a ``pc<id>`` chunk
    resolves to its owning paper). Best-effort: unresolvable supporters
    were already handled by the write, so they add nothing here."""
    resolved, _errors = authoring.resolve_supporters(store, supporters)
    seen: set[int] = set()
    out = ""
    for sup in resolved:
        pid = sup["paper_ref_id"]
        if pid in seen:
            continue
        seen.add(pid)
        out += multi_article_advisory(store, pid, str(sup["paper"]))
    return out


def _put_hub(
    store: Store,
    *,
    sentence: str,
    scope: dict[str, Any],
    supporters: list[dict[str, Any]],
    set_by: ActorSlug,
    dedup: bool | str,
    embedder: Any,
    extra_meta: dict[str, Any] | None,
    block_fn: BlockFn,
    judge_fn: JudgeFn,
    merge_confirm_fn: MergeConfirmFn,
    containment_fn: ContainmentFn,
) -> Response:
    claim = CanonicalClaim(sentence=sentence, scope=dict(scope or {}))
    mode, attach_to = _parse_dedup(dedup)

    if mode == "attach":
        assert attach_to is not None
        return _attach_by_choice(store, claim, attach_to, supporters, set_by=set_by)

    if mode == "off":
        result = authoring.seed_claim_hub(
            store,
            sentence=sentence,
            scope=scope,
            supporters=supporters,
            set_by=set_by,
            extra_meta=extra_meta,
        )
        return Response(body=_mint_response_body(result, sentence) + "\ndedup skipped")

    if embedder is None:
        raise Unsupported(
            "hub-mint dedup needs an embedder to search for near-duplicate "
            "claims, and none is configured on this hub",
            next="pass dedup=False to mint without the semantic-dedup cascade",
        )

    try:
        candidates = block_fn(claim, store, embedder)
    except (EmbedderUnavailable, Upstream) as exc:
        raise _dedup_unavailable_upstream(exc) from exc
    # An identical (sentence, scope) re-put converges onto its own hub before
    # any same-passage / containment logic runs: an idempotent re-put is never
    # refused, even when the hub has no embedding yet (a same-passage sibling
    # would otherwise be judged against itself).
    converged = _converge_identical(store, claim, supporters, set_by=set_by)
    if converged is not None:
        return converged
    passage_hubs = _same_passage_neighbours(store, supporters)
    by_id = {c.hub_ref_id: c for c in candidates}
    merged = list(candidates) + [c for c in passage_hubs if c.hub_ref_id not in by_id]
    judged = judge_candidates(sentence, merged, judge_fn)
    placement = place(claim, judged, merge_confirm_fn=merge_confirm_fn)

    if placement.action == "new":
        try:
            hit = _containment_hit(
                sentence, merged, {c.hub_ref_id for c in passage_hubs}, containment_fn
            )
        except Upstream as exc:
            # dedup='force' is the documented escape from a judge outage
            # (the Upstream's next=): mint, and record that it went unjudged.
            if mode != "force":
                raise
            hit = None
            extra_meta = {
                **(extra_meta or {}),
                "dedup_override": {"verdict": "unjudged", "reason": str(exc)[:300]},
            }
        if hit is not None:
            cand, verdict = hit
            if mode != "force":
                raise _near_duplicate_refusal(store, sentence, cand, verdict)
            extra_meta = {
                **(extra_meta or {}),
                "dedup_override": {
                    "neighbour": handle_registry.format_handle(
                        "finding", cand.hub_ref_id
                    ),
                    "verdict": verdict["verdict"],
                    "reason": verdict["rationale"],
                },
            }

    if placement.action == "attach":
        return _attach_to_existing(
            store, claim, placement, judged, supporters, set_by=set_by
        )
    if placement.action == "needs_review":
        return _mint_with_review_todo(
            store, claim, placement, supporters, set_by=set_by, extra_meta=extra_meta
        )
    return _mint_new(
        store, claim, placement, supporters, set_by=set_by, extra_meta=extra_meta
    )


# ── near-duplicate refusal (docs/backlog/hub-mint-gate-fixes.md section 3) ──

_DEDUP_ATTACH_RE = re.compile(r"^attach:(?:fi)?(\d+)$")


def _parse_dedup(dedup: bool | str) -> tuple[str, int | None]:
    """``dedup=`` -> ``(mode, attach_hub_ref_id)``: ``off`` (False),
    ``on`` (True), ``force`` or ``attach``. Anything else is a BadInput."""
    if dedup is True:
        return "on", None
    if dedup is False:
        return "off", None
    if isinstance(dedup, str):
        text = dedup.strip().lower()
        if text in ("true", "on"):
            return "on", None
        if text in ("false", "off"):
            return "off", None
        if text == "force":
            return "force", None
        m = _DEDUP_ATTACH_RE.match(text)
        if m:
            return "attach", int(m.group(1))
    raise BadInput(
        f"dedup={dedup!r} is not a recognised value",
        options=["True (default)", "False", "'force'", "'attach:fi<N>'"],
        next="dedup='attach:fi<N>' attaches your supporters to hub fi<N>; "
        "dedup='force' mints despite a near-duplicate neighbour; "
        "dedup=False skips the check entirely",
    )


def _same_passage_neighbours(
    store: Store, supporters: list[dict[str, Any]]
) -> list[MergeCandidate]:
    """Live claim hubs that already carry an evidence edge on the chunk any
    supporter's ``source_handle`` names — same-burst siblings, which an ANN
    search misses while their body chunk is still unembedded. Unresolvable
    supporters are skipped (the mint's own validation reports them)."""
    resolved, _errors = authoring.resolve_supporters(store, supporters)
    found: dict[int, MergeCandidate] = {}
    with store.pool.connection() as conn:
        for sup in resolved:
            ord_ = _grounding_chunk_ord(
                store,
                paper_ref_id=sup["paper_ref_id"],
                meta={"source_handle": sup.get("source_handle")},
            )
            if ord_ is None:
                continue
            rows = conn.execute(
                f"""
                SELECT DISTINCT r.ref_id, r.title
                FROM links l
                JOIN chunks c ON c.chunk_id = l.src_chunk_id
                             AND c.ref_id = l.src_ref_id AND c.ord = %(ord)s
                             AND c.retired_at IS NULL
                JOIN refs r ON r.ref_id = l.dst_ref_id
                WHERE l.src_ref_id = %(paper)s
                  AND r.kind = 'finding' AND r.retired_at IS NULL
                  AND {claim_hub_predicate_sql()}
                ORDER BY r.ref_id
                """,
                {
                    "ord": ord_,
                    "paper": sup["paper_ref_id"],
                    **CLAIM_HUB_PREDICATE_PARAMS,
                },
            ).fetchall()
            for ref_id, title in rows:
                found.setdefault(
                    int(ref_id),
                    MergeCandidate(
                        hub_ref_id=int(ref_id), claim=str(title), distance=1.0
                    ),
                )
    return list(found.values())[:_SAME_PASSAGE_MAX]


def _containment_hit(
    sentence: str,
    merged: list[MergeCandidate],
    passage_ids: set[int],
    containment_fn: ContainmentFn,
) -> tuple[MergeCandidate, ContainmentVerdict] | None:
    """Second, bounded judge when the dedup cascade said ``new``: up to
    :data:`CONTAINMENT_MAX_CANDIDATES` ANN neighbours under
    :data:`CONTAINMENT_MAX_DISTANCE`, plus every same-passage neighbour.
    Returns the first neighbour not judged ``different`` (candidate order)."""
    near = sorted(
        (
            c
            for c in merged
            if c.hub_ref_id not in passage_ids
            and c.distance <= CONTAINMENT_MAX_DISTANCE
        ),
        key=lambda c: c.distance,
    )[:CONTAINMENT_MAX_CANDIDATES]
    cands = near + [c for c in merged if c.hub_ref_id in passage_ids]
    for cand, v in judge_containment(sentence, cands, containment_fn):
        if v["verdict"] != "different":
            return cand, v
    return None


def _near_duplicate_refusal(
    store: Store, sentence: str, cand: MergeCandidate, verdict: ContainmentVerdict
) -> BadInput:
    from precis.taproot.hub import _publish_states_past_candidate

    handle = handle_registry.format_handle("finding", cand.hub_ref_id)
    relation = {
        "same": "says the same thing as",
        "component": "is one component of",
        "generalization": "is a broader/narrower form of",
    }.get(verdict["verdict"], verdict["verdict"])
    options = [
        f"attach: re-put the same call with dedup='attach:{handle}' to add "
        f"your supporters as evidence on {handle}",
    ]
    with store.pool.connection() as conn:
        frozen = _publish_states_past_candidate(conn, cand.hub_ref_id)
    refine_note = ""
    if frozen:
        refine_note = (
            f"\nrefine unavailable: {handle} is past candidate in "
            f"nanopub_publish ({', '.join(frozen)}); its wording is frozen"
        )
    else:
        options.append(
            f"refine: reword {handle} so it stays distinct from your claim — "
            f"edit(kind='finding', id='{handle}', title='<new sentence>'), "
            "then re-put"
        )
    options.append(
        "force: re-put with dedup='force' to mint anyway (records "
        "meta.dedup_override with this verdict)"
    )
    return BadInput(
        f"near-duplicate: your claim {relation} existing hub {handle} "
        f"({verdict['rationale']}) — nothing minted.\n"
        f"  new:      {sentence[:200]}\n"
        f"  existing: {handle} {cand.claim[:200]}" + refine_note,
        options=options,
        next=f"re-put with dedup='attach:{handle}' if {handle} already states "
        "your claim, or dedup='force' if yours is genuinely distinct",
    )


def _converge_identical(
    store: Store,
    claim: CanonicalClaim,
    supporters: list[dict[str, Any]],
    *,
    set_by: ActorSlug,
) -> Response | None:
    """The attach response when a live hub already carries this exact
    ``(sentence, scope)`` pub_id; ``None`` when there is none."""
    hub_id = authoring.find_hub_by_pub_id(
        store, make_pub_id(make_taproot_hub_paper_id(claim.sentence, claim.scope))
    )
    existing = store.get_ref(kind="finding", id=hub_id) if hub_id else None
    if existing is None or hub_id is None:
        return None
    reason = "identical sentence and scope (same pub_id)"
    judged = [
        (
            MergeCandidate(hub_ref_id=hub_id, claim=existing.title, distance=0.0),
            Verdict(verdict="same", confidence=1.0, rationale=reason),
        )
    ]
    placement = Placement(action="attach", hub_ref_id=hub_id, reason=reason)
    return _attach_to_existing(
        store, claim, placement, judged, supporters, set_by=set_by
    )


def _is_live_claim_hub(store: Store, ref: Ref) -> bool:
    """A live ``TAPROOT:claim`` hub that is not a composite (attach refuses
    composites; checking here turns that into the BadInput below)."""
    from precis.taproot.hub import _is_composite_hub

    if getattr(ref, "retired_at", None) is not None:
        return False
    if not store.has_tag(ref.id, TAPROOT_NAMESPACE, TAPROOT_CLAIM):
        return False
    with store.pool.connection() as conn:
        return not _is_composite_hub(ref.id, conn=conn)


def _attach_by_choice(
    store: Store,
    claim: CanonicalClaim,
    hub_ref_id: int,
    supporters: list[dict[str, Any]],
    *,
    set_by: ActorSlug,
) -> Response:
    """``dedup='attach:fi<N>'`` — the agent's own choice from a near-duplicate
    refusal: no search, no judge, straight to the shared attach path."""
    ref = store.get_ref(kind="finding", id=hub_ref_id)
    if ref is None:
        raise BadInput(
            f"dedup='attach:fi{hub_ref_id}': no such finding",
            next="pick a claim hub handle from the refusal's options",
        )
    if not _is_live_claim_hub(store, ref):
        handle = handle_registry.format_handle("finding", hub_ref_id)
        raise BadInput(
            f"{handle} is not a live claim hub",
            options=[
                "re-put with dedup='attach:fi<N>' naming a live claim hub "
                "from the near-duplicate refusal",
                "dedup='force' to mint a new hub",
            ],
            next=f"{handle} is retired, not a TAPROOT:claim hub, or a "
            "composite; attach only targets a live single-claim hub",
        )
    placement = Placement(
        action="attach",
        hub_ref_id=hub_ref_id,
        reason="agent chose attach (dedup='attach:fi<N>')",
    )
    judged = [
        (
            MergeCandidate(hub_ref_id=hub_ref_id, claim=ref.title, distance=0.0),
            Verdict(verdict="same", confidence=1.0, rationale=placement.reason),
        )
    ]
    return _attach_to_existing(
        store, claim, placement, judged, supporters, set_by=set_by
    )


# ── dedup-gate contention: capacity is not absence ──────────────────────


def _dedup_unavailable_upstream(exc: Exception) -> Upstream:
    """The ``Upstream`` raised when the mint-door dedup search's embed fails
    under embedder contention (gripe #450123).

    The near-duplicate check is a HARD GATE before minting ("never mint
    without searching first", ``precis-taproot-mint-help``): unlike a
    read-path search it has no lexical leg to degrade to, so a busy embedder
    means the gate could not run at all. Left raw, the failure surfaces as
    the read-path message (``BoundedConcurrencyEmbedder`` even says
    "semantic search degraded to lexical-only" — untrue for a write) or a
    bare "unavailable", and an agent that reads either as "the corpus is
    broken / this claim doesn't exist" mints a duplicate hub — the exact
    inference ``precis-taproot-mint-help`` warns about, and the second-order
    cost this gripe flagged.

    So the message names capacity (not absence), threads through the
    service's ``retry_after_s`` hint when present (gripe #450123 option d,
    same channel :func:`precis.utils.embed_query.semantic_unavailable_upstream`
    uses for the read path), and explicitly steers OFF the ``dedup=False``
    escape hatch — forcing the mint past a check that merely couldn't run is
    how the duplicate gets born.
    """
    from precis.utils.embed_query import _embedder_unavailable_cause

    cause = _embedder_unavailable_cause(exc)
    retry_after = getattr(cause, "retry_after_s", None)
    if retry_after is not None:
        when = f"~{retry_after:g} s"
        msg = (
            "the near-duplicate check could not run — the query embedder is "
            f"at capacity (the service asked for a retry in {when}); the mint "
            "is refused rather than risk a duplicate hub"
        )
    else:
        when = "shortly"
        msg = (
            "the near-duplicate check could not run — the query embedder is "
            "busy/unavailable; the mint is refused rather than risk a "
            "duplicate hub"
        )
    return Upstream(
        msg,
        next=(
            f"wait {when} and retry the SAME put() — this is contention, not "
            "absence, so do NOT conclude the claim is un-minted and do NOT "
            "re-run with dedup=False to force it (that skips the duplicate "
            "check and is exactly how duplicate hubs are born)"
        ),
    )


# ── attach — converge onto an existing hub, no mint ─────────────────────


def _attach_to_existing(
    store: Store,
    claim: CanonicalClaim,
    placement: Placement,
    judged: list[tuple[MergeCandidate, Verdict]],
    supporters: list[dict[str, Any]],
    *,
    set_by: ActorSlug,
) -> Response:
    """``attach`` — the closest verdict was a confirmed ``same``: attach
    ``supporters`` as evidence on ``placement.hub_ref_id`` directly
    (:func:`~precis.taproot.hub.attach_evidence`), never through
    :func:`~precis.taproot.authoring.seed_claim_hub` (which mints/converges
    on THIS claim's own ``(sentence, scope)`` identity — exactly the
    identity this branch is bypassing in favour of the matched neighbour).
    """
    hub_ref_id = placement.hub_ref_id
    if hub_ref_id is None:  # pragma: no cover — defensive; place() always
        # sets hub_ref_id on "attach".
        raise BadInput(
            "attach placement has no hub_ref_id",
            next="a dedup-cascade invariant broke (a server bug, not your "
            "supporters) — file it: put(kind='gripe', text='hub-mint attach "
            "placement had no hub_ref_id')",
        )
    neighbour_sentence = next(
        (cand.claim for cand, _v in judged if cand.hub_ref_id == hub_ref_id), ""
    )

    resolved, errors = authoring.resolve_supporters(store, supporters)
    if errors:
        raise BadInput(
            f"{len(errors)} of {len(supporters)} supporter(s) failed "
            "validation — nothing attached:\n" + "\n".join(errors),
            next="fix every listed supporter and retry",
        )

    # One transaction for the whole supporter loop (mirrors
    # authoring.seed_claim_hub's mint+attach loop, gr263195's atomicity
    # backstop): an exception partway through must roll every edge attached
    # so far back too, not leave a half-attached set. Retraction checks
    # (trigger 1, a network call) must never run inside an open transaction
    # (pool-exhaustion deadlock risk) -- collected in owned_checks and
    # drained via run_retraction_checks only after commit.
    attached = 0
    already = 0
    ungrounded = 0
    owned_checks: list[int] = []
    with store.tx() as conn:
        for supporter in resolved:
            paper_ref_id = supporter["paper_ref_id"]
            role = supporter["role"]
            src_handle = supporter.get("source_handle")
            src_ord = _grounding_chunk_ord(
                store, paper_ref_id=paper_ref_id, meta={"source_handle": src_handle}
            )
            if authoring._evidence_edge_exists(
                store,
                paper_ref_id=paper_ref_id,
                hub_ref_id=hub_ref_id,
                role=role,
                src_ord=src_ord,
                conn=conn,
            ):
                already += 1
                continue
            attach_evidence(
                store,
                hub_ref_id=hub_ref_id,
                paper_ref_id=paper_ref_id,
                role=role,
                meta=_supporter_meta(claim, supporter),
                set_by=set_by,
                conn=conn,
                pending_checks=owned_checks,
            )
            attached += 1
            if not src_handle:
                ungrounded += 1

    # Transaction committed -- now safe to reach the network (trigger 1).
    run_retraction_checks(store, owned_checks, hub_ref_id=hub_ref_id)

    hub_handle = handle_registry.format_handle("finding", hub_ref_id)
    body = (
        f"converged onto {hub_handle} (same claim: {neighbour_sentence[:120]})\n"
        f"claim: {claim.sentence[:120]}\n"
        f"evidence: {attached} attached, {already} already present"
        + (f", {ungrounded} ref-level (ungrounded)" if ungrounded else "")
        + f"\nreason: {placement.reason}\n"
        f"cite it inline as [{hub_handle}] — resolves to the current derived "
        "originator(s) on every render"
    )
    return Response(body=body)


# ── new / new_contradicts — mint as today ───────────────────────────────


def _mint_new(
    store: Store,
    claim: CanonicalClaim,
    placement: Placement,
    supporters: list[dict[str, Any]],
    *,
    set_by: ActorSlug,
    extra_meta: dict[str, Any] | None = None,
) -> Response:
    """``new`` / ``new_contradicts`` — no matching claim: mint through the
    existing door unchanged. A ``new_contradicts`` verdict additionally
    writes the hub<->hub ``disputes`` link
    :func:`~precis.taproot.hub.apply_placement` writes for the automated
    cascade callers (``directed_mint``/backfill/chase) — ``add_link`` is
    idempotent on its unique tuple, so this is a plain reuse of that
    shape, not a second implementation of it."""
    result = authoring.seed_claim_hub(
        store,
        sentence=claim.sentence,
        scope=claim.scope,
        supporters=supporters,
        set_by=set_by,
        extra_meta=extra_meta,
    )
    hub_ref_id = result["hub_ref_id"]
    if (
        placement.action == "new_contradicts"
        and placement.contradicts_hub_ref_id is not None
    ):
        store.add_link(
            src_ref_id=hub_ref_id,
            dst_ref_id=placement.contradicts_hub_ref_id,
            relation=validate_relation("disputes", store=store),
            set_by=set_by,
        )
    return Response(body=_mint_response_body(result, claim.sentence))


# ── needs_review — mint anyway, file one todo naming both hubs ──────────


def _mint_with_review_todo(
    store: Store,
    claim: CanonicalClaim,
    placement: Placement,
    supporters: list[dict[str, Any]],
    *,
    set_by: ActorSlug,
    extra_meta: dict[str, Any] | None = None,
) -> Response:
    """``needs_review`` — a risky, unconfirmed ``same`` verdict. The
    automated cascade callers (``apply_placement``) attach nothing here and
    rely on a later re-run; an agent's ``put`` has no re-run to fall back
    on, so this door mints anyway and files ONE ``kind='todo'`` naming both
    hubs for a human to adjudicate (module docstring)."""
    result = authoring.seed_claim_hub(
        store,
        sentence=claim.sentence,
        scope=claim.scope,
        supporters=supporters,
        set_by=set_by,
        extra_meta=extra_meta,
    )
    hub_ref_id = result["hub_ref_id"]
    nearest_ref_id = placement.hub_ref_id
    todo_id = _file_dedup_review_todo(
        store,
        hub_ref_id=hub_ref_id,
        nearest_ref_id=nearest_ref_id,
        placement=placement,
        sentence=claim.sentence,
        set_by=set_by,
    )

    hub_handle = handle_registry.format_handle("finding", hub_ref_id)
    todo_handle = handle_registry.format_handle("todo", todo_id)
    nearest_handle = (
        handle_registry.format_handle("finding", nearest_ref_id)
        if nearest_ref_id is not None
        else "(none)"
    )
    body = (
        f"claim hub {hub_handle} minted — possible duplicate flagged, not "
        "auto-merged\n"
        f"claim: {claim.sentence[:120]}\n"
        f"evidence: {result['attached']} attached, {result['already']} already present\n"
        f"nearest existing claim: {nearest_handle} — {placement.reason}\n"
        f"filed {todo_handle} for a human to review the possible duplicate\n"
        f"cite it inline as [{hub_handle}] — resolves to the current derived "
        "originator(s) on every render"
    )
    return Response(body=body)


def _file_dedup_review_todo(
    store: Store,
    *,
    hub_ref_id: int,
    nearest_ref_id: int | None,
    placement: Placement,
    sentence: str,
    set_by: ActorSlug,
) -> int:
    """File the ``kind='todo'`` for a ``needs_review`` dedup verdict —
    unlike :func:`~precis.taproot.directed._file_review_todo` (keyed on
    the un-minted candidate + a source passage), THIS hub already minted,
    so the todo names both hubs for a human to adjudicate (merge, or
    confirm they're genuinely distinct) rather than gating a write that
    already landed."""
    nearest_sentence = ""
    if nearest_ref_id is not None:
        nearest_ref = store.get_ref(kind="finding", id=nearest_ref_id)
        if nearest_ref is not None:
            nearest_sentence = nearest_ref.title

    title = f"review possible duplicate: fi{hub_ref_id} vs fi{nearest_ref_id}"
    meta: dict[str, Any] = {
        "source": "taproot:hub-mint-dedup",
        "new_hub_ref_id": hub_ref_id,
        "candidate_hub_ref_id": nearest_ref_id,
        "claim_sentence": sentence,
        "candidate_sentence": nearest_sentence,
        "placement_reason": placement.reason,
    }
    with store.tx() as c:
        todo = store.insert_ref(
            kind="todo", slug=None, title=title[:200], meta=meta, conn=c
        )
        store.add_tag(
            todo.id,
            Tag.closed("STATUS", "open"),
            set_by=set_by,
            replace_prefix=True,
            conn=c,
        )
    return int(todo.id)


# ── shared response rendering ────────────────────────────────────────────


def _supporter_meta(claim: CanonicalClaim, supporter: dict[str, Any]) -> dict[str, Any]:
    """The evidence-edge ``meta`` for one supporter — same shape
    :func:`~precis.taproot.authoring.seed_claim_hub` writes (support is a
    verdict, never a default; see that function's docstring)."""
    meta: dict[str, Any] = {"source_handle": supporter.get("source_handle")}
    support = supporter.get("support")
    support_reason = supporter.get("support_reason")
    verified_by = supporter.get("verified_by")
    if support and support_reason and verified_by:
        meta.update(
            {
                "support": support,
                "support_reason": support_reason,
                "caveats": list(supporter.get("caveats") or []),
                "verified_by": verified_by,
                "verified_at": datetime.now(UTC).isoformat(),
                "verified_claim_sha": claim_sha(claim.sentence),
            }
        )
    return meta


# ── get(view='similar') — read-only nearest-hubs query ──────────────────


def render_similar_view(
    store: Store, ref: Ref, *, embedder: Any, k: int = 10
) -> Response:
    """``get(kind='finding', id='fi<id>', view='similar')`` — the ``k``
    nearest OTHER live claim hubs to ``ref``'s own sentence
    (:func:`~precis.taproot.canon.nearest_hubs`, the same ANN retrieval
    :func:`put_hub`'s cascade runs before minting) — a read-only "what's
    near this hub" query for an author checking for a duplicate before
    mint, or a reviewer eyeballing the neighbourhood by hand. ``ref``'s
    own hub excludes itself from the result.
    """
    from precis.format import render_agent_table

    if embedder is None:
        raise Unsupported(
            "view='similar' needs an embedder to search for near-duplicate "
            "claims, and none is configured on this hub",
            next="no dedup-free substitute — retry once the embedder is up",
        )

    candidates = nearest_hubs(
        ref.title, (ref.meta or {}).get("scope"), store, embedder, k=k + 1
    )
    others = [c for c in candidates if c.hub_ref_id != ref.id][:k]

    header = [f"# nearest claims to finding {ref.id}", "", ref.title]
    if not others:
        header.append("")
        header.append("no other claim hubs found nearby")
        return Response(body="\n".join(header))

    rows = [
        {
            "hub": f"[{handle_registry.format_handle('finding', c.hub_ref_id)}]",
            "distance": f"{c.distance:.4f}",
            "claim": c.claim[:120],
        }
        for c in others
    ]
    table = render_agent_table(rows, schema=["hub", "distance", "claim"])
    hint = (
        "a listed hub is a duplicate? preview collapsing it into this one "
        "(read-only): view='merge-plan' on this hub with args={'loser': <hub>}"
    )
    body = "\n\n".join(["\n".join(header), table, hint])
    return Response(body=body)


def _mint_response_body(result: dict[str, Any], sentence: str) -> str:
    """The put() response body for a fresh mint/converge — same shape the
    pre-dedup door rendered, minus ``pub_id=`` (slice 2: the column stays
    for ``mint_hub``'s race-convergence key, but agent-facing output never
    names it — skills already tell authors to cite ``[fi<id>]``)."""
    ungrounded = result["ungrounded"]
    lints = [*(result.get("notation") or []), *(result.get("scope_lint") or [])]
    lint_note = (
        "\nlint (advisory, hub already minted):\n"
        + "\n".join(f"  - {w}" for w in lints)
        if lints
        else ""
    )
    hub_handle = handle_registry.format_handle("finding", result["hub_ref_id"])
    return (
        f"claim hub {hub_handle}\n"
        f"claim: {sentence[:120]}\n"
        f"evidence: {result['attached']} attached, {result['already']} already present"
        + (f", {ungrounded} ref-level (ungrounded)" if ungrounded else "")
        + "\n"
        f"cite it inline as [{hub_handle}] — resolves to the current derived "
        "originator(s) on every render" + lint_note
    )
