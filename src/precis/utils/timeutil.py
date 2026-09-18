"""Coerce whatever a boundary hands you into a tz-aware UTC datetime.

Timestamps reach this codebase in three shapes: a tz-aware ``datetime`` from
psycopg (every column is ``timestamptz``), a *naive* one from a value that
round-tripped through a string, and an ISO-8601 string that may or may not
carry an offset (``meta.started_at`` is stamped as text; ``Retry-After``-style
API headers arrive with one).

Mixing them bites twice. ``datetime.now(UTC) - naive`` raises ``TypeError``,
which call sites that only catch ``ValueError`` around the parse turn into an
uncaught crash; and formatting an offset-bearing value under a hardcoded
``"UTC"`` label prints the wrong hour while claiming otherwise.

So: coerce once, at the boundary. Naive is *assumed* UTC — everything this
system writes is UTC (``docs/conventions/time.md``), so a naive value is a
UTC value that lost its label, not a local one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def as_utc(value: Any) -> datetime | None:
    """A tz-aware UTC datetime from a datetime / ISO string, else ``None``.

    ``None``, empty strings and unparseable strings all return ``None`` rather
    than raising: these are display and age-check paths, where an unreadable
    stamp should degrade to "unknown", not abort the request.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
