"""Hourly tally of paid LLM admission outcomes per work class (gr347551).

/budget shows how often each class (:mod:`precis.budget.work_class`) was
``served`` (admitted past the budget gate), ``paused`` (refused by the
quota taper / park or the dollar cap) or ``degraded`` (a deliverable or
interactive caller re-asked on a smaller tier after a pause) over the last
24h. A paused call never reaches ``llm_call_log`` — the gate returns before
dispatch — so the tally lives in its own table, ``llm_outcome_hourly``
(one upserted row per hour × class × outcome).

``served``/``paused`` are counted per gate decision, so a degraded call
shows once as ``paused`` (the refused tier), once as ``served`` (the
smaller tier) and once as ``degraded``. Best-effort and dark: no store or
a failed write is logged at debug and never raises.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from precis.budget.work_class import WorkClass

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

OUTCOMES: tuple[str, ...] = ("served", "degraded", "paused")


def record(store: Store | None, work_class: WorkClass, outcome: str) -> None:
    """Add one ``outcome`` for ``work_class`` to the current hour's bucket."""
    if store is None:
        return
    try:
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO llm_outcome_hourly (hour, work_class, outcome, n) "
                "VALUES (date_trunc('hour', now()), %s, %s, 1) "
                "ON CONFLICT (hour, work_class, outcome) "
                "DO UPDATE SET n = llm_outcome_hourly.n + 1",
                (work_class.value, outcome),
            )
    except Exception:
        log.debug("budget outcomes: tally write failed", exc_info=True)


def counts_24h(store: Store | None) -> dict[str, dict[str, int]]:
    """``{work_class: {outcome: n}}`` over the last 24 hourly buckets, every
    class × outcome present (zeros included). Empty on no store / failure."""
    if store is None:
        return {}
    try:
        with store.tx() as conn:
            rows = conn.execute(
                "SELECT work_class, outcome, sum(n)::bigint FROM llm_outcome_hourly "
                "WHERE hour > now() - interval '24 hours' "
                "GROUP BY work_class, outcome"
            ).fetchall()
    except Exception:
        log.debug("budget outcomes: tally read failed", exc_info=True)
        return {}
    out = {c.value: dict.fromkeys(OUTCOMES, 0) for c in WorkClass}
    for cls, outcome, n in rows:
        out.setdefault(str(cls), dict.fromkeys(OUTCOMES, 0))[str(outcome)] = int(n)
    return out


__all__ = ["OUTCOMES", "counts_24h", "record"]
