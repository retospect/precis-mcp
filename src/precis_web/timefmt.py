"""Shared relative / absolute timestamp formatting for the web UI.

Single-sourced so the route code (Status page sections) and the
templates (via the ``ago`` Jinja filter) render time the same way.
Both helpers tolerate a ``datetime`` *or* an ISO-8601 string (some
store methods — ``stub_backlog`` — stringify timestamps before they
reach the view) *or* ``None`` / empty, returning ``""`` when there's
nothing to show.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _as_datetime(value: Any) -> datetime | None:
    """Coerce a datetime / ISO string into a tz-aware datetime, or None."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def age_seconds(value: Any) -> float | None:
    """Seconds since ``value`` (datetime or ISO string), or ``None``."""
    dt = _as_datetime(value)
    if dt is None:
        return None
    return (datetime.now(UTC) - dt).total_seconds()


def _bucket(secs: float) -> str:
    """Compact magnitude label for a non-negative second count ('4m', '2h')."""
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs / 60)}m"
    if secs < 172800:
        return f"{int(secs / 3600)}h"
    return f"{int(secs / 86400)}d"


def ago(value: Any) -> str:
    """Compact relative-time string ('3s ago', '4m ago', '2h ago', '5d ago').

    Accepts a datetime or an ISO-8601 string; returns ``""`` for
    anything unparseable so a template can ``{{ ts | ago }}`` without
    guarding the empty case. A future ``value`` clamps to ``"0s ago"`` —
    use :func:`relative` when the sign matters (e.g. a lease expiry).
    """
    secs = age_seconds(value)
    if secs is None:
        return ""
    return f"{_bucket(max(0.0, secs))} ago"


def relative(value: Any) -> str:
    """Signed relative-time string: ``"in 12m"`` (future) or ``"3m ago"``
    (past) — same bucket thresholds as :func:`ago`, but for values where the
    direction matters, like a lease expiry that can be either side of now.
    """
    secs = age_seconds(value)
    if secs is None:
        return ""
    if secs <= 0:
        return f"in {_bucket(-secs)}"
    return f"{_bucket(secs)} ago"


def abs_ts(value: Any) -> str:
    """Absolute ``YYYY-MM-DD HH:MM`` (UTC) for hover tooltips, or ``""``."""
    dt = _as_datetime(value)
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
