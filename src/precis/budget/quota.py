"""Claude OAuth subscription-quota gate — the right rail for the ``claude -p``
transports, whose dollar cost is *notional* (see :data:`meter.OAUTH_TRANSPORTS`).

The dollar breaker meters **real money** (OpenRouter, paid fetches). Claude via
the OAuth subscription doesn't spend money per call — it draws down the
account's rate-limit windows (``five_hour`` / ``seven_day`` / …). So gating the
claude lane on a dollar figure is category-wrong: it pauses valuable work over a
phantom cap while the subscription sits at ``allowed`` with headroom to spare.

The right backstop is the snapshot ``quota_check`` refreshes into
``claude_quota_snapshot`` (:mod:`precis.utils.claude_quota`). This gate pauses
expensive/paid claude work when a rate-limit window is **rejected** (or, when
the CLI reports a ``used_percentage``, at or over a configurable ceiling), and
auto-clears when the window resets or usage drops. A manual **resume** override
(web ``/budget``) bypasses a soft pause so the operator can unstick the factory.

Dark by construction: no bound store, no snapshot, or an unreadable one → no
pause (mirrors the dollar meter). The gate never raises.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Rate-limit statuses that mean the window still has room. Anything else that
#: is a recognised *blocking* status (below) pauses the claude lane; an
#: unknown status is treated as allowed (never pause on a shape we don't know).
_ALLOWED_STATUSES: frozenset[str] = frozenset(
    {"allowed", "allowed_warning", "warning", "ok"}
)

#: Statuses that pause the claude lane — the account is (or is about to be)
#: rate-limited on that window.
_BLOCKING_STATUSES: frozenset[str] = frozenset({"rejected", "blocked", "exhausted"})

#: ``used_percentage`` ceiling when the CLI reports one. Default 100 → pause
#: only on an explicit rejection; lower it (env or the web override) to leave
#: interactive headroom. The snapshot often omits the percentage, in which case
#: only the ``status`` signal applies.
DEFAULT_CEILING_PCT = 100.0

#: Windows to consider, in binding-priority order (first hit wins the message).
_WINDOWS: tuple[str, ...] = (
    "overage",
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
)


@dataclass(frozen=True, slots=True)
class QuotaPause:
    """A resolved claude-lane pause decision."""

    window: str
    reason: str


def _ceiling_pct(store: Store | None) -> float:
    """Ceiling resolution: DB override (web-set) → env → compiled default."""
    from precis.budget import settings as _s

    override = _s.get_float(store, _s.QUOTA_CEILING_KEY)
    if override is not None:
        return override
    raw = os.environ.get("PRECIS_QUOTA_CEILING_PCT")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_CEILING_PCT


def _fmt_reset(iso: object) -> str:
    if not isinstance(iso, str) or not iso:
        return "the next window reset"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.strftime("%H:%M UTC")


def evaluate(store: Store | None) -> QuotaPause | None:
    """The current claude-lane pause decision, or ``None`` to allow.

    Reads the last quota snapshot; a window that is blocking-status or over the
    ``used_percentage`` ceiling pauses the lane. Best-effort — any failure (no
    store, no snapshot, bad shape) returns ``None`` (dark). The ``resume``
    override is applied by the breaker, not here, so callers can still see the
    real reason.
    """
    if store is None:
        return None
    try:
        row = store.read_claude_quota()
    except Exception:
        log.debug("quota gate: snapshot read failed", exc_info=True)
        return None
    if row is None:
        return None
    windows = row.data.get("windows")
    if not isinstance(windows, dict):
        return None
    ceiling = _ceiling_pct(store)
    ordered = list(_WINDOWS) + [k for k in windows if k not in _WINDOWS]
    for name in ordered:
        bucket = windows.get(name)
        if not isinstance(bucket, dict):
            continue
        status = str(bucket.get("status", "")).strip().lower()
        raw_used = bucket.get("used_percentage")
        used_pct = float(raw_used) if isinstance(raw_used, (int, float)) else None
        blocked_by_status = status in _BLOCKING_STATUSES
        over_ceiling = used_pct is not None and used_pct >= ceiling
        if not (blocked_by_status or over_ceiling):
            continue
        resets = _fmt_reset(bucket.get("resets_at"))
        if over_ceiling and not blocked_by_status and used_pct is not None:
            why = f"{used_pct:.0f}% of the {name} window used (ceiling {ceiling:.0f}%)"
        else:
            why = f"the {name} window is {status or 'rejected'}"
        reason = (
            f"budget: claude subscription quota reached — {why}. Paid claude "
            f"work is paused; free local work still runs. Auto-clears at "
            f"{resets}, or resume now on /budget."
        )
        return QuotaPause(window=name, reason=reason)
    return None


__all__ = ["DEFAULT_CEILING_PCT", "QuotaPause", "evaluate"]
