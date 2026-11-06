"""The ``llm_account`` / pool ledger and usage-signal writer.

Two of the five offering records (memory me568184): **account** (one
credential at one provider under one plan, ``llm_account``) and **pool**
(a rate bucket with a reset rule and explicit scope, a row of
``external_rate_limits`` whose ``provider`` starts with ``llm:``). This
module supplies typed reads and the single UPDATE-only usage-signal writer
for existing pools; the owner-only account write path lives in the handler's
``put`` (validation needs :mod:`precis.todo.guards` and
:mod:`precis.secret_status`, which would make this module import the
handler layer if the upsert lived here too).

:func:`parse_reset_rule` / :func:`format_reset_rule` / :func:`next_reset`
are the typed reset-rule grammar: ``rolling:<seconds>``;
``calendar:<daily|weekly|monthly>:<anchor>@<IANA tz>`` (``anchor`` is
``HH:MM`` for ``daily``, ``<mon|tue|...>:HH:MM`` for ``weekly``,
``<1-28>:HH:MM`` for ``monthly`` — the day-of-month is capped at 28 so
every month has one); ``expires:<ISO UTC>``. Garbage refuses with
``ValueError``; the handler's ``put`` wraps that into a ``BadInput``.

No routing behaviour changes here: ``pick_lane``, ``ladder``,
``resolve_profile`` and ``rate_limit.acquire``/``park``/``_record_refusal``
read ``external_rate_limits`` exactly as before. Reporting never changes a
pool's unit or reset rule, and older reports cannot overwrite newer ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from precis import settings

if TYPE_CHECKING:
    from precis.store import Store

#: Pool keys this account/pool ledger reads: ``llm:<account>:<window>``,
#: never ``host:`` (the existing per-netloc outbound-policy rows).
POOL_PREFIX = "llm:"
log = logging.getLogger(__name__)

#: Calendar weekday tokens, Monday-first (``datetime.weekday()`` order) —
#: the grammar's ``<mon|tue|...>`` anchor vocabulary.
_WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_CALENDAR_PERIODS: frozenset[str] = frozenset({"daily", "weekly", "monthly"})

#: Monthly anchor day is capped here (not 1-31) so every month has a
#: day-of-month to reset on — no "day 31 doesn't exist in February" case.
_MONTHLY_DAY_MAX = 28


@dataclass(frozen=True, slots=True)
class Account:
    """One row of ``llm_account`` — a credential at a provider under a plan."""

    name: str
    vendor: str
    plan: str
    adapter: str
    profile: str | None
    api_key_ref: str | None
    concurrency: int
    task_classes: tuple[str, ...]
    models: tuple[str, ...]
    default_model: str | None
    enabled: bool
    entitlement: dict[str, Any]
    privacy: dict[str, Any]
    promo_expiry: date | None
    pools: tuple[str, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Pool:
    """One ``llm:``-prefixed row of ``external_rate_limits`` — a rate bucket
    with a reset rule and explicit scope. Includes the token-bucket carrier
    columns (0121/0233) alongside the reporting columns this migration adds;
    the carrier columns keep ``rate_limit.acquire``/``park`` working and are
    not surfaced on the ``/pools`` view except for ``parked_until`` as
    ``retry_at``. Freshness derives from the last usage report, so a pause
    alone cannot make unknown utilisation look current."""

    provider: str
    capacity: int
    refill_per_sec: float
    tokens: float
    daily_cap: int | None
    cls: str | None
    unit: str | None
    limit_reported: float | None
    used_reported: float | None
    remaining_reported: float | None
    reported_at: datetime | None
    source: str | None
    confidence: str | None
    reset_rule: str | None
    resets_at: datetime | None
    expires_at: datetime | None
    parked_until: datetime | None
    stale: bool


_ACCOUNT_COLS = (
    "name, vendor, plan, adapter, profile, api_key_ref, concurrency, "
    "task_classes, models, default_model, enabled, entitlement, privacy, "
    "promo_expiry, pools, updated_at"
)


def _account_from_row(row: tuple[Any, ...]) -> Account:
    return Account(
        name=row[0],
        vendor=row[1],
        plan=row[2],
        adapter=row[3],
        profile=row[4],
        api_key_ref=row[5],
        concurrency=row[6],
        task_classes=tuple(row[7] or ()),
        models=tuple(row[8] or ()),
        default_model=row[9],
        enabled=row[10],
        entitlement=row[11] or {},
        privacy=row[12] or {},
        promo_expiry=row[13],
        pools=tuple(row[14] or ()),
        updated_at=row[15],
    )


def list_accounts(store: Store) -> list[Account]:
    """Every ``llm_account`` row, name-ordered."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"SELECT {_ACCOUNT_COLS} FROM llm_account ORDER BY name"
        ).fetchall()
    return [_account_from_row(r) for r in rows]


