"""The in-flight tool-call counter a watchdog quiesces against.

Plain threading primitives on purpose — see :mod:`precis.inflight`. These
pin the three properties the watchdog depends on: the count is visible from
another thread, the drain wait is bounded, and traffic arriving mid-drain
cannot stop it settling.
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
    first = inflight.enter()
    second = inflight.enter()
    assert inflight.count() == 2
    assert inflight.wait_for_drain(0.15) is False

    inflight.leave(first)
    assert inflight.wait_for_drain(0.15) is False, "drained with one call still running"

    inflight.leave(second)
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


def test_leaving_an_unknown_ticket_cannot_release_a_live_call() -> None:
    """A stray or double ``leave`` must not make the module look drained
    while a real call is running — tickets are discarded by identity, so a
    ticket nobody holds releases nothing."""
    inflight.leave(9999)
    assert inflight.count() == 0
    ticket = inflight.enter()
    inflight.leave(ticket)
    inflight.leave(ticket)
    assert inflight.count() == 0

    live = inflight.enter()
    inflight.leave(live - 1)
    assert inflight.count() == 1
    assert inflight.wait_for_drain(0.1) is False


def test_dispatch_is_counted_at_the_offload_seam() -> None:
    """The counter is useless if nothing increments it. ``_offload_sync``
    is the one place every sync tool call passes through."""
    from precis import server

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "ticket = inflight.enter()" in source
    assert "inflight.leave(ticket)" in source


def test_arrivals_during_a_drain_cannot_stop_it_settling() -> None:
    """gr457887: the shipped drain waited for the process to go *idle*, which
    on a server shared by a dozen sessions can never happen — a new call
    lands before the last returns, so the bound expires and the exit kills
    whatever is running. The drain is against the calls in flight when it
    started, so steady traffic must not extend it.

    Keeping traffic flowing is the whole test. Without the background
    arrivals this passes against the defect too.
    """
    slow = inflight.enter()  # the one call a bounce must wait for
    stop = threading.Event()

    def _traffic() -> None:
        while not stop.is_set():
            ticket = inflight.enter()
            time.sleep(0.005)
            inflight.leave(ticket)

    churn = threading.Thread(target=_traffic, daemon=True)
    churn.start()

    def _finish_slow_call() -> None:
        time.sleep(0.3)
        inflight.leave(slow)

    threading.Thread(target=_finish_slow_call, daemon=True).start()

    started = time.monotonic()
    try:
        drained = inflight.wait_for_drain(5.0)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        churn.join(5.0)

    assert drained is True, "drain never settled while traffic kept arriving"
    assert elapsed < 3.0, f"drain waited {elapsed:.1f}s for calls it does not own"
    assert inflight.count() >= 0


def test_a_mark_pins_the_set_the_drain_owns() -> None:
    """``pending_at`` is what the watchdog logs, so it must count only the
    calls the drain is actually waiting on."""
    old = inflight.enter()
    mark = inflight.high_water()
    new = inflight.enter()

    assert inflight.pending_at(mark) == 1
    assert inflight.count() == 2

    inflight.leave(old)
    assert inflight.pending_at(mark) == 0
    assert inflight.wait_for_drain(2.0, mark=mark) is True, (
        "a call issued after the mark held the drain open"
    )
    inflight.leave(new)
