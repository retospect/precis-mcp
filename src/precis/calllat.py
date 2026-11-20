"""Per-call latency of dispatched MCP tool calls (gr479849).

:func:`record` is called once per tool dispatch from ``server._offload_sync``
with the verb, ``kind``, view/mode, outcome, wall time and result size. It
emits one ``precis.calllat`` log line and keeps the last hour of durations in
memory so ``precis-status`` can show p50/p95 per verb+kind (:func:`summary`).

Never records argument text: the log line and the ring hold names, a duration
and a byte count only. The ring is per process (the shared session server
serves every session), bounded by :data:`_MAX_PER_KEY`, and lost on restart;
the durable view across processes and restarts is
:func:`precis.health_checks.tool_latency` over the ``tool_calls`` ledger.

``duration_ms`` is the full time the caller waited (semaphore queue + the
worker-thread run); ``wait_ms`` is the queue part alone, so a slow call from
a saturated pool reads differently from a slow handler.

Two more process-local facts feed the same surfaces (gr677857): queue-wait
refusals are stamped by :func:`record_refusal` so ``precis-status``'s
``dispatch_slots`` row can report ``refused_1h``, and :func:`retry_after_s`
turns the median recent duration into the back-off hint the refusal text
hands the agent.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import deque

log = logging.getLogger("precis.calllat")

_WINDOW_S = 3600.0
_MAX_PER_KEY = 2000

_lock = threading.Lock()
#: (verb, kind) → deque of (monotonic ts, duration_ms, wait_ms, is_error)
_ring: dict[tuple[str, str], deque[tuple[float, float, float, bool]]] = {}

#: Monotonic timestamps of queue-wait refusals — calls that waited for a
#: dispatch slot past ``PRECIS_MCP_QUEUE_WAIT_S`` and were refused unrun.
#: Counted apart from ``_ring`` errors so a status row can say "the server
#: is turning calls away" without lumping them in with handler faults
#: (gr677857). Per process and lost on restart, like the ring.
#: No maxlen — the deque is pruned to the window on append instead: a
#: cap would stop counting exactly when the count says the most (a busy
#: hour past _MAX_PER_KEY refusals), hiding the overload the row exists
#: to show (review td696650).
_refusals: deque[float] = deque()

#: ``retry_after_s`` bounds. The floor keeps a retry from hammering an
#: already-busy queue; the cap keeps the hint a back-off hint rather than
#: a second queue — past it the caller should read the status page.
_RETRY_MIN_S = 5
_RETRY_MAX_S = 30
#: No calls recorded yet → the median is unmeasurable; 10 s is the guess.
_RETRY_FALLBACK_S = 10


def record(
    verb: str,
    kind: str | None,
    mode: str | None,
    *,
    outcome: str,
    duration_ms: float,
    wait_ms: float = 0.0,
    size: int | None = None,
) -> None:
    """Log and remember one tool call. Never raises."""
    try:
        key = (verb, kind or "-")
        now = time.monotonic()
        with _lock:
            dq = _ring.get(key)
            if dq is None:
                dq = _ring[key] = deque(maxlen=_MAX_PER_KEY)
            dq.append((now, duration_ms, wait_ms, outcome != "ok"))
        log.info(
            "calllat verb=%s kind=%s mode=%s outcome=%s duration_ms=%.0f "
            "wait_ms=%.0f size=%s",
            verb,
            key[1],
            mode or "-",
            outcome,
            duration_ms,
            wait_ms,
            "-" if size is None else size,
        )
    except Exception:  # pragma: no cover — instrumentation must not break a call
        pass


def record_refusal() -> None:
    """Stamp one queue-wait refusal. Never raises."""
    try:
        now = time.monotonic()
        with _lock:
            _refusals.append(now)
            # Prune to the window so memory tracks the refusal rate, not
            # uptime; a stale tail between appends is filtered by
            # refusals()'s own cutoff anyway.
            cutoff = now - _WINDOW_S
            while _refusals and _refusals[0] < cutoff:
                _refusals.popleft()
    except Exception:  # pragma: no cover — instrumentation must not break a call
        pass


def refusals(window_s: float = _WINDOW_S, *, now: float | None = None) -> int:
    """How many queue-wait refusals happened in the window. At most
    ``_WINDOW_S`` of history is kept between appends, so a wider
    ``window_s`` still reads only that span."""
    cutoff = (time.monotonic() if now is None else now) - window_s
    with _lock:
        return sum(1 for ts in _refusals if ts >= cutoff)


def retry_after_s(window_s: float = _WINDOW_S, *, now: float | None = None) -> int:
    """Seconds a refused call should wait before retrying, clamped 5..30.

    The median duration of calls served in the window answers "how long
    until a slot frees": when the holders are fast the floor keeps a
    retry from hammering, when they are slow the cap keeps the hint a
    back-off hint rather than a second queue. Nothing recorded yet →
    :data:`_RETRY_FALLBACK_S`.
    """
    cutoff = (time.monotonic() if now is None else now) - window_s
    with _lock:
        # Served calls only (duration > wait): an unrun call — refused
        # at the queue bound or cancelled while still queued — records
        # its whole measured time as wait. Counting it would report the
        # queue wait itself as the pace slots free at: one refusal at
        # the default 45 s bound would pin the hint at the cap
        # (review td696650).
        ms = [
            r[1]
            for rows in _ring.values()
            for r in rows
            if r[0] >= cutoff and r[1] > r[2]
        ]
    if not ms:
        return _RETRY_FALLBACK_S
    return int(min(_RETRY_MAX_S, max(_RETRY_MIN_S, statistics.median(ms) / 1000)))


def _pct(sorted_ms: list[float], p: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(p * len(sorted_ms)))]


def summary(
    window_s: float = _WINDOW_S, *, now: float | None = None
) -> list[tuple[str, str, int, float, float, int]]:
    """``[(verb, kind, n, p50_ms, p95_ms, errors), …]`` over the window,
    slowest p95 first. Keys with no call in the window are omitted."""
    cutoff = (time.monotonic() if now is None else now) - window_s
    out: list[tuple[str, str, int, float, float, int]] = []
    with _lock:
        snap = {k: list(v) for k, v in _ring.items()}
    for (verb, kind), rows in snap.items():
        live = [r for r in rows if r[0] >= cutoff]
        if not live:
            continue
        ms = sorted(r[1] for r in live)
        out.append(
            (
                verb,
                kind,
                len(live),
                _pct(ms, 0.5),
                _pct(ms, 0.95),
                sum(1 for r in live if r[3]),
            )
        )
    out.sort(key=lambda r: r[4], reverse=True)
    return out


def _dispatch_slots_row(now: float | None) -> tuple[str, str] | None:
    """The ``dispatch_slots`` row: the queue's ceiling and its current load.

    Answers the gr677857 question — "where does the concurrency ceiling
    sit, and is the server turning calls away?" — on the same page an
    agent is sent to by the refusal text. ``limit`` is the semaphore's
    own (the setting in effect, not the env re-read), ``queued`` counts
    calls waiting on it, ``refused_1h`` the calls it turned away in the
    window. Lazy imports: ``precis.server`` imports this module, so a
    top-level import here would be a cycle.
    """
    try:
        from precis import inflight, server

        fair = server._get_tool_semaphore()
        wait = server._queue_wait_limit_s()
        return (
            "dispatch_slots",
            f"limit={fair.limit} in_flight={inflight.count()} "
            f"queued={fair.queued()} refused_1h={refusals(now=now)} "
            f"queue_wait_s={'off' if wait is None else int(wait)}",
        )
    except Exception:  # pragma: no cover — a status row must not raise
        return None


def status_rows(top: int = 5, *, now: float | None = None) -> list[tuple[str, str]]:
    """``precis-status`` Runtime rows: the dispatch ceiling, a total line,
    plus the slowest keys. ``dispatch_slots`` renders always — it reads
    config, not traffic; the ``tool_*`` rows need one served call."""
    out: list[tuple[str, str]] = []
    if (dispatch := _dispatch_slots_row(now)) is not None:
        out.append(dispatch)
    rows = summary(now=now)
    if not rows:
        return out
    total = sum(r[2] for r in rows)
    errs = sum(r[5] for r in rows)
    out.append(
        ("tool_calls_1h", f"{total} calls, {errs} errors, {len(rows)} verb/kind")
    )
    for verb, kind, n, p50, p95, e in rows[:top]:
        out.append(
            (
                f"tool_latency_1h {verb}/{kind}",
                f"n={n} p50={p50:.0f}ms p95={p95:.0f}ms errors={e}",
            )
        )
    return out


def reset() -> None:
    """Drop all recorded calls and refusals (tests)."""
    with _lock:
        _ring.clear()
        _refusals.clear()
