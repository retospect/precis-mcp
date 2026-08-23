"""Shared relative / absolute timestamp formatting for the web UI.

Single-sourced so the route code (Status page sections) and the
templates (via the ``ago`` Jinja filter) render time the same way.
Every helper tolerates a ``datetime`` *or* an ISO-8601 string (some
store methods — ``stub_backlog`` — stringify timestamps before they
reach the view; job ``meta.started_at`` is stamped as text) *or*
``None`` / empty, returning ``""`` (or ``None``) when there's nothing
to show.

Two vocabularies, deliberately kept apart:

* :func:`ago` / :func:`relative` — "how long ago", rounded to one
  unit, for a timestamp.
* :func:`duration` / :func:`span_seconds` — an elapsed *span* between
  two timestamps, to two units, for a run time the operator compares
  against a timeout.
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


def duration(secs: float | None) -> str:
    """Two-unit elapsed label for a span of seconds ('12s', '4m18s', '2h05m').

    Distinct from :func:`_bucket`, which deliberately rounds to ONE unit
    for "how long ago" — a span the operator is comparing against a
    timeout (a job's run time vs the sweeper's stuck threshold) wants the
    minutes, not "2h". Negative / ``None`` reads as ``""`` so a caller
    with a missing endpoint can interpolate it unguarded.
    """
    if secs is None or secs < 0:
        return ""
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d{(secs % 86400) // 3600:02d}h"


def span_seconds(start: Any, end: Any) -> float | None:
    """Seconds between two timestamps (datetime or ISO string), or ``None``
    when either end is missing / unparseable. Negative spans (clock skew
    between the worker that stamped the start and the DB that stamped the
    end) clamp to ``0.0`` rather than rendering a nonsense backwards span.
    """
    a = _as_datetime(start)
    b = _as_datetime(end)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())
