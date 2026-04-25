"""Phase A — clock handler tests.

Covers:

- Default rich render with weekday + week + day-of-year header.
- IANA-tz, UTC, local, unix, date variants.
- Duration calculations: until / since / between with ISO and named
  shorthands (eoq, eoy, eom, eow, new-year, easter-YYYY, christmas,
  next-monday … next-sunday, tomorrow, yesterday).
- ISO 8601 parsing: rejects DD/MM/YYYY ambiguous, two-digit years,
  garbage inputs; accepts ISO date, datetime, +offset, ``Z``, Unix epoch.
- Live description callable runs and embeds current weekday + UTC.
- ``RegisteredKind.description`` resolves callable lazily (description-
  as-callable plumbing).
"""

from __future__ import annotations

import datetime as _dt

import pytest

from precis.handlers.clock import (
    ClockHandler,
    _easter_date,
    _ensure_iso,
    _last_day_of_month,
    _live_description,
    _resolve_named,
    _split_query,
)
from precis.protocol import ErrorCode, KindSpec, PrecisError
from precis.registry import KINDS, RegisteredKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(h: ClockHandler, path: str) -> str:
    return h.read(
        path=path,
        selector=None,
        view=None,
        subview=None,
        query="",
        summarize=False,
        depth=0,
        page=1,
    )


# ---------------------------------------------------------------------------
# Query-string splitting
# ---------------------------------------------------------------------------


class TestSplitQuery:
    def test_no_query(self):
        assert _split_query("foo/bar") == ("foo/bar", {})

    def test_simple_query(self):
        assert _split_query("foo?n=3") == ("foo", {"n": "3"})

    def test_multiple_params(self):
        path, params = _split_query("foo?a=1&b=2")
        assert path == "foo"
        assert params == {"a": "1", "b": "2"}

    def test_empty_path(self):
        assert _split_query("?n=3") == ("", {"n": "3"})

    def test_value_with_equals(self):
        # ``%H:%M`` etc.  Equals can survive in the value.
        assert _split_query("?fmt=%Y=%m") == ("", {"fmt": "%Y=%m"})


# ---------------------------------------------------------------------------
# ISO date parsing
# ---------------------------------------------------------------------------


class TestEnsureIso:
    def test_iso_date(self):
        dt = _ensure_iso("2027-01-01")
        assert dt.year == 2027
        assert dt.month == 1
        assert dt.day == 1
        assert dt.tzinfo is _dt.UTC

    def test_iso_datetime_naive(self):
        dt = _ensure_iso("2027-01-01T18:30")
        assert dt.tzinfo is _dt.UTC  # naive treated as UTC
        assert dt.hour == 18

    def test_iso_datetime_with_offset(self):
        dt = _ensure_iso("2027-01-01T18:30+01:00")
        assert dt.tzinfo is not None

    def test_iso_datetime_z(self):
        dt = _ensure_iso("2027-01-01T18:30Z")
        assert dt.tzinfo is not None

    def test_unix_epoch(self):
        # 2024-01-01T00:00:00Z = 1704067200
        dt = _ensure_iso("1704067200")
        assert dt.year == 2024
        assert dt.month == 1

    def test_ambiguous_dmy_mdy_rejected(self):
        with pytest.raises(PrecisError) as exc:
            _ensure_iso("01/02/2027")
        assert exc.value.code == ErrorCode.PARAM_INVALID
        assert "ambiguous" in exc.value.cause.lower()
        # The error must show both interpretations as ISO.
        assert "2027-02-01" in exc.value.next
        assert "2027-01-02" in exc.value.next

    def test_dot_separator_rejected(self):
        with pytest.raises(PrecisError):
            _ensure_iso("01.02.2027")

    def test_two_digit_year_rejected(self):
        with pytest.raises(PrecisError) as exc:
            _ensure_iso("27-04-25")
        assert exc.value.code == ErrorCode.PARAM_INVALID
        assert "two-digit" in exc.value.cause.lower()

    def test_garbage_rejected(self):
        with pytest.raises(PrecisError):
            _ensure_iso("not a date at all")

    def test_empty_rejected(self):
        with pytest.raises(PrecisError):
            _ensure_iso("")


