"""Deterministic UTC windows, with the existing point response kept intact."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.time import TimeHandler, TimeWindow, resolve_window

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
JANUARY = datetime(2026, 1, 15, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("expr", "now", "since", "until", "widened"),
    [
        ("recently", NOW, "2026-10-20T12:00:00Z", "2026-11-03T12:00:00Z", True),
        ("last week", JANUARY, "2026-01-05T00:00:00Z", "2026-01-12T00:00:00Z", False),
        ("last month", JANUARY, "2025-12-01T00:00:00Z", "2026-01-01T00:00:00Z", False),
        (
            "about a month ago",
            NOW,
            "2026-08-05T12:00:00Z",
            "2026-10-04T12:00:00Z",
            True,
        ),
        (
            "around a month ago",
            NOW,
            "2026-08-05T12:00:00Z",
            "2026-10-04T12:00:00Z",
            True,
        ),
        (
            "a few months ago",
            NOW,
            "2026-05-07T12:00:00Z",
            "2026-09-04T12:00:00Z",
            True,
        ),
        ("last year", JANUARY, "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z", False),
        ("2026-10-01", NOW, "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", False),
        (
            "2026-10-01..2026-10-15",
            NOW,
            "2026-10-01T00:00:00Z",
            "2026-10-15T00:00:00Z",
            False,
        ),
        ("2 days ago", NOW, "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z", True),
        (
            "2026-10-01T00:30:00+02:00",
            NOW,
            "2026-09-30T00:00:00Z",
            "2026-10-01T00:00:00Z",
            True,
        ),
    ],
)
def test_resolve_window_shapes(
    expr: str, now: datetime, since: str, until: str, widened: bool
) -> None:
    window = resolve_window(expr, now=now, zone=UTC)
    assert isinstance(window, TimeWindow)
    assert window.since == datetime.fromisoformat(since)
    assert window.until == datetime.fromisoformat(until)
    assert window.since.tzinfo is UTC
    assert window.until.tzinfo is UTC
    assert window.source == expr
    assert window.widened is widened


@pytest.mark.parametrize(
    ("expr", "now", "since", "until"),
    [
        (
            "last week",
            datetime(2026, 1, 5, tzinfo=UTC),
            "2025-12-29T00:00:00Z",
            "2026-01-05T00:00:00Z",
        ),
        (
            "last month",
            datetime(2024, 3, 1, tzinfo=UTC),
            "2024-02-01T00:00:00Z",
            "2024-03-01T00:00:00Z",
        ),
        (
            "last year",
            datetime(2025, 1, 1, tzinfo=UTC),
            "2024-01-01T00:00:00Z",
            "2025-01-01T00:00:00Z",
        ),
    ],
)
def test_calendar_boundaries(expr: str, now: datetime, since: str, until: str) -> None:
    window = resolve_window(expr, now=now, zone=UTC)
    assert window.since == datetime.fromisoformat(since)
    assert window.until == datetime.fromisoformat(until)
    assert window.widened is False


@pytest.mark.parametrize(
    ("expr", "since", "until"),
    [
        ("2026-03-29", "2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z"),
        (
            "2026-03-29..2026-03-30",
            "2026-03-28T23:00:00Z",
            "2026-03-29T22:00:00Z",
        ),
        ("last week", "2026-03-22T23:00:00Z", "2026-03-29T22:00:00Z"),
        ("last month", "2026-02-28T23:00:00Z", "2026-03-31T22:00:00Z"),
    ],
)
def test_source_zone_calendar_bounds_survive_dst(
    expr: str, since: str, until: str
) -> None:
    window = resolve_window(
        expr,
        now=datetime(2026, 4, 1, 12, tzinfo=UTC),
        zone=ZoneInfo("Europe/Zurich"),
    )
    assert window.since == datetime.fromisoformat(since)
    assert window.until == datetime.fromisoformat(until)
    assert window.since.tzinfo is UTC
    assert window.until.tzinfo is UTC


def test_fuzzy_duration_uses_elapsed_utc_days() -> None:
    window = resolve_window(
        "recently",
        now=datetime(2026, 4, 1, 14, tzinfo=ZoneInfo("Europe/Zurich")),
        zone=ZoneInfo("Europe/Zurich"),
    )
    assert window.since == datetime(2026, 3, 18, 12, tzinfo=UTC)
    assert window.until == datetime(2026, 4, 1, 12, tzinfo=UTC)


def test_naive_clock_is_interpreted_as_utc() -> None:
    window = resolve_window(
        "recently",
        now=datetime(2026, 4, 1, 12),
        zone=ZoneInfo("Europe/Zurich"),
    )
    assert window.since == datetime(2026, 3, 18, 12, tzinfo=UTC)
    assert window.until == datetime(2026, 4, 1, 12, tzinfo=UTC)


def test_window_preserves_source_and_is_frozen() -> None:
    expr = "  Around A Month Ago  "
    window = resolve_window(expr, now=NOW, zone=UTC)
    assert window.source == expr
    assert window.since == datetime(2026, 8, 5, 12, tzinfo=UTC)
    with pytest.raises(FrozenInstanceError):
        window.__setattr__("since", NOW)


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "sometime before lunch",
        "in 3 parsecs",
        "2026-02-30",
        "2026-10-15..2026-10-01",
        "2026-10-01..2026-10-01",
        "2026-10-01..2026-02-30",
        "2026-10-01..",
        "..2026-10-15",
        "2026-10-01..2026-10-15..2026-10-20",
        "9999-12-31",
        "in 999999 years",
    ],
)
def test_window_refusal_names_forms_and_explicit_bounds(expr: str) -> None:
    with pytest.raises(BadInput) as caught:
        resolve_window(expr, now=NOW, zone=UTC)
    message = str(caught.value)
    for form in (
        "ISO",
        "recently",
        "last week",
        "last month",
        "about a month ago",
        "around a month ago",
        "a few months ago",
        "last year",
        "2 days ago",
    ):
        assert form in message
    assert caught.value.next == (
        "search(kind='memory', view='count', args={'since': "
        "'2026-10-01T00:00:00Z', 'until': '2026-10-15T00:00:00Z'})"
    )


def test_handler_echoes_fuzzy_window_and_explicit_bounds() -> None:
    handler = TimeHandler(hub=Hub(), clock=lambda: NOW, local_zone=UTC)
    assert handler.get(q="about a month ago").body == (
        "window: 2026-08-05T12:00:00Z … 2026-10-04T12:00:00Z "
        "(from 'about a month ago', widened)\n"
        "since='2026-08-05T12:00:00Z', until='2026-10-04T12:00:00Z' "
        "(until exclusive)"
    )


def test_handler_echoes_explicit_window_without_widening() -> None:
    handler = TimeHandler(hub=Hub(), clock=lambda: NOW, local_zone=UTC)
    assert handler.get(q="2026-10-01..2026-10-15").body == (
        "window: 2026-10-01T00:00:00Z … 2026-10-15T00:00:00Z "
        "(from '2026-10-01..2026-10-15')\n"
        "since='2026-10-01T00:00:00Z', until='2026-10-15T00:00:00Z' "
        "(until exclusive)"
    )


def test_handler_calendar_window_honours_from_zone() -> None:
    handler = TimeHandler(
        hub=Hub(),
        clock=lambda: datetime(2026, 4, 1, 12, tzinfo=UTC),
        local_zone=UTC,
    )
    assert handler.get(q="last month", args={"from": "Europe/Zurich"}).body == (
        "window: 2026-02-28T23:00:00Z … 2026-03-31T22:00:00Z "
        "(from 'last month')\n"
        "since='2026-02-28T23:00:00Z', until='2026-03-31T22:00:00Z' "
        "(until exclusive)"
    )


def test_handler_unknown_phrase_refusal_includes_windows_and_bounds() -> None:
    handler = TimeHandler(hub=Hub(), clock=lambda: NOW, local_zone=UTC)
    with pytest.raises(BadInput) as caught:
        handler.get(q="sometime before lunch")
    message = str(caught.value)
    for form in ("recently", "last month", "about a month ago", "a few months ago"):
        assert form in message
    assert caught.value.next == (
        "search(kind='memory', view='count', args={'since': "
        "'2026-10-01T00:00:00Z', 'until': '2026-10-15T00:00:00Z'})"
    )


@pytest.mark.parametrize("args", [{"to": "Asia/Tokyo"}, {"format": "%Y-%m-%d"}])
def test_window_refuses_point_only_rendering_options(args: dict[str, str]) -> None:
    handler = TimeHandler(hub=Hub(), clock=lambda: NOW, local_zone=UTC)
    with pytest.raises(BadInput) as caught:
        handler.get(q="recently", args=args)
    assert caught.value.next == "get(kind='time', q='recently')"


@pytest.mark.parametrize("args", [{"to": "Asia/Tokyo"}, {"format": "%Y-%m-%d"}])
def test_window_option_refusal_preserves_from_zone(args: dict[str, str]) -> None:
    handler = TimeHandler(hub=Hub(), clock=lambda: NOW, local_zone=UTC)
    with pytest.raises(BadInput) as caught:
        handler.get(q="recently", args={"from": "Europe/Zurich", **args})
    assert caught.value.next == (
        "get(kind='time', q='recently', args={'from': 'Europe/Zurich'})"
    )


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        (
            "2 days ago",
            "parsed: '2 days ago' → 2d ago\n"
            "utc:   2026-10-07T14:30:00Z (Wednesday)\n"
            "local: 2026-10-07T16:30:00+02:00 (Europe/Zurich)\n"
            "epoch: 1791383400",
        ),
        (
            "2026-10-09",
            "parsed: '2026-10-09' → 14h 30m ago\n"
            "utc:   2026-10-09T00:00:00Z (Friday)\n"
            "local: 2026-10-09T02:00:00+02:00 (Europe/Zurich)\n"
            "epoch: 1791504000",
        ),
    ],
)
def test_existing_point_output_is_unchanged(expr: str, expected: str) -> None:
    handler = TimeHandler(
        hub=Hub(),
        clock=lambda: datetime(2026, 10, 9, 14, 30, tzinfo=UTC),
        local_zone=ZoneInfo("Europe/Zurich"),
    )
    assert handler.get(q=expr).body == expected


def test_rfc_comment_with_dots_stays_a_point_query() -> None:
    handler = TimeHandler(hub=Hub(), clock=lambda: NOW, local_zone=UTC)
    body = handler.get(q="Fri, 09 Oct 2026 14:30:00 +0200 (sent..received)").body
    assert "utc:   2026-10-09T12:30:00Z" in body


@pytest.mark.parametrize(
    "expr", ["2026-10-01..", "..2026-10-15", "2026-10-01..2026-10-15..2026-10-20"]
)
def test_handler_refuses_malformed_ranges_as_windows(expr: str) -> None:
    handler = TimeHandler(hub=Hub(), clock=lambda: NOW, local_zone=UTC)
    with pytest.raises(BadInput, match="could not resolve time window"):
        handler.get(q=expr)
