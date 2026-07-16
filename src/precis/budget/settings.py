"""Web-editable runtime overrides for the budget caps (``app_settings``).

The caps have a three-tier resolution: a DB row (set from the /budget page)
overrides the ``PRECIS_BUDGET_*`` env default, which overrides the compiled
default. This module is the DB tier — a thin, defensive wrapper over the
generic ``app_settings`` key/value table (migration 0067).

Every read/write is best-effort: if the table is missing (un-migrated DB) or
the query fails, reads return ``None`` (caller falls back to env) and writes
raise a clean :class:`ValueError` the route surfaces. The meter reads these on
each status recompute (cached ~15s), so there's no hot-path cost.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

HOURLY_KEY = "budget.hourly_usd"
DAILY_KEY = "budget.daily_usd"


def get_setting(store: Store, key: str) -> str | None:
    """Read one ``app_settings`` value, or ``None`` if absent / unavailable."""
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = %s", (key,)
            ).fetchone()
    except Exception:
        log.debug(
            "app_settings read failed for %s (table missing?)", key, exc_info=True
        )
        return None
    return str(row[0]) if row else None


def get_float(store: Store | None, key: str) -> float | None:
    """Read a setting as a positive float, or ``None`` when unset / invalid."""
    if store is None:
        return None
    raw = get_setting(store, key)
    if raw is None:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return val if val > 0 else None


def set_float(store: Store, key: str, value: float) -> None:
    """Upsert a positive float setting. Raises ``ValueError`` on a bad value."""
    if value <= 0:
        raise ValueError("value must be positive")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = now()",
            (key, repr(float(value))),
        )


def clear_setting(store: Store, key: str) -> None:
    """Delete one setting (revert to the env / compiled default)."""
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = %s", (key,))


__all__ = [
    "DAILY_KEY",
    "HOURLY_KEY",
    "clear_setting",
    "get_float",
    "get_setting",
    "set_float",
]
