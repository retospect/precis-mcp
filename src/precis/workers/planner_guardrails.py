"""Planner-coroutine guardrails — three backstops against runaway.

The default-on planner pattern ("any LLM:*-tagged todo runs") is a
credit-card incinerator without sanity bounds. This module is the
three caps the dispatcher consults before minting a planner job:

1. **Per-todo tick cap** (``meta.tick_count``). If a planner has
   re-fired ``MAX_TICKS`` times without finishing, auto-tag
   ``halt:tick-cap`` and yield. The cap means "you're not
   converging; a human needs to look."

2. **Per-todo cost cap** (``meta.cost_usd``). Accumulated from each
   child plan_tick job. If a todo's lineage cost exceeds
   ``MAX_TODO_USD`` (default $2), auto-tag ``halt:cost-cap``.
   Bounds how much one task can cost regardless of depth.

3. **Global daily cost ceiling** (``PRECIS_DAILY_COST_CEILING``,
   default $20/day). Sums plan_tick costs across all parents over
   the last 24h; when the ceiling is hit the dispatcher returns 0
   candidates until the rolling window clears. Coarse but
   effective — protects the overall budget envelope.

This module is read-only on the dispatcher path: it returns ``True``
("OK to dispatch") or applies a halt tag and returns ``False``.
The halt-application path is async to the dispatch loop's tx so it
doesn't deadlock on the candidate query's read lock.

Tunables (env vars, all $-denominated):

* ``PRECIS_MAX_TICKS`` (int, default 10)
* ``PRECIS_MAX_TODO_USD`` (float, default 2.0)
* ``PRECIS_DAILY_COST_CEILING`` (float, default 20.0)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
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
        log.warning("planner_guardrails: %s=%r is not an int; using %d",
                    name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("planner_guardrails: %s=%r is not a float; using %f",
                    name, raw, default)
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


def check_parent(store: Store, *, parent_ref_id: int) -> GuardrailVerdict:
    """Run the three checks against a planner-candidate parent todo.

    Order: tick cap, then cost cap, then daily ceiling. Tick cap is
    cheapest (no SQL aggregate); daily ceiling is the broadest
    safety net but the most expensive to compute, so it runs last
    and benefits from the prior cheap rejections.
    """
    max_ticks = _env_int("PRECIS_MAX_TICKS", 10)
    max_todo_usd = _env_float("PRECIS_MAX_TODO_USD", 2.0)
    daily_ceiling = _env_float("PRECIS_DAILY_COST_CEILING", 20.0)

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

    daily_cost = _read_daily_cost(store)
    if daily_cost >= daily_ceiling:
        # Global ceiling — DON'T tag this specific parent; just skip
        # the dispatch wholesale until the window rolls. Other
        # parents on the candidate list will hit the same gate and
        # also skip.
        log.warning(
            "planner_guardrails: daily ceiling hit ($%.2f >= $%.2f); "
            "dispatcher skipping parent #%d",
            daily_cost,
            daily_ceiling,
            parent_ref_id,
        )
        return GuardrailVerdict(
            allow=False,
            halt_tag=None,
            reason=f"daily ceiling ${daily_cost:.2f} >= ${daily_ceiling:.2f}",
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
    """Sum ``cost_usd`` across every child job under ``ref_id``.

    Uses each job's ``meta.cost_usd`` (written by the runner from
    ``claude -p``'s cost output). Robust to missing fields — older
    jobs lack the meta key and count as 0.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE((meta->>'cost_usd')::float, 0)), 0)
              FROM refs
             WHERE parent_id = %s
               AND kind = 'job'
               AND deleted_at IS NULL
            """,
            (ref_id,),
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _read_daily_cost(store: Store) -> float:
    """Sum job costs across all parents over the rolling last 24h."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE((meta->>'cost_usd')::float, 0)), 0)
              FROM refs
             WHERE kind = 'job'
               AND deleted_at IS NULL
               AND created_at >= now() - interval '24 hours'
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
    "GuardrailVerdict",
    "bump_tick_count",
    "check_parent",
]
