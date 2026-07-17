"""Quest allocator — which striving advances when a compute slot frees (4d).

The scheduling of the autonomous loop is *emergent*: a quest-thread is I/O-bound
(a short active phase, then a long block while a sim runs), so many quests
interleave on their own. The allocator's only real decision is the narrow one —
**when a slot frees, which idle / ready active quest ticks next?** — scored by a
long-running average so it damps on the smoothed trend, not a single result:

    score = EWMA( priority × progress_factor × (1 + promise) ) + aging

* **priority** — the striving weight (slice 2's `base_weight` over `refs.prio`);
* **progress_factor** — decays geometrically each tick a quest makes no
  *external progress* (a new result / resolved hypothesis / paper / frontier
  gain), floored so it never hard-zeroes. This deliberately replaced the old
  *momentum* term, which rated a furiously-logging spin "active" and let it
  monopolise the loop — activity is not progress;
* **promise** — the frontier-improvement rate (slice 4c's acquisition term);
* **aging** — a weighted-round-robin fairness floor: a quest's score rises by
  `EXPLORE` per round it has waited since its last pick, so nobody starves and
  higher-priority strivings simply come up more often (replaces the old
  picks-decay explore, which a much-picked quest could never recover).

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

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from precis.quest.logbook import LOG_KIND, append_entry

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: EWMA smoothing on the raw bandit score (higher = more reactive).
EWMA_ALPHA = float(os.environ.get("PRECIS_QUEST_EWMA_ALPHA", "0.3"))
#: Aging weight — a quest's pick_score gains this per allocator round it has
#: waited since it was last picked (anti-starvation; replaces the old
#: picks-decay explore that a much-picked quest could never recover).
EXPLORE = float(os.environ.get("PRECIS_QUEST_EXPLORE", "0.15"))
#: Days a stalled quest may sit with no improvement before it cools to dormant.
COOL_AFTER_TICKS = int(os.environ.get("PRECIS_QUEST_COOL_AFTER_TICKS", "12"))

#: Trailing window (days) the proportional budget meters spend over — the
#: fair-share horizon. Default 7 (weekly); the quests tab tunes it down to
#: 24/48h so a burst is felt sooner (design §9). Env
#: ``PRECIS_QUEST_BUDGET_WINDOW_DAYS``; a per-call ``window_days`` overrides.
BUDGET_WINDOW_DAYS = int(os.environ.get("PRECIS_QUEST_BUDGET_WINDOW_DAYS", "7"))


def _resolve_window(window_days: int | None) -> int:
    """The effective budget window — explicit arg wins, else the env default."""
    return BUDGET_WINDOW_DAYS if window_days is None else window_days


#: Per-tick geometric decay of the progress factor: a quest making no external
#: progress loses ground each tick, so a spinner falls behind a fresh or
#: productive quest well before `cool_stalled` fires — without ever rewarding
#: mere activity (the old momentum term rated a furiously-logging spin "active").
PROGRESS_DECAY = float(os.environ.get("PRECIS_QUEST_PROGRESS_DECAY", "0.8"))
#: Floor under the progress factor so a stalled quest still gets rare, aged picks
#: (never a hard zero — cooling to dormant is the deliberate exit, not silent
#: starvation).
STALL_FLOOR = float(os.environ.get("PRECIS_QUEST_STALL_FLOOR", "0.1"))

#: app_state key holding the monotonic allocator round counter (drives aging).
_ROUND_KEY = "quest_allocator:round"


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


def progress_factor(store: Store, quest_id: int) -> float:
    """Geometric decay on ticks-since-progress, floored — the anti-spin term.

    `ticks_since_frontier_improve` is the cascade's broadened progress clock: it
    resets to 0 on any external progress (result / resolved hypothesis / paper /
    frontier gain) and climbs otherwise. So a quest that only re-reasons decays
    toward :data:`STALL_FLOOR` and loses picks to fresher / productive quests,
    while never hard-zeroing (cooling to dormant is the deliberate exit).
    """
    ref = store.get_ref(kind="quest", id=quest_id)
    ticks = (
        int((ref.meta or {}).get("ticks_since_frontier_improve", 0) or 0) if ref else 0
    )
    return max(STALL_FLOOR, PROGRESS_DECAY**ticks)


def raw_score(store: Store, quest_id: int) -> float:
    """priority × progress_factor × (1 + promise) — the un-smoothed bandit value."""
    from precis.quest import cascade, reweight

    ref = store.get_ref(kind="quest", id=quest_id)
    if ref is None:
        return 0.0
    base = reweight.base_weight(ref.prio)
    promise = max(0.0, cascade.quest_promise(store, quest_id))
    return base * progress_factor(store, quest_id) * (1.0 + promise)


def _pick_count(store: Store, quest_id: int) -> int:
    """How many times the allocator has picked this quest (0 = never ticked)."""
    ref = store.get_ref(kind="quest", id=quest_id)
    return int((ref.meta or {}).get("picks", 0) or 0) if ref else 0


def _current_round(store: Store) -> int:
    """The monotonic allocator round counter (0 before the first pick)."""
    raw = store.get_setting(_ROUND_KEY)
    try:
        return int(raw) if raw is not None else 0
    except ValueError:
        return 0


def _slots_since_last_pick(store: Store, quest_id: int, *, now_round: int) -> int:
    """Rounds this quest has waited since its last pick (the aging term)."""
    ref = store.get_ref(kind="quest", id=quest_id)
    last = int((ref.meta or {}).get("last_pick_round", 0) or 0) if ref else 0
    return max(0, now_round - last)


def pick_score(store: Store, quest_id: int, *, now_round: int | None = None) -> float:
    """The EWMA-smoothed raw score + the anti-starvation aging bonus, for ranking."""
    ref = store.get_ref(kind="quest", id=quest_id)
    meta = (ref.meta or {}) if ref else {}
    raw = raw_score(store, quest_id)
    prev = meta.get("ewma_score")
    smoothed = float(prev) if isinstance(prev, (int, float)) else raw
    rnd = _current_round(store) if now_round is None else now_round
    aging = EXPLORE * _slots_since_last_pick(store, quest_id, now_round=rnd)
    return smoothed + aging


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
    store: Store,
    quest_id: int,
    active: list[int],
    *,
    total_budget: float | None,
    window_days: int | None = None,
) -> bool:
    """True when the quest has spent past its proportional share of the window.

    The window defaults to :data:`BUDGET_WINDOW_DAYS` (env-configurable, 7d)
    and is narrowed by the quests tab to 24/48h so a spend burst is felt
    sooner (design §9). ``share`` is the priority-weighted slice of
    ``total_budget``; the quest is over when its windowed spend meets it.
    """
    if total_budget is None or not active:
        return False
    from precis.quest import reweight

    weights = {
        q: reweight.base_weight(store.get_ref(kind="quest", id=q).prio) for q in active
    }
    denom = sum(weights.values()) or 1.0
    share = total_budget * (weights.get(quest_id, 0.0) / denom)
    return weekly_spend(store, quest_id, days=_resolve_window(window_days)) >= share


# ── the pick + the cool ───────────────────────────────────────────────


def pick_next_quest(
    store: Store,
    *,
    total_budget: float | None = None,
    window_days: int | None = None,
) -> Pick | None:
    """The highest-scoring active quest that is under its windowed budget."""
    active = active_quest_ids(store)
    if not active:
        return None
    budget = total_budget if total_budget is not None else _budget_total()
    eligible = [
        q
        for q in active
        if not over_budget(
            store, q, active, total_budget=budget, window_days=window_days
        )
    ]
    if not eligible:
        return None
    # Cold-start: bootstrap every never-ticked striving before the smoothed
    # bandit narrows, so a quest cannot be starved by another's accumulated
    # EWMA (the "exploration, not starvation" contract in the module header).
    # A converged-on spinner locks its EWMA far above the ~0.15 exploration
    # bonus, which alone could never surface an untried quest — so untried
    # go first, hottest raw score among them leading.
    untried = [q for q in eligible if _pick_count(store, q) == 0]
    if untried:
        best = max(untried, key=lambda q: raw_score(store, q))
    else:
        now_round = _current_round(store)
        best = max(eligible, key=lambda q: pick_score(store, q, now_round=now_round))
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
    window_days: int | None = None,
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
    pick = pick_next_quest(store, total_budget=total_budget, window_days=window_days)
    if pick is None:
        return {"enabled": True, "cooled": len(cooled), "picked": None}

    from precis.quest import tick as tick_mod

    outcome = tick_mod.run_quest_tick(store, pick.quest_id, compute=compute)
    if outcome.status == "paused":
        # Window-scoped breaker pause, not a real tick. Don't burn the pick
        # (no EWMA/pick bump → no premature cooling) and report a skip so the
        # dispatch pass leaves the FAILED-PASSES panel clean. Surface the reason
        # the tick would otherwise discard, so a capped window is observable.
        log.info(
            "quest allocator: quest %d paused by breaker; skipping (%s)",
            pick.quest_id,
            outcome.note,
        )
        return {
            "enabled": True,
            "cooled": len(cooled),
            "picked": None,
            "status": "paused",
        }
    _record_pick(store, pick.quest_id, pick.raw)
    return {
        "enabled": True,
        "cooled": len(cooled),
        "picked": pick.quest_id,
        "score": round(pick.score, 4),
        "status": outcome.status,
    }


def _record_pick(store: Store, quest_id: int, raw: float) -> None:
    """Advance the round, stamp this quest's pick, fold raw into its EWMA.

    Bumping the global round on every pick is what makes the aging bonus tick:
    an un-picked quest's ``now_round - last_pick_round`` grows each round while
    the just-picked quest resets to 0.
    """
    ref = store.get_ref(kind="quest", id=quest_id)
    meta = (ref.meta or {}) if ref else {}
    prev = meta.get("ewma_score")
    prev_v = float(prev) if isinstance(prev, (int, float)) else raw
    ewma = EWMA_ALPHA * raw + (1.0 - EWMA_ALPHA) * prev_v
    now_round = _current_round(store) + 1
    store.set_setting(_ROUND_KEY, str(now_round))
    _merge_meta(
        store,
        quest_id,
        {
            "picks": int(meta.get("picks", 0) or 0) + 1,
            "ewma_score": round(ewma, 6),
            "last_pick_round": now_round,
        },
    )


__all__ = [
    "BUDGET_WINDOW_DAYS",
    "COOL_AFTER_TICKS",
    "EWMA_ALPHA",
    "EXPLORE",
    "PROGRESS_DECAY",
    "STALL_FLOOR",
    "Pick",
    "active_quest_ids",
    "cool_stalled",
    "over_budget",
    "pick_next_quest",
    "pick_score",
    "progress_factor",
    "raw_score",
    "run_allocator_pass",
    "weekly_spend",
]
