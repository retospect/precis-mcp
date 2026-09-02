"""Dispatch worker — Slice 5 of ``docs/backlog/todo-tree-plan.md``.

The bridge between the todo tree (intent) and the job substrate
(execution). Walks open todos with ``meta.executor`` set, picks
the next one that has no successful child job yet, and mints a
``kind='job'`` ref under it. The existing ``job_claude_inproc``
worker (or any future executor) claims the job and runs it.

When the job succeeds, the parent todo's ``meta.auto_check`` of
type ``child_job_succeeded`` resolves the leaf to ``STATUS:done``
on the next ``auto_check`` pass. When the job fails, the
failure-bubble path (see Slice-5 task #6) tags the parent
``child-failed:<job_id>`` so the operator / asa-bot sees a stuck
parent in the nursery digest.

Idempotency
-----------

A todo with ``meta.executor`` already has a child job: skip. The
dispatcher does **not** auto-re-dispatch after a failure (per the
"bubble back up" rule — the parent decides). Once any child job is
queued / running / succeeded / failed, no new job mints until the
parent's owner intervenes (remove the ``child-failed:N`` tag,
delete the failed child job, or change the executor). The lone
exception is a ``plan_tick`` coroutine parent, which re-ticks on its
own succeeded jobs — see ``_job_blocks_dispatch_sql`` for the gate.

Multi-host concurrency
----------------------

Same row-lock pattern as the schedule worker: per-todo
``SELECT … FOR UPDATE OF r SKIP LOCKED`` inside ``store.tx()``
spans the claim → child-job-insert. Two dispatch workers racing on
the same todo serialise on the refs row's tx lock; the loser walks
past via SKIP LOCKED.

Auto-injection of ``auto_check``
--------------------------------

If a writer set ``meta.executor`` but forgot ``meta.auto_check``,
the dispatcher silently injects
``{"type": "child_job_succeeded"}``. Without it the todo would
never resolve on job success — the spawned job would finish, the
parent would sit open forever. The default-on behaviour matches
the user's stated discipline ("true unless false") and is harmless
when explicit auto_check was already set (we only inject when the
key is missing).
"""

from __future__ import annotations

import logging
from typing import Any

from precis.handlers._todo_views import (
    _doable_exclusion_clause,
    _hard_block_clause,
    _replan_bypass_clause,
)
from precis.store import Store
from precis.store.types import Tag
from precis.utils.ref_tree import deleted_in_ancestry
from precis.workers import planner_guardrails
from precis.workers.executors import (
    EXECUTOR_PROVIDES,
    ZERO_LLM_EXECUTORS,
    is_known_executor,
    suspended_job_types,
)
from precis.workers.job_types import get_job_type
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


# Statuses on the parent that are eligible for dispatch. A paused /
# done / blocked parent doesn't dispatch.
_OPEN_PARENT_STATUSES: frozenset[str] = frozenset({"open", "doing"})


# Job types whose parent manages its OWN terminal state and must NOT
# get a ``child_job_succeeded`` auto_check injected. ``plan_tick`` is
# the LLM planner coroutine: a tick exits ``STATUS:succeeded`` whenever
# the claude -p subprocess runs cleanly — including when the planner
# *yielded* (``ask-user:``) or *minted children* (``continue``).
# Injecting ``child_job_succeeded`` would then auto-close the parent on
# the first clean tick, before any work landed. The planner instead
# closes itself with its own ``STATUS:done`` tag (guarded), or parks on
# ``ask-user:`` / ``halt`` — so a coroutine parent needs no auto_check.
_SELF_RESOLVING_JOB_TYPES: frozenset[str] = frozenset({"plan_tick"})


# Cooldown before a parent whose only live-child blocker is a *parked*
# (ask-user: / waiting-for:) child todo becomes a dispatch candidate
# again. Without this, loosening the child-liveness gate for those two
# tags would re-tick the planner (a real LLM call) on every dispatch
# sweep for as long as the child sits parked — review.py's
# ``_recent_failure`` docstring records what an undamped version of
# this exact failure mode looks like in practice (124k ERROR log
# lines/24h on one host, from a re-run-every-sweep loop). ``halt`` /
# ``halt:`` / ``child-failed:`` are NOT in the bypass registry
# (``_replan_bypass_clause``), so a child carrying one of those keeps
# blocking unconditionally — no cooldown ever lifts it.
_PARKED_CHILD_REPLAN_COOLDOWN = "6 hours"


