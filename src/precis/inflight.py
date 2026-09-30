"""Count tool calls currently executing, so a watchdog can quiesce before exit.

The install watchdog's ``os._exit(0)`` was designed for a process serving
exactly one MCP client: killing an in-flight call was the same blast radius
as the wedge it was avoiding. The checkout watchdog
(:mod:`precis.install_watchdog`) runs in a process serving *every* session on
the machine, so an exit that lands mid-dispatch fails a dozen callers at once.
This module lets the watchdog wait for dispatch to drain first.

**Plain ``threading`` primitives, deliberately.** The obvious mechanism —
draining ``server._get_tool_semaphore()`` — does not work and must not be
attempted: that is an ``anyio.Semaphore`` whose ``acquire()`` is ``async
def``, only ever awaited inside ``async with sem:`` on the FastMCP event-loop
thread. A watchdog is a plain ``threading.Thread`` with no event loop, so
calling ``.acquire()`` there returns an unawaited coroutine — a silent no-op
past a ``RuntimeWarning`` nobody reads — and the exit proceeds anyway.
Bridging correctly would need an ``anyio.from_thread`` blocking portal handed
over from the async side. A ``threading.Condition`` guarding a set of tickets
is visible from both threads with no portal at all.

Its own module rather than ``server.py`` so ``install_watchdog`` can import it
without importing the server (which imports ``install_watchdog``).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

_cond = threading.Condition()

#: Tickets handed out by :func:`enter` and not yet returned by :func:`leave`.
_active: set[int] = set()

#: Monotonic ticket source. Never reset in production — the high-water mark
#: :func:`wait_for_drain` takes is only meaningful while it keeps rising.
_issued = 0


def enter() -> int:
    """Mark one tool call as started; returns its ticket for :func:`leave`."""
    global _issued
    with _cond:
        _issued += 1
        _active.add(_issued)
        return _issued


def leave(ticket: int) -> None:
    """Mark the call holding ``ticket`` as finished."""
    with _cond:
        _active.discard(ticket)
        _cond.notify_all()


@contextmanager
def tracked() -> Iterator[None]:
    """Count the ``with`` block as one in-flight call."""
    ticket = enter()
    try:
        yield
    finally:
        leave(ticket)


def count() -> int:
    """How many tool calls are executing right now."""
    with _cond:
        return len(_active)


def wait_for_drain(timeout: float, *, mark: int | None = None) -> bool:
    """Block until every call in flight *at entry* has finished.

    Returns True if that set drained, False on timeout.

    **The bound is on the calls already running, not on the process going
    idle.** Waiting for ``count() == 0`` is what the first version did, and
    on a server shared by a dozen sessions that condition can simply never
    hold: each arriving call re-clears the idle flag, so the drain runs out
    its timeout and the exit kills whatever is running at that moment —
    exactly the failure the quiesce exists to prevent. Latching a high-water
    ticket makes the wait finite by construction: tickets only rise, the
    watched set only shrinks, and calls that arrive mid-drain are the *new*
    process's problem (the client re-``initialize``s and retries them).

    Pass ``mark`` from an earlier :func:`high_water` to drain against that
    exact set — the caller can then report how many of *those* calls are
    still running rather than a count polluted by mid-drain arrivals.

    A timeout is still not an error for the caller to retry — one wedged
    call must never hold a bounce open forever, so the watchdog exits anyway
    and fails that one call rather than leaving every session talking to a
    stale-code server.
    """
    deadline = time.monotonic() + timeout
    with _cond:
        if mark is None:
            mark = _issued
        while any(ticket <= mark for ticket in _active):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _cond.wait(remaining)
        return True


def pending_at(mark: int) -> int:
    """How many in-flight calls were issued at or before ``mark``."""
    with _cond:
        return sum(1 for ticket in _active if ticket <= mark)


def high_water() -> int:
    """The last ticket issued — the mark a bounce drains against."""
    with _cond:
        return _issued


def _reset_for_tests() -> None:
    """Drop the counter back to idle (tests only — no production caller)."""
    global _issued
    with _cond:
        _active.clear()
        _issued = 0
        _cond.notify_all()
