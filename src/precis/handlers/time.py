"""Stateless clock / timestamp kind. No DB, no network, stdlib only.

``get(kind='time')`` answers "what time is it" — now in UTC (ISO 8601,
``Z`` suffix), the server's local time and zone, and epoch seconds.
With ``q=`` (or ``id=``) the same three lines describe the *parsed*
instant instead: an ISO 8601 / RFC 2822 timestamp, a bare epoch, or a
small relative grammar (``now``, ``today``, ``tomorrow``, ``in 3 hours``,
``2 days ago``, ``next monday``). ``args={'to': '<IANA zone>'}`` adds a
line in that zone; ``args={'format': '<strftime>'}`` adds a formatted
line; ``args={'from': '<IANA zone>'}`` says which zone a *naive* input
(no offset) is in — the default is UTC, matching the house convention
that timestamps are UTC unless labelled otherwise.

Deliberately stdlib-only (:mod:`datetime`, :mod:`zoneinfo`,
:mod:`email.utils`): a natural-language date library would widen what
the kind accepts but make the accepted grammar depend on a third-party
heuristic the agent cannot see. The grammar here is small enough to
list in full in the ``precis-time-help`` skill, and a miss returns a
``BadInput`` with the canonical shapes rather than a plausible-but-wrong
instant. The clock and local zone are constructor-injectable so tests
are deterministic.
"""

from __future__ import annotations

import calendar
import os
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.protocol import Handler, KindSpec
from precis.response import Response

Clock = Callable[[], datetime]

_NEXT_NOW = "get(kind='time')"
_NEXT_PARSE = "get(kind='time', q='2026-10-09T14:30:00Z')"
_NEXT_CONVERT = "get(kind='time', q='now', args={'to': 'Asia/Tokyo'})"


