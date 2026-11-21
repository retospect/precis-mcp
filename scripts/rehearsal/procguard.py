"""Signals only to processes that belong to this run.

A recorded pid or process group can be stale (after a reboot, after a worker
exit) and then name someone else's process, perhaps a production session that
leads its own group. Nothing in the harness signals a group on the strength
of a recorded id alone: :func:`signal_group` first reads the process table and
signals only when a member of the group has this run's directory in its
command line (the worker, a scripted session, a ``scripts/ship`` of a land
folder all do: their interpreter, script or working folder lies under the run
directory). :func:`end_run_processes` finds every such group, however it was
started (the worker's detached children run in sessions of their own), and
ends them.
"""

from __future__ import annotations

import os
import re
import signal
import time
from pathlib import Path

from . import proc as launch


def _holds(run_dir: Path, command: str) -> bool:
    """Whether ``command`` names a path under ``run_dir`` (``run-2`` is not
    ``run-20``: the directory name must end at a separator or the end)."""
    return re.search(re.escape(str(run_dir)) + r"(?=[/\s]|$)", command) is not None


def process_table() -> list[tuple[int, int, str]]:
    """``[(pid, pgid, command)]`` of every process, full command lines."""
    out = launch.run(["ps", "-axwwo", "pid=,pgid=,command="], timeout=30)
    rows: list[tuple[int, int, str]] = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def run_groups(run_dir: Path, *, exclude: tuple[int, ...] = ()) -> set[int]:
    """Process groups with a member whose command line holds ``run_dir``
    (never the caller's own group)."""
    own = {os.getpgrp(), *exclude}
    return {
        pgid
        for pid, pgid, cmd in process_table()
        if _holds(run_dir, cmd) and pgid not in own and pgid > 1 and pid != os.getpid()
    }


def group_belongs(pgid: int, run_dir: Path) -> bool:
    """Whether a member of group ``pgid`` has ``run_dir`` in its command."""
    return pgid > 1 and any(
        _holds(run_dir, cmd) for _pid, group, cmd in process_table() if group == pgid
    )


def signal_group(pgid: int, sig: int, run_dir: Path) -> bool:
    """Signal group ``pgid`` when it belongs to the run; ``False`` when it
    does not (or is gone). Never signals a group it cannot identify."""
    if not group_belongs(pgid, run_dir):
        return False
    try:
        os.killpg(pgid, sig)
    except OSError:
        return False
    return True


def end_run_processes(run_dir: Path, *, grace: float = 4.0) -> list[int]:
    """TERM, then after ``grace`` seconds KILL, every group of the run.
    Returns the group ids that were signalled."""
    groups = sorted(run_groups(run_dir))
    for pgid in groups:
        signal_group(pgid, signal.SIGTERM, run_dir)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and run_groups(run_dir):
        time.sleep(0.2)
    for pgid in sorted(run_groups(run_dir)):
        signal_group(pgid, signal.SIGKILL, run_dir)
    return groups
