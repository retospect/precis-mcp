"""Regression test for gr541144 — a repo hook killed at its 1-minute
timeout mid-``git status`` (and Devin lanes running repo hooks the same
way) can die between open() and write() on index.lock, leaving a 0-byte
file no process holds; every later git write in that worktree then fails
"Unable to create '.../index.lock': File exists." until a human removes
it. Exercises the real ``scripts/reap-index-locks`` (staged byte-for-byte
into a throwaway repo — never reimplemented here) plus its
``scripts/reap-worktrees`` wiring (the caller staged the same way, with a
stub ``scripts/inflight`` so the worktree pass finds nothing to do and the
lock sweep is the only visible action).

lsof is the script's open-lock proof on every platform, but the dev image
ships no lsof package — so the fixture drops a minimal /proc-backed
``lsof`` onto PATH whenever the real binary is absent, answering the
script's two call shapes (``-t -- <path>`` and ``-a -p <pid> -d cwd -Fn``)
with the same results real lsof gives on Linux. No DB / MCP fixtures —
pure git, ps and the shipped scripts.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="git worktree admin dirs + ps/proc liveness are POSIX-only",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REAP_LOCKS_SRC = REPO_ROOT / "scripts" / "reap-index-locks"
REAP_WORKTREES_SRC = REPO_ROOT / "scripts" / "reap-worktrees"

#: The script's STALE_SECONDS is a fixed 10-minute module constant, so a
#: test makes a lock "stale" by backdating its mtime past the threshold.
STALE_AGE_SECONDS = 1200

#: A minimal, truthful lsof stand-in for the lsof-free dev image: the same
#: answers real lsof gives for the two calls scripts/reap-index-locks
#: makes. `-t -- <path>` prints the pid of every process holding the file
#: open (matched by dev+inode — lsof's own notion of "same open file", so
#: hardlinks/symlink paths can't fool it); `-a -p <pid> -d cwd -Fn` prints
#: the process's cwd in -F form. Linux-only (it reads /proc), which is
#: exactly where the real lsof is missing.
FAKE_LSOF = """#!/usr/bin/env python3
import os
import sys


