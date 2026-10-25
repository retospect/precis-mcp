"""Gate-slot wait UX: the rough ETA (gr515328) and the bounded wait
(gr515704).

gr515328: the wait banner showed a queue position but nothing to plan
against. ``gate_slot_release`` now appends each hold's seconds to
``<git-common>/precis-gate-stats`` (kept to the freshest 50) and the
banner prints the recent median plus the implied free-slot interval.

gr515704: the queue is fair but was unbounded — a ``--bg --selected``
builder sat 40+ min at position 1 of 5 behind three sibling gates.
``PRECIS_GATE_WAIT_MAX_S`` (default 1800) now caps the wait; on expiry the
acquire refuses rc 4 with the holder dump instead of queueing forever.

Same staging as test_gate_slot_fairness.py: the real lib sourced under
``bash -c`` against a throwaway git repo with real slot/queue dirs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: bash mkdir-mutex, pids, hostname"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = _REPO_ROOT / "scripts" / "lib" / "gate-slot.sh"


def _this_host() -> str:
    return (
        subprocess.run(
            ["hostname", "-s"], capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()
        or "unknown"
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _hold_slot(repo: Path, index: int, pid: int) -> Path:
    d = repo / ".git" / f"precis-gate-slot-{index}.lock.d"
    d.mkdir()
    (d / "holder").write_text(
        f"{repo} pid={pid} host={_this_host()}\n", encoding="utf-8"
    )
    return d


def _start_acquire(
    repo: Path, done: Path, err: Path, *, slots: int = 1, extra_env: str = ""
) -> subprocess.Popen:
    """``gate_slot_acquire`` in the background; won slot -> ``done``."""
    script = (
        f'cd "{repo}"; source "{_LIB}"; {extra_env}'
        f'gate_slot_acquire; rc=$?; printf "%s" "$GATE_SLOT_DIR" > "{done}"; '
        f'exit "$rc"'
    )
    return subprocess.Popen(
        ["bash", "-c", script],
        env={**os.environ, "PRECIS_GATE_SLOTS": str(slots)},
        stderr=err.open("w", encoding="utf-8"),
    )


def _wait_for(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.2)
    return path.exists()


def test_bounded_wait_refuses_with_holder_dump(repo: Path, tmp_path: Path) -> None:
    """A wait past PRECIS_GATE_WAIT_MAX_S exits 4, names the holders and
    the knob — it does not queue forever."""
    holder = subprocess.Popen(["sleep", "60"])
    try:
        _hold_slot(repo, 0, holder.pid)
        done = tmp_path / "done"
        err = tmp_path / "acquire.err"
        proc = _start_acquire(
            repo, done, err, slots=1, extra_env="PRECIS_GATE_WAIT_MAX_S=2; "
        )
        rc = proc.wait(timeout=30)
        text = err.read_text(encoding="utf-8")
        assert rc == 4, f"expected the bounded refusal, got rc={rc}: {text}"
        assert "gave up after 2s" in text
        assert "PRECIS_GATE_WAIT_MAX_S" in text
        assert f"pid {holder.pid}" in text
        assert not done.exists() or not done.read_text(encoding="utf-8"), (
            "a refused acquire must not report a won slot"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_unbounded_wait_still_waits(repo: Path, tmp_path: Path) -> None:
    """PRECIS_GATE_WAIT_MAX_S=0 keeps the old behaviour: no refusal, the
    waiter out-sits the cap it would have hit."""
    holder = subprocess.Popen(["sleep", "60"])
    try:
        _hold_slot(repo, 0, holder.pid)
        done = tmp_path / "done"
        err = tmp_path / "acquire.err"
        proc = _start_acquire(
            repo, done, err, slots=1, extra_env="PRECIS_GATE_WAIT_MAX_S=0; "
        )
        try:
            # Well past a 2 s bound; must still be queued, not refused.
            assert not _wait_for(done, timeout=7)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        assert "gave up" not in err.read_text(encoding="utf-8")
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_release_records_hold_seconds(repo: Path, tmp_path: Path) -> None:
    """Each release appends one integer hold line to precis-gate-stats —
    the ETA's only data source."""
    stats = repo / ".git" / "precis-gate-stats"
    script = (
        f'cd "{repo}"; source "{_LIB}"; gate_slot_acquire; sleep 1; gate_slot_release'
    )
    cp = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "PRECIS_GATE_SLOTS": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    assert stats.is_file(), f"no stats file written: {cp.stderr}"
    lines = stats.read_text(encoding="utf-8").split()
    assert len(lines) == 1
    assert lines[0].isdigit() and int(lines[0]) >= 1


def test_wait_banner_prints_rough_eta(repo: Path, tmp_path: Path) -> None:
    """With >=3 recorded holds the wait banner carries the median-based
    rough ETA next to the queue position."""
    stats = repo / ".git" / "precis-gate-stats"
    stats.write_text("600\n700\n800\n", encoding="utf-8")
    holder = subprocess.Popen(["sleep", "60"])
    try:
        _hold_slot(repo, 0, holder.pid)
        done = tmp_path / "done"
        err = tmp_path / "acquire.err"
        proc = _start_acquire(repo, done, err, slots=1)
        try:
            # First announce fires on the first poll cycle; give it two.
            time.sleep(5)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        text = err.read_text(encoding="utf-8")
        assert "median recent hold ~11m" in text, text
        assert "a slot frees ~every" in text, text
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_no_eta_without_history(repo: Path, tmp_path: Path) -> None:
    """Under 3 recorded holds no ETA line prints — a median of 1-2 points
    would be noise, not a hint."""
    stats = repo / ".git" / "precis-gate-stats"
    stats.write_text("600\n", encoding="utf-8")
    holder = subprocess.Popen(["sleep", "60"])
    try:
        _hold_slot(repo, 0, holder.pid)
        done = tmp_path / "done"
        err = tmp_path / "acquire.err"
        proc = _start_acquire(repo, done, err, slots=1)
        try:
            time.sleep(5)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        text = err.read_text(encoding="utf-8")
        assert "median recent hold" not in text
        assert "gate slot" in text  # the position line itself still prints
    finally:
        holder.terminate()
        holder.wait(timeout=10)
