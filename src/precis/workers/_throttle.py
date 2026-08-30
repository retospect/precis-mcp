"""Shared ``PRECIS_<X>_REFRESH_HOURS`` throttle idiom for demand-driven passes.

Six workers (``openalex_enrich``, ``llm_reconcile``, ``paper_reconcile``,
``backlog_groom``, ``paper_meta_enrich``, ``corpus_reconcile``) each gate a
pass to "once per N hours" by comparing an ``app_state`` ISO-8601 timestamp
against ``now()``. This module is the one implementation; callers supply
their own env var name, default, and state key.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from precis.store import Store


def refresh_hours(env_var: str, default_hours: float) -> float:
    """Read ``env_var`` as a float number of hours, floored at 0.1.

    Unset or unparsable -> ``default_hours``.
    """
    raw = os.environ.get(env_var)
    if not raw:
        return default_hours
    try:
        return max(0.1, float(raw))
    except ValueError:
        return default_hours


def due(store: Store, state_key: str, env_var: str, default_hours: float) -> bool:
    """True once ``refresh_hours(env_var, default_hours)`` has elapsed since
    ``store.get_setting(state_key)``.

    No setting yet, or a value that doesn't parse as ISO-8601 -> True (run
    now). Caller is responsible for writing ``state_key`` back via
    ``store.set_setting(state_key, datetime.now(UTC).isoformat())`` once the
    pass completes.
    """
    last = store.get_setting(state_key)
    if not last:
        return True
    try:
        last_ts = datetime.fromisoformat(last)
    except ValueError:
        return True
    hours = refresh_hours(env_var, default_hours)
    return datetime.now(UTC) - last_ts >= timedelta(hours=hours)


__all__ = ["due", "refresh_hours"]