def get_account(store: Store, name: str) -> Account | None:
    """The ``llm_account`` row named ``name``, or ``None``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT {_ACCOUNT_COLS} FROM llm_account WHERE name = %s", (name,)
        ).fetchone()
    return _account_from_row(row) if row is not None else None


_POOL_COLS = (
    'provider, capacity, refill_per_sec, tokens, daily_cap, "class", unit, '
    "limit_reported, used_reported, remaining_reported, reported_at, "
    "source, confidence, reset_rule, resets_at, expires_at, parked_until"
)


def _pool_from_row(row: tuple[Any, ...], *, now: datetime, stale_after_s: int) -> Pool:
    return Pool(
        provider=row[0],
        capacity=row[1],
        refill_per_sec=row[2],
        tokens=row[3],
        daily_cap=row[4],
        cls=row[5],
        unit=row[6],
        limit_reported=float(row[7]) if row[7] is not None else None,
        used_reported=float(row[8]) if row[8] is not None else None,
        remaining_reported=float(row[9]) if row[9] is not None else None,
        reported_at=row[10],
        source=row[11],
        confidence=row[12],
        reset_rule=row[13],
        resets_at=row[14],
        expires_at=row[15],
        parked_until=row[16],
        stale=row[10] is None or now - row[10] > timedelta(seconds=stale_after_s),
    )


def list_pools(store: Store) -> list[Pool]:
    """Every ``external_rate_limits`` row whose ``provider`` is a pool key
    (``llm:<account>:<window>``), provider-ordered. ``host:`` rows (the
    outbound-policy per-netloc rows) are a different scope and never
    returned here."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"SELECT {_POOL_COLS} FROM external_rate_limits "
            "WHERE provider LIKE %s ORDER BY provider",
            (POOL_PREFIX + "%",),
        ).fetchall()
    stale_after_s = settings.get_int("llm.pool_stale_after_s", store=store)
    if stale_after_s is None:
        stale_after_s = 7200
    now = datetime.now(UTC)
    return [_pool_from_row(r, now=now, stale_after_s=stale_after_s) for r in rows]


def record_pool_report(
    store: Store,
    key: str,
    *,
    used: float | Decimal | None = None,
    remaining: float | Decimal | None = None,
    limit: float | Decimal | None = None,
    source: str,
    reported_at: datetime,
    resets_at: datetime | None = None,
) -> bool:
    """Record an existing pool's report, ignoring older observations.

    A ``None`` ``used`` / ``remaining`` / ``resets_at`` keeps the stored
    value (the report just did not carry it); the report still stamps
    ``reported_at``. Limits fill NULL once; seeded limits (notably Devin's
    one session) survive every report. Unknown keys never create carrier
    rows.
    """
    if not key.startswith(POOL_PREFIX):
        log.warning("pool report: unknown pool key %s", key)
        return False
    with store.pool.connection() as conn:
        row = conn.execute(
            "UPDATE external_rate_limits SET "
            "used_reported = COALESCE(%s, used_reported), "
            "remaining_reported = COALESCE(%s, remaining_reported), "
            "limit_reported = COALESCE(limit_reported, %s), "
            "source = %s, reported_at = %s, "
            "resets_at = COALESCE(%s, resets_at) WHERE provider = %s "
            "AND (reported_at IS NULL OR reported_at <= %s) RETURNING provider",
            (used, remaining, limit, source, reported_at, resets_at, key, reported_at),
        ).fetchone()
        if row is not None:
            return True
        known = conn.execute(
            "SELECT 1 FROM external_rate_limits WHERE provider = %s", (key,)
        ).fetchone()
    if known is None:
        log.warning("pool report: unknown pool key %s", key)
    return False


