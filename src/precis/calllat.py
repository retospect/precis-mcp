"""Per-call latency of dispatched MCP tool calls (gr479849).

:func:`record` is called once per tool dispatch from ``server._offload_sync``
with the verb, ``kind``, view/mode, outcome, wall time and result size. It
emits one ``precis.calllat`` log line and keeps the last hour of durations in
memory so ``precis-status`` can show p50/p95 per verb+kind (:func:`summary`).

Never records argument text: the log line and the ring hold names, a duration
and a byte count only. The ring is per process (the shared session server
serves every session), bounded by :data:`_MAX_PER_KEY`, and lost on restart;
a durable table is a follow-up if trending across restarts is wanted.

``duration_ms`` is the full time the caller waited (semaphore queue + the
worker-thread run); ``wait_ms`` is the queue part alone, so a slow call from
a saturated pool reads differently from a slow handler.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

log = logging.getLogger("precis.calllat")

_WINDOW_S = 3600.0
_MAX_PER_KEY = 2000

_lock = threading.Lock()
#: (verb, kind) → deque of (monotonic ts, duration_ms, wait_ms, is_error)
_ring: dict[tuple[str, str], deque[tuple[float, float, float, bool]]] = {}


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


def status_rows(top: int = 5) -> list[tuple[str, str]]:
    """``precis-status`` Runtime rows: a total line plus the slowest keys.
    Empty when no call has been served yet."""
    rows = summary()
    if not rows:
        return []
    total = sum(r[2] for r in rows)
    errs = sum(r[5] for r in rows)
    out = [("tool_calls_1h", f"{total} calls, {errs} errors, {len(rows)} verb/kind")]
    for verb, kind, n, p50, p95, e in rows[:top]:
        out.append(
            (
                f"tool_latency_1h {verb}/{kind}",
                f"n={n} p50={p50:.0f}ms p95={p95:.0f}ms errors={e}",
            )
        )
    return out


def reset() -> None:
    """Drop all recorded calls (tests)."""
    with _lock:
        _ring.clear()
