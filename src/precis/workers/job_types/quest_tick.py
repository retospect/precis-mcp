"""``quest_tick`` — a quest's perpetual research loop as a coordinator campaign.

One coordinator job per quest drives the autonomous loop **indefinitely and
event-driven** (no cron): each active slice

1. **harvests** finished sims (barriers → the frontier),
2. runs the **LLM review + propose** step (local model via the ADR-0046 router
   at ``tier``) — which rewrites the dossier, does the **lit-search**, and emits
   the next batch of candidate catalysts,
3. **materialises + dispatches** those candidates' barrier/relax sims, then
4. **yields** until the sims land — and the next slice harvests them and
   proposes again.

Both (1)-(3) ride ``run_quest_tick(compute=True)`` (the same tick the manual CLI
runs); the coordinator only owns the *scheduling*: it waits on the in-flight sims
and resumes when they are done. That makes the cadence **self-paced by sim
completion**, not a timer.

**Liveness + backpressure.** Like ``good_search``, the wait uses an ``at_time``
heartbeat (not a bare ``children_done``) so a sim stuck at ``STATUS:queued``
behind other spark work can't park the loop forever, and — the property the
operator asked for — **no new batch is proposed while the previous one is still
in flight** (per-quest backpressure), and a slice **defers** rather than piling
on when spark's compute queue is already deep (starvation gate).

A slice that dispatches nothing on a successful tick backs off and retries
rather than resting, mirroring the failed/paused budget
(``_max_tick_failures()``) — but on one of two budgets, depending on whether
the model *engaged*: a **genuine dry** tick (wrote to the logbook, rewrote
the dossier, proposed a candidate, or pinned a ledger direction, but had
nothing new to dispatch) is real evidence the space may be exhausted, so it
gets the small ``_max_dry_ticks()`` budget; a **punt** (produced nothing
substantive at all) isn't evidence of anything but a flaky slice, so it gets
the larger, more-forgiving ``_max_punt_ticks()`` budget. Only after that many
*consecutive* ticks of the same flavor does the loop reach ``Done`` and rest
until a fresh coordinator job re-awakens it. RC2: it also self-rests
immediately, on any phase, once the quest itself is no longer active (see
``_dispatch``) — it no longer only winds down passively via the dry/punt
budgets.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from precis.quest.allocator import active_quest_ids
from precis.quest.weave_tick import QUEST_BODY_META_KEY, QUEST_BODY_WEAVE
from precis.workers.executors._yield import Done, WakeWhen, Yield
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

#: Sim job_types this quest's compute lane mints (barrier + stability). Their
#: non-terminal count is the loop's wait set + backpressure signal.
#:
#: The barrier lane fans out into ``autocatpath_seed`` (one job per model×seed)
#: plus an ``autocatpath_aggregate`` rollup — both landed with 47332ad3,
#: retiring the flat ``autocatpath_explore`` job. That new pair was never added
#: here, so once the fan-out shipped this wait set saw only the (fast)
#: ``struct_relax`` lane and went empty the instant the relax finished — blind
#: to a deep queue of still-running barrier seeds. The loop then proposed a new
#: batch every slice regardless of how many seeds were queued: the per-quest
#: backpressure and cross-quest starvation gate were both disconnected, which is
#: how one catalyst quest piled up 238 seeds. ``autocatpath_explore`` is kept so
#: any lingering non-terminal legacy job still registers.
_SIM_JOB_TYPES = (
    "autocatpath_explore",  # legacy flat barrier job (retired by the fan-out)
    "autocatpath_seed",  # barrier lane — per-(model, seed) compute (the fan-out)
    "autocatpath_aggregate",  # barrier lane — the rollup that closes the eval
    "struct_relax",  # stability lane
)


def _env_int(name: str, default: int, *, lo: int = 1, hi: int = 100_000) -> int:
    try:
        n = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(lo, min(hi, n))


def _heartbeat_s() -> int:
    """Seconds between liveness wakes while a batch's sims run (default 300).

    5 min: fine-grained enough that the loop resumes shortly after ~15-20 min
    full-network sims land, coarse enough not to hammer the DB while waiting.
    """
    return _env_int("PRECIS_QUEST_TICK_HEARTBEAT_S", 300, lo=30, hi=7200)


def _max_queued_sims() -> int:
    """Starvation gate: defer a new batch when spark's compute queue already has
    at least this many non-terminal sims *across all quests* (default 6)."""
    return _env_int("PRECIS_QUEST_TICK_MAX_QUEUED", 6, lo=1, hi=1000)


def _max_tick_failures() -> int:
    """Consecutive *failed* ticks tolerated before the loop rests (default 5).

    A transient LLM error (endpoint 400/502) or a breaker/quota *pause* must not
    end the perpetual loop — it backs off and retries. But a persistent failure
    (a real config break) should eventually rest the loop rather than spin every
    heartbeat forever; after this many consecutive failures it goes ``Done`` and
    waits to be re-armed by a fresh ``quest_tick`` job.
    """
    return _env_int("PRECIS_QUEST_TICK_MAX_FAILURES", 5, lo=1, hi=1000)


def _max_dry_ticks() -> int:
    """Consecutive *genuine dry* ticks tolerated before the loop rests (default 3).

    A genuine dry tick is a *successful* tick where the model **engaged**
    (wrote to the logbook, rewrote the dossier, proposed candidates, or
    pinned a ledger direction) but dispatched zero new sims — real evidence
    the space may be exhausted. This is the small, unforgiving budget; a
    *punt* (the model produced nothing at all) uses the larger
    ``_max_punt_ticks`` instead (see there for why the two are split). Only
    after this many *consecutive* genuine-dry ticks does the loop go
    ``Done`` and wait to be re-armed by a fresh ``quest_tick`` job.
    """
    return _env_int("PRECIS_QUEST_TICK_MAX_DRY", 3, lo=1, hi=1000)


def _max_punt_ticks() -> int:
    """Consecutive *punt* ticks tolerated before the loop rests (default 8).

    A punt is a successful tick where the model produced **nothing
    substantive** at all (no logbook entries, no dossier rewrite, no
    proposals, no pinned ledger direction) — a flaky slice, not evidence the
    quest is out of ideas. That's weaker evidence than a *genuine dry* tick
    (the model engaged but had nothing new to dispatch — see
    ``_max_dry_ticks``), so a punt gets a higher, more-forgiving ceiling
    before the loop rests.
    """
    return _env_int("PRECIS_QUEST_TICK_MAX_PUNT", 8, lo=1, hi=1000)


def _force_acquire_enabled() -> bool:
    """Gate for the guaranteed-acquisition fallback (default ON).

    ``PaperHandler.acquire`` is idempotent (identifier-collapse on an
    already-held/already-wanted paper is a no-op), so leaving this on is
    self-limiting long-term — it just keeps nudging a quiet quest with one
    fresh literature query per slice; re-acquiring the same top hit twice
    costs nothing. A later dial-down (once a quest's corpus fills, or
    acquisition volume needs throttling) is just flipping this env var, no
    redeploy of the fallback logic itself.
    """
    raw = os.environ.get("PRECIS_QUEST_FORCE_ACQUIRE", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "quest_id": {"type": "integer"},
        # LLM tier for the review/propose call (a router.Tier value, e.g.
        # 'big'). 'big' routes to a node-local served OSS model via the
        # OpenAI-tools seam when the backend/chain routes there.
        "tier": {"type": ["string", "null"]},
    },
    "required": ["quest_id"],
    "additionalProperties": True,
}
COMPATIBLE_EXECUTORS = frozenset({"coordinator"})
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Perpetual catalyst-quest loop: harvest → review+propose (local LLM) → "
    "dispatch barrier sims → wait for them → repeat (async coordinator)."
)


def _pending_sim_ids(store: Any, quest_id: int) -> list[int]:
    """Non-terminal sim jobs anywhere under this quest's candidate structures.

    A sim hangs off a *candidate structure* that ``serves`` the quest — but at
    **different depths** per lane. A ``struct_relax`` (and the retired flat
    ``autocatpath_explore``) is parented directly on the candidate; the barrier
    fan-out sits deeper — ``autocatpath_seed`` is three levels down
    (seed_job → seed_todo → agg_todo → candidate) and ``autocatpath_aggregate``
    two. So we walk the whole parent-tree under each serving candidate rather
    than joining a single hop: the pre-fan-out query only reached the 1-hop
    shape, which silently zeroed the barrier-lane backpressure once the seed/
    aggregate fan-out landed (the 238-seed runaway — see ``_SIM_JOB_TYPES``).

    This is the in-flight set the loop waits on and the per-quest backpressure
    signal (empty ⇒ safe to propose the next batch).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE roots AS (
                SELECT l.src_ref_id AS ref_id
                  FROM links l
                 WHERE l.dst_ref_id = %s AND l.relation = 'serves'
            ),
            subtree AS (
                SELECT ref_id FROM roots
                UNION
                SELECT r.ref_id
                  FROM refs r
                  JOIN subtree s ON r.parent_id = s.ref_id
                 WHERE r.deleted_at IS NULL
            )
            SELECT j.ref_id
              FROM refs j
              JOIN subtree s ON s.ref_id = j.ref_id
             WHERE j.kind = 'job'
               AND j.deleted_at IS NULL
               AND (j.meta->>'job_type') = ANY(%s)
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt
                        JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'queued'
                   ) NOT IN ('succeeded', 'failed', 'cancelled')
            """,
            (quest_id, list(_SIM_JOB_TYPES)),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _queued_sim_count(store: Any) -> int:
    """Count non-terminal sim jobs across ALL quests — the node-load signal for
    the starvation gate (don't stack a new batch onto an already-deep queue)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(*)
              FROM refs j
             WHERE j.kind = 'job'
               AND j.deleted_at IS NULL
               AND (j.meta->>'job_type') = ANY(%s)
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt
                        JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'queued'
                   ) NOT IN ('succeeded', 'failed', 'cancelled')
            """,
            (list(_SIM_JOB_TYPES),),
        ).fetchone()
    return int(row[0]) if row else 0


def _await_yield(state: dict[str, Any], pending: list[int]) -> Yield:
    """Park on an ``at_time`` heartbeat while ``pending`` sims run.

    ``children_done`` semantics without its stuck-queued footgun: we re-check the
    quest's pending set each wake (see ``_phase_await``). ``child_job_ids`` is
    carried for forensics; the authoritative wait is the live pending query.
    """
    now = time.time()
    return Yield(
        state={
            **state,
            "phase": "await",
            "child_job_ids": pending,
            "await_since": state.get("await_since") or now,
        },
        wake_when=WakeWhen("at_time", {"ts": int(now + _heartbeat_s())}),
    )


def _carry_budgets(state: dict[str, Any]) -> dict[str, Any]:
    """The give-up counters that must survive a *defer* — a Yield where no tick
    actually ran (starvation gate / backpressure).

    A defer is not a tick outcome, so it must reset neither the consecutive-
    *failed*, consecutive-*dry*, nor consecutive-*punt* budget; otherwise
    recurring defers under node-wide queue load would silently zero a
    quest's streak and a genuinely out-of-ideas quest could never reach its
    rest condition. (A *productive* tick deliberately rebuilds fresh state
    without these — that reset is correct; a *failed* tick carries only
    ``tick_failures`` — a failure legitimately breaks a dry/punt streak, and
    vice versa.)
    """
    return {
        "tick_failures": int(state.get("tick_failures") or 0),
        "dry_ticks": int(state.get("dry_ticks") or 0),
        "punt_ticks": int(state.get("punt_ticks") or 0),
    }


def _quest_status(store: Any, quest_id: int) -> str:
    """The quest's own STATUS tag value — "deleted" when the quest is gone,
    "unknown" when present but untagged. Only used to annotate a RC2
    self-rest report; the routing decision itself is ``active_quest_ids``
    (see ``_dispatch``)."""
    try:
        ref = store.get_ref(kind="quest", id=quest_id)
    except Exception:
        return "unknown"
    if ref is None or getattr(ref, "deleted_at", None) is not None:
        return "deleted"
    for t in store.tags_for(quest_id):
        if getattr(t, "namespace", None) == "STATUS":
            return str(t.value)
    return "unknown"


def _quest_body(store: Any, quest_id: int) -> str | None:
    """The quest's ``meta.quest_body`` marker (rung 6e-2), or ``None``.

    ``"weave"`` (:data:`precis.quest.weave_tick.QUEST_BODY_WEAVE`) routes
    ``_phase_tick`` to the paper-writing weave body instead of the default
    catalyst ``run_quest_tick`` — see :func:`precis.quest.weave_tick.
    mark_weave_quest`. Same defensive shape as ``_quest_status`` (a missing/
    exception-raising ``get_ref`` — the common case in unit tests that don't
    stub it — degrades to the catalyst default, not a crash).
    """
    try:
        ref = store.get_ref(kind="quest", id=quest_id)
    except Exception:
        return None
    if ref is None:
        return None
    meta = ref.meta or {}
    val = meta.get(QUEST_BODY_META_KEY)
    return str(val) if val is not None else None


def _dispatch(ctx: Any, spec: Any) -> Any:
    """Coordinator phase machine. Returns ``Done`` | ``Yield``."""
    state = (ctx.meta or {}).get("coordinator_state") or {}
    if ctx.is_cancel_requested():
        return Done(
            summary="quest loop cancelled by request",
            success=False,
            summary_meta={"cancelled": True},
        )
    params = (ctx.meta or {}).get("params") or {}
    quest_id = int(params["quest_id"])  # schema-required
    # RC2: a loop whose quest is no longer active self-rests here — BEFORE
    # routing to await/tick — so an *awaiting* loop also rests on its next
    # heartbeat, not only once its dry-tick budget winds it down passively.
    # ``active_quest_ids`` is the same STATUS:active filter the reconciler
    # (:mod:`precis.quest.loop`) uses to decide "does this quest still get a
    # loop", so the two never disagree.
    if quest_id not in set(active_quest_ids(ctx.store)):
        status = _quest_status(ctx.store, quest_id)
        return Done(
            summary=(
                f"quest {quest_id} no longer active ({status}) — loop self-resting"
            ),
            success=True,
            summary_meta={"self_rested": True, "quest_status": status},
        )
    if (state.get("phase") or "tick") == "await":
        return _phase_await(ctx, state)
    return _phase_tick(ctx, state)


def _phase_await(ctx: Any, state: dict[str, Any]) -> Any:
    """Heartbeat wake: still-pending sims → re-yield; all done → tick again."""
    params = (ctx.meta or {}).get("params") or {}
    quest_id = int(params["quest_id"])  # schema-required
    pending = _pending_sim_ids(ctx.store, quest_id)
    if pending:
        return _await_yield(state, pending)
    ctx.append_chunk(
        "job_event",
        f"batch complete ({len(state.get('child_job_ids') or [])} sim(s)) "
        "→ harvest + propose next",
    )
    return _phase_tick(
        ctx,
        {
            "slice_count": int(state.get("slice_count") or 0),
            # Carry the consecutive-failure count across the await hop so a run of
            # transient failures can eventually rest the loop (a success resets it).
            "tick_failures": int(state.get("tick_failures") or 0),
            # Same for consecutive *dry* / *punt* ticks (a success with zero
            # new proposals, split by whether the model engaged) — a
            # productive tick rebuilds state fresh, so both naturally reset
            # to 0.
            "dry_ticks": int(state.get("dry_ticks") or 0),
            "punt_ticks": int(state.get("punt_ticks") or 0),
        },
    )


def _quest_topic(store: Any, quest_id: int) -> str:
    """Short topic string for the quest — ``meta.reaction_config`` (substrate
    + target + slab element) when present, else the quest's own title."""
    try:
        ref = store.get_ref(kind="quest", id=quest_id)
    except Exception:
        return ""
    if ref is None:
        return ""

    meta = ref.meta or {}
    rc = meta.get("reaction_config")
    rc = rc if isinstance(rc, dict) else None
    if rc:
        substrate = rc.get("substrate") or ""
        target = rc.get("target") or ""
        slab = rc.get("slab")
        element = (slab or {}).get("element", "") if isinstance(slab, dict) else ""
        parts = [p for p in (substrate, target, element) if p]
        if parts:
            return " ".join(parts)

    return (ref.title or "").strip()


def _fallback_queries(store: Any, quest_id: int, slice_count: int) -> list[str]:
    """One rotating lit-search query for the guaranteed-acquisition fallback
    (fired when a tick's propose step emitted no ``searches`` of its own — the
    loop should still ask the literature for something new every slice, not
    only when the model happens to).

    Rather than repeating the same query every quiet slice, it walks a small
    facet list keyed on the quest's own topic and picks one by
    ``slice_count % N`` — so consecutive fallback slices explore mechanism,
    then dopants, then recent reviews, instead of the same hit over and over.
    """
    topic = _quest_topic(store, quest_id)
    if not topic:
        return []

    facets = [
        f"{topic} DFT barrier mechanism",
        f"{topic} dopant single-atom-alloy catalyst",
        f"{topic} review 2023 2024",
    ]
    return [facets[slice_count % len(facets)]]


def _phase_weave_tick(
    ctx: Any,
    quest_id: int,
    params: dict[str, Any],
    state: dict[str, Any],
    slice_count: int,
) -> Any:
    """Weave-quest leg of ``_phase_tick`` (rung 6e-2): one ``weave_tick`` call
    (place + weave the topic dossier's unintegrated papers) instead of the
    catalyst ``run_quest_tick``.

    Unlike the catalyst path there are no async sim children to await — a
    weave tick writes sections/citations synchronously — so a productive tick
    simply heartbeats before the next one (still via the shared ``await``
    phase/``at_time`` wake, so the loop paces itself the same way; ``_phase_
    await``'s ``_pending_sim_ids`` query naturally returns empty for a weave
    quest, since it never mints ``autocatpath_explore``/``struct_relax`` jobs, so
    the very next wake falls straight through to another tick). The give-up
    budgets are reused verbatim (consecutive-*failed* / consecutive-*punt*)
    so the loop still winds down + rests exactly like the catalyst path on
    sustained trouble, rather than spinning forever.
    """
    from precis.quest.weave_tick import weave_tick
    from precis.utils.llm.router import DispatchClient, tier_from_str

    tier = params.get("tier") or "big"
    # tier_from_str degrades a pre-Phase-C legacy string (an already-baked
    # job's meta.params.tier) onto its capability-tier analogue instead of
    # raising — see router.tier_from_str.
    client = DispatchClient(
        tier=tier_from_str(tier), source="quest_weave", tools_needed=True
    )

    try:
        result = weave_tick(ctx.store, client, quest_id)
    except Exception as exc:  # defensive — mirrors run_quest_tick's own
        # internal try/except-to-"failed" shape; a weave_tick bug must back
        # off the loop, not crash the coordinator.
        log.exception("tick #%s: weave_tick raised", slice_count)
        result = {"ok": False, "error": f"weave_tick exception: {exc}"}

    if not result.get("ok"):
        error = result.get("error", "?")
        ctx.append_chunk("job_event", f"tick #{slice_count}: weave error — {error}")
        fails = int(state.get("tick_failures") or 0) + 1
        if fails >= _max_tick_failures():
            return Done(
                summary=(
                    f"quest {quest_id} weave loop resting after {fails} "
                    f"consecutive failed tick(s) (last: {error}). Re-armed by "
                    "a fresh quest_tick coordinator job once the cause is "
                    "fixed."
                ),
                success=False,
                summary_meta={
                    "slices": slice_count,
                    "tick_failures": fails,
                    "last_status": error,
                },
            )
        ctx.append_chunk(
            "job_event",
            f"tick #{slice_count}: weave error — backing off {_heartbeat_s()}s "
            f"then retrying (failure {fails}/{_max_tick_failures()})",
        )
        return Yield(
            state={
                "phase": "await",
                "slice_count": slice_count,
                "tick_failures": fails,
                "child_job_ids": [],
            },
            wake_when=WakeWhen("at_time", {"ts": int(time.time() + _heartbeat_s())}),
        )

    woven = result.get("woven") or []
    new_sections = result.get("new_sections") or []
    ok_sections = sum(1 for w in woven if w.get("ok"))
    engaged = bool(ok_sections or new_sections)
    ctx.append_chunk(
        "job_event",
        f"tick #{slice_count}: weave — batch {result.get('batch_size', 0)}, "
        f"{ok_sections}/{len(woven)} section(s) woven, {len(new_sections)} "
        f"new; {result.get('note', '')}"[:500],
    )

    if not engaged:
        punts = int(state.get("punt_ticks") or 0) + 1
        if punts >= _max_punt_ticks():
            return Done(
                summary=(
                    f"quest {quest_id} weave loop resting after {punts} "
                    "consecutive empty tick(s) (nothing to weave). Re-armed "
                    "by a fresh quest_tick coordinator job."
                ),
                success=True,
                summary_meta={"slices": slice_count, "punt_ticks": punts},
            )
        ctx.append_chunk(
            "job_event",
            f"tick #{slice_count}: weave punt (nothing to weave) — retrying "
            f"(punt {punts}/{_max_punt_ticks()})",
        )
        return Yield(
            state={
                "phase": "await",
                "slice_count": slice_count,
                "punt_ticks": punts,
                "child_job_ids": [],
            },
            wake_when=WakeWhen("at_time", {"ts": int(time.time() + _heartbeat_s())}),
        )

    # A productive weave tick rebuilds state fresh — resets the give-up
    # budgets, mirroring the catalyst path's "a success resets" convention.
    return Yield(
        state={"phase": "await", "slice_count": slice_count, "child_job_ids": []},
        wake_when=WakeWhen("at_time", {"ts": int(time.time() + _heartbeat_s())}),
    )


