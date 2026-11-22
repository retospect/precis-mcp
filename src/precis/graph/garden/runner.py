"""The garden tick — detectors → proposer → proposals.

:func:`run_garden_tick` is the whole pass the ``graph_garden`` job runs:
collect candidates (bounded), skip keys already pending or already
rejected, ask the proposer, emit accepted proposals and record rejected
ones (so the next run does not re-ask; gr506046). The only writes are
``reviews`` rows — plus, for an evaluator ``ask`` on a memory-body
candidate, one ``memory-review`` todo pointing at the memory (gr560762)
whose body carries the ask reason, detector, pinned sha and a bounded
evidence excerpt (gr732271), and for a ``griped`` no-change the
replay-after close of the complaint itself once no sibling fix path
remains (gr561873).

``review_due`` answers review debt, so its verdicts discharge it
(gr732278): a no-change stamps the review itself — the
``kg-review:<model>:<date>:ok`` consider act on the pinned sha, in the
same transaction as the rejection row — so the memory leaves the review
queue until its content changes, its expiry passes or the review ages,
and an accepted change routes to a human ``memory-review`` todo instead
of a proposal, because the detector has no apply door and an emitted
proposal would sit pending forever.

The ``max_proposals`` budget is shared round-robin across the detectors
that produced candidates (gr504843): each gets ``max_proposals // n``
first, and whatever a detector leaves unspent goes to the others. A
high-volume detector (dangling_edge) can no longer take the whole tick
because it sorts first. Reader complaints go before the round-robin
(gr657257): the ``griped`` queue — open ``project:memory`` gripes, newest
first — is drained against the budget before any other detector turns.

``propose=`` is the test seam: a callable ``(store, candidate) ->
Verdict``. Default :func:`propose_llm` (one routed call per candidate).
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.graph.garden.candidates import (
    CONSIDERED_DAYS,
    DETECTOR_DEADLINE_S,
    GARDEN_KINDS,
    SIBLING_REJECT_REASON,
    STALE_DAYS,
    GardenCandidate,
    run_detectors,
    short_sibling_pair,
)
from precis.graph.garden.proposals import emit_proposal, key_block, record_rejection
from precis.graph.garden.proposer import (
    COMPACT_PROMPT_VERSION,
    MEMORY_PROMPT_VERSION,
    PROMPT_VERSION,
    Verdict,
    propose_llm,
)
from precis.utils.memory_reconcile import uses_memory_policy

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Per-tick ceilings — the "bounded" half of "runs bounded per tick".
#: ``limit`` caps candidates per detector; ``max_proposals`` caps the
#: proposals one tick may write.
DEFAULT_LIMIT = 50
DEFAULT_MAX_PROPOSALS = 20
#: Detectors drained ahead of the round-robin, in order (gr657257): an
#: open reader complaint outranks every self-found smell.
FIRST_DETECTORS: tuple[str, ...] = ("griped",)

#: The evaluator's "ask" lands as a memory-review todo (gr560762); the
#: tier follows the smell class — content smells need repo/entity
#: archaeology (opus), shape smells are mechanical (sonnet).
_ASK_LLM_TIERS = {
    "anchor_rot": "opus",
    "compact": "opus",
    "dangling_mention": "opus",
    "entity_claim_stale": "opus",
    "griped": "opus",
    "host_facts": "opus",
    "negative_undated": "opus",
    "notation": "sonnet",
    "restated_claim": "opus",
    "review_due": "opus",
    "source_drift": "opus",
    "thread_shape": "sonnet",
    "title_shape": "sonnet",
}


def _gripe_review_filed(store: Store, memory_id: int, gripe_id: int) -> bool:
    """Any ``memory-review`` todo, open *or closed*, already linking this
    (memory, gripe) pair. A closed review is the answer to the ask — a
    re-mint would repeat the identical opus review (gr652974)."""
    from precis.utils.memory_hubs import REVIEW_TAG

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM refs t "
            "WHERE t.kind = 'todo' AND t.retired_at IS NULL "
            "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags g "
            "ON g.tag_id = rt.tag_id WHERE rt.ref_id = t.ref_id "
            "AND g.namespace = 'OPEN' AND g.value = %s) "
            "AND EXISTS (SELECT 1 FROM links l WHERE l.src_ref_id = t.ref_id "
            "AND l.relation = 'raises-concern-about' AND l.dst_ref_id = %s) "
            "AND EXISTS (SELECT 1 FROM links l WHERE l.src_ref_id = t.ref_id "
            "AND l.relation = 'raises-concern-about' AND l.dst_ref_id = %s) "
            "LIMIT 1",
            (REVIEW_TAG, memory_id, gripe_id),
        ).fetchone()
    return row is not None


def _completed_review_filed(
    store: Store, memory_id: int, detector: str, sha: str
) -> bool:
    """A completed review at the same pin already settled this detector's ask."""
    from precis.store.types import TODO_BODY_KIND
    from precis.utils.memory_hubs import REVIEW_TAG

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM links l JOIN refs t ON t.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = %s AND l.relation = 'raises-concern-about' "
            "AND t.kind = 'todo' AND t.retired_at IS NULL "
            "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags g "
            "ON g.tag_id = rt.tag_id WHERE rt.ref_id = t.ref_id "
            "AND g.namespace = 'OPEN' AND g.value = %s) "
            "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags g "
            "ON g.tag_id = rt.tag_id WHERE rt.ref_id = t.ref_id "
            "AND g.namespace = 'STATUS' AND g.value = 'done') "
            "AND EXISTS (SELECT 1 FROM chunks c WHERE c.ref_id = t.ref_id "
            "AND c.chunk_kind = %s AND strpos(c.text, %s) > 0) "
            "LIMIT 1",
            (
                memory_id,
                REVIEW_TAG,
                TODO_BODY_KIND,
                f"\n{_review_pin(detector, sha)}\n",
            ),
        ).fetchone()
    return row is not None


