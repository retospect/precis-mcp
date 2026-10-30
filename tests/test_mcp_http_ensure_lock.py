"""gr529876 — the mcp-http ensure lock: a live holder is never broken on
age; a destructive mode refuses a held lock, naming the holder.

Two concurrent ``--recreate`` runs once tore down each other's new colour:
the mkdir lock's 2-minute mtime orphan-break fired inside a multi-minute
blue-green swap, so the second run proceeded over the first's. The lock
dir now carries a ``holder`` file (pid, host, mode): a live pid's lock is
waited out (ensure) or refused (--recreate, --recreate-proxy, --migrate);
only a dead holder — or an unreadable/foreign one aged past
LOCK_MAX_AGE_MIN — is reclaimed, atomically (mv-aside + re-judge), so a
stolen check can never delete a lock a racing run just took.

No docker needed: the lock is taken before the first docker call, so a
refusal is exit 3 and a passed lock is proven by reaching the image check
(exit 2 for a guaranteed-absent rig image) or the canonical guard.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ENSURE = _ROOT / "deploy" / "mcp-http" / "precis-mcp-http-ensure.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: runs the bash ensure script"
)


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    """A rig-shaped env: own NAME/STATE/HOME/TMPDIR and an absent image tag."""
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "PRECIS_MCP_HTTP_NAME": f"precis-mcp-lock-{uuid.uuid4().hex[:8]}",
        "PRECIS_MCP_HTTP_STATE": str(tmp_path / "state"),
        "PRECIS_MCP_REPO": str(repo),
        # A tag nothing pulls: a run past the lock and guard stops at the
        # image check with exit 2 and never creates a container.
        "PRECIS_MCP_IMAGE": "precis-mcp-lock-absent:tag",
        "PRECIS_MCP_DB_HOST": "dbnode",
        "PRECIS_MCP_DB_IP": "203.0.113.10",
    }


def _ensure(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_ENSURE), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def _lock(env: dict[str, str]) -> Path:
    return Path(env["TMPDIR"]) / f"{env['PRECIS_MCP_HTTP_NAME']}-ensure.lock"


def _hold(env: dict[str, str], pid: int, age_min: float = 0) -> Path:
    """Take the lock dir as pid $pid (a holder file without host= reads local)."""
    lock = _lock(env)
    lock.mkdir(exist_ok=True)
    (lock / "holder").write_text(
        f"/somewhere pid={pid} mode=recreate\n", encoding="utf-8"
    )
    if age_min:
        old = time.time() - age_min * 60
        os.utime(lock, (old, old))
    return lock


def _live_holder() -> subprocess.Popen[bytes]:
    return subprocess.Popen(["sleep", "60"])


def test_recreate_refuses_a_live_holder_even_when_the_lock_is_old(
    env: dict[str, str],
) -> None:
    """The incident: a swap runs far past the old 2-minute mtime break. An
    hour-old lock held by a live pid must still refuse, not be stolen."""
    holder = _live_holder()
    try:
        _hold(env, holder.pid, age_min=120)
        r = _ensure(env, "--recreate")
    finally:
        holder.kill()
        holder.wait()
    assert r.returncode == 3
    assert "--recreate refused" in r.stderr and "held" in r.stderr
    assert str(holder.pid) in r.stderr
    # Refused before the canonical guard could record a marker.
    state = Path(env["PRECIS_MCP_HTTP_STATE"])
    assert not (state / "main-checkout").exists()


@pytest.mark.parametrize("mode", ["--recreate", "--recreate-proxy", "--migrate"])
def test_destructive_modes_refuse_a_held_lock(env: dict[str, str], mode: str) -> None:
    env["PRECIS_MCP_ENSURE_LOCK_TRIES"] = "2"  # ~0.6 s instead of the ~9 s default
    holder = _live_holder()
    try:
        _hold(env, holder.pid)
        r = _ensure(env, mode)
    finally:
        holder.kill()
        holder.wait()
    assert r.returncode == 3
    assert f"{mode} refused" in r.stderr and str(holder.pid) in r.stderr


def test_ensure_yields_to_a_live_holder(env: dict[str, str]) -> None:
    """A SessionStart hook finding a swap in flight exits 0 — the held run
    will finish the work; proceeding would race it."""
    env["PRECIS_MCP_ENSURE_LOCK_TRIES"] = "2"
    holder = _live_holder()
    try:
        _hold(env, holder.pid)
        r = _ensure(env)
    finally:
        holder.kill()
        holder.wait()
    assert r.returncode == 0
    assert "leaving it to that run" in r.stderr


def test_dead_holder_lock_is_reclaimed_and_released(env: dict[str, str]) -> None:
    dead = subprocess.Popen(["sleep", "0.05"])
    dead_pid = dead.pid
    dead.wait()
    lock = _hold(env, dead_pid)
    r = _ensure(env, "--recreate")
    # Past the lock and the canonical guard: stops at the image check.
    assert r.returncode == 2, r.stderr
    assert "refused" not in r.stderr
    # The stolen dir is gone and the run's own lock was released on exit.
    assert not lock.exists()
    assert list(Path(env["TMPDIR"]).glob("*.stale.*")) == []


def test_unparseable_lock_obeys_only_the_age_bound(env: dict[str, str]) -> None:
    """A run killed between mkdir and the holder write leaves a lock with
    no pid: fresh counts as held (the write gap), aged is reclaimed."""
    env["PRECIS_MCP_ENSURE_LOCK_TRIES"] = "2"
    lock = _lock(env)
    lock.mkdir()
    r = _ensure(env)
    assert r.returncode == 0 and "leaving it to that run" in r.stderr
    old = time.time() - 15 * 60  # LOCK_MAX_AGE_MIN is 10
    os.utime(lock, (old, old))
    r = _ensure(env, "--recreate")
    assert r.returncode == 2, r.stderr


def test_foreign_host_holder_obeys_the_age_bound(env: dict[str, str]) -> None:
    """A holder written on another host: its pid means nothing here, so
    only age reclaims it (pid=1 is alive everywhere — proving the pid
    check is not what frees this lock)."""
    env["PRECIS_MCP_ENSURE_LOCK_TRIES"] = "2"
    lock = _lock(env)
    lock.mkdir()
    (lock / "holder").write_text(
        "/somewhere pid=1 host=no-such-host mode=recreate\n", encoding="utf-8"
    )
    r = _ensure(env)
    assert r.returncode == 0 and "leaving it to that run" in r.stderr
    old = time.time() - 15 * 60
    os.utime(lock, (old, old))
    r = _ensure(env, "--recreate")
    assert r.returncode == 2, r.stderr
