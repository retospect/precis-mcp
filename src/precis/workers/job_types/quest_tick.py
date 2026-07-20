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

The loop only reaches ``Done`` when a tick proposes **nothing new** (the quest
graduated, or the model is out of ideas): it then rests until a fresh coordinator
job re-awakens it.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from precis.workers.executors._yield import Done, WakeWhen, Yield
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

#: Sim job_types this quest's compute lane mints (barrier + stability). Their
#: non-terminal count is the loop's wait set + backpressure signal.
_SIM_JOB_TYPES = ("catpath_explore", "struct_relax")


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
        # LLM tier for the review/propose call. 'local-big' routes to the
        # node-local OSS model (PRECIS_LOCAL_BIG_MODEL) via the OpenAI-tools seam.
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
    """Non-terminal sim jobs under this quest's candidate structures.

    A sim (``catpath_explore`` / ``struct_relax``) is parented on its *candidate
    structure*, which ``serves`` the quest. This is the in-flight set the loop
    waits on and the per-quest backpressure signal (empty ⇒ safe to propose the
    next batch).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT j.ref_id
              FROM refs j
              JOIN links l ON l.src_ref_id = j.parent_id
             WHERE j.kind = 'job'
               AND j.deleted_at IS NULL
               AND (j.meta->>'job_type') = ANY(%s)
               AND l.dst_ref_id = %s
               AND l.relation = 'serves'
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt
                        JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'queued'
                   ) NOT IN ('succeeded', 'failed', 'cancelled')
            """,
            (list(_SIM_JOB_TYPES), quest_id),
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


def _dispatch(ctx: Any, spec: Any) -> Any:
    """Coordinator phase machine. Returns ``Done`` | ``Yield``."""
    state = (ctx.meta or {}).get("coordinator_state") or {}
    if ctx.is_cancel_requested():
        return Done(
            summary="quest loop cancelled by request",
            success=False,
            summary_meta={"cancelled": True},
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
    return _phase_tick(ctx, {"slice_count": int(state.get("slice_count") or 0)})


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


def _phase_tick(ctx: Any, state: dict[str, Any]) -> Any:
    """Harvest finished sims + review/propose (local LLM) + dispatch a batch."""
    from precis.dispatch import Hub
    from precis.quest.search import make_acquiring_search
    from precis.quest.tick import run_quest_tick

    params = (ctx.meta or {}).get("params") or {}
    quest_id = int(params["quest_id"])  # schema-required
    tier = params.get("tier") or "local-big"
    slice_count = int(state.get("slice_count") or 0) + 1

    # Backpressure: never dispatch a new batch while this quest's sims are still
    # in flight (defensive — _phase_await only routes here when idle).
    pending = _pending_sim_ids(ctx.store, quest_id)
    if pending:
        return _await_yield({"slice_count": slice_count}, pending)

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
            state={"phase": "await", "slice_count": slice_count, "child_job_ids": []},
            wake_when=WakeWhen("at_time", {"ts": int(now + _heartbeat_s())}),
        )

    search_fn = make_acquiring_search(quest_id, Hub(store=ctx.store))
    outcome = run_quest_tick(
        ctx.store, quest_id, compute=True, tier=tier, search_fn=search_fn
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
    pending = _pending_sim_ids(ctx.store, quest_id)
    if pending:
        return _await_yield({"slice_count": slice_count}, pending)

    # Nothing dispatched — graduated / no proposals / paused. Rest until re-armed.
    return Done(
        summary=(
            f"quest {quest_id} loop idle after tick #{slice_count}: no new sims "
            f"dispatched (status={status}). Loop rests until re-awakened by a "
            "fresh quest_tick coordinator job."
        ),
        success=True,
        summary_meta={"slices": slice_count, "last_status": status},
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
