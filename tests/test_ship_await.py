"""``scripts/ship --await`` — the per-worktree wait on a backgrounded run
(gr541681).

Every ship run writes a state record (pid, log, start, then status/exit/
ship_status) to ``$TMPDIR/precis-ship-state-<worktree cksum>`` — the
``PRECIS_SHIP_STATE_FILE`` seam points the record at a tmp path here, so
no real ship ever runs. The bare ``--await`` form polls that file every
5 s, exits with the recorded exit code once ``status=done`` lands, exits
124 + the log path on ``--timeout``, and exits 2 when nothing is recorded.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

REPO = Path(__file__).resolve().parent.parent
_SHIP = REPO / "scripts" / "ship"
_BASH = "/bin/bash"  # macOS ships 3.2: the version scripts/ship must run on

RED_LOG = """\
▶ sync — fetching origin main
▶ gate — ruff over the diff
✖ gate RED — ruff found 2 errors
  src/precis/foo.py:12: unused import
  src/precis/foo.py:30: line too long
extra-log-line-3
extra-log-line-4
extra-log-line-5
extra-log-line-6
extra-log-line-7
extra-log-line-8
extra-log-line-9
extra-log-line-10
extra-log-line-11
extra-log-line-12 beyond the block
SHIP_STATUS: error
"""


def _await(state: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PRECIS_SHIP_STATE_FILE": str(state)}
    return subprocess.run(
        [_BASH, str(_SHIP), "--await", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=env,
    )


def _state(
    path: Path,
    *,
    pid: int = 1,
    log: Path | None = None,
    status: str,
    exit_code: int | None = None,
    ship_status: str | None = None,
) -> None:
    lines = [
        f"pid={pid}",
        f"log={log if log is not None else ''}",
        "start=1700000000",
        f"status={status}",
    ]
    if exit_code is not None:
        lines.append(f"exit={exit_code}")
    if ship_status is not None:
        lines.append(f"ship_status={ship_status}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_await_no_recorded_ship_exits_2(tmp_path: Path) -> None:
    """Nothing ever shipped from this worktree: refuse with one line."""
    proc = _await(tmp_path / "absent.state")
    assert proc.returncode == 2
    assert "no ship recorded" in proc.stderr


def test_await_finished_ship_returns_exit_and_status(tmp_path: Path) -> None:
    """A done record answers immediately: the SHIP_STATUS line, the log's
    first ✖ block (the line plus the 10 after it), and the recorded exit."""
    log = tmp_path / "ship.log"
    log.write_text(RED_LOG, encoding="utf-8")
    state = tmp_path / "ship.state"
    _state(state, log=log, status="done", exit_code=3, ship_status="SHIP_STATUS: error")
    proc = _await(state)
    assert proc.returncode == 3
    assert "SHIP_STATUS: error" in proc.stdout
    assert "✖ gate RED — ruff found 2 errors" in proc.stdout
    assert "extra-log-line-10" in proc.stdout
    assert "beyond the block" not in proc.stdout


def test_await_shipped_ship_exits_0(tmp_path: Path) -> None:
    state = tmp_path / "ship.state"
    _state(state, status="done", exit_code=0, ship_status="SHIP_STATUS: shipped")
    proc = _await(state)
    assert proc.returncode == 0
    assert "SHIP_STATUS: shipped" in proc.stdout


def test_await_dead_pid_ends_the_wait(tmp_path: Path) -> None:
    """A kill never reaches the EXIT trap: status stays running but the
    recorded pid is gone, so the wait derives the verdict from the log."""
    log = tmp_path / "ship.log"
    log.write_text(RED_LOG, encoding="utf-8")
    state = tmp_path / "ship.state"
    _state(state, pid=999999999, log=log, status="running")
    proc = _await(state)
    assert proc.returncode == 1
    assert "SHIP_STATUS: error" in proc.stdout


def test_await_permission_denied_pid_is_alive_not_dead(tmp_path: Path) -> None:
    """gr626879: an observer that may not signal the ship's pid (another
    user, or outside its sandbox) gets EPERM from ``kill -0`` — that is a
    LIVE process. The wait keeps going (124 at --timeout) instead of ending
    in a false ``SHIP_STATUS: error``. pid 1 is foreign to any non-root
    caller; under root it is simply alive — both must keep waiting."""
    log = tmp_path / "ship.log"
    log.write_text("partial\n", encoding="utf-8")
    state = tmp_path / "ship.state"
    _state(state, pid=1, log=log, status="running")
    proc = _await(state, "--timeout", "0")
    assert proc.returncode == 124, proc.stdout + proc.stderr
    assert "SHIP_STATUS" not in proc.stdout


def test_await_interrupted_record_never_reads_as_shipped(tmp_path: Path) -> None:
    """A signalled run records exit 143 + error + ``interrupted=1``: the
    wait relays 143, says it was interrupted, and prints no shipped token."""
    state = tmp_path / "ship.state"
    _state(state, status="done", exit_code=143, ship_status="SHIP_STATUS: error")
    with state.open("a", encoding="utf-8") as fh:
        fh.write("interrupted=1\n")
    proc = _await(state)
    assert proc.returncode == 143
    assert "SHIP_STATUS: error" in proc.stdout
    assert "shipped" not in proc.stdout.replace("nothing landed", "")
    assert "killed by a signal" in proc.stdout


def test_await_exit_zero_with_error_token_does_not_exit_zero(tmp_path: Path) -> None:
    """Whatever wrote the record, exit 0 never rides under a non-shipped token."""
    state = tmp_path / "ship.state"
    _state(state, status="done", exit_code=0, ship_status="SHIP_STATUS: error")
    proc = _await(state)
    assert proc.returncode == 1
    assert "SHIP_STATUS: shipped" not in proc.stdout


def test_await_dead_pid_needs_the_land_receipt_to_say_shipped(tmp_path: Path) -> None:
    """A run killed before its exit trap, with no token in its log: the
    prose 'landed on main' is not a receipt; the post-CAS banner is."""
    state = tmp_path / "ship.state"
    prose = tmp_path / "prose.log"
    prose.write_text("▶ gate\nnote: nothing has landed on main yet\n", encoding="utf-8")
    _state(state, pid=999999999, log=prose, status="running")
    proc = _await(state)
    assert proc.returncode == 1
    assert "SHIP_STATUS: error" in proc.stdout

    landed = tmp_path / "landed.log"
    landed.write_text(
        "▶ gate\n\x1b[1m▶ shipped — landed on main:\x1b[0m\nabc123 work\n",
        encoding="utf-8",
    )
    _state(state, pid=999999999, log=landed, status="running")
    proc = _await(state)
    assert proc.returncode == 0
    assert "SHIP_STATUS: shipped" in proc.stdout


def test_await_timeout_exits_124_with_log_path(tmp_path: Path) -> None:
    """A run still going at --timeout: 124 and the log path to tail."""
    log = tmp_path / "ship.log"
    log.write_text("partial\n", encoding="utf-8")
    state = tmp_path / "ship.state"
    # The test process itself is a live pid the wait can never outlast.
    _state(state, pid=os.getpid(), log=log, status="running")
    proc = _await(state, "--timeout", "0")
    assert proc.returncode == 124
    assert str(log) in proc.stdout
