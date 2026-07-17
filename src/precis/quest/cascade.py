"""Quest cascade — local grind, frontier-model escalation on a signal (4c).

The two-speed cascade of ADR 0047 applied to inquiry. A quest tick runs at the
**local / cheap** tier by default (propose, interpret, keep notes current). The
**frontier** tier is the escalation rung: it fires on a *signal*, not a
schedule, to review the accumulated evidence, rewrite the dossier, and set the
next lines of inquiry.

Signals (any one escalates the next tick):

* **first-review** — candidates exist but the frontier model has never reviewed
  them;
* **new-evidence** — at least ``FRONTIER_REVIEW_EVERY`` new ``result`` logbook
  entries have landed since the last review;
* **stalled** — ``STALL_TICKS`` ticks have passed since the frontier last
  *improved*, with candidates in play (the local grind is spinning; bring in the
  bigger model to change direction).

The cascade also maintains the **promise** proxy — the frontier-improvement rate
(objective gained per unit compute over the recent window) — which rung 4d's
allocator reads to decide which quest earns the next compute slot. All state
lives in the quest's ``meta`` (``tick_count``, ``frontier_reviews``,
``frontier_best``, ``promise``, …), updated once per tick by
:func:`update_cascade_state`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from precis.quest.logbook import LOG_KIND

if TYPE_CHECKING:
    from precis.store import Store

#: New `result` entries since the last review that trigger a frontier review.
FRONTIER_REVIEW_EVERY = int(os.environ.get("PRECIS_QUEST_FRONTIER_REVIEW_EVERY", "5"))
#: Ticks without a frontier improvement before we escalate on a stall.
STALL_TICKS = int(os.environ.get("PRECIS_QUEST_STALL_TICKS", "4"))


@dataclass(frozen=True)
class Escalation:
    escalate: bool
    reason: str  # "first-review" | "new-evidence" | "stalled" | ""


def _merge_meta(store: Store, ref_id: int, patch: dict[str, Any]) -> None:
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (Jsonb(patch), ref_id),
        )


def _n_entries(store: Store, quest_id: int, entry_type: str) -> int:
    return sum(
        1
        for b in store.list_blocks_for_ref(quest_id)
        if b.chunk_kind == LOG_KIND and (b.meta or {}).get("entry_type") == entry_type
    )


def _n_results(store: Store, quest_id: int) -> int:
    return _n_entries(store, quest_id, "result")


def _has_candidates(store: Store, quest_id: int) -> bool:
    from precis.quest.gaps import _live_servers

    return any(s.kind == "structure" for s in _live_servers(store, quest_id))


def _n_open_hypotheses(store: Store, quest_id: int) -> int:
    from precis.quest.gaps import _open_hypotheses

    return len(_open_hypotheses(store, quest_id))


def _n_paper_servers(store: Store, quest_id: int) -> int:
    from precis.quest.gaps import _live_servers

    return sum(1 for s in _live_servers(store, quest_id) if s.kind == "paper")


def escalation_signal(store: Store, quest_id: int) -> Escalation:
    """Decide whether the next tick should run at the frontier tier."""
    ref = store.get_ref(kind="quest", id=quest_id)
    meta = (ref.meta or {}) if ref else {}
    has_candidates = _has_candidates(store, quest_id)
    reviews = int(meta.get("frontier_reviews", 0) or 0)

    if reviews == 0 and has_candidates:
        return Escalation(True, "first-review")

    new_results = _n_results(store, quest_id) - int(
        meta.get("frontier_review_results", 0) or 0
    )
    if new_results >= FRONTIER_REVIEW_EVERY:
        return Escalation(True, "new-evidence")

    stall = int(meta.get("tick_count", 0) or 0) - int(
        meta.get("frontier_review_tick", 0) or 0
    )
    if has_candidates and stall >= STALL_TICKS:
        return Escalation(True, "stalled")

    return Escalation(False, "")


def _frontier_best_energy(store: Store, quest_id: int) -> float | None:
    """The lowest energy on the current Pareto frontier (None if none yet)."""
    from precis.quest.frontier import quest_frontier

    fr = quest_frontier(store, quest_id)
    energies = [c.measures["energy"] for c in fr.frontier if "energy" in c.measures]
    return min(energies) if energies else None


def _recent_cost(store: Store, quest_id: int, window: int = 10) -> float:
    """Sum of the cost on the last ``window`` result entries (a tote slice)."""
    results = [
        b
        for b in store.list_blocks_for_ref(quest_id)
        if b.chunk_kind == LOG_KIND and (b.meta or {}).get("entry_type") == "result"
    ]
    return sum(float((b.meta or {}).get("cost", 0) or 0) for b in results[-window:])


def update_cascade_state(store: Store, quest_id: int, *, reviewed: bool) -> float:
    """Advance the per-quest cascade counters; return the new **promise**.

    Called once at the end of every tick. Bumps ``tick_count``; recomputes the
    frontier best energy and, when it improved, resets the stall clock and
    records a positive **promise** = improvement / recent-compute-cost (the
    frontier-improvement rate, rung-4d's acquisition term). When ``reviewed`` is
    set, stamps the review counters so the next escalation signal resets.
    """
    ref = store.get_ref(kind="quest", id=quest_id)
    meta = (ref.meta or {}) if ref else {}
    tick_count = int(meta.get("tick_count", 0) or 0) + 1

    prev_best = meta.get("frontier_best")
    curr_best = _frontier_best_energy(store, quest_id)
    promise = 0.0
    patch: dict[str, Any] = {"tick_count": tick_count}

    # ── progress signal (broadened) ──────────────────────────────────
    # A compute quest has a Pareto frontier; a reasoning quest never does, so
    # judging progress by frontier energy alone left `cool_stalled` blind to a
    # spin (the tell tonight: 10 restated hypotheses, no frontier, never cooled).
    # Progress is now ANY external-evidence signal since the last tick:
    #   frontier improved · a new milestone deed · a new `result` entry ·
    #   an open hypothesis got resolved (count dropped) · a paper server was
    #   acquired (a lit search paid off). Activity alone (more hypotheses, a
    #   rewritten dossier) is NOT progress — that is exactly the spin.
    n_milestones = _n_entries(store, quest_id, "milestone")
    n_results = _n_results(store, quest_id)
    n_open_hyp = _n_open_hypotheses(store, quest_id)
    n_papers = _n_paper_servers(store, quest_id)
    patch["milestones_seen"] = n_milestones
    patch["results_seen"] = n_results
    patch["open_hyp_seen"] = n_open_hyp
    patch["paper_servers_seen"] = n_papers

    frontier_improved = False
    if curr_best is not None:
        patch["frontier_best"] = curr_best
        if isinstance(prev_best, (int, float)) and curr_best < float(prev_best):
            improvement = float(prev_best) - curr_best
            cost = _recent_cost(store, quest_id)
            promise = improvement / cost if cost > 0 else improvement
            frontier_improved = True

    made_progress = (
        frontier_improved
        or n_milestones > int(meta.get("milestones_seen", 0) or 0)
        or n_results > int(meta.get("results_seen", 0) or 0)
        or n_open_hyp < int(meta.get("open_hyp_seen", n_open_hyp) or n_open_hyp)
        or n_papers > int(meta.get("paper_servers_seen", 0) or 0)
    )
    patch["ticks_since_frontier_improve"] = (
        0
        if made_progress
        else int(meta.get("ticks_since_frontier_improve", 0) or 0) + 1
    )
    patch["promise"] = round(promise, 6)

    if reviewed:
        patch["frontier_reviews"] = int(meta.get("frontier_reviews", 0) or 0) + 1
        patch["frontier_review_results"] = _n_results(store, quest_id)
        patch["frontier_review_tick"] = tick_count

    _merge_meta(store, quest_id, patch)
    return promise


def quest_promise(store: Store, quest_id: int) -> float:
    """The last-recorded promise (frontier-improvement rate) for a quest."""
    ref = store.get_ref(kind="quest", id=quest_id)
    return float((ref.meta or {}).get("promise", 0.0) or 0.0) if ref else 0.0


__all__ = [
    "FRONTIER_REVIEW_EVERY",
    "STALL_TICKS",
    "Escalation",
    "escalation_signal",
    "quest_promise",
    "update_cascade_state",
]
