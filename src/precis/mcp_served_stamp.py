"""The shared session MCP stamps what it serves, in the store (gr685985).

``release_confirm`` attests a release from worker heartbeats, but the
shared session MCP serves from a *watched checkout* that only a manual
step moves, so two releases were confirmed while the MCP still served the
release before them. The server is the one party that knows what it
serves (``precis-status``'s ``served_sha`` / ``served_from``); the factory
has no token for it and must not parse its rendered text. So the server
writes one small record — ``{sha, served_from, started_at, at, instance}``
— when it starts serving and refreshes ``at`` every :data:`REFRESH_S`;
:func:`live_stamps` is what the confirm reads, store only.

**One key per instance** (gr762430). The record lives under
``release.mcp_served.<instance>`` (:data:`STAMP_PREFIX`; ``instance`` is
``host:pid``), never one shared key: a second stamping server — a stale
dev stack holding the prod DSN — used to overwrite the shared MCP's
record every refresh, and the confirm read whichever wrote last. A reader
sees every instance; one whose ``at`` is older than :data:`FRESH`
(three refreshes) is ignored, so a killed stack ages out on its own. A
graceful stop deletes the instance's own key at the exit itself
(:func:`forget_own_stamp` — from
:func:`precis.install_watchdog.drain_then_exit` just before ``os._exit``,
and from ``atexit`` on a normal shutdown): a blue/green colour swap gives
the new container a new hostname, so the old colour's record would
otherwise keep the old sha live for :data:`FRESH` — the single key used
to be overwritten instead. The delete is deliberately *late*: the drain's
quiet deferral and in-flight drain still serve sessions, so the stamp
must stay while they run, and a SIGKILL before the exit leaves it to age
out — the safe direction, since a confirm parked on a leftover record
waits, while a stamp dropped early would skip a server still serving
stale code. ``at`` is the store's ``now()``, never the writer's clock, so
freshness and the confirm's answer-time comparison read one clock. A
restart on the same host supersedes the record its predecessor left
(:func:`live_stamps`) — but only once that record has demonstrably
stopped being written (``at`` older than :data:`SUPERSEDE_AFTER`): a
record still being refreshed belongs to a live server and always counts,
so two stamping servers sharing one hostname are both held to the
release. The stamper prunes records a day old at start
(:func:`prune_stamps`).

**The legacy key, one release only** (gr762430 — remove after the release
that ships it). A server still on the pre-gr762430 code writes the single
``release.mcp_served`` key (:data:`LEGACY_KEY`). :func:`live_stamps` reads
it as one more instance record, named by its own ``instance`` field: it
joins the per-instance records unless one of them carries the same
instance, then passes the same freshness and same-host rules. New code
never writes it, so once every stamping server runs this code it ages
out. Remove :func:`read_stamp`, :data:`LEGACY_KEY` and the merge in
:func:`live_stamps` together.

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
public MCP daemon runs from the git-installed web venv (``vcs-install``)
and is restarted by the deploy itself, so it never stamps; nor does a
stdio server, a dev server from a working tree, a baked test image or a
CLI one-shot. A stack that does stamp — any watched-checkout server with
the prod DSN — is one more instance the confirm holds to the release, and
its ask names it. A stamp failure is logged and never stops the server
from serving.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: The single last-writer key the pre-gr762430 servers write (a registered
#: setting, the same arrangement as ``release.prod_head``). Read only, as
#: the rollout fallback; remove with the fallback (module docstring).
LEGACY_KEY = "release.mcp_served"

#: ``app_settings`` key prefix of the per-instance records:
#: ``release.mcp_served.<host>:<pid>`` (gr762430).
STAMP_PREFIX = f"{LEGACY_KEY}."

#: A per-instance record whose ``at`` is this old is removed when a
#: stamper starts (:func:`prune_stamps`) — far past any reader's window.
PRUNE_AFTER = timedelta(days=1)

#: Refresh cadence of ``at``.
REFRESH_S = 300.0

#: A record whose ``at`` (store clock) is older than this is not live:
#: three missed refreshes — a killed stack ages out in ~15 minutes, not
#: the worker heartbeat window's 60.
FRESH = timedelta(seconds=3 * REFRESH_S)

#: How old a same-host record's ``at`` must be before a newer-started
#: instance stands for that host (:func:`live_stamps`): a live stamper
#: writes every :data:`REFRESH_S`, so a record this old has missed a full
#: refresh and then some — the leftover a restart leaves, hidden a little
#: before :data:`FRESH` drops it anyway. A record still being refreshed
#: is a live peer and is never superseded.
SUPERSEDE_AFTER = timedelta(seconds=2 * REFRESH_S)

#: How long :func:`forget_own_stamp` may hold a process exit.
_FORGET_TIMEOUT_S = 3.0

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
#: Set by :func:`forget_own_stamp`: no later write may resurrect the key.
_stopped = False
_atexit_registered = False


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


def _instance() -> str:
    """``host:pid`` — this process's instance name and its key suffix."""
    return f"{socket.gethostname()}:{os.getpid()}"


