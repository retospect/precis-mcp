"""``_parse_window_bound`` accepts the same span spellings as the
source-search primitive (gr735598: ``search(kind='job', since='30m')``
style relative windows must not die with "must be an ISO date/timestamp"
on any path that reads a raw string — the count views, closed windows
and ingest bounds that share this parser)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.errors import BadInput
from precis.handlers._numeric_ref import _parse_window_bound
from precis.utils.time_span import SPAN_FORMS

NOW_FROZEN = datetime(2026, 11, 19, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30m", NOW_FROZEN - timedelta(minutes=30)),
        ("2h", NOW_FROZEN - timedelta(hours=2)),
        ("3d", NOW_FROZEN - timedelta(days=3)),
        ("1w", NOW_FROZEN - timedelta(weeks=1)),
        ("1.5h", NOW_FROZEN - timedelta(hours=1, minutes=30)),
    ],
)
def test_span_bound_reads_as_that_long_ago(
    text: str, expected: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("precis.utils.time_span.datetime", _FrozenClock)
    assert _parse_window_bound(text, "since") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("2026-10-01", datetime(2026, 10, 1, tzinfo=UTC)),
        ("2026-10-01T02:00:00+02:00", datetime(2026, 10, 1, tzinfo=UTC)),
        (NOW_FROZEN, NOW_FROZEN),
    ],
)
def test_iso_and_datetime_shapes_unchanged(
    value: str | datetime | None, expected: datetime | None
) -> None:
    assert _parse_window_bound(value, "since") == expected


def test_naive_iso_is_assumed_utc() -> None:
    assert _parse_window_bound("2026-10-01T00:00:00", "since") == datetime(
        2026, 10, 1, tzinfo=UTC
    )


@pytest.mark.parametrize("text", ["yesterday-ish", "30", "2h ago"])
def test_unparseable_bound_names_both_forms(text: str) -> None:
    with pytest.raises(BadInput) as caught:
        _parse_window_bound(text, "since")
    assert f"since= must be {SPAN_FORMS}, got {text!r}" in str(caught.value)
    assert caught.value.next == "since='30m' or '2026-10-01' or '2026-10-01T00:00:00Z'"


class _FrozenClock:
    """Stands in for the ``datetime`` class with ``now(UTC)`` pinned."""

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return NOW_FROZEN