def _parked_child_still_blocks_sql(parent_alias: str, child_alias: str) -> str:
    """SQL fragment: true when a not-done ``child_alias`` should still
    block ``parent_alias``'s re-candidacy.

    A not-done child always blocks UNLESS it is *bypassable-parked*
    AND the cooldown since the parent's last ``plan_tick`` job has
    elapsed. "Bypassable-parked" means carrying a replan-bypass tag
    (``ask-user`` / ``waiting-for:``) AND **not** also carrying a hard
    block tag (``halt`` / ``halt:`` / ``child-failed:``) — a child can
    carry both at once (e.g. the planner escalates an already-parked
    child by adding ``halt:`` without first removing ``ask-user:``),
    and the hard block must always win:

        bypassable = bypass_tag_present AND NOT hard_block_tag_present
        still_blocks = NOT bypassable OR recent_plan_tick_job_exists
                      = (NOT bypass_tag_present OR hard_block_tag_present)
                        OR recent_plan_tick_job_exists

    Embed inside a ``NOT EXISTS`` child-liveness check, ANDed after the
    existing STATUS filter — see both call sites below. Used
    identically (same alias convention) in ``_candidate_parent_ids``
    and ``_claim_and_dispatch`` so the two stay symmetric.
    """
    return f"""(
           NOT EXISTS (
               SELECT 1 FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                WHERE rt2.ref_id = {child_alias}.ref_id
                  AND t2.namespace = 'OPEN'
                  AND {_replan_bypass_clause("t2")}
           )
           OR EXISTS (
               SELECT 1 FROM ref_tags rt3 JOIN tags t3 ON t3.tag_id = rt3.tag_id
                WHERE rt3.ref_id = {child_alias}.ref_id
                  AND t3.namespace = 'OPEN'
                  AND {_hard_block_clause("t3")}
           )
           OR EXISTS (
               SELECT 1 FROM refs j
                WHERE j.parent_id = {parent_alias}.ref_id
                  AND j.kind = 'job'
                  AND j.retired_at IS NULL
                  AND j.meta->>'job_type' = 'plan_tick'
                  AND j.created_at > now() - interval '{_PARKED_CHILD_REPLAN_COOLDOWN}'
           )
       )"""


def _job_blocks_dispatch_sql(parent_alias: str, child_alias: str) -> str:
    """SQL bool: child job ``child_alias`` should block its ``parent_alias``'s
    (re-)dispatch.

    Two cases block:

    * **In-flight** (``queued`` / ``running`` — anything not terminal): real
      work is underway; never mint a second concurrent job.
    * **Terminally succeeded, non-coroutine.** For a *deterministic* parent
      (``meta.executor`` + a ``child_job_succeeded`` auto_check) a ``succeeded``
      child IS the finished work — the parent is done-pending-resolution and the
      ``auto_check`` pass flips it ``STATUS:done`` on its next sweep. Re-minting
      in the gap between the job succeeding and that flip is the runaway
      (gr192606: the daily ``briefing`` todo minted 46 jobs in 23h — each one
      destructively replacing the ``briefing-<date>`` news ref — because the
      ``auto_check`` pass was starved by the same wedged ``system`` worker).

    A succeeded child is therefore EXEMPT from blocking only when the parent
    legitimately re-ticks on success — i.e. it is a planner coroutine. We test
    the same signal ``child_job_succeeded``'s guard 1 uses (``meta ? 'llm_tier'``
    on the parent) so the dispatch re-candidacy gate and the auto_check
    resolution gate stay in agreement about who self-resolves; we also exempt a
    child whose ``job_type`` is itself self-resolving (``plan_tick``) as
    belt-and-suspenders for any future coroutine dispatched under a non-tier
    parent. ``done`` / ``won't-do`` are settled and never block; ``failed``
    doesn't block here either — a failed child bubbles ``child-failed:N`` onto
    the parent, which the exclusion registry (``_doable_exclusion_clause``)
    handles. Used identically in ``_candidate_parent_ids`` and
    ``_claim_and_dispatch`` so the enumerate/lock gates stay symmetric.
    """
    # Invariant: _SELF_RESOLVING_JOB_TYPES is non-empty (``{'plan_tick'}``), so the
    # rendered ``NOT IN (...)`` is always valid SQL. If it's ever made dynamic and
    # could empty, guard this to avoid an ``NOT IN ()`` syntax error.
    self_resolving = ", ".join(f"'{t}'" for t in sorted(_SELF_RESOLVING_JOB_TYPES))
    status = (
        "COALESCE("
        "(SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        f"WHERE rt.ref_id = {child_alias}.ref_id AND t.namespace = 'STATUS' LIMIT 1),"
        " 'open')"
    )
    return f"""(
           {status} NOT IN ('done', 'failed', 'succeeded', 'won''t-do')
           OR (
               {status} = 'succeeded'
               AND NOT ({parent_alias}.meta ? 'llm_tier')
               AND COALESCE({child_alias}.meta->>'job_type', '') NOT IN ({self_resolving})
           )
       )"""


