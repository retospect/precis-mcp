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

from precis.budget.work_class import WorkClass
from precis.budget.work_class import resolve as resolve_work_class
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
#: interactive headroom. A snapshot missing percentages (older probe shapes)
#: leaves only the ``status`` signal.
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


@dataclass(frozen=True, slots=True)
class ClassFloor:
    """When the taper stops a tier for one work class.

    ``stop_big_pct`` / ``stop_medium_pct``: percent of a window *used* at or
    past which BIG+FRONTIER / MEDIUM too stop being admitted (``None`` =
    never). ``warning_stops_big``: whether the provider's warning status
    alone stops BIG+FRONTIER for this class — consulted only when no live
    window reports a ``used_percentage``; once a percentage is visible it
    is the sole signal and the warning band stops nothing on its own.
    """

    stop_big_pct: float | None
    stop_medium_pct: float | None
    warning_stops_big: bool


#: The taper table (gr347551; Reto 2026-10-11 tiers, 2026-10-12 work-class
#: axis, 2026-10-13 "warning 2% or so"): per class, when each paid tier
#: stops. Background BIG stops at ~2% remaining, MEDIUM at ~1%; deliverables
#: hold out a point longer. At 100% used (or a blocking status)
#: :func:`evaluate` parks every class and paid tier until the reset;
#: SMALL and local tiers never reach this gate; the resume override bypasses
#: both. Deliverables stop BIG later than background and their caller
#: degrades to MEDIUM and posts a notice rather than pausing silently
#: (``precis.workers.briefing``); interactive work is never tapered.
TAPER_FLOORS: dict[WorkClass, ClassFloor] = {
    WorkClass.BACKGROUND: ClassFloor(
        stop_big_pct=98.0, stop_medium_pct=99.0, warning_stops_big=True
    ),
    WorkClass.DELIVERABLE: ClassFloor(
        stop_big_pct=99.0, stop_medium_pct=99.5, warning_stops_big=True
    ),
    WorkClass.INTERACTIVE: ClassFloor(
        stop_big_pct=None, stop_medium_pct=None, warning_stops_big=False
    ),
}

#: Background floors, kept as names for callers that predate the table.
TAPER_STOP_BIG_PCT = TAPER_FLOORS[WorkClass.BACKGROUND].stop_big_pct
TAPER_STOP_MEDIUM_PCT = TAPER_FLOORS[WorkClass.BACKGROUND].stop_medium_pct

_BIG_UP: frozenset[str] = frozenset({"big", "frontier"})
_MEDIUM_UP: frozenset[str] = frozenset({"medium", "big", "frontier"})

#: Statuses the CLI raises ahead of exhaustion: the window is inside its
#: warning band (the provider's own "running low" signal, which starts far
#: above the floors). Only a fallback: when no live window reports a
#: percentage the band still stops BIG so a blind taper stays conservative;
#: once a ``used_percentage`` is visible the floors alone decide.
_WARNING_STATUSES: frozenset[str] = frozenset({"allowed_warning", "warning"})


#: Windows the taper reads; ``overage`` and unknown buckets are ignored.
_TAPER_WINDOWS: frozenset[str] = frozenset({"five_hour", "seven_day"})


@dataclass(frozen=True, slots=True)
class Taper:
    """Admission-taper reading of the quota snapshot.

    ``used_pct`` is the highest ``used_percentage`` over live windows
    (``None`` when none reports one) and ``pct_reason`` names that window;
    ``warning_reason`` names a live window in its warning band (empty when
    none). When ``used_pct`` is set it is the only signal — the floors
    alone decide and a warning band stops nothing; ``warning_reason``
    applies only as the blind fallback. :meth:`blocked` turns the reading
    into the tiers stopped for a work class via :data:`TAPER_FLOORS`.

    ``level`` / ``reason`` are the background reading (0 admit all, 1 stop
    BIG/FRONTIER, 2 also stop MEDIUM) — what /budget shows as the headline.
    ``no_signal`` is True when the snapshot carried neither a usable
    ``used_percentage`` nor a warning status, so the taper cannot see the
    remaining headroom and fails open (surfaced on /budget so a blind taper
    is visible rather than silently inert).
    """

    no_signal: bool
    used_pct: float | None = None
    pct_reason: str = ""
    warning_reason: str = ""

    def blocked(self, work_class: str | WorkClass | None = None) -> frozenset[str]:
        """Tier names (``medium``/``big``/``frontier``) the taper stops for
        ``work_class`` (resolved like a request's: ambient → background)."""
        floor = TAPER_FLOORS[resolve_work_class(work_class)]
        used = self.used_pct
        if used is not None:
            # A percentage is visible: the floors alone decide. The
            # provider's warning band starts far above them, so the
            # warning status must not stop anything on its own.
            if floor.stop_medium_pct is not None and used >= floor.stop_medium_pct:
                return _MEDIUM_UP
            if floor.stop_big_pct is not None and used >= floor.stop_big_pct:
                return _BIG_UP
            return frozenset()
        if self.warning_reason and floor.warning_stops_big:
            return _BIG_UP
        return frozenset()

    def why(self, work_class: str | WorkClass | None = None) -> str:
        """Human reason for :meth:`blocked`, with the remaining headroom."""
        floor = TAPER_FLOORS[resolve_work_class(work_class)]
        blocked = self.blocked(work_class)
        if not blocked:
            return ""
        used = self.used_pct
        threshold = floor.stop_medium_pct if "medium" in blocked else floor.stop_big_pct
        if used is not None and threshold is not None and used >= threshold:
            return f"{self.pct_reason}; under {100 - threshold:.0f}% remaining"
        return f"{self.warning_reason}; in the provider's warning band"

    @property
    def level(self) -> int:
        blocked = self.blocked(WorkClass.BACKGROUND)
        return 2 if "medium" in blocked else 1 if blocked else 0

    @property
    def reason(self) -> str:
        blocked = self.blocked(WorkClass.BACKGROUND)
        if not blocked:
            return ""
        used = self.used_pct
        if (
            used is not None
            and TAPER_STOP_BIG_PCT is not None
            and used >= TAPER_STOP_BIG_PCT
        ):
            return self.pct_reason
        return self.warning_reason


def taper_state(store: Store | None) -> Taper:
    """Read the snapshot's remaining headroom into a :class:`Taper`.

    Over live windows (``five_hour`` / ``seven_day``; a window already past
    its ``resets_at`` has rolled and is ignored): the highest
    ``used_percentage``, and whether any window is in its warning band.
    Dark (``no_signal=True``, blocks nothing) on no store / snapshot / bad
    shape; never raises.
    """
    dark = Taper(no_signal=True)
    if store is None:
        return dark
    try:
        row = store.read_claude_quota()
    except Exception:
        log.debug("quota taper: snapshot read failed", exc_info=True)
        return dark
    if row is None:
        return dark
    windows = row.data.get("windows")
    if not isinstance(windows, dict):
        return dark
    now = datetime.now(UTC)
    used_pct: float | None = None
    pct_reason = warning_reason = ""
    for name, bucket in windows.items():
        # Headroom windows only: overage is extra-usage credit, not headroom.
        if name not in _TAPER_WINDOWS or not isinstance(bucket, dict):
            continue
        reset_dt = as_utc(bucket.get("resets_at"))
        if reset_dt is not None and reset_dt <= now:
            continue
        status = str(bucket.get("status", "")).strip().lower()
        raw = bucket.get("used_percentage")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if used_pct is None or raw > used_pct:
                used_pct = float(raw)
                pct_reason = f"{raw:.0f}% of the {name} window used"
        elif status in _WARNING_STATUSES and not warning_reason:
            warning_reason = f"the {name} window is {status}"
    return Taper(
        no_signal=used_pct is None and not warning_reason,
        used_pct=used_pct,
        pct_reason=pct_reason,
        warning_reason=warning_reason,
    )


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
    "TAPER_FLOORS",
    "ClassFloor",
    "QuotaPause",
    "Taper",
    "evaluate",
    "probe_due",
    "stamp_exhausted",
    "taper_state",
    "window_for_notice",
]
