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

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput, Unsupported
from precis.handlers._link_tag_ops import validate_relation
from precis.response import Response
from precis.store.types import ActorSlug, Tag
from precis.taproot import authoring
from precis.taproot.canon import (
    CanonicalClaim,
    MergeCandidate,
    Placement,
    Verdict,
    block,
    claim_sha,
    dedup_judge,
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

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.store.types import Ref

BlockFn = Callable[[CanonicalClaim, Any, Any], list[MergeCandidate]]
JudgeFn = Callable[[str, str], Verdict]
MergeConfirmFn = Callable[[str, str], Verdict]

__all__ = ["put_hub", "render_similar_view"]


def put_hub(
    store: Store,
    *,
    sentence: str,
    scope: dict[str, Any],
    supporters: list[dict[str, Any]],
    set_by: ActorSlug = "agent",
    dedup: bool = True,
    embedder: Any = None,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = merge_confirm,
) -> Response:
    """``supporters=`` hub-mint, gated by the semantic dedup cascade — see
    the module docstring for the branching contract."""
    claim = CanonicalClaim(sentence=sentence, scope=dict(scope or {}))

    if not dedup:
        result = authoring.seed_claim_hub(
            store, sentence=sentence, scope=scope, supporters=supporters, set_by=set_by
        )
        return Response(body=_mint_response_body(result, sentence) + "\ndedup skipped")

    if embedder is None:
        raise Unsupported(
            "hub-mint dedup needs an embedder to search for near-duplicate "
            "claims, and none is configured on this hub",
            next="pass dedup=False to mint without the semantic-dedup cascade",
        )

    candidates = block_fn(claim, store, embedder)
    judged = [(cand, judge_fn(sentence, cand.claim)) for cand in candidates]
    placement = place(claim, judged, merge_confirm_fn=merge_confirm_fn)

    if placement.action == "attach":
        return _attach_to_existing(
            store, claim, placement, judged, supporters, set_by=set_by
        )
    if placement.action == "needs_review":
        return _mint_with_review_todo(
            store, claim, placement, supporters, set_by=set_by
        )
    return _mint_new(store, claim, placement, supporters, set_by=set_by)


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
        raise BadInput("attach placement has no hub_ref_id")
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
    body = "\n\n".join(["\n".join(header), table])
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
