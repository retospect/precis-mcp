"""Quest allocator — which striving advances when a compute slot frees (4d).

The scheduling of the autonomous loop is *emergent*: a quest-thread is I/O-bound
(a short active phase, then a long block while a sim runs), so many quests
interleave on their own. The allocator's only real decision is the narrow one —
**when a slot frees, which idle / ready active quest ticks next?** — scored by a
long-running average so it damps on the smoothed trend, not a single result:

    score = EWMA( priority × momentum × (1 + promise) ) + exploration

* **priority** — the striving weight (slice 2's `base_weight` over `refs.prio`);
* **momentum** — is anything flowing in (slice 3's label → a factor);
* **promise** — the frontier-improvement rate (slice 4c's acquisition term);
* **exploration** — a decaying bonus so a rarely-picked quest is not starved.

A **weekly proportional budget** (``PRECIS_QUEST_WEEKLY_BUDGET``, unset = no cap)
bounds total draw: each active quest's share ∝ its priority weight, metered
against the **tote** (the dated `cost` entries in its logbook over the last 7
days). A quest over its share is skipped this round; one that has gone cold
(no promise, long since the frontier improved, no recent activity) **cools to
`dormant`** on its own.

**Cost/credit under overlap** (open Q1) is resolved by construction here:
candidate `structure`s are content-addressed *per quest* (the slug carries the
quest id), so a sim is owned by exactly one quest — its cost is billed once, no
double-count. Shared candidates across quests (and the credit-sharing that would
imply) are a later refinement.

The whole pass gates on ``PRECIS_QUEST_LOOP_ENABLED`` (default OFF), so it merges
dark; ``precis quest run`` invokes it manually.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from precis.quest.logbook import LOG_KIND, append_entry

if TYPE_CHECKING:
    from precis.store import Store

#: EWMA smoothing on the raw bandit score (higher = more reactive).
EWMA_ALPHA = float(os.environ.get("PRECIS_QUEST_EWMA_ALPHA", "0.3"))
#: Exploration weight — the bonus a never-picked quest gets over a hot one.
EXPLORE = float(os.environ.get("PRECIS_QUEST_EXPLORE", "0.15"))
#: Days a stalled quest may sit with no improvement before it cools to dormant.
COOL_AFTER_TICKS = int(os.environ.get("PRECIS_QUEST_COOL_AFTER_TICKS", "12"))

#: momentum label → a multiplicative factor on the bandit score.
_MOMENTUM_FACTOR = {"active": 1.0, "warming": 0.7, "quiet": 0.5, "stalled": 0.3}


@dataclass(frozen=True)
class Pick:
    quest_id: int
    score: float
    raw: float


def _merge_meta(store: Store, ref_id: int, patch: dict[str, Any]) -> None:
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (Jsonb(patch), ref_id),
        )


def active_quest_ids(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE r.kind = 'quest' AND r.deleted_at IS NULL "
            "AND t.namespace = 'STATUS' AND t.value = 'active' "
            "ORDER BY COALESCE(r.prio, 5) ASC, r.ref_id ASC"
        ).fetchall()
    return [int(r[0]) for r in rows]


def raw_score(store: Store, quest_id: int) -> float:
    """priority × momentum × (1 + promise) — the un-smoothed bandit value."""
    from precis.quest import cascade, gaps, reweight

    ref = store.get_ref(kind="quest", id=quest_id)
    if ref is None:
        return 0.0
    base = reweight.base_weight(ref.prio)
    momentum = _MOMENTUM_FACTOR.get(gaps.quest_momentum(store, quest_id).label, 0.5)
    promise = max(0.0, cascade.quest_promise(store, quest_id))
    return base * momentum * (1.0 + promise)


def pick_score(store: Store, quest_id: int) -> float:
    """The EWMA-smoothed score + a decaying exploration bonus, for ranking."""
    ref = store.get_ref(kind="quest", id=quest_id)
    meta = (ref.meta or {}) if ref else {}
    raw = raw_score(store, quest_id)
    prev = meta.get("ewma_score")
    smoothed = float(prev) if isinstance(prev, (int, float)) else raw
    picks = int(meta.get("picks", 0) or 0)
    return smoothed + EXPLORE / (picks + 1)


# ── weekly budget (metered against the tote) ──────────────────────────


def weekly_spend(store: Store, quest_id: int, *, days: int = 7) -> float:
    """The quest's compute cost over the trailing window — a tote slice."""
    since = datetime.now(UTC) - timedelta(days=days)

    def _aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    total = 0.0
    for b in store.list_blocks_for_ref(quest_id):
        if b.chunk_kind != LOG_KIND or b.created_at is None:
            continue
        if _aware(b.created_at) < since:
            continue
        total += float((b.meta or {}).get("cost", 0) or 0)
    return total


