"""Regression test for gr172130 — the worktree auto-reaper deleting a live
session's worktree because nothing ever acquired the `git worktree lock` its
liveness check depends on, PLUS the follow-up bug in the first fix attempt:
``scripts/hooks/session-start-lock.sh`` identified the durable session
process by a `*claude*` SUBSTRING match on a process's full command line.
Every worktree's own path is ``.claude/worktrees/<name>/...``, so a
short-lived intermediate wrapper invoked with that path in its own argv (a
shell-snapshot loader, etc.) also matches — and if the ancestry walk hits
that transient wrapper before the real session process, it locks the
worktree to a pid that's already gone by the time anything checks it,
producing an immediate ``dead-lock#<pid>`` that a sibling session's reaper
then deletes out from under the still-live session. Exactly the bug the
hook exists to fix, reintroduced.

Exercises the REAL ``scripts/inflight``, ``scripts/reap-worktrees``, and
``scripts/hooks/session-start-lock.sh`` (copied byte-for-byte into a
throwaway repo — never reimplemented here) against a synthetic multi-worktree
repo that mirrors the ``/land`` squash-merge-then-reset shape, PLUS a
synthetic process tree that mirrors the real ancestry shape (durable
"session" process -> transient path-embedding wrapper -> hook script).
Confirms:

- ``session-start-lock.sh``, run under a controlled process tree with a
  transient wrapper whose OWN command line embeds the worktree's path (the
  exact shape that broke the substring-based walk), locks the worktree to
  the DURABLE process's pid — identified by exact executable-basename match,
  not a command-line substring — never the wrapper's,
- that lock protects a merged+clean worktree from ``scripts/reap-worktrees``
  while the locking pid is alive (the original fix), and
- once that pid is dead, the SAME worktree is still reaped — the fix must
  not neuter legitimate orphan cleanup (``dead-lock#<pid>`` stays
  ``safe_remove``, per ``scripts/inflight``'s ``session_field``).

No DB / MCP fixtures involved — pure git + the three shipped scripts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="git worktree lock + the shipped bash scripts are POSIX-only",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INFLIGHT_SRC = REPO_ROOT / "scripts" / "inflight"
REAP_SRC = REPO_ROOT / "scripts" / "reap-worktrees"
LOCK_HOOK_SRC = REPO_ROOT / "scripts" / "hooks" / "session-start-lock.sh"


def _test_env() -> dict[str, str]:
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
    return env


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd, cwd=str(cwd), env=_test_env(), capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"cmd failed: {cmd}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _bucket_for(json_blob: str, path: Path) -> dict:
    data = json.loads(json_blob)
    target = os.path.realpath(str(path))
    for wt in data["worktrees"]:
        if os.path.realpath(wt["path"]) == target:
            return wt
    raise AssertionError(f"{path} not found in inflight --json output: {json_blob}")


def _lock_reason_for(primary: Path, worktree: Path) -> str | None:
    """Parse `git worktree list --porcelain` for `worktree`'s `locked <reason>`
    line, the same shape scripts/inflight's session_field() parses."""
    out = _git(primary, "worktree", "list", "--porcelain").stdout
    target = os.path.realpath(str(worktree))
    blocks = out.split("\n\n")
    for block in blocks:
        lines = block.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        if os.path.realpath(lines[0][len("worktree ") :]) != target:
            continue
        for line in lines[1:]:
            if line.startswith("locked"):
                return line[len("locked") :].strip()
        return None
    return None


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _make_fake_claude_binary(tmp_path: Path) -> Path:
    """A real `bash` binary, copied to a file literally named `claude`, run
    DIRECTLY (never through a `#!` shebang indirection). This is what makes
    `comm` (the kernel's own process-name field — `ps -o comm=` /
    `/proc/<pid>/comm` on Linux) read exactly "claude", matching what the
    real Claude Code process presents as.

    Two things that do NOT work and were tried first:
      - `exec -a claude <cmd>` only overrides argv[0] (visible in `ps args`)
        -- on Linux, `comm` still reports the real executable's own
        basename, so this technique doesn't fake a `comm == claude` process
        there (it did on this macOS host, which isn't the authoritative
        test environment per `scripts/test`'s container).
      - naming a `#!/usr/bin/env bash` SCRIPT "claude" and exec'ing it
        directly: the shebang indirection makes the kernel report `comm` as
        the interpreter's name ("bash"), not the script's own file name.
    Only a real binary, executed by its own "claude" path with no shebang
    hop, gets `comm == claude` on both platforms.
    """
    claude_bin = tmp_path / "claude"
    shutil.copy(shutil.which("bash") or "/bin/bash", claude_bin)
    claude_bin.chmod(0o755)
    return claude_bin