def _served_model_requirement(
    conn: Any, job_type: str, params: dict[str, Any]
) -> dict[str, int] | None:
    """A ``{"llm:<model>": 1}`` requirement that gates a planner tick onto a host
    serving its rung-0 OSS model, or ``None`` when the tick isn't a served-OSS
    planner tick.

    Narrow by design (the "run agents where the model is served" affinity): only
    ``plan_tick`` jobs, whose ``params['model']`` is the tier picker, resolve a
    concrete rung-0 model (:func:`precis.utils.llm.router.planner_rung0_model`).
    The requirement is stamped *only* when that model is actually advertised as
    an ``llm:`` slot on some host; otherwise the job stays host-agnostic
    (byte-identical to before), so a cloud tick or a model served nowhere never
    gets an unsatisfiable gate. The claim-side ``llm:`` affinity gate
    (``executors/_common._finish_claim``) then *prefers* a serving host for up
    to ``LLM_AFFINITY_GRACE_MIN`` minutes, auto-following wherever the slot
    lives — then falls back to claiming it host-agnostic anyway, since the
    model is also reachable from any host over the LLM router HTTP path. That
    fallback is what keeps a model served only on a host that runs no
    executor for this job type (e.g. a system-profile-only host) from
    stranding the job forever.
    """
    if job_type != "plan_tick":
        return None
    model_alias = params.get("model")
    if not isinstance(model_alias, str):
        return None
    from precis.utils.llm.router import planner_rung0_model

    served_model = planner_rung0_model(model_alias, job_type)
    if not served_model:
        return None
    resource = f"llm:{served_model}"
    row = conn.execute(
        "SELECT 1 FROM resource_slots WHERE resource = %s LIMIT 1", (resource,)
    ).fetchone()
    if row is None:
        return None
    return {resource: 1}


def _halt_bad_dispatch(
    store: Store, conn: Any, ref_id: int, detail: str
) -> tuple[int, bool]:
    """Self-halt a mis-configured parent so it stops re-warning forever.

    A parent whose ``executor`` / ``job_type`` is invalid can never mint
    a child, so it stays a dispatch candidate and re-warns on *every*
    sweep. Left unhalted, a handful of such todos flood ``worker_logs``
    indefinitely — a real incident was six todos carrying a bogus
    ``meta.executor='plan_tick'`` (``plan_tick`` is a job_type, never an
    executor) warning ~40k times/day/host.

    Tagging ``halt:bad-dispatch`` (an exclusion-registry tag, see
    ``handlers/_todo_views._DOABLE_EXCLUSION_TAGS``) drops the parent
    from candidate enumeration: warn once, surface in the halt /
    attention view, and resume by removing the tag once the meta is
    fixed. The tag is written on the dispatch tx's own ``conn`` while it
    holds ``FOR UPDATE OF r`` on the parent — atomic with the claim.
    """
    store.add_tag(ref_id, Tag.open("halt:bad-dispatch"), set_by="system", conn=conn)
    log.warning("dispatch: parent #%d %s; halted (halt:bad-dispatch)", ref_id, detail)
    return (1, False)


