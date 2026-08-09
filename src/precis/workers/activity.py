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

import threading
from datetime import UTC, datetime
from typing import Any

_lock = threading.Lock()

#: The single process-wide state dict. Empty before the first
#: :func:`set_pass` call — :func:`snapshot` then returns ``{}``.
_state: dict[str, Any] = {}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def set_pass(name: str) -> None:
    """Begin a pass: state becomes ``{"pass": name, "since": <now>}``.

    Drops any previous pass's detail — a fresh pass starts with a clean
    slate, never carrying over a stale ``note()`` from whatever ran before
    it.
    """
    with _lock:
        _state.clear()
        _state["pass"] = name
        _state["since"] = _utc_now_iso()


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
    with _lock:
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


__all__ = ["clear", "note", "set_pass", "snapshot"]
