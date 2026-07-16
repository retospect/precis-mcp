"""Rolling spend meter — the tote, a query over the existing ledger.

Sums *actual* recorded cost over a time window from the two authoritative
ledgers, with no double-count (see ``docs/design/budget-guardrails.md`` open
question #1):

* ``llm_call_log.cost_usd`` — every router dispatch (claude reports true
  dollars; OSS/local is priced via :mod:`precis.budget.pricing`).
* ``cache_state.cost_usd`` — paid fetches (perplexity, …); ``fetched_at`` is
  the last-fetch time, so a window sum approximates spend in that window.

``ref_events`` is deliberately **excluded**: an agentic call already logs to
``llm_call_log`` via ``router.dispatch``, so summing ``ref_events`` too would
double-count it.

**Dark by construction.** :func:`bind_store` wires the process store at worker
/ runtime boot (mirroring :mod:`precis.route_log`); with none bound,
:func:`current_status` returns ``None`` and the breaker never trips. A short
TTL cache keeps the per-dispatch overhead to one query every
:data:`_CACHE_TTL_S` seconds — safe because only slow ``expensive``-band calls
are gated.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store

#: Process-bound store the meter queries. ``bind_store`` sets it at boot;
#: unbound → :func:`current_status` returns ``None`` (breaker stays dark).
_STORE: Store | None = None

#: Default caps (USD). Placeholders — tune from a week of observed spend on
#: the read-only meter before the breaker goes live. Env-overridable.
DEFAULT_HOURLY_USD = 5.0
DEFAULT_DAILY_USD = 20.0

#: Transports whose ``llm_call_log.cost_usd`` is **notional**, not real money.
#: The ``claude -p`` OAuth path reports an API-list-price-equivalent dollar
#: figure, but that spend draws down the account's rate-limit *quota*, not a
#: metered balance — so it is excluded from the dollar meter (which must
#: reflect real money) and gated on the quota snapshot instead
#: (:mod:`precis.budget.quota`). Keep in sync with the ``claude`` transports in
#: :class:`precis.utils.llm.router.Transport`.
OAUTH_TRANSPORTS: tuple[str, ...] = ("claude_agent", "claude_p")

#: How long a computed :class:`BudgetStatus` is reused before re-querying.
#: Bounds per-dispatch DB overhead; short enough that a trip is seen promptly.
_CACHE_TTL_S = 15.0

_HOUR_S = 3600
_DAY_S = 86400

_lock = threading.Lock()
_cached: tuple[float, BudgetStatus] | None = None


def bind_store(store: Store | None) -> None:
    """Bind (or clear) the process store the meter queries. Also drops any
    cached status so a rebind takes effect immediately."""
    global _STORE, _cached
    _STORE = store
    _cached = None


def active_store() -> Store | None:
    """The process-bound store, if any — the breaker's fallback when a caller
    doesn't pass one explicitly."""
    return _STORE


def _cap(env_var: str, default: float) -> float:
    raw = os.environ.get(env_var)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """Rolling spend vs caps for the hourly + 24h windows."""

    hourly_spent: float
    daily_spent: float
    hourly_cap: float
    daily_cap: float

    @property
    def tripped(self) -> bool:
        """True when either window is at or over its cap."""
        return (
            self.hourly_spent >= self.hourly_cap or self.daily_spent >= self.daily_cap
        )

    @property
    def tripped_window(self) -> str | None:
        """``'hourly'`` / ``'daily'`` (hourly wins ties) — the tripped window,
        or ``None`` when under both caps."""
        if self.hourly_spent >= self.hourly_cap:
            return "hourly"
        if self.daily_spent >= self.daily_cap:
            return "daily"
        return None


def spent_usd(store: Store, *, since_seconds: int) -> float:
    """Total recorded **real-money** USD spend over the trailing window.

    Union of ``llm_call_log`` (router LLMs) and ``cache_state`` (paid fetches);
    ``ref_events`` is excluded to avoid double-counting agentic calls, and the
    notional :data:`OAUTH_TRANSPORTS` rows (claude subscription, priced but not
    billed) are excluded so the dollar cap reflects money actually spent. A
    ``NULL``-transport row is kept (unknown → counted, conservative for a
    catastrophe rail).
    """
    interval = f"{int(since_seconds)} seconds"
    with store.pool.connection() as conn:
        llm = conn.execute(
            "SELECT COALESCE(sum(cost_usd), 0)::float FROM llm_call_log "
            "WHERE cost_usd IS NOT NULL AND ts > now() - %s::interval "
            "AND (transport IS NULL OR transport <> ALL(%s))",
            (interval, list(OAUTH_TRANSPORTS)),
        ).fetchone()
        fetch = conn.execute(
            "SELECT COALESCE(sum(cost_usd), 0)::float FROM cache_state "
            "WHERE cost_usd IS NOT NULL AND cost_usd > 0 "
            "AND fetched_at > now() - %s::interval",
            (interval,),
        ).fetchone()
    return float(llm[0] if llm else 0.0) + float(fetch[0] if fetch else 0.0)


def _resolve_cap(store: Store, key: str, env_var: str, default: float) -> float:
    """Cap resolution: DB override (web-set) → env default → compiled default."""
    from precis.budget.settings import get_float

    override = get_float(store, key)
    return override if override is not None else _cap(env_var, default)


def _compute_status(store: Store) -> BudgetStatus:
    from precis.budget import settings as _s

    return BudgetStatus(
        hourly_spent=spent_usd(store, since_seconds=_HOUR_S),
        daily_spent=spent_usd(store, since_seconds=_DAY_S),
        hourly_cap=_resolve_cap(
            store, _s.HOURLY_KEY, "PRECIS_BUDGET_HOURLY_USD", DEFAULT_HOURLY_USD
        ),
        daily_cap=_resolve_cap(
            store, _s.DAILY_KEY, "PRECIS_BUDGET_DAILY_USD", DEFAULT_DAILY_USD
        ),
    )


def current_status(
    store: Store | None = None, *, use_cache: bool = True
) -> BudgetStatus | None:
    """The current rolling status, or ``None`` when no store is available.

    ``store`` defaults to the process-bound one. Results are memoised for
    :data:`_CACHE_TTL_S` seconds (skip with ``use_cache=False`` — the web tote
    wants a live read). Any query failure degrades to ``None`` so a metering
    problem can never break dispatch.
    """
    st = store if store is not None else _STORE
    if st is None:
        return None
    global _cached
    if use_cache:
        with _lock:
            if _cached is not None and (time.monotonic() - _cached[0]) < _CACHE_TTL_S:
                return _cached[1]
    try:
        status = _compute_status(st)
    except Exception:
        return None
    if use_cache:
        with _lock:
            _cached = (time.monotonic(), status)
    return status


__all__ = [
    "DEFAULT_DAILY_USD",
    "DEFAULT_HOURLY_USD",
    "OAUTH_TRANSPORTS",
    "BudgetStatus",
    "bind_store",
    "current_status",
    "spent_usd",
]