@pytest.fixture
def repo_trio(tmp_path: Path) -> dict[str, Path]:
    """A throwaway git repo with three worktrees: primary, A, and B — B
    nested under ``primary/.claude/worktrees/B``, mirroring this repo's real
    layout (``session-start-lock.sh`` no-ops on anything not under
    ``.claude/worktrees/``).

    B is set up to mirror ``scripts/ship``'s squash-merge-then-reset: a real
    branch commit gets squash-merged into main, then B's own branch is
    hard-reset onto that shipped main -- merged (tip is an ancestor of main)
    AND clean, exactly the state a just-shipped worktree is left in (the
    trigger for the bug).
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")

    scripts_dir = primary / "scripts"
    hooks_dir = scripts_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    shutil.copy2(REAP_SRC, scripts_dir / "reap-worktrees")
    shutil.copy2(LOCK_HOOK_SRC, hooks_dir / "session-start-lock.sh")
    (scripts_dir / "inflight").chmod(0o755)
    (scripts_dir / "reap-worktrees").chmod(0o755)
    (hooks_dir / "session-start-lock.sh").chmod(0o755)
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")

    a = primary / ".claude" / "worktrees" / "A"
    b = primary / ".claude" / "worktrees" / "B"
    _git(primary, "worktree", "add", "-q", "-b", "worktree-A", str(a), "main")
    _git(primary, "worktree", "add", "-q", "-b", "worktree-B", str(b), "main")

    # B does real work...
    (b / "feature.txt").write_text("feature work\n", encoding="utf-8")
    _git(b, "add", "-A")
    _git(b, "commit", "-q", "-m", "feature work")

    # ...which /land squash-merges into main...
    _git(primary, "merge", "-q", "--squash", "worktree-B")
    _git(primary, "commit", "-q", "-m", "feature work (squashed)")

    # ...then resets B's branch onto the shipped main (scripts/ship's step 6):
    # merged + clean, the exact trigger state for the reaper. Worktrees of one
    # repo share the ref namespace, so "main" is directly visible from B.
    _git(b, "reset", "-q", "--hard", "main")

    return {"primary": primary, "a": a, "b": b}


def test_session_start_lock_locks_durable_pid_not_transient_wrapper(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    """Directly exercises scripts/hooks/session-start-lock.sh (not a
    hand-crafted `git worktree lock`) under a synthetic process tree shaped
    like the real ancestry: a durable process presenting as `comm == claude`
    (the actual session), with a SEPARATE, SHORT-LIVED wrapper process in
    between whose own command line embeds the worktree's `.claude/worktrees`
    path — the exact shape that broke the old `*claude*` substring walk,
    since the wrapper's argv contains that substring too.

    A regression back to substring matching makes this fail: it would lock
    B to the wrapper's pid, which is already dead by the time we assert.
    """
    primary, b = repo_trio["primary"], repo_trio["b"]
    hook = b / "scripts" / "hooks" / "session-start-lock.sh"
    claude_bin = _make_fake_claude_binary(tmp_path)

    # Scratch dir OUTSIDE the worktree — anything written inside `b` itself
    # would show up as an untracked file and make it "dirty", which would
    # break the merged+CLEAN invariant the reap/inflight bucketing (and the
    # sibling test) depends on. `cwd=b` below is what makes the hook resolve
    # `HERE` to `b` regardless of where these control files physically live.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    claude_pid_file = scratch / "claude.pid"
    wrapper_pid_file = scratch / "wrapper.pid"

    # The durable "session" process: a real `bash` binary run directly as a
    # file named "claude" (see `_make_fake_claude_binary`), so its `comm` is
    # exactly "claude", matching the real Claude Code process. It spawns a
    # wrapper as a genuine CHILD (a trailing `;:` after the hook call
    # prevents bash from tail-call-exec'ing the hook in the wrapper's own
    # place, so the wrapper is a real, separate, short-lived pid — not
    # collapsed into the hook's pid). The wrapper's own command line embeds
    # the worktree's `.claude/worktrees/B` path (via `cd`/the hook's own
    # path argument), exactly like a real shell-snapshot wrapper whose argv
    # happens to mention the worktree it's running in.
    inner = scratch / "inner_session.sh"
    inner.write_text(
        f"""echo $$ > "{claude_pid_file}"
