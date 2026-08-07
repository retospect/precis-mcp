"""Planner-coroutine guardrails — three backstops against runaway.

The default-on planner pattern ("any meta.llm_tier-set todo runs") is a
credit-card incinerator without sanity bounds. This module is the
three caps the dispatcher consults before minting a planner job:

1. **Per-todo tick cap** (``meta.tick_count``). If a planner has
   re-fired ``MAX_TICKS`` times without finishing, auto-tag
   ``halt:tick-cap`` and yield. The cap means "you're not
   converging; a human needs to look."

2. **Per-todo cost cap**. Real recorded spend attributed to the todo
   in ``llm_call_log`` (``ref_id`` = the parent todo, stamped by
   ``plan_tick``'s ``LlmRequest``). If it exceeds ``MAX_TODO_USD``
   (default $2), auto-tag ``halt:cost-cap``. Bounds how much one
   task can cost regardless of depth.

3. **Per-tree cost cap** (``PRECIS_MAX_TREE_USD``, default $10).
   The per-todo cap is per-*todo*, so a wide fan-out multiplies it:
   258 sibling todos under one project each get their own $2, i.e.
   $516 of headroom nobody authorised. This cap sums recorded spend
   across the candidate's whole root subtree and halts the candidate
   when the project as a whole has spent enough.

4. **Global daily cost ceiling** (``PRECIS_DAILY_COST_CEILING``,
   default $20/day). Sums *all* recorded LLM spend over the last
   24h; when the ceiling is hit the dispatcher returns 0 candidates
   until the rolling window clears. Coarse but effective — protects
   the overall budget envelope.

   Cap 4 is the only one that isn't planner-specific, and it is
   re-exported as :func:`daily_budget` because the dispatcher is not
   the only lane that spends: the **scheduler's** LLM cadences
   (``dream_agent`` / ``structural`` / ``deep_review``,
   :mod:`precis.workers.scheduler`) run on their own leases and used
   to spend straight through a tripped ceiling. That inverted the
   gate — prod froze the dispatcher for 18h from 2026-08-06 19:02
   while the *more* expensive opus cadences kept firing. Both lanes
   now read this one number.

**Why ``llm_call_log`` and not ``meta.cost_usd``.** Checks 2 and 3
originally summed a ``meta.cost_usd`` key on child job refs that
*nothing in the codebase ever wrote* — so both caps read $0.00 and
could never fire, however far spend ran (a soft-deleted project tree
burned $291 over five days against a $20/day ceiling before anyone
noticed). ``llm_call_log`` is the authoritative per-dispatch ledger
and needs no cooperation from the runner, so the caps cannot rot the
same way again. Deliberately **includes** the ``claude_agent`` /
``claude_p`` OAuth transports that :mod:`precis.budget.meter`
excludes: the meter tracks real money, while these caps bound the
planner's *discretionary* burn — subscription quota very much
included, since that is the path that actually ran away.

This module is read-only on the dispatcher path: it returns ``True``
("OK to dispatch") or applies a halt tag and returns ``False``.
The halt-application path is async to the dispatch loop's tx so it
doesn't deadlock on the candidate query's read lock.

Tunables (env vars):

* ``PRECIS_MAX_TICKS`` (int, default 10)
* ``PRECIS_MAX_TODO_USD`` (float, default 2.0)
* ``PRECIS_MAX_TREE_USD`` (float, default 10.0)
* ``PRECIS_DAILY_COST_CEILING`` (float, default 20.0)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "planner_guardrails: %s=%r is not an int; using %d", name, raw, default
        )
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "planner_guardrails: %s=%r is not a float; using %f", name, raw, default
        )
        return default


@dataclass(frozen=True)
class GuardrailVerdict:
    """Result of checking a candidate parent against the guardrails.

    ``allow=True`` → dispatcher mints the planner job.
    ``allow=False`` → dispatcher skips; ``halt_tag`` was applied if
    set so the parent surfaces under ``view='attention'``.
    """

    allow: bool
    halt_tag: str | None = None
    reason: str | None = None


@dataclass
class RoundContext:
    """Per-dispatch-round memo for the two broad cost queries.

    ``check_parent`` runs once per candidate (up to 50 a round, every
    minute), but the daily total is identical for every one of them and
    sibling candidates share a subtree — so without a memo the incident
    shape (hundreds of siblings under one root) re-runs the same
    fleet-wide sum and the same tree walk once per sibling. Scoped to a
    single round so the numbers stay fresh between sweeps.
    """

    daily_cost: float | None = None
    tree_cost: dict[int, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DailyBudget:
    """The global 24h envelope: what the fleet has recorded vs the ceiling.

    Exposed (rather than left private to :func:`check_parent`) so the
    scheduler's spending cadences gate on the *same* number the dispatcher
    does. Two independent notions of "the day's spend" is precisely how the
    original ``meta.cost_usd`` caps rotted into never firing — one definition,
    one env var, or the caps drift apart again.
    """

    spent: float
    ceiling: float

    @property
    def over(self) -> bool:
        return self.spent >= self.ceiling

    def __str__(self) -> str:
        return f"${self.spent:.2f} >= ${self.ceiling:.2f}"


def daily_budget(store: Store, *, ctx: RoundContext | None = None) -> DailyBudget:
    """Recorded fleet-wide LLM spend over the trailing 24h, vs the ceiling.

    ``ctx`` memoises the (broad) aggregate across one dispatch round; callers
    outside the dispatcher pass none and get a fresh read.
    """
    return DailyBudget(
        spent=_daily_cost(store, ctx),
        ceiling=_env_float("PRECIS_DAILY_COST_CEILING", 20.0),
    )


def check_parent(
    store: Store, *, parent_ref_id: int, ctx: RoundContext | None = None
) -> GuardrailVerdict:
    """Run the four checks against a planner-candidate parent todo.

    Order: tick cap, per-todo cost, per-tree cost, then daily ceiling.
    Tick cap is cheapest (single indexed row); the tree walk and the
    daily aggregate are the broadest safety nets but the most
    expensive to compute, so they run last and benefit from the prior
    cheap rejections.
    """
    max_ticks = _env_int("PRECIS_MAX_TICKS", 10)
    max_todo_usd = _env_float("PRECIS_MAX_TODO_USD", 2.0)
    max_tree_usd = _env_float("PRECIS_MAX_TREE_USD", 10.0)

    tick_count = _read_tick_count(store, parent_ref_id)
    if tick_count >= max_ticks:
        return _apply_halt(
            store,
            parent_ref_id,
            "halt:tick-cap",
            f"tick cap hit ({tick_count} >= {max_ticks})",
        )

    cost_usd = _read_cost_usd(store, parent_ref_id)
    if cost_usd >= max_todo_usd:
        return _apply_halt(
            store,
            parent_ref_id,
            "halt:cost-cap",
            f"per-todo cost cap hit (${cost_usd:.2f} >= ${max_todo_usd:.2f})",
        )

    tree_usd = _tree_cost(store, parent_ref_id, ctx)
    if tree_usd >= max_tree_usd:
        return _apply_halt(
            store,
            parent_ref_id,
            "halt:tree-cost-cap",
            f"per-tree cost cap hit (${tree_usd:.2f} >= ${max_tree_usd:.2f})",
        )

    budget = daily_budget(store, ctx=ctx)
    if budget.over:
        # Global ceiling — DON'T tag this specific parent; just skip
        # the dispatch wholesale until the window rolls. Other
        # parents on the candidate list will hit the same gate and
        # also skip.
        log.warning(
            "planner_guardrails: daily ceiling hit (%s); dispatcher skipping "
            "parent #%d",
            budget,
            parent_ref_id,
        )
        return GuardrailVerdict(
            allow=False,
            halt_tag=None,
            reason=f"daily ceiling {budget}",
        )

    return GuardrailVerdict(allow=True)


def _read_tick_count(store: Store, ref_id: int) -> int:
    """Read ``meta.tick_count`` (default 0). Bump happens at job mint."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT (meta->>'tick_count')::int FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _read_cost_usd(store: Store, ref_id: int) -> float:
    """Sum recorded spend attributed to ``ref_id`` in ``llm_call_log``.

    ``plan_tick`` stamps ``LlmRequest.ref_id = parent_ref_id``, so every
    tick's dispatch lands on the parent todo's own ledger rows — the
    lifetime cost of the task, no runner cooperation required.

    Lifetime, not windowed: this cap answers "how much has this one task
    cost", so an old expensive todo stays halted. The ledger's own
    retention GC (``route_log.prune``) is the only thing that ages a
    row out.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0)::float
              FROM llm_call_log
             WHERE ref_id = %s AND cost_usd IS NOT NULL
            """,
            (ref_id,),
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _tree_cost(store: Store, ref_id: int, ctx: RoundContext | None) -> float:
    """``_read_tree_cost_usd`` memoised per *root* for this round.

    Keyed on the root, not the candidate, so N siblings under one project
    share a single walk instead of doing N identical ones.
    """
    if ctx is None:
        return _read_tree_cost_usd(store, ref_id)
    root = _root_of(store, ref_id)
    if root not in ctx.tree_cost:
        ctx.tree_cost[root] = _read_tree_cost_usd(store, ref_id)
    return ctx.tree_cost[root]