def stamp_key(instance: str) -> str:
    """The ``app_settings`` key ``instance``'s record lives under."""
    return f"{STAMP_PREFIX}{instance}"


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
        "instance": _instance(),
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
    """Write this instance's record under its own key (:func:`stamp_key`);
    ``True`` when written. ``at`` is the store's clock (:func:`_db_now`)
    unless ``now`` pins it. The process's pending value
    (:func:`note_pending`) is always carried, read under the write lock;
    after :func:`forget_own_stamp` nothing is written. Never raises — a
    stamp failure (store down, unwritable) is logged and the server serves
    on."""
    try:
        with _write_lock:
            if _stopped:
                return False
            if now is None and _identity() is not None:
                now = _db_now(store)
            rec = build_record(started_at, now=now, pending_sha=pending_sha or _pending)
            if not rec:
                return False
            from precis import settings

            # Re-checked right before the write: a forget whose lock wait
            # timed out (this write slow on the store clock) has already
            # deleted the key, and this late write must not restore it.
            if _stopped:
                return False
            settings.set_setting(
                stamp_key(str(rec["instance"])),
                json.dumps(rec, separators=(",", ":")),
                store=store,
            )
    except Exception:
        log.warning("mcp served stamp: write failed", exc_info=True)
        return False
    return True


def _db_now(store: Store) -> datetime:
    """The store's ``now()`` — the one clock ``at`` and its readers use.
    Raises when the store is unreadable."""
    with store.pool.connection() as conn:
        row = conn.execute("SELECT now()").fetchone()
    if row is None or row[0] is None:
        raise RuntimeError("mcp served stamp: store clock unreadable")
    value: datetime = row[0]
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def forget_own_stamp(timeout_s: float = _FORGET_TIMEOUT_S) -> bool:
    """A graceful stop: delete this instance's own key so a reader stops
    counting it at once, instead of after :data:`FRESH` (gr762430 — a
    colour swap's new container has a new hostname, nothing overwrites
    the old record). Called *at the exit*, never at drain start:
    :func:`precis.install_watchdog.drain_then_exit` (also the checkout
    watchdog's bounce, whose ``pending_sha`` attests until then) calls it
    just before ``os._exit``, and ``atexit`` on a normal shutdown —
    idempotent. The quiet deferral and the in-flight drain still serve
    sessions, so the stamp must stay live while they run; a SIGKILL
    before the exit leaves the key to age out over :data:`FRESH`, the
    safe direction — a leftover parks the confirm, a stamp dropped early
    would skip a server still serving stale code. No later write lands
    (``_stopped``, re-checked by :func:`write_stamp` right before its
    store write). Best effort and bounded: waits at most about
    ``timeout_s`` for an in-flight write, then as long again for the
    delete — inline instead when the interpreter is already shutting
    down, where Python 3.12+ refuses a new thread. ``True`` when the key
    is gone, never raises, a no-op (``False``) in a process that does not
    stamp."""
    global _stopped
    try:
        state = _state
        if state is None:
            return False
        _stopped = True
        held = _write_lock.acquire(timeout=timeout_s)
        try:
            key = stamp_key(_instance())
            done = threading.Event()

            def _clear() -> None:
                try:
                    from precis import settings

                    settings.clear_setting(
                        key, store=state[0], reason="mcp served stamp: instance stopped"
                    )
                    done.set()
                except Exception:
                    log.warning("mcp served stamp: forget failed", exc_info=True)

            t = threading.Thread(
                target=_clear, name="mcp-served-stamp-forget", daemon=True
            )
            try:
                t.start()
            except RuntimeError:
                # Python 3.12+ refuses a new thread during interpreter
                # shutdown — exactly where the ``atexit`` call runs. The
                # delete must still happen, so it runs inline here; the
                # bound matters less than leaving the key behind.
                _clear()
                return done.is_set()
            t.join(timeout_s)
            return done.is_set()
        finally:
            if held:
                _write_lock.release()
    except Exception:
        log.warning("mcp served stamp: forget failed", exc_info=True)
        return False


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
    prune_stamps(store)
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
    global _state, _atexit_registered
    try:
        if not stamps_this_process(transport):
            return None
    except Exception:
        log.warning("mcp served stamp: identity unreadable", exc_info=True)
        return None
    started = started_at or datetime.now(UTC)
    _state = (store, started)
    # A graceful exit that runs atexit (uvicorn's own SIGTERM shutdown)
    # drops the key; the drain exit (os._exit) calls forget itself.
    if not _atexit_registered:
        atexit.register(forget_own_stamp)
        _atexit_registered = True
    thread = threading.Thread(
        target=_loop,
        args=(store, started, interval_s),
        name="mcp-served-stamp",
        daemon=True,
    )
    thread.start()
    return thread


def _parse(raw: str | None) -> dict[str, Any] | None:
    """One raw record with ``at`` parsed to an aware datetime, or ``None``
    when absent, unparsable or malformed."""
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


