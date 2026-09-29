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
over from the async side. A lock-guarded counter plus an ``Event`` is visible
from both threads with no portal at all.

Its own module rather than ``server.py`` so ``install_watchdog`` can import it
without importing the server (which imports ``install_watchdog``).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock = threading.Lock()
_count = 0

#: Set whenever ``_count == 0``. Starts set — an idle process is drained.
_idle = threading.Event()
_idle.set()


def enter() -> None:
    """Mark one tool call as started."""
    global _count
    with _lock:
        _count += 1
        _idle.clear()


def leave() -> None:
    """Mark one tool call as finished."""
    global _count
    with _lock:
        _count = max(0, _count - 1)
        if _count == 0:
            _idle.set()


@contextmanager
def tracked() -> Iterator[None]:
    """Count the ``with`` block as one in-flight call."""
    enter()
    try:
        yield
    finally:
        leave()


def count() -> int:
    """How many tool calls are executing right now."""
    with _lock:
        return _count


def wait_for_drain(timeout: float) -> bool:
    """Block until no tool call is in flight, or ``timeout`` expires.

    Returns True if the process drained, False on timeout. **A timeout is
    not an error for the caller to retry** — the point of the bound is that
    one wedged call must never hold a bounce open forever, so the watchdog
    exits anyway and fails that one call rather than leaving every session
    talking to a stale-code server.
    """
    return _idle.wait(timeout)


def _reset_for_tests() -> None:
    """Drop the counter back to idle (tests only — no production caller)."""
    global _count
    with _lock:
        _count = 0
        _idle.set()
