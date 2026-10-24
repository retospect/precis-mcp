"""Run a job's compute in a killable child process (gr506395).

A legacy blocking ``dispatch`` runs on a daemon thread inside the compute
worker (``ssh_node._dispatch_bounded``). A Python thread cannot be killed,
so a timeout used to fail the job while the compute kept running: one
5105-atom ``hexfold_relax`` held ~44 GB of GPU memory for hours after its
job was marked failed, and kept driving the worker's cached, non-thread-safe
MACE calculator that the next job would share.

:func:`run_in_child` moves the compute into a ``spawn``-started child that
is its own process-group leader. The parent records the handle (pid, pgid,
host) through ``on_start`` before waiting, so the job's meta names the
process. The parent then polls the result pipe; between polls it checks
the wall-clock deadline and ``should_stop`` (cancel / operator kill). On
any stop it signals the whole group SIGTERM, escalates to SIGKILL after a
grace period, reaps the child and confirms that no process remains in the
group. Only then does it raise :class:`ChildStopped`, so the caller
terminalizes the job after the compute is gone, not while it still runs.

Why ``spawn``, not ``fork``: forking a process that has loaded torch/CUDA
duplicates driver and allocator state (the same reason
``precis.ingest.marker`` spawns). Why a process group: the compute may
start its own children (``IsolatedTBLite`` spawns an xtb child), and those
must die with it. Why the orphan watch: if the worker itself is killed,
the child notices its parent changed and SIGKILLs its own group, so no
compute outlives the worker that owns its job.

``fn`` and every argument must pickle by reference (a module-level
function, plain data). Exceptions raised in the child are re-raised in the
parent unchanged when they pickle, else as ``RuntimeError`` carrying the
child's traceback. The child's log records go to the inherited stderr (the
worker log) at the parent's root level. :func:`emit` sends an
``(kind, payload)`` event from the child to the parent's ``on_event``
callback (for example, the device MACE actually loaded on).
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import socket
import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from typing import Any

log = logging.getLogger(__name__)

#: Seconds between deadline / stop checks while the child computes.
DEFAULT_POLL_S = 5.0
#: Seconds the child gets to exit after SIGTERM before SIGKILL, and again
#: after SIGKILL before the kill is reported unverified.
DEFAULT_GRACE_S = 30.0
#: Seconds between the child's checks that its parent is still alive.
_ORPHAN_WATCH_S = 2.0

#: The result pipe, set only inside a supervised child (see :func:`emit`).
_EVENT_CONN: Connection | None = None


class ChildStopped(Exception):
    """The supervisor stopped the child: ``reason`` is ``"timeout"``,
    ``"cancel"`` or ``"kill"``. ``handle`` is the final handle (pid, pgid,
    exit code, ``kill_verified``)."""

    def __init__(self, reason: str, handle: dict[str, Any]) -> None:
        self.reason = reason
        self.handle = handle
        super().__init__(
            f"child pid {handle.get('pid')} stopped ({reason}); "
            f"kill_verified={handle.get('kill_verified')}"
        )


class ChildDied(RuntimeError):
    """The child exited without sending a result (crash, OOM kill, signal
    from outside). ``handle['exitcode']`` is negative for a signal."""

    def __init__(self, handle: dict[str, Any]) -> None:
        self.handle = handle
        code = handle.get("exitcode")
        how = (
            f"killed by signal {-code}"
            if isinstance(code, int) and code < 0
            else (f"exit code {code}")
        )
        super().__init__(f"child pid {handle.get('pid')} died without a result ({how})")


def emit(kind: str, payload: Any) -> None:
    """Send an event to the supervising parent's ``on_event``. A no-op
    outside a supervised child, so compute code may call it unconditionally."""
    if _EVENT_CONN is not None:
        _EVENT_CONN.send(("event", kind, payload))


def _watch_parent(parent_pid: int) -> None:
    while True:
        time.sleep(_ORPHAN_WATCH_S)
        if os.getppid() != parent_pid:
            os.killpg(os.getpgrp(), signal.SIGKILL)


def _child_main(
    conn: Connection,
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    parent_pid: int,
    log_level: int,
) -> None:
    """Child entry: lead a new process group, watch the parent, run ``fn``
    and send ``("ok", result)`` or ``("err", exception)``. Module-level so
    ``spawn`` can pickle it by reference."""
    global _EVENT_CONN
    os.setsid()
    threading.Thread(
        target=_watch_parent, args=(parent_pid,), daemon=True, name="orphan-watch"
    ).start()
    logging.basicConfig(
        level=log_level,
        format=f"%(asctime)s %(levelname)s [child {os.getpid()}] %(name)s: %(message)s",
    )
    _EVENT_CONN = conn
    try:
        result = fn(*args)
    except BaseException as exc:
        try:
            conn.send(("err", exc))
        except Exception:
            conn.send(("err", RuntimeError(traceback.format_exc())))
    else:
        conn.send(("ok", result))
    finally:
        conn.close()


def _group_alive(pgid: int) -> bool:
    """True while any process remains in ``pgid``. Call only after the
    leader is reaped: it reaps group members that are our own children
    (orphans re-parented to a PID-1 worker would otherwise linger as
    zombies), which would steal the leader's exit status from
    ``multiprocessing``."""
    try:
        while os.waitpid(-pgid, os.WNOHANG)[0] > 0:
            pass
    except ChildProcessError:
        pass
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal(pid: int, sig: int) -> None:
    """Signal the group ``pid`` leads, else the pid alone (a stop that
    lands before the child's ``setsid``). A group id cannot be reused while
    any member lives, so ``killpg(pid)`` only ever reaches our child's
    group."""
    try:
        os.killpg(pid, sig)
        return
    except ProcessLookupError:
        pass
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _wait_gone(proc: Any, pgid: int, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while True:
        proc.join(0.05)
        if not proc.is_alive() and not _group_alive(pgid):
            return True
        if time.monotonic() >= end:
            return False


def stop_group(proc: Any, *, grace_s: float = DEFAULT_GRACE_S) -> bool:
    """SIGTERM the child's process group, SIGKILL whatever is left after
    ``grace_s``, and return whether the child and every group member are
    confirmed gone (``False`` only for a process that survives SIGKILL,
    e.g. stuck in uninterruptible device I/O)."""
    pid = int(proc.pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if _wait_gone(proc, pid, 0):
            return True
        _signal(pid, sig)
        if _wait_gone(proc, pid, grace_s):
            return True
    return False


def run_in_child(
    fn: Callable[..., Any],
    args: tuple[Any, ...] = (),
    *,
    label: str,
    timeout_s: float,
    should_stop: Callable[[], str | None] | None = None,
    on_start: Callable[[dict[str, Any]], None] | None = None,
    on_event: Callable[[str, Any], None] | None = None,
    on_end: Callable[[dict[str, Any]], None] | None = None,
    poll_s: float = DEFAULT_POLL_S,
    grace_s: float = DEFAULT_GRACE_S,
) -> Any:
    """Run ``fn(*args)`` in a supervised child and return its result.

    ``on_start(handle)`` runs once the child exists (persist the handle
    there); ``on_end(handle)`` runs after the child and its group are gone,
    with ``exitcode``, ``kill_verified`` and ``stopped`` filled in.
    ``should_stop()`` runs every ``poll_s``; a non-``None`` return
    (``"cancel"``, ``"kill"``) stops the child. Past ``timeout_s`` the child
    is stopped with reason ``"timeout"``. A stop raises
    :class:`ChildStopped` after :func:`stop_group` has run; a child that
    exits without a result raises :class:`ChildDied`; an exception inside
    ``fn`` is re-raised here. The child and its group are stopped on every
    exit path, including an exception in a callback."""
    mp = multiprocessing.get_context("spawn")
    parent_conn, child_conn = mp.Pipe(duplex=False)
    proc = mp.Process(
        target=_child_main,
        args=(
            child_conn,
            fn,
            args,
            os.getpid(),
            logging.getLogger().getEffectiveLevel(),
        ),
        name=f"precis-{label}",
    )
    proc.start()
    child_conn.close()
    handle: dict[str, Any] = {
        "pid": proc.pid,
        "pgid": proc.pid,
        "host": os.environ.get("PRECIS_NODE") or socket.gethostname(),
        "label": label,
        "started_at": datetime.now(UTC).isoformat(),
    }
    log.info("%s: child pid %d started (timeout %.0fs)", label, proc.pid, timeout_s)
    deadline = time.monotonic() + timeout_s
    msg: tuple[Any, ...] | None = None
    stop: str | None = None
    # The deadline/should_stop checks run at least every poll_s even
    # while events stream in: a child emitting faster than poll_s used to
    # skip them on every pass, so a cancel or kill never landed.
    next_check = time.monotonic() + poll_s
    try:
        if on_start is not None:
            on_start(dict(handle))
        while True:
            wait = max(0.0, min(poll_s, deadline - time.monotonic()))
            if parent_conn.poll(wait):
                try:
                    got = parent_conn.recv()
                except EOFError:
                    break
                if got[0] != "event":
                    msg = got
                    break
                if on_event is not None:
                    on_event(got[1], got[2])
                if time.monotonic() < next_check:
                    continue
            next_check = time.monotonic() + poll_s
            if not proc.is_alive() and not parent_conn.poll(0):
                break
            if time.monotonic() >= deadline:
                stop = "timeout"
                break
            if should_stop is not None:
                stop = should_stop()
                if stop:
                    break
    finally:
        if msg is not None and stop is None:
            # A finished child exits on its own; give it the grace period
            # before the group is signalled.
            proc.join(grace_s)
        verified = stop_group(proc, grace_s=grace_s)
        parent_conn.close()
        handle.update(
            ended_at=datetime.now(UTC).isoformat(),
            exitcode=proc.exitcode,
            kill_verified=verified,
            stopped=stop,
        )
        if not verified:
            log.error(
                "%s: child pid %d (or a member of its group) survived SIGKILL",
                label,
                proc.pid,
            )
        if on_end is not None:
            on_end(dict(handle))
    if stop is not None:
        log.warning("%s: child pid %d stopped (%s)", label, proc.pid, stop)
        raise ChildStopped(stop, handle)
    if msg is None:
        raise ChildDied(handle)
    status, payload = msg
    if status == "err":
        raise payload
    return payload


def job_stop_reason(ctx: Any) -> str | None:
    """``should_stop`` for a job's :class:`DispatchContext`: ``"cancel"``
    once ``STATUS:cancel_requested`` is set, ``"kill"`` once an operator
    stamped ``meta.kill_requested`` (``precis jobs kill``), else ``None``."""
    if ctx.is_cancel_requested():
        return "cancel"
    with ctx.store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta ? 'kill_requested' FROM refs WHERE ref_id = %s",
            (ctx.ref_id,),
        ).fetchone()
    return "kill" if row is not None and row[0] else None


__all__ = [
    "DEFAULT_GRACE_S",
    "DEFAULT_POLL_S",
    "ChildDied",
    "ChildStopped",
    "emit",
    "job_stop_reason",
    "run_in_child",
    "stop_group",
]