def run_dispatch_pass(store: Store, *, limit: int = 50) -> BatchResult:
    """Drain up to ``limit`` dispatchable todos. Returns BatchResult.

    Counters:

    * ``claimed`` = number of parent todos we successfully locked
    * ``ok`` = number of child jobs minted
    * ``failed`` = number of parents we couldn't dispatch for
      (bad executor / job_type, validation failure)
    """
    candidate_ids = _candidate_parent_ids(store, limit=limit)
    if not candidate_ids:
        return BatchResult(handler="dispatch", claimed=0, ok=0, failed=0)
    n_claimed = 0
    n_ok = 0
    n_failed = 0
    # One memo for the whole round: the daily total is the same for every
    # candidate, and siblings share a subtree (see RoundContext).
    guard_ctx = planner_guardrails.RoundContext()
    # Resolved lazily, and only if the daily ceiling actually trips — the common
    # round never pays for these queries.
    exempt_ids: set[int] | None = None
    for parent_id in candidate_ids:
        # Planner-coroutine guardrails: tick cap, per-todo cost cap,
        # global daily ceiling. The first two halt the parent
        # in-place (tag halt:tick-cap / halt:cost-cap so attention
        # view surfaces it); the third skips the whole dispatch
        # round. See workers/planner_guardrails.py.
        try:
            verdict = planner_guardrails.check_parent(
                store, parent_ref_id=parent_id, ctx=guard_ctx
            )
        except Exception:
            # Fail CLOSED. A cost guardrail that errors must not wave the
            # candidate through — that is the failure mode this whole
            # change exists to kill. Skipping one candidate also keeps a
            # single bad row from aborting the round for everyone else,
            # which an uncaught raise here would do (runner only catches
            # at the per-pass boundary).
            log.exception(
                "dispatch: guardrail check failed for parent todo id=%d; "
                "skipping (fail-closed)",
                parent_id,
            )
            n_failed += 1
            continue
        if not verdict.allow:
            if verdict.halt_tag is not None:
                # Per-todo halt — counted as a skip, not a failure.
                continue
            # Global daily ceiling. This used to `break` the whole round, which
            # applied a guardrail built for *open-ended planner coroutines* to
            # every candidate — including the committed daily cadences. On
            # 2026-08-07 a runaway planner parent held the fleet over the
            # ceiling from 07:00 UTC, so the morning brief's tick sat six hours
            # with no job minted; the cast finally composed at 13:27 for about
            # $0.05. Cadence work is exempt: it is scheduled, bounded (one job
            # per watch per fire), and the *user's actual deliverable*, so a
            # discretionary loop burning the envelope must not take it down.
            # Discretionary candidates still stop dead. Cadence work is not
            # un-capped — the per-todo and per-tree caps are checked ahead of
            # the ceiling and still halt it.
            #
            # Zero-LLM compute is exempt for the same reason from the other
            # side: the ceiling is an *LLM* budget, and a candidate minting
            # onto a lane that can't spend it (``ssh_node`` aggregate rollup,
            # ``job_inproc`` batch) gains nothing from waiting. Cadence-exempt
            # quest ticks kept the trailing-24h window over the ceiling
            # permanently, which starved every ``autocatpath_aggregate`` mint
            # for 29h (2026-08-16/17) while their seed jobs — minted outside
            # this dispatcher — kept succeeding.
            if exempt_ids is None:
                cadence_ids = _cadence_parent_ids(store, candidate_ids)
                zero_llm_ids = _zero_llm_parent_ids(store, candidate_ids)
                exempt_ids = cadence_ids | zero_llm_ids
                log.info(
                    "dispatch: daily ceiling (%s) — discretionary dispatch "
                    "paused; %d cadence + %d zero-LLM candidate(s) still "
                    "eligible",
                    verdict.reason,
                    len(cadence_ids),
                    len(zero_llm_ids),
                )
            if parent_id not in exempt_ids:
                continue
        try:
            claimed, minted = _claim_and_dispatch(store, parent_id)
        except Exception:
            log.exception("dispatch: failed to process parent todo id=%d", parent_id)
            n_failed += 1
            continue
        n_claimed += claimed
        if minted:
            n_ok += 1
            # Bump tick count so the next sweep sees the increment;
            # caps land on the next candidate enumeration.
            planner_guardrails.bump_tick_count(store, parent_id)
        elif claimed:
            n_failed += 1
    return BatchResult(handler="dispatch", claimed=n_claimed, ok=n_ok, failed=n_failed)


# ── candidate enumeration (unlocked) ──────────────────────────────


def _cadence_parent_ids(store: Store, parent_ids: list[int]) -> set[int]:
    """Of ``parent_ids``, the ones that are *cadence* work — a tick spawned by a
    recurring watch, i.e. whose parent carries ``meta.schedule``.

    This is the exemption predicate for the global daily ceiling. Membership is
    read off the tree rather than a marker on the tick itself, because the
    recurring spawner deliberately does **not** copy ``meta.schedule`` down to
    the child (a tick isn't itself recurring) — the watch above it is the only
    place the cadence is recorded.
    """
    if not parent_ids:
        return set()
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r "
            "JOIN refs w ON w.ref_id = r.parent_id "
            "WHERE r.ref_id = ANY(%s) "
            "AND w.retired_at IS NULL "
            "AND w.meta ? 'schedule'",
            (parent_ids,),
        ).fetchall()
    return {int(r[0]) for r in rows}


