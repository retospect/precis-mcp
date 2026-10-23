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


def _start_acquire(
    repo: Path, done: Path, *, slots: int | None = 1
) -> subprocess.Popen:
    """Run the real ``gate_slot_acquire`` in the background.

    ``slots=None`` leaves ``PRECIS_GATE_SLOTS`` unset, exercising the script's
    default cap. On success it writes the won slot dir to ``done``, then
    sleeps so the slot stays held while the test inspects it.
    """
    script = (
        f'cd "{repo}"; source "{_LIB}"; '
        f'gate_slot_acquire; printf "%s" "$GATE_SLOT_DIR" > "{done}"; sleep 30'
    )
    env = {**os.environ}
    if slots is None:
        env.pop("PRECIS_GATE_SLOTS", None)
    else:
        env["PRECIS_GATE_SLOTS"] = str(slots)
    return subprocess.Popen(
        ["bash", "-c", script],
        env=env,
        stderr=subprocess.DEVNULL,
    )


def _hold_slot(repo: Path, index: int, pid: int) -> Path:
    """Occupy slot ``index`` as a live same-host holder would."""
    d = repo / ".git" / f"precis-gate-slot-{index}.lock.d"
    d.mkdir()
    (d / "holder").write_text(
        f"{repo} pid={pid} host={_this_host()}\n", encoding="utf-8"
    )
    return d


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
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    assert not ticket.exists(), (
        "gate_slot_release left the waiter's ticket in the queue"
    )


def test_default_cap_is_three_and_env_override_wins(repo: Path, tmp_path: Path) -> None:
    """The default cap is exactly 3 (OrbStack sizing, 2026-10-21).

    ``PRECIS_GATE_SLOTS`` unset: with slots 0-1 held an acquirer must win
    slot 2 (default >= 3), and once 0-2 are all held a second acquirer must
    WAIT (default <= 3). The env still overrides: a third acquirer under
    ``PRECIS_GATE_SLOTS=4`` takes slot 3 while the capped one still waits.
    """
    holder = subprocess.Popen(["sleep", "60"])
    done_a = tmp_path / "done-a"
    done_b = tmp_path / "done-b"
    done_wide = tmp_path / "done-wide"
    proc_a = proc_b = proc_wide = None
    try:
        for i in range(2):
            _hold_slot(repo, i, holder.pid)

        # Lower edge: the default cap reaches slot 2.
        proc_a = _start_acquire(repo, done_a, slots=None)
        assert _wait_for(done_a, timeout=10), (
            "an acquirer with PRECIS_GATE_SLOTS unset never took the free "
            "slot 2 — the default cap is under 3"
        )
        assert done_a.read_text(encoding="utf-8").endswith("precis-gate-slot-2.lock.d")

        # Upper edge: slots 0-2 held (0-1 by `holder`, 2 by proc_a itself)
        # and the default-capped waiter must queue, not reach a slot 3.
        proc_b = _start_acquire(repo, done_b, slots=None)
        assert not _wait_for(done_b, timeout=7), (
            "a default-capped acquirer won a fourth slot while 0-2 are held "
            "— the default cap is over 3"
        )

        # The env override still wins: =4 admits the waiter to slot 3.
        proc_wide = _start_acquire(repo, done_wide, slots=4)
        assert _wait_for(done_wide, timeout=10), (
            "PRECIS_GATE_SLOTS=4 did not take the free slot 3 — the env "
            "override no longer wins"
        )
        assert done_wide.read_text(encoding="utf-8").endswith(
            "precis-gate-slot-3.lock.d"
        )
        assert not done_b.exists(), (
            "the default-capped waiter slipped past its 3-slot bound"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)
        for proc in (proc_a, proc_b, proc_wide):
            if proc is not None:
                proc.terminate()
                proc.wait(timeout=10)