def _review_pin(detector: str, sha: str) -> str:
    return f"Detector: {detector}\nPinned content sha: {sha}"


#: Bound on the detector-evidence JSON excerpt a review todo's body
#: carries — enough to name the failed anchors, retired handles or
#: compact sentences the worker starts from (gr732271).
_ASK_EVIDENCE_CHARS = 2000


def _review_todo_body(cand: GardenCandidate, verdict: Verdict, sha: str | None) -> str:
    """The review todo's details body: the evaluator's ask reason, the
    detector and the content sha it pinned, a bounded evidence excerpt
    and the pointer to the work-the-todo skill section (gr732271)."""
    evidence = json.dumps(
        cand.evidence, ensure_ascii=False, sort_keys=True, default=str
    )
    if len(evidence) > _ASK_EVIDENCE_CHARS:
        evidence = evidence[:_ASK_EVIDENCE_CHARS].rstrip() + "…"
    lines = [
        f"The garden evaluator's ask needs a human on memory me{cand.target_id}.",
        "",
        f"Ask: {verdict.ask}",
        _review_pin(cand.detector, sha) if sha else f"Detector: {cand.detector}",
    ]
    gripe = cand.payload.get("gripe")
    if isinstance(gripe, int):
        lines.append(f"Filing gripe: gr{gripe}")
    reason = cand.evidence.get("reason")
    if cand.detector == "griped" and isinstance(reason, str) and reason:
        lines.append(f"Fix asked: {reason}")
    lines += [
        "",
        f"Detector evidence (bounded): {evidence}",
        "",
        "Brief: get(kind='skill', id='precis-memory-help'), section "
        '"Working a memory-review todo".',
    ]
    return "\n".join(lines)


def _file_review_todo(store: Store, cand: GardenCandidate, verdict: Verdict) -> None:
    """File the evaluator's ask: one ``memory-review`` todo pointing at
    the memory, deduped on an already-open review todo or a completed
    review for this detector and content pin (the key's
    rejection row stops the re-ask; the todo tracks the human follow-up).
    The minted body carries the ask reason, detector, pinned sha and a
    bounded evidence excerpt so the worker does not re-derive the
    question from the bare title (gr732271).
    """
    from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
    from precis.utils.memory_hubs import REVIEW_TAG, review_todos

    if review_todos(store, [cand.target_id]):
        return
    gripe = cand.payload.get("gripe") if cand.detector == "griped" else None
    if isinstance(gripe, int) and _gripe_review_filed(store, cand.target_id, gripe):
        return
    sha = cand.payload.get("base_sha") or cand.evidence.get("content_sha")
    if (
        isinstance(sha, str)
        and sha
        and _completed_review_filed(store, cand.target_id, cand.detector, sha)
    ):
        return
    title = f"review memory me{cand.target_id}: {cand.title}"[:200]
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=title,
        meta={"llm_tier": _ASK_LLM_TIERS.get(cand.detector, "opus")},
    )
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text=_review_todo_body(cand, verdict, sha),
                meta={"chunk_kind": TODO_BODY_KIND},
            )
        ],
    )
    for tag in (Tag.parse_strict("STATUS:open", kind="todo"), Tag.open(REVIEW_TAG)):
        store.add_tag(ref.id, tag, set_by="system")
    store.add_link(
        src_ref_id=ref.id,
        dst_ref_id=cand.target_id,
        relation="raises-concern-about",
        set_by="system",
    )
    if cand.detector == "griped" and isinstance(cand.payload.get("gripe"), int):
        # The reader follows the complaint's story, not only the node:
        # the review todo links the filing gripe too.
        store.add_link(
            src_ref_id=ref.id,
            dst_ref_id=int(cand.payload["gripe"]),
            relation="raises-concern-about",
            set_by="system",
        )


