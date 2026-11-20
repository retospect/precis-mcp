"""The shared session MCP stamps what it serves, in the store (gr685985).

``release_confirm`` attests a release from worker heartbeats, but the
shared session MCP serves from a *watched checkout* that only a manual
step moves, so two releases were confirmed while the MCP still served the
release before them. The server is the one party that knows what it
serves (``precis-status``'s ``served_sha`` / ``served_from``); the factory
has no token for it and must not parse its rendered text. So the server
writes one small record to the ``release.mcp_served`` setting — ``{sha,
served_from, started_at, at, instance}`` — when it starts serving and refreshes
``at`` every :data:`REFRESH_S`; :func:`read_stamp` is what the confirm
reads, store only.

A pinned checkout does not restart the server at once: the checkout
watchdog defers the exit while sessions are live, up to its cap (30
minutes), so ``sha`` (what is served) can lag a correct pin. When the
watchdog first sees the checkout at a new HEAD it calls
:func:`note_pending`, which adds ``pending_sha`` — the checkout's HEAD —
to the record; the restart is then bounded and the confirm does not wait
for it.

Who stamps (:func:`stamps_this_process`): only the process that serves
from a watched checkout — build identity ``served_from: watched-checkout``
AND ``PRECIS_CHECKOUT_WATCHDOG`` set (the shared session MCP's launcher
sets it) — and is not a per-worktree server (``PRECIS_MCP_WORKTREE``). The
key is global, so a second server writing it would make the confirm read
the wrong one: the public MCP daemon runs from the git-installed web venv
(``vcs-install``) and is restarted by the deploy itself, so it never
stamps; nor does a stdio server, a dev server from a working tree, a baked
test image or a CLI one-shot. ``instance`` (``host:pid``) names the writer
for a reader of the raw record. A stamp failure is logged and never stops
the server from serving.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: ``app_settings`` key the record lives under (a registered setting, the
#: same arrangement as ``release.prod_head``).
STAMP_KEY = "release.mcp_served"

#: Refresh cadence of ``at``; the confirm treats a stamp older than the
#: worker liveness window as not attested, so this sits far inside it.
REFRESH_S = 300.0

#: The one ``served_from`` lane (``precis.handlers.skill._git_identity_lane``)
#: that stamps, together with the checkout watchdog env.
_STAMPING_LANE = "watched-checkout"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: ``(store, started_at)`` of the running stamper, ``None`` in a process
#: that does not stamp; and the checkout HEAD :func:`note_pending` saw —
#: process state every later write carries. ``_write_lock`` serialises
#: build-and-write, so a refresh already in flight cannot land after the
#: pending write with a record built before the pending value was set.
_state: tuple[Store, datetime] | None = None
_pending: str | None = None
_write_lock = threading.Lock()


def _identity() -> tuple[str, str] | None:
    """``(sha, served_from)`` of this process when it should stamp, else
    ``None``: the build identity ``precis-status`` reports, on the
    watched-checkout lane with ``PRECIS_CHECKOUT_WATCHDOG`` set, outside a
    worktree server."""
    from precis import worktree_server

    if worktree_server.worktree_name() is not None:
        return None
    from precis.handlers.skill import _git_identity_lane

    served_from, lane = _git_identity_lane()
    sha = lane.get("git_sha")
    if served_from != _STAMPING_LANE or not sha:
        return None
    from precis.install_watchdog import _CHECKOUT_ROOT_ENV

    if not (os.environ.get(_CHECKOUT_ROOT_ENV) or "").strip():
        return None
    return sha, served_from


def stamps_this_process(transport: str) -> bool:
    """Whether a server on ``transport`` writes the stamp: a network
    transport (never stdio) with a stamping identity (:func:`_identity`)."""
    return transport != "stdio" and _identity() is not None


def build_record(
    started_at: datetime,
    *,
    now: datetime | None = None,
    pending_sha: str | None = None,
) -> dict[str, Any]:
    """The record for this process, or ``{}`` when it must not stamp.
    ``pending_sha`` (when it differs from the served sha) is the watched
    checkout's HEAD the server has seen but not yet restarted onto."""
    ident = _identity()
    if ident is None:
        return {}
    sha, served_from = ident
    rec: dict[str, Any] = {
        "sha": sha,
        "served_from": served_from,
        "started_at": started_at.strftime(_TS_FMT),
        "at": (now or datetime.now(UTC)).strftime(_TS_FMT),
        "instance": f"{socket.gethostname()}:{os.getpid()}",
    }
    if pending_sha and pending_sha != sha:
        rec["pending_sha"] = pending_sha
    return rec