def _daily_cost(store: Store, ctx: RoundContext | None) -> float:
    """``_read_daily_cost`` computed once per round — it has no per-candidate
    predicate, so every candidate in a round gets the same number."""
    if ctx is None:
        return _read_daily_cost(store)
    if ctx.daily_cost is None:
        ctx.daily_cost = _read_daily_cost(store)
    return ctx.daily_cost


def _root_of(store: Store, ref_id: int) -> int:
    """The topmost ancestor of ``ref_id`` (itself when it has no parent)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            WITH RECURSIVE up (ref_id, parent_id, depth) AS (
                SELECT r.ref_id, r.parent_id, 0
                  FROM refs r WHERE r.ref_id = %(id)s
                UNION ALL
                SELECT p.ref_id, p.parent_id, u.depth + 1
                  FROM up u JOIN refs p ON p.ref_id = u.parent_id
                 WHERE u.depth < %(max_depth)s
            )
            SELECT ref_id FROM up ORDER BY depth DESC LIMIT 1
            """,
            {"id": ref_id, "max_depth": _MAX_TREE_DEPTH},
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else ref_id


#: Max ancestor/descendant hops the tree walk will follow. A guard against a
#: cyclic ``parent_id`` (which would otherwise spin the recursive CTE forever),
#: not a real depth limit — planner trees are nowhere near this deep.
_MAX_TREE_DEPTH = 64


def _read_tree_cost_usd(store: Store, ref_id: int) -> float:
    """Sum recorded spend across the whole root subtree containing ``ref_id``.

    Walks up to the root ancestor, then back down over every
    descendant, and totals their ``llm_call_log`` rows. Soft-deleted
    refs are **included** on purpose: spend under a deleted parent is
    still spend, and excluding it would let a delete reset the budget.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            WITH RECURSIVE up (ref_id, parent_id, depth) AS (
                SELECT r.ref_id, r.parent_id, 0
                  FROM refs r WHERE r.ref_id = %(id)s
                UNION ALL
                SELECT p.ref_id, p.parent_id, u.depth + 1
                  FROM up u JOIN refs p ON p.ref_id = u.parent_id
                 WHERE u.depth < %(max_depth)s
            ),
            root AS (
                SELECT ref_id FROM up ORDER BY depth DESC LIMIT 1
            ),
            down (ref_id, depth) AS (
                SELECT ref_id, 0 FROM root
                UNION ALL
                SELECT c.ref_id, d.depth + 1
                  FROM down d JOIN refs c ON c.parent_id = d.ref_id
                 WHERE d.depth < %(max_depth)s
            )
            SELECT COALESCE(SUM(l.cost_usd), 0)::float
              FROM llm_call_log l
             WHERE l.cost_usd IS NOT NULL
               AND l.ref_id IN (SELECT ref_id FROM down)
            """,
            {"id": ref_id, "max_depth": _MAX_TREE_DEPTH},
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _read_daily_cost(store: Store) -> float:
    """Sum *all* recorded LLM spend over the rolling last 24h.

    Every source, every transport — not just planner ticks. The ceiling
    gates the planner because that is the expensive discretionary work,
    but the envelope it protects is the whole day's spend: if the fleet
    has already burned the day's budget, minting more opus ticks is
    exactly the wrong move.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0)::float
              FROM llm_call_log
             WHERE cost_usd IS NOT NULL
               AND ts >= now() - interval '24 hours'
            """
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _apply_halt(
    store: Store,
    ref_id: int,
    halt_tag: str,
    reason: str,
) -> GuardrailVerdict:
    """Tag the parent ``halt:<reason>`` and return a deny verdict.

    Writes the tag in its own connection so the dispatch query's
    transaction doesn't get tangled. The next dispatch sweep will
    see the tag in the exclusion registry and skip the parent
    cleanly; attention view surfaces it.
    """
    from precis.store.types import Tag

    try:
        store.add_tag(ref_id, Tag.open(halt_tag), set_by="system")
        log.info("planner_guardrails: halted parent #%d: %s", ref_id, reason)
    except Exception:
        log.exception("planner_guardrails: failed to halt parent #%d", ref_id)
    return GuardrailVerdict(allow=False, halt_tag=halt_tag, reason=reason)


def bump_tick_count(store: Store, ref_id: int) -> int:
    """Increment ``meta.tick_count`` on a parent and return the new value.

    Called by the dispatcher at job-mint time so the next candidate
    enumeration sees the updated count. Uses a JSONB update that's
    idempotent on missing key.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            UPDATE refs
               SET meta = jsonb_set(
                     COALESCE(meta, '{}'::jsonb),
                     '{tick_count}',
                     to_jsonb(COALESCE((meta->>'tick_count')::int, 0) + 1),
                     true
                   )
             WHERE ref_id = %s
         RETURNING (meta->>'tick_count')::int
            """,
            (ref_id,),
        ).fetchone()
        conn.commit()
    return int(row[0]) if row else 0


__all__ = [
    "DailyBudget",
    "GuardrailVerdict",
    "bump_tick_count",
    "check_parent",
    "daily_budget",
]
