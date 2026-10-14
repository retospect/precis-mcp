"""The garden tick — detectors → proposer → proposals.

:func:`run_garden_tick` is the whole pass the ``graph_garden`` job runs:
collect candidates (bounded), skip keys already pending, ask the
proposer, emit accepted proposals. It never touches the graph — the only
writes are ``reviews`` rows.

``propose=`` is the test seam: a callable ``(store, candidate) ->
Verdict``. Default :func:`propose_llm` (one routed call per candidate).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.graph.garden.candidates import (
    GARDEN_KINDS,
    STALE_DAYS,
    GardenCandidate,
    run_detectors,
)
from precis.graph.garden.proposals import emit_proposal, proposal_key_pending
from precis.graph.garden.proposer import Verdict, propose_llm

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
    rejected: int = 0
    llm_errors: int = 0
    paused: bool = False
    detectors: list[str] = field(default_factory=list)


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
    propose: ProposerFn | None = None,
) -> GardenOutcome:
    """One bounded tick. Returns the outcome; writes only reviews rows.

    ``propose=None`` uses the routed LLM proposer. A paused verdict stops
    the tick — remaining candidates are re-found next tick (dedup makes
    that free).
    """
    if propose is None:
        propose = propose_llm
    candidates = run_detectors(
        store,
        passes=passes,
        kinds=kinds,
        limit=limit,
        scan_cap=scan_cap,
        stale_days=stale_days,
        tag_namespaces=tag_namespaces,
        tag_kinds=tag_kinds,
        exclusive_axes=exclusive_axes,
    )
    out = GardenOutcome(candidates=len(candidates))
    for cand in candidates:
        if out.proposals >= max_proposals:
            break
        if proposal_key_pending(store, cand):
            out.skipped_pending += 1
            continue
        try:
            verdict = propose(store, cand)
        except Exception as exc:
            log.warning("garden: proposer raised on %s: %s", cand.key, exc)
            out.llm_errors += 1
            continue
        if verdict is None:
            continue
        if verdict.paused:
            out.paused = True
            break
        if verdict.error is not None:
            out.llm_errors += 1
            continue
        if not verdict.accept:
            out.rejected += 1
            continue
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
        if emitted is not None:
            out.proposals += 1
        else:
            out.skipped_pending += 1
    return out


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
