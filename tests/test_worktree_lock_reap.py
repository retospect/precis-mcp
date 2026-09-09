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

Also covers gr256469's follow-up bugs, both about a NESTED `claude -p` (one
inherits the caller's cwd + hook wiring, so its own SessionStart/SessionEnd
fire this repo's hooks too) stepping on the REAL session's lock:

- ``session-start-lock.sh`` run from underneath an already-locked live outer
  session must leave that lock alone instead of re-locking to itself
  (``test_nested_session_start_does_not_steal_outer_lock``).
- ``session-end-reap.sh`` run as a nested session must not drop a live
  outer session's lock (nor, therefore, reap the tree out from under it)
  (``test_nested_session_end_does_not_unlock_or_reap_outer_session``) — but
  a lock naming an actually-dead pid must still be released and the tree
  still reaped normally (``test_session_end_reap_still_reaps_dead_lock``),
  proving the fix doesn't neuter legitimate cleanup.

These exercise the real ``scripts/lib/session-lock.sh`` + real
``scripts/hooks/session-end-reap.sh`` (also staged byte-for-byte, alongside
the three already listed above) under the same synthetic-process-tree
technique.

Also covers gr260192's hardening of the two open proposals in
``docs/backlog/reaper-removed-live-session-worktree.md`` (1: grace-period
re-verify right before removal; 2: a fresh ``.claude/purpose`` tripwire),
against the real ``scripts/reap-worktrees`` staged into a SEPARATE small
fixture (``guard_repo``, below) rather than ``repo_trio`` — it needs a
``.gitignore`` for ``.claude/*`` (mirroring this repo's own) so writing
``.claude/purpose`` into the throwaway worktree doesn't itself make it
"dirty" via an untracked file and mask what's actually being tested.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="git worktree lock + the shipped bash scripts are POSIX-only",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INFLIGHT_SRC = REPO_ROOT / "scripts" / "inflight"
REAP_SRC = REPO_ROOT / "scripts" / "reap-worktrees"
REAP_DB_SRC = REPO_ROOT / "scripts" / "reap-test-dbs"
COMPOSE_PROJECT_LIB_SRC = REPO_ROOT / "scripts" / "lib" / "compose-project.sh"
LOCK_HOOK_SRC = REPO_ROOT / "scripts" / "hooks" / "session-start-lock.sh"
SESSION_END_HOOK_SRC = REPO_ROOT / "scripts" / "hooks" / "session-end-reap.sh"
SESSION_LOCK_LIB_SRC = REPO_ROOT / "scripts" / "lib" / "session-lock.sh"


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
    lib_dir = scripts_dir / "lib"
    hooks_dir.mkdir(parents=True)
    lib_dir.mkdir(parents=True)
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    shutil.copy2(REAP_SRC, scripts_dir / "reap-worktrees")
    shutil.copy2(LOCK_HOOK_SRC, hooks_dir / "session-start-lock.sh")
    shutil.copy2(SESSION_END_HOOK_SRC, hooks_dir / "session-end-reap.sh")
    # scripts/lib/session-lock.sh: sourced (guarded, `[ -f ... ] && source
    # ...`) by both hooks above. Staging it here is what makes the
    # gr256469 nested-session guards in those hooks actually engage in this
    # throwaway repo instead of silently no-op'ing (the guarded source
    # degrading gracefully for an older checkout without the file) — the new
    # nested-session tests below would go green for the wrong reason
    # (guard never firing at all) if this were left unstaged.
    shutil.copy2(SESSION_LOCK_LIB_SRC, lib_dir / "session-lock.sh")
    (scripts_dir / "inflight").chmod(0o755)
    (scripts_dir / "reap-worktrees").chmod(0o755)
    (hooks_dir / "session-start-lock.sh").chmod(0o755)
    (hooks_dir / "session-end-reap.sh").chmod(0o755)
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


def test_nested_session_start_does_not_steal_outer_lock(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    """gr256469: a nested `claude -p`'s own SessionStart fires this hook
    too, from underneath the real interactive session that already holds
    the lock. The ancestry walk from THAT invocation resolves to the
    nested process itself (the nearest comm==claude ancestor) — so without
    the lock_pid_for/is_ancestor guard, the unconditional unlock-then-lock
    below steals the lock from the live outer session and hands it to the
    nested one-shot, which exits moments later and leaves a dead-lock a
    sibling's reaper then deletes the still-live tree out from under. This
    is the exact mechanism behind gr256469's 1497-file loss.
    """
    primary, b = repo_trio["primary"], repo_trio["b"]
    hook = b / "scripts" / "hooks" / "session-start-lock.sh"
    claude_bin = _make_fake_claude_binary(tmp_path)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outer_pid_file = scratch / "outer.pid"
    nested_pid_file = scratch / "nested.pid"

    # Nested: also comm==claude (a real nested `claude -p` presents that
    # way too), records its own pid, then fires the same hook again — the
    # shape that broke the pre-fix unconditional unlock/lock.
    nested_inner = scratch / "nested_inner.sh"
    nested_inner.write_text(
        f"""echo $$ > "{nested_pid_file}"
bash "{hook}"
""",
        encoding="utf-8",
    )
    nested_inner.chmod(0o755)

    # Outer: a durable comm==claude process that locks B to ITSELF first
    # (the real starting state — an interactive session already holds the
    # lock) and only THEN spawns the nested comm==claude child as a genuine
    # OS child of itself, mirroring a nested `claude -p` inheriting the
    # outer session's cwd/hook wiring and running underneath it in the
    # process tree.
    outer_inner = scratch / "outer_inner.sh"
    outer_inner.write_text(
        f"""echo $$ > "{outer_pid_file}"
bash "{hook}"
"{claude_bin}" "{nested_inner}"
sleep 60
""",
        encoding="utf-8",
    )
    outer_inner.chmod(0o755)

    proc = subprocess.Popen(
        [str(claude_bin), str(outer_inner)],
        cwd=str(b),
        env=_test_env(),
    )
    try:
        assert _wait_for(lambda: outer_pid_file.exists()), "outer pid never recorded"
        outer_pid = int(outer_pid_file.read_text(encoding="utf-8").strip())
        assert outer_pid == proc.pid

        assert _wait_for(lambda: _lock_reason_for(primary, b) == f"pid {outer_pid}"), (
            "outer session-start-lock.sh never locked B to itself"
        )

        assert _wait_for(lambda: nested_pid_file.exists()), "nested pid never recorded"
        nested_pid = int(nested_pid_file.read_text(encoding="utf-8").strip())
        assert nested_pid != outer_pid

        # The nested claude_bin process is run in the foreground of
        # outer_inner.sh (no trailing `&`), so it's gone once outer_inner
        # moves on to `sleep 60` — give it a moment to actually finish.
        assert _wait_for(lambda: not _pid_alive(nested_pid), timeout=10.0), (
            "nested claude process never exited"
        )

        reason = _lock_reason_for(primary, b)
        assert reason == f"pid {outer_pid}", (
            f"expected the lock to still name the OUTER session pid "
            f"{outer_pid}, got {reason!r} (nested pid was {nested_pid}) — "
            "the nested SessionStart invocation stole the lock"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    _git(primary, "worktree", "unlock", str(b))


def test_nested_session_end_does_not_unlock_or_reap_outer_session(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    """gr256469: mirrors the SessionStart guard above for SessionEnd. A
    nested `claude -p`'s own SessionEnd also fires session-end-reap.sh —
    while the REAL interactive session that holds the lock is still alive
    and sitting in the tree. The pre-fix hook unconditionally unlocked
    BEFORE its bucket check, so by the time inflight ran, the tree looked
    unlocked+merged+clean and got removed out from under the still-live
    outer session. B is already merged+clean (see repo_trio) — exactly the
    trigger shape scripts/ship leaves a just-shipped worktree in.
    """
    primary, b = repo_trio["primary"], repo_trio["b"]
    hook = b / "scripts" / "hooks" / "session-end-reap.sh"
    claude_bin = _make_fake_claude_binary(tmp_path)

    # Outer: any live process standing in for the real interactive session.
    # Doesn't need comm==claude itself — the SessionEnd guard only compares
    # the LOCK's pid against the pid the nested hook resolves to, it never
    # walks looking for an ancestor the way the SessionStart guard does.
    outer = subprocess.Popen(["sleep", "120"], env=_test_env())
    try:
        outer_pid = outer.pid
        _git(primary, "worktree", "lock", str(b), "--reason", f"pid {outer_pid}")
        assert _lock_reason_for(primary, b) == f"pid {outer_pid}"

        scratch = tmp_path / "scratch"
        scratch.mkdir()
        nested_pid_file = scratch / "nested.pid"
        payload_file = scratch / "payload.json"
        payload_file.write_text(
            json.dumps({"reason": "logout", "cwd": str(b)}), encoding="utf-8"
        )

        inner = scratch / "inner.sh"
        inner.write_text(
            f"""echo $$ > "{nested_pid_file}"
cat "{payload_file}" | bash "{hook}"
""",
            encoding="utf-8",
        )
        inner.chmod(0o755)

        nested = subprocess.Popen(
            [str(claude_bin), str(inner)],
            cwd=str(b),
            env=_test_env(),
        )
        try:
            nested.wait(timeout=15)
        except subprocess.TimeoutExpired:
            nested.kill()
            nested.wait(timeout=10)
            raise

        assert nested_pid_file.exists(), "nested pid never recorded"

        # The outer session's lock must be untouched...
        reason = _lock_reason_for(primary, b)
        assert reason == f"pid {outer_pid}", (
            f"expected the lock to still name the OUTER session pid "
            f"{outer_pid}, got {reason!r} — the nested SessionEnd dropped it"
        )
        # ...and B must still be there: the nested SessionEnd must have
        # exited before ever reaching the unlock / reap-bucket check.
        assert b.exists()
        wt_list = _git(primary, "worktree", "list", "--porcelain").stdout
        assert str(b) in wt_list
    finally:
        outer.terminate()
        try:
            outer.wait(timeout=10)
        except subprocess.TimeoutExpired:
            outer.kill()
            outer.wait(timeout=10)
    _git(primary, "worktree", "unlock", str(b))


def test_session_end_reap_still_reaps_dead_lock(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    """Guards against over-correcting gr256469's fix into a hook that never
    reaps anything: a lock naming an ACTUALLY DEAD pid (the ordinary
    stale-lock case scripts/inflight already flips to dead-lock/safe_remove)
    must still be released, and B still reaped, by a normal (non-nested)
    session-end-reap.sh run.
    """
    primary, b = repo_trio["primary"], repo_trio["b"]
    hook = primary / "scripts" / "hooks" / "session-end-reap.sh"

    dead = subprocess.Popen(["true"], env=_test_env())
    dead.wait(timeout=10)
    dead_pid = dead.pid
    assert _wait_for(lambda: not _pid_alive(dead_pid), timeout=5.0)

    _git(primary, "worktree", "lock", str(b), "--reason", f"pid {dead_pid}")
    assert _lock_reason_for(primary, b) == f"pid {dead_pid}"

    payload = json.dumps({"reason": "logout", "cwd": str(b)})
    result = subprocess.run(
        ["bash", str(hook)],
        cwd=str(primary),
        input=payload,
        env=_test_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    assert not b.exists()
    wt_list = _git(primary, "worktree", "list", "--porcelain").stdout
    assert str(b) not in wt_list


def test_session_end_reap_holds_when_it_cannot_identify_itself(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    """gr256469, fail-safe direction: the lock names a LIVE pid and
    ``find_session_pid`` cannot resolve this invocation to any session at all
    (no ``claude`` ancestor in the chain — a detached/reparented hook, a ``ps``
    failure, or simply more than 25 hops). "Can't tell whose lock this is"
    must resolve to "don't touch it", never to "unlock anyway".

    The two outcomes are not symmetric, which is what makes this worth a test:
    holding a lock we shouldn't is self-healing (the pid dies, the next run
    reclaims the tree via the dead-lock path above), while releasing one we
    shouldn't is the 1497-file loss. An identity check that bails only on a
    positive mismatch — ``[ -n "$OWN" ] && [ "$OWN" != "$LOCK" ]`` — passes the
    nested test above yet still unlocks and reaps here.
    """
    primary, b = repo_trio["primary"], repo_trio["b"]
    hook = primary / "scripts" / "hooks" / "session-end-reap.sh"

    outer = subprocess.Popen(["sleep", "120"], env=_test_env())
    try:
        outer_pid = outer.pid
        _git(primary, "worktree", "lock", str(b), "--reason", f"pid {outer_pid}")

        # Run the hook straight from the test process: nothing in this
        # ancestry is a `claude`, so find_session_pid yields nothing.
        result = subprocess.run(
            ["bash", str(hook)],
            cwd=str(primary),
            input=json.dumps({"reason": "logout", "cwd": str(b)}),
            env=_test_env(),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        assert _lock_reason_for(primary, b) == f"pid {outer_pid}"
        assert b.exists()
    finally:
        outer.kill()
        outer.wait(timeout=10)


def test_session_end_reap_holds_when_shared_lib_is_missing(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    """gr256469, second fail-safe direction. This hook ``cd``s to the PRIMARY
    checkout, so its ``scripts/lib/session-lock.sh`` source resolves THERE —
    not in the worktree the hook shipped from. A worktree running ahead of
    main therefore has the new hook but an old primary with no lib, and the
    ownership guard has no reader for the lock at all.

    With no way to tell a genuinely-unlocked tree from one it merely cannot
    read, the hook must hold. The cost is an un-reaped worktree: visible,
    harmless, and self-correcting the moment the lib reaches the primary.
    """
    primary, b = repo_trio["primary"], repo_trio["b"]
    hook = primary / "scripts" / "hooks" / "session-end-reap.sh"
    lib = primary / "scripts" / "lib" / "session-lock.sh"
    assert lib.exists(), "fixture must stage the lib for its removal to mean anything"
    lib.unlink()

    outer = subprocess.Popen(["sleep", "120"], env=_test_env())
    try:
        outer_pid = outer.pid
        _git(primary, "worktree", "lock", str(b), "--reason", f"pid {outer_pid}")

        result = subprocess.run(
            ["bash", str(hook)],
            cwd=str(primary),
            input=json.dumps({"reason": "logout", "cwd": str(b)}),
            env=_test_env(),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        assert _lock_reason_for(primary, b) == f"pid {outer_pid}"
        assert b.exists()
    finally:
        outer.kill()
        outer.wait(timeout=10)


def _reassert(
    lib: Path, worktree: Path, start_pid: int, cwd: Path, **env_extra: str
) -> subprocess.CompletedProcess[str]:
    """Drive ``reassert_session_lock`` straight out of the staged lib.

    ``start_pid`` is handed in explicitly rather than faked up through a
    process tree: ``find_session_pid`` starts its walk AT that pid, so passing
    a live ``comm == claude`` process's own pid resolves on the first hop.
    That keeps these tests about the re-assertion policy (steal / don't steal)
    instead of re-testing the ancestry walk, which the tests above already pin.
    """
    env = _test_env()
    env.update(env_extra)
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "{lib}"; reassert_session_lock "{worktree}" "{start_pid}"',
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def test_reassert_session_lock_relocks_a_lockless_shipped_tree(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    """The point of proposal 3 in
    ``docs/backlog/reaper-removed-live-session-worktree.md``: ``scripts/ship``
    leaves a tree clean + merged, which is exactly the ``safe_remove`` shape a
    sibling session's reaper deletes. If the lock has gone missing while the
    session is still alive, ship must put it back.

    The fixture's B is already in that post-ship state, so asserting the
    bucket flips away from ``safe_remove`` is what makes this a test of the
    actual exposure rather than of the lock string alone.
    """
    primary, b = repo_trio["primary"], repo_trio["b"]
    lib = primary / "scripts" / "lib" / "session-lock.sh"
    claude_bin = _make_fake_claude_binary(tmp_path)

    assert _lock_reason_for(primary, b) is None
    before = _bucket_for(
        _run([str(primary / "scripts" / "inflight"), "--json"], primary).stdout, b
    )
    assert before["bucket"] == "safe_remove", (
        "fixture must start in the exposed state for this test to mean anything"
    )

    session = subprocess.Popen(
        [str(claude_bin), "-c", "sleep 120; true"], env=_test_env()
    )
    try:
        result = _reassert(lib, b, session.pid, cwd=primary)
        assert result.returncode == 0, result.stderr
        assert result.stdout == str(session.pid), result.stderr

        assert _lock_reason_for(primary, b) == f"pid {session.pid}"
        after = _bucket_for(
            _run([str(primary / "scripts" / "inflight"), "--json"], primary).stdout, b
        )
        assert after["bucket"] != "safe_remove"
    finally:
        session.kill()
        session.wait(timeout=10)


def test_reassert_session_lock_never_steals_a_live_lock(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    """A lock naming a pid that is alive right now is left strictly alone — it
    is either this session's already (nothing to do) or an unrelated
    session's, and moving one out from under a live session is the failure
    mode this whole file exists to prevent. Same asymmetry as the hooks: a
    stale lock self-heals, a stolen one loses work.
    """
    primary, b = repo_trio["primary"], repo_trio["b"]
    lib = primary / "scripts" / "lib" / "session-lock.sh"
    claude_bin = _make_fake_claude_binary(tmp_path)

    holder = subprocess.Popen(["sleep", "120"], env=_test_env())
    session = subprocess.Popen(
        [str(claude_bin), "-c", "sleep 120; true"], env=_test_env()
    )
    try:
        _git(primary, "worktree", "lock", str(b), "--reason", f"pid {holder.pid}")

        result = _reassert(lib, b, session.pid, cwd=primary)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "", "must not report having taken a lock it left alone"
        assert _lock_reason_for(primary, b) == f"pid {holder.pid}"
    finally:
        for proc in (holder, session):
            proc.kill()
            proc.wait(timeout=10)


def test_reassert_session_lock_reclaims_a_dead_lock(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    """The mirror of the test above, and the reason it can't simply bail on
    "a lock exists": the state that actually shows up in practice is a lock
    naming a pid that has since died (a nested ``claude -p`` that exited, a
    killed background task). That is not a live claim on the tree, so ship
    reclaims it.
    """
    primary, b = repo_trio["primary"], repo_trio["b"]
    lib = primary / "scripts" / "lib" / "session-lock.sh"
    claude_bin = _make_fake_claude_binary(tmp_path)

    dead = subprocess.Popen(["true"], env=_test_env())
    dead.wait(timeout=10)
    assert _wait_for(lambda: not _pid_alive(dead.pid), timeout=5.0)
    _git(primary, "worktree", "lock", str(b), "--reason", f"pid {dead.pid}")

    session = subprocess.Popen(
        [str(claude_bin), "-c", "sleep 120; true"], env=_test_env()
    )
    try:
        result = _reassert(lib, b, session.pid, cwd=primary)
        assert result.returncode == 0, result.stderr
        assert _lock_reason_for(primary, b) == f"pid {session.pid}"
    finally:
        session.kill()
        session.wait(timeout=10)


def test_reassert_session_lock_honours_the_no_autoreap_escape_hatch(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    """``PRECIS_NO_AUTOREAP=1`` turns off the whole reap apparatus, so there is
    nothing for a lock to protect against — all three reapers no-op on it and
    this must too, or a nested ``claude -p`` running a ship would start
    writing locks the reapers have been told to ignore.
    """
    primary, b = repo_trio["primary"], repo_trio["b"]
    lib = primary / "scripts" / "lib" / "session-lock.sh"
    claude_bin = _make_fake_claude_binary(tmp_path)

    session = subprocess.Popen(
        [str(claude_bin), "-c", "sleep 120; true"], env=_test_env()
    )
    try:
        result = _reassert(lib, b, session.pid, cwd=primary, PRECIS_NO_AUTOREAP="1")
        assert result.returncode == 0, result.stderr
        assert _lock_reason_for(primary, b) is None
    finally:
        session.kill()
        session.wait(timeout=10)


def test_reassert_session_lock_noops_on_the_primary_checkout(
    repo_trio: dict[str, Path],
    tmp_path: Path,
) -> None:
    """Only trees under ``.claude/worktrees/`` are ever locked — the primary
    checkout is never reaped and ``session-start-lock.sh`` skips it too. ship
    runs from the primary whenever a repo isn't using worktrees at all, so
    this path is reachable and must stay quiet rather than locking it.
    """
    primary = repo_trio["primary"]
    lib = primary / "scripts" / "lib" / "session-lock.sh"
    claude_bin = _make_fake_claude_binary(tmp_path)

    session = subprocess.Popen(
        [str(claude_bin), "-c", "sleep 120; true"], env=_test_env()
    )
    try:
        result = _reassert(lib, primary, session.pid, cwd=primary)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert _lock_reason_for(primary, primary) is None
    finally:
        session.kill()
        session.wait(timeout=10)


@pytest.fixture
def guard_repo(tmp_path: Path) -> dict[str, Path]:
    """A throwaway repo with the same post-ship 'merged + clean' shape for
    worktree B as ``repo_trio``, but with a ``.gitignore`` for ``.claude/*``
    (mirroring THIS repo's own — see the module docstring) and only the two
    scripts these tests actually exercise (``inflight``, ``reap-worktrees`` —
    no hooks/lock lib, since none of the gr260192 guards touch locking).
    Kept separate from ``repo_trio`` rather than risking a change to a
    fixture several other tests in this module already pin the exact shape
    of.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")

    scripts_dir = primary / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    shutil.copy2(REAP_SRC, scripts_dir / "reap-worktrees")
    (scripts_dir / "inflight").chmod(0o755)
    (scripts_dir / "reap-worktrees").chmod(0o755)

    (primary / ".gitignore").write_text(".claude/*\n", encoding="utf-8")
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")

    b = primary / ".claude" / "worktrees" / "B"
    _git(primary, "worktree", "add", "-q", "-b", "worktree-B", str(b), "main")

    # Real work, squash-merged into main, then B reset onto shipped main —
    # merged + clean, the safe_remove trigger shape (see repo_trio).
    (b / "feature.txt").write_text("feature work\n", encoding="utf-8")
    _git(b, "add", "-A")
    _git(b, "commit", "-q", "-m", "feature work")
    _git(primary, "merge", "-q", "--squash", "worktree-B")
    _git(primary, "commit", "-q", "-m", "feature work (squashed)")
    _git(b, "reset", "-q", "--hard", "main")

    return {"primary": primary, "b": b}


def _reap_env(**overrides: str) -> dict[str, str]:
    env = _test_env()
    env.update(overrides)
    return env


def test_reap_worktrees_purpose_tripwire_blocks_removal(
    guard_repo: dict[str, Path],
) -> None:
    """docs/backlog/reaper-removed-live-session-worktree.md proposal 2: a
    FRESH ``.claude/purpose`` file in an otherwise safe_remove tree (merged +
    clean, per the fixture) demotes it to not-removed even though nothing
    else about the bucket changed — the false positive the tripwire exists
    to catch: a session that just wrote its purpose and is mid-turn in a
    tree that momentarily looks removable.
    """
    primary, b = guard_repo["primary"], guard_repo["b"]
    reap = primary / "scripts" / "reap-worktrees"
    inflight = primary / "scripts" / "inflight"

    before = _bucket_for(_run([str(inflight), "--json"], primary).stdout, b)
    assert before["bucket"] == "safe_remove", (
        "fixture must start safe_remove for this test to mean anything"
    )

    purpose_dir = b / ".claude"
    purpose_dir.mkdir(parents=True, exist_ok=True)
    (purpose_dir / "purpose").write_text("mid-task work\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(reap)],
        cwd=str(primary),
        env=_reap_env(PRECIS_REAP_GRACE_SECONDS="1"),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "purpose" in result.stdout.lower(), result.stdout

    assert b.exists()
    wt_list = _git(primary, "worktree", "list", "--porcelain").stdout
    assert str(b) in wt_list


def test_reap_worktrees_purpose_tripwire_respects_staleness_threshold(
    guard_repo: dict[str, Path],
) -> None:
    """The tripwire is a THRESHOLD, not "any purpose file ever blocks reap
    forever": an old purpose file (mtime older than
    PRECIS_REAP_PURPOSE_FRESH_SECONDS) must not shield a genuinely-abandoned,
    merged+clean tree from the normal safe_remove path — otherwise a stale
    purpose left over from a long-finished task would leak worktrees forever.
    """
    primary, b = guard_repo["primary"], guard_repo["b"]
    reap = primary / "scripts" / "reap-worktrees"

    purpose_dir = b / ".claude"
    purpose_dir.mkdir(parents=True, exist_ok=True)
    purpose_file = purpose_dir / "purpose"
    purpose_file.write_text("old task, long done\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(purpose_file, (old, old))

    result = subprocess.run(
        ["bash", str(reap)],
        cwd=str(primary),
        env=_reap_env(
            PRECIS_REAP_GRACE_SECONDS="1",
            PRECIS_REAP_PURPOSE_FRESH_SECONDS="60",
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    assert not b.exists()
    wt_list = _git(primary, "worktree", "list", "--porcelain").stdout
    assert str(b) not in wt_list


def test_reap_worktrees_grace_period_skips_a_tree_that_goes_dirty_mid_sleep(
    guard_repo: dict[str, Path],
) -> None:
    """docs/backlog/reaper-liveness-race.md / proposal 1: the bucket is
    re-verified a SECOND time, right before removal, after a grace-period
    sleep — not just once up front. A tree that was safe_remove at the start
    of the sleep but picks up untracked work during it (a session resuming
    mid-turn) must survive.
    """
    primary, b = guard_repo["primary"], guard_repo["b"]
    reap = primary / "scripts" / "reap-worktrees"

    proc = subprocess.Popen(
        ["bash", str(reap)],
        cwd=str(primary),
        env=_reap_env(PRECIS_REAP_GRACE_SECONDS="3"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.5)
        assert b.exists(), "worktree removed before the grace sleep even elapsed"
        (b / "resumed-work.txt").write_text("mid-turn edit\n", encoding="utf-8")

        stdout, stderr = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    assert proc.returncode == 0, stderr
    assert "skip" in stdout.lower(), stdout

    assert b.exists()
    wt_list = _git(primary, "worktree", "list", "--porcelain").stdout
    assert str(b) in wt_list


def test_reap_worktrees_grace_period_still_reaps_when_nothing_changes(
    guard_repo: dict[str, Path],
) -> None:
    """Guards against over-correcting proposal 1 into a reaper that never
    removes anything: a tree that is STILL safe_remove after the grace sleep
    (nothing changed) must be reaped normally, same as before the hardening.
    """
    primary, b = guard_repo["primary"], guard_repo["b"]
    reap = primary / "scripts" / "reap-worktrees"

    result = subprocess.run(
        ["bash", str(reap)],
        cwd=str(primary),
        env=_reap_env(PRECIS_REAP_GRACE_SECONDS="1"),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    assert not b.exists()
    wt_list = _git(primary, "worktree", "list", "--porcelain").stdout
    assert str(b) not in wt_list


def test_ship_wires_the_lock_re_assertion_at_both_windows() -> None:
    """The lib function is inert unless ``scripts/ship`` actually calls it, and
    a shell script draws no coverage signal from the gate — so pin the wiring
    directly. Two calls, deliberately: one up front (repairing a lock already
    lost before this run, so the exposure never spans the whole gate) and one
    after the branch reset, which is the moment the tree becomes clean +
    merged and therefore removable.
    """
    ship = (REPO_ROOT / "scripts" / "ship").read_text(encoding="utf-8")
    assert "scripts/lib/session-lock.sh" in ship, "ship must source the shared lib"
    assert ship.count("\n_relock\n") == 2, (
        "expected exactly two _relock call sites in scripts/ship "
        "(pre-gate repair + post-reset window)"
    )


# --- gr331378: scripts/inflight's squash-absorbed content-equality fallback ---
#
# `git cherry`'s per-commit patch-id matching only ever recognises a commit as
# merged if SOME commit reachable from $BASE carries an equivalent patch. A
# worktree with several local commits whose CUMULATIVE diff was squash-merged
# into main as one commit never satisfies that per-commit test -- none of the
# individual commits' patch-ids match the one squash commit -- so it stays
# `has_unmerged_work` forever, and nothing ever reaps it (this is the leak
# gr331378 traces the leaked precis-test-* networks back to). The three tests
# below build the exact multi-commit-then-squash-merge shape directly (no
# `repo_trio` -- that fixture's B is a SINGLE commit, already handled by the
# pre-existing `git cherry` path, and it also resets B onto main afterwards,
# which would mask the very gap being tested).


def test_inflight_buckets_squash_absorbed_multi_commit_branch_safe_remove(
    tmp_path: Path,
) -> None:
    """The positive case live-verified before this fix shipped: B carries TWO
    real commits, both squash-merged into main as a single commit, and B's
    own branch is never reset (the trigger state -- see module docstring
    above). `git cherry` alone reports both commits unmerged; the
    `merge-tree --write-tree` content-equality fallback must recognise the
    branch is fully absorbed anyway and flip the verdict/bucket to
    `in-main` / `safe_remove`.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")

    scripts_dir = primary / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    (scripts_dir / "inflight").chmod(0o755)
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")

    b = primary / ".claude" / "worktrees" / "b"
    _git(primary, "worktree", "add", "-q", "-b", "worktree-b", str(b), "main")

    (b / "feature.txt").write_text("line1\n", encoding="utf-8")
    _git(b, "add", "-A")
    _git(b, "commit", "-q", "-m", "commit 1")
    (b / "feature.txt").write_text("line1\nline2\n", encoding="utf-8")
    _git(b, "add", "-A")
    _git(b, "commit", "-q", "-m", "commit 2")

    # /land's squash-merge into main -- deliberately NOT followed by
    # resetting B's branch (unlike repo_trio): B still carries its own two
    # real commits, exactly the state gr331378's stuck branches were found in.
    _git(primary, "merge", "-q", "--squash", "worktree-b")
    _git(primary, "commit", "-q", "-m", "feature work (squashed)")

    cherry = _git(primary, "cherry", "main", "worktree-b").stdout
    assert cherry.count("+") == 2, (
        "fixture must reproduce git cherry reporting BOTH commits unmerged "
        "for this test to mean anything"
    )

    json_out = _run([str(primary / "scripts" / "inflight"), "--json"], primary).stdout
    bucket = _bucket_for(json_out, b)
    assert bucket["verdict"].startswith("in-main"), bucket
    assert bucket["bucket"] == "safe_remove", bucket


def test_inflight_keeps_true_unmerged_commit_as_has_unmerged_work(
    tmp_path: Path,
) -> None:
    """The negative guard against over-correcting the fallback: B's first
    commit is squash-merged into main, but B then goes on to add a SECOND
    commit whose diff was never merged anywhere. `merge-tree --write-tree`
    against main must NOT come out equal to main's own tree (the second
    commit's change is real, unabsorbed work), so the branch must stay
    `has_unmerged_work` -- the fallback must never mistake "some of this is
    absorbed" for "all of this is absorbed".
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")

    scripts_dir = primary / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    (scripts_dir / "inflight").chmod(0o755)
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")

    b = primary / ".claude" / "worktrees" / "b"
    _git(primary, "worktree", "add", "-q", "-b", "worktree-b", str(b), "main")

    (b / "feature.txt").write_text("line1\n", encoding="utf-8")
    _git(b, "add", "-A")
    _git(b, "commit", "-q", "-m", "commit 1")

    _git(primary, "merge", "-q", "--squash", "worktree-b")
    _git(primary, "commit", "-q", "-m", "feature work (squashed)")

    # B carries on with real, still-unmerged work.
    (b / "feature.txt").write_text("line1\nline2\n", encoding="utf-8")
    _git(b, "add", "-A")
    _git(b, "commit", "-q", "-m", "commit 2 (never merged)")

    json_out = _run([str(primary / "scripts" / "inflight"), "--json"], primary).stdout
    bucket = _bucket_for(json_out, b)
    assert bucket["bucket"] == "has_unmerged_work", bucket
    assert bucket["verdict"].startswith("↑"), bucket


def test_inflight_squash_absorbed_falls_back_gracefully_on_old_git(
    tmp_path: Path,
) -> None:
    """The explicit git-version guard: on a git that errors on `merge-tree
    --write-tree` (older than 2.38, or any other reason it fails), the
    fallback must swallow the failure rather than crash `set -uo pipefail`
    inflight, and the verdict must fall all the way back through to
    whatever `git cherry` already decided -- unabsorbed stays
    `has_unmerged_work`, never silently upgraded to safe_remove on a check
    that never actually ran.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    real_git = shutil.which("git")
    assert real_git, "need a real git on PATH to build the fixture through the shim"

    scripts_dir = primary / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    (scripts_dir / "inflight").chmod(0o755)

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    git_shim = fakebin / "git"
    git_shim.write_text(
        f"""#!/usr/bin/env bash
if [ "${{1:-}}" = "merge-tree" ] && [ "${{2:-}}" = "--write-tree" ]; then
    echo "error: unknown option '"'"'--write-tree'"'"'" >&2
    exit 129
fi
exec "{real_git}" "$@"
""",
        encoding="utf-8",
    )
    git_shim.chmod(0o755)

    env = _test_env()
    env["PATH"] = f"{fakebin}:{env['PATH']}"

    def _shimgit(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True
        )
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        return result

    _shimgit(primary, "init", "-q", "-b", "main")
    _shimgit(primary, "config", "user.email", "test@example.com")
    _shimgit(primary, "config", "user.name", "Test")
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _shimgit(primary, "add", "-A")
    _shimgit(primary, "commit", "-q", "-m", "initial")

    b = primary / ".claude" / "worktrees" / "b"
    _shimgit(primary, "worktree", "add", "-q", "-b", "worktree-b", str(b), "main")

    (b / "feature.txt").write_text("line1\n", encoding="utf-8")
    _shimgit(b, "add", "-A")
    _shimgit(b, "commit", "-q", "-m", "commit 1")
    (b / "feature.txt").write_text("line1\nline2\n", encoding="utf-8")
    _shimgit(b, "add", "-A")
    _shimgit(b, "commit", "-q", "-m", "commit 2")

    _shimgit(primary, "merge", "-q", "--squash", "worktree-b")
    _shimgit(primary, "commit", "-q", "-m", "feature work (squashed)")

    # Confirm the shim actually intercepts before trusting the result below.
    probe = subprocess.run(
        ["git", "merge-tree", "--write-tree", "main", "worktree-b"],
        cwd=str(primary),
        env=env,
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0, "shim must fail merge-tree --write-tree"

    result = subprocess.run(
        [str(primary / "scripts" / "inflight"), "--json"],
        cwd=str(primary),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    bucket = _bucket_for(result.stdout, b)
    assert bucket["bucket"] == "has_unmerged_work", bucket
    assert bucket["verdict"].startswith("↑"), bucket


# --- gr331378: scripts/reap-test-dbs's abandoned-but-present carve-out ---
#
# The carve-out is only reachable through the script's very first line
# (`docker ps -a ... || exit 0`), so it cannot be exercised at all without
# something answering to `docker` on PATH -- and this repo's real Docker
# daemon runs actual sibling worktrees' compose projects that must never be
# touched by a test. The tests below build a fully synthetic `docker`
# (`_FAKE_DOCKER_SCRIPT`) driven entirely by fixture files, so the real
# daemon is never contacted. What genuinely can't be covered this way: the
# `docker inspect` / `docker ps --filter` OUTPUT SHAPES themselves (this
# fixture defines them, so a real-daemon format drift wouldn't be caught
# here) -- that half is only live-testable, e.g. by hand-verifying
# `scripts/reap-test-dbs --dry-run` against a real multi-day-old orphaned
# project.

_FAKE_DOCKER_SCRIPT = r"""#!/usr/bin/env bash
# Synthetic docker for scripts/reap-test-dbs tests -- NEVER touches the real
# daemon. Driven by three files named by env vars:
#   FAKE_DOCKER_CONTAINERS  rows: project|cid|service|config_files
#   FAKE_DOCKER_CREATED     rows: cid|RFC3339-created-timestamp
#   FAKE_DOCKER_LOG         appended to on every `compose ... down` call
set -u
CONTAINERS="${FAKE_DOCKER_CONTAINERS:?}"
CREATED="${FAKE_DOCKER_CREATED:?}"
LOG="${FAKE_DOCKER_LOG:?}"
args=("$@")
joined="$*"

case "${args[0]:-}" in
  ps)
    if [[ "$joined" == *"config_files"* ]]; then
        awk -F'|' '{print $1"|"$4}' "$CONTAINERS"
    elif [[ "$joined" == *"--filter"* ]]; then
        proj=""
        for a in "${args[@]}"; do
            case "$a" in
                label=com.docker.compose.project=*)
                    proj="${a#label=com.docker.compose.project=}"
                    ;;
            esac
        done
        awk -F'|' -v p="$proj" '$1==p {print $2"|"$3}' "$CONTAINERS"
    fi
    exit 0
    ;;
  inspect)
    cid="${args[$((${#args[@]}-1))]}"
    awk -F'|' -v c="$cid" '$1==c {print $2}' "$CREATED"
    exit 0
    ;;
  network)
    exit 0
    ;;
  compose)
    proj=""
    for i in "${!args[@]}"; do
        if [ "${args[$i]}" = "-p" ]; then
            proj="${args[$((i+1))]}"
        fi
    done
    echo "down|$proj" >> "$LOG"
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""


@pytest.fixture
def reap_db_repo(tmp_path: Path) -> dict[str, Path]:
    """A throwaway repo staging exactly what the carve-out needs:
    scripts/inflight (criterion 1 reads its `session` field verbatim),
    scripts/lib/compose-project.sh (project-name derivation), and
    scripts/reap-test-dbs itself -- plus one PRESENT worktree, `b`, whose
    `precis-test-b` project a synthetic `docker` (see `_FAKE_DOCKER_SCRIPT`)
    stands in for.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")

    scripts_dir = primary / "scripts"
    lib_dir = scripts_dir / "lib"
    lib_dir.mkdir(parents=True)
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    shutil.copy2(REAP_DB_SRC, scripts_dir / "reap-test-dbs")
    shutil.copy2(COMPOSE_PROJECT_LIB_SRC, lib_dir / "compose-project.sh")
    (scripts_dir / "inflight").chmod(0o755)
    (scripts_dir / "reap-test-dbs").chmod(0o755)
    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")

    b = primary / ".claude" / "worktrees" / "b"
    _git(primary, "worktree", "add", "-q", "-b", "worktree-b", str(b), "main")

    return {"primary": primary, "b": b}


def _iso_created(days_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def _run_reap_test_dbs(
    primary: Path,
    tmp_path: Path,
    containers: list[str],
    created: list[str],
    dry_run: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    docker_bin = fakebin / "docker"
    docker_bin.write_text(_FAKE_DOCKER_SCRIPT, encoding="utf-8")
    docker_bin.chmod(0o755)

    data_dir = tmp_path / "fake-docker-data"
    data_dir.mkdir(exist_ok=True)
    containers_file = data_dir / "containers"
    created_file = data_dir / "created"
    log_file = data_dir / "log"
    containers_file.write_text(
        "".join(f"{row}\n" for row in containers), encoding="utf-8"
    )
    created_file.write_text("".join(f"{row}\n" for row in created), encoding="utf-8")
    log_file.write_text("", encoding="utf-8")

    env = _test_env()
    env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"
    env["FAKE_DOCKER_CONTAINERS"] = str(containers_file)
    env["FAKE_DOCKER_CREATED"] = str(created_file)
    env["FAKE_DOCKER_LOG"] = str(log_file)

    cmd = [str(primary / "scripts" / "reap-test-dbs")]
    if dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(
        cmd, cwd=str(primary), env=env, capture_output=True, text=True
    )
    return result, log_file.read_text(encoding="utf-8")


def test_reap_test_dbs_carve_out_reaps_stale_present_project(
    reap_db_repo: dict[str, Path], tmp_path: Path
) -> None:
    """The positive path: `b` exists, is sessionless, has no
    `.claude/purpose`, its project has nothing running but the test-db
    container, and that container has been up for 3 days (>= the 48h
    default) -- all four guards pass, so the carve-out downs it."""
    primary, b = reap_db_repo["primary"], reap_db_repo["b"]
    containers = [f"precis-test-b|dbcid1|precis-test-db|{b}/docker/dev/compose.yaml"]
    created = [f"dbcid1|{_iso_created(3)}"]

    result, log = _run_reap_test_dbs(primary, tmp_path, containers, created)
    assert result.returncode == 0, result.stderr
    assert "down|precis-test-b" in log, (result.stdout, result.stderr, log)
    assert "precis-test-b" in result.stdout
    assert "reaped" in result.stdout.lower()


def test_reap_test_dbs_carve_out_holds_when_session_is_live(
    reap_db_repo: dict[str, Path], tmp_path: Path
) -> None:
    """Guard 1: a live session lock on `b` (naming this test process's own,
    definitely-alive pid) must block the carve-out even though the other
    three guards would otherwise pass."""
    primary, b = reap_db_repo["primary"], reap_db_repo["b"]
    _git(primary, "worktree", "lock", str(b), "--reason", f"pid {os.getpid()}")
    try:
        containers = [
            f"precis-test-b|dbcid1|precis-test-db|{b}/docker/dev/compose.yaml"
        ]
        created = [f"dbcid1|{_iso_created(3)}"]
        result, log = _run_reap_test_dbs(primary, tmp_path, containers, created)
        assert result.returncode == 0, result.stderr
        assert "down|precis-test-b" not in log, log
    finally:
        _git(primary, "worktree", "unlock", str(b))


def test_reap_test_dbs_carve_out_holds_when_purpose_is_fresh(
    reap_db_repo: dict[str, Path], tmp_path: Path
) -> None:
    """Guard 2: a fresh `.claude/purpose` (mirroring reap-worktrees' own
    tripwire) blocks the carve-out even though nothing else about `b`
    changed."""
    primary, b = reap_db_repo["primary"], reap_db_repo["b"]
    purpose_dir = b / ".claude"
    purpose_dir.mkdir(parents=True, exist_ok=True)
    (purpose_dir / "purpose").write_text("mid-task work\n", encoding="utf-8")

    containers = [f"precis-test-b|dbcid1|precis-test-db|{b}/docker/dev/compose.yaml"]
    created = [f"dbcid1|{_iso_created(3)}"]
    result, log = _run_reap_test_dbs(primary, tmp_path, containers, created)
    assert result.returncode == 0, result.stderr
    assert "down|precis-test-b" not in log, log


def test_reap_test_dbs_carve_out_holds_when_another_container_is_running(
    reap_db_repo: dict[str, Path], tmp_path: Path
) -> None:
    """Guard 3: a second, non-test-db container in the same project (a
    `precis-gate` or `run --rm` container) means a gate is actually in
    flight right now -- must never be downed out from under it."""
    primary, b = reap_db_repo["primary"], reap_db_repo["b"]
    containers = [
        f"precis-test-b|dbcid1|precis-test-db|{b}/docker/dev/compose.yaml",
        f"precis-test-b|gatecid1|precis-gate|{b}/docker/dev/compose.yaml",
    ]
    created = [f"dbcid1|{_iso_created(3)}"]
    result, log = _run_reap_test_dbs(primary, tmp_path, containers, created)
    assert result.returncode == 0, result.stderr
    assert "down|precis-test-b" not in log, log


def test_reap_test_dbs_carve_out_holds_when_db_container_is_too_young(
    reap_db_repo: dict[str, Path], tmp_path: Path
) -> None:
    """Guard 4: a test-db container that's only been up an hour is almost
    certainly mid-use -- must not be downed just because the other three
    guards happen to pass."""
    primary, b = reap_db_repo["primary"], reap_db_repo["b"]
    containers = [f"precis-test-b|dbcid1|precis-test-db|{b}/docker/dev/compose.yaml"]
    created = [f"dbcid1|{_iso_created(1 / 24)}"]
    result, log = _run_reap_test_dbs(primary, tmp_path, containers, created)
    assert result.returncode == 0, result.stderr
    assert "down|precis-test-b" not in log, log


def test_reap_test_dbs_carve_out_dry_run_lists_without_downing(
    reap_db_repo: dict[str, Path], tmp_path: Path
) -> None:
    """`--dry-run` reports the same candidate the live path would reap, but
    never actually calls `docker compose ... down`."""
    primary, b = reap_db_repo["primary"], reap_db_repo["b"]
    containers = [f"precis-test-b|dbcid1|precis-test-db|{b}/docker/dev/compose.yaml"]
    created = [f"dbcid1|{_iso_created(3)}"]
    result, log = _run_reap_test_dbs(
        primary, tmp_path, containers, created, dry_run=True
    )
    assert result.returncode == 0, result.stderr
    assert log == "", "dry-run must never actually down anything"
    assert "precis-test-b" in result.stdout
    assert "would reap" in result.stdout.lower()