def _phase_tick(ctx: Any, state: dict[str, Any]) -> Any:
    """Harvest finished sims + review/propose (local LLM) + dispatch a batch."""
    from precis.dispatch import Hub
    from precis.quest.search import make_acquiring_search
    from precis.quest.tick import run_quest_tick

    params = (ctx.meta or {}).get("params") or {}
    quest_id = int(params["quest_id"])  # schema-required
    tier = params.get("tier") or "big"
    slice_count = int(state.get("slice_count") or 0) + 1

    # Backpressure: never dispatch a new batch while this quest's sims are still
    # in flight (defensive — _phase_await only routes here when idle). No tick
    # ran, so carry the give-up budgets forward unchanged.
    pending = _pending_sim_ids(ctx.store, quest_id)
    if pending:
        return _await_yield(
            {"slice_count": slice_count, **_carry_budgets(state)}, pending
        )

    # Starvation gate: don't stack a batch onto an already-deep compute queue.
    queued = _queued_sim_count(ctx.store)
    if queued >= _max_queued_sims():
        ctx.append_chunk(
            "job_event",
            f"tick #{slice_count}: deferring — {queued} sim(s) queued node-wide "
            f"(≥ {_max_queued_sims()}); waiting for the queue to drain",
        )
        now = time.time()
        return Yield(
            state={
                "phase": "await",
                "slice_count": slice_count,
                "child_job_ids": [],
                # A defer is not a tick — preserve both give-up budgets.
                **_carry_budgets(state),
            },
            wake_when=WakeWhen("at_time", {"ts": int(now + _heartbeat_s())}),
        )

    # Rung 6e-2: a quest marked ``meta.quest_body == "weave"`` (a paper-writing/
    # topic-dossier quest — see ``precis.quest.weave_tick.mark_weave_quest``)
    # runs the weave body instead of the catalyst propose-experiment tick
    # below. Checked here (not up-front in ``_dispatch``) so it still benefits
    # from the backpressure/starvation-gate checks above unchanged.
    if _quest_body(ctx.store, quest_id) == QUEST_BODY_WEAVE:
        return _phase_weave_tick(ctx, quest_id, params, state, slice_count)

    search_fn = make_acquiring_search(quest_id, Hub(store=ctx.store))
    outcome = run_quest_tick(
        ctx.store,
        quest_id,
        compute=True,
        tier=tier,
        search_fn=search_fn,
        job_ref_id=ctx.ref_id,
    )
    status = getattr(outcome, "status", "?")
    note = getattr(outcome, "note", "") or ""
    ctx.append_chunk(
        "job_event",
        f"tick #{slice_count}: {status} — "
        f"{getattr(outcome, 'candidates_created', 0)} candidate(s), "
        f"{getattr(outcome, 'sims_dispatched', 0)} sim(s), "
        f"{getattr(outcome, 'results_harvested', 0)} harvested, "
        f"{getattr(outcome, 'graduated', 0)} graduated, "
        f"{getattr(outcome, 'searches_run', 0)} search(es) "
        f"(+{getattr(outcome, 'papers_linked', 0)} papers); {note}"[:500],
    )

    # A failed / paused tick is NOT a dry loop — back off and retry rather than
    # resting. A transient LLM error (endpoint 400/502) or a breaker/quota pause
    # would otherwise fall through to the "no sims dispatched → Done" branch and
    # silently end the perpetual loop (the 2026-07-20 failure mode: two loop
    # instances died on a local-model 400). A *failure* counts toward a bounded
    # give-up budget so a persistent config break eventually rests; a *pause* is
    # a wait-for-window, so it retries without consuming the budget. Return early
    # so a failed/paused tick doesn't also fire the acquisition fallback below.
    if status in ("failed", "paused"):
        fails = int(state.get("tick_failures") or 0)
        if status == "failed":
            fails += 1
        if fails >= _max_tick_failures():
            return Done(
                summary=(
                    f"quest {quest_id} loop resting after {fails} consecutive "
                    f"failed tick(s) (last: {status} — {note}). Re-armed by a "
                    "fresh quest_tick coordinator job once the cause is fixed."
                ),
                success=False,
                summary_meta={
                    "slices": slice_count,
                    "tick_failures": fails,
                    "last_status": status,
                },
            )
        ctx.append_chunk(
            "job_event",
            f"tick #{slice_count}: {status} — backing off "
            f"{_heartbeat_s()}s then retrying "
            f"(failure {fails}/{_max_tick_failures()})",
        )
        now = time.time()
        return Yield(
            state={
                "phase": "await",
                "slice_count": slice_count,
                "tick_failures": fails,
                "child_job_ids": [],
            },
            wake_when=WakeWhen("at_time", {"ts": int(now + _heartbeat_s())}),
        )

    # Guaranteed-acquisition fallback: the model's propose step doesn't always
    # emit `searches` (it might not think of one this tick), but the operator
    # wants the loop to keep asking the literature for something new every
    # slice regardless (dial-able via PRECIS_QUEST_FORCE_ACQUIRE). If this
    # tick ran zero searches of its own, fire a rotating fallback query built
    # from the quest's own goal — never fails the slice.
    if _force_acquire_enabled() and not getattr(outcome, "searches_run", 0):
        try:
            from precis.quest.search import run_search_step

            fallback_queries = _fallback_queries(ctx.store, quest_id, slice_count)
            if fallback_queries:
                run_search_step(
                    ctx.store,
                    quest_id,
                    fallback_queries,
                    by="agent",
                    search_fn=make_acquiring_search(quest_id, Hub(store=ctx.store)),
                )
                ctx.append_chunk(
                    "job_event",
                    f"fallback lit-search: {len(fallback_queries)} query(ies)",
                )
        except Exception:
            log.exception("tick #%s: fallback lit-search failed", slice_count)

    # The sims this tick just dispatched (now in flight) are the next wait set.
    # A success resets the consecutive-failure budget (state is rebuilt fresh).
    pending = _pending_sim_ids(ctx.store, quest_id)
    if pending:
        return _await_yield({"slice_count": slice_count}, pending)

    # Nothing dispatched on a *successful* tick — graduated / no proposals /
    # a local-model punt. Split on whether the model actually *engaged* this
    # slice (wrote to the logbook, rewrote the dossier, proposed a
    # candidate, or pinned a ledger direction): engaged-but-nothing-new is
    # real evidence the space may be exhausted (genuine dry, small budget);
    # producing nothing at all is not (a punt, larger budget) — see
    # ``_max_dry_ticks`` / ``_max_punt_ticks``. Same shape as the
    # failed/paused budget above: a single empty tick backs off and retries
    # (the fallback lit-search above already injected fresh evidence for the
    # next propose); only N *consecutive* ticks of the same flavor rest the
    # loop. This subsumes "genuinely out of ideas" — no separate graduation
    # special-case needed.
    engaged = bool(
        getattr(outcome, "logbook_added", 0)
        or getattr(outcome, "dossier_rewritten", False)
        or getattr(outcome, "proposals", 0)
        or getattr(outcome, "ledger_added", 0)
    )
    if not engaged:
        punts = int(state.get("punt_ticks") or 0) + 1
        if punts >= _max_punt_ticks():
            return Done(
                summary=(
                    f"quest {quest_id} loop resting after {punts} consecutive "
                    f"empty punt(s) (last: {status}, no logbook/dossier/"
                    "proposals/ledger output). Re-armed by a fresh quest_tick "
                    "coordinator job."
                ),
                success=True,
                summary_meta={
                    "slices": slice_count,
                    "punt_ticks": punts,
                    "last_status": status,
                },
            )
        ctx.append_chunk(
            "job_event",
            f"tick #{slice_count}: punt (empty response) — retrying "
            f"(punt {punts}/{_max_punt_ticks()})",
        )
        return Yield(
            state={
                "phase": "await",
                "slice_count": slice_count,
                "punt_ticks": punts,
                "child_job_ids": [],
            },
            wake_when=WakeWhen("at_time", {"ts": int(time.time() + _heartbeat_s())}),
        )

    dry = int(state.get("dry_ticks") or 0) + 1
    if dry >= _max_dry_ticks():
        return Done(
            summary=(
                f"quest {quest_id} loop resting after {dry} consecutive dry "
                f"tick(s) (last: {status}, engaged, no new sims dispatched). "
                "Re-armed by a fresh quest_tick coordinator job."
            ),
            success=True,
            summary_meta={
                "slices": slice_count,
                "dry_ticks": dry,
                "last_status": status,
            },
        )
    ctx.append_chunk(
        "job_event",
        f"tick #{slice_count}: dry (engaged, no new sims) — backing off "
        f"{_heartbeat_s()}s then retrying (dry {dry}/{_max_dry_ticks()})",
    )
    return Yield(
        state={
            "phase": "await",
            "slice_count": slice_count,
            "dry_ticks": dry,
            "child_job_ids": [],
        },
        wake_when=WakeWhen("at_time", {"ts": int(time.time() + _heartbeat_s())}),
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("quest_tick runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="quest_tick",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
