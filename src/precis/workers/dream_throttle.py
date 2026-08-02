"""Live cadence knob for the dream pass — ``dream.min_interval_minutes``.

Launchd stays dumb: the plist keeps firing every 15 min
(``deploy/roles/precis_dream``), unchanged. What changes is whether a given
fire actually does anything — this module lets the pass **self-throttle**,
mirroring the budget-cap pattern (:mod:`precis.budget.settings`): a web-set
``app_settings`` row (no migration needed — the table exists from migration
0070) overrides an env default, which overrides a compiled default.

Resolution order (DB > env > compiled default), same tri-tier shape as the
budget caps:

1. ``dream.min_interval_minutes`` in ``app_settings`` — set from the Budget
   sub-tab, live, no redeploy.
2. ``PRECIS_DREAM_MIN_INTERVAL_MINUTES`` env.
3. Compiled default :data:`DEFAULT_MIN_INTERVAL_MINUTES` — 15, matching the
   plist's own cadence, so an unset knob is byte-identical to today.

The pass reads its own last-*real*-run timestamp (``dream.last_real_run_at``,
written by :func:`mark_real_run` — only for a tick that actually dispatched
the LLM, not one that no-op'd on a pre-existing gate) and compares elapsed
time against the interval, minus a ~60s jitter guard (:data:`_JITTER_GUARD_S`)
so launchd's own scheduling slop around a 15-min mark never causes a spurious
skip at the default interval.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from precis.budget import settings as app_settings
from precis.store import Store

log = logging.getLogger(__name__)

#: app_settings key for the web-set cadence override.
MIN_INTERVAL_KEY = "dream.min_interval_minutes"

#: app_settings key for the pass's own last-real-run timestamp (ISO-8601 UTC).
LAST_REAL_RUN_KEY = "dream.last_real_run_at"

#: Compiled default — matches the launchd plist's 15-min fire, so an unset
#: knob changes nothing about today's cadence.
DEFAULT_MIN_INTERVAL_MINUTES = 15.0

#: Slack subtracted from the interval before comparing elapsed time. Launchd
#: fires with its own jitter around the :StartCalendarInterval: marks, so at
#: the (byte-identical) default interval a fire landing a few seconds early
#: must never be skipped.
_JITTER_GUARD_S = 60.0


def resolve_min_interval_minutes(store: Store | None) -> float:
    """The active cadence: DB override > env > compiled default.

    A non-positive or unparseable DB/env value is treated as unset (falls
    through to the next tier), matching :func:`precis.budget.settings.get_float`.
    """
    db_val = app_settings.get_float(store, MIN_INTERVAL_KEY)
    if db_val is not None:
        return db_val
    raw = os.environ.get("PRECIS_DREAM_MIN_INTERVAL_MINUTES")
    if raw:
        try:
            val = float(raw)
        except ValueError:
            val = None
        if val is not None and val > 0:
            return val
    return DEFAULT_MIN_INTERVAL_MINUTES


def last_real_run_at(store: Store) -> datetime | None:
    """The instant the last *real* dream tick (an actual dispatch, not a
    gated no-op) was marked, or ``None`` if the pass has never run for real
    (or the app_settings row is unset/unparseable)."""
    raw = app_settings.get_setting(store, LAST_REAL_RUN_KEY)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def mark_real_run(store: Store, *, when: datetime | None = None) -> None:
    """Stamp ``dream.last_real_run_at`` — call once a tick commits to
    dispatching the LLM (never for a tick that no-op'd on a pre-existing gate
    such as the env flag, high load, or this same throttle)."""
    ts = when or datetime.now(UTC)
    app_settings.set_setting(store, LAST_REAL_RUN_KEY, ts.isoformat())


def skip_if_too_soon(store: Store, *, now: datetime | None = None) -> bool:
    """True if this tick should no-op — the configured interval hasn't
    elapsed since the last real run, past the jitter guard.

    A pass with no recorded real run yet (fresh DB, or the row was never
    written) always proceeds — the throttle only holds back a tick that's
    arriving *too soon after a previous real run*, it never withholds the
    first one.
    """
    last = last_real_run_at(store)
    if last is None:
        return False
    interval_s = resolve_min_interval_minutes(store) * 60.0
    threshold_s = max(0.0, interval_s - _JITTER_GUARD_S)
    elapsed_s = ((now or datetime.now(UTC)) - last).total_seconds()
    if elapsed_s >= threshold_s:
        return False
    log.info(
        "dream_agent: throttled — %.0fs since last real run, need >=%.0fs "
        "(min_interval=%.1fmin)",
        elapsed_s,
        threshold_s,
        interval_s / 60.0,
    )
    return True


__all__ = [
    "DEFAULT_MIN_INTERVAL_MINUTES",
    "LAST_REAL_RUN_KEY",
    "MIN_INTERVAL_KEY",
    "last_real_run_at",
    "mark_real_run",
    "resolve_min_interval_minutes",
    "skip_if_too_soon",
]
