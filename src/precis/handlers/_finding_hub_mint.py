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
  branch is bypassing. The converge mints no row, so the writer
  annotation (``meta.{model,prompt_version,run_id,why}``) would drop —
  instead the run's observation rides a ``considered`` edge from the
  run's scope (``from_memory=``, else ``meta.run_id``'s agentlog, else
  the env-attributed run) to the hub — verdict ``useful``, the run's
  ``why`` as its reason, every supporter's identity on the edge
  (grounded pins in ``passages``, the full set incl. ref-level sources
  in ``supporters``), the env-attributed run in ``run`` when
  ``run_id`` is absent (gr626718, the link-reason ruling's positive
  half — "far ref is relevant here because <reason>"). The edge write
  shares the attach's transaction, so a reported converge always
  retained its observation — a failure fails the put, never drops it
  silently. Every judgment also lands an append-only ``considerations``
  history row, so two runs converging on one hub each keep their own
  reasoned record for fix-forward audit.
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
``force`` also bypasses a judge outage in EITHER stage (gr639349): a stage-one
``judge_candidates``/``place`` outage or a stage-two containment outage mints
with ``dedup_override`` verdict ``'unjudged'`` carrying the error, so a later
pass can re-judge what force minted blind. When a stage-one outage is followed
by a stage-two containment hit in the same force-mint, the ``unjudged`` marker
stays and the containment verdict rides under
``dedup_override['containment']`` (td650548) — the re-judge marker is never
overwritten.

**Create-time extras** (gr676914): ``tags=``/``link=``/``rel=`` are checked
before the cascade runs (:func:`~precis.handlers._finding_common.
check_put_extras`) and written in the outcome's own transaction — the mint's
on a new hub (:func:`_seed`), the attach's on a converge — so a refused extra
leaves no hub and no evidence edge. They land on the hub the response names:
on an attach/converge outcome that is the MATCHED hub, and the response's
``applied to fi<N>`` line says so.

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
``tests/test_finding_hub_mint.py``. Every attempt logs one INFO
``finding.mint phases`` record, including refusals and failures. Wall seconds
include failed calls: block includes ANN and passage retrieval; judge is dedup;
place includes merge confirmation; containment is the bounded second stage;
mint includes identity convergence and writes. Candidate counts describe the
merged first-stage set and bounded containment set; ``judge_retries`` counts
actual scheduled retries across both stages, never repeated claim wording.
Skipped phases are zero. Containment concurrency awaits production measurements.

The response never carries ``pub_id=`` (docs/backlog/read-for-question-
loop.md slice 2): the column stays (it's ``mint_hub``'s own race-
convergence key), but skills already tell authors to cite ``[fi<id>]``,
and surfacing it here only invited an agent to try citing it directly.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING, Any

from precis.embedder import EmbedderUnavailable
from precis.errors import BadInput, Unsupported, Upstream
from precis.handlers._finding_common import (
    PutExtras,
    apply_put_extras,
    check_put_extras,
    multi_article_advisory,
)
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

log = logging.getLogger(__name__)

BlockFn = Callable[[CanonicalClaim, Any, Any], list[MergeCandidate]]
JudgeFn = Callable[[str, str], Verdict]
MergeConfirmFn = Callable[[str, str], Verdict]
ContainmentFn = Callable[[str, str], ContainmentVerdict]

