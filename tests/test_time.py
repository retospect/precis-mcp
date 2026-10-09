"""TimeHandler — stateless clock / parse / convert / format.

Every test pins the clock (``2026-10-09T14:30:00Z``, a Friday) and the
server zone (``Europe/Zurich``, UTC+2 on that date) through the
constructor, so nothing here reads the wall clock or the host's zone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from precis.dispatch import Hub, boot
from precis.errors import BadInput, Unsupported
from precis.handlers.time import TimeHandler, parse_instant

NOW = datetime(2026, 10, 9, 14, 30, tzinfo=UTC)
ZURICH = ZoneInfo("Europe/Zurich")


@pytest.fixture
def handler() -> TimeHandler:
    return TimeHandler(hub=Hub(), clock=lambda: NOW, local_zone=ZURICH)


def _lines(body: str) -> dict[str, str]:
    return {
        k.strip(): v.strip() for k, v in (ln.split(":", 1) for ln in body.splitlines())
    }


# ── now ─────────────────────────────────────────────────────────────


def test_no_args_returns_now_utc_local_epoch(handler: TimeHandler) -> None:
    out = _lines(handler.get().body)
    assert out["utc"] == "2026-10-09T14:30:00Z (Friday)"
    assert out["local"] == "2026-10-09T16:30:00+02:00 (Europe/Zurich)"
    assert out["epoch"] == str(int(NOW.timestamp()))
    assert "parsed" not in out


def test_clock_is_read_per_call(handler: TimeHandler) -> None:
    ticks = iter([NOW, NOW.replace(hour=15)])
    h = TimeHandler(hub=Hub(), clock=lambda: next(ticks), local_zone=ZURICH)
    assert "14:30:00Z" in h.get().body
    assert "15:30:00Z" in h.get().body


def test_default_clock_is_aware_utc() -> None:
    body = TimeHandler(hub=Hub()).get().body
    utc_line = _lines(body)["utc"]
    assert utc_line.endswith(")") and "Z (" in utc_line
    stamp = utc_line.split(" ")[0]
    assert datetime.fromisoformat(stamp).tzinfo is not None


# ── parse ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("now", "2026-10-09T14:30:00Z"),
        ("2026-10-09T12:30:00Z", "2026-10-09T12:30:00Z"),
        ("2026-10-09T14:30:00+02:00", "2026-10-09T12:30:00Z"),
        ("2026-10-09 14:30", "2026-10-09T14:30:00Z"),  # naive → UTC
        ("2026-10-09", "2026-10-09T00:00:00Z"),
        ("20261009", "2026-10-09T00:00:00Z"),
        ("Fri, 09 Oct 2026 14:30:00 +0200", "2026-10-09T12:30:00Z"),
        ("Fri, 09 Oct 2026 14:30:00 GMT", "2026-10-09T14:30:00Z"),
        ("9 Oct 2026 14:30", "2026-10-09T14:30:00Z"),
        ("Oct 9, 2026", "2026-10-09T00:00:00Z"),
        ("2026/10/09 2:30 pm", "2026-10-09T14:30:00Z"),
        ("09.10.2026", "2026-10-09T00:00:00Z"),
        ("1791549000", "2026-10-09T12:30:00Z"),
        ("@1791549000", "2026-10-09T12:30:00Z"),
        ("1791549000500", "2026-10-09T12:30:00.500000Z"),  # millis
        ("today", "2026-10-09T00:00:00Z"),
        ("tomorrow", "2026-10-10T00:00:00Z"),
        ("yesterday", "2026-10-08T00:00:00Z"),
        ("in 3 hours", "2026-10-09T17:30:00Z"),
        ("in 45 min", "2026-10-09T15:15:00Z"),
        ("2 days ago", "2026-10-07T14:30:00Z"),
        ("1 day 3 hours from now", "2026-10-10T17:30:00Z"),
        ("2h30m ago", "2026-10-09T12:00:00Z"),
        ("in 1 week", "2026-10-16T14:30:00Z"),
        ("in 1 month", "2026-11-09T14:30:00Z"),
        ("in 2 years", "2028-10-09T14:30:00Z"),
        ("next monday", "2026-10-12T00:00:00Z"),
        ("monday", "2026-10-12T00:00:00Z"),
        ("friday", "2026-10-16T00:00:00Z"),  # today is Friday → next one
        ("last friday", "2026-10-02T00:00:00Z"),
        ("this wed", "2026-10-07T00:00:00Z"),
    ],
)
def test_parse_shapes(handler: TimeHandler, q: str, expected: str) -> None:
    out = _lines(handler.get(q=q).body)
    assert out["utc"].startswith(expected), out["utc"]
    assert out["parsed"].startswith(repr(q))


def test_id_is_accepted_as_the_expression(handler: TimeHandler) -> None:
    assert "2026-10-09T12:30:00Z" in handler.get(id="2026-10-09T12:30:00Z").body
    assert "2026-10-09T12:30:00Z" in handler.get(id=1791549000).body


def test_parsed_line_shows_offset_from_now(handler: TimeHandler) -> None:
    assert _lines(handler.get(q="in 3 hours").body)["parsed"].endswith("→ in 3h")
    assert _lines(handler.get(q="2h30m ago").body)["parsed"].endswith("→ 2h 30m ago")
    assert _lines(handler.get(q="now").body)["parsed"].endswith("→ now")


def test_month_arithmetic_clamps_to_month_end() -> None:
    jan31 = datetime(2026, 1, 31, 12, tzinfo=UTC)
    assert parse_instant("in 1 month", now=jan31, zone=UTC) == datetime(
        2026, 2, 28, 12, tzinfo=UTC
    )


def test_trailing_zone_token_sets_the_input_zone(handler: TimeHandler) -> None:
    body = handler.get(q="2026-10-09 16:30 Europe/Zurich").body
    assert "utc:   2026-10-09T14:30:00Z" in body
    assert "utc:   2026-10-09T14:30:00Z" in handler.get(q="2026-10-09 14:30 UTC").body


def test_from_arg_sets_the_input_zone(handler: TimeHandler) -> None:
    body = handler.get(q="2026-10-09 14:30", args={"from": "America/New_York"}).body
    assert "utc:   2026-10-09T18:30:00Z" in body


def test_today_is_midnight_in_the_from_zone(handler: TimeHandler) -> None:
    # 14:30Z on Oct 9 is already Oct 9 23:30 in Tokyo; "today" there is
    # Oct 9 00:00 JST = Oct 8 15:00Z.
    body = handler.get(q="today", args={"from": "Asia/Tokyo"}).body
    assert "utc:   2026-10-08T15:00:00Z" in body


@pytest.mark.parametrize(
    "bad", ["garbage", "in 3 parsecs", "2026-13-45", "someday", "in", "ago"]
)
def test_unparseable_raises_bad_input_with_next(handler: TimeHandler, bad: str) -> None:
    with pytest.raises(BadInput) as exc:
        handler.get(q=bad)
    assert exc.value.next is not None
    assert "ISO 8601" in str(exc.value)


# ── convert / format ────────────────────────────────────────────────


def test_to_arg_adds_a_zone_line(handler: TimeHandler) -> None:
    out = _lines(handler.get(args={"to": "Asia/Tokyo"}).body)
    assert out["Asia/Tokyo"] == "2026-10-09T23:30:00+09:00"
    # the three standard lines are still there
    assert out["utc"].startswith("2026-10-09T14:30:00Z")


def test_to_utc_is_labelled_utc(handler: TimeHandler) -> None:
    assert (
        _lines(handler.get(args={"to": "UTC"}).body)["UTC"]
        == "2026-10-09T14:30:00+00:00"
    )


def test_format_arg_renders_utc_by_default(handler: TimeHandler) -> None:
    out = _lines(handler.get(args={"format": "%Y-%m-%d %H:%M %Z"}).body)
    assert out["format"] == "2026-10-09 14:30 UTC"


def test_format_follows_the_to_zone(handler: TimeHandler) -> None:
    out = _lines(
        handler.get(
            q="tomorrow",
            args={"to": "America/Los_Angeles", "format": "%a %d %b %H:%M %Z"},
        ).body
    )
    assert out["format"] == "Fri 09 Oct 17:00 PDT"


@pytest.mark.parametrize(
    "args", [{"to": "Mars/Olympus"}, {"to": "PDT"}, {"to": 3}, {"from": ""}]
)
def test_bad_zone_arg_raises_bad_input(handler: TimeHandler, args: dict) -> None:
    with pytest.raises(BadInput) as exc:
        handler.get(args=args)
    assert exc.value.next is not None


def test_bad_format_arg_raises_bad_input(handler: TimeHandler) -> None:
    with pytest.raises(BadInput):
        handler.get(args={"format": ""})
    with pytest.raises(BadInput):
        handler.get(args={"format": 42})


# ── registration / spec ─────────────────────────────────────────────


def test_kindspec_declares_only_get() -> None:
    spec = TimeHandler.spec
    assert spec.kind == "time"
    assert spec.supports_get is True
    assert spec.id_required is False
    assert spec.is_numeric is False
    assert spec.placement == "system"
    for verb in ("search", "put", "edit", "delete", "tag", "link"):
        assert not spec.supports(verb)


def test_other_verbs_unsupported(handler: TimeHandler) -> None:
    with pytest.raises(Unsupported):
        handler.search(q="x")
    with pytest.raises(Unsupported):
        handler.put(text="x")


def test_boot_registers_time_without_a_store() -> None:
    hub = boot(store=None)
    assert "time" in hub.kinds
    assert hub.verbs_for("time") == {"get"}
    assert isinstance(hub.handler_for("time"), TimeHandler)


def test_non_dict_args_raises_bad_input(handler: TimeHandler) -> None:
    not_a_dict: Any = "Asia/Tokyo"
    with pytest.raises(BadInput):
        handler.get(args=not_a_dict)
