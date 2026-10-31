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

The sweep is REPORT-ONLY by default: a stale candidate prints
"would reap <path> (age N m, 0 B)" and the file stays; only an explicit
``--apply`` deletes, and no hook passes it. Liveness is fail-safe — a git
process counts as dead only on positive evidence (pid gone, or zombie
state in /proc/<pid>/stat / `ps -o stat=`); an alive git with an
unreadable cwd keeps every lock. The two process-table cases are
deterministic: a real long-lived ``git cat-file --batch`` stands in one
worktree with its stdin held open, and a real zombie is made by fork+exec
of ``git`` the parent never waitpids (a subprocess.Popen child would be
auto-reaped by subprocess._cleanup() the moment the test spawned the
reaper).

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


def _ps_snapshot(pid: int) -> tuple[str, str]:
    """(comm, stat) for one pid per `ps`; empty strings when ps can't see
    it. Linux reports a zombie's comm as its name, macOS as "<defunct>" —
    callers assert on stat, never on zombie comm."""
    result = subprocess.run(
        ["ps", "-o", "comm=,stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    parts = result.stdout.split()
    comm = parts[0] if parts else ""
    stat = parts[1] if len(parts) > 1 else ""
    return comm, stat


def _wait_proc(pid: int, pred, desc: str, timeout: float = 15.0) -> None:
    """Poll ps until pred(comm, stat) — a deterministic hand-off point, so
    the reaper meets the exact process state the test means to show."""
    comm = stat = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        comm, stat = _ps_snapshot(pid)
        if pred(comm, stat):
            return
        time.sleep(0.05)
    pytest.fail(f"pid {pid} never reached {desc}: last ps comm={comm!r} stat={stat!r}")


def _spawn_live_git(cwd: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    """A real, long-lived git standing in worktree `cwd`: cat-file --batch
    blocks reading stdin, so the process lives — comm exactly "git", no
    fake binary, nothing for multicall/runner quirks to distort — until
    the caller closes the pipe. The earlier fake-`git` (a copied `sleep`
    exec'd under executable=) died on some CI runners and left the lock
    exposed — the c9ab33945 revert."""
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_proc(
        proc.pid,
        lambda comm, stat: os.path.basename(comm) == "git" and not stat.startswith("Z"),
        "a live git (cat-file --batch)",
    )
    return proc


def _spawn_zombie_git(cwd: Path) -> int:
    """A REAL git that execs and exits inside worktree `cwd` without being
    reaped: a 'git' sitting in the process table as a zombie — the exact
    veto case gr541144 hit (a dead git has no cwd; only positive liveness
    evidence may ignore it). os.fork is deliberate: a subprocess.Popen
    child is reaped by subprocess._cleanup() as soon as the test spawns
    the reaper, which would turn 'zombie' into 'pid gone' — also a valid
    dead case, but not the one this test is for."""
    pid = os.fork()
    if pid == 0:  # child — exec at once, never re-enter pytest
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.chdir(str(cwd))
            os.execvp("git", ["git", "--version"])
        finally:
            os._exit(127)
    _wait_proc(pid, lambda comm, stat: stat.startswith("Z"), "a zombie git")
    return pid


def _would_reap(lock: Path) -> re.Pattern[str]:
    return re.compile(rf"would reap {re.escape(str(lock))} \(age \d+m, 0 B\)")


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


def test_stale_worktree_lock_reports_then_applies(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    """The default lane is report-only: the candidate line names the lock
    and the file stays. --apply is what removes."""
    lock = _make_stale(_admin_lock(lock_repo, "A"))
    env = _reap_env(tmp_path)
    result = _run_reaper(lock_repo["primary"], env)
    assert lock.exists()
    assert _would_reap(lock).search(result.stdout), result.stdout
    result = _run_reaper(lock_repo["primary"], env, "--apply")
    assert not lock.exists()
    assert re.search(
        rf"reaped stale index\.lock: {re.escape(str(lock))} \(age \d+m, 0 B\)",
        result.stdout,
    ), result.stdout


def test_stale_primary_lock_reports_then_applies(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    lock = _make_stale(lock_repo["common"] / "index.lock")
    env = _reap_env(tmp_path)
    result = _run_reaper(lock_repo["primary"], env)
    assert lock.exists()
    assert _would_reap(lock).search(result.stdout), result.stdout
    result = _run_reaper(lock_repo["primary"], env, "--apply")
    assert not lock.exists()
    assert re.search(
        rf"reaped stale index\.lock: {re.escape(str(lock))} \(age \d+m, 0 B\)",
        result.stdout,
    ), result.stdout


def test_fresh_zero_byte_lock_is_kept(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    lock = _admin_lock(lock_repo, "A")
    lock.touch()  # 0 bytes but YOUNG — a writer may still be mid-open
    env = _reap_env(tmp_path)
    result = _run_reaper(lock_repo["primary"], env)
    assert lock.exists()
    assert "would reap" not in result.stdout


def test_non_empty_old_lock_is_kept(lock_repo: dict[str, Path], tmp_path: Path) -> None:
    lock = _admin_lock(lock_repo, "A")
    lock.write_text("12345\n", encoding="utf-8")
    old = time.time() - STALE_AGE_SECONDS
    os.utime(lock, (old, old))  # old but NON-EMPTY — a payload is a live lock's
    env = _reap_env(tmp_path)
    result = _run_reaper(lock_repo["primary"], env)
    assert lock.exists()
    assert "would reap" not in result.stdout


def test_lock_held_open_is_kept(lock_repo: dict[str, Path], tmp_path: Path) -> None:
    """Old + empty but a live process still holds the fd — the writer may
    be mid-fill, so the lsof check must outrank size+age."""
    lock = _make_stale(_admin_lock(lock_repo, "A"))
    env = _reap_env(tmp_path)
    fd = os.open(lock, os.O_RDONLY)
    try:
        result = _run_reaper(lock_repo["primary"], env)
        assert lock.exists()
        assert "would reap" not in result.stdout
    finally:
        os.close(fd)


def test_git_process_in_worktree_keeps_its_lock(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    """A live git standing in worktree A keeps ONLY A's lock — B's
    identically stale lock is still reported, and removed under --apply,
    proving the cwd check is per-tree and not a global veto."""
    lock_a = _make_stale(_admin_lock(lock_repo, "A"))
    lock_b = _make_stale(_admin_lock(lock_repo, "B"))
    env = _reap_env(tmp_path)
    proc = _spawn_live_git(lock_repo["a"], env)
    try:
        result = _run_reaper(lock_repo["primary"], env)
        assert lock_a.exists()
        assert lock_b.exists()  # report-only: reported, not removed
        assert not _would_reap(lock_a).search(result.stdout), result.stdout
        assert "a git process is running in the worktree" in result.stdout, (
            result.stdout
        )
        assert _would_reap(lock_b).search(result.stdout), result.stdout
        result = _run_reaper(lock_repo["primary"], env, "--apply")
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.close()  # cat-file exits on stdin EOF
            except BrokenPipeError:
                pass  # already dead — wait() below still collects it
        proc.wait(timeout=10)
    assert lock_a.exists(), "a live git in the worktree must protect its lock"
    assert not lock_b.exists(), "a git in A must not shield B's lock"


def test_zombie_git_in_worktree_does_not_veto(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    """gr541144's CI red: a git that died in A and sits unreaped has no
    cwd — positive liveness evidence (state Z) must clear it or it reads
    as a live git with an unknowable cwd and vetoes every lock."""
    lock_a = _make_stale(_admin_lock(lock_repo, "A"))
    lock_b = _make_stale(_admin_lock(lock_repo, "B"))
    env = _reap_env(tmp_path)
    pid = _spawn_zombie_git(lock_repo["a"])
    try:
        result = _run_reaper(lock_repo["primary"], env)
    finally:
        os.waitpid(pid, 0)
    assert lock_a.exists() and lock_b.exists()  # report-only
    assert _would_reap(lock_a).search(result.stdout), result.stdout
    assert _would_reap(lock_b).search(result.stdout), result.stdout
    assert "cwd is unreadable" not in result.stdout, result.stdout


def test_head_lock_is_never_touched(lock_repo: dict[str, Path], tmp_path: Path) -> None:
    """The sweep names index.lock exactly — a same-age, same-shape
    HEAD.lock beside a reported index.lock is neither printed nor
    touched."""
    index_lock = _make_stale(_admin_lock(lock_repo, "A"))
    head_lock = _make_stale(lock_repo["common"] / "worktrees" / "A" / "HEAD.lock")
    env = _reap_env(tmp_path)
    result = _run_reaper(lock_repo["primary"], env)
    assert index_lock.exists()  # reported only
    assert _would_reap(index_lock).search(result.stdout), result.stdout
    assert head_lock.exists()
    assert str(head_lock) not in result.stdout


def test_dry_run_reports_without_removing(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    """--dry-run stays accepted and names the default lane."""
    lock = _make_stale(_admin_lock(lock_repo, "A"))
    env = _reap_env(tmp_path)
    result = _run_reaper(lock_repo["primary"], env, "--dry-run")
    assert lock.exists()
    assert _would_reap(lock).search(result.stdout), result.stdout


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
    assert "would reap" not in result.stdout


def test_reap_worktrees_for_hook_invokes_the_sweep(
    lock_repo: dict[str, Path], tmp_path: Path
) -> None:
    """The wiring half of gr541144: `scripts/reap-worktrees --for-hook`
    invokes the sweep — which under report-only prints the "would reap"
    line and leaves the file; the hook path never applies."""
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
    assert lock.exists(), "the SessionStart path must never delete"
    assert _would_reap(lock).search(result.stdout), result.stdout