#: Same-passage neighbours judged per put (besides the ANN ones).
_SAME_PASSAGE_MAX = 3
#: Same-paper hubs with no embedding yet, judged per put (besides the above).
_UNEMBEDDED_PAPER_MAX = 3

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
    from_memory: str | None = None,
    tags: list[str] | None = None,
    link: str | None = None,
    rel: str | None = None,
    hub: Any = None,
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
    keys hang on the run's ``considered`` observation edge instead (see
    :func:`_attach_to_existing`). ``from_memory`` names the run's scope
    ref (the memory/quest that reasoned here) for that edge — falling
    back to ``meta.run_id``'s agentlog, then the env-attributed run.

    ``tags=``/``link=``/``rel=`` are the put schema's create-time extras:
    checked here before anything runs, then written on the hub the outcome
    names, in that outcome's transaction (module docstring)."""
    extras = check_put_extras(store, tags=tags, link=link, rel=rel, hub=hub)
    resp = _put_hub(
        store,
        sentence=sentence,
        scope=scope,
        supporters=supporters,
        set_by=set_by,
        dedup=dedup,
        embedder=embedder,
        extra_meta=extra_meta,
        from_memory=from_memory,
        extras=extras,
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


class _PhaseTimer:
    """Elapsed wall seconds, including failures; retries count scheduling events.

    Candidates are the merged ANN/passage hubs offered to stage one and the
    bounded subset offered to containment. Skipped phases stay zero. Keep the
    stages serial until a day of production measurements supports changing them.
    """

    def __init__(self) -> None:
        self.seconds = dict.fromkeys(
            ("block", "judge", "place", "containment", "mint"), 0.0
        )
        self.candidates = 0
        self.containment_candidates = 0
        self.judge_retries = 0
        self._lock = Lock()

    def retry(self) -> None:
        with self._lock:
            self.judge_retries += 1

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        start = perf_counter()
        try:
            yield
        finally:
            self.seconds[name] += perf_counter() - start


def _put_hub(store: Store, **kwargs: Any) -> Response:
    timer = _PhaseTimer()
    try:
        return _put_hub_cascade(store, timer=timer, **kwargs)
    finally:
        log.info(
            "finding.mint phases block=%.3f judge=%.3f place=%.3f "
            "containment=%.3f mint=%.3f candidates=%d "
            "containment_candidates=%d judge_retries=%d",
            *(
                timer.seconds[key]
                for key in ("block", "judge", "place", "containment", "mint")
            ),
            timer.candidates,
            timer.containment_candidates,
            timer.judge_retries,
        )


def _put_hub_cascade(
    store: Store,
    *,
    timer: _PhaseTimer,
    sentence: str,
    scope: dict[str, Any],
    supporters: list[dict[str, Any]],
    set_by: ActorSlug,
    dedup: bool | str,
    embedder: Any,
    extra_meta: dict[str, Any] | None,
    from_memory: str | None,
    extras: PutExtras | None,
    block_fn: BlockFn,
    judge_fn: JudgeFn,
    merge_confirm_fn: MergeConfirmFn,
    containment_fn: ContainmentFn,
) -> Response:
    claim = CanonicalClaim(sentence=sentence, scope=dict(scope or {}))
    mode, attach_to = _parse_dedup(dedup)

    if mode == "attach":
        assert attach_to is not None
        with timer.phase("mint"):
            return _attach_by_choice(
                store,
                claim,
                attach_to,
                supporters,
                set_by=set_by,
                extra_meta=extra_meta,
                from_memory=from_memory,
                extras=extras,
            )

    if mode == "off":
        with timer.phase("mint"):
            result, applied = _seed(
                store,
                claim,
                supporters,
                set_by=set_by,
                extra_meta=extra_meta,
                extras=extras,
            )
        return Response(
            body=_mint_response_body(result, sentence) + "\ndedup skipped" + applied
        )

    if embedder is None:
        raise Unsupported(
            "hub-mint dedup needs an embedder to search for near-duplicate "
            "claims, and none is configured on this hub",
            next="pass dedup=False to mint without the semantic-dedup cascade",
        )

    try:
        with timer.phase("block"):
            candidates = block_fn(claim, store, embedder)
    except (EmbedderUnavailable, Upstream) as exc:
        raise _dedup_unavailable_upstream(exc) from exc
    # An identical (sentence, scope) re-put converges onto its own hub before
    # any same-passage / containment logic runs: an idempotent re-put is never
    # refused, even when the hub has no embedding yet (a same-passage sibling
    # would otherwise be judged against itself).
    with timer.phase("mint"):
        converged = _converge_identical(
            store,
            claim,
            supporters,
            set_by=set_by,
            extra_meta=extra_meta,
            from_memory=from_memory,
            extras=extras,
        )
    if converged is not None:
        return converged
    with timer.phase("block"):
        passage_hubs = _same_passage_neighbours(store, supporters)
        by_id = {c.hub_ref_id: c for c in candidates}
        merged = list(candidates) + [
            c for c in passage_hubs if c.hub_ref_id not in by_id
        ]
    timer.candidates = len(merged)
    try:
        with timer.phase("judge"):
            judged = judge_candidates(sentence, merged, judge_fn, on_retry=timer.retry)
        with timer.phase("place"):
            placement = place(claim, judged, merge_confirm_fn=merge_confirm_fn)
    except Upstream as exc:
        # dedup='force' is the documented escape from a judge outage (the
        # stage-two handler's precedent, gr639349): the candidates are
        # unjudged, so treat the placement as 'new', mint, and record that
        # it went unjudged — the marker lets a later pass re-judge. A long
        # outage force-mints blind, which is exactly why the non-force
        # re-raise below names the escape instead of taking it silently.
        if mode != "force":
            raise Upstream(
                str(exc.cause),
                next="retry the same put() once the dedup judge is back "
                "(nothing was written). If the outage persists, dedup='force' "
                "mints without the duplicate check and records "
                "meta.dedup_override verdict 'unjudged' for a later re-judge "
                "— mind that force-minting through a long outage can create "
                "duplicates",
            ) from exc
        judged = []
        placement = Placement(
            action="new",
            reason=f"dedup judge outage, force-minted unjudged: {exc.cause[:200]}",
        )
        extra_meta = {
            **(extra_meta or {}),
            "dedup_override": {"verdict": "unjudged", "reason": str(exc.cause)[:300]},
        }

    if placement.action == "new":
        try:
            with timer.phase("containment"):
                hit = _containment_hit(
                    sentence,
                    merged,
                    {c.hub_ref_id for c in passage_hubs},
                    containment_fn,
                    timer=timer,
                )
        except Upstream as exc:
            # dedup='force' is the documented escape from a judge outage
            # (the Upstream's next=): mint, and record that it went unjudged.
            # A stage-one outage already stamped the marker — keep its
            # reason; the verdict is the same 'unjudged'.
            if mode != "force":
                raise
            hit = None
            if not (extra_meta or {}).get("dedup_override"):
                extra_meta = {
                    **(extra_meta or {}),
                    "dedup_override": {
                        "verdict": "unjudged",
                        "reason": str(exc.cause)[:300],
                    },
                }
        if hit is not None:
            cand, verdict = hit
            if mode != "force":
                raise _near_duplicate_refusal(store, sentence, cand, verdict)
            containment = {
                "neighbour": handle_registry.format_handle("finding", cand.hub_ref_id),
                "verdict": verdict["verdict"],
                "reason": verdict["rationale"],
            }
            stamped = (extra_meta or {}).get("dedup_override")
            if stamped is not None and stamped.get("verdict") == "unjudged":
                # A stage-one outage already stamped the re-judge marker
                # (gr639349): keep it — the containment verdict rides
                # alongside it, never replaces it (td650548).
                stamped = {**stamped, "containment": containment}
            else:
                stamped = containment
            extra_meta = {**(extra_meta or {}), "dedup_override": stamped}

    if placement.action == "attach":
        with timer.phase("mint"):
            return _attach_to_existing(
                store,
                claim,
                placement,
                judged,
                supporters,
                set_by=set_by,
                extra_meta=extra_meta,
                from_memory=from_memory,
                extras=extras,
            )
    if placement.action == "needs_review":
        with timer.phase("mint"):
            return _mint_with_review_todo(
                store,
                claim,
                placement,
                supporters,
                set_by=set_by,
                extra_meta=extra_meta,
                extras=extras,
            )
    with timer.phase("mint"):
        return _mint_new(
            store,
            claim,
            placement,
            supporters,
            set_by=set_by,
            extra_meta=extra_meta,
            extras=extras,
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
    supporter's ``source_handle`` names, plus the still-unembedded hubs of the
    supporters' papers — same-burst siblings, which an ANN
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
        passage = list(found.values())[:_SAME_PASSAGE_MAX]
        seen = {c.hub_ref_id for c in passage}
        # gr530995: a reworded restatement of a claim from ANOTHER passage of
        # the same paper (abstract vs results) sits in the same embedding
        # window, so the passage lookup above never sees it either.
        for paper_id in dict.fromkeys(int(s["paper_ref_id"]) for s in resolved):
            rows = conn.execute(
                f"""
                SELECT r.ref_id, r.title
                FROM refs r
                JOIN chunks c ON c.ref_id = r.ref_id AND c.ord = 0
                             AND c.chunk_kind = 'finding_body'
                             AND c.retired_at IS NULL
                WHERE r.kind = 'finding' AND r.retired_at IS NULL
                  AND {claim_hub_predicate_sql()}
                  AND EXISTS (SELECT 1 FROM links l
                              WHERE l.src_ref_id = %(paper)s
                                AND l.dst_ref_id = r.ref_id)
                  AND NOT EXISTS (SELECT 1 FROM chunk_embeddings ce
                                  WHERE ce.chunk_id = c.chunk_id
                                    AND ce.status = 'ok')
                ORDER BY r.ref_id DESC
                LIMIT %(n)s
                """,
                {
                    "paper": paper_id,
                    "n": _UNEMBEDDED_PAPER_MAX,
                    **CLAIM_HUB_PREDICATE_PARAMS,
                },
            ).fetchall()
            for ref_id, title in rows:
                if int(ref_id) not in seen:
                    seen.add(int(ref_id))
                    passage.append(
                        MergeCandidate(
                            hub_ref_id=int(ref_id), claim=str(title), distance=1.0
                        )
                    )
    return passage


def _containment_hit(
    sentence: str,
    merged: list[MergeCandidate],
    passage_ids: set[int],
    containment_fn: ContainmentFn,
    *,
    timer: _PhaseTimer | None = None,
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
    if timer is not None:
        timer.containment_candidates = len(cands)
    for cand, v in judge_containment(
        sentence, cands, containment_fn, on_retry=timer.retry if timer else None
    ):
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
    extra_meta: dict[str, Any] | None = None,
    from_memory: str | None = None,
    extras: PutExtras | None = None,
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
        store,
        claim,
        placement,
        judged,
        supporters,
        set_by=set_by,
        extra_meta=extra_meta,
        from_memory=from_memory,
        extras=extras,
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
    extra_meta: dict[str, Any] | None = None,
    from_memory: str | None = None,
    extras: PutExtras | None = None,
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
        store,
        claim,
        placement,
        judged,
        supporters,
        set_by=set_by,
        extra_meta=extra_meta,
        from_memory=from_memory,
        extras=extras,
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
    extra_meta: dict[str, Any] | None = None,
    from_memory: str | None = None,
    extras: PutExtras | None = None,
) -> Response:
    """``attach`` — the closest verdict was a confirmed ``same``: attach
    ``supporters`` as evidence on ``placement.hub_ref_id`` directly
    (:func:`~precis.taproot.hub.attach_evidence`), never through
    :func:`~precis.taproot.authoring.seed_claim_hub` (which mints/converges
    on THIS claim's own ``(sentence, scope)`` identity — exactly the
    identity this branch is bypassing in favour of the matched neighbour).

    The converge mints no ref, so the writer annotation
    (``extra_meta.{model,prompt_version,run_id,why}``) has no row to land
    on — it rides a ``considered`` edge from the run's scope to the
    matched hub instead (gr626718, the link-reason ruling's positive
    half): verdict ``useful``, the run's ``why`` as the reason, the
    complete supporter set on the edge (grounded pins in ``passages``,
    every supporter incl. ref-level sources in ``supporters``), the
    env-attributed run in ``run`` when ``run_id`` is absent. The edge
    write shares this transaction — a failure rolls the attached
    evidence back too and the put fails audibly for retry (atomic
    retention: a reported converge always kept its observation,
    never a silent drop). The ``considerations`` history keeps one
    row per judgment, so two runs converging on the same hub each
    leave their own reasoned record — the fix-forward audit trail
    mint-time dedup used to drop.

    ``extras`` (the put's create-time tags and link, gr676914) land on
    the MATCHED hub in the same transaction, and the response's
    ``applied to`` line names it; a refused extra rolls the attached
    evidence back too.
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

    scope = _observation_scope(store, extra_meta, from_memory)
    obs_meta = (
        _observation_meta(store, resolved, extra_meta, set_by=set_by)
        if scope is not None
        else None
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
    observed = False
    applied = ""
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

        if scope is not None:
            # The run's observation (gr626718) shares the converge's
            # transaction — atomic retention, the gripe's contract: an
            # edge-write failure rolls the evidence back too and the put
            # fails audibly for retry, never reporting a converge whose
            # reasoned edge silently dropped.
            store.add_link(
                src_ref_id=scope[0],
                dst_ref_id=hub_ref_id,
                relation="considered",
                meta=obs_meta,
                set_by=set_by,
                conn=conn,
            )
            observed = True

        if extras is not None:
            applied = "\n" + apply_put_extras(
                store, extras, hub_ref_id, conn=conn, set_by=set_by
            )

    # Transaction committed -- now safe to reach the network (trigger 1).
    run_retraction_checks(store, owned_checks, hub_ref_id=hub_ref_id)

    hub_handle = handle_registry.format_handle("finding", hub_ref_id)
    body = (
        f"converged onto {hub_handle} (same claim: {neighbour_sentence[:120]})\n"
        f"claim: {claim.sentence[:120]}\n"
        f"evidence: {attached} attached, {already} already present"
        + (f", {ungrounded} ref-level (ungrounded)" if ungrounded else "")
        + f"\nreason: {placement.reason}\n"
        + (
            f"observation: {scope[1]} --considered[useful]--> {hub_handle} "
            "(this run's reasoned edge)\n"
            if observed and scope is not None
            else ""
        )
        + f"cite it inline as [{hub_handle}] — resolves to the current derived "
        "originator(s) on every render" + applied
    )
    return Response(body=body)


def _observation_scope(
    store: Store, extra_meta: dict[str, Any] | None, from_memory: str | None
) -> tuple[int, str] | None:
    """The ``(ref_id, handle)`` this run's converge-observation edge hangs
    on — the scope of its ``considered`` verdict.

    Resolution order: the caller's ``from_memory`` provenance (the
    memory/quest that reasoned its way here — same param the hypothesis
    door uses for its origin links), then ``meta.run_id`` resolved to a
    live agentlog (``ag<N>`` handle or a bare id), then the run the
    environment attributes this write to (``PRECIS_CURRENT_AGENTLOG``).
    ``None`` when nothing resolves — a put with no run identity records
    no observation rather than inventing a scope.
    """
    if from_memory:
        try:
            resolved = store.resolve_handle(from_memory)
        except Exception:
            log.warning(
                "hub-mint: unresolvable from_memory=%r", from_memory, exc_info=True
            )
            resolved = None
        if resolved is not None:
            # A chunk handle scopes to its owning ref — the considered
            # edge judges the scope ref, never a chunk endpoint.
            handle = handle_registry.try_format(resolved.kind, int(resolved.ref_id))
            if handle is not None:
                return int(resolved.ref_id), handle
    run_id = (extra_meta or {}).get("run_id")
    if run_id is not None and str(run_id).strip():
        parsed = handle_registry.parse(str(run_id).strip())
        ref_id: int | None = None
        if parsed is not None and parsed[0] == "agentlog" and not parsed[1]:
            ref_id = parsed[2]
        elif str(run_id).strip().isdigit():
            ref_id = int(str(run_id).strip())
        if ref_id is not None and store.get_ref(kind="agentlog", id=ref_id) is not None:
            return ref_id, handle_registry.format_handle("agentlog", ref_id)
    env_id = _env_run_ref_id(store)
    if env_id is not None:
        return env_id, handle_registry.format_handle("agentlog", env_id)
    return None


def _env_run_ref_id(store: Store) -> int | None:
    """The environment-attributed run's agentlog ``ref_id``, or ``None``
    — ``PRECIS_CURRENT_AGENTLOG`` / the in-process tick override
    (:func:`precis.agentlog.current_from_env`), gated on the ref
    actually being a live agentlog."""
    from precis import agentlog

    env_id = agentlog.current_from_env()
    if env_id is not None and store.get_ref(kind="agentlog", id=env_id) is not None:
        return int(env_id)
    return None


def _supporter_identities(store: Store, resolved: list[dict[str, Any]]) -> list[str]:
    """Every resolved supporter's identity for the observation edge —
    its ``source_handle`` pin when the supporter is passage-grounded,
    else the live source ref's universal handle (``pa<N>``/``pt<N>``/…)
    so a ref-level supporter still names the evidence the run leaned
    on. Falls back to the supporter's raw ``paper`` token when the
    ref's kind has no handle code (defensive — ``resolve_supporters``
    already gated the kind to an evidence source)."""
    refs = store.fetch_refs_by_ids(
        [int(s["paper_ref_id"]) for s in resolved], include_deleted=False
    )
    out: list[str] = []
    for s in resolved:
        source_handle = s.get("source_handle")
        if source_handle:
            out.append(str(source_handle))
            continue
        ref = refs.get(int(s["paper_ref_id"]))
        handle = (
            handle_registry.try_format(ref.kind, int(ref.id))
            if ref is not None
            else None
        )
        out.append(handle or str(s.get("paper") or s["paper_ref_id"]))
    return out


def _observation_meta(
    store: Store,
    resolved: list[dict[str, Any]],
    extra_meta: dict[str, Any] | None,
    *,
    set_by: ActorSlug,
) -> dict[str, Any]:
    """The ``considered``-edge meta for one run's converge observation —
    the writer annotation restated in the verdict block's spelling
    (``run_id`` → ``run``, ``prompt_version`` → ``prompt``), the run's
    ``why`` as the reason, and this run's complete evidence association
    on the edge: ``passages`` keeps every supporter's grounding chunk
    pin (no cap — a truncated pin list would lose supporters) and
    ``supporters`` names every resolved supporter, ref-level ones by
    their source handle. ``run`` falls back to the env-attributed
    agentlog when ``run_id`` is absent, so a ``from_memory``-scoped
    judgment still names the run that wrote it. Falls back to a
    generic reason when ``why`` is absent or a content-free marker —
    ``validate_considered_meta`` requires a real sentence, and the
    endpoint-text refusal (gr626896) never sees the claim restated
    here since the reason names the mechanism, not the sentence."""
    meta = dict(extra_meta or {})
    why = str(meta.get("why") or "").strip()
    if why in (".", "reviewed-obvious"):
        why = ""
    passages = [str(s["source_handle"]) for s in resolved if s.get("source_handle")]
    out: dict[str, Any] = {
        "verdict": "useful",
        "reason": why or "dedup converge — this run reached the same claim",
        "depth": "passages" if passages else "metadata",
        "actor": str(set_by),
        "supporters": _supporter_identities(store, resolved),
    }
    if passages:
        out["passages"] = passages
    run = str(meta.get("run_id") or "").strip()
    if not run:
        env_run = _env_run_ref_id(store)
        if env_run is not None:
            run = handle_registry.format_handle("agentlog", env_run)
    for key, value in (
        ("run", run),
        ("model", str(meta.get("model") or "").strip()),
        ("prompt", str(meta.get("prompt_version") or "").strip()),
    ):
        if value:
            out[key] = value
    return out


# ── new / new_contradicts — mint as today ───────────────────────────────


def _seed(
    store: Store,
    claim: CanonicalClaim,
    supporters: list[dict[str, Any]],
    *,
    set_by: ActorSlug,
    extra_meta: dict[str, Any] | None,
    extras: PutExtras | None,
) -> tuple[dict[str, Any], str]:
    """:func:`~precis.taproot.authoring.seed_claim_hub` with the put's
    create-time ``extras`` written inside the mint's transaction (its
    ``in_tx`` hook), so a refused tag or link rolls the hub and its evidence
    back (gr676914). Returns the seed result and the ``applied to`` response
    line (newline-led; empty without extras)."""
    lines: list[str] = []

    def _apply(conn: Any, hub_ref_id: int) -> None:
        assert extras is not None
        lines.append(
            apply_put_extras(store, extras, hub_ref_id, conn=conn, set_by=set_by)
        )

    result = authoring.seed_claim_hub(
        store,
        sentence=claim.sentence,
        scope=claim.scope,
        supporters=supporters,
        set_by=set_by,
        extra_meta=extra_meta,
        in_tx=_apply if extras is not None else None,
    )
    return result, "".join(f"\n{line}" for line in lines)


def _mint_new(
    store: Store,
    claim: CanonicalClaim,
    placement: Placement,
    supporters: list[dict[str, Any]],
    *,
    set_by: ActorSlug,
    extra_meta: dict[str, Any] | None = None,
    extras: PutExtras | None = None,
) -> Response:
    """``new`` / ``new_contradicts`` — no matching claim: mint through the
    existing door unchanged. A ``new_contradicts`` verdict additionally
    writes the hub<->hub ``disputes`` link
    :func:`~precis.taproot.hub.apply_placement` writes for the automated
    cascade callers (``directed_mint``/backfill/chase) — ``add_link`` is
    idempotent on its unique tuple, so this is a plain reuse of that
    shape, not a second implementation of it."""
    result, applied = _seed(
        store,
        claim,
        supporters,
        set_by=set_by,
        extra_meta=extra_meta,
        extras=extras,
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
    return Response(body=_mint_response_body(result, claim.sentence) + applied)


# ── needs_review — mint anyway, file one todo naming both hubs ──────────


def _mint_with_review_todo(
    store: Store,
    claim: CanonicalClaim,
    placement: Placement,
    supporters: list[dict[str, Any]],
    *,
    set_by: ActorSlug,
    extra_meta: dict[str, Any] | None = None,
    extras: PutExtras | None = None,
) -> Response:
    """``needs_review`` — a risky, unconfirmed ``same`` verdict. The
    automated cascade callers (``apply_placement``) attach nothing here and
    rely on a later re-run; an agent's ``put`` has no re-run to fall back
    on, so this door mints anyway and files ONE ``kind='todo'`` naming both
    hubs for a human to adjudicate (module docstring)."""
    result, applied = _seed(
        store,
        claim,
        supporters,
        set_by=set_by,
        extra_meta=extra_meta,
        extras=extras,
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
        "originator(s) on every render" + applied
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
    # gr530995: these two codes hard-block at mint-preflight, so the put
    # response says so in approval terms — the hub exists, but approve
    # will refuse it as written.
    mint_blockers = result.get("sentence_lint") or []
    blocker_note = (
        "\nmint-preflight will REFUSE these (reword before approve):\n"
        + "\n".join(f"  - {w}" for w in mint_blockers)
        if mint_blockers
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
        "originator(s) on every render" + blocker_note + lint_note
    )
