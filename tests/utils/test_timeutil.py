"""Unit tests for the shared naive/aware → UTC coercion.

The four input shapes that reach it in the wild, and what each one is
protecting: a tz-aware datetime from psycopg, a *naive* one from a value that
round-tripped through a string, an ISO string with an offset (API headers), and
something unparseable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from precis.utils.timeutil import as_utc


def test_aware_datetime_passes_through_as_utc() -> None:
    dt = datetime(2026, 9, 18, 11, 30, tzinfo=UTC)
    assert as_utc(dt) == dt


def test_naive_datetime_is_assumed_utc_not_local() -> None:
    # Everything this system writes is UTC, so a naive value is a UTC value
    # that lost its label — never a local one. Labelling it must not shift
    # the clock reading.
    naive = datetime(2026, 9, 18, 11, 30)
    got = as_utc(naive)
    assert got is not None
    assert got.tzinfo is UTC
    assert got.hour == 11 and got.minute == 30


def test_offset_bearing_value_is_converted_not_relabelled() -> None:
    """The bug this exists to stop: an offset-bearing value printed under a
    hardcoded "UTC" label at the wrong hour (budget/quota.py::_fmt_reset)."""
    ist = timezone(timedelta(hours=5, minutes=30))
    got = as_utc(datetime(2026, 9, 18, 17, 0, tzinfo=ist))
    assert got is not None
    assert got.hour == 11 and got.minute == 30  # 17:00+05:30 -> 11:30Z
    assert got.utcoffset() == timedelta(0)


def test_offset_bearing_string_is_converted() -> None:
    got = as_utc("2026-09-18T17:00:00+05:30")
    assert got is not None
    assert (got.hour, got.minute) == (11, 30)


def test_z_suffix_string_parses() -> None:
    got = as_utc("2026-09-18T11:30:00Z")
    assert got is not None
    assert got.utcoffset() == timedelta(0)
    assert got.hour == 11


def test_naive_string_is_assumed_utc() -> None:
    got = as_utc("2026-09-18T11:30:00")
    assert got is not None
    assert got.tzinfo is UTC
    assert got.hour == 11


def test_surrounding_whitespace_tolerated() -> None:
    assert as_utc("  2026-09-18T11:30:00Z  ") == as_utc("2026-09-18T11:30:00Z")


def test_unusable_input_is_none_never_raises() -> None:
    # Display and age-check paths: an unreadable stamp degrades to "unknown"
    # rather than aborting the request.
    for value in (None, "", "   ", "not a date", 17, [], object()):
        assert as_utc(value) is None


def test_result_supports_arithmetic_against_now() -> None:
    """The TypeError this prevents: ``datetime.now(UTC) - naive`` raises, and
    the call sites only caught ValueError around the parse."""
    then = as_utc("2026-09-18T11:30:00")  # naive on purpose
    assert then is not None
    assert isinstance(datetime.now(UTC) - then, timedelta)
