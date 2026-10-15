"""Overall wall-clock deadline for blocking calls.

httpx (and requests, habanero's transport) timeouts are *per phase* — a
peer that trickles one byte inside every read-timeout window can hold a
response open forever. That is how gr486823's ``orcid_enrich`` fetch
blocked ~15 min inside an httpcore SSL recv against pub.orcid.org while
its nominal ``timeout=30`` never tripped, starving the whole serial
worker rotation (cadence alert al486675). No client-level knob bounds
total elapsed time, so the only portable enforcement is to run the call
on a worker thread and stop waiting on it.

A wedged call is ABANDONED, not interrupted: CPython cannot cancel a
``recv`` blocked in another thread, and closing the client does not
reliably wake one either (measured 2026-10-14 —
``httpx.Client.close()`` against a mid-read socket leaves the recv
blocked). The abandoned thread is a daemon: it unwinds whenever the
socket finally dies, never blocks process shutdown, and is handed to the
caller on :class:`DeadlineExceeded` so a caller can decline to re-invoke
while the previous attempt is still running (the worker loop's ref-pass
bound and the scheduler's per-cadence bound both use this to avoid
concurrent duplicate runs).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class DeadlineExceeded(TimeoutError):
    """``call_with_deadline``'s bound expired while the work still ran.

    Carries ``thread`` — the abandoned daemon thread, still running.
    Callers that re-invoke periodically should keep it and skip the next
    invocation while it is alive, rather than piling a second attempt on
    top of a wedged first.
    """

    def __init__(self, message: str, *, thread: threading.Thread) -> None:
        super().__init__(message)
        self.thread = thread


def call_with_deadline[T](
    fn: Callable[[], T],
    *,
    deadline_s: float,
    abort: Callable[[], None] | None = None,
    label: str = "",
) -> T:
    """Run ``fn()`` on a daemon thread; raise :class:`DeadlineExceeded` if
    it outlives ``deadline_s``.

    ``abort`` is a best-effort cancellation hook invoked once on expiry —
    for an httpx call, ``client.close`` (frees the pool; per the module
    docstring it may not wake a blocked recv, so abandonment is the real
    guarantee). The calling thread returns at the bound either way.
    Errors raised by ``fn`` propagate unchanged.
    """
    boxed: dict[str, Any] = {}

    def _run() -> None:
        try:
            boxed["result"] = fn()
        except BaseException as exc:  # re-raised on the caller thread below
            boxed["error"] = exc

    what = label or getattr(fn, "__name__", "call")
    t = threading.Thread(target=_run, name=f"deadline-{what}", daemon=True)
    t.start()
    t.join(deadline_s)
    if t.is_alive():
        if abort is not None:
            try:
                abort()
            except Exception:
                log.debug("deadline: abort() failed for %s", what, exc_info=True)
        raise DeadlineExceeded(
            f"{what} exceeded overall deadline of {deadline_s:g}s "
            "(call abandoned; still running detached)",
            thread=t,
        )
    if "error" in boxed:
        raise boxed["error"]
    return boxed["result"]


__all__ = ["DeadlineExceeded", "call_with_deadline"]
