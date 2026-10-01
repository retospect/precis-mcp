"""The gate-slot semaphore must serve waiters in arrival order (gr294498).

Found 2026-09-02 in production: a small 2-file ``scripts/test`` run starved for
~55 min under heavy sibling congestion. ``gate_slot_acquire`` was a plain unfair
spin-race — every waiter re-scanned the slots and ``mkdir``'d on each 3 s wake-up
with no arrival order, so a slot freed mid-run went to whichever sibling's
``mkdir`` happened to land first. Under a constant stream of freshly-arriving
siblings a long-queued job kept losing the race until the 45-min forced-steal
finally fired.

The fix gives each waiter an arrival ticket in a shared queue dir and only lets
it enter the ``mkdir`` race while it is among the oldest ``PRECIS_GATE_SLOTS``
outstanding tickets. These are behavioural tests: the real ``gate_slot_acquire``
is sourced and run against real queue/slot dirs under a throwaway git repo, so
they exercise the actual admission decision, not a transcription of it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# The subject is bash: mkdir-mutexes, `hostname -s`, pid liveness. It only runs
# on the POSIX hosts that run gates.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: bash mkdir-mutex, pids, hostname"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = _REPO_ROOT / "scripts" / "lib" / "gate-slot.sh"

#: An epoch that predates any real waiter, so a ticket carrying it always sorts
#: ahead of one this test's acquire mints with `date +%s`.
_ANCIENT_EPOCH = 1_000_000_000


def _this_host() -> str:
    return (
        subprocess.run(
            ["hostname", "-s"], capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()
        or "unknown"
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo whose common dir hosts the queue and slot dirs."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _queue_dir(repo: Path) -> Path:
    q = repo / ".git" / "precis-gate-queue.d"
    q.mkdir(parents=True, exist_ok=True)
    return q


def _slot_dirs(repo: Path) -> list[Path]:
    return sorted((repo / ".git").glob("precis-gate-slot-*.lock.d"))


def _start_acquire(repo: Path, done: Path, *, slots: int = 1) -> subprocess.Popen:
    """Run the real ``gate_slot_acquire`` in the background.

    On success it writes the won slot dir to ``done``, then sleeps so the slot
    stays held while the test inspects it.
    """
    script = (
        f'cd "{repo}"; source "{_LIB}"; '
        f'gate_slot_acquire; printf "%s" "$GATE_SLOT_DIR" > "{done}"; sleep 30'
    )
    return subprocess.Popen(
        ["bash", "-c", script],
        env={**os.environ, "PRECIS_GATE_SLOTS": str(slots)},
        stderr=subprocess.DEVNULL,
    )


def _wait_for(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.2)
    return path.exists()


def test_acquires_immediately_when_uncongested(repo: Path, tmp_path: Path) -> None:
    """The common path: an empty queue wins a slot at once."""
    done = tmp_path / "done"
    proc = _start_acquire(repo, done, slots=1)
    try:
        assert _wait_for(done, timeout=10), "an uncongested acquire never got a slot"
        assert _slot_dirs(repo), "acquire reported success but created no slot dir"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_older_live_waiter_wins_the_only_slot(repo: Path, tmp_path: Path) -> None:
    """The regression: a fresh arrival must NOT jump an older live waiter.

    A live ticket sits ahead of us with only one slot, so we must wait — the old
    unfair race would grab the free slot at once. When the older ticket clears we
    take the slot, proving the wait was arrival-order, not a deadlock.
    """
    qdir = _queue_dir(repo)
    older = subprocess.Popen(["sleep", "60"])  # a live pid to sit ahead of us
    older_ticket = qdir / f"{_ANCIENT_EPOCH}-{older.pid}-{_this_host()}"
    older_ticket.touch()

    done = tmp_path / "done"
    proc = _start_acquire(repo, done, slots=1)
    try:
        # Give it several 3 s poll cycles; a fair acquirer holds off the whole time.
        assert not _wait_for(done, timeout=7), (
            "acquire took the only slot while an OLDER live waiter was queued — "
            "the gr294498 starvation race is back"
        )
        assert not _slot_dirs(repo), "no slot should be held while we wait our turn"

        older_ticket.unlink()  # older waiter leaves the queue
        assert _wait_for(done, timeout=10), (
            "acquire never took the slot after the older ticket cleared"
        )
        assert _slot_dirs(repo), "won the queue but created no slot dir"
    finally:
        older.terminate()
        older.wait(timeout=10)
        proc.terminate()
        proc.wait(timeout=10)


def test_dead_ticket_ahead_is_pruned_not_blocking(repo: Path, tmp_path: Path) -> None:
    """A leaked ticket (waiter -9'd before its trap ran) must not wedge the queue.

    Its owning pid is dead on this host, so the prune drops it at once and we
    acquire without waiting out any timer.
    """
    qdir = _queue_dir(repo)
    reaped = subprocess.Popen(["true"])
    reaped.wait()  # reaped, so kill -0 cannot even see a zombie
    dead_ticket = qdir / f"{_ANCIENT_EPOCH}-{reaped.pid}-{_this_host()}"
    dead_ticket.touch()

    done = tmp_path / "done"
    proc = _start_acquire(repo, done, slots=1)
    try:
        assert _wait_for(done, timeout=10), (
            "a dead ticket ahead blocked acquisition — the prune did not reap it"
        )
        assert not dead_ticket.exists(), "the dead-pid ticket should have been pruned"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_release_clears_an_unacquired_waiters_ticket(
    repo: Path, tmp_path: Path
) -> None:
    """The EXIT-trap path (``gate_slot_release``) drops a still-queued ticket.

    A waiter aborted before it won a slot must not leave its arrival ticket in
    the queue, or it would sit forever ahead of real waiters. Exercises the
    release branch directly against a ticket this process is registered to hold.
    """
    qdir = _queue_dir(repo)
    ticket = qdir / f"{_ANCIENT_EPOCH}-{os.getpid()}-{_this_host()}"
    ticket.touch()
    script = (
        f'cd "{repo}"; source "{_LIB}"; '
        f'GATE_SLOT_TICKET="{ticket}"; gate_slot_release; '
        # A second call must be a harmless no-op (idempotent EXIT trap).
        "gate_slot_release"
    )
    subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert not ticket.exists(), (
        "gate_slot_release left the waiter's ticket in the queue"
    )