def _close_no_change_gripe(
    store: Store,
    cand: GardenCandidate,
    rationale: str,
    queues: dict[str, deque[GardenCandidate]],
    open_gripes: set[int],
) -> None:
    """The evaluator's no-change on a griped candidate is the complaint's
    resolution when the deterministic replay pass agrees — the runner
    records the rejection for dedup, then closes the gripe through the
    same replay-after door the applied close uses. Sibling candidates
    still queued, a pending proposal, or any live fix path this tick
    keeps it open; so does a failing pass — a silent close never lands."""
    from precis.graph.garden.consumer import _close_griped_gripe

    gripe = cand.payload.get("gripe")
    if not isinstance(gripe, int):
        return
    if gripe in open_gripes:
        return
    if any(c.payload.get("gripe") == gripe for c in queues.get(cand.detector, ())):
        return  # a sibling (gripe, memory) pair may still carry the fix
    pinned = cand.evidence.get("replay")
    try:
        _close_griped_gripe(
            store,
            gripe,
            (
                f"resolved: garden:griped replay pass — evaluator no-change on "
                f"me{cand.target_id} ({rationale or 'no repair needed'}); "
                "routes still arrive, answers consistent or scoped, handles "
                "resolve, hub recall preserved"
            ),
            pinned_replay=pinned if isinstance(pinned, list) else None,
        )
    except Exception:
        # The gripe staying open is safe; the recorded rejection still
        # blocks the re-ask and the fault must not abort the tick.
        log.warning(
            "garden: griped no-change close failed on gr%s", gripe, exc_info=True
        )


ProposerFn = Callable[["Store", GardenCandidate], "Verdict | None"]


def _record_review_no_change(
    store: Store,
    cand: GardenCandidate,
    verdict: Verdict,
    prompt_version: str,
) -> None:
    """A ``review_due`` no-change IS the review (gr732278): record the
    proposer's rejection row and the ``kg-review:<model>:<date>:ok``
    consider act — expected_sha pinned to the candidate's base_sha,
    carrying the checklist version the judge answered under — in one
    transaction. The stamped memory leaves the review queue (it
    re-enters on content change, expiry or age) instead of staying
    ``never_reviewed`` behind a rejection row that only hides it until
    the prompt version bumps. A drifted target keeps the plain
    rejection: ``sha_changed`` re-queues it honestly.
    """
    from datetime import UTC, datetime

    from precis.graph.garden.apply import _review_model_token
    from precis.graph.garden.proposals import GARDEN_ACTOR
    from precis.store.revision_context import TrustAct

    base_sha = str(
        cand.payload.get("base_sha") or cand.evidence.get("content_sha") or ""
    )
    with store.atomic() as conn:
        row = conn.execute(
            "SELECT precis_target_sha('ref', %s) FROM refs "
            "WHERE ref_id = %s FOR UPDATE",
            (cand.target_id, cand.target_id),
        ).fetchone()
        record_rejection(
            store,
            cand,
            rationale=verdict.rationale,
            prompt_version=prompt_version,
            model=verdict.model,
        )
        if row is None or not base_sha or str(row[0]) != base_sha:
            log.warning(
                "garden: review_due no-change on me%s without a sha pin match "
                "— rejection only",
                cand.target_id,
            )
            return
        store.record_trust_act(
            TrustAct(
                action="consider",
                target_kind="ref",
                target_id=cand.target_id,
                expected_sha=str(row[0]),
                reason=(
                    f"kg-review:{_review_model_token(verdict.model)}:"
                    f"{datetime.now(UTC):%Y-%m-%d}:ok"
                ),
                prompt_version=prompt_version,
            ),
            actor=GARDEN_ACTOR,
            actor_type="agent",
            model=verdict.model,
        )


