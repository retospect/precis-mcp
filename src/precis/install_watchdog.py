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
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

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


def start_install_watchdog(
    *, interval_s: float = _DEFAULT_INTERVAL_S
) -> threading.Thread | None:
    """Arm the watchdog; returns the thread, or ``None`` when not armed
    (disabled by env, source install, or unstat-able baseline)."""
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

    def _watch() -> None:
        while True:
            time.sleep(interval_s)
            if _install_replaced(baseline):
                log.warning(
                    "install watchdog: %s replaced on disk — exiting cleanly "
                    "so the MCP client restarts a fresh server (gr338977)",
                    baseline[0],
                )
                sys.stderr.flush()
                os._exit(0)

    thread = threading.Thread(target=_watch, name="install-watchdog", daemon=True)
    thread.start()
    log.info("install watchdog armed on %s (every %.0fs)", baseline[0], interval_s)
    return thread
