"""Cost-accounting tick audit (gr690184 item 2, gr415941 part 4).

Question: are the periodic ticks that keep the spend ledgers themselves an
expensive *token* burn — real LLM tokens spent for little value? Measure,
then keep / thin / drop.

The ticks in question and what they actually run:

* ``route_log.rollup`` / ``route_log.gc`` (the sweeper tick) — pure SQL
  over ``llm_call_log`` / ``llm_call_rollup`` / ``app_state``; no router
  dispatch, no LLM.
* :func:`precis.budget.meter.current_status` — one cached tote query per
  TTL window at the dispatch chokepoint; no router dispatch, no LLM.
* :func:`precis.budget.outcomes` upsert — one row write per gate decision;
  no router dispatch, no LLM.
* ``quota_check`` — the only LLM-touching budget tick: one ``claude -p``
  call per ``REFRESH_INTERVAL_S`` (600 s), fired only when the persisted
  snapshot is older than that. Its input size is dominated by the CLI's own
  context, not by the prompt word, so it is NOT a few-token call and no
  per-day token figure is assumed here. The probe runs the CLI directly
  (``utils/claude_quota.py``) and, as far as the code shows, writes no
  ``llm_call_log`` row, so the table below may not see it at all.

So the rollup / gc / meter / outcomes burns zero LLM tokens **by
construction** — they never call the router. :func:`measure_tick_tokens`
is the belt-and-braces proof: one grouped read over ``llm_call_log`` for
any row attributed to a cost-accounting source, so the audit is repeatable
and a future metered call added to one of these paths shows up here instead
of slipping in unnoticed.

Decision: rollup / gc / meter / outcomes burn zero LLM tokens by
construction (KEEP, nothing to thin). The quota probe is UNMEASURED: whether
to keep, thin (longer ``REFRESH_INTERVAL_S``) or drop it depends on what
:func:`measure_tick_tokens` returns once the probe's calls are logged under
a source in :data:`COST_ACCOUNTING_SOURCES`; zero rows today means "not
logged", not "free". The expensive side of cost accounting was the pre-0314
seq scans over ``llm_call_log`` (gr575088), already fixed by the hourly
rollup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store

#: ``llm_call_log.source`` keys that belong to cost-accounting passes. The
#: rollup/gc/meter/outcomes passes dispatch nothing, so they can never
#: appear here. ``quota_check`` is a guess at the probe's source label —
#: unverified against the rows the probe actually writes (see module doc).
COST_ACCOUNTING_SOURCES: tuple[str, ...] = ("quota_check",)


@dataclass(frozen=True, slots=True)
class TickTokens:
    """What the cost-accounting ticks burned in LLM terms over a window."""

    rows: int
    input_tokens: int
    output_tokens: int
    cost_usd: float

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def measure_tick_tokens(store: Store, *, days: int = 7) -> TickTokens:
    """Sum LLM tokens + billed cost attributed to cost-accounting sources
    over the trailing ``days`` (from ``llm_call_log``). Zero rows means the
    ticks burned no LLM tokens at all — the expected, by-construction answer
    for every pass except the bounded quota probe."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*), "
            "coalesce(sum(input_tokens), 0)::bigint, "
            "coalesce(sum(output_tokens), 0)::bigint, "
            "coalesce(sum(cost_usd), 0)::float "
            "FROM llm_call_log "
            "WHERE ts > now() - make_interval(days => %s) "
            "AND source = ANY(%s::text[])",
            (days, list(COST_ACCOUNTING_SOURCES)),
        ).fetchone()
    if row is None:
        return TickTokens(rows=0, input_tokens=0, output_tokens=0, cost_usd=0.0)
    return TickTokens(
        rows=int(row[0]),
        input_tokens=int(row[1]),
        output_tokens=int(row[2]),
        cost_usd=float(row[3]),
    )


__all__ = ["COST_ACCOUNTING_SOURCES", "TickTokens", "measure_tick_tokens"]