# ---------------------------------------------------------------------------
# Named shorthand resolution
# ---------------------------------------------------------------------------


class TestResolveNamed:
    def test_new_year_returns_january_first(self):
        now = _dt.datetime(2026, 4, 25, tzinfo=_dt.UTC)
        target = _resolve_named("new-year", now=now)
        assert target == _dt.datetime(2027, 1, 1, tzinfo=_dt.UTC)

    def test_eoq_in_q2_returns_june_30(self):
        now = _dt.datetime(2026, 4, 25, tzinfo=_dt.UTC)
        target = _resolve_named("eoq", now=now)
        assert target.month == 6
        assert target.day == 30

    def test_eoq_in_q4_returns_december_31(self):
        now = _dt.datetime(2026, 11, 15, tzinfo=_dt.UTC)
        target = _resolve_named("eoq", now=now)
        assert target.month == 12
        assert target.day == 31

    def test_christmas_after_dec25_rolls_to_next_year(self):
        now = _dt.datetime(2026, 12, 26, tzinfo=_dt.UTC)
        target = _resolve_named("christmas", now=now)
        assert target.year == 2027
        assert target.month == 12
        assert target.day == 25

    def test_easter_2027(self):
        now = _dt.datetime(2026, 4, 25, tzinfo=_dt.UTC)
        target = _resolve_named("easter-2027", now=now)
        assert target == _dt.datetime(2027, 3, 28, tzinfo=_dt.UTC)

    def test_tomorrow(self):
        now = _dt.datetime(2026, 4, 25, tzinfo=_dt.UTC)
        target = _resolve_named("tomorrow", now=now)
        assert target == _dt.datetime(2026, 4, 26, tzinfo=_dt.UTC)

    def test_next_monday_when_today_is_saturday(self):
        # 2026-04-25 is a Saturday (weekday=5).  Next Monday is 27th.
        now = _dt.datetime(2026, 4, 25, tzinfo=_dt.UTC)
        target = _resolve_named("next-monday", now=now)
        assert target == _dt.datetime(2026, 4, 27, tzinfo=_dt.UTC)

    def test_next_friday_on_friday_means_a_week_later(self):
        # 2026-04-24 is a Friday.  "next-friday" → 2026-05-01.
        now = _dt.datetime(2026, 4, 24, tzinfo=_dt.UTC)
        target = _resolve_named("next-friday", now=now)
        assert target == _dt.datetime(2026, 5, 1, tzinfo=_dt.UTC)

    def test_unknown_returns_none(self):
        now = _dt.datetime(2026, 4, 25, tzinfo=_dt.UTC)
        assert _resolve_named("definitely-not-a-name", now=now) is None


# ---------------------------------------------------------------------------
# Easter algorithm spot checks
# ---------------------------------------------------------------------------


class TestEaster:
    def test_easter_2024(self):
        # 2024-03-31 (verified)
        assert _easter_date(2024) == _dt.date(2024, 3, 31)

    def test_easter_2027(self):
        assert _easter_date(2027) == _dt.date(2027, 3, 28)

    def test_easter_2030(self):
        assert _easter_date(2030) == _dt.date(2030, 4, 21)


# ---------------------------------------------------------------------------
# Last-day-of-month
# ---------------------------------------------------------------------------


class TestLastDayOfMonth:
    def test_january(self):
        assert _last_day_of_month(2026, 1) == 31

    def test_february_non_leap(self):
        assert _last_day_of_month(2026, 2) == 28

    def test_february_leap(self):
        assert _last_day_of_month(2024, 2) == 29

    def test_april(self):
        assert _last_day_of_month(2026, 4) == 30

    def test_december(self):
        assert _last_day_of_month(2026, 12) == 31