@dataclass(slots=True)
class GardenOutcome:
    """One tick's tallies — stamped on the job's ``meta.garden``."""

    candidates: int = 0
    proposals: int = 0
    skipped_pending: int = 0
    #: Candidates whose key was rejected (by a judge, or by the proposer
    #: under the current prompt) and whose target is unchanged since —
    #: not re-asked (gr504843, gr506046).
    skipped_rejected: int = 0
    #: Candidates dropped before the proposer because their target was
    #: considered-and-unchanged inside the review window
    #: (:data:`~precis.graph.garden.candidates.CONSIDERED_DAYS`;
    #: ``griped`` and ``review_due`` candidates are exempt).
    skipped_considered: int = 0
    rejected: int = 0
    #: Evaluator "ask" verdicts on memory-body candidates — each files a
    #: memory-review todo and closes the key under this prompt version
    #: (gr560762).
    asked: int = 0
    #: Accepted ``review_due`` changes routed to a human review todo —
    #: the detector has no apply door, so an emitted proposal would sit
    #: pending forever (gr732278). The key closes under this prompt
    #: version like an ask.
    routed: int = 0
    #: Rejected by the short-sibling rule before any model call
    #: (``reason='sibling_distinct_names'`` on the row; gr504843).
    rule_rejected: int = 0
    llm_errors: int = 0
    #: The share of ``llm_errors`` cut on the completion-length cap
    #: (gr504896) — a cap too small for the model's reasoning.
    llm_truncated: int = 0
    paused: bool = False
    detectors: list[str] = field(default_factory=list)
    #: Detectors skipped on their timeout/budget this tick (gr503580) —
    #: the job summary names them so a partial tick is visible.
    skipped_detectors: list[str] = field(default_factory=list)
    #: Unavailable evidence sources; never counted as maintenance findings.
    not_checked: dict[str, str] = field(default_factory=dict)
    #: Proposals written per detector — the round-robin budget's spend.
    proposals_by_detector: dict[str, int] = field(default_factory=dict)
    #: Grounding-handoff claims commissioned this tick (gr634487) —
    #: minted, reused or asked; capped at
    #: :data:`~precis.graph.garden.handoff.MAX_HANDOFFS_PER_TICK`.
    handoffs: int = 0
    #: Scan positions for the next run (:attr:`DetectorRun.cursors`).
    cursors: dict[str, int] = field(default_factory=dict)


