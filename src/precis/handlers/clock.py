"""ClockHandler — current time, dates, and "how long until" durations.

Stateless, read-only, free.  Uses stdlib ``datetime`` / ``zoneinfo`` only;
no network, no external deps.

Why this kind exists: agents are LLMs and can't tell time on their own.
They also can't compute "how many days until the end of the quarter" or
"is the meeting yesterday or tomorrow?" without a clock + calendar
oracle.  This kind is the minimal grounding primitive — current time
in any IANA timezone, plus the most-needed duration calculations,
exposed through the standard precis URI surface.

Design choices (see docs/stochastic-kinds-plan.md → Kind 4):

- ISO 8601 only for date inputs.  Ambiguous formats like
  ``01/02/2027`` are refused with a clear error naming both
  interpretations.  The DMY/MDY/YMD locale guessing game has no
  winning move; explicit dates have no failure mode.
- Default output leads with the human-readable weekday + month name,
  the single most useful fact for an LLM trying to answer
  "is this Friday?".
- The kind's ``KindSpec.description`` is a callable so the tool-enum
  description rendered to the agent surfaces *current time + a few
  ready-to-use durations* every time the schema is built — agents
  see the time and the calendar scale of "now" before invoking the
  kind for the first time.

Dispatch (path is the entire opaque path after ``clock:``):

- empty                   → default rich now (UTC + local + Unix)
- ``utc`` / ``iso``       → UTC ISO 8601
- ``local``               → server-local time
- ``<IANA-tz>``           → ``Europe/Dublin`` etc.
- ``unix``                → epoch seconds (int)
- ``unix/ms``             → epoch milliseconds
- ``date``                → ISO ``YYYY-MM-DD`` (UTC)
- ``date/<tz>``           → date in tz
- ``rfc3339``             → RFC 3339 with offset
- ``until/<iso>[/<tz>]``  → days/hours/seconds from now to a target
- ``since/<iso>``         → time elapsed since a past date
- ``between/<a>/<b>``     → duration between two ISO points
- ``/zones``              → common timezones + offsets
- ``/help``               → onboarding skill inline
- ``?format=<strftime>``  → custom strftime of current UTC

Named shorthands accepted by ``until/`` and ``since/`` and ``between/``:
``new-year``, ``christmas``, ``easter-YYYY``, ``eoy``, ``eoq``, ``eom``,
``eow``, ``tomorrow``, ``yesterday``, ``next-monday`` … ``next-sunday``.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from precis.protocol import ErrorCode, Handler, PrecisError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_KNOWN_VIEWS = frozenset({"help", "zones"})

# Set of common IANA timezones that we surface in ``/zones``.  Not
# exhaustive — the handler accepts ANY zoneinfo name, this is just the
# default cheat-sheet.
_COMMON_ZONES = (
    "UTC",
    "Europe/Dublin",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Zurich",
    "America/New_York",
    "America/Los_Angeles",
    "America/Chicago",
    "America/Sao_Paulo",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Australia/Sydney",
    "Pacific/Auckland",
)

# Refuse ambiguous date separators outright — see module docstring.
# Accept ISO 8601 (``-`` between date parts, ``T`` or space before time)
# and Unix epoch (all-digits).  Anything else with ``/`` or ``.`` as a
# separator triggers the ambiguity error.
_AMBIGUOUS_DATE_RE = re.compile(r"^\d{1,4}[/.]\d{1,4}[/.]\d{1,4}")

# Two-digit year detector for ISO-shaped inputs.  ``25-04-25`` is
# ambiguous (1925 vs 2025) and should be refused.
_TWO_DIGIT_YEAR_RE = re.compile(r"^\d{2}-\d{1,2}-\d{1,2}")

# Weekday-name shorthands for ``next-monday`` etc.
_WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> _dt.datetime:
    """Return the current UTC datetime.  Indirected for testability."""
    return _dt.datetime.now(_dt.UTC)


def _split_query(s: str) -> tuple[str, dict[str, str]]:
    """Split ``foo/bar?a=1&b=2`` into ``("foo/bar", {"a": "1", "b": "2"})``.

    Empty or missing query string yields an empty dict.  Multiple ``=``
    in a value are preserved (``?fmt=%Y-%m-%dT%H:%M:%S`` works).
    """
    if "?" not in s:
        return s, {}
    path, qs = s.split("?", 1)
    params: dict[str, str] = {}
    for kv in qs.split("&"):
        if not kv:
            continue
        if "=" in kv:
            k, v = kv.split("=", 1)
            params[k] = v
        else:
            params[kv] = ""
    return path, params


def _ensure_iso(token: str) -> _dt.datetime:
    """Parse an ISO-8601 token to an aware UTC datetime, refusing ambiguity.

    Accepted shapes:

    - ``YYYY-MM-DD``
    - ``YYYY-MM-DDTHH:MM[:SS]``
    - ``YYYY-MM-DDTHH:MM:SS+HH:MM`` (or ``Z``)
    - all-digit Unix epoch seconds

    Naive datetimes are interpreted as UTC.

    Raises:
        PrecisError: ``PARAM_INVALID`` with a clear two-option hint
            for ``DD/MM/YYYY``-style ambiguous inputs, and a generic
            "use ISO 8601" hint for everything else that doesn't parse.
    """
    token = token.strip()
    if not token:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause="missing date",
            next="provide an ISO 8601 date: e.g. 2027-01-01 or 2027-01-01T18:30",
        )

    # All-digit → Unix epoch.
    if token.isdigit() and len(token) >= 9:
        try:
            return _dt.datetime.fromtimestamp(int(token), tz=_dt.UTC)
        except (ValueError, OSError) as exc:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"unix epoch out of range: {token!r}",
            ) from exc

    # Reject ambiguous separators outright.
    if _AMBIGUOUS_DATE_RE.match(token):
        # Try to split into three integers for the helpful error.
        parts = re.split(r"[/.]", token, maxsplit=2)
        if len(parts) == 3:
            a, b, c = parts
            # Heuristic: which interpretation has a year that looks like a year?
            # Show both options regardless — refusing to guess is the point.
            iso_dmy = f"{c}-{b.zfill(2)}-{a.zfill(2)}"
            iso_mdy = f"{c}-{a.zfill(2)}-{b.zfill(2)}"
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=(
                    f"ambiguous date {token!r} — could be DMY or MDY; "
                    f"not guessing"
                ),
                next=(
                    f"use ISO 8601 (YYYY-MM-DD): try {iso_dmy!r} (DMY) "
                    f"or {iso_mdy!r} (MDY)"
                ),
            )

    # Reject two-digit years in ISO shape.
    if _TWO_DIGIT_YEAR_RE.match(token):
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=(
                f"two-digit year in {token!r} is ambiguous "
                f"(1900s vs 2000s)"
            ),
            next="use four-digit year: 2027-04-25",
        )

    # Try the standard parser.
    try:
        # ``fromisoformat`` accepts ``Z`` from 3.11+.
        dt = _dt.datetime.fromisoformat(token)
    except ValueError:
        # Fall back to date-only parse.
        try:
            d = _dt.date.fromisoformat(token)
            dt = _dt.datetime(d.year, d.month, d.day, tzinfo=_dt.UTC)
        except ValueError as exc:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"could not parse {token!r} as ISO 8601",
                next=(
                    "examples: 2027-01-01, 2027-01-01T18:30, "
                    "2027-01-01T18:30+01:00, or a Unix epoch (1830000000)"
                ),
            ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return dt


def _resolve_named(token: str, *, now: _dt.datetime) -> _dt.datetime | None:
    """Resolve named-shorthand tokens (``new-year``, ``eoq``, …).

    Returns ``None`` when ``token`` isn't a recognised name so the
    caller can fall through to ISO parsing.  All returns are UTC-aware
    datetimes.
    """
    t = token.lower().strip()
    today = now.date()

    if t in {"new-year", "newyear"}:
        # Next 1 January after now.
        return _dt.datetime(today.year + 1, 1, 1, tzinfo=_dt.UTC)
    if t == "christmas":
        c = _dt.datetime(today.year, 12, 25, tzinfo=_dt.UTC)
        return c if c > now else c.replace(year=today.year + 1)
    if t.startswith("easter-"):
        try:
            year = int(t.split("-", 1)[1])
        except ValueError:
            return None
        return _dt.datetime.combine(_easter_date(year), _dt.time(0), tzinfo=_dt.UTC)
    if t in {"eoy", "end-of-year"}:
        return _dt.datetime(today.year, 12, 31, 23, 59, 59, tzinfo=_dt.UTC)
    if t in {"eoq", "end-of-quarter"}:
        # Determine current quarter's last day.
        q_end_month = ((today.month - 1) // 3 + 1) * 3
        last_day = _last_day_of_month(today.year, q_end_month)
        return _dt.datetime(today.year, q_end_month, last_day, 23, 59, 59, tzinfo=_dt.UTC)
    if t in {"eom", "end-of-month"}:
        last_day = _last_day_of_month(today.year, today.month)
        return _dt.datetime(today.year, today.month, last_day, 23, 59, 59, tzinfo=_dt.UTC)
    if t in {"eow", "end-of-week"}:
        # ISO week ends on Sunday.
        days_to_sunday = 6 - today.weekday()
        eow = today + _dt.timedelta(days=days_to_sunday)
        return _dt.datetime(eow.year, eow.month, eow.day, 23, 59, 59, tzinfo=_dt.UTC)
    if t == "tomorrow":
        d = today + _dt.timedelta(days=1)
        return _dt.datetime(d.year, d.month, d.day, tzinfo=_dt.UTC)
    if t == "yesterday":
        d = today - _dt.timedelta(days=1)
        return _dt.datetime(d.year, d.month, d.day, tzinfo=_dt.UTC)
    if t.startswith("next-"):
        name = t[5:]
        if name in _WEEKDAY_NAMES:
            target = _WEEKDAY_NAMES[name]
            delta = (target - today.weekday()) % 7
            if delta == 0:
                delta = 7  # "next monday" on a monday means a week away
            d = today + _dt.timedelta(days=delta)
            return _dt.datetime(d.year, d.month, d.day, tzinfo=_dt.UTC)
    return None


def _last_day_of_month(year: int, month: int) -> int:
    """Return the last calendar day of a given month."""
    if month == 12:
        first_next = _dt.date(year + 1, 1, 1)
    else:
        first_next = _dt.date(year, month + 1, 1)
    return (first_next - _dt.timedelta(days=1)).day


def _easter_date(year: int) -> _dt.date:
    """Compute Western (Gregorian) Easter Sunday using Anonymous Gregorian.

    The Anonymous algorithm is exact for years 1583–4099 and is small
    enough to inline — no need to depend on ``dateutil`` for this one
    use.  https://en.wikipedia.org/wiki/Date_of_Easter#Anonymous_Gregorian_algorithm
    """
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month = (h + L - 7 * m + 114) // 31
    day = ((h + L - 7 * m + 114) % 31) + 1
    return _dt.date(year, month, day)


def _format_duration(delta: _dt.timedelta) -> str:
    """Render a timedelta in multiple resolutions for an LLM consumer.

    Always returns positive magnitude — caller is responsible for
    surfacing direction.  Output covers days/weeks-and-days, hours +
    minutes, seconds — pick whichever resolution is useful.
    """
    total_seconds = int(abs(delta).total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    weeks, day_remainder = divmod(days, 7)

    lines = []
    if weeks:
        lines.append(
            f"{days:,} days   ·   {weeks:,} weeks + {day_remainder} days"
        )
    else:
        lines.append(f"{days:,} days")
    total_hours = total_seconds // 3600
    total_minutes = (total_seconds % 3600) // 60
    lines.append(f"{total_hours:,} hours {total_minutes} minutes")
    lines.append(f"{total_seconds:,} seconds")
    return "\n".join(lines)


def _zone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name with a clean PrecisError on miss."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"unknown timezone: {name!r}",
            next="see clock:/zones for common IANA timezones",
        ) from exc


# ---------------------------------------------------------------------------
# Description callable — surfaced in the tool enum
# ---------------------------------------------------------------------------


def _live_description() -> str:
    """Build the live tool-enum description for the ``clock:`` kind.

    Called every time the MCP tool schema is built.  Returns a single
    string with current UTC time, weekday, week-of-year, day-of-year,
    plus three rotating durations chosen for broad utility.

    Errors are caught at the registry layer (see
    ``RegisteredKind.description``) so a transient failure here can't
    kill schema generation.
    """
    now = _now_utc()
    weekday = now.strftime("%A")
    iso_week = now.isocalendar().week
    doy = now.timetuple().tm_yday
    days_in_year = 366 if _is_leap_year(now.year) else 365

    # Rotating durations: new-year, eoq, christmas-or-easter.
    new_year = _dt.datetime(now.year + 1, 1, 1, tzinfo=_dt.UTC)
    days_to_ny = (new_year - now).days

    q_end_month = ((now.month - 1) // 3 + 1) * 3
    eoq = _dt.datetime(
        now.year, q_end_month, _last_day_of_month(now.year, q_end_month),
        23, 59, 59, tzinfo=_dt.UTC,
    )
    days_to_eoq = (eoq - now).days

    if now.month == 12:
        # In December, christmas is too soon to be useful — pivot to easter.
        easter = _easter_date(now.year + 1)
        easter_dt = _dt.datetime.combine(easter, _dt.time(0), tzinfo=_dt.UTC)
        third_label = f"easter-{now.year + 1}"
        days_to_third = (easter_dt - now).days
    else:
        christmas = _dt.datetime(now.year, 12, 25, tzinfo=_dt.UTC)
        if christmas < now:
            christmas = christmas.replace(year=now.year + 1)
        third_label = "christmas"
        days_to_third = (christmas - now).days

    return (
        "Current time + durations.  Now: "
        f"{weekday} {now:%Y-%m-%d %H:%M} UTC "
        f"(week {iso_week}, day {doy}/{days_in_year}).  "
        f"{days_to_ny}d to new-year · {days_to_eoq}d to eoq · "
        f"{days_to_third}d to {third_label}.  "
        "Use get(id='clock:') for full detail, clock:<tz> for a "
        "timezone, or clock:until/<ISO-date> for a custom duration."
    )


def _is_leap_year(year: int) -> bool:
    """Standard Gregorian leap-year predicate."""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class ClockHandler(Handler):
    """Handler for the ``clock:`` scheme — current time + durations.

    Agent usage::

        get(id='clock:')                       — rich now (UTC + local)
        get(id='clock:Europe/Dublin')          — time in a specific tz
        get(id='clock:until/2027-01-01')       — duration until target
        get(id='clock:since/2025-01-01')       — elapsed since past
        get(id='clock:between/2026-04-01/2026-12-31')
        get(id='clock:until/eoq')              — to end of quarter
        get(id='clock:date')                   — today's UTC date
        get(id='clock:unix')                   — epoch seconds
        get(id='clock:/zones')                 — common timezones
        get(id='clock:/help')                  — onboarding skill
    """

    scheme = "clock"
    writable = False
    views: ClassVar[set[str]] = set(_KNOWN_VIEWS)
    onboarding_skill = "clock-basics"

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
    ) -> str:
        # ``clock:`` is opaque, so the URI parser leaves the entire path
        # in ``path`` and ``view`` / ``subview`` are always None.  We
        # do all dispatch internally.
        raw = (path or "").strip()
        body, params = _split_query(raw)
        return self._dispatch(body, params)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, body: str, params: dict[str, str]) -> str:
        if body in {"", "/"}:
            return self._render_default(params)

        # Special views
        if body == "/help":
            return self._help()
        if body == "/zones":
            return self._zones()

        # Strip leading slash if present (``clock:/zones`` already
        # handled above; this catches ``clock:/utc`` shorthand).
        token = body.lstrip("/")

        if token == "utc" or token == "iso":
            return self._utc_iso()
        if token == "local":
            return self._local()
        if token == "rfc3339":
            return self._rfc3339()
        if token == "unix":
            return str(int(_now_utc().timestamp()))
        if token == "unix/ms":
            return str(int(_now_utc().timestamp() * 1000))
        if token == "date":
            return _now_utc().date().isoformat()
        if token.startswith("date/"):
            return self._date_in_tz(token[len("date/"):])
        if token.startswith("until/"):
            return self._until(token[len("until/"):])
        if token.startswith("since/"):
            return self._since(token[len("since/"):])
        if token.startswith("between/"):
            return self._between(token[len("between/"):])
        # Otherwise treat as IANA timezone name.
        return self._in_zone(token, params)

    # ------------------------------------------------------------------
    # Default render
    # ------------------------------------------------------------------

    def _render_default(self, params: dict[str, str]) -> str:
        """Rich default: weekday, ISO UTC, ISO local, Unix.

        ``?format=<strftime>`` swaps in a custom-formatted UTC line
        instead of the default block.
        """
        now = _now_utc()
        if "format" in params:
            try:
                return now.strftime(params["format"])
            except ValueError as exc:
                raise PrecisError(
                    ErrorCode.PARAM_INVALID,
                    cause=f"strftime format error: {exc}",
                    next="use Python strftime tokens, e.g. %Y-%m-%dT%H:%M",
                ) from exc

        weekday = now.strftime("%A")
        weekday_short = now.strftime("%a")
        iso_week = now.isocalendar().week
        doy = now.timetuple().tm_yday
        days_in_year = 366 if _is_leap_year(now.year) else 365

        # Local time — server timezone, falls back to UTC if zoneinfo
        # can't resolve the host's local zone.
        try:
            local_zone = ZoneInfo("localtime")  # may not exist on all hosts
        except ZoneInfoNotFoundError:
            local_zone = _dt.datetime.now().astimezone().tzinfo or _dt.UTC
        local = now.astimezone(local_zone)
        local_name = str(local_zone) if local_zone is not _dt.UTC else "UTC"
        local_abbrev = local.strftime("%Z") or ""
        local_weekday_short = local.strftime("%a")

        unix = int(now.timestamp())

        header = (
            f"🕒 {weekday}, {now:%-d %B %Y} · {now:%H:%M} UTC · "
            f"week {iso_week} · day {doy}/{days_in_year}"
        )
        utc_line = f"UTC        {now:%Y-%m-%dT%H:%M:%SZ}"
        local_line = (
            f"Local      {local:%Y-%m-%dT%H:%M:%S%z}  "
            f"({local_name}{', ' + local_abbrev if local_abbrev else ''}, "
            f"{local_weekday_short})"
        )
        unix_line = f"Unix       {unix}"

        footer = (
            "\nUse `clock:<tz>` for other timezones, "
            "`clock:until/<date>` for durations, or `clock:/zones` for the list."
        )

        # Suppress unused-variable warning for weekday_short (kept for
        # future use in a terser ``?layer=short`` mode).
        _ = weekday_short

        return "\n".join([header, "", utc_line, local_line, unix_line]) + footer

    # ------------------------------------------------------------------
    # Format-specific renders
    # ------------------------------------------------------------------

    def _utc_iso(self) -> str:
        return _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _local(self) -> str:
        local = _dt.datetime.now().astimezone()
        return local.isoformat()

    def _rfc3339(self) -> str:
        return _now_utc().isoformat().replace("+00:00", "Z")

    def _date_in_tz(self, tz_name: str) -> str:
        zone = _zone(tz_name)
        return _now_utc().astimezone(zone).date().isoformat()

    def _in_zone(self, tz_name: str, params: dict[str, str]) -> str:
        zone = _zone(tz_name)
        now = _now_utc().astimezone(zone)
        weekday = now.strftime("%A")
        return (
            f"🕒 {weekday}, {now:%-d %B %Y} · {now:%H:%M} {tz_name}\n\n"
            f"ISO 8601   {now:%Y-%m-%dT%H:%M:%S%z}\n"
            f"Unix       {int(now.timestamp())}\n"
        )

    # ------------------------------------------------------------------
    # Durations
    # ------------------------------------------------------------------

    def _resolve_target(self, token: str) -> _dt.datetime:
        """Resolve a target token to a UTC datetime via name → ISO fallback."""
        now = _now_utc()
        named = _resolve_named(token, now=now)
        if named is not None:
            return named
        return _ensure_iso(token)

    def _until(self, body: str) -> str:
        # ``until/<target>[/<tz>]``
        parts = body.split("/", 1)
        target_token = parts[0]
        tz_name = parts[1] if len(parts) > 1 else None
        target = self._resolve_target(target_token)
        if tz_name:
            target = target.astimezone(_zone(tz_name))
        now = _now_utc()
        delta = target - now
        sign = "" if delta.total_seconds() >= 0 else "-"
        passed_hint = (
            "" if delta.total_seconds() >= 0 else "  (target already passed)"
        )
        weekday = target.strftime("%A")
        iso_week = target.isocalendar().week
        body_text = _format_duration(delta)
        if sign:
            body_text = "\n".join(f"{sign}{line}" for line in body_text.splitlines())
        return (
            f"📅 duration to {target.isoformat().replace('+00:00', 'Z')}"
            f"{passed_hint}\n\n"
            f"{body_text}\n\n"
            f"That's {weekday}, in calendar week {iso_week} of {target.year}."
        )

    def _since(self, body: str) -> str:
        target = self._resolve_target(body.rstrip("/"))
        now = _now_utc()
        if target > now:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=(
                    f"clock:since/ requires a past date — "
                    f"{target.date().isoformat()} is in the future"
                ),
                next=(
                    f"use clock:until/{target.date().isoformat()} for "
                    "future targets"
                ),
            )
        delta = now - target
        weekday = target.strftime("%A")
        iso_week = target.isocalendar().week
        return (
            f"📅 elapsed since {target.isoformat().replace('+00:00', 'Z')} "
            f"({weekday}, week {iso_week} of {target.year})\n\n"
            f"{_format_duration(delta)}"
        )

    def _between(self, body: str) -> str:
        # ``between/<a>/<b>`` — both ISO or named.
        # Need to be careful about splitting because each token might
        # contain its own ``/`` (named shorthands don't, ISO dates don't,
        # but datetimes with seconds and offsets do — though in practice
        # we expect plain dates here).  Split on the LAST ``/`` first,
        # then peel off the rest.
        if "/" not in body:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause="clock:between/ requires two endpoints separated by /",
                next="example: clock:between/2026-04-01/2026-12-31",
            )
        a_str, b_str = body.split("/", 1)
        a = self._resolve_target(a_str)
        b = self._resolve_target(b_str.rstrip("/"))
        delta = b - a if b >= a else a - b
        later, earlier = (b, a) if b >= a else (a, b)
        return (
            f"📅 between\n"
            f"  earlier: {earlier.isoformat().replace('+00:00', 'Z')}\n"
            f"  later:   {later.isoformat().replace('+00:00', 'Z')}\n\n"
            f"{_format_duration(delta)}"
        )

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def _zones(self) -> str:
        now = _now_utc()
        lines = ["🌐 Common timezones (offset shown vs UTC right now):", ""]
        for name in _COMMON_ZONES:
            try:
                zone = ZoneInfo(name)
            except ZoneInfoNotFoundError:
                continue
            local = now.astimezone(zone)
            lines.append(
                f"  {name:<24} {local:%Y-%m-%dT%H:%M:%S%z}  ({local:%a})"
            )
        lines.append("")
        lines.append(
            "Any IANA timezone name works in clock:<tz> — these are just "
            "common reference points."
        )
        return "\n".join(lines)

    def _help(self) -> str:
        return (
            "# clock — current time, dates, and durations\n\n"
            "Read-only, free, stdlib-only.  Use to ground the agent in\n"
            "what time it is and how long until / since a date.\n\n"
            "## Current time\n\n"
            "- `get(id='clock:')`                — rich default (UTC + local)\n"
            "- `get(id='clock:utc')`             — ISO 8601 UTC\n"
            "- `get(id='clock:Europe/Dublin')`   — any IANA timezone\n"
            "- `get(id='clock:unix')`            — epoch seconds\n"
            "- `get(id='clock:date')`            — today's UTC date\n"
            "- `get(id='clock:date/America/New_York')` — date in tz\n"
            "- `get(id='clock:?format=%Y-%m-%d')` — custom strftime\n\n"
            "## Durations\n\n"
            "- `get(id='clock:until/2027-01-01')`     — to a date\n"
            "- `get(id='clock:until/2026-12-25T18:00')` — to a datetime\n"
            "- `get(id='clock:since/2025-01-01')`     — elapsed\n"
            "- `get(id='clock:between/2026-04-01/2026-12-31')`\n"
            "- `get(id='clock:until/eoq')`            — end of quarter\n"
            "- `get(id='clock:until/new-year')`       — to next 1 Jan\n"
            "- `get(id='clock:until/christmas')`\n"
            "- `get(id='clock:until/next-friday')`\n\n"
            "## Date format\n\n"
            "ISO 8601 only.  Ambiguous formats (`01/02/2027` could be 1 Feb\n"
            "or 2 Jan) are refused with both interpretations shown.  Use\n"
            "`YYYY-MM-DD`.  Two-digit years also refused.\n\n"
            "## Named shorthands\n\n"
            "`new-year`, `christmas`, `easter-YYYY`, `eoy`, `eoq`, `eom`,\n"
            "`eow`, `tomorrow`, `yesterday`, `next-monday` … `next-sunday`.\n"
        )