def _zero_llm_parent_ids(store: Store, parent_ids: list[int]) -> set[int]:
    """Of ``parent_ids``, the ones whose mint is a *non-LLM* job: an explicit
    ``meta.executor`` in :data:`ZERO_LLM_EXECUTORS` and no ``meta.llm_tier``.

    The second exemption predicate for the global daily ceiling (the first is
    ``_cadence_parent_ids``). The ceiling sums ``llm_call_log`` — an LLM
    budget — so a compute-only mint can't spend against it and must not be
    paused by it. The ``llm_tier`` veto is belt-and-suspenders: a hybrid
    candidate that would also run a planner stays discretionary.
    """
    if not parent_ids:
        return set()
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id FROM refs r "
            "WHERE r.ref_id = ANY(%s) "
            "AND r.meta->>'executor' = ANY(%s) "
            "AND NOT (r.meta ? 'llm_tier')",
            (parent_ids, list(ZERO_LLM_EXECUTORS)),
        ).fetchall()
    return {int(r[0]) for r in rows}


def _candidate_parent_ids(store: Store, *, limit: int) -> list[int]:
    """Return ref ids of dispatchable parent todos.

    Eligibility (planner-coroutine slice):

    * ``kind='todo'`` and not deleted — nor descended from a deleted
      todo. The SQL below only checks the candidate's own
      ``retired_at``; the ancestor walk is ``_drop_orphaned``, applied
      to the result.
    * Auto-run signal: either a closed-vocab ``meta.llm_tier``
      (opus / sonnet / haiku — runs the LLM planner) OR an
      ``executor:<runner>`` tag (code-path runner) OR — legacy —
      ``meta.executor`` set (back-compat with the v1 ``fix_gripe``
      shape; new code uses ``meta.llm_tier`` / the ``executor:`` tag).
    * STATUS in ``open|doing`` (paused / done / blocked skip).
    * No **blocking** child job — see ``_job_blocks_dispatch_sql``. An
      in-flight (``queued`` / ``running``) job always blocks. A
      ``succeeded`` job blocks too UNLESS its job_type is a self-resolving
      coroutine (``plan_tick``): for a deterministic parent a succeeded
      child is the finished work (auto_check resolves the parent next
      sweep), so re-minting in that gap is the gr192606 runaway; only
      ``plan_tick`` legitimately re-ticks on success. ``done`` /
      ``won't-do`` never block; ``failed`` bubbles ``child-failed:N`` to
      the parent, which the exclusion registry handles.
    * No **live** child todo — a child of ``kind='todo'`` whose own
      STATUS is open / doing (the planner spawned children and they
      are still working). Terminal child statuses are ``done`` /
      ``won't-do`` / ``auto-timeout`` — the full closed set
      ``auto_check.py`` defines; omitting ``auto-timeout`` here once
      wedged a parent (and the whole plan_tick mint) permanently and
      alert-invisibly when its auto-check children timed out. This is the coroutine yield: a parent that
      minted children sits silent until they all resolve, then
      re-becomes a candidate so the planner can read the
      ``job_summary`` chunks and continue. **Exception:** a child
      parked on ``ask-user:`` / ``waiting-for:`` (not ``halt`` /
      ``child-failed:``) stops blocking once ``COOLDOWN`` has elapsed
      since the parent's last ``plan_tick`` job — see
      ``_parked_child_still_blocks_sql`` — so the planner keeps periodically
      re-ticking instead of freezing the whole project on one
      parked-on-human leaf.
    * No exclusion tag (registry: halt / halt:* / ask-user* /
      waiting-for:* / child-failed:*).
    * Not a recurring watch root (``meta.schedule`` set) — cadence for
      those is owned by the schedule worker
      (``precis.workers.schedule.worker``), which spawns an ordinary
      worker-mintable subtask child each tick; that child (not the
      root) is the legitimate dispatch candidate. Without this
      exclusion, a recurring root whose latest child resolves instantly
      re-satisfies every other eligibility clause immediately, and the
      dispatcher re-mints a job directly under the root on every pass —
      a tight spin (root and schedule worker fighting over the same
      cadence).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id
              FROM refs r
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND (
                   r.meta ? 'executor'
                   OR r.meta ? 'llm_tier'
                   OR EXISTS (
                       SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id
                          AND t.namespace = 'OPEN' AND t.value LIKE 'executor:%%'
                   )
               )
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) = ANY(%s)
               AND NOT EXISTS (
                   SELECT 1 FROM refs c
                    WHERE c.parent_id = r.ref_id
                      AND c.kind = 'job'
                      AND c.retired_at IS NULL
                      AND """
            + _job_blocks_dispatch_sql("r", "c")
            + """
               )
               AND NOT EXISTS (
                   SELECT 1 FROM refs c
                    WHERE c.parent_id = r.ref_id
                      AND c.kind = 'todo'
                      AND c.retired_at IS NULL
                      AND COALESCE(
                            (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                              WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                            'open'
                          -- terminal-status set must match auto_check.py's
                          -- closed states: a child parked on auto-timeout is
                          -- settled, not live (gr236586 planner wedge)
                          ) NOT IN ('done', 'won''t-do', 'auto-timeout')
                      AND """
            + _parked_child_still_blocks_sql("r", "c")
            + """
               )
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND """
            + _doable_exclusion_clause()
            + """
               )
               AND NOT (r.meta ? 'schedule')
             ORDER BY r.ref_id
             LIMIT %s
            """,
            (sorted(_OPEN_PARENT_STATUSES), limit),
        ).fetchall()
    return _drop_orphaned(store, [int(r[0]) for r in rows])