# ---------------------------------------------------------------------------
# Default render
# ---------------------------------------------------------------------------


class TestDefaultRender:
    def test_contains_weekday_and_iso_utc(self):
        h = ClockHandler()
        out = _read(h, "")
        assert "🕒" in out
        # Must surface a weekday name spelt out.
        weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday",
                    "Friday", "Saturday", "Sunday")
        assert any(w in out for w in weekdays)
        assert "UTC" in out
        assert "Unix" in out

    def test_contains_week_and_day_of_year(self):
        h = ClockHandler()
        out = _read(h, "")
        assert "week " in out
        assert "day " in out

    def test_format_param_overrides(self):
        h = ClockHandler()
        out = _read(h, "?format=%Y-%m-%d")
        # Output is exactly a date.
        assert len(out.strip()) == 10
        assert out[4] == "-" and out[7] == "-"

    def test_format_param_strftime_tokens(self):
        h = ClockHandler()
        # Verify a couple of standard tokens roundtrip cleanly.
        out = _read(h, "?format=%H:%M")
        assert ":" in out and len(out.strip()) == 5
        out2 = _read(h, "?format=%A")
        weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday",
                    "Friday", "Saturday", "Sunday")
        assert out2.strip() in weekdays


# ---------------------------------------------------------------------------
# Format-specific renders
# ---------------------------------------------------------------------------


class TestFormatVariants:
    def test_utc(self):
        h = ClockHandler()
        out = _read(h, "utc")
        assert out.endswith("Z")
        assert "T" in out

    def test_iso_alias_of_utc(self):
        h = ClockHandler()
        assert _read(h, "iso").endswith("Z")

    def test_unix_is_integer(self):
        h = ClockHandler()
        out = _read(h, "unix")
        assert out.isdigit()
        assert int(out) > 1_700_000_000  # post 2023

    def test_unix_ms(self):
        h = ClockHandler()
        out = _read(h, "unix/ms")
        assert out.isdigit()
        assert int(out) > 1_700_000_000_000

    def test_date(self):
        h = ClockHandler()
        out = _read(h, "date")
        assert len(out) == 10
        assert out[4] == "-"

    def test_date_in_tz(self):
        h = ClockHandler()
        out = _read(h, "date/America/New_York")
        assert len(out) == 10

    def test_iana_tz(self):
        h = ClockHandler()
        out = _read(h, "Europe/Dublin")
        assert "Europe/Dublin" in out

    def test_unknown_tz(self):
        h = ClockHandler()
        with pytest.raises(PrecisError) as exc:
            _read(h, "Europe/Atlantis")
        assert exc.value.code == ErrorCode.PARAM_INVALID


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------


class TestDurations:
    def test_until_iso_date(self):
        h = ClockHandler()
        out = _read(h, "until/2099-01-01")
        assert "duration" in out.lower()
        assert "days" in out
        assert "seconds" in out

    def test_until_named_eoq(self):
        h = ClockHandler()
        out = _read(h, "until/eoq")
        assert "days" in out

    def test_until_past_target_marked(self):
        h = ClockHandler()
        out = _read(h, "until/2000-01-01")
        # Target is in the past — should be flagged.
        assert "passed" in out.lower()

    def test_since_past(self):
        h = ClockHandler()
        out = _read(h, "since/2000-01-01")
        assert "elapsed since" in out.lower()

    def test_since_future_rejected(self):
        h = ClockHandler()
        with pytest.raises(PrecisError) as exc:
            _read(h, "since/2099-01-01")
        assert exc.value.code == ErrorCode.PARAM_INVALID
        assert "past" in exc.value.cause.lower()

    def test_between(self):
        h = ClockHandler()
        out = _read(h, "between/2026-01-01/2026-12-31")
        assert "earlier" in out.lower()
        assert "later" in out.lower()

    def test_between_either_order(self):
        h = ClockHandler()
        out_a = _read(h, "between/2026-01-01/2026-12-31")
        out_b = _read(h, "between/2026-12-31/2026-01-01")
        # Both must mention the same duration in days.
        # Extract the days line.
        days_a = next(line for line in out_a.splitlines() if "days" in line)
        days_b = next(line for line in out_b.splitlines() if "days" in line)
        assert days_a == days_b

    def test_between_missing_separator(self):
        h = ClockHandler()
        with pytest.raises(PrecisError):
            _read(h, "between/2026-01-01")

    def test_until_with_tz_suffix(self):
        h = ClockHandler()
        out = _read(h, "until/2099-01-01/Europe/Dublin")
        assert "days" in out


