"""``scripts/git-tidy`` — the operator-run object-store tidy (gr548630).

Runs the real script (plus its ``scripts/lib/lock-holder.sh`` dependency,
copied into the rig) against a throwaway repo under tmp_path. The rig's
global git config is a temp file so the ``--local only`` contract is
checkable and no real user config can leak in or out.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "git-tidy"
_LIB = _REPO / "scripts" / "lib" / "lock-holder.sh"


class Rig:
    """A throwaway repo carrying git-tidy + lock-holder.sh at repo layout."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        scripts = self.repo / "scripts"
        (scripts / "lib").mkdir(parents=True)
        shutil.copy2(_SCRIPT, scripts / "git-tidy")
        shutil.copy2(_LIB, scripts / "lib" / "lock-holder.sh")
        gitcfg = tmp_path / "gitconfig"
        gitcfg.write_text(
            "[user]\n\tname = T\n\temail = t@example.invalid\n",
            encoding="utf-8",
        )
        self.global_cfg = gitcfg
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(gitcfg),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.g("init", "-q", "-b", "main")
        (self.repo / "f.txt").write_text("base\n", encoding="utf-8")
        self.g("add", "f.txt")
        self.g("commit", "-qm", "base")

    def g(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
        ).stdout.strip()

    def tidy(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "scripts/git-tidy", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            timeout=120,
        )

    def common(self) -> Path:
        return self.repo / ".git"

    def set_lock(self, lockdir: Path, pid: int) -> None:
        lockdir.mkdir(parents=True)
        # holder format is `<dir> pid=<n> host=<hostname -s>`: a foreign
        # host reads as unknowable and only ages out, so the stamp must
        # carry THIS host for the dead-pid reclaim path to fire.
        host = subprocess.run(
            ["hostname", "-s"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
        (lockdir / "holder").write_text(
            f"{self.repo} pid={pid} host={host}\n", encoding="utf-8"
        )


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    yield Rig(tmp_path)


def _dead_pid() -> int:
    """A pid guaranteed not to exist: spawn, reap, reuse."""
    p = subprocess.Popen(["sleep", "60"])
    pid = p.pid
    p.kill()
    p.wait()
    return pid


def test_dry_run_lists_stash_and_writes_nothing(rig: Rig) -> None:
    (rig.repo / "f.txt").write_text("dirty\n", encoding="utf-8")
    rig.g("stash")
    r = rig.tidy()
    assert r.returncode == 0, r.stderr
    assert "unreachable objects" in r.stdout
    assert "stash@{0}" in r.stdout
    assert "dry run — --apply would" in r.stdout
    # the plan names the never-now prune window
    assert "--prune=1.day.ago" in r.stdout
    assert "gc --prune=now" not in r.stdout
    # and nothing was written
    assert (
        subprocess.run(
            ["git", "config", "--local", "--get", "gc.pruneExpire"],
            cwd=rig.repo,
            capture_output=True,
            env=rig.env,
        ).returncode
        != 0
    )


def test_apply_writes_local_gc_config_idempotent(rig: Rig) -> None:
    for _ in range(2):
        r = rig.tidy("--apply")
        assert r.returncode == 0, r.stderr
    for key, want in (
        ("gc.reflogExpireUnreachable", "7.days"),
        ("gc.pruneExpire", "3.days"),
        ("gc.auto", "1000"),
    ):
        assert rig.g("config", "--local", "--get-all", key) == want
    # --local only: the (temp-file) global config gained nothing
    assert "gc." not in rig.global_cfg.read_text(encoding="utf-8")


def test_apply_refuses_while_ship_lock_held(rig: Rig) -> None:
    """A live holder pid on the repo-wide squash lock refuses the whole
    apply — config writes included."""
    rig.set_lock(rig.common() / "precis-ship.lock.d", os.getpid())
    r = rig.tidy("--apply")
    assert r.returncode == 1
    assert "ship lock held" in r.stderr
    assert (
        subprocess.run(
            ["git", "config", "--local", "--get", "gc.pruneExpire"],
            cwd=rig.repo,
            capture_output=True,
            env=rig.env,
        ).returncode
        != 0
    )


def test_apply_refuses_per_worktree_ship_lock(rig: Rig) -> None:
    """The lock a ship holds for its whole run is the per-worktree one —
    a live holder in any worktree's gitdir refuses too."""
    rig.set_lock(
        rig.common() / "worktrees" / "wt1" / "precis-ship-worktree.lock.d",
        os.getpid(),
    )
    r = rig.tidy("--apply")
    assert r.returncode == 1
    assert "ship lock held" in r.stderr


def test_apply_refuses_fresh_holderless_lock(rig: Rig) -> None:
    """A lock dir with no holder file is a ship mid-acquire: can't prove
    it's free, so refuse (it self-heals past the age cap)."""
    (rig.common() / "precis-ship.lock.d").mkdir()
    r = rig.tidy("--apply")
    assert r.returncode == 1
    assert "ship lock held" in r.stderr


def test_apply_warns_on_stale_lock_and_proceeds(rig: Rig) -> None:
    """A dead holder pid is a stale lock: warn (never delete), proceed."""
    rig.set_lock(rig.common() / "precis-ship.lock.d", _dead_pid())
    r = rig.tidy("--apply")
    assert r.returncode == 0, r.stderr
    assert "stale ship lock" in r.stderr
    assert (rig.common() / "precis-ship.lock.d").exists()
    assert rig.g("config", "--local", "--get", "gc.pruneExpire") == "3.days"
