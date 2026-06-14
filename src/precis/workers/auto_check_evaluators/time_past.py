"""``type='time_past'`` — scheduled wake at a specific ISO timestamp.

Resolves to ``True`` when ``now() >= spec['at']``. Useful for
deferring a leaf until a specific date ("revisit this Tuesday").
The 60-second poll cadence means resolution is within a minute of
the scheduled time — fine for the human-scale use cases we have.

Spec
====

```json
{
  "type": "time_past",
  "at": "2026-07-01T09:00:00+00:00"
}
```

The string is parsed by :func:`datetime.datetime.fromisoformat`,
so both naive (``YYYY-MM-DDTHH:MM:SS``) and tz-aware shapes work.
Naive timestamps are interpreted as UTC for the comparison.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput

if TYPE_CHECKING:
    from precis.store import Store


def evaluate(store: Store, spec: dict[str, Any], **_kw: Any) -> bool | None:
    raw = spec.get("at")
    if not isinstance(raw, str) or not raw.strip():
        raise BadInput(
            "time_past needs an 'at' ISO timestamp",
            next="meta.auto_check.at='2026-07-01T09:00:00+00:00'",
        )
    try:
        at = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BadInput(
            f"time_past.at is not parseable: {exc}",
            next="meta.auto_check.at='YYYY-MM-DDTHH:MM:SS+00:00' (ISO 8601)",
        ) from exc
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    # The reference clock is the DB's ``now()`` so a misaligned
    # worker host (DST surprises, NTP drift) doesn't fire the leaf
    # early. One round-trip is plenty cheap compared to the
    # surrounding LLM-driven path.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT now() >= %s::timestamptz",
            (at.isoformat(),),
        ).fetchone()
    if row is None:
        return False
    return bool(row[0])
