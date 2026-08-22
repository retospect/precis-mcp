"""precis.utils.utc_logging — %(asctime)s must render UTC, not host-local time."""

from __future__ import annotations

import logging
import sys
import time

import pytest

from precis.utils.utc_logging import force_utc_timestamps

#: Both tests pin the process TZ, which only takes effect via ``time.tzset``
#: — POSIX-only, absent on Windows. The behaviour under test (asctime in UTC)
#: is what the Linux daemons emit; there's no Windows deployment to guard.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only time.tzset (TZ pinning)"
)


@pytest.fixture()
def _pacific_tz(monkeypatch: pytest.MonkeyPatch):
    """Pin the process to a non-UTC zone so local vs UTC actually differ."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _render_asctime(created: float) -> str:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
    record.created = created
    return logging.Formatter().formatTime(record)


@pytest.mark.usefixtures("_pacific_tz")
def test_force_utc_timestamps_renders_gmtime(monkeypatch: pytest.MonkeyPatch):
    # Baseline: stdlib default renders epoch 0 as local time (1969 in LA).
    monkeypatch.setattr(logging.Formatter, "converter", time.localtime)
    assert _render_asctime(0.0).startswith("1969-12-31")

    force_utc_timestamps()
    assert _render_asctime(0.0).startswith("1970-01-01 00:00:00")


@pytest.mark.usefixtures("_pacific_tz")
def test_converter_covers_preexisting_formatters(monkeypatch: pytest.MonkeyPatch):
    """Class-level assignment retrofits formatters built before the call."""
    monkeypatch.setattr(logging.Formatter, "converter", time.localtime)
    fmt = logging.Formatter("%(asctime)s")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)
    record.created = 0.0
    assert fmt.formatTime(record).startswith("1969-12-31")

    force_utc_timestamps()
    assert fmt.formatTime(record).startswith("1970-01-01 00:00:00")
