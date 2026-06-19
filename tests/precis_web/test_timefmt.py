"""Unit tests for the shared relative/absolute timestamp formatting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("fastapi")  # keeps the suite skippable without web extra

from precis_web.timefmt import abs_ts, age_seconds, ago


def test_ago_buckets() -> None:
    now = datetime.now(UTC)
    assert ago(now - timedelta(seconds=5)).endswith("s ago")
    assert ago(now - timedelta(minutes=3)).endswith("m ago")
    assert ago(now - timedelta(hours=4)).endswith("h ago")
    assert ago(now - timedelta(days=5)).endswith("d ago")


def test_ago_accepts_iso_string() -> None:
    # stub_backlog stringifies timestamps before the view sees them.
    iso = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    assert ago(iso) == "2h ago"


def test_ago_handles_z_suffix_and_naive() -> None:
    # 'Z' suffix and tz-naive strings both coerce to UTC, no crash.
    assert ago("2026-06-19T11:00:00Z") != ""
    assert ago("2026-06-19T11:00:00") != ""


def test_ago_empty_and_unparseable_return_blank() -> None:
    assert ago(None) == ""
    assert ago("") == ""
    assert ago("not-a-date") == ""
    assert ago(12345) == ""


def test_ago_future_clamps_to_zero() -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    assert ago(future) == "0s ago"


def test_abs_ts_formats_utc() -> None:
    dt = datetime(2026, 6, 14, 9, 5, tzinfo=UTC)
    assert abs_ts(dt) == "2026-06-14 09:05 UTC"
    assert abs_ts("2026-06-14T09:05:00+00:00") == "2026-06-14 09:05 UTC"
    assert abs_ts(None) == ""


def test_age_seconds_none_for_garbage() -> None:
    assert age_seconds(None) is None
    assert age_seconds("nope") is None
    assert age_seconds(datetime.now(UTC)) is not None
