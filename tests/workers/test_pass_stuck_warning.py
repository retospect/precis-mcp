"""gr262888 — the heartbeat thread names a pass stuck past the threshold."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pytest

from precis.workers import activity
from precis.workers import heartbeat as hb


def _fake_pass_blocks(release: threading.Event, entered: threading.Event) -> None:
    activity.set_pass("_fake_slow_pass")
    entered.set()
    try:
        release.wait(10)
    finally:
        activity.clear()


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == activity.log.name]


def _run(monkeypatch, caplog, fn) -> list[logging.LogRecord]:
    monkeypatch.setenv(activity.STUCK_WARN_ENV, "0.05")
    monkeypatch.setattr(hb, "_THREAD_TICK_SECONDS", 0.02)
    monkeypatch.setattr(hb, "run_heartbeat_pass", lambda *a, **k: None)
    caplog.set_level(logging.WARNING, logger=activity.log.name)
    stop = threading.Event()
    from precis.store import Store

    monkeypatch.setattr(Store, "connect", lambda dsn, **kw: None)
    t = hb.start_heartbeat_thread("x", should_stop=stop.is_set)
    try:
        fn()
    finally:
        stop.set()
        t.join(5)
    return [r for r in caplog.records if r.name == activity.log.name]


def test_slow_pass_warns_once_with_stack(monkeypatch, caplog) -> None:
    release, entered = threading.Event(), threading.Event()
    worker = threading.Thread(target=_fake_pass_blocks, args=(release, entered))

    def scenario() -> None:
        worker.start()
        entered.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _warnings(caplog):
            time.sleep(0.02)
        time.sleep(0.3)  # several more ticks: must still be exactly one
        release.set()
        worker.join(5)

    recs = _run(monkeypatch, caplog, scenario)
    assert len(recs) == 1
    msg = recs[0].getMessage()
    assert "_fake_slow_pass" in msg
    assert "_fake_pass_blocks" in msg


def test_fast_pass_does_not_warn(monkeypatch, caplog) -> None:
    def scenario() -> None:
        activity.set_pass("_fast_pass")
        activity.clear()
        time.sleep(0.3)

    assert _run(monkeypatch, caplog, scenario) == []


def test_tracked_handler_warns_but_stays_idle_in_snapshot(monkeypatch, caplog) -> None:
    release, entered = threading.Event(), threading.Event()
    seen: dict[str, dict[str, Any]] = {}

    def handler_batch() -> None:
        activity.track("_fake_handler")
        entered.set()
        try:
            release.wait(10)
        finally:
            activity.untrack()

    worker = threading.Thread(target=handler_batch)

    def scenario() -> None:
        worker.start()
        entered.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _warnings(caplog):
            time.sleep(0.02)
        seen["snap"] = activity.snapshot()
        release.set()
        worker.join(5)

    recs = _run(monkeypatch, caplog, scenario)
    assert len(recs) == 1
    assert "_fake_handler" in recs[0].getMessage()
    assert "handler_batch" in recs[0].getMessage()
    assert "pass" not in seen["snap"]


@pytest.fixture(autouse=True)
def _idle():
    activity.clear()
    yield
    activity.clear()