# ---------------------------------------------------------------------------
# Special views
# ---------------------------------------------------------------------------


class TestViews:
    def test_zones(self):
        h = ClockHandler()
        out = _read(h, "/zones")
        assert "UTC" in out
        assert "Europe/Dublin" in out
        assert "America/New_York" in out

    def test_help(self):
        h = ClockHandler()
        out = _read(h, "/help")
        assert "clock" in out.lower()
        assert "until" in out.lower()
        assert "ISO 8601" in out


# ---------------------------------------------------------------------------
# Live description (the "enum shows now" feature)
# ---------------------------------------------------------------------------


class TestLiveDescription:
    def test_runs_without_error(self):
        out = _live_description()
        assert isinstance(out, str)
        assert len(out) > 50

    def test_contains_weekday_and_now(self):
        out = _live_description()
        weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday",
                    "Friday", "Saturday", "Sunday")
        assert any(w in out for w in weekdays)
        assert "UTC" in out

    def test_contains_durations(self):
        out = _live_description()
        assert "new-year" in out
        assert "eoq" in out
        # Christmas in non-December months, easter in December.
        assert "christmas" in out or "easter" in out

    def test_contains_call_hints(self):
        out = _live_description()
        assert "clock:" in out


# ---------------------------------------------------------------------------
# Description-as-callable plumbing (the upstream tweak)
# ---------------------------------------------------------------------------


class TestDescriptionCallable:
    def test_string_description_unchanged(self):
        spec = KindSpec(name="x", description="static description")
        rk = RegisteredKind(spec=spec, handler_cls=ClockHandler, plugin_name="x")
        assert rk.description == "static description"

    def test_callable_description_evaluated(self):
        spec = KindSpec(name="x", description=lambda: "dynamic value")
        rk = RegisteredKind(spec=spec, handler_cls=ClockHandler, plugin_name="x")
        assert rk.description == "dynamic value"

    def test_callable_re_evaluated_each_call(self):
        # Ensure the callable runs every time (not memoised).
        counter = {"n": 0}

        def desc():
            counter["n"] += 1
            return f"call #{counter['n']}"

        spec = KindSpec(name="x", description=desc)
        rk = RegisteredKind(spec=spec, handler_cls=ClockHandler, plugin_name="x")
        assert rk.description == "call #1"
        assert rk.description == "call #2"
        assert rk.description == "call #3"

    def test_callable_failure_returns_fallback(self):
        def boom():
            raise RuntimeError("nope")

        spec = KindSpec(name="x", description=boom)
        rk = RegisteredKind(spec=spec, handler_cls=ClockHandler, plugin_name="x")
        # Must not propagate; returns fallback string.
        out = rk.description
        assert "x" in out
        assert "unavailable" in out

    def test_clock_kind_uses_callable(self):
        # Wired up correctly in registry.py.
        from precis.registry import _discover

        _discover()
        if "clock" not in KINDS:
            pytest.skip("clock kind not registered (filtered or import failure)")
        clock_kind = KINDS["clock"]
        # First call returns one snapshot, second call may differ
        # (the seconds field will, in particular).  Just verify it
        # works and contains the expected substrings.
        desc = clock_kind.description
        assert "Now:" in desc
        assert "UTC" in desc
