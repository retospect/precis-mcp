"""``<N>m`` / ``<N>h`` / ``<N>d`` / ``<N>w`` relative spans — the "now minus
that long" spelling ``since=``/``until=`` accept next to an ISO timestamp.

One parser shared by the source-search primitive and the job handler's
window bound, so ``since='2h'`` means the same on every path a job search
can take (gr735229).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

#: ``30m`` / ``2h`` / ``3d`` / ``1w`` (decimals allowed: ``1.5h``). The unit
#: is required — a bare number is the caller's ambiguity to resolve.
_SPAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([mhdw])", re.IGNORECASE)
_SPAN_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}

#: What the BadInput texts name when a bound is neither an ISO value nor a span.
SPAN_FORMS = "an ISO date/timestamp or a span like 30m, 2h, 3d"


def parse_span(text: str) -> timedelta | None:
    """``'2h'`` → ``timedelta(hours=2)``; ``None`` when ``text`` is not a
    span (an ISO value, a bare number, anything else) or is one too long
    to represent (``'99999999999w'``) — the caller's refusal then names
    the accepted forms instead of an overflow escaping."""
    m = _SPAN_RE.fullmatch(text.strip())
    if m is None:
        return None
    try:
        return timedelta(**{_SPAN_UNITS[m.group(2).lower()]: float(m.group(1))})
    except OverflowError:
        return None


def span_ago(text: str, now: datetime | None = None) -> datetime | None:
    """The instant ``text`` (a span) before ``now`` (default: current UTC),
    or ``None`` when ``text`` is not a span or reaches before year 1
    (``'9999999d'``)."""
    span = parse_span(text)
    if span is None:
        return None
    try:
        return (now or datetime.now(UTC)) - span
    except OverflowError:
        return None
