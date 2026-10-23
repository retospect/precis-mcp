"""The garden tick — detectors → proposer → proposals.

:func:`run_garden_tick` is the whole pass the ``graph_garden`` job runs:
collect candidates (bounded), skip keys already pending or already
rejected, ask the proposer, emit accepted proposals and record rejected
ones (so the next run does not re-ask; gr506046). It never touches the
graph — the only writes are ``reviews`` rows.

The ``max_proposals`` budget is shared round-robin across the detectors
that produced candidates (gr504843): each gets ``max_proposals // n``
first, and whatever a detector leaves unspent goes to the others. A
high-volume detector (dangling_edge) can no longer take the whole tick
because it sorts first.

``propose=`` is the test seam: a callable ``(store, candidate) ->
Verdict``. Default :func:`propose_llm` (one routed call per candidate).
"""

from __future__ import annotations

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
from precis.graph.garden.proposer import PROMPT_VERSION, Verdict, propose_llm

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Per-tick ceilings — the "bounded" half of "runs bounded per tick".
#: ``limit`` caps candidates per detector; ``max_proposals`` caps the
#: proposals one tick may write.
DEFAULT_LIMIT = 50
DEFAULT_MAX_PROPOSALS = 20

ProposerFn = Callable[["Store", GardenCandidate], "Verdict | None"]


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
    #: (:data:`~precis.graph.garden.candidates.CONSIDERED_DAYS`).
    skipped_considered: int = 0
    rejected: int = 0
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
    #: Proposals written per detector — the round-robin budget's spend.
    proposals_by_detector: dict[str, int] = field(default_factory=dict)
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
) -> GardenOutcome:
    """One bounded tick. Returns the outcome; writes only reviews rows.

    ``propose=None`` uses the routed LLM proposer. A paused verdict stops
    the tick — remaining candidates are re-found next tick (dedup makes
    that free).
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
        cursors=dict(run.cursors),
    )

    def _one(cand: GardenCandidate) -> bool:
        """Propose one candidate; True when a proposal row was written.
        Sets ``out.paused`` on a window-scoped pause."""
        block = key_block(
            store,
            cand.target_kind,
            cand.target_id,
            cand.key,
            prompt_version=PROMPT_VERSION,
        )
        if block == "rejected":
            out.skipped_rejected += 1
            return False
        if block is not None:
            out.skipped_pending += 1
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
                prompt_version=PROMPT_VERSION,
                reason=SIBLING_REJECT_REASON,
            )
            return False
        try:
            verdict = propose(store, cand)
        except Exception as exc:
            log.warning("garden: proposer raised on %s: %s", cand.key, exc)
            out.llm_errors += 1
            return False
        if verdict is None:
            return False
        if verdict.paused:
            out.paused = True
            return False
        if verdict.error is not None:
            out.llm_errors += 1
            out.llm_truncated += int(verdict.truncated)
            return False
        if not verdict.accept:
            out.rejected += 1
            record_rejection(
                store,
                cand,
                rationale=verdict.rationale,
                prompt_version=PROMPT_VERSION,
                model=verdict.model,
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
        emitted = emit_proposal(
            store,
            effective,
            rationale=verdict.rationale,
            model=verdict.model,
            key=cand.key,
        )
        if emitted is None:
            out.skipped_pending += 1
            return False
        out.proposals += 1
        out.proposals_by_detector[cand.detector] = (
            out.proposals_by_detector.get(cand.detector, 0) + 1
        )
        return True

    queues = _by_detector(candidates)
    share = max(1, max_proposals // len(queues)) if queues else 0
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
    "GardenOutcome",
    "ProposerFn",
    "run_garden_tick",
]