class TimeHandler(Handler):
    #: args= keys each verb reads out of ``args`` (gr475332); dispatch
    #: rejects any other key. Keep in step with the verb bodies.
    ARGS_KEYS: ClassVar[dict[str, frozenset[str]]] = {
        "get": frozenset({"from", "to", "format"}),
    }

    #: clock/timestamp arithmetic; stores nothing: the agent-write secret gate (dispatch) skips this kind.
    stores_opaque_text: ClassVar[bool] = True
    spec: ClassVar[KindSpec] = KindSpec(
        kind="time",
        title="Time",
        description=(
            "Local clock and timestamp arithmetic, stateless. No args: now "
            "in UTC (ISO 8601, Z), the server's local time + zone, epoch "
            "seconds. q=<timestamp or expression> parses it instead: ISO "
            "8601, RFC 2822, epoch seconds/millis, or 'now' / 'today' / "
            "'tomorrow' / 'in 3 hours' / '2 days ago' / 'next monday'. "
            "args={'to': 'Asia/Tokyo'} converts to an IANA zone; "
            "args={'format': '%Y-%m-%d'} renders with strftime; "
            "args={'from': 'Europe/Zurich'} fixes the zone of a naive input "
            "(default UTC)."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=False,
        placement="system",
    )

    def __init__(
        self,
        *,
        hub: Hub,
        clock: Clock | None = None,
        local_zone: tzinfo | None = None,
    ) -> None:
        # Stateless — ``hub`` is taken for signature uniformity (see calc).
        # ``clock`` / ``local_zone`` exist so tests pin the instant and the
        # server zone instead of reading the wall clock.
        _ = hub
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._local_zone: tzinfo = local_zone or _detect_local_zone()

    def get(
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        args = args or {}
        if not isinstance(args, dict):
            raise BadInput("args must be a dict", next=_NEXT_CONVERT)
        # Whole seconds: sub-second noise on "now" costs tokens and tells
        # an agent nothing; explicit fractional input (millis epoch) keeps it.
        now = self._clock().astimezone(UTC).replace(microsecond=0)
        source_zone = _zone_arg(args.get("from"), "from") or UTC
        expr = _coerce_expr(id, q)
        instant = (
            now if expr is None else parse_instant(expr, now=now, zone=source_zone)
        )
        instant = instant.astimezone(UTC)

        lines = [
            f"utc:   {_iso_utc(instant)} ({instant.strftime('%A')})",
            f"local: {instant.astimezone(self._local_zone).isoformat()} "
            f"({_zone_label(self._local_zone, instant)})",
            f"epoch: {_epoch(instant)}",
        ]
        if expr is not None:
            lines.insert(0, f"parsed: {expr!r} → {_relative(instant, now)}")
        target = _zone_arg(args.get("to"), "to")
        if target is not None:
            converted = instant.astimezone(target)
            lines.append(f"{_zone_label(target, converted)}: {converted.isoformat()}")
        fmt = args.get("format")
        if fmt is not None:
            if not isinstance(fmt, str) or not fmt:
                raise BadInput(
                    "args['format'] must be a non-empty strftime string",
                    next="get(kind='time', args={'format': '%Y-%m-%d %H:%M'})",
                )
            shown = instant.astimezone(target) if target is not None else instant
            try:
                rendered = shown.strftime(fmt)
            except ValueError as e:
                raise BadInput(
                    f"bad strftime format {fmt!r}: {e}",
                    next="get(kind='time', args={'format': '%Y-%m-%d %H:%M'})",
                ) from e
            lines.append(f"format: {rendered}")
        return Response(body="\n".join(lines))


# ── parsing ─────────────────────────────────────────────────────────

# ``hours`` / ``hrs`` / ``hr`` / ``h`` all map to one canonical unit; the
# canonical units then resolve to a fixed number of seconds, or (months,
# years) to a count of calendar months applied by :func:`_add_months`.
_UNIT_ALIASES: dict[str, str] = {
    alias: canon
    for canon, aliases in {
        "s": ("s", "sec", "secs", "second", "seconds"),
        "m": ("m", "min", "mins", "minute", "minutes"),
        "h": ("h", "hr", "hrs", "hour", "hours"),
        "d": ("d", "day", "days"),
        "w": ("w", "week", "weeks"),
        "mo": ("mo", "month", "months"),
        "y": ("y", "yr", "yrs", "year", "years"),
    }.items()
    for alias in aliases
}
_UNIT_SECONDS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_CALENDAR_MONTHS: dict[str, int] = {"mo": 1, "y": 12}
_UNIT_RE = r"(?:s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?|w|weeks?|mo|months?|y|yrs?|years?)"
# ``3 hours``, ``2h``, ``1 day 3 hours``, ``2h30m`` — one or more (number,
# unit) pairs, whitespace / commas / ``and`` between them optional.
_SPAN_RE = re.compile(
    rf"^(?:\s*(?:,|and)?\s*(\d+(?:\.\d+)?)\s*({_UNIT_RE})(?![a-z]))+\s*$", re.IGNORECASE
)
_SPAN_PART_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*({_UNIT_RE})(?![a-z])", re.IGNORECASE)
_IN_RE = re.compile(r"^in\s+(.+)$", re.IGNORECASE)
_AGO_RE = re.compile(r"^(.+?)\s+ago$", re.IGNORECASE)
_FROM_NOW_RE = re.compile(r"^(.+?)\s+from\s+now$", re.IGNORECASE)
_WEEKDAY_RE = re.compile(
    r"^(?:(next|last|this)\s+)?"
    r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:rs(?:day)?)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)$",
    re.IGNORECASE,
)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# ``friday 17:00`` / ``tomorrow at 9am`` — a day phrase plus a time of day.
_DAY_TIME_RE = re.compile(
    r"^(?P<day>.+?)\s+(?:at\s+)?(?P<tod>\d{1,2}(?::\d{2}(?::\d{2})?)?(?:\s*[ap]m)?)$",
    re.IGNORECASE,
)
_EPOCH_RE = re.compile(r"^@?(\d{9,})(?:\.(\d+))?$")
_ZONEINFO_KEY_RE = re.compile(r"zoneinfo[^/]*/(.+)$")
_TRAILING_ZONE_RE = re.compile(
    r"^(.*\S)\s+([A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)+|UTC|GMT|Z)$"
)
# Common non-ISO spellings. Date-only and date+time variants are
# generated from these in :func:`_strptime_formats`.
_DATE_FORMATS = (
    "%Y/%m/%d",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y %b %d",
    "%Y %B %d",
)
_TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I %p")


def _strptime_formats() -> tuple[str, ...]:
    out: list[str] = []
    for d in _DATE_FORMATS:
        out.append(d)
        out.extend(f"{d} {t}" for t in _TIME_FORMATS)
    return tuple(out)


_STRPTIME_FORMATS = _strptime_formats()


