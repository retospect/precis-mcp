"""Router-owned LLM failure classification — one table, every consumer.

Every production LLM call goes through :func:`precis.utils.llm.router.route`,
so the router is the one place that sees a failure with its full context
(the exception, the transport, the error text). It stamps
:attr:`~precis.utils.llm.router.LlmResult.reason_class` and
:attr:`~precis.utils.llm.router.LlmResult.retry_at` from this module; the
executor's parked-job backoff
(:func:`precis.workers.executors._common.classify_transient_backoff_hours`)
reads the same table off the captured reason *string*, so a live result
and a parked job agree on both the class and the horizon.

Classes (:data:`FailureClass`):

* ``quota`` — the Claude subscription window is exhausted
  (:mod:`precis.utils.llm.quota` wording). Horizon: the reset instant.
* ``budget`` — a spend cap: the dollar breaker, a provider credit/spend
  limit, HTTP 402. Horizon: two hours (the next window or a top-up).
* ``rate`` — a bare 429 / overloaded / 529. Horizon: fifteen minutes;
  the SMALL lane retries these in-process first.
* ``transport`` — the wire, not the model: timeouts, connection failures,
  5xx. Horizon: thirty minutes.
* ``content`` — the model ran and refused or was filtered. No horizon:
  a different rung gets the same prompt and the same answer, so a chain
  never falls through on it.

Unknown wording stays unclassified (``None``) — a false negative keeps
today's generic cool-down, a false positive would only bring one retry
forward. Patterns are matched case-insensitively against the error text;
an exception, when the caller has one, is classified structurally first
(an :class:`~urllib.error.HTTPError` status, an :class:`OSError`) because
the text of a ``urllib`` error is often just ``"HTTP Error 429"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.error import HTTPError

from precis.utils.llm.quota import quota_retry_at

FailureClass = Literal["quota", "rate", "budget", "transport", "content"]

#: Classes a failover chain may fall through on: the *rung* is the problem,
#: not the prompt. ``content`` is deliberately absent.
FALL_THROUGH_CLASSES: frozenset[str] = frozenset(
    {"quota", "rate", "budget", "transport"}
)

#: Classes the SMALL lane retries in-process (router ``route()``): short
#: horizons that a few seconds of backoff plausibly clear.
INPROCESS_RETRY_CLASSES: frozenset[str] = frozenset({"rate", "transport"})

#: Per-class fixed horizon when the text names no instant of its own.
#: ``content`` has none (retrying is pointless).
DEFAULT_HORIZON: dict[str, timedelta] = {
    "rate": timedelta(minutes=15),
    "budget": timedelta(hours=2),
    "transport": timedelta(minutes=30),
    # Only for a quota failure the router pins by construction (the gate
    # trip, whose text names no reset); a quota *notice* always parses to
    # its own instant first.
    "quota": timedelta(hours=2),
}

#: Reason-text signatures, first match wins. Order matters: a message
#: naming both a rate limit and a spend cap is retryable at the shorter
#: horizon, so ``rate`` precedes ``budget``; ``content`` comes last so a
#: refusal wording embedded in a transport error does not mask it.
_PATTERNS: tuple[tuple[FailureClass, re.Pattern[str]], ...] = (
    ("rate", re.compile(r"rate.?limit|\b429\b|overloaded|\b529\b", re.IGNORECASE)),
    (
        "budget",
        re.compile(
            r"^budget:|spend(?:ing)?\s+(?:limit|cap)"
            r"|budget\s+(?:limit|cap|exceeded)|credit balance|out of credits"
            r"|insufficient (?:credits|funds|quota)|HTTP Error 402|payment required",
            re.IGNORECASE,
        ),
    ),
    (
        "transport",
        re.compile(
            r"internal server error|service unavailable|bad gateway"
            r"|gateway time.?out|temporarily unavailable|connection reset by peer"
            r"|connection refused|connection (?:error|failed|aborted)"
            r"|\btimed? ?out\b|HTTP Error 50[0-9]|name or service not known"
            r"|remote end closed|network is unreachable|econnrefused",
            re.IGNORECASE,
        ),
    ),
    (
        "content",
        re.compile(
            r"content[_ ]policy|content[_ ]filter|content_management_policy"
            r"|\brefus(?:ed|al)\b|flagged by|moderation|safety (?:filter|system)",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Classified:
    """One classification: the class and the earliest sensible retry."""

    reason_class: FailureClass | None
    retry_at: datetime | None


UNCLASSIFIED = Classified(None, None)


def _from_exception(exc: BaseException) -> FailureClass | None:
    """Structural classification off a caught transport exception, or
    ``None`` when the exception type alone does not decide (a 4xx other
    than 402/429, a ``RuntimeError`` carrying a response body)."""
    if isinstance(exc, HTTPError):
        if exc.code == 429:
            return "rate"
        if exc.code == 402:
            return "budget"
        if exc.code >= 500:
            return "transport"
        return None
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "transport"
    return None


def classify_text(reason: str, *, now: datetime | None = None) -> Classified:
    """Classify a failure by its wording alone — the executor's view.

    The Claude quota wording is checked first (its horizon is the parsed
    reset instant, not a fixed backoff); then :data:`_PATTERNS` in order.
    """
    now = now if now is not None else datetime.now(UTC)
    if not reason:
        return UNCLASSIFIED
    quota_at = quota_retry_at(reason, now=now)
    if quota_at is not None:
        return Classified("quota", quota_at)
    for cls, pattern in _PATTERNS:
        if pattern.search(reason):
            horizon = DEFAULT_HORIZON.get(cls)
            return Classified(cls, now + horizon if horizon is not None else None)
    return UNCLASSIFIED


def classify(
    reason: str | None,
    *,
    exc: BaseException | None = None,
    timed_out: bool = False,
    now: datetime | None = None,
) -> Classified:
    """Classify a failure with everything the router knows.

    ``exc`` (the caught transport exception) decides structurally when it
    can; ``timed_out`` (a wall-clock timeout the transport flagged) is
    ``transport``; otherwise the wording decides (:func:`classify_text`).
    A quota notice in the text always wins over a structural 429 — the
    CLI reports an exhausted window as a 429 whose body names the reset.
    """
    now = now if now is not None else datetime.now(UTC)
    text = reason or ""
    quota_at = quota_retry_at(text, now=now) if text else None
    if quota_at is not None:
        return Classified("quota", quota_at)
    structural = _from_exception(exc) if exc is not None else None
    if structural is None and timed_out:
        structural = "transport"
    if structural is not None:
        return Classified(structural, now + DEFAULT_HORIZON[structural])
    return classify_text(text, now=now)


def backoff_hours(reason: str, *, now: datetime | None = None) -> float | None:
    """Hours until :func:`classify_text`'s horizon, or ``None`` when the
    wording is unclassified or has no horizon (``content``). The executor's
    parked-job cool-down reads this."""
    now = now if now is not None else datetime.now(UTC)
    found = classify_text(reason, now=now)
    if found.retry_at is None:
        return None
    return max((found.retry_at - now).total_seconds() / 3600.0, 0.0)


__all__ = [
    "DEFAULT_HORIZON",
    "FALL_THROUGH_CLASSES",
    "INPROCESS_RETRY_CLASSES",
    "UNCLASSIFIED",
    "Classified",
    "FailureClass",
    "backoff_hours",
    "classify",
    "classify_text",
]
