"""A live lock holder must never be stolen from, however long it has held.

Found 2026-09-14, in production: a ``/go`` settle-up stole the ship lock from a
sibling worktree 70 minutes into a healthy ``scripts/ship --mutate`` run and
announced "assuming crashed or from another host". The holder was alive on this
host. Two concurrent ships then ran against one shared ``.git`` until it was
noticed by hand.

Both mkdir-mutexes had written the same defect independently. Each documented
two ordered reclaim paths — pid-dead first, age as *the fallback for when the
pid check cannot decide* — but the pid-dead branch consumes the only case where
the pid check fails, so the age branch was reached exactly when the holder was
alive and parseable. Any hold past the timer was stolen from a running process:
30 min for the ship lock (``scripts/ship`` §3), re-opening the shared-index
clobber race the mutex exists to prevent, and 45 min for a gate slot
(``scripts/lib/gate-slot.sh``), admitting a third gate container into a 2-slot
semaphore sized to the shared ~8GB VM — i.e. manufacturing the OOM churn of
gr202193.

These are behavioural tests rather than text assertions because the bug was
invisible in the text: both scripts *said* the right rule in their comments.
Only executing the decision shows which branch is reachable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# The subject is bash: these sh-spawning, `hostname -s`, pid-signalling tests
# describe locks that only exist on the POSIX hosts that run ships and gates.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: bash mkdir-mutexes, pids, hostname"
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = _REPO_ROOT / "scripts" / "lib" / "lock-holder.sh"

#: Comfortably past both call sites' timers (ship 30 min, gate slot 45 min).
_AGED_MINUTES = 90


def _reclaim(lockdir: Path, max_age_min: int = 30) -> tuple[bool, str]:
    """Run ``lock_holder_reclaim_reason`` against a real lock dir.

    Returns ``(may_steal, reason)`` — exit 0 means the lock is reclaimable.
    """
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{_LIB}"; lock_holder_reclaim_reason "$1" "$2"',
            "_",
            str(lockdir),
            str(max_age_min),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return (proc.returncode == 0, proc.stdout.strip())


def _age(lockdir: Path, minutes: int = _AGED_MINUTES) -> None:
    """Backdate the lock dir's mtime — what ``find -mmin`` reads.

    Uses ``os.utime`` rather than ``touch -t`` so the test is portable between
    the macOS host and the Linux gate container.
    """
    old = time.time() - minutes * 60
    os.utime(lockdir, (old, old))


def _write_holder(lockdir: Path, *, pid: int, host: str | None) -> None:
    host_part = f" host={host}" if host is not None else ""
    (lockdir / "holder").write_text(
        f"/some/worktree pid={pid}{host_part}\n", encoding="utf-8"
    )


@pytest.fixture
def lockdir(tmp_path: Path) -> Path:
    d = tmp_path / "precis-ship.lock.d"
    d.mkdir()
    return d


def _this_host() -> str:
    return subprocess.run(
        ["hostname", "-s"], capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def test_live_local_holder_is_never_stolen_even_when_ancient(lockdir: Path) -> None:
    """The regression. A 90-minute hold by a LIVE pid stays untouched."""
    _write_holder(lockdir, pid=os.getpid(), host=_this_host())
    _age(lockdir)

    may_steal, reason = _reclaim(lockdir)
    assert not may_steal, (
        "a live holder on this host was declared reclaimable "
        f"(reason: {reason!r}) — this is the concurrent-ship bug: a slow "
        "`ship --mutate` loses its lock to a sibling and both ships run"
    )


def test_live_local_holder_without_host_field_is_still_protected(
    lockdir: Path,
) -> None:
    """Holder files predating ``host=`` are treated as local, not foreign.

    Back-compat matters in the transition: an in-flight ship wrote its holder
    with the old format, and it must not become stealable the moment the new
    code ships.
    """
    _write_holder(lockdir, pid=os.getpid(), host=None)
    _age(lockdir)

    may_steal, reason = _reclaim(lockdir)
    assert not may_steal, (
        f"legacy holder file with a live pid was reclaimable (reason: {reason!r})"
    )


def test_dead_holder_is_stolen_immediately_without_waiting_out_the_timer(
    lockdir: Path,
) -> None:
    """The path that must keep working: crashed ship, fresh lock dir."""
    dead_pid = _spawn_and_reap()
    _write_holder(lockdir, pid=dead_pid, host=_this_host())
    # Deliberately NOT aged — a dead holder should not need the timer.

    may_steal, reason = _reclaim(lockdir)
    assert may_steal, "a dead holder's lock must be reclaimable at once"
    assert "dead" in reason, f"unexpected reason for a dead holder: {reason!r}"


def test_foreign_host_holder_is_reclaimed_on_age_only(lockdir: Path) -> None:
    """A foreign pid means nothing locally, so only the timer can reclaim it."""
    _write_holder(lockdir, pid=999_999, host="some-other-node")

    may_steal, _ = _reclaim(lockdir)
    assert not may_steal, "a fresh foreign lock must be waited out, not stolen"

    _age(lockdir)
    may_steal, reason = _reclaim(lockdir)
    assert may_steal, "an aged foreign lock is the case the timer exists for"
    assert "another host" in reason, f"unexpected reason: {reason!r}"


def test_unparseable_holder_is_reclaimed_on_age_only(lockdir: Path) -> None:
    """Crashed before writing a holder: the other case the timer exists for."""
    may_steal, _ = _reclaim(lockdir)
    assert not may_steal, (
        "a fresh lock with no holder file must be waited out — the holder may "
        "be mid-write, and stealing it has no recovery"
    )

    _age(lockdir)
    may_steal, reason = _reclaim(lockdir)
    assert may_steal, "an aged lock with no parseable holder is abandoned"
    assert "no parseable holder" in reason, f"unexpected reason: {reason!r}"


def test_live_local_holder_survives_the_gate_slot_timer_too(lockdir: Path) -> None:
    """Same rule, the other call site's timer (45 min, gate-slot.sh)."""
    _write_holder(lockdir, pid=os.getpid(), host=_this_host())
    _age(lockdir)

    may_steal, reason = _reclaim(lockdir, max_age_min=45)
    assert not may_steal, (
        f"live gate-slot holder was reclaimable (reason: {reason!r}) — a third "
        "gate container against the shared VM is the gr202193 OOM churn"
    )