def record_pool_failure(
    store: Store,
    key: str,
    *,
    reason_class: str,
    retry_at: datetime,
    notice: str | None = None,
) -> bool:
    """Extend an existing pool's park without shortening its horizon.

    ``notice='provider'`` identifies an explicit provider reset/pause;
    absent that evidence the horizon is an estimate. Provenance follows
    the horizon retained, including when a shorter failure arrives later.
    """
    if not key.startswith(POOL_PREFIX):
        log.warning("pool failure: unknown pool key %s (%s)", key, reason_class)
        return False
    source = "provider" if notice == "provider" else "estimate"
    with store.pool.connection() as conn:
        row = conn.execute(
            "UPDATE external_rate_limits SET "
            "source = CASE WHEN parked_until IS NULL OR parked_until <= %s "
            "THEN %s ELSE source END, "
            "parked_until = GREATEST(parked_until, %s) "
            "WHERE provider = %s RETURNING provider",
            (retry_at, source, retry_at, key),
        ).fetchone()
    if row is None:
        log.warning("pool failure: unknown pool key %s (%s)", key, reason_class)
        return False
    return True


# ── reset-rule grammar ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RollingReset:
    """``rolling:<seconds>`` — resets ``seconds`` after it was last touched.

    This ledger carries no per-window "started at" state (S3 reconciles the
    bucket consume to actual on completion), so :func:`next_reset` answers
    "a full window from now" rather than reading a stored start.
    """

    seconds: int


@dataclass(frozen=True, slots=True)
class CalendarReset:
    """``calendar:<daily|weekly|monthly>:<anchor>@<IANA tz>``.

    ``weekday`` (0=mon .. 6=sun) is set for ``period='weekly'`` only;
    ``day_of_month`` (1-28) for ``period='monthly'`` only.
    """

    period: str
    hour: int
    minute: int
    tz: str
    weekday: int | None = None
    day_of_month: int | None = None


@dataclass(frozen=True, slots=True)
class ExpiresReset:
    """``expires:<ISO UTC>`` — a one-shot promo/entitlement expiry, not a
    recurring reset. ``at`` is always normalized to UTC."""

    at: datetime


ResetRule = RollingReset | CalendarReset | ExpiresReset


def _bad_rule(text: str, why: str) -> ValueError:
    return ValueError(
        f"bad reset_rule {text!r}: {why}; grammar is rolling:<seconds>, "
        "calendar:<daily|weekly|monthly>:<anchor>@<IANA tz>, or "
        "expires:<ISO UTC>"
    )


def parse_reset_rule(text: str) -> ResetRule:
    """Parse the reset-rule grammar into a typed :data:`ResetRule`.

    Raises ``ValueError`` on anything outside the three grammars — never
    returns a partially-understood rule."""
    s = (text or "").strip()
    if s.startswith("rolling:"):
        raw = s[len("rolling:") :]
        try:
            seconds = int(raw)
        except ValueError:
            raise _bad_rule(text, "rolling:<seconds> needs an integer") from None
        if seconds <= 0:
            raise _bad_rule(text, "rolling seconds must be positive")
        return RollingReset(seconds)
    if s.startswith("calendar:"):
        rest = s[len("calendar:") :]
        period, _, anchor_tz = rest.partition(":")
        if not anchor_tz:
            raise _bad_rule(text, "missing <anchor>@<tz>")
        if period not in _CALENDAR_PERIODS:
            raise _bad_rule(text, f"unknown calendar period {period!r}")
        if "@" not in anchor_tz:
            raise _bad_rule(text, "calendar rule needs @<IANA tz>")
        anchor, _, tz_name = anchor_tz.rpartition("@")
        try:
            ZoneInfo(tz_name)
        except Exception:
            raise _bad_rule(text, f"unknown IANA tz {tz_name!r}") from None
        parts = anchor.split(":")
        weekday: int | None = None
        day_of_month: int | None = None
        if period == "daily":
            if len(parts) != 2:
                raise _bad_rule(text, "daily anchor is HH:MM")
            hh, mm = parts
        elif period == "weekly":
            if len(parts) != 3:
                raise _bad_rule(text, "weekly anchor is <mon|tue|...>:HH:MM")
            wkd, hh, mm = parts
            if wkd not in _WEEKDAYS:
                raise _bad_rule(text, f"unknown weekday {wkd!r}")
            weekday = _WEEKDAYS.index(wkd)
        else:
            if len(parts) != 3:
                raise _bad_rule(text, "monthly anchor is <1-28>:HH:MM")
            day_s, hh, mm = parts
            try:
                day_of_month = int(day_s)
            except ValueError:
                raise _bad_rule(text, "monthly day must be an integer") from None
            if not 1 <= day_of_month <= _MONTHLY_DAY_MAX:
                raise _bad_rule(text, f"monthly day must be 1-{_MONTHLY_DAY_MAX}")
        try:
            hour, minute = int(hh), int(mm)
        except ValueError:
            raise _bad_rule(text, "HH:MM must be integers") from None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise _bad_rule(text, "HH:MM out of range")
        return CalendarReset(period, hour, minute, tz_name, weekday, day_of_month)
    if s.startswith("expires:"):
        raw = s[len("expires:") :]
        try:
            at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise _bad_rule(
                text, "expires:<ISO UTC> is not a valid timestamp"
            ) from None
        if at.tzinfo is None:
            raise _bad_rule(text, "expires timestamp must be UTC/tz-aware")
        return ExpiresReset(at.astimezone(UTC))
    raise _bad_rule(text, "unrecognized grammar")