def _budget_total() -> float | None:
    raw = os.environ.get("PRECIS_QUEST_WEEKLY_BUDGET")
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def over_budget(
    store: Store, quest_id: int, active: list[int], *, total_budget: float | None
) -> bool:
    """True when the quest has spent past its proportional weekly share."""
    if total_budget is None or not active:
        return False
    from precis.quest import reweight

    weights = {
        q: reweight.base_weight(store.get_ref(kind="quest", id=q).prio) for q in active
    }
    denom = sum(weights.values()) or 1.0
    share = total_budget * (weights.get(quest_id, 0.0) / denom)
    return weekly_spend(store, quest_id) >= share


# ── the pick + the cool ───────────────────────────────────────────────


def pick_next_quest(store: Store, *, total_budget: float | None = None) -> Pick | None:
    """The highest-scoring active quest that is under its weekly budget."""
    active = active_quest_ids(store)
    if not active:
        return None
    budget = total_budget if total_budget is not None else _budget_total()
    eligible = [
        q for q in active if not over_budget(store, q, active, total_budget=budget)
    ]
    if not eligible:
        return None
    best = max(eligible, key=lambda q: pick_score(store, q))
    return Pick(
        quest_id=best, score=pick_score(store, best), raw=raw_score(store, best)
    )


def cool_stalled(store: Store) -> list[int]:
    """Cool to `dormant` any active quest that has gone cold on its own.

    Cold = no promise, the frontier hasn't improved in ``COOL_AFTER_TICKS``
    ticks, and it has actually been ticking (so a brand-new quest isn't cooled).
    Records a `reflection` entry noting why.
    """
    from precis.quest import cascade
    from precis.store import Tag

    cooled: list[int] = []
    for qid in active_quest_ids(store):
        ref = store.get_ref(kind="quest", id=qid)
        meta = (ref.meta or {}) if ref else {}
        ticks = int(meta.get("tick_count", 0) or 0)
        since_improve = int(meta.get("ticks_since_frontier_improve", 0) or 0)
        promise = cascade.quest_promise(store, qid)
        if (
            ticks >= COOL_AFTER_TICKS
            and since_improve >= COOL_AFTER_TICKS
            and promise <= 0.0
        ):
            store.add_tag(
                qid,
                Tag.closed("STATUS", "dormant"),
                set_by="system",
                replace_prefix=True,
            )
            append_entry(
                store,
                qid,
                text=(
                    f"cooled to dormant — no frontier improvement in {since_improve} "
                    "ticks and promise is flat; set aside until re-awakened"
                ),
                entry_type="reflection",
                by="agent",
            )
            cooled.append(qid)
    return cooled


def run_allocator_pass(
    store: Store,
    *,
    enabled: bool | None = None,
    total_budget: float | None = None,
    compute: bool = True,
) -> dict[str, Any]:
    """One allocator step: cool the cold, pick the best, tick it once.

    Gated on ``PRECIS_QUEST_LOOP_ENABLED`` unless ``enabled`` overrides. Returns
    a summary dict. ``run_quest_tick`` is looked up on the module so tests can
    monkeypatch it (no live model).
    """
    from precis.quest.tick import quest_loop_enabled

    on = quest_loop_enabled() if enabled is None else enabled
    if not on:
        return {"enabled": False, "cooled": 0, "picked": None}

    cooled = cool_stalled(store)
    pick = pick_next_quest(store, total_budget=total_budget)
    if pick is None:
        return {"enabled": True, "cooled": len(cooled), "picked": None}

    from precis.quest import tick as tick_mod

    outcome = tick_mod.run_quest_tick(store, pick.quest_id, compute=compute)
    _record_pick(store, pick.quest_id, pick.raw)
    return {
        "enabled": True,
        "cooled": len(cooled),
        "picked": pick.quest_id,
        "score": round(pick.score, 4),
        "status": outcome.status,
    }


def _record_pick(store: Store, quest_id: int, raw: float) -> None:
    """Bump pick count + fold the raw score into the quest's EWMA."""
    ref = store.get_ref(kind="quest", id=quest_id)
    meta = (ref.meta or {}) if ref else {}
    prev = meta.get("ewma_score")
    prev_v = float(prev) if isinstance(prev, (int, float)) else raw
    ewma = EWMA_ALPHA * raw + (1.0 - EWMA_ALPHA) * prev_v
    _merge_meta(
        store,
        quest_id,
        {"picks": int(meta.get("picks", 0) or 0) + 1, "ewma_score": round(ewma, 6)},
    )


__all__ = [
    "COOL_AFTER_TICKS",
    "EWMA_ALPHA",
    "EXPLORE",
    "Pick",
    "active_quest_ids",
    "cool_stalled",
    "over_budget",
    "pick_next_quest",
    "pick_score",
    "raw_score",
    "run_allocator_pass",
    "weekly_spend",
]
