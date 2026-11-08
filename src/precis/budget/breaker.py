"""The global circuit breaker — refuse *new paid* work over the cap.

The hard rail. Two gates, one for each spend chokepoint:

* :func:`gate_tier` — called from ``router.route`` before a provider runs.
  It gates the resource the *resolved rung* spends, not the tier band: a
  free local rung always passes; a claude-OAuth rung spends subscription
  quota (the quota snapshot and its taper, then the Claude pools' pacing
  for background work; ``SMALL`` stays ungated there); every other rung —
  cloud ``SMALL`` included (Reto 2026-10-21) — spends dollars and is gated
  on the rolling meter.
* :func:`gate_paid` — called from the cache-backed ``_fetch`` path before a
  paid HTTP call. Any fetch with a non-zero estimated cost is gated; only
  free lookups (cache hits, zero-cost providers) always run.

Both return ``None`` to allow, or a human-readable reason string to refuse;
a refusal that knows its horizon is a :class:`GateRefusal`, a ``str`` that
also carries ``retry_at`` (pool pacing, quota pause, taper trip). Free
work is never gated. The breaker
**auto-clears**: it re-reads the rolling meter, the quota snapshot and the
pool ledger each time, so once a window ages back under its cap or a pool
resets, paid work flows again — no manual reset.

Alerts fire on the *transition* into and out of a tripped state (deduped via
``precis.alerts``), so a standing trip pages once, not every call, and routes
to Discord via the existing alert→news channel. Dark when no store is bound.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from precis.alert_ids import RULES_BY_ID
from precis.budget import meter
from precis.budget.bands import Cost, cost_from_usd, is_paid
from precis.budget.work_class import WorkClass

if TYPE_CHECKING:
    from precis.budget.meter import BudgetStatus
    from precis.store import Store
    from precis.utils.llm.accounts import Pacing
    from precis.utils.llm.router import Tier

log = logging.getLogger(__name__)


class GateRefusal(str):
    """A refusal reason that also says when to retry.

    Still a ``str``, so every caller that reads :func:`gate_tier`'s reason
    keeps working; ``retry_at`` is when the refusing resource opens again
    (``None`` when the gate cannot tell): the pool's ``retry_at`` for a
    pacing refusal, the blocking window's own ``resets_at`` for a quota
    pause or taper trip (gr592789). The router copies it onto the paused
    result.
    """

    retry_at: datetime | None

    def __new__(cls, reason: str, retry_at: datetime | None = None) -> GateRefusal:
        refusal = super().__new__(cls, reason)
        refusal.retry_at = retry_at
        return refusal


_ALERT_SOURCE = RULES_BY_ID["budget/cap-{window}"].rule
_ALERT_SOURCE_QUOTA = RULES_BY_ID["budget:quota/quota-{window}"].rule

_alert_lock = threading.Lock()
#: A standing trip re-raises its alert this often (seconds) so the alert's
#: ``updated_at`` stays fresh and the quiet-window auto-resolve
#: (:func:`precis.alerts.resolve_quiet_alerts`, gr500373) can tell a live
#: trip from a stale one. Must stay well under ``QUIET_RESOLVE_BUDGET_S``.
_ALERT_REFRESH_S = 600
_last_refresh = 0.0
#: Refresh clock; a module attribute so tests can drive it. Monotonic time
#: counts from host boot, so a test cannot fake "refresh due" by zeroing
#: ``_last_refresh`` (on a CI runner up <10 min, 0.0 is still "recent").
_clock = time.monotonic
#: Last tripped window we alerted on (``'hourly'`` / ``'daily'`` / ``None``),
#: so we only touch the alert store on a state *transition*.
_last_window: str | None = None
#: Same, for the claude-OAuth quota gate (the window name, or ``None``).
_last_quota_window: str | None = None


def gate_tier(
    tier: Tier,
    *,
    transport: str | None = None,
    local: bool = False,
    bare: bool = False,
    store: Store | None = None,
    work_class: str | None = None,
    tally: bool = False,
    degraded: bool = False,
) -> str | None:
    """Gate an LLM dispatch. ``None`` to allow; a reason string to refuse.

    Free local work always passes. Anything else is gated on the resource
    the resolved rung actually spends, keyed by ``transport`` — not on the
    tier band:

    * the ``claude -p`` OAuth transports (:data:`meter.OAUTH_TRANSPORTS`) spend
      *subscription quota*, not money — gated on the quota snapshot and its
      taper (:mod:`precis.budget.quota`), then, for background work, on the
      Claude pools' pacing (:func:`_gate_pools`: an ``ahead`` or
      ``exhausted`` :data:`~precis.utils.llm.accounts.CLAUDE_SUB_POOLS` pool
      refuses with a :class:`GateRefusal` carrying its ``retry_at``). The
      free-band ``SMALL`` tier (:func:`~precis.budget.bands.is_paid`) is
      never gated on this lane;
    * every other transport (OpenRouter / OpenAI-compatible, paid fetches)
      spends real dollars — gated on the rolling dollar meter, ``SMALL``
      included: a tripped dollar cap pauses cloud ``SMALL`` too (Reto
      2026-10-21).

    ``local=True`` means this call *resolved* to a free local slot (a
    ``served_by`` llama-swap slot / the litellm loopback / a ``placement:
    "local"`` chain rung — the caller classifies it: the router via
    :func:`~precis.utils.llm.router._rung_is_cloud`, the policy ranker via the
    card's ``served_by``). Free local work spends neither dollars nor quota, so
    it is **never** gated — even when a paid cap is tripped. This is the
    local-first invariant: a tripped $ cap must not starve the free local rung
    a paid tier failed over to. Gating on the *resolved transport*, not the
    tier band, is what keeps a paid-band tier (BIG/MEDIUM) flowing on local
    and what stops a free-band tier on a paid cloud rung.

    ``bare=True`` means the resolved rung opted into ``--bare`` API-key auth
    (``Rung.bare``): it is still a ``claude_p``/``claude_agent`` *transport*,
    but it spends **real dollars**, not subscription quota — so it takes the
    dollar gate, not the quota gate. Keying on the transport alone would gate
    a per-token-billed call against the quota snapshot: a false refusal when
    quota is spent, and — the dangerous direction — a tripped dollar cap that
    fails to stop the one call shape the operator flipped *to* per-token
    billing. ⚠ The gate is correct; the *meter* is not yet: ``meter.spent_usd``
    still excludes every ``claude_p``/``claude_agent`` row as notional, so bare
    spend does not itself accumulate toward the cap it is now gated on —
    ``docs/backlog/bare-rung-billing-accounting.md``.

    ``transport=None`` (legacy callers, tests) falls back to the dollar gate,
    preserving the pre-split behaviour.

    ``work_class`` (:mod:`precis.budget.work_class`; ``None`` → the ambient
    scope → background) picks the quota-taper floors and decides whether
    pool pacing applies (background only). ``tally=True`` (the
    router's dispatch gates only — not selection probes like the policy
    ranker's eligibility filter) counts the paid decision per class as
    ``served`` or ``paused`` for /budget (:mod:`precis.budget.outcomes`);
    ``degraded=True`` (the request carries ``degraded_from`` — a caller's
    re-ask on a smaller tier after a pause) also counts ``degraded`` when
    admitted.
    """
    if local:
        return None
    oauth = transport in meter.OAUTH_TRANSPORTS and not bare
    if oauth and not is_paid(tier):
        return None
    from precis.budget import outcomes
    from precis.budget.work_class import resolve
    from precis.utils.llm.accounts import CLAUDE_SUB_POOLS

    cls = resolve(work_class)
    st = store if store is not None else meter.active_store()
    reason: str | None
    if oauth:
        reason = _gate_quota(st, tier, cls)
        if reason is None:
            reason = _gate_pools(st, CLAUDE_SUB_POOLS, cls)
    else:
        reason = _gate(st, label="paid model call")
    if tally:
        outcomes.record(st, cls, "paused" if reason is not None else "served")
        if degraded and reason is None:
            outcomes.record(st, cls, "degraded")
    return reason


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


def _taper_reason(store: Store | None, tier: Tier, work_class: WorkClass) -> str | None:
    from precis.budget import quota

    taper = quota.taper_state(store)
    name = str(getattr(tier, "value", tier)).lower()
    if name not in taper.blocked(work_class):
        if taper.no_signal:
            log.debug("quota taper: no signal in snapshot; failing open")
        return None
    # GateRefusal carries the window's own reset so a parked job wakes at
    # the real horizon, not the class default (gr592789).
    return GateRefusal(
        f"budget: claude quota low ({taper.why(work_class)}) "
        f"— {name} {work_class.value} work paused. Smaller tiers and free local "
        f"work still run; auto-clears when the window resets, or resume now on "
        f"/budget.",
        retry_at=taper.resets_at,
    )


def _gate_quota(
    store: Store | None,
    tier: Tier | None = None,
    work_class: WorkClass = WorkClass.BACKGROUND,
) -> str | None:
    """Gate the claude-OAuth lane on the subscription quota snapshot.

    Parks every paid tier and class on an exhausted window; otherwise tapers
    by (work class, tier) as remaining headroom shrinks
    (:data:`~precis.budget.quota.TAPER_FLOORS`)."""
    from precis.budget import quota

    pause = quota.evaluate(store)
    window = pause.window if pause is not None else None
    resumed = _resume_active(store)
    if window is not None and resumed:
        window, pause = None, None  # operator override
    # Sync first so a cleared park resolves its alert even on a taper return.
    _sync_quota_alert(store, window, pause)
    if pause is not None:
        # GateRefusal carries the window's own reset instant — the router
        # stamps it as ``retry_at`` so a parked job re-queues AT the reset
        # instead of re-parking every two hours until its park cap fails
        # it on a long window (gr592789).
        return GateRefusal(pause.reason, retry_at=pause.resets_at)
    if tier is not None and not resumed:
        return _taper_reason(store, tier, work_class)
    return None


def _gate_pools(
    store: Store | None, keys: tuple[str, ...], work_class: WorkClass
) -> GateRefusal | None:
    """Pace background work on the pool ledger (memory me568188).

    Deliverable and interactive work return before any read: they continue
    under the quota taper alone. The resume override admits too. Background
    work is refused while any pool in ``keys`` reads ``ahead`` or
    ``exhausted`` (:func:`~precis.utils.llm.accounts.pacing`); the refusal
    carries the latest refusing pool's ``retry_at``. Dark without a store;
    a store with no ledger (a fake with no ``.pool``/``fetchall``) fails
    open quietly, and any other failed read warns and fails open."""
    if work_class is not WorkClass.BACKGROUND or store is None:
        return None
    if _resume_active(store):
        return None
    from precis.utils.llm.accounts import list_pools, pacing

    try:
        pools = {pool.provider: pool for pool in list_pools(store)}
    except (AttributeError, TypeError) as exc:
        # A store with no ``.pool`` / ``fetchall`` (a test fake): quiet.
        log.debug("budget breaker: store has no pool ledger (%s); not pacing", exc)
        return None
    except Exception:
        log.warning("budget breaker: pool ledger unreadable; not pacing", exc_info=True)
        return None
    now = datetime.now(UTC)
    refusing: list[tuple[str, Pacing]] = []
    for key in keys:
        pool = pools.get(key)
        if pool is None:
            continue
        pace = pacing(pool, now)
        if pace.state in ("ahead", "exhausted"):
            refusing.append((key, pace))
    if not refusing:
        return None
    key, pace = max(refusing, key=lambda item: item[1].retry_at or now)
    used = "unknown" if pace.fraction is None else f"{pace.fraction:.1%}"
    until = (
        pace.retry_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")
        if pace.retry_at is not None
        else "the pool resets"
    )
    return GateRefusal(
        f"budget: claude pool {key} is {pace.state} ({used} used, target "
        f"{pace.target:.0%}) — background work paused until {until}. "
        "Deliverable and interactive work still run; resume now on /budget.",
        retry_at=pace.retry_at,
    )


def _sync_alert(
    store: Store | None, window: str | None, status: BudgetStatus | None
) -> None:
    """Raise / resolve the budget alert on a tripped-state transition.

    Best-effort: never raises. Touches the store only when the tripped window
    changes, so a standing trip doesn't spam and a cleared trip resolves once.
    """
    if store is None:
        return
    global _last_window, _last_refresh
    with _alert_lock:
        now = _clock()
        if window == _last_window:
            if window is None or now - _last_refresh < _ALERT_REFRESH_S:
                return
            # Standing trip: refresh liveness (re-raises if auto-resolved).
        _last_refresh = now
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
    global _last_window, _last_quota_window, _last_refresh
    with _alert_lock:
        _last_refresh = 0.0
        _last_window = None
        _last_quota_window = None


__all__: list[str] = ["GateRefusal", "gate_paid", "gate_tier"]