def format_reset_rule(rule: ResetRule) -> str:
    """Render a typed :data:`ResetRule` back to its canonical text form —
    the inverse of :func:`parse_reset_rule` for a canonically-formed rule."""
    if isinstance(rule, RollingReset):
        return f"rolling:{rule.seconds}"
    if isinstance(rule, CalendarReset):
        if rule.period == "daily":
            anchor = f"{rule.hour:02d}:{rule.minute:02d}"
        elif rule.period == "weekly":
            if rule.weekday is None:
                raise ValueError("weekly CalendarReset needs weekday")
            anchor = f"{_WEEKDAYS[rule.weekday]}:{rule.hour:02d}:{rule.minute:02d}"
        elif rule.period == "monthly":
            if rule.day_of_month is None:
                raise ValueError("monthly CalendarReset needs day_of_month")
            anchor = f"{rule.day_of_month}:{rule.hour:02d}:{rule.minute:02d}"
        else:
            raise ValueError(f"unknown calendar period {rule.period!r}")
        return f"calendar:{rule.period}:{anchor}@{rule.tz}"
    if isinstance(rule, ExpiresReset):
        return f"expires:{rule.at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    raise TypeError(f"unknown reset rule: {rule!r}")  # pragma: no cover


def next_reset(rule: ResetRule, now: datetime) -> datetime:
    """The next reset instant at or after ``now``, in UTC.

    ``rolling`` has no stored window-start, so this answers "a full window
    from ``now``" (S3 reconciles the bucket consume to actual on
    completion — the ledger doesn't track in-progress windows). ``expires``
    is a fixed one-shot instant. ``calendar`` walks ``now`` into the rule's
    ``tz`` via :mod:`zoneinfo`, finds the next occurrence of the anchor
    strictly after ``now``, and converts back to UTC — correct across a
    tz/UTC day boundary (e.g. a daily reset anchored past midnight UTC in
    a tz still on the previous UTC day).
    """
    if now.tzinfo is None:
        raise ValueError("next_reset: now must be tz-aware")
    now_utc = now.astimezone(UTC)
    if isinstance(rule, RollingReset):
        return now_utc + timedelta(seconds=rule.seconds)
    if isinstance(rule, ExpiresReset):
        return rule.at
    if isinstance(rule, CalendarReset):
        tz = ZoneInfo(rule.tz)
        local_now = now_utc.astimezone(tz)
        candidate = local_now.replace(
            hour=rule.hour, minute=rule.minute, second=0, microsecond=0
        )
        if rule.period == "daily":
            if candidate <= local_now:
                candidate += timedelta(days=1)
        elif rule.period == "weekly":
            if rule.weekday is None:
                raise ValueError("weekly CalendarReset needs weekday")
            days_ahead = (rule.weekday - candidate.weekday()) % 7
            candidate += timedelta(days=days_ahead)
            if candidate <= local_now:
                candidate += timedelta(days=7)
        elif rule.period == "monthly":
            if rule.day_of_month is None:
                raise ValueError("monthly CalendarReset needs day_of_month")
            candidate = candidate.replace(day=rule.day_of_month)
            if candidate <= local_now:
                year = candidate.year + (1 if candidate.month == 12 else 0)
                month = 1 if candidate.month == 12 else candidate.month + 1
                candidate = candidate.replace(year=year, month=month)
        else:
            raise ValueError(f"unknown calendar period {rule.period!r}")
        return candidate.astimezone(UTC)
    raise TypeError(f"unknown reset rule: {rule!r}")  # pragma: no cover


__all__ = [
    "POOL_PREFIX",
    "Account",
    "CalendarReset",
    "ExpiresReset",
    "Pool",
    "ResetRule",
    "RollingReset",
    "format_reset_rule",
    "get_account",
    "list_accounts",
    "list_pools",
    "next_reset",
    "parse_reset_rule",
    "record_pool_failure",
    "record_pool_report",
]