def test_write_then_read_round_trip_protects_the_writer(lockdir: Path) -> None:
    """What the real scripts do: stamp ownership, then be judged by the rule.

    Covers the seam the unit cases skip — a holder file this code *wrote* must
    be recognised as a live local holder. A format drift between
    ``lock_holder_write`` and the ``pid=``/``host=`` parsers would leave every
    lock stealable on age while all the cases above still passed.
    """
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{_LIB}"; lock_holder_write "$1"; '
            f'lock_holder_reclaim_reason "$1" 30 && echo STEALABLE',
            "_",
            str(lockdir),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    holder = (lockdir / "holder").read_text(encoding="utf-8")
    assert "pid=" in holder and "host=" in holder, (
        f"lock_holder_write produced an unparseable holder: {holder!r}"
    )
    assert "STEALABLE" not in proc.stdout, (
        "a lock is stealable immediately after being claimed — write/parse "
        f"formats disagree. holder={holder!r} reason={proc.stdout.strip()!r}"
    )
    # The aged-but-live case is covered above with a pid that outlives the
    # check; it cannot be asserted here because `lock_holder_write` records
    # `$$` — this bash -c exits, so its holder is then legitimately dead.


def _spawn_and_reap() -> int:
    """A pid that is definitely not running: spawn, wait, return it.

    Reaped rather than merely killed so ``kill -0`` cannot see a zombie.
    """
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid
