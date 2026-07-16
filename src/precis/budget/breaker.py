"""The global circuit breaker — refuse *new paid* work over the cap.

The hard rail. Two gates, one for each spend chokepoint:

* :func:`gate_tier` — called from ``router.dispatch`` before a provider runs.
  Any *paid* tier (non-``free`` band — cheap ``CLOUD_MID``/``CLOUD_SMALL`` as
  well as expensive ``CLOUD_SUPER``) is gated; if it costs money, the cap
  limits it. Only free local tiers pass untouched.
* :func:`gate_paid` — called from the cache-backed ``_fetch`` path before a
  paid HTTP call. Any fetch with a non-zero estimated cost is gated; only
  free lookups (cache hits, zero-cost providers) always run.

Both return ``None`` to allow, or a human-readable reason string to refuse.
Free work is never gated. The breaker **auto-clears**: it re-reads the rolling
meter each time, so once the window ages the spend back under the cap, paid
work flows again — no manual reset.

Alerts fire on the *transition* into and out of a tripped state (deduped via
``precis.alerts``), so a standing trip pages once, not every call, and routes
to Discord via the existing alert→news channel. Dark when no store is bound.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from precis.budget import meter
from precis.budget.bands import Cost, cost_from_usd, is_paid

if TYPE_CHECKING:
    from precis.budget.meter import BudgetStatus
    from precis.store import Store
    from precis.utils.llm.router import Tier

log = logging.getLogger(__name__)

_ALERT_SOURCE = "budget"
_ALERT_SOURCE_QUOTA = "budget:quota"

_alert_lock = threading.Lock()
#: Last tripped window we alerted on (``'hourly'`` / ``'daily'`` / ``None``),
#: so we only touch the alert store on a state *transition*.
_last_window: str | None = None
#: Same, for the claude-OAuth quota gate (the window name, or ``None``).
_last_quota_window: str | None = None


def gate_tier(
    tier: Tier,
    *,
    transport: str | None = None,
    store: Store | None = None,
) -> str | None:
    """Gate an LLM dispatch. ``None`` to allow; a reason string to refuse.

    Free local tiers always pass. A *paid* tier is gated on the resource it
    actually spends, keyed by ``transport``:

    * the ``claude -p`` OAuth transports (:data:`meter.OAUTH_TRANSPORTS`) spend
      *subscription quota*, not money — gated on the quota snapshot
      (:mod:`precis.budget.quota`);
    * every other paid transport (OpenRouter / OpenAI-compatible, paid fetches)
      spends real dollars — gated on the rolling dollar meter.

    ``transport=None`` (legacy callers, tests) falls back to the dollar gate,
    preserving the pre-split behaviour.
    """
    if not is_paid(tier):
        return None
    st = store if store is not None else meter.active_store()
    if transport in meter.OAUTH_TRANSPORTS:
        return _gate_quota(st)
    return _gate(st, label="paid model call")


def gate_paid(
    expected_cost_usd: float | None, *, store: Store | None = None
) -> str | None:
    """Gate a paid fetch by its *estimated* per-call cost. ``None`` to allow.

    Any fetch with a non-zero estimated cost is gated once a cap trips; only
    free lookups (cache hits, zero-cost providers) always run.
    """
    if cost_from_usd(expected_cost_usd) is Cost.FREE:
        return None
    return _gate(store, label="paid fetch")


def _resume_active(store: Store | None) -> bool:
    """True when the operator's "resume now" override is live (bypass a soft
    trip). Best-effort \u2014 an unreadable setting reads as *not* active."""
    from precis.budget import settings

    try:
        return settings.resume_active(store)
    except Exception:
        log.debug("budget breaker: resume-override read failed", exc_info=True)
        return False


def _gate(store: Store | None, *, label: str) -> str | None:
    st = store if store is not None else meter.active_store()
    status = meter.current_status(st)
    window = status.tripped_window if status is not None else None
    if window is not None and _resume_active(st):
        window = None  # operator override \u2014 resume paid work now
    _sync_alert(st, window, status)
    if window is None or status is None:
        return None
    if window == "hourly":
        spent, cap = status.hourly_spent, status.hourly_cap
    else:
        spent, cap = status.daily_spent, status.daily_cap
    return (
        f"budget: {window} cap ${cap:.2f} reached (${spent:.2f} spent) \u2014 "
        f"{label} paused. Free local work still runs; wait for the window to "
        f"roll off or raise the cap on /budget."
    )


def _gate_quota(store: Store | None) -> str | None:
    """Gate the claude-OAuth lane on the subscription quota snapshot."""
    from precis.budget import quota

    pause = quota.evaluate(store)
    window = pause.window if pause is not None else None
    if window is not None and _resume_active(store):
        window, pause = None, None  # operator override
    _sync_quota_alert(store, window, pause)
    return pause.reason if pause is not None else None


def _sync_alert(
    store: Store | None, window: str | None, status: BudgetStatus | None
) -> None:
    """Raise / resolve the budget alert on a tripped-state transition.

    Best-effort: never raises. Touches the store only when the tripped window
    changes, so a standing trip doesn't spam and a cleared trip resolves once.
    """
    if store is None:
        return
    global _last_window
    with _alert_lock:
        if window == _last_window:
            return
        prior = _last_window
        _last_window = window
    try:
        _apply_alert(store, window, prior, status)
    except Exception:
        log.debug("budget breaker: alert sync failed", exc_info=True)


def _sync_quota_alert(store: Store | None, window: str | None, pause: object) -> None:
    """Raise / resolve the claude-quota alert on a transition (mirrors
    :func:`_sync_alert`)."""
    if store is None:
        return
    global _last_quota_window
    with _alert_lock:
        if window == _last_quota_window:
            return
        _last_quota_window = window
    try:
        _apply_quota_alert(store, window, pause)
    except Exception:
        log.debug("budget breaker: quota alert sync failed", exc_info=True)


def _apply_quota_alert(store: Store, window: str | None, pause: object) -> None:
    from precis.alerts import (
        notify_critical_alert,
        raise_alert,
        resolve_stale_alerts,
    )

    if window is None:
        resolve_stale_alerts(store, source=_ALERT_SOURCE_QUOTA, live_fingerprints=set())
        return
    reason = getattr(pause, "reason", "") or ""
    fingerprint = f"quota-{window}"
    title = f"[budget] claude subscription quota reached ({window})"
    detail = (
        reason
        or f"Paid claude work is paused: the {window} rate-limit window is "
        "exhausted. Free local work still runs; auto-clears on window reset, "
        "or resume now on /budget."
    )
    _ref, is_new = raise_alert(
        store,
        source=_ALERT_SOURCE_QUOTA,
        fingerprint=fingerprint,
        title=title,
        detail=detail,
        severity="critical",
    )
    resolve_stale_alerts(
        store, source=_ALERT_SOURCE_QUOTA, live_fingerprints={fingerprint}
    )
    if is_new:
        notify_critical_alert(store, title, detail, fingerprint=fingerprint)


def _apply_alert(
    store: Store, window: str | None, prior: str | None, status: BudgetStatus | None
) -> None:
    from precis.alerts import notify_critical_alert, raise_alert, resolve_stale_alerts

    if window is None:
        # Transitioned back under both caps — resolve any open budget alert.
        resolve_stale_alerts(store, source=_ALERT_SOURCE, live_fingerprints=set())
        if prior is not None:
            log.info("budget breaker: spend back under cap; expensive work resumed")
        return
    assert status is not None
    if window == "hourly":
        spent, cap = status.hourly_spent, status.hourly_cap
    else:
        spent, cap = status.daily_spent, status.daily_cap
    fingerprint = f"cap-{window}"
    title = f"[budget] {window} spend cap reached (${spent:.2f} / ${cap:.2f})"
    detail = (
        f"Paid autonomous work is paused: {window} spend ${spent:.2f} "
        f"has reached the ${cap:.2f} cap. Only free local calls still "
        f"run. Auto-clears as the window rolls off; raise the cap on /budget "
        f"to resume paid work now."
    )
    _ref, is_new = raise_alert(
        store,
        source=_ALERT_SOURCE,
        fingerprint=fingerprint,
        title=title,
        detail=detail,
        severity="critical",
    )
    # Keep only this window's alert live, so switching windows resolves the old.
    resolve_stale_alerts(store, source=_ALERT_SOURCE, live_fingerprints={fingerprint})
    if is_new:
        notify_critical_alert(store, title, detail, fingerprint=fingerprint)


def _reset_alert_state() -> None:
    """Test hook: forget the last-alerted windows."""
    global _last_window, _last_quota_window
    with _alert_lock:
        _last_window = None
        _last_quota_window = None


__all__: list[str] = ["gate_paid", "gate_tier"]
