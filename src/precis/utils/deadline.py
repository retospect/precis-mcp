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

The same bound is also published as the *ambient deadline* — a
:class:`ContextVar` readable via :func:`deadline_at` — on the bounded
call's worker thread. Bounded outbound waits inside the call (the shared
rate limiter's token wait, the local-serving slot wait) cap themselves at
the earliest of their own configured wait and this bound, so a wait can
never silently eat the budget of the pass that owns it (gr506756: a 30 s
limiter wait against a ref pass with seconds left). Callers can set the
ambient deadline directly with :func:`deadline_scope`; it does not cross a
thread-pool boundary on its own — see :func:`deadline_scope` for the
``copy_context`` submit pattern.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

log = logging.getLogger(__name__)

#: The ambient wall-clock bound of the bounded call this code runs inside —
#: an absolute ``time.monotonic()`` point. :func:`call_with_deadline`
#: publishes it on its worker thread; :func:`deadline_scope` sets it
#: directly. Bounded outbound waits (``rate_limit.acquire``'s token wait,
#: ``local_serving.acquire``'s slot wait) bound themselves to it, so a
#: limiter wait can never eat the budget of the pass that owns it
#: (gr506756, outbound-policy S2) — without threading an argument through
#: every call site. ``None`` = no ambient bound; waits then run to their
#: own configured limits, exactly as before.
_call_deadline: ContextVar[float | None] = ContextVar(
    "precis_call_deadline", default=None
)


def deadline_at() -> float | None:
    """The ambient deadline as an absolute ``time.monotonic()`` point, or
    ``None`` when the caller runs unbounded."""
    return _call_deadline.get()


@contextmanager
def deadline_scope(deadline_at_s: float | None) -> Iterator[None]:
    """Run the block with ``deadline_at_s`` — an absolute
    ``time.monotonic()`` point — as the ambient outbound-wait deadline.
    ``None`` is a no-op (no ambient bound). Nested scopes restore the outer
    value on exit.

    Note the ambient deadline lives in a :class:`ContextVar`: it follows
    the logical call flow but does NOT cross a ``ThreadPoolExecutor``
    boundary on its own — submit workers via
    ``pool.submit(contextvars.copy_context().run, fn)`` (a fresh copy per
    task; a shared ``Context`` would leak one worker's ``ContextVar``
    writes into the others).
    """
    if deadline_at_s is None:
        yield
        return
    token = _call_deadline.set(deadline_at_s)
    try:
        yield
    finally:
        _call_deadline.reset(token)


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

    The bound is also published as the ambient deadline
    (:func:`deadline_at`) on the worker thread, so outbound waits inside
    ``fn`` bound themselves to it — a limiter or slot wait can never eat
    the budget of the call that owns it.
    """
    boxed: dict[str, Any] = {}
    deadline_at_s = time.monotonic() + deadline_s

    def _run() -> None:
        # Publish the bound as the ambient deadline so outbound waits inside
        # ``fn`` (limiter tokens, serving slots) bound themselves to it
        # rather than outliving the pass — the call's own budget is the only
        # correct bound for a wait it contains.
        token = _call_deadline.set(deadline_at_s)
        try:
            boxed["result"] = fn()
        except BaseException as exc:  # re-raised on the caller thread below
            boxed["error"] = exc
        finally:
            _call_deadline.reset(token)

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


__all__ = [
    "DeadlineExceeded",
    "call_with_deadline",
    "deadline_at",
    "deadline_scope",
]
