"""1-minute load-average gate for heavy worker passes.

Used by the agentic reviewers (structural, deep_review, dream_agent)
to back off when the host is under load. The cheap SQL passes
(dispatch, schedule, nursery, auto_check) don't need a gate —
they're idempotent and short; even on a saturated box they finish
inside the rotation tick.

Heuristic
=========

Pass *skips* (returns silently as a no-op) when the 1-minute load
average exceeds ``PRECIS_LOAD_CEILING``. Default ceiling scales
with the host's reported CPU count:

* ``os.cpu_count() * 1.5`` when ``PRECIS_LOAD_CEILING`` is unset.
* Absolute float when set: ``PRECIS_LOAD_CEILING=12.0``.

Why 1.5×: 1.0× is the "fully loaded" mark; pushing past it is
fine briefly. The 0.5 cushion gives the gate hysteresis — a spike
to 1.4× during a fix_gripe runs doesn't kill an unrelated review.

Platform notes
==============

``os.getloadavg()`` returns a NotImplementedError on Windows. Our
deployment is macOS/Linux only, but the helper degrades to "always
allow" on platforms without the API rather than crashing the
worker — better to skip the safety net than to take the box down.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def _load_ceiling() -> float:
    """Resolve the ceiling from env or auto-scale by CPU count."""
    raw = os.environ.get("PRECIS_LOAD_CEILING")
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            log.warning(
                "load_gate: PRECIS_LOAD_CEILING=%r is not a float; "
                "falling back to auto-scale",
                raw,
            )
    cpus = os.cpu_count() or 4
    return cpus * 1.5


def current_load() -> float | None:
    """Return the 1-min load avg, or ``None`` on platforms without it."""
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        # AttributeError on Windows (no os.getloadavg);
        # OSError on some restricted sandboxes.
        return None


def skip_if_high_load(pass_label: str) -> bool:
    """Return True if the current pass should skip due to load.

    Logs the skip at INFO level so the operator can see why a
    scheduled pass didn't fire on a given tick. ``pass_label`` is the
    free-text identifier surfaced in the log (e.g. ``"review[deep]"``,
    ``"dream_agent"``).
    """
    load1 = current_load()
    if load1 is None:
        return False
    ceiling = _load_ceiling()
    if load1 > ceiling:
        log.info(
            "%s: load %.1f > %.1f ceiling; skipping",
            pass_label,
            load1,
            ceiling,
        )
        return True
    return False


__all__ = ["current_load", "skip_if_high_load"]