def _pids_holding(path):
    try:
        want = os.stat(path)
    except OSError:
        return
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fd_dir = os.path.join("/proc", pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                st = os.stat(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == (want.st_dev, want.st_ino):
                print(pid)
                break


def main(argv):
    if argv[:2] == ["-t", "--"]:
        _pids_holding(argv[2])
        return 0
    if "-p" in argv and "-d" in argv:  # lsof -a -p <pid> -d cwd -Fn
        pid = argv[argv.index("-p") + 1]
        try:
            cwd = os.readlink(os.path.join("/proc", pid, "cwd"))
        except OSError:
            return 1
        print("p" + pid)
        print("n" + cwd)
        return 0
    return 1


sys.exit(main(sys.argv[1:]))
"""


def _test_env(path: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PRECIS_NO_AUTOREAP", None)  # the escape hatch must be OFF here
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    if path is not None:
        env["PATH"] = path
    return env


def _run(
    cmd: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env if env is not None else _test_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"cmd failed: {cmd}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd)


def _make_stale(path: Path) -> Path:
    """A 0-byte lock backdated past the 10-minute threshold — the exact
    corpse a killed writer leaves."""
    path.touch()
    old = time.time() - STALE_AGE_SECONDS
    os.utime(path, (old, old))
    return path


def _fake_lsof_bindir(tmp_path: Path) -> Path | None:
    """A PATH prefix dir holding a working `lsof`. None when the platform
    has a real one — the script then verifies the same contract against
    the real binary."""
    if shutil.which("lsof"):
        return None
    bindir = tmp_path / "lsof-bin"
    bindir.mkdir()
    fake = bindir / "lsof"
    fake.write_text(FAKE_LSOF, encoding="utf-8")
    fake.chmod(0o755)
    return bindir


def _reap_env(tmp_path: Path) -> dict[str, str]:
    bindir = _fake_lsof_bindir(tmp_path)
    path = (
        os.environ["PATH"]
        if bindir is None
        else f"{bindir}{os.pathsep}{os.environ['PATH']}"
    )
    return _test_env(path=path)


def _run_reaper(
    repo: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    script = repo / "scripts" / "reap-index-locks"
    return _run([sys.executable, str(script), *args], cwd=repo, env=env)


def _admin_lock(repo: dict[str, Path], tree: str) -> Path:
    return repo["common"] / "worktrees" / tree / "index.lock"


def _make_fake_git_binary(tmp_path: Path) -> Path:
    """A runnable binary whose `comm` reads exactly "git" — what ps must
    see for the no-git-in-tree check. Same shape as
    tests/test_worktree_lock_reap.py's fake claude binary: on Linux `comm`
    is the exec'd file's own name (a real copy of `sleep` named "git"), on
    Darwin it's argv[0] (a SYMLINK — a copied Apple platform binary is
    SIGKILL'd at exec by launch constraints, gr514910)."""
    git_bin = tmp_path / "git"
    sleep = shutil.which("sleep") or "/bin/sleep"
    if sys.platform == "darwin":
        git_bin.symlink_to(sleep)
    else:
        shutil.copy(sleep, git_bin)
        git_bin.chmod(0o755)
    return git_bin


@pytest.fixture
def lock_repo(tmp_path: Path) -> dict[str, Path]:
    """A throwaway repo with the real reaper staged into primary/scripts/,
    plus two linked worktrees (checkouts at tmp_path/{A,B}, admin dirs at
    <common>/worktrees/{A,B} — the worktree location is irrelevant to the
    sweep, which reads only the admin dirs)."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")
    scripts = primary / "scripts"
    scripts.mkdir()
    shutil.copy2(REAP_LOCKS_SRC, scripts / "reap-index-locks")
    (scripts / "reap-index-locks").chmod(0o755)
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")
    a, b = tmp_path / "A", tmp_path / "B"
    _git(primary, "worktree", "add", "-q", "-b", "wt-A", str(a), "main")
    _git(primary, "worktree", "add", "-q", "-b", "wt-B", str(b), "main")
    common = Path(
        _git(
            primary, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout.strip()
    )
    return {
        "primary": primary,
        "a": a,
        "b": b,
        "common": common,
    }


def test_old_zero_byte_worktree_lock_is_reaped(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    lock = _make_stale(_admin_lock(lock_repo, "A"))
    env = _reap_env(tmp_path)
    result = _run_reaper(lock_repo["primary"], env)
    assert not lock.exists()
    assert re.fullmatch(
        r"reaped stale index\.lock: A \(age \d+m, 0 B\)", result.stdout.strip()
    ), result.stdout


def test_old_zero_byte_primary_lock_is_reaped(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    lock = _make_stale(lock_repo["common"] / "index.lock")
    env = _reap_env(tmp_path)
    result = _run_reaper(lock_repo["primary"], env)
    assert not lock.exists()
    assert re.fullmatch(
        r"reaped stale index\.lock: primary \(age \d+m, 0 B\)",
        result.stdout.strip(),
    ), result.stdout


def test_fresh_zero_byte_lock_is_kept(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    lock = _admin_lock(lock_repo, "A")
    lock.touch()  # 0 bytes but YOUNG — a writer may still be mid-open
    env = _reap_env(tmp_path)
    result = _run_reaper(lock_repo["primary"], env)
    assert lock.exists()
    assert "reaped" not in result.stdout


def test_non_empty_old_lock_is_kept(lock_repo: dict[str, Path], tmp_path: Path) -> None:
    lock = _admin_lock(lock_repo, "A")
    lock.write_text("12345\n", encoding="utf-8")
    old = time.time() - STALE_AGE_SECONDS
    os.utime(lock, (old, old))  # old but NON-EMPTY — a payload is a live lock's
    env = _reap_env(tmp_path)
    result = _run_reaper(lock_repo["primary"], env)
    assert lock.exists()
    assert "reaped" not in result.stdout


def test_lock_held_open_is_kept(lock_repo: dict[str, Path], tmp_path: Path) -> None:
    """Old + empty but a live process still holds the fd — the writer may
    be mid-fill, so the lsof check must outrank size+age."""
    lock = _make_stale(_admin_lock(lock_repo, "A"))
    env = _reap_env(tmp_path)
    fd = os.open(lock, os.O_RDONLY)
    try:
        result = _run_reaper(lock_repo["primary"], env)
        assert lock.exists()
        assert "reaped" not in result.stdout
    finally:
        os.close(fd)


def test_git_process_in_worktree_keeps_its_lock(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    """A git process standing in worktree A keeps ONLY A's lock — B's
    identically stale lock is still reaped, proving the cwd check is
    per-tree and not a global veto."""
    lock_a = _make_stale(_admin_lock(lock_repo, "A"))
    lock_b = _make_stale(_admin_lock(lock_repo, "B"))
    env = _reap_env(tmp_path)
    fake_git = _make_fake_git_binary(tmp_path)
    proc = subprocess.Popen([str(fake_git), "60"], cwd=str(lock_repo["a"]), env=env)
    try:
        result = _run_reaper(lock_repo["primary"], env)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert lock_a.exists(), "a live git in the worktree must protect its lock"
    assert not lock_b.exists(), "a git in A must not shield B's lock"
    assert re.search(r"reaped stale index\.lock: B \(age \d+m, 0 B\)", result.stdout), (
        result.stdout
    )


def test_head_lock_is_never_touched(lock_repo: dict[str, Path], tmp_path: Path) -> None:
    """The sweep names index.lock exactly — a same-age, same-shape
    HEAD.lock beside a reaped index.lock must survive the run."""
    index_lock = _make_stale(_admin_lock(lock_repo, "A"))
    head_lock = _make_stale(lock_repo["common"] / "worktrees" / "A" / "HEAD.lock")
    env = _reap_env(tmp_path)
    _run_reaper(lock_repo["primary"], env)
    assert not index_lock.exists()
    assert head_lock.exists()


def test_dry_run_reports_without_removing(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    lock = _make_stale(_admin_lock(lock_repo, "A"))
    env = _reap_env(tmp_path)
    result = _run_reaper(lock_repo["primary"], env, "--dry-run")
    assert lock.exists()
    assert re.search(
        r"would reap stale index\.lock: A \(age \d+m, 0 B\)", result.stdout
    ), result.stdout


def test_missing_lsof_reports_and_keeps(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    """Condition 3 fails closed: with no lsof the sweep cannot prove a lock
    is unheld, so it reports the stale candidate and keeps it — the "just
    report" half of the spec, pinned."""
    lock = _make_stale(_admin_lock(lock_repo, "A"))
    bindir = tmp_path / "no-lsof-bin"
    bindir.mkdir()
    for tool in ("git", "ps"):
        real = shutil.which(tool)
        assert real, f"{tool} must exist for this test"
        (bindir / tool).symlink_to(real)
    env = _test_env(path=str(bindir))
    result = _run_reaper(lock_repo["primary"], env)
    assert lock.exists()
    assert "lsof" in result.stdout, result.stdout
    assert "reaped" not in result.stdout


def test_reap_worktrees_for_hook_invokes_the_sweep(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    """The wiring half of gr541144: a stale lock in the primary .git is
    reaped by `scripts/reap-worktrees --for-hook` itself, and the reap
    line is the only output (worktree pass idles on the stub inflight)."""
    primary = lock_repo["primary"]
    scripts = primary / "scripts"
    shutil.copy2(REAP_WORKTREES_SRC, scripts / "reap-worktrees")
    (scripts / "reap-worktrees").chmod(0o755)
    inflight = scripts / "inflight"
    inflight.write_text(
        '#!/bin/sh\n[ "$1" = "--json" ] && echo \'{"worktrees": []}\'\n',
        encoding="utf-8",
    )
    inflight.chmod(0o755)
    lock = _make_stale(lock_repo["common"] / "index.lock")
    env = _reap_env(tmp_path)
    result = _run(
        ["bash", "scripts/reap-worktrees", "--for-hook"], cwd=primary, env=env
    )
    assert not lock.exists()
    assert "reaped stale index.lock: primary" in result.stdout
