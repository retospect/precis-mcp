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

**Reactive stamp** (:func:`stamp_exhausted`). The probe runs on a ten-minute
cadence, so a live quota 429 used to leave the snapshot stale and every
surface rediscovered exhaustion on its own (28 slipped through on
2026-09-17 alone). The router now writes the exhausted window back the
moment a ``quota``-class result lands — status ``exceeded``, ``resets_at``
from the notice — so the very next claude call on the same store pauses
without spawning a subprocess. The stamp also records one ``probe_due``
(the reset plus a little jitter); :mod:`precis.workers.quota_check` runs
its refresh early when that instant passes, so one probe per exhausted
window re-reads the real state however many jobs parked. A blocking
window whose ``resets_at`` is already in the past is ignored by
:func:`evaluate` — time clears a stamp even if no probe ever runs.

Dark by construction: no bound store, no snapshot, or an unreadable one → no
pause (mirrors the dollar meter). The gate never raises.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.utils.timeutil import as_utc

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
_BLOCKING_STATUSES: frozenset[str] = frozenset(
    {"rejected", "blocked", "exhausted", "exceeded"}
)

#: Jitter added to a reset instant before the reactive probe is due, so a
#: fleet that parked together does not probe in the same second the window
#: rolls — and so the probe lands after Anthropic's clock, not on it.
_PROBE_JITTER_S: tuple[int, int] = (30, 180)

#: Which snapshot window a quota notice names, by wording. The CLI's
#: ``rate_limit_event`` keys (``five_hour`` / ``seven_day`` / ``overage``)
#: are what :func:`evaluate` orders on, so the stamp has to speak them.
_NOTICE_WINDOWS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"weekly limit", re.IGNORECASE), "seven_day"),
    (re.compile(r"session limit", re.IGNORECASE), "five_hour"),
    (re.compile(r"extra usage", re.IGNORECASE), "overage"),
)

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
    """Ceiling resolution via the registered settings layer: DB row
    (web-set) → ``PRECIS_QUOTA_CEILING_PCT`` → compiled default."""
    from precis import settings as _psettings
    from precis.budget.settings import QUOTA_CEILING_KEY

    value = _psettings.get_float(
        QUOTA_CEILING_KEY, store=store, default=DEFAULT_CEILING_PCT
    )
    return value if value is not None else DEFAULT_CEILING_PCT


def _fmt_reset(iso: object) -> str:
    if not isinstance(iso, str) or not iso:
        return "the next window reset"
    dt = as_utc(iso)
    if dt is None:
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
        # A window past its own reset has rolled: a stamp (reactive or
        # probed) that outlived its window must not keep the lane paused
        # until the next probe happens to run.
        reset_dt = as_utc(bucket.get("resets_at"))
        if reset_dt is not None and reset_dt <= datetime.now(UTC):
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


def window_for_notice(text: str | None) -> str:
    """The snapshot window a quota notice names (``five_hour`` when the
    wording names none — the shortest window, so the stamp clears soonest)."""
    for pattern, window in _NOTICE_WINDOWS:
        if text and pattern.search(text):
            return window
    return "five_hour"


def stamp_exhausted(
    store: Store | None,
    *,
    resets_at: datetime | None,
    notice: str | None = None,
    window: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Write a live quota exhaustion back to ``claude_quota_snapshot`` so
    the gate pauses every subsequent claude call for the rest of the window.

    ``resets_at`` is the router's parsed horizon (``None`` → two hours,
    the quota wording's own fallback). Other windows in the existing
    snapshot are kept; the exhausted one becomes ``{"status": "exceeded",
    "resets_at": ..., "source": "reactive"}`` and ``probe_due`` is set to
    the reset plus jitter — the one probe :mod:`precis.workers.quota_check`
    runs early for this window. Idempotent: a stamp for the same window
    and reset is a no-op (``False``), so a storm of parked jobs writes
    once. Best-effort and dark: no store or a failed write → ``False``,
    never raises.
    """
    if store is None:
        return False
    now = now if now is not None else datetime.now(UTC)
    reset_dt = resets_at if resets_at is not None else now + timedelta(hours=2)
    reset_dt = (
        reset_dt.astimezone(UTC) if reset_dt.tzinfo else reset_dt.replace(tzinfo=UTC)
    )
    name = window or window_for_notice(notice)
    try:
        row = store.read_claude_quota()
        data: dict[str, Any] = dict(row.data) if row is not None else {}
        windows = data.get("windows")
        windows = dict(windows) if isinstance(windows, dict) else {}
        current = windows.get(name)
        if (
            isinstance(current, dict)
            and str(current.get("status", "")).strip().lower() in _BLOCKING_STATUSES
            and as_utc(current.get("resets_at")) == reset_dt
        ):
            return False
        windows[name] = {
            "status": "exceeded",
            "resets_at": reset_dt.isoformat(),
            "source": "reactive",
        }
        data["windows"] = windows
        data["probe_due"] = (
            reset_dt + timedelta(seconds=random.randint(*_PROBE_JITTER_S))
        ).isoformat()
        data["reactive_notice"] = (notice or "")[:200]
        store.record_claude_quota(scope="unified", data=data)
    except Exception:
        log.debug("quota gate: reactive stamp failed", exc_info=True)
        return False
    log.warning(
        "quota gate: claude %s window stamped exceeded from a live notice; "
        "paid claude work pauses until %s (probe due %s)",
        name,
        reset_dt.strftime("%H:%M UTC"),
        data["probe_due"],
    )
    return True


def probe_due(store: Store | None, *, now: datetime | None = None) -> bool:
    """``True`` when a reactive stamp's scheduled probe instant has passed
    — :mod:`precis.workers.quota_check` refreshes regardless of its cadence.
    Dark on any failure."""
    if store is None:
        return False
    try:
        row = store.read_claude_quota()
    except Exception:
        return False
    if row is None:
        return False
    due = as_utc(row.data.get("probe_due"))
    if due is None:
        return False
    return due <= (now if now is not None else datetime.now(UTC))


__all__ = [
    "DEFAULT_CEILING_PCT",
    "QuotaPause",
    "evaluate",
    "probe_due",
    "stamp_exhausted",
    "window_for_notice",
]