def run_garden_tick(
    store: Store,
    *,
    passes: tuple[str, ...] = ("all",),
    kinds: tuple[str, ...] = GARDEN_KINDS,
    limit: int = DEFAULT_LIMIT,
    scan_cap: int = 2000,
    stale_days: int = STALE_DAYS,
    tag_namespaces: tuple[str, ...] = ("DOMAIN", "TOPIC"),
    tag_kinds: tuple[str, ...] = ("finding", "paper", "concept", "memory"),
    exclusive_axes: tuple[str, ...] = (),
    max_proposals: int = DEFAULT_MAX_PROPOSALS,
    detector_deadline_s: float = DETECTOR_DEADLINE_S,
    cursors: dict[str, int] | None = None,
    considered_days: int = CONSIDERED_DAYS,
    propose: ProposerFn | None = None,
    embedder: Any | None = None,
) -> GardenOutcome:
    """One bounded tick writing review rows and grounding follow-up todos.

    ``propose=None`` uses the routed LLM proposer. A paused verdict stops
    the tick — remaining candidates are re-found next tick (dedup makes
    that free). ``embedder`` (when wired) lets the grounding audit's
    bounded conflict probe retrieve candidates; without one the probe
    degrades to ``not_probed`` rather than spending nothing on silence.
    """
    if propose is None:
        propose = propose_llm
    run = run_detectors(
        store,
        passes=passes,
        kinds=kinds,
        limit=limit,
        scan_cap=scan_cap,
        stale_days=stale_days,
        tag_namespaces=tag_namespaces,
        tag_kinds=tag_kinds,
        exclusive_axes=exclusive_axes,
        detector_deadline_s=detector_deadline_s,
        cursors=cursors,
        considered_days=considered_days,
    )
    candidates = run.candidates
    out = GardenOutcome(
        candidates=len(candidates),
        skipped_considered=run.skipped_considered,
        skipped_detectors=list(run.skipped),
        not_checked=dict(run.not_checked),
        cursors=dict(run.cursors),
    )
    # Gripe ids with a live fix path this tick — an ask, an emitted or
    # pending proposal, an error or a pause. A no-change verdict closes the
    # gripe only while nothing else may still repair it.
    open_gripes: set[int] = set()

    def _one(cand: GardenCandidate) -> bool:
        """Propose one candidate; True when a proposal row was written.
        Sets ``out.paused`` on a window-scoped pause."""
        if cand.detector == "grounding_audit":
            from precis.graph.garden.grounding import (
                GroundingPaused,
                audit_candidate,
                latest_audit,
            )
            from precis.graph.garden.handoff import (
                MAX_HANDOFFS_PER_TICK,
                commission_handoffs,
            )

            result = None
            if cand.action == "audit":
                try:
                    # The audit row and its repair proposals all land in the
                    # reviews ledger, so they spend the tick's shared budget:
                    # repairs are capped at what remains after this audit's
                    # own row, and the outcome counts what was actually
                    # written (dedup may emit fewer than the cap).
                    result = audit_candidate(
                        store,
                        cand,
                        embedder=embedder,
                        repair_limit=max_proposals - out.proposals - 1,
                    )
                except GroundingPaused:
                    out.paused = True
                    return False
            record = latest_audit(store, cand.target_id)
            if (
                record is not None
                and record.current
                and record.target_sha == cand.payload["sha"]
            ):
                out.handoffs += commission_handoffs(
                    store,
                    cand.target_id,
                    record,
                    cap=MAX_HANDOFFS_PER_TICK - out.handoffs,
                )
            if result is None:
                return False
            _review_id, repairs = result
            out.proposals += 1 + repairs
            out.proposals_by_detector[cand.detector] = (
                out.proposals_by_detector.get(cand.detector, 0) + 1 + repairs
            )
            return True
        prompt_version = (
            MEMORY_PROMPT_VERSION
            if uses_memory_policy(cand.detector, cand.evidence, cand.payload)
            else PROMPT_VERSION
        )
        if cand.detector == "compact":
            prompt_version = COMPACT_PROMPT_VERSION
        gid = (
            int(cand.payload["gripe"])
            if cand.detector == "griped" and isinstance(cand.payload.get("gripe"), int)
            else None
        )
        block = key_block(
            store,
            cand.target_kind,
            cand.target_id,
            cand.key,
            prompt_version=prompt_version,
        )
        if block == "rejected":
            out.skipped_rejected += 1
            return False
        if block is not None:
            out.skipped_pending += 1
            if gid is not None:
                open_gripes.add(gid)
            return False
        if (
            cand.detector == "dup_pair"
            and cand.other_id
            and short_sibling_pair(store, cand.target_id, int(cand.other_id))
        ):
            # Deterministic, no model call (gr504843: 8288/8300/8302).
            out.rule_rejected += 1
            record_rejection(
                store,
                cand,
                rationale="short sibling taxa under one parent share no name word",
                prompt_version=prompt_version,
                reason=SIBLING_REJECT_REASON,
            )
            return False
        try:
            verdict = propose(store, cand)
        except Exception as exc:
            log.warning("garden: proposer raised on %s: %s", cand.key, exc)
            out.llm_errors += 1
            if gid is not None:
                open_gripes.add(gid)
            return False
        if verdict is None:
            if gid is not None:
                open_gripes.add(gid)
            return False
        if verdict.paused:
            out.paused = True
            if gid is not None:
                open_gripes.add(gid)
            return False
        if verdict.error is not None:
            out.llm_errors += 1
            out.llm_truncated += int(verdict.truncated)
            if gid is not None:
                open_gripes.add(gid)
            return False
        if verdict.ask is not None:
            out.asked += 1
            _file_review_todo(store, cand, verdict)
            record_rejection(
                store,
                cand,
                rationale=verdict.ask,
                prompt_version=prompt_version,
                model=verdict.model,
                reason="asked",
            )
            if gid is not None:
                open_gripes.add(gid)
            return False
        if not verdict.accept:
            out.rejected += 1
            if cand.detector == "review_due":
                _record_review_no_change(store, cand, verdict, prompt_version)
            else:
                record_rejection(
                    store,
                    cand,
                    rationale=verdict.rationale,
                    prompt_version=prompt_version,
                    model=verdict.model,
                )
            if gid is not None:
                _close_no_change_gripe(
                    store, cand, verdict.rationale, queues, open_gripes
                )
            return False
        # The proposer may refine the action/payload; the dedup key stays
        # the deterministic candidate key.
        effective = cand
        if verdict.action is not None and verdict.action != cand.action:
            effective = _replace(cand, action=verdict.action)
        if verdict.payload_overrides:
            merged = dict(effective.payload)
            merged.update(verdict.payload_overrides)
            effective = _replace(effective, payload=merged)
        if cand.detector == "review_due":
            # gr732278: review_due has no apply door — an emitted proposal
            # would sit pending forever. The human review todo is the door:
            # it carries the accepted change, and the rejection row closes
            # the key under this prompt version.
            out.routed += 1
            ask = (
                f"the evaluator accepted a {effective.action} change: "
                f"{verdict.rationale or 'no reason given'}. review_due has "
                "no apply door — verify the change and carry it out by hand"
            )
            _file_review_todo(
                store,
                effective,
                Verdict(
                    accept=False,
                    rationale=verdict.rationale,
                    ask=ask,
                    model=verdict.model,
                ),
            )
            record_rejection(
                store,
                effective,
                rationale=verdict.rationale,
                prompt_version=prompt_version,
                model=verdict.model,
                reason="accepted_no_apply",
            )
            return False
        emitted = emit_proposal(
            store,
            effective,
            rationale=verdict.rationale,
            model=verdict.model,
            key=cand.key,
        )
        if emitted is None:
            out.skipped_pending += 1
            if gid is not None:
                open_gripes.add(gid)
            return False
        if gid is not None:
            open_gripes.add(gid)
        out.proposals += 1
        out.proposals_by_detector[cand.detector] = (
            out.proposals_by_detector.get(cand.detector, 0) + 1
        )
        return True

    queues = _by_detector(candidates)
    # gr657257: memory gripes first. A reader is waiting on each one.
    for det in FIRST_DETECTORS:
        queue = queues.get(det)
        while queue and out.proposals < max_proposals and not out.paused:
            _one(queue.popleft())
    rest = [det for det, queue in queues.items() if queue]
    budget = max_proposals - out.proposals
    share = max(1, budget // len(rest)) if rest else 0
    # Round-robin, one candidate per detector per turn. First pass: each
    # detector stops at its equal share of the budget. Second pass: the
    # share a quiet detector left unspent goes to the rest, same rotation.
    for cap in (share, None):
        while out.proposals < max_proposals and not out.paused:
            turned = False
            for det, queue in queues.items():
                if out.proposals >= max_proposals or out.paused:
                    break
                if not queue:
                    continue
                if cap is not None and out.proposals_by_detector.get(det, 0) >= cap:
                    continue
                turned = True
                _one(queue.popleft())
            if not turned:
                break
    if out.paused or queues.get("grounding_audit"):
        from precis.graph.garden.grounding import BASELINE_DUE_KEY, CURSOR_KEY

        # An omitted cursor carries the saved window forward for retry.
        out.cursors.pop(CURSOR_KEY, None)
        # A baseline candidate the budget could not reach is an owed
        # slot: the next collection promotes it ahead of need candidates
        # so a budget-limited tick cannot starve the starvation guard
        # (gr634490). A persisted marker for a ref outside this window
        # already survived in out.cursors — keep the older debt; a
        # cleared (0) marker means a debt was paid this window and a
        # fresh one may be recorded.
        owed = [
            c.target_id
            for c in queues.get("grounding_audit") or ()
            if c.action == "audit" and c.payload.get("baseline")
        ]
        if owed and not out.cursors.get(BASELINE_DUE_KEY):
            out.cursors[BASELINE_DUE_KEY] = min(owed)
    return out


def _by_detector(
    candidates: list[GardenCandidate],
) -> dict[str, deque[GardenCandidate]]:
    """Candidates queued per detector, detectors in first-seen order and
    each queue in the detector's own (best-first) order."""
    queues: dict[str, deque[GardenCandidate]] = {}
    for cand in candidates:
        queues.setdefault(cand.detector, deque()).append(cand)
    return queues


def _replace(cand: GardenCandidate, **kw: Any) -> GardenCandidate:
    from dataclasses import replace

    return replace(cand, **kw)


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_PROPOSALS",
    "FIRST_DETECTORS",
    "GardenOutcome",
    "ProposerFn",
    "run_garden_tick",
]
