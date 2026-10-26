"""Opt-in local attempt, acceptance check, then a metered paid escalation.

No call site is switched here. Off (including master-off) passes the original
request straight through; shadow stays on that paid path until S2 supplies its
paired comparison. Acceptance callbacks stay outside serialized requests.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from precis import settings
from precis.budget import meter
from precis.utils.llm import router
from precis.utils.llm.operations import LOCAL_FIRST_OPS, LOCAL_FIRST_SETS
from precis.utils.llm.router import LlmRequest, LlmResult

log = logging.getLogger(__name__)

AcceptFn = Callable[[LlmResult], str | None]
CompareFn = Callable[[LlmResult, LlmResult], tuple[bool, Any]]
_REASONS = frozenset(
    {
        "parse",
        "truncated",
        "timeout",
        "refusal",
        "low_confidence",
        "validator",
        "saturated",
        "transport",
        "no_local_rung",
    }
)
_SKIP_SECONDS = 600.0
_FAILURE_LIMIT = 5


@dataclass(frozen=True)
class LocalFirstResult:
    """Chosen answer, optional local attempt, and why it was escalated/parked.

    ``escalated='budget'`` means no paid call ran; the local result remains
    available for the caller's fallback. ``None`` means the chosen answer
    needed no escalation (or the helper was off).
    ``deferred`` names an op-policy park reason: ``result.paused`` is true,
    ``local`` retains any attempted answer, and no paid call ran.
    """

    result: LlmResult
    local: LlmResult | None = None
    escalated: str | None = None
    deferred: str | None = None


@dataclass
class _Circuit:
    failures: int = 0
    skip_until: float = 0.0
    reason: str = "transport"


_state = _Circuit()
_lock = threading.Lock()
_slots = {
    op: threading.BoundedSemaphore(cfg.cap) for op, cfg in LOCAL_FIRST_OPS.items()
}
_paid_locks = {op: threading.Lock() for op in LOCAL_FIRST_OPS}
_warned_missing: set[str] = set()


def _spent_usd(op: str) -> float:
    """Recorded escalation cost today, including quota-priced paid calls.

    Missing accounting fails closed. This is deliberately not meter.spent_usd:
    it includes every escalated cost_usd, and uses a UTC day, not trailing 24h.
    """
    store = meter.active_store()
    if store is None:
        return math.inf
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(sum(cost_usd), 0)::float FROM llm_call_log "
                "WHERE source = %s AND ts >= %s AND ts < %s "
                "AND features->'local_first'->>'arm' = 'paid' "
                "AND features->'local_first'->>'escalated' IS NOT NULL",
                (op, start, start + timedelta(days=1)),
            ).fetchone()
        return float(row[0]) if row else 0.0
    except Exception:
        log.warning(
            "local-first %s: escalation accounting unavailable", op, exc_info=True
        )
        return math.inf


def _features(
    req: LlmRequest, *, arm: str, reason: str | None = None
) -> dict[str, Any]:
    stamp: dict[str, Any] = {"mode": "on", "arm": arm}
    if reason is not None:
        stamp["escalated"] = reason
    return {**(req.features or {}), "local_first": stamp}


def _failure(result: LlmResult) -> str | None:
    # The local completion adapter classifies wire timeouts as transport but
    # does not set timed_out (the agent adapters do). Preserve D3's distinction.
    error = (result.error or "").lower()
    if result.timed_out or "timed out" in error or "timeout" in error:
        return "timeout"
    if result.stop_reason in {"length", "max_tokens"}:
        return "truncated"
    if result.reason_class == "content":
        return "refusal"
    if result.error and (
        result.error.startswith("placement_refusal")
        or result.error.startswith("placement=")
    ):
        return "no_local_rung"
    if result.reason_class == "transport":
        return "transport"
    if result.paused:
        return "saturated"
    if result.error:
        return "transport"
    return None


def _skip_reason() -> str | None:
    with _lock:
        if _state.skip_until:
            if time.monotonic() < _state.skip_until:
                return _state.reason
            _state.skip_until = 0.0
            _state.failures = 0
        return None


def _observe(reason: str | None) -> None:
    with _lock:
        # In-flight completions cannot undo or extend an already-open circuit.
        if _state.skip_until:
            return
        if reason not in {"timeout", "transport"}:
            _state.failures = 0
            return
        _state.failures += 1
        _state.reason = reason
        if _state.failures >= _FAILURE_LIMIT:
            _state.skip_until = time.monotonic() + _SKIP_SECONDS
            log.warning(
                "local-first: skipping local attempts for 10 minutes after %s", reason
            )


def _paused(req: LlmRequest, reason: str) -> LlmResult:
    return LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model=req.model or "",
        tier=req.tier,
        error=f"local-first: {reason}",
        paused=True,
        placement="local",
        reason_class="rate" if reason == "saturated" else "transport",
        timed_out=reason == "timeout",
    )


def run(
    req: LlmRequest,
    *,
    op: str,
    accept: AcceptFn,
    compare: CompareFn | None = None,
) -> LocalFirstResult:
    """Try the registered local arm and escalate a rejected answer once.

    Each op's ``park`` reasons defer without paid dispatch; live consumers
    arrive in S3–S5. ``compare`` is reserved for S2; shadow currently returns
    only today's paid answer. Invalid modes fail dark, like ``off``.
    """
    cfg = LOCAL_FIRST_OPS[op]
    if not settings.get_bool("llm.local_first.enabled") or not any(
        settings.get_str(f"llm.local_first.set.{name}.mode") == "on"
        for name, members in LOCAL_FIRST_SETS.items()
        if op in members
    ):
        return LocalFirstResult(router.route(req))

    reason = _skip_reason()
    local: LlmResult | None = None
    if reason is None:
        slot = _slots[op]
        if not slot.acquire(blocking=False):
            reason = "saturated"
        else:
            try:
                local = router.route(
                    replace(
                        req,
                        source=f"{op}:local",
                        placement="local",
                        max_tokens=None,
                        features=_features(req, arm="local"),
                        log_call=True,
                    )
                )
            finally:
                slot.release()
            reason = _failure(local)
            _observe(reason)
            if reason is None:
                reason = accept(local)
                if reason is not None and reason not in _REASONS:
                    raise ValueError(
                        f"Unknown local-first acceptance reason: {reason!r}"
                    )
            if reason is None:
                return LocalFirstResult(local, local=local)

    if reason == "no_local_rung":
        with _lock:
            if op not in _warned_missing:
                _warned_missing.add(op)
                log.warning("local-first %s: no local op rung configured", op)

    if reason in cfg.park:
        return LocalFirstResult(_paused(req, reason), local=local, deferred=reason)

    # Hold the process lock through dispatch, so another caller observes this
    # call's logged cost before checking its budget. Across hosts the ledger
    # remains authoritative; this is an admission check, not a cost reservation.
    with _paid_locks[op]:
        budget = settings.get_float(f"llm.local_first.{cfg.name}.escalate_usd_day")
        spent = _spent_usd(cfg.paid_source)
        if budget is None or not math.isfinite(budget) or not spent < budget:
            return LocalFirstResult(
                local or _paused(req, reason), local=local, escalated="budget"
            )
        paid = router.route(
            replace(
                req,
                source=cfg.paid_source,
                features=_features(req, arm="paid", reason=reason),
                log_call=True,
            )
        )
    return LocalFirstResult(paid, local=local, escalated=reason)
