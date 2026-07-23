"""Minimal cron parser + ``every:`` shorthand translator for Slice 4.

The plan reserves a vetted cron library if there's one in the dep
tree; there isn't, so we hand-roll a 5-field cron subset. What we
accept:

* Five space-separated fields: ``minute hour dom month dow``.
* Per-field shapes: ``*``, ``N`` (integer), ``*/N`` (step from 0),
  ``A,B,C`` (list), ``A-B`` (range), ``A-B/N`` (stepped range).
* No aliases (``@daily``, ``@hourly``). Dreamer / weather / arxiv
  watches don't need them; the shorthand form covers the common
  cases more readably.

The ``every:`` shorthand translates at write time so the runtime
only ever sees one shape (cron). Accepted shapes:

* ``Nh`` (every N hours, on minute 0)
* ``Nm`` (every N minutes)
* ``Nd`` (every N days, at midnight)
* ``mon|tue|...|sun HH:MM`` (weekly, at HH:MM on that day)

A third shape, ``at`` (an absolute ISO 8601 timestamp), covers the *one-shot*
case -- "remind me at/in N" -- that used to live on the retired
``kind='cron'`` (ADR 0061, superseding ADR 0030). Mutually exclusive with
``cron``/``every``: a schedule either repeats (``cron``) or fires exactly
once (``at``). ``catch_up`` is the ``at``-only sibling of
``backfill_missed`` -- whether an overdue one-shot still fires (``True``,
the default, matching the old cron one-shot default) or is silently marked
expired (``False``).

This module is the **single point of truth** for both write-time
validation (called from ``handlers/_todo_guards``) and tick
expansion (called from ``workers/schedule/worker``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from precis.errors import BadInput

# ── Public surface ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Schedule:
    """Parsed ``meta.schedule`` block.

    Exactly one of ``cron`` / ``at`` is set -- a schedule either repeats
    (``cron``, the canonical 5-field string; ``every:`` shorthand already
    translated) or fires exactly once (``at``, an ISO 8601 timestamp).

    ``backfill_missed`` (``cron`` schedules only) defaults to ``False``:
    weather / news / "yesterday's headlines" don't owe the missed tick;
    opt-in ``True`` for birthdays and anniversaries where the action is
    still owed.

    ``catch_up`` (``at`` schedules only) defaults to ``True`` -- a one-shot
    reminder missed while the machine was down still fires late (matches
    the retired ``kind='cron'`` one-shot default); ``False`` means "fire at
    the moment or not at all."
    """

    cron: str | None = None
    backfill_missed: bool = False
    at: str | None = None
    catch_up: bool = True


#: Grace window past ``at`` before an overdue one-shot with ``catch_up=False``
#: is marked expired instead of fired. Mirrors the retired cron-tick's
#: ``_MISS_GRACE`` (60s tick cadence + 30s slack).
ONE_SHOT_GRACE = timedelta(seconds=90)


def validate_schedule(spec: Any) -> Schedule:
    """Validate ``meta.schedule`` at write time. Returns the parsed
    :class:`Schedule` so the handler can store the canonical form.

    Raises :class:`BadInput` with the catalogue on any malformed
    shape — bad cron, bad shorthand, bad type, extra keys.
    """
    if not isinstance(spec, dict):
        raise BadInput(
            f"meta.schedule must be a dict, got {type(spec).__name__}",
            next=(
                "meta={'schedule': {'cron': '0 9 * * 1', "
                "'backfill_missed': false}} "
                "or {'every': '1d'} or {'at': '2026-06-12T09:00:00Z'}"
            ),
        )
    extra = set(spec) - {"cron", "every", "at", "backfill_missed", "catch_up"}
    if extra:
        raise BadInput(
            f"unknown meta.schedule keys: {sorted(extra)}",
            options=["cron", "every", "at", "backfill_missed", "catch_up"],
        )
    cron_str = spec.get("cron")
    every_str = spec.get("every")
    at_str = spec.get("at")
    given = [
        k
        for k, v in (("cron", cron_str), ("every", every_str), ("at", at_str))
        if v is not None
    ]
    if not given:
        raise BadInput(
            "meta.schedule needs one of 'cron', 'every', or 'at'",
            next=(
                "schedule={'cron': '0 9 * * 1'} or schedule={'every': '1d'} "
                "or schedule={'at': '2026-06-12T09:00:00Z'} (one-shot)"
            ),
        )
    if len(given) > 1:
        raise BadInput(
            f"meta.schedule cannot carry more than one of 'cron'/'every'/'at'; got {given}",
            next=(
                "every='1d' is shorthand for cron='0 0 * * *'; "
                "'at' is a one-shot absolute fire time, mutually exclusive "
                "with the recurring cron/every forms"
            ),
        )
    if at_str is not None:
        if not isinstance(at_str, str):
            raise BadInput(
                f"meta.schedule.at must be a string, got {type(at_str).__name__}",
            )
        if "backfill_missed" in spec:
            raise BadInput(
                "meta.schedule.backfill_missed is not accepted alongside 'at' "
                "-- use 'catch_up' for one-shot schedules",
                next="schedule={'at': '...', 'catch_up': true}",
            )
        canonical_at = _coerce_iso(at_str.strip())
        catch_up = spec.get("catch_up", True)
        if not isinstance(catch_up, bool):
            raise BadInput(
                f"meta.schedule.catch_up must be a bool, got {type(catch_up).__name__}",
            )
        return Schedule(at=canonical_at, catch_up=catch_up)
    if "catch_up" in spec:
        raise BadInput(
            "meta.schedule.catch_up is not accepted alongside 'cron'/'every' "
            "-- use 'backfill_missed' for recurring schedules",
            next="schedule={'cron': '...', 'backfill_missed': false}",
        )
    if cron_str is not None:
        if not isinstance(cron_str, str):
            raise BadInput(
                f"meta.schedule.cron must be a string, got {type(cron_str).__name__}",
            )
        canonical = cron_str.strip()
    else:
        if not isinstance(every_str, str):
            raise BadInput(
                f"meta.schedule.every must be a string, got {type(every_str).__name__}",
            )
        canonical = every_to_cron(every_str.strip())
    # Round-trip parse so a bad cron raises here, not at the next tick.
    parse_cron(canonical)
    backfill = spec.get("backfill_missed", False)
    if not isinstance(backfill, bool):
        raise BadInput(
            f"meta.schedule.backfill_missed must be a bool, got "
            f"{type(backfill).__name__}",
        )
    return Schedule(cron=canonical, backfill_missed=backfill)


def _coerce_iso(s: str) -> str:
    """Parse + re-render an ISO 8601 timestamp to a canonical aware-UTC form.

    Raises :class:`BadInput` on a malformed value.
    """
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadInput(
            f"unparseable meta.schedule.at timestamp {s!r}",
            next=(
                "use ISO 8601: at='2026-06-12T09:00:00Z' or '2026-06-12T09:00:00+00:00'"
            ),
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def one_shot_action(
    at: datetime, *, catch_up: bool, now: datetime
) -> Literal["wait", "fire", "expire"]:
    """Decide the action for a one-shot ``at`` schedule at ``now``.

    ``'wait'`` -- not due yet. ``'fire'`` -- due (on time, overdue with
    ``catch_up=True``, or overdue but within :data:`ONE_SHOT_GRACE`).
    ``'expire'`` -- overdue past the grace window with ``catch_up=False``:
    the slot was missed and won't be honoured late.
    """
    if now < at:
        return "wait"
    overdue_by = now - at
    if catch_up or overdue_by <= ONE_SHOT_GRACE:
        return "fire"
    return "expire"


def parse_schedule(spec: dict[str, Any]) -> Schedule:
    """Same as :func:`validate_schedule` but returns the parsed shape
    without re-validating. Used at tick time when we know the stored
    block already passed write-time validation.
    """
    return validate_schedule(spec)


# ── Cron parser ───────────────────────────────────────────────────

# Field bounds: (lo, hi). dow uses 0..6 with 0=Sun, matching cron(5).
_FIELD_BOUNDS: tuple[tuple[int, int], ...] = (
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day-of-month
    (1, 12),  # month
    (0, 6),  # day-of-week
)
_FIELD_NAMES: tuple[str, ...] = ("minute", "hour", "dom", "month", "dow")


def parse_cron(cron: str) -> tuple[frozenset[int], ...]:
    """Parse a 5-field cron string into per-field allowed-value sets.

    Returns a 5-tuple of frozensets in field order (minute, hour, dom,
    month, dow). A field that's ``*`` expands to its full range.

    Raises :class:`BadInput` on any malformed shape.
    """
    if not isinstance(cron, str):
        raise BadInput(f"cron must be a string, got {type(cron).__name__}")
    fields = cron.split()
    if len(fields) != 5:
        raise BadInput(
            f"cron must have 5 fields ({_FIELD_NAMES!r}), got {len(fields)}: {cron!r}",
            next="example: '0 9 * * 1' (Monday 09:00)",
        )
    return tuple(
        _parse_field(field, lo, hi, _FIELD_NAMES[i])
        for i, (field, (lo, hi)) in enumerate(zip(fields, _FIELD_BOUNDS, strict=True))
    )


def _parse_field(field: str, lo: int, hi: int, name: str) -> frozenset[int]:
    """Expand one cron field into its allowed-value set."""
    if field == "*":
        return frozenset(range(lo, hi + 1))
    out: set[int] = set()
    for chunk in field.split(","):
        if not chunk:
            raise BadInput(f"empty entry in cron {name} field: {field!r}")
        step = 1
        if "/" in chunk:
            base, _, step_str = chunk.partition("/")
            try:
                step = int(step_str)
            except ValueError as exc:
                raise BadInput(f"bad step in cron {name} field: {chunk!r}") from exc
            if step < 1:
                raise BadInput(f"cron {name} step must be >= 1: {chunk!r}")
        else:
            base = chunk
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError as exc:
                raise BadInput(f"bad range in cron {name} field: {chunk!r}") from exc
            if start > end:
                raise BadInput(f"cron {name} range out of order: {chunk!r}")
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise BadInput(f"bad value in cron {name} field: {chunk!r}") from exc
        if start < lo or end > hi:
            raise BadInput(f"cron {name} value out of range ({lo}..{hi}): {chunk!r}")
        out.update(range(start, end + 1, step))
    return frozenset(out)


# ── Shorthand translator ──────────────────────────────────────────

_DOW: dict[str, int] = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}

_EVERY_DURATION = re.compile(r"^(\d+)([mhd])$")
_EVERY_DOW_HHMM = re.compile(r"^(sun|mon|tue|wed|thu|fri|sat)\s+(\d{1,2}):(\d{2})$")


def every_to_cron(every: str) -> str:
    """Translate ``every:`` shorthand to a canonical 5-field cron string.

    Accepted shapes:

    * ``Nm`` — every N minutes (``N`` ≥ 1, ≤ 59 for the ``*/N`` form)
    * ``Nh`` — every N hours, on minute 0
    * ``Nd`` — every N days, at 00:00 (``1d`` is the only safe form;
      ``2d``+ doesn't round-trip cleanly because cron doesn't have a
      "every N days from epoch" field, only "this dom". We accept
      ``1d`` and reject the rest.)
    * ``mon HH:MM`` (et al.) — weekly, at HH:MM on that dow

    Anything else raises :class:`BadInput`.
    """
    s = every.lower()
    m = _EVERY_DURATION.match(s)
    if m:
        n_str, unit = m.group(1), m.group(2)
        n = int(n_str)
        if n < 1:
            raise BadInput(f"every: count must be >= 1, got {every!r}")
        if unit == "m":
            if n > 59:
                raise BadInput(
                    f"every:Nm: N must be <= 59 (use 1h for hourly), got {every!r}"
                )
            return f"*/{n} * * * *" if n != 1 else "* * * * *"
        if unit == "h":
            if n > 23:
                raise BadInput(
                    f"every:Nh: N must be <= 23 (use 1d for daily), got {every!r}"
                )
            return f"0 */{n} * * *" if n != 1 else "0 * * * *"
        # n followed by 'd'
        if n != 1:
            raise BadInput(
                f"every:Nd: only every:1d is supported (cron has no "
                f"reliable 'every N days' field); got {every!r}",
                next="for weekly-ish cadences use 'every: mon 09:00'",
            )
        return "0 0 * * *"
    m = _EVERY_DOW_HHMM.match(s)
    if m:
        dow = _DOW[m.group(1)]
        hour = int(m.group(2))
        minute = int(m.group(3))
        if hour > 23 or minute > 59:
            raise BadInput(f"every:dow HH:MM: HH<=23, MM<=59, got {every!r}")
        return f"{minute} {hour} * * {dow}"
    raise BadInput(
        f"unrecognised every: shorthand {every!r}",
        next=("shapes: 'Nm' / 'Nh' / '1d' / 'mon HH:MM' (sun|mon|...|sat)"),
    )


# ── Tick expansion ────────────────────────────────────────────────


def ticks_since(
    last_tick: datetime | None,
    schedule: Schedule,
    *,
    now: datetime,
) -> list[datetime]:
    """Yield each cron tick at or before ``now`` that's past ``last_tick``.

    When ``schedule.backfill_missed`` is False, only the most recent
    tick is returned (the spawner skips the rest — weather doesn't
    catch up). When True, all missed ticks since ``last_tick`` are
    returned in chronological order (birthdays catch up).

    ``last_tick`` ``None`` means "this recurring has never fired" —
    the next-tick-at-or-before-now is computed and returned alone.

    The walker steps minute-by-minute from ``last_tick`` (or 60
    minutes back from ``now`` for first-fire) up to ``now``,
    yielding any minute that satisfies the cron mask. 60-minute
    bound on first-fire keeps the walk cheap (≤60 iterations);
    realistic ``last_tick`` walks bound at the recurring's period
    (hourly → 60, daily → 1440, weekly → 10k). Caller passes ``now``
    explicitly so tests can pin time.
    """
    if schedule.cron is None:
        raise ValueError("ticks_since requires a recurring (cron) schedule, not 'at'")
    fields = parse_cron(schedule.cron)
    if last_tick is None:
        start = now - timedelta(minutes=60)
    else:
        start = last_tick + timedelta(minutes=1)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if start > now:
        return []
    cursor = start.replace(second=0, microsecond=0)
    matches: list[datetime] = []
    # Safety: don't walk beyond ~7 days of minutes (~10k) without a tick.
    # A schedule that hasn't fired in over a week with backfill=False
    # is correctly truncated to "just the most recent tick" anyway.
    max_iters = 10080
    iters = 0
    while cursor <= now and iters < max_iters:
        if _cron_matches(cursor, fields):
            matches.append(cursor)
        cursor += timedelta(minutes=1)
        iters += 1
    if not matches:
        return []
    if schedule.backfill_missed:
        return matches
    return [matches[-1]]


def _cron_matches(ts: datetime, fields: tuple[frozenset[int], ...]) -> bool:
    """True iff ``ts`` minute satisfies the 5-field cron mask."""
    minute_ok = ts.minute in fields[0]
    hour_ok = ts.hour in fields[1]
    dom_ok = ts.day in fields[2]
    month_ok = ts.month in fields[3]
    # cron(5): dow 0 = Sunday; Python's weekday() is 0=Mon..6=Sun, so
    # remap. (isoweekday() is 1..7 with 7=Sun.)
    dow_ok = (ts.weekday() + 1) % 7 in fields[4]
    return minute_ok and hour_ok and dom_ok and month_ok and dow_ok


__all__ = [
    "ONE_SHOT_GRACE",
    "Schedule",
    "every_to_cron",
    "one_shot_action",
    "parse_cron",
    "parse_schedule",
    "ticks_since",
    "validate_schedule",
]
