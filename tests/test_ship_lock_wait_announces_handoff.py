"""The ship-lock wait names the LIVE holder (gr343941, comment 9).

``acquire_ship_lock`` is a 3 s mkdir poll with no ticket order, so the lock
routinely passes from the holder first seen to a fresh acquirer that
started its attempt at the right moment. The wait used to print "held by"
once and then sleep silently, so a log tail showed a holder that had exited
hours earlier — a lost handoff indistinguishable from a hang. Now every
holder change re-announces.

Behavioural: the real function is lifted out of ``scripts/ship`` and run
against a real lock dir whose holder file a background writer swaps, then
removes.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: bash mkdir-mutex"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SHIP = _REPO_ROOT / "scripts" / "ship"


def _acquire_fn() -> str:
    text = _SHIP.read_text(encoding="utf-8")
    m = re.search(r"^acquire_ship_lock\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "acquire_ship_lock() not found in scripts/ship"
    return m.group(0)


def test_wait_reannounces_when_the_holder_changes(tmp_path: Path) -> None:
    lockdir = tmp_path / "precis-ship.lock.d"
    lockdir.mkdir()
    (lockdir / "holder").write_text("worktree=first pid=1 host=x", encoding="utf-8")
    script = f"""
set -u
say() {{ printf '%s\\n' "$*"; }}
sleep() {{ command sleep 0.1; }}
lock_holder_pid() {{ echo 0; }}
lock_holder_reclaim_reason() {{ return 1; }}
lock_holder_write() {{ :; }}
LOCKDIR="{lockdir}"
{_acquire_fn()}
(
  command sleep 0.5
  echo "worktree=second pid=2 host=x" > "$LOCKDIR/holder"
  command sleep 0.5
  rm -rf "$LOCKDIR"
) &
acquire_ship_lock
wait
echo ACQUIRED
"""
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    out = proc.stdout
    assert proc.returncode == 0, proc.stderr
    assert "waiting for the ship lock — held by: worktree=first pid=1 host=x" in out
    assert "ship lock changed hands — now held by: worktree=second pid=2 host=x" in out
    assert out.count("changed hands") == 1, out  # once per change, not per poll
    assert out.strip().endswith("ACQUIRED")