def _drop_orphaned(store: Store, ids: list[int]) -> list[int]:
    """Filter out candidates with a soft-deleted **ancestor**.

    ``retired_at`` is not transitive: deleting a project todo leaves
    every descendant's own ``retired_at`` NULL, so the candidate query's
    ``r.retired_at IS NULL`` says nothing about whether the *tree* the
    candidate lives in is still alive. Without this walk, deleting a
    parent doesn't stop its subtree — it only removes the row you'd look
    at to notice the subtree is still dispatching. (One such orphaned
    tree ran planner ticks for four days after its parent was deleted,
    and even minted fresh children two days *post*-delete.)

    Skips silently rather than tagging ``halt:``: an orphan is not
    something a human needs to triage on the attention view — the
    delete already said what should happen to it.
    """
    if not ids:
        return ids
    # Strict ancestors only — the candidate query already filtered rows
    # whose own ``retired_at`` is set.
    orphaned = deleted_in_ancestry(store, ids)
    if orphaned:
        log.info(
            "dispatch: skipping %d candidate(s) under a deleted ancestor: %s",
            len(orphaned),
            sorted(orphaned)[:10],
        )
    return [i for i in ids if i not in orphaned]


# ── per-parent locked mint ────────────────────────────────────────


def _claim_and_dispatch(store: Store, parent_id: int) -> tuple[int, bool]:
    """Lock one parent todo and mint its child job.

    Returns ``(claimed, minted)``:

    * ``claimed`` = 1 if we locked the parent, 0 if another worker
      held the row.
    * ``minted`` = True if we wrote the child job, False if we
      rejected (bad executor / job_type / etc).
    """
    with store.tx() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id,
                   r.meta->>'executor' AS executor,
                   r.meta->>'job_type' AS job_type,
                   r.meta->'params' AS params,
                   r.meta ? 'auto_check' AS has_auto_check,
                   r.meta->>'llm_tier' AS llm_tier,
                   r.meta->'llm_select' AS llm_select,
                   (SELECT t.value FROM ref_tags rt
                      JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = r.ref_id
                       AND t.namespace = 'OPEN'
                       AND t.value LIKE 'executor:%%'
                     LIMIT 1) AS executor_tag,
                   r.prio AS parent_prio
              FROM refs r
             WHERE r.ref_id = %s
               AND r.kind = 'todo'
               AND r.retired_at IS NULL
               AND (
                   r.meta ? 'executor'
                   OR r.meta ? 'llm_tier'
                   OR EXISTS (
                       SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id
                          AND t.namespace = 'OPEN' AND t.value LIKE 'executor:%%'
                   )
               )
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) = ANY(%s)
               AND NOT EXISTS (
                   SELECT 1 FROM refs c
                    WHERE c.parent_id = r.ref_id
                      AND c.kind = 'job'
                      AND c.retired_at IS NULL
                      AND """
            + _job_blocks_dispatch_sql("r", "c")
            + """
               )
               AND NOT EXISTS (
                   SELECT 1 FROM refs c
                    WHERE c.parent_id = r.ref_id
                      AND c.kind = 'todo'
                      AND c.retired_at IS NULL
                      AND COALESCE(
                            (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                              WHERE rt.ref_id = c.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                            'open'
                          -- terminal-status set must match auto_check.py's
                          -- closed states: a child parked on auto-timeout is
                          -- settled, not live (gr236586 planner wedge)
                          ) NOT IN ('done', 'won''t-do', 'auto-timeout')
                      AND """
            + _parked_child_still_blocks_sql("r", "c")
            + """
               )
               AND NOT EXISTS (
                   -- Re-check the exclusion registry inside the
                   -- FOR UPDATE — guards against a halt / ask-user
                   -- tag landing between candidate enumeration and
                   -- the lock.
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND """
            + _doable_exclusion_clause()
            + """
               )
             FOR UPDATE OF r SKIP LOCKED
            """,
            (parent_id, sorted(_OPEN_PARENT_STATUSES)),
        ).fetchone()
        if row is None:
            # Another worker holds it, or the parent's state changed
            # between enumeration and lock (status flipped, child
            # job was just minted by a race). Either way, no-op.
            return (0, False)
        ref_id = int(row[0])
        executor = row[1]
        job_type = row[2]
        params = dict(row[3] or {})
        has_auto_check = bool(row[4])
        llm_tier = row[5]
        llm_select = row[6]
        executor_tag = row[7]
        # The parent todo's prio flows down the DAG onto the minted job so
        # the claim ordering (slice 6a, 0014 direction: lower = more urgent)
        # can favour it — quests/urgent projects get their compute claimed
        # first. NULL stays NULL → the claim's COALESCE default (byte-
        # identical to FIFO for unset work).
        parent_prio = int(row[8]) if row[8] is not None else None

        # Planner-coroutine path: when a todo carries ``meta.llm_tier``
        # but lacks ``meta.executor``, synthesize the dispatch parameters
        # from it. The model picker IS the tier value (``llm_tier='opus'``
        # → ``model=opus``); the job_type is the generic planner tick
        # (``plan_tick``) which knows how to read the parent's body,
        # ancestry, and prior child summaries into a single prompt.
        # ``executor:<runner>`` tags route to code-path runners with a
        # parallel synthesis (job_type = ``executor:<runner>``).
        if not isinstance(executor, str) and llm_tier:
            executor = "claude_inproc"
            job_type = job_type or "plan_tick"
        elif not isinstance(executor, str) and executor_tag:
            runner = str(executor_tag).removeprefix("executor:")
            # Reserved; v1 has no registered executor:* values, so
            # this branch only fires if the closed-vocab guard is
            # widened in a future slice. The runner name is the
            # job_type by convention.
            executor = runner
            job_type = job_type or runner

        # A todo can also declare ``meta.executor``/``meta.job_type``
        # explicitly (the documented precis-job-help pattern) while
        # still carrying ``meta.llm_tier`` as the model picker — e.g. a
        # filer who wants plan_tick but an explicit executor for
        # clarity. Synthesize the model param in that case too, not
        # only on the NULL-executor planner-coroutine path above,
        # else plan_tick crashes on a missing params['model'].
        if llm_tier and job_type == "plan_tick":
            params.setdefault("model", str(llm_tier))
            # ``meta.llm_select`` (structured selection, optional
            # sibling of ``llm_tier``) rides along as ``params['select']`` —
            # plan_tick reads it defensively (a corrupt/malformed dict can't
            # crash a tick), so a plain dict is threaded through as-is.
            if isinstance(llm_select, dict):
                params.setdefault("select", llm_select)

        # Operator hold switch (``PRECIS_SUSPENDED_JOB_TYPES``): don't mint
        # a job of a suspended type at all — the claim path independently
        # refuses to run them, but skipping the mint too keeps the queue
        # from accumulating rows during a long hold. No halt tag: the
        # parent stays an ordinary candidate and mints on the next sweep
        # after the hold clears. Counts as a no-op, not a failure.
        if isinstance(job_type, str) and job_type in suspended_job_types():
            log.debug(
                "dispatch: parent todo id=%d job_type=%r is operator-"
                "suspended (PRECIS_SUSPENDED_JOB_TYPES); skipping mint",
                ref_id,
                job_type,
            )
            return (0, False)

        # Validate executor + job_type at dispatch time. The TodoHandler
        # doesn't validate ``meta.executor`` / ``meta.job_type`` on
        # ``put`` (it's just a meta key from the handler's perspective);
        # the dispatcher is the boundary that rejects mis-spelled or
        # incompatible combinations. Logs + skips on failure so the
        # operator sees the broken parent in logs without crashing
        # the pass.
        # Validation failures self-halt the parent (see
        # ``_halt_bad_dispatch``) rather than warn-and-skip: an
        # un-dispatchable parent that merely skips stays a candidate and
        # re-warns on every sweep, forever.
        if not isinstance(executor, str) or not is_known_executor(executor):
            return _halt_bad_dispatch(
                store, conn, ref_id, f"has unknown meta.executor={executor!r}"
            )
        if not isinstance(job_type, str):
            return _halt_bad_dispatch(store, conn, ref_id, "has missing meta.job_type")
        spec = get_job_type(job_type)
        if spec is None:
            return _halt_bad_dispatch(
                store, conn, ref_id, f"has unknown meta.job_type={job_type!r}"
            )
        if executor not in spec.compatible_executors:
            return _halt_bad_dispatch(
                store,
                conn,
                ref_id,
                f"job_type={job_type!r} incompatible with executor={executor!r}",
            )
        missing_caps = spec.requires - EXECUTOR_PROVIDES[executor]
        if missing_caps:
            return _halt_bad_dispatch(
                store,
                conn,
                ref_id,
                f"executor={executor!r} missing caps for {job_type!r}: "
                f"{sorted(missing_caps)}",
            )

        # A todo can set ``meta.executor``/``meta.job_type='plan_tick'``
        # explicitly without a resolvable ``meta.llm_tier`` (nothing for the
        # synthesis block above to key off), and without ``params.model``
        # either — the mint would otherwise proceed with a modelless
        # ``params={}`` and the child job crashes instantly on
        # ``params["model"]`` (plan_tick.run). Reuse the same
        # ``validate_submit`` the direct put-job path already runs
        # (``handlers/job.py``) so this stays a single source of truth for
        # "what's a valid plan_tick params dict", including the
        # ``PLANNER_MODEL_ALIASES`` check — rather than a second bespoke
        # model-presence check here.
        if job_type == "plan_tick" and spec.validate_submit is not None:
            err = spec.validate_submit(store, gripe_id=None, params=params)
            if err is not None:
                return _halt_bad_dispatch(
                    store,
                    conn,
                    ref_id,
                    f"{err} (set meta.llm_tier or params.model on the todo)",
                )

        # Auto-inject ``auto_check`` if the writer didn't set one, so a
        # deterministic job's parent resolves on the child's success.
        # Skip it for self-resolving job types (the ``plan_tick``
        # coroutine drives its own STATUS — see
        # ``_SELF_RESOLVING_JOB_TYPES``); injecting there would close the
        # parent on its first clean tick.
        if job_type in _SELF_RESOLVING_JOB_TYPES:
            # Belt-and-suspenders: declining to *inject* isn't enough —
            # a stale / hand-authored / legacy ``child_job_succeeded``
            # spec can already be attached (this is exactly what
            # auto-closed an in-progress paper cascade on its first clean
            # planning tick). Strip it so the auto_check worker can't fire
            # it. Only the footgun type is removed; a deliberate
            # ``time_past`` / ``ask-user`` spec on a planner is left alone.
            conn.execute(
                """
                UPDATE refs
                   SET meta = meta - 'auto_check'
                 WHERE ref_id = %s
                   AND meta->'auto_check'->>'type' = 'child_job_succeeded'
                """,
                (ref_id,),
            )
        elif not has_auto_check:
            conn.execute(
                """
                UPDATE refs
                   SET meta = meta || jsonb_build_object(
                                'auto_check',
                                jsonb_build_object('type', 'child_job_succeeded')
                              )
                 WHERE ref_id = %s
                """,
                (ref_id,),
            )

        # Mint the child job ref. Stay inside the tx so the row
        # lock spans claim + mint + status tag + event append.
        child_meta: dict[str, Any] = {
            "job_type": job_type,
            "executor": executor,
            "params": params,
            "dispatched_from_todo": ref_id,
        }
        # Serving affinity (narrow: planner ticks that drive a served-OSS model).
        # When the tick's rung-0 model is advertised as an ``llm:`` slot on some
        # host, stamp ``meta.requires`` so the ``_finish_claim`` ``llm:`` hard veto
        # gates the job onto a serving host — the agent runs where the model lives
        # (using the local slot) instead of falling to hosted cloud. A claude/cloud
        # tick, or a model served nowhere, gets no requirement → host-agnostic.
        served_requires = _served_model_requirement(conn, job_type, params)
        if served_requires is not None:
            child_meta["requires"] = served_requires
        title = f"{job_type} (dispatched from todo:{ref_id})"
        child = store.insert_ref(
            kind="job",
            slug=None,
            title=title,
            meta=child_meta,
            parent_id=ref_id,
            prio=parent_prio,
            conn=conn,
        )
        store.add_tag(
            child.id,
            Tag.closed("STATUS", "queued"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.append_event(
            ref_id,
            # Vocabulary-compaction Stage D: the worker registered as
            # `minter` (registry name; legacy `dispatch`) — this is the
            # historical source string on ref_events, not the pass's
            # worker_logs identity (see registry.py's `log_name`).
            source="minter",
            event="job-minted",
            payload={
                "job_id": int(child.id),
                "job_type": job_type,
                "executor": executor,
            },
            conn=conn,
        )
        log.info(
            "dispatch: parent #%d → minted job #%d (job_type=%s, executor=%s)",
            ref_id,
            child.id,
            job_type,
            executor,
        )
        return (1, True)


__all__ = ["run_dispatch_pass"]