def write_stamp(
    store: Store,
    started_at: datetime,
    *,
    now: datetime | None = None,
    pending_sha: str | None = None,
) -> bool:
    """Write the record; ``True`` when written. The process's pending
    value (:func:`note_pending`) is always carried, read under the write
    lock. Never raises — a stamp failure (store down, unwritable) is
    logged and the server serves on."""
    try:
        with _write_lock:
            rec = build_record(started_at, now=now, pending_sha=pending_sha or _pending)
            if not rec:
                return False
            from precis import settings

            settings.set_setting(
                STAMP_KEY, json.dumps(rec, separators=(",", ":")), store=store
            )
    except Exception:
        log.warning("mcp served stamp: write failed", exc_info=True)
        return False
    return True


def note_pending(sha: str) -> threading.Thread | None:
    """The checkout watchdog saw the watched checkout at ``sha`` (not what
    is served): record it as process state and write the stamp on a daemon
    thread, returning at once — the watchdog's exit must never wait on the
    store. ``None`` (a no-op) in a process that does not stamp, else the
    write thread. Never raises."""
    global _pending
    try:
        state = _state
        if state is None:
            return None
        _pending = sha
        t = threading.Thread(
            target=write_stamp,
            args=(state[0], state[1]),
            name="mcp-served-stamp-pending",
            daemon=True,
        )
        t.start()
        return t
    except Exception:
        log.warning("mcp served stamp: pending update failed", exc_info=True)
        return None


def _loop(store: Store, started_at: datetime, interval_s: float) -> None:
    while True:
        write_stamp(store, started_at)
        time.sleep(interval_s)


def start_served_stamp_thread(
    store: Store,
    *,
    transport: str,
    started_at: datetime | None = None,
    interval_s: float = REFRESH_S,
) -> threading.Thread | None:
    """Stamp now and every ``interval_s`` on a daemon thread; ``None`` (no
    thread) when this process must not stamp (:func:`stamps_this_process`)."""
    global _state
    try:
        if not stamps_this_process(transport):
            return None
    except Exception:
        log.warning("mcp served stamp: identity unreadable", exc_info=True)
        return None
    started = started_at or datetime.now(UTC)
    _state = (store, started)
    thread = threading.Thread(
        target=_loop,
        args=(store, started, interval_s),
        name="mcp-served-stamp",
        daemon=True,
    )
    thread.start()
    return thread


def read_stamp(store: Store) -> dict[str, Any] | None:
    """The ``release.mcp_served`` record with ``at`` parsed to an aware
    datetime, or ``None`` when absent, unparsable or malformed."""
    from precis import settings

    raw = settings.get_str(STAMP_KEY, store=store)
    if not raw:
        return None
    try:
        rec = json.loads(raw)
        at = datetime.strptime(str(rec["at"]), _TS_FMT).replace(tzinfo=UTC)
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("sha"), str):
        return None
    return {**rec, "at": at}


__all__ = [
    "REFRESH_S",
    "STAMP_KEY",
    "build_record",
    "note_pending",
    "read_stamp",
    "stamps_this_process",
    "start_served_stamp_thread",
    "write_stamp",
]