bash -c 'echo $$ > "{wrapper_pid_file}"; cd "{b}" && bash "{hook}"; :'
sleep 60
""",
        encoding="utf-8",
    )
    inner.chmod(0o755)

    proc = subprocess.Popen(
        [str(claude_bin), str(inner)],
        cwd=str(b),
        env=_test_env(),
    )
    try:
        assert _wait_for(lambda: claude_pid_file.exists()), "durable pid never recorded"
        assert _wait_for(lambda: wrapper_pid_file.exists()), (
            "wrapper pid never recorded"
        )
        durable_pid = int(claude_pid_file.read_text(encoding="utf-8").strip())
        wrapper_pid = int(wrapper_pid_file.read_text(encoding="utf-8").strip())

        # `claude_bin` is exec'd directly by Popen (no intervening shell),
        # so the Popen'd pid IS the "claude"-named process.
        assert durable_pid == proc.pid, (durable_pid, proc.pid)
        assert wrapper_pid != durable_pid

        assert _wait_for(lambda: _lock_reason_for(primary, b) is not None), (
            "session-start-lock.sh never locked the worktree"
        )
        reason = _lock_reason_for(primary, b)
        assert reason is not None
        assert reason == f"pid {durable_pid}", (
            f"expected lock on the durable session pid {durable_pid}, "
            f"got {reason!r} (wrapper pid was {wrapper_pid}) — the ancestry "
            "walk locked onto a transient wrapper instead of the durable "
            "session process"
        )

        # The wrapper is genuinely transient: it's gone shortly after the
        # hook returns, unlike the durable pid the lock actually recorded.
        assert _wait_for(lambda: not _pid_alive(wrapper_pid), timeout=5.0)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    _git(primary, "worktree", "unlock", str(b))


def test_live_locked_session_survives_reap_then_dead_lock_is_still_reaped(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    primary, a, b = repo_trio["primary"], repo_trio["a"], repo_trio["b"]
    inflight = a / "scripts" / "inflight"
    reap = a / "scripts" / "reap-worktrees"
    hook = b / "scripts" / "hooks" / "session-start-lock.sh"
    claude_bin = _make_fake_claude_binary(tmp_path)

    # Scratch dir OUTSIDE the worktree — see the sibling test for why
    # (writing control files inside `b` would make it "dirty" and break the
    # merged+clean bucketing this test asserts on).
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    # A background process standing in for a live session sitting in B,
    # presenting as `comm == claude` so it's the pid session-start-lock.sh's
    # (real, unmodified) ancestry walk actually resolves to — running the
    # real hook, not hand-crafting `git worktree lock` as the prior version
    # of this test did.
    inner = scratch / "inner_session.sh"
    inner.write_text(
        f"""bash "{hook}"
sleep 120
""",
        encoding="utf-8",
    )
    inner.chmod(0o755)

    proc = subprocess.Popen(
        [str(claude_bin), str(inner)],
        cwd=str(b),
        env=_test_env(),
    )
    try:
        assert _wait_for(lambda: _lock_reason_for(primary, b) is not None), (
            "session-start-lock.sh never locked the worktree"
        )
        reason = _lock_reason_for(primary, b)
        assert reason == f"pid {proc.pid}", reason

        json_out = _run(["bash", str(inflight), "--json"], a).stdout
        bucket = _bucket_for(json_out, b)
        assert bucket["verdict"] == "merged", bucket
        assert bucket["session"] == f"live#{proc.pid}", bucket
        assert bucket["bucket"] == "live_session", bucket

        # Real reap-worktrees, run from a DIFFERENT worktree (A).
        result = subprocess.run(
            ["bash", str(reap)],
            cwd=str(a),
            env=_test_env(),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        # B must still exist — the live session protects it.
        assert b.exists()
        wt_list = _git(primary, "worktree", "list", "--porcelain").stdout
        assert str(b) in wt_list
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    for _ in range(50):
        if not _pid_alive(proc.pid):
            break
        time.sleep(0.1)
    assert not _pid_alive(proc.pid), "background pid did not die in time"

    # Negative case: same merged+clean B, same lock reason, but the locking
    # pid is now dead -> inflight must flip it to dead-lock (still reapable),
    # proving the fix doesn't neuter legitimate orphan reaping.
    json_out = _run(["bash", str(inflight), "--json"], a).stdout
    bucket = _bucket_for(json_out, b)
    assert bucket["session"] == f"dead-lock#{proc.pid}", bucket
    assert bucket["bucket"] == "safe_remove", bucket

    result = subprocess.run(
        ["bash", str(reap)],
        cwd=str(a),
        env=_test_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    assert not b.exists()
    wt_list = _git(primary, "worktree", "list", "--porcelain").stdout
    assert str(b) not in wt_list