def parse_instant(expr: str, *, now: datetime, zone: tzinfo) -> datetime:
    """Parse ``expr`` to an aware datetime.

    ``now`` anchors relative expressions; ``zone`` is applied to a naive
    result (``2026-10-09 14:30``, ``today``) — the caller passes UTC
    unless the agent said ``args={'from': ...}``. Raises :class:`BadInput`
    listing the accepted shapes on a miss.
    """
    text = expr.strip()
    if not text:
        raise BadInput("time: empty expression", next=_NEXT_NOW)
    zone_given = False
    # ``2026-10-09 14:30 Europe/Zurich`` — a trailing zone token overrides
    # ``zone`` for this input only.
    m = _TRAILING_ZONE_RE.match(text)
    if m is not None:
        tz = _try_zone(m.group(2))
        if tz is not None:
            text, zone = m.group(1), tz
            zone_given = True
    low = text.lower()
    local_now = now.astimezone(zone)

    if low == "now":
        return now
    day = _day_phrase(low, local_now, zone)
    if day is not None:
        return day
    m = _DAY_TIME_RE.match(low)
    if m is not None:
        tod = _time_of_day(m.group("tod"))
        base = _day_phrase(m.group("day"), local_now, zone)
        if tod is not None and base is not None:
            return datetime.combine(base.date(), tod, zone)
    rel = _relative_span(low)
    if rel is not None:
        if zone_given:
            raise BadInput(
                f"time: {text!r} is relative to the clock, so a zone suffix "
                "has no effect; drop it and use args={'to': '<IANA zone>'} "
                "to render the result in that zone.",
                next=_NEXT_CONVERT,
            )
        sign, span = rel
        return _shift(now, span, sign)
    m = _EPOCH_RE.match(text)
    if m is not None:
        digits, frac = m.group(1), m.group(2)
        secs = float(f"{digits}.{frac or 0}")
        if len(digits) >= 13:  # milliseconds
            secs /= 1000.0
        try:
            return datetime.fromtimestamp(secs, UTC)
        except (OverflowError, OSError, ValueError) as e:
            raise BadInput(f"epoch out of range: {text!r}", next=_NEXT_PARSE) from e
    parsed = _parse_absolute(text)
    if parsed is None:
        raise BadInput(
            f"could not parse time expression {text!r}. Accepted: ISO 8601 "
            "(2026-10-09T14:30:00Z, 2026-10-09 14:30, 2026-10-09), RFC 2822, "
            "epoch seconds or millis, or now / today / tomorrow / yesterday / "
            "in 3 hours / 2 days ago / next monday / friday 17:00 / tomorrow 9am / 9 Oct 2026 14:30.",
            next=_NEXT_PARSE,
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def _day_phrase(low: str, local_now: datetime, zone: tzinfo) -> datetime | None:
    """``today`` / ``tomorrow`` / ``yesterday`` / ``[next|last|this] <weekday>``
    → midnight of that day in ``zone``; ``None`` if ``low`` is neither."""
    if low in ("today", "tomorrow", "yesterday"):
        shift = {"today": 0, "tomorrow": 1, "yesterday": -1}[low]
        return datetime.combine(local_now.date() + timedelta(days=shift), time(), zone)
    m = _WEEKDAY_RE.match(low)
    if m is not None:
        return _weekday(local_now, m.group(1), m.group(2)[:3], zone)
    return None


def _time_of_day(text: str) -> time | None:
    norm = re.sub(r"\s*([AP]M)$", r" \1", text.strip().upper())
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(norm, fmt).time()
        except ValueError:
            continue
    return None


def _parse_absolute(text: str) -> datetime | None:
    # ``Z`` is accepted by fromisoformat on 3.11+, but a lowercase ``z``
    # or a trailing `` UTC`` is not — normalise those two spellings.
    iso = re.sub(r"(?i)z$", "+00:00", text)
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        pass
    try:
        return datetime.combine(date.fromisoformat(text), time())
    except ValueError:
        pass
    for fmt in _STRPTIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None


def _relative_span(low: str) -> tuple[int, dict[str, float]] | None:
    """``in 3 hours`` → ``(+1, span)``; ``2 days ago`` → ``(-1, span)``;
    ``3h from now`` → ``(+1, span)``. ``span`` maps ``seconds`` and
    ``months`` to float totals."""
    for pattern, sign in ((_IN_RE, 1), (_FROM_NOW_RE, 1), (_AGO_RE, -1)):
        m = pattern.match(low)
        if m is None or not _SPAN_RE.match(m.group(1)):
            continue
        span = {"seconds": 0.0, "months": 0.0}
        for num, unit in _SPAN_PART_RE.findall(m.group(1)):
            key = _UNIT_ALIASES[unit.lower()]
            if key in _UNIT_SECONDS:
                span["seconds"] += float(num) * _UNIT_SECONDS[key]
            else:
                span["months"] += float(num) * _CALENDAR_MONTHS[key]
        return sign, span
    return None


def _shift(now: datetime, span: dict[str, float], sign: int) -> datetime:
    out = now + timedelta(seconds=sign * span["seconds"])
    months = round(span["months"])
    if months:
        out = _add_months(out, sign * months)
    return out


def _add_months(dt: datetime, months: int) -> datetime:
    """Calendar month arithmetic with end-of-month clamping (Jan 31 + 1
    month → Feb 28/29), the convention agents expect from "in 1 month"."""
    total = dt.year * 12 + (dt.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _weekday(
    local_now: datetime, qualifier: str | None, day: str, zone: tzinfo
) -> datetime:
    """``next monday`` / ``monday`` → the next such day strictly after today;
    ``last monday`` → the most recent one strictly before today; ``this
    monday`` → this calendar week's (Mon-Sun) occurrence. Midnight in
    ``zone``."""
    target = _WEEKDAYS.index(day)
    today = local_now.date()
    delta = (target - today.weekday()) % 7
    if qualifier == "last":
        delta = delta - 7 if delta else -7
    elif qualifier == "this":
        delta = target - today.weekday()
    elif delta == 0:
        delta = 7
    return datetime.combine(today + timedelta(days=delta), time(), zone)


# ── zones ───────────────────────────────────────────────────────────


def _try_zone(name: str) -> tzinfo | None:
    if name.upper() in ("UTC", "Z", "GMT"):
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _zone_arg(value: Any, key: str) -> tzinfo | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise BadInput(f"args[{key!r}] must be an IANA zone name", next=_NEXT_CONVERT)
    tz = _try_zone(value.strip())
    if tz is None:
        raise BadInput(
            f"unknown zone {value!r} — use an IANA name such as "
            "'Europe/Zurich', 'America/New_York', 'Asia/Tokyo', or 'UTC'",
            next=_NEXT_CONVERT,
        )
    return tz


def _detect_local_zone() -> tzinfo:
    """The server's zone as an IANA :class:`ZoneInfo` when discoverable
    (``TZ`` env, then the ``/etc/localtime`` symlink), else the fixed
    offset the C library reports — still correct, named by abbreviation."""
    tz_env = os.environ.get("TZ", "").strip().lstrip(":")
    if tz_env:
        tz = _try_zone(tz_env)
        if tz is not None:
            return tz
    # ``/etc/localtime`` is a symlink into a zoneinfo tree on Linux and
    # macOS; the key is whatever follows the ``zoneinfo…/`` directory.
    # Read the raw link first (macOS's fully-resolved path lands in
    # ``zoneinfo.default/``), then the resolved one.
    localtime = Path("/etc/localtime")
    candidates: list[str] = []
    try:
        if localtime.is_symlink():
            candidates.append(os.readlink(localtime))
        candidates.append(localtime.resolve().as_posix())
    except OSError:
        pass
    for path in candidates:
        m = _ZONEINFO_KEY_RE.search(path)
        if m is not None:
            tz = _try_zone(m.group(1))
            if tz is not None:
                return tz
    return datetime.now(UTC).astimezone().tzinfo or UTC


def _zone_label(tz: tzinfo, at: datetime) -> str:
    key = getattr(tz, "key", None)
    if isinstance(key, str):
        return key
    if tz is UTC:
        return "UTC"
    return at.astimezone(tz).tzname() or "local"


# ── rendering ───────────────────────────────────────────────────────


def _coerce_expr(id: str | int | None, q: str | None) -> str | None:
    if isinstance(id, str) and id.strip():
        return id.strip()
    if isinstance(id, int):
        return str(id)
    if isinstance(q, str) and q.strip():
        return q.strip()
    return None


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _epoch(dt: datetime) -> str:
    ts = dt.timestamp()
    return str(int(ts)) if ts == int(ts) else f"{ts:.3f}"


def _relative(instant: datetime, now: datetime) -> str:
    """``in 3h`` / ``2d 4h ago`` / ``now`` — the parsed instant relative to
    the clock, so an agent sees at a glance which way it resolved."""
    delta = round((instant - now).total_seconds())
    if delta == 0:
        return "now"
    mag = abs(delta)
    parts: list[str] = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        n, mag = divmod(mag, size)
        if n:
            parts.append(f"{n}{label}")
        if len(parts) == 2:
            break
    span = " ".join(parts)
    return f"in {span}" if delta > 0 else f"{span} ago"
