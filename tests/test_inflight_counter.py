"""The in-flight tool-call counter a watchdog quiesces against.

Plain threading primitives on purpose — see :mod:`precis.inflight`. These
pin the two properties the watchdog depends on: the count is visible from
another thread, and the drain wait is bounded.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from precis import inflight


@pytest.fixture(autouse=True)
def _idle() -> Iterator[None]:
    inflight._reset_for_tests()
    yield
    inflight._reset_for_tests()


def test_idle_process_drains_immediately() -> None:
    assert inflight.count() == 0
    started = time.monotonic()
    assert inflight.wait_for_drain(5.0) is True
    assert time.monotonic() - started < 1.0


def test_drain_blocks_until_the_last_call_leaves() -> None:
    inflight.enter()
    inflight.enter()
    assert inflight.count() == 2
    assert inflight.wait_for_drain(0.15) is False

    inflight.leave()
    assert inflight.wait_for_drain(0.15) is False, "drained with one call still running"

    inflight.leave()
    assert inflight.wait_for_drain(2.0) is True


def test_drain_is_bounded_so_a_wedged_call_cannot_hold_a_bounce_open() -> None:
    """Returning False is the *designed* outcome, not an error to retry:
    the watchdog exits anyway and fails one call rather than leaving every
    session talking to a stale-code server."""
    inflight.enter()
    started = time.monotonic()
    assert inflight.wait_for_drain(0.2) is False
    assert time.monotonic() - started < 2.0


def test_counter_is_visible_across_threads() -> None:
    """The whole point: the watchdog's daemon thread must see what the
    event-loop thread is doing, which an ``anyio.Semaphore`` would not
    give it."""
    holding = threading.Event()
    release = threading.Event()

    def _worker() -> None:
        with inflight.tracked():
            holding.set()
            release.wait(5.0)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    assert holding.wait(5.0)
    assert inflight.count() == 1
    assert inflight.wait_for_drain(0.15) is False

    release.set()
    assert inflight.wait_for_drain(5.0) is True
    thread.join(5.0)


def test_tracked_releases_on_an_exception() -> None:
    with pytest.raises(RuntimeError), inflight.tracked():
        raise RuntimeError("tool blew up")
    assert inflight.count() == 0


def test_leave_without_enter_cannot_go_negative() -> None:
    """A stray decrement must not make the counter permanently 'drained'
    while real calls are running."""
    inflight.leave()
    assert inflight.count() == 0
    inflight.enter()
    assert inflight.count() == 1
    assert inflight.wait_for_drain(0.1) is False


def test_dispatch_is_counted_at_the_offload_seam() -> None:
    """The counter is useless if nothing increments it. ``_offload_sync``
    is the one place every sync tool call passes through."""
    from precis import server

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "inflight.enter()" in source
    assert "inflight.leave()" in source