def _instance_rows(store: Store) -> list[tuple[str, str]]:
    """``(key, value)`` of every per-instance row, store only; ``[]`` when
    the store is unreadable (the reader attests nothing then, as an absent
    stamp does)."""
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT key, value FROM app_settings WHERE starts_with(key, %s) "
                "ORDER BY key",
                (STAMP_PREFIX,),
            ).fetchall()
    except Exception:
        log.warning("mcp served stamp: per-instance read failed", exc_info=True)
        return []
    return [(str(k), str(v)) for k, v in rows]


def read_stamps(store: Store) -> list[dict[str, Any]]:
    """Every per-instance record (:func:`_parse` shape), ``instance`` set
    from the key when the record lacks it; malformed rows are skipped.
    No freshness filter — :func:`live_stamps` is the confirm's reader."""
    out: list[dict[str, Any]] = []
    for key, raw in _instance_rows(store):
        rec = _parse(raw)
        if rec is None:
            continue
        if not isinstance(rec.get("instance"), str) or not rec["instance"]:
            rec["instance"] = key[len(STAMP_PREFIX) :]
        out.append(rec)
    return out


def read_stamp(store: Store) -> dict[str, Any] | None:
    """The legacy single ``release.mcp_served`` record a pre-gr762430
    server writes (:func:`_parse` shape), or ``None``. The rollout
    fallback only — remove after the release that ships gr762430."""
    from precis import settings

    return _parse(settings.get_str(LEGACY_KEY, store=store))


def _host(instance: str) -> str | None:
    """The host half of ``host:pid``, else ``None`` (no pid: never
    superseded)."""
    host, sep, pid = instance.rpartition(":")
    return host if sep and host and pid.isdigit() else None


def live_stamps(store: Store, *, fresh: timedelta = FRESH) -> list[dict[str, Any]]:
    """The records of the instances live now, sorted by instance; "now" is
    the store's clock (:func:`_db_now`), the one ``at`` is written on, so a
    writer's or reader's skewed clock never moves the window. ``[]`` when
    the store is unreadable.

    Every per-instance record (:func:`read_stamps`) plus — the rollout
    fallback (gr762430, remove after its release) — the legacy single
    record (:func:`read_stamp`) as one more instance, named by its own
    ``instance`` field (``legacy`` when it has none) and dropped when a
    per-instance record already carries that instance. Then a record whose
    ``at`` is older than ``fresh`` is ignored (a stopped stack ages out),
    and of the live records sharing one host an older-started record is
    superseded by the newest-started one only once it has demonstrably
    stopped being refreshed (``at`` older than :data:`SUPERSEDE_AFTER`) —
    the leftover a restart leaves; a second still-stamping server on the
    same hostname is a live peer and always counts."""
    try:
        now = _db_now(store)
    except Exception:
        log.warning("mcp served stamp: store clock unreadable", exc_info=True)
        return []
    recs = read_stamps(store)
    legacy = read_stamp(store)
    if legacy is not None:
        legacy["instance"] = str(legacy.get("instance") or "legacy")
        if all(r["instance"] != legacy["instance"] for r in recs):
            recs.append(legacy)
    live = [r for r in recs if now - r["at"] <= fresh]
    newest: dict[str, str] = {}
    for r in live:
        host = _host(str(r["instance"]))
        if host is None:
            continue
        started = str(r.get("started_at") or "")
        if host not in newest or started > newest[host]:
            newest[host] = started
    kept = [
        r
        for r in live
        if (host := _host(str(r["instance"]))) is None
        or str(r.get("started_at") or "") == newest[host]
        or now - r["at"] <= SUPERSEDE_AFTER
    ]
    return sorted(kept, key=lambda r: str(r["instance"]))


def prune_stamps(store: Store, *, now: datetime | None = None) -> int:
    """Clear every per-instance record whose ``at`` is :data:`PRUNE_AFTER`
    old — the keys dead instances (each restart is a new pid) leave
    behind; returns how many. Never raises."""
    from precis import settings

    cutoff = (now or datetime.now(UTC)) - PRUNE_AFTER
    n = 0
    try:
        for key, raw in _instance_rows(store):
            rec = _parse(raw)
            if rec is not None and rec["at"] < cutoff:
                settings.clear_setting(
                    key, store=store, reason="mcp served stamp: dead instance"
                )
                n += 1
    except Exception:
        log.warning("mcp served stamp: prune failed", exc_info=True)
    return n


__all__ = [
    "FRESH",
    "LEGACY_KEY",
    "PRUNE_AFTER",
    "REFRESH_S",
    "STAMP_PREFIX",
    "SUPERSEDE_AFTER",
    "build_record",
    "forget_own_stamp",
    "live_stamps",
    "note_pending",
    "prune_stamps",
    "read_stamp",
    "read_stamps",
    "stamp_key",
    "stamps_this_process",
    "start_served_stamp_thread",
    "write_stamp",
]
