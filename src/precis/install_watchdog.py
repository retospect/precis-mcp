"""Exit cleanly when this process's install is replaced on disk (gr338977).

A deploy refreshes ``/opt/mcps/venv`` with an in-place ``uv pip install
--upgrade`` while session MCP servers launched from it are still running.
A live server whose site-packages are swapped underneath it doesn't fail
fast — it desyncs at the protocol level ("response for an unknown message
ID") and then hangs until the client's 1800 s idle timeout kills the call.
The observed *recoverable* failure mode is a clean exit: the MCP client
logs "server process exited cleanly" and restarts a fresh server on the
next connection, which then runs the new code.

So: fingerprint the installed package at boot, poll it from a daemon
thread, and ``os._exit(0)`` the moment it changes. Worst case is a
~:data:`_DEFAULT_INTERVAL_S`-second blip instead of a half-hour wedge —
and servers stop serving stale code after a deploy as a side effect.

Only real installs are watched: when ``precis.__file__`` is not under a
``site-packages`` directory (a source checkout / editable install, where
every ``git checkout`` touches mtimes) the watchdog stays off. The
fingerprint is the ``precis/__init__.py`` *file* stat, not its directory:
interpreter ``__pycache__`` writes bump the directory mtime without any
reinstall having happened.

``PRECIS_INSTALL_WATCHDOG=0`` disables it outright.

gr341515: the exit itself was invisible — the warning above only reaches
stderr, which no MCP client surfaces, so an operator found the server
simply gone with zero explanation. :func:`_write_exit_breadcrumb` drops a
small JSON note immediately before the ``os._exit(0)`` (and from a
chained ``sys.excepthook``/``atexit`` pair for a genuine crash or a
normal shutdown — see :func:`install_exit_breadcrumb_hooks`); the next
boot's ``precis-status`` reads and consumes it (see
``handlers/skill.py::_consume_last_exit_breadcrumb``).
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

log = logging.getLogger(__name__)

#: Poll cadence. Low enough that a post-deploy stale server is gone in
#: well under a minute; high enough that the stat is free.
_DEFAULT_INTERVAL_S = 20.0

Fingerprint = tuple[str, int, int, int, int]


def _fingerprint_for(init_py: Path) -> Fingerprint | None:
    """Identity of the install that owns ``init_py``.

    ``None`` when the path is not under ``site-packages`` — a source /
    editable install whose mtimes churn for reasons that aren't a
    reinstall. Raises ``OSError`` when the file can't be stat'ed; the
    caller decides whether that means "don't watch" (at baseline) or
    "install ripped out mid-swap" (in the poll loop).
    """
    resolved = init_py.resolve()
    if "site-packages" not in resolved.parts:
        return None
    st = resolved.stat()
    # ino + mtime + ctime + size: a reinstall replaces the file, so at
    # least one of these moves even when the filesystem reuses inodes.
    return (str(resolved), st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def install_fingerprint() -> Fingerprint | None:
    """:func:`_fingerprint_for` applied to the imported ``precis`` package."""
    import precis

    if precis.__file__ is None:  # pragma: no cover — namespace-pkg guard
        return None
    return _fingerprint_for(Path(precis.__file__))


def _install_replaced(baseline: Fingerprint) -> bool:
    """One poll: has the install diverged from ``baseline``?

    A missing file counts as replaced — mid-reinstall the old tree is
    partially deleted, and that window is exactly the wedge.
    """
    try:
        return install_fingerprint() != baseline
    except OSError:
        return True


def _breadcrumb_path() -> Path:
    """Stable per-host path for the "why did the last server end" note.

    Reuses :func:`precis.config.cache_root` — the same ``~/.cache/precis/*``
    root the patent/edgar raw-mirror caches already write under — so this
    needs no new directory, no new permission, and no configuration.
    """
    from precis.config import cache_root

    return cache_root("server-state") / "last-exit.json"


def _short_fingerprint(fp: Fingerprint | None) -> str | None:
    """Digest a :data:`Fingerprint` down to 8 hex chars for a one-line
    status message — the raw tuple (absolute path + inode + two
    nanosecond timestamps + size) is unreadable there."""
    if fp is None:
        return None
    return hashlib.sha256(repr(fp).encode("utf-8")).hexdigest()[:8]


def _git_sha_short() -> str:
    """Best-effort short git sha of the running checkout.

    Deliberately independent of ``handlers/skill.py``'s fuller build-info
    chain (env-baked image args, ``direct_url.json`` fallback, …): that
    module consumes this one's breadcrumb (see module docstring), and
    importing it back here would cycle. ``"unknown"`` covers every
    non-git-checkout case (installed wheel, no ``git`` binary) — same
    fallback value the fuller chain uses.
    """
    import precis

    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(Path(precis.__file__).resolve().parent),
                "rev-parse",
                "--short=12",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    return proc.stdout.strip() or "unknown"


def _write_exit_breadcrumb(
    reason: str,
    *,
    old_fingerprint: Fingerprint | None = None,
    new_fingerprint: Fingerprint | None = None,
    detail: str | None = None,
) -> None:
    """Drop a small "why did the previous server end" note (gr341515).

    Called from two places: immediately before the watchdog's
    ``os._exit(0)`` (``reason="install-swapped"``), and from the
    crash/exit hooks :func:`install_exit_breadcrumb_hooks` registers
    (``reason="crash"``/``"exit"``). Both call sites are exit paths —
    an exception or a block here would itself become the failure it's
    trying to record, so every step is best-effort and swallowed.
    """
    try:
        payload: dict[str, object] = {
            "written_at": datetime.now(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "reason": reason,
            "old_fingerprint": _short_fingerprint(old_fingerprint),
            "new_fingerprint": _short_fingerprint(new_fingerprint),
            "git_sha": _git_sha_short(),
            "pid": os.getpid(),
        }
        if detail:
            payload["detail"] = detail
        path = _breadcrumb_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:  # pragma: no cover — best-effort, never block an exit
        log.debug("install watchdog: exit breadcrumb write failed", exc_info=True)


def consume_last_exit_breadcrumb() -> dict[str, object] | None:
    """Read + delete the previous run's exit breadcrumb, if any.

    Called by ``handlers/skill.py`` when rendering ``precis-status``.
    "Consume" (delete on read) is the age-out mechanism gr341515 item 3
    asks for, picked over a TTL/staleness check for the simpler
    invariant it gives: a breadcrumb is surfaced exactly once, ever,
    regardless of how long the previous server stayed up or how long
    this one waits before its first ``precis-status`` call — no
    clock-skew or "how old is too old" judgement call needed. The
    downside is symmetric: a breadcrumb nobody ever asks about lingers
    until someone does, which is the *point* — the message answers "why
    did the server I'm about to talk to not already know something",
    not "poll this proactively".
    """
    path = _breadcrumb_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:  # pragma: no cover — already-gone race, harmless
        pass
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


#: Set by :func:`_excepthook` when a genuine unhandled exception reaches
#: it, so the paired :func:`_atexit_breadcrumb` can tell "crashed" from
#: "exited normally" — both fire from the same process-teardown moment,
#: but only one of them saw the exception.
_crash_detail: str | None = None


def _excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    global _crash_detail
    _crash_detail = f"{exc_type.__name__}: {exc}"
    _ORIGINAL_EXCEPTHOOK(exc_type, exc, tb)  # never swallow — same stderr trace


#: The interpreter's default hook, captured once at import — chaining to
#: *this* frozen value (rather than whatever ``sys.excepthook`` currently
#: is) keeps repeated :func:`install_exit_breadcrumb_hooks` calls
#: idempotent instead of nesting a new wrapper layer each time.
_ORIGINAL_EXCEPTHOOK = sys.excepthook


def _atexit_breadcrumb() -> None:
    reason = "crash" if _crash_detail is not None else "exit"
    _write_exit_breadcrumb(reason, detail=_crash_detail)


#: Guards :func:`install_exit_breadcrumb_hooks` against registering a
#: second ``atexit`` callback on a repeat call (every
#: :func:`start_install_watchdog` call, including in tests) — ``atexit``
#: itself has no "already registered" dedup, so without this a process
#: that calls it N times would write the same breadcrumb N times at exit.
_hooks_installed = False


def install_exit_breadcrumb_hooks() -> None:
    """Arm the crash/exit breadcrumb (gr341515 item 2).

    ``os._exit(0)`` — what the watchdog itself calls — skips ``atexit``
    entirely by design, so this can never double-write against the
    watchdog's own explicit ``install-swapped`` breadcrumb; the two
    reasons are mutually exclusive by construction, not by a check here.
    Safe (and a no-op past the first call — see :data:`_hooks_installed`)
    to call more than once; ``start_install_watchdog`` calls it
    unconditionally so a crash is diagnosable even when the watchdog
    itself stays unarmed (source install, or ``PRECIS_INSTALL_WATCHDOG=0``).
    """
    global _hooks_installed
    if _hooks_installed:
        return
    sys.excepthook = _excepthook
    atexit.register(_atexit_breadcrumb)
    _hooks_installed = True


class InstallWatchdog(threading.Thread):
    """The poll thread, with a ``stop()`` (gr347099).

    Without one, a thread armed against a throwaway baseline outlives its
    creator and fires the real ``os._exit(0)`` later — two tests did
    exactly that with a 3600 s interval, killing their xdist worker one
    hour into any gate slow enough to still be running (the "hangs at
    95%" signature). The server never stops its watchdog; the handle is
    for tests and for any future embedder that re-arms.
    """

    def __init__(self, *, baseline: Fingerprint, interval_s: float) -> None:
        super().__init__(name="install-watchdog", daemon=True)
        self._baseline = baseline
        self._interval_s = interval_s
        self._stop_event = threading.Event()

    def stop(self, timeout: float | None = 5.0) -> None:
        """Ask the loop to end and wait for it (a no-op if never started)."""
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)

    def run(self) -> None:
        while not self._stop_event.wait(self._interval_s):
            if _install_replaced(self._baseline):
                try:
                    current = install_fingerprint()
                except OSError:
                    current = None
                _write_exit_breadcrumb(
                    "install-swapped",
                    old_fingerprint=self._baseline,
                    new_fingerprint=current,
                )
                log.warning(
                    "install watchdog: %s replaced on disk — exiting cleanly "
                    "so the MCP client restarts a fresh server (gr338977)",
                    self._baseline[0],
                )
                sys.stderr.flush()
                os._exit(0)


def start_install_watchdog(
    *, interval_s: float = _DEFAULT_INTERVAL_S
) -> InstallWatchdog | None:
    """Arm the watchdog; returns the thread (``.stop()`` ends it), or
    ``None`` when not armed (disabled by env, source install, or
    unstat-able baseline)."""
    install_exit_breadcrumb_hooks()
    if os.environ.get("PRECIS_INSTALL_WATCHDOG", "1") == "0":
        log.debug("install watchdog: disabled by PRECIS_INSTALL_WATCHDOG=0")
        return None
    try:
        baseline = install_fingerprint()
    except OSError:  # pragma: no cover — boot-time stat race
        log.debug("install watchdog: baseline stat failed — not watching")
        return None
    if baseline is None:
        log.debug("install watchdog: source install — not watching")
        return None

    thread = InstallWatchdog(baseline=baseline, interval_s=interval_s)
    thread.start()
    log.info("install watchdog armed on %s (every %.0fs)", baseline[0], interval_s)
    return thread
