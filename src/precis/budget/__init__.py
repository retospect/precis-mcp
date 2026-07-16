"""Budget guardrails — a lightweight spend backstop.

Two deliverables (see ``docs/design/budget-guardrails.md``):

* **A "sense" of cost** — :mod:`precis.budget.bands` maps every router
  :class:`~precis.utils.llm.router.Tier` (and any dollar figure) to a
  qualitative ``free · cheap · expensive`` + ``fast · slow`` band. Surfaced
  to the model as words, not dollar arithmetic.
* **A global circuit breaker** — :mod:`precis.budget.meter` rolls the
  existing cost ledger (``llm_call_log`` + ``cache_state``) into an hourly +
  24h spend total; :mod:`precis.budget.breaker` refuses *new paid* work
  once a cap is crossed. Only free local work always flows.

The breaker is **dark by construction**: with no store bound (DB-free
callers, tests) it never trips. :func:`bind_store` wires the process store at
worker / runtime boot, mirroring :mod:`precis.route_log`.
"""

from __future__ import annotations

from precis.budget.bands import (
    Band,
    Cost,
    Pace,
    band_for_tier,
    cost_from_usd,
    is_expensive,
    is_paid,
)
from precis.budget.meter import BudgetStatus, bind_store, current_status, spent_usd

__all__ = [
    "Band",
    "BudgetStatus",
    "Cost",
    "Pace",
    "band_for_tier",
    "bind_store",
    "cost_from_usd",
    "current_status",
    "is_expensive",
    "is_paid",
    "spent_usd",
]
