"""Live per-process worker activity — "what is THIS worker doing right now".

Born of the 2026-08-09 ``fetch_oa`` monopolization incident: a single
long-running pass on the serial ``claude_inproc`` lane held the worker for
hours with nothing in the logs to show it — no crash, no exception, just
silence. From the outside that is indistinguishable from a dead worker; the
only way anyone found the true cause was ``uvx py-spy dump`` against the live
process. That's an SSH-and-hope diagnostic, not something the web Status page
(or an alert) can act on.

This module is the fix: a tiny in-process registry a pass loop stamps as it
works, so "what is the worker doing" becomes a plain DB read (published via
:mod:`precis.workers.heartbeat`'s ``host_heartbeat.meta.activity``) instead
of an SSH session. It is deliberately dumb — module-level state, no DB, no
async — because it has to be callable from deep inside any pass body without
threading a context object through every signature.

Thread-safety is a plain :class:`threading.Lock` around a single dict. All
stored values are JSON-serializable (ISO-8601 UTC strings, never raw
``datetime`` objects) since the whole point is that this state gets
serialized into ``host_heartbeat.meta`` on the next beat.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from datetime import UTC, datetime
from typing import Any

_lock = threading.Lock()

#: The single process-wide state dict. Empty before the first
#: :func:`set_pass` call — :func:`snapshot` then returns ``{}``.
_state: dict[str, Any] = {}

log = logging.getLogger(__name__)

#: gr262888 — a pass running longer than this gets one WARNING (with the
#: pass thread's stack) from the heartbeat thread. ``worker_logs`` records
#: only pass ENDS, so a 50-minute silence otherwise can't name its pass.
STUCK_WARN_SECONDS = 600.0
STUCK_WARN_ENV = "PRECIS_PASS_STUCK_WARN_SECONDS"
_STUCK_MAX_FRAMES = 40

#: Non-serialized companions to ``_state`` (guarded by ``_lock``): monotonic
#: start and thread ident of the active pass, and whether its stuck warning
#: already fired. ``_mono_start`` is None while idle.
_mono_start: float | None = None
_pass_ident: int | None = None
_stuck_warned = False


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def set_pass(name: str) -> None:
    """Begin a pass: state becomes ``{"pass": name, "since": <now>}``.

    Drops any previous pass's detail — a fresh pass starts with a clean
    slate, never carrying over a stale ``note()`` from whatever ran before
    it.
    """
    global _mono_start, _pass_ident, _stuck_warned
    with _lock:
        _state.clear()
        _state["pass"] = name
        _state["since"] = _utc_now_iso()
        _mono_start = time.monotonic()
        _pass_ident = threading.get_ident()
        _stuck_warned = False


def note(detail: str) -> None:
    """Attach/update short progress text on the current pass (e.g. ``"stub
    37/120"``).

    A silent no-op when no pass is active (:func:`set_pass` was never called,
    or :func:`clear` already ran) — a pass body that calls this defensively
    on every loop iteration shouldn't have to guard for that itself. Must
    never raise: this is called from deep inside hot loops in code (like
    ``fetch_oa``) whose whole job is fetching from flaky external services,
    so a bug here must never become a NEW way for that code to fail.
    """
    try:
        with _lock:
            if "pass" not in _state:
                return
            _state["detail"] = detail
    except Exception:
        pass


def clear() -> None:
    """Pass finished: state becomes idle, remembering what just ran.

    ``last_pass`` is present only when a pass was actually active (calling
    ``clear()`` with no active pass still flips to idle, just without a
    ``last_pass`` to report).
    """
    global _mono_start, _pass_ident
    with _lock:
        _mono_start = None
        _pass_ident = None
        last_pass = _state.get("pass")
        _state.clear()
        _state["idle"] = True
        if last_pass is not None:
            _state["last_pass"] = last_pass
        _state["finished"] = _utc_now_iso()


def snapshot() -> dict[str, Any]:
    """Thread-safe shallow copy of the current state for a heartbeat thread
    to publish. ``{}`` before :func:`set_pass` has ever been called."""
    with _lock:
        return dict(_state)


#: A chunk handler batch tracked for the stuck warning ONLY. Deliberately
#: not in ``_state``: handlers must not appear in the published
#: ``meta.activity`` (the pass-wedged probe would see them). Guarded by
#: ``_lock``.
_tracked: dict[str, Any] | None = None


def track(name: str) -> None:
    """Start stuck-warning tracking for ``name`` without touching
    :func:`snapshot`."""
    global _tracked
    with _lock:
        _tracked = {
            "name": name,
            "start": time.monotonic(),
            "ident": threading.get_ident(),
            "warned": False,
        }


def untrack() -> None:
    """End :func:`track`."""
    global _tracked
    with _lock:
        _tracked = None


def _stuck_threshold() -> float:
    raw = os.environ.get(STUCK_WARN_ENV)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return STUCK_WARN_SECONDS


def warn_if_stuck(threshold: float | None = None) -> bool:
    """Called from the heartbeat thread: if the active pass has run longer
    than the threshold and hasn't been warned about yet, log ONE WARNING
    naming it, its elapsed time and the pass thread's current stack.
    Returns True when a warning was emitted. Never raises."""
    global _stuck_warned
    try:
        limit = _stuck_threshold() if threshold is None else threshold
        with _lock:
            now = time.monotonic()
            if (
                _mono_start is not None
                and not _stuck_warned
                and "pass" in _state
                and now - _mono_start >= limit
            ):
                _stuck_warned = True
                elapsed = now - _mono_start
                name = _state["pass"]
                ident = _pass_ident
            elif (
                _tracked is not None
                and not _tracked["warned"]
                and now - _tracked["start"] >= limit
            ):
                _tracked["warned"] = True
                elapsed = now - _tracked["start"]
                name = _tracked["name"]
                ident = _tracked["ident"]
            else:
                return False
        frame = sys._current_frames().get(ident) if ident is not None else None
        stack = traceback.format_stack(frame) if frame is not None else []
        stack_txt = "".join(stack[-_STUCK_MAX_FRAMES:]).rstrip()
        log.warning(
            "worker: pass %s running for %.0fs (> %.0fs); pass thread stack:\n%s",
            name,
            elapsed,
            limit,
            stack_txt or "<unavailable>",
            extra={
                "payload": {
                    "handler": name,
                    "stuck": True,
                    "elapsed_s": round(elapsed, 1),
                }
            },
        )
        return True
    except Exception:
        return False


__all__ = ["clear", "note", "set_pass", "snapshot", "track", "untrack", "warn_if_stuck"]
