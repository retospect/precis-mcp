"""Tests for :mod:`precis.utils._claude_subprocess` — the shared ``run_claude``
harness: ``bootstrap_oauth`` (§H cycle a, finding 1), the env-copy-not-mutate
contract, and the non-zero-exit message carrying its cause (gr211457).

Unlike ``tests/test_claude_agent.py``'s ``test_env_base_skips_oauth_bootstrap``
(which monkeypatches ``run_claude`` itself and never actually calls it), these
tests exercise the REAL ``run_claude`` end to end: only the subprocess LAYER
is faked, via a tiny stub shell script that dumps its own environment to a
file. That's the only way to prove the OAuth-isolation guarantee actually
holds at the process boundary, not just at the mock boundary.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import precis.utils._claude_subprocess as _sub
from precis.utils._claude_subprocess import (
    ClaudeProcessError,
    exit_detail,
    run_claude,
    run_claude_async,
    terminate_claude,
)
from precis.utils.claude_oauth import ENV_VAR
from tests._timing import budget

#: Every test here execs a shebang'd bash stub as the ``claude`` binary — a
#: shape Windows can't run at all (``OSError: [WinError 193]``), and one that
#: only ever matters on the Linux hosts/containers that actually run claude.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only shebang stub exec'd as the claude binary",
)


def _write_env_dump_stub(path: Path, dump_path: Path) -> None:
    """A stub ``claude`` binary that dumps its OWN process env (exactly what
    ``subprocess.run(env=...)`` handed it — a full replacement, not a merge)
    to ``dump_path``, then exits 0."""
    path.write_text(
        f"#!/usr/bin/env bash\nenv > {shlex.quote(str(dump_path))}\nexit 0\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def _vault(monkeypatch, value: str | None) -> None:
    """Seed the token in the vault — the only store since 2026-08-07, when the
    per-user ``~/.claude_oauth_token`` leg was retired."""
    monkeypatch.setattr(
        "precis.secrets.get_secret", lambda name, **kw: value, raising=True
    )


def test_bootstrap_oauth_false_keeps_isolated_env_free_of_real_token(
    tmp_path: Path, monkeypatch
) -> None:
    """An isolated-env caller (fix_gripe's ``env_base``, §H cycle a) passes
    ``bootstrap_oauth=False`` precisely so the worker's REAL OAuth token
    never leaks into a sandboxed run that's meant to auth some other way
    (``ANTHROPIC_API_KEY``). Must hold even when the vault WOULD hand one over
    — the bootstrap must not even be consulted."""
    home = tmp_path / "home"
    home.mkdir()
    _vault(monkeypatch, ("sk-a" + "nt-oat01-REAL-WORKER-TOKEN"))

    stub = tmp_path / "claude_stub.sh"
    dump = tmp_path / "env.out"
    _write_env_dump_stub(stub, dump)

    isolated_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "ANTHROPIC_API_KEY": ("sk-a" + "nt-fake-isolated"),
    }
    assert ENV_VAR not in isolated_env

    run_claude(
        [str(stub)],
        binary=str(stub),
        label="claude -p (test)",
        timeout_s=10.0,
        error_cls=ClaudeProcessError,
        env=isolated_env,
        bootstrap_oauth=False,
    )

    dumped = dump.read_text(encoding="utf-8")
    assert ENV_VAR not in dumped
    # the caller's own dict must be untouched — run_claude copies, never
    # mutates the dict it was handed.
    assert ENV_VAR not in isolated_env


def test_bootstrap_oauth_default_true_injects_token_from_vault(
    tmp_path: Path, monkeypatch
) -> None:
    """Contrast case: a caller that does NOT ask for isolation
    (``bootstrap_oauth`` left at its default True) still gets the
    vault bootstrap — today's behavior, unchanged."""
    home = tmp_path / "home"
    home.mkdir()
    _vault(monkeypatch, ("sk-a" + "nt-oat01-FROM-VAULT"))

    stub = tmp_path / "claude_stub.sh"
    dump = tmp_path / "env.out"
    _write_env_dump_stub(stub, dump)

    caller_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}

    run_claude(
        [str(stub)],
        binary=str(stub),
        label="claude -p (test)",
        timeout_s=10.0,
        error_cls=ClaudeProcessError,
        env=caller_env,
    )

    dumped = dump.read_text(encoding="utf-8")
    assert (f"{ENV_VAR}=sk-a" + "nt-oat01-FROM-VAULT") in dumped
    # Still never mutates the caller's dict — only the internal copy that
    # was actually handed to the subprocess gains the token.
    assert ENV_VAR not in caller_env


def test_run_claude_env_none_does_not_touch_process_environ(
    tmp_path: Path, monkeypatch
) -> None:
    """``env=None`` (the ``os.environ``-inheriting default) must not leave
    the bootstrapped token sitting in ``os.environ`` itself — only the
    subprocess's copy should gain it."""
    home = tmp_path / "home"
    home.mkdir()
    _vault(monkeypatch, ("sk-a" + "nt-oat01-FROM-VAULT-2"))
    monkeypatch.delenv(ENV_VAR, raising=False)

    stub = tmp_path / "claude_stub.sh"
    dump = tmp_path / "env.out"
    _write_env_dump_stub(stub, dump)

    run_claude(
        [str(stub)],
        binary=str(stub),
        label="claude -p (test)",
        timeout_s=10.0,
        error_cls=ClaudeProcessError,
        env=None,
    )

    dumped = dump.read_text(encoding="utf-8")
    assert (f"{ENV_VAR}=sk-a" + "nt-oat01-FROM-VAULT-2") in dumped
    assert ENV_VAR not in os.environ


# ── non-zero exit: the cause must reach the message (gr211457) ────────────


def _write_failing_stub(path: Path, *, stdout: str = "", stderr: str = "") -> None:
    """A stub ``claude`` that writes the given streams and exits 1 — the
    shape of a real ``claude -p`` auth failure, which reports on STDOUT."""
    lines = ["#!/usr/bin/env bash"]
    if stdout:
        lines.append(f"printf %s {shlex.quote(stdout)}")
    if stderr:
        lines.append(f"printf %s {shlex.quote(stderr)} >&2")
    lines.append("exit 1")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def test_exit_detail_prefers_stderr_then_falls_back_to_stdout_tail() -> None:
    assert exit_detail("on stdout", "on stderr") == "on stderr"
    assert exit_detail("on stdout", "   ") == "on stdout"
    assert exit_detail("", "") == "(no output on stdout or stderr)"
    # stream-json puts the terminal result event (with the error text) LAST,
    # so a long stdout is tailed, not headed.
    long_out = "x" * 500 + "THE-REAL-ERROR"
    assert exit_detail(long_out, "").endswith("THE-REAL-ERROR")
    assert len(exit_detail(long_out, "")) == 400


def test_run_claude_missing_binary_sets_binary_missing_flag(tmp_path: Path) -> None:
    """gr335087: a missing ``claude`` binary carries a structural
    ``binary_missing`` flag (never sniffed from the message text downstream —
    :attr:`~precis.utils.llm.router.LlmResult.cli_unavailable`) distinct from
    every other failure mode (timeout, non-zero exit) — a HOST-configuration
    defect that retrying the identical call on the SAME host can never
    self-heal."""
    missing = tmp_path / "no-such-claude-binary"
    with pytest.raises(ClaudeProcessError) as exc:
        run_claude(
            [str(missing)],
            binary=str(missing),
            label="claude -p (test)",
            timeout_s=10.0,
            error_cls=ClaudeProcessError,
            env={"PATH": os.environ.get("PATH", "")},
            bootstrap_oauth=False,
        )
    assert exc.value.binary_missing is True
    assert exc.value.timed_out is False
    assert "claude binary not found" in str(exc.value)


def test_run_claude_async_missing_binary_sets_binary_missing_flag(
    tmp_path: Path, monkeypatch
) -> None:
    """Same structural flag on the async runner — its docstring promises the
    sync runner's failure shape, so the two must not drift."""
    _vault(monkeypatch, None)
    missing = tmp_path / "no-such-claude-binary"

    with pytest.raises(ClaudeProcessError) as exc:
        asyncio.run(
            run_claude_async(
                [str(missing)],
                binary=str(missing),
                label="claude -p (test)",
                timeout_s=10.0,
                error_cls=ClaudeProcessError,
                env={"PATH": os.environ.get("PATH", "")},
            )
        )
    assert exc.value.binary_missing is True
    assert exc.value.timed_out is False


def test_run_claude_surfaces_stdout_when_stderr_is_empty(tmp_path: Path) -> None:
    """A dead ``ANTHROPIC_API_KEY`` makes ``claude -p`` print
    "API key is invalid" on stdout and exit non-zero with an EMPTY stderr.
    The message used to read a bare "exited 1:" — the cause has to be in
    there, or the failure is undiagnosable from the job row alone."""
    stub = tmp_path / "claude_stub.sh"
    _write_failing_stub(stub, stdout="Invalid API key · Please run /login")

    with pytest.raises(ClaudeProcessError) as exc:
        run_claude(
            [str(stub)],
            binary=str(stub),
            label="claude -p (test)",
            timeout_s=10.0,
            error_cls=ClaudeProcessError,
            env={"PATH": os.environ.get("PATH", "")},
            bootstrap_oauth=False,
        )

    assert "Invalid API key" in str(exc.value)
    assert exc.value.returncode == 1
    # stderr still wins when it has content — unchanged behaviour.
    stub2 = tmp_path / "claude_stub2.sh"
    _write_failing_stub(stub2, stdout="noise on stdout", stderr="the real stderr")
    with pytest.raises(ClaudeProcessError) as exc2:
        run_claude(
            [str(stub2)],
            binary=str(stub2),
            label="claude -p (test)",
            timeout_s=10.0,
            error_cls=ClaudeProcessError,
            env={"PATH": os.environ.get("PATH", "")},
            bootstrap_oauth=False,
        )
    assert "the real stderr" in str(exc2.value)
    assert "noise on stdout" not in str(exc2.value)


def test_run_claude_async_surfaces_stdout_when_stderr_is_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """Same contract on the async runner — its docstring promises the sync
    runner's failure shape, so the two must not drift."""
    _vault(monkeypatch, None)
    stub = tmp_path / "claude_stub.sh"
    _write_failing_stub(stub, stdout='{"type":"result","is_error":true}\n')

    with pytest.raises(ClaudeProcessError) as exc:
        asyncio.run(
            run_claude_async(
                [str(stub)],
                binary=str(stub),
                label="claude -p (test)",
                timeout_s=10.0,
                error_cls=ClaudeProcessError,
                env={"PATH": os.environ.get("PATH", "")},
            )
        )

    assert '"is_error":true' in str(exc.value)


# --------------------------------------------------------------------------
# gripe 256469 — a nested ``claude -p`` must not run session-lifecycle hooks
# against the CALLER's worktree. Without PRECIS_NO_AUTOREAP the nested
# session's SessionEnd fires scripts/hooks/session-end-reap.sh, which runs
# ``git worktree remove`` on a clean+merged tree: one big-tier LLM call
# deleted 1497 tracked files out from under a live session on 2026-08-25.
# --------------------------------------------------------------------------

#: The env var scripts/hooks/session-end-reap.sh checks first thing.
_NO_AUTOREAP = "PRECIS_NO_AUTOREAP"


def test_run_claude_disarms_session_reap_hook(tmp_path: Path, monkeypatch) -> None:
    """Every ``run_claude`` subprocess is marked as a nested call, so the
    project's SessionEnd reaper no-ops instead of removing the caller's
    worktree (gr256469)."""
    home = tmp_path / "home"
    home.mkdir()
    _vault(monkeypatch, None)
    monkeypatch.delenv(_NO_AUTOREAP, raising=False)

    stub = tmp_path / "claude_stub.sh"
    dump = tmp_path / "env.out"
    _write_env_dump_stub(stub, dump)

    caller_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}

    run_claude(
        [str(stub)],
        binary=str(stub),
        label="claude -p (test)",
        timeout_s=10.0,
        error_cls=ClaudeProcessError,
        env=caller_env,
    )

    assert f"{_NO_AUTOREAP}=1" in dump.read_text(encoding="utf-8")
    # copy-not-mutate holds for this injection too
    assert _NO_AUTOREAP not in caller_env


def test_run_claude_disarms_reap_even_for_isolated_env_callers(
    tmp_path: Path, monkeypatch
) -> None:
    """The guard is NOT tied to the OAuth bootstrap: a caller that opts out of
    token injection (``bootstrap_oauth=False``, fix_gripe's sandboxed
    ``env_base``) still spawns a real nested ``claude`` and so still must not
    be able to reap the caller's tree."""
    home = tmp_path / "home"
    home.mkdir()
    _vault(monkeypatch, ("sk-a" + "nt-oat01-REAL-WORKER-TOKEN"))
    monkeypatch.delenv(_NO_AUTOREAP, raising=False)

    stub = tmp_path / "claude_stub.sh"
    dump = tmp_path / "env.out"
    _write_env_dump_stub(stub, dump)

    isolated_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}

    run_claude(
        [str(stub)],
        binary=str(stub),
        label="claude -p (test)",
        timeout_s=10.0,
        error_cls=ClaudeProcessError,
        env=isolated_env,
        bootstrap_oauth=False,
    )

    dumped = dump.read_text(encoding="utf-8")
    assert f"{_NO_AUTOREAP}=1" in dumped
    assert ENV_VAR not in dumped  # isolation still intact
    assert _NO_AUTOREAP not in isolated_env


def test_run_claude_async_disarms_session_reap_hook(
    tmp_path: Path, monkeypatch
) -> None:
    """Same guarantee on the async runner — the ``claude_p``/``claude_agent``
    streaming path spawns just as real a nested session."""
    home = tmp_path / "home"
    home.mkdir()
    _vault(monkeypatch, None)
    monkeypatch.delenv(_NO_AUTOREAP, raising=False)

    stub = tmp_path / "claude_stub.sh"
    dump = tmp_path / "env.out"
    _write_env_dump_stub(stub, dump)

    caller_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}

    asyncio.run(
        run_claude_async(
            [str(stub)],
            binary=str(stub),
            label="claude -p (test)",
            timeout_s=10.0,
            error_cls=ClaudeProcessError,
            env=caller_env,
        )
    )

    assert f"{_NO_AUTOREAP}=1" in dump.read_text(encoding="utf-8")


def test_run_claude_async_copies_caller_env(tmp_path: Path, monkeypatch) -> None:
    """``run_claude_async`` must honour the same copy-not-mutate contract
    ``run_claude`` documents — it previously injected into the caller's dict in
    place, so an isolated ``env_base`` silently gained the OAuth token."""
    home = tmp_path / "home"
    home.mkdir()
    _vault(monkeypatch, ("sk-a" + "nt-oat01-FROM-VAULT"))
    monkeypatch.delenv(_NO_AUTOREAP, raising=False)

    stub = tmp_path / "claude_stub.sh"
    dump = tmp_path / "env.out"
    _write_env_dump_stub(stub, dump)

    caller_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}

    asyncio.run(
        run_claude_async(
            [str(stub)],
            binary=str(stub),
            label="claude -p (test)",
            timeout_s=10.0,
            error_cls=ClaudeProcessError,
            env=caller_env,
        )
    )

    # the subprocess got both injections ...
    dumped = dump.read_text(encoding="utf-8")
    assert (f"{ENV_VAR}=sk-a" + "nt-oat01-FROM-VAULT") in dumped
    assert f"{_NO_AUTOREAP}=1" in dumped
    # ... while the caller's own dict stayed exactly as it was handed over.
    assert ENV_VAR not in caller_env
    assert _NO_AUTOREAP not in caller_env


# ── timeout kills: SIGTERM the child pid first, then SIGKILL ─────────────
#
# The runners keep claude in the worker's OWN process group (no
# start_new_session, Reto ruling 2026-10-26): launchd kills the job's
# group on exit (AbandonProcessGroup=false), so a deploy restart reaps an
# in-flight run — and killpg is off the table because the child's group
# is OURS. When PRECIS_CLAUDE_BIN is the sudo -n re-exec wrapper the
# direct child is `sudo`: SIGKILL cannot be relayed — a bare proc.kill()
# reaps sudo and leaves claude running as the run user, a uid nothing on
# the host can signal — and sudo relays TERM only from a sender OUTSIDE
# its own group (no-pty mode drops si_pgrp == ppgrp), so TERM goes out
# via a session-fresh /bin/kill. terminate_claude TERM-to-KILLs the pid
# with a bounded grace; the wrapper's own parent-death watchdog covers a
# sudo that dies without relaying. The stubs below trap TERM to prove
# ordering.


def _write_term_stub(
    path: Path, trap_body: str, pid_path: Path, ready_path: Path
) -> None:
    """A stub that installs ``trap_body`` on TERM, records its pid, touches
    ``ready_path``, then runs forever — ``sleep & wait`` so the trap fires
    promptly when the pid is signalled (bash's ``wait`` returns at once on
    a trapped signal). Trap FIRST: a TERM delivered before the install
    would hit the default disposition, so callers must wait on
    ``ready_path``. The 1 s sleep keeps any orphaned grandchild short
    lived."""
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"trap '{trap_body}' TERM\n"
        f"echo $$ > {shlex.quote(str(pid_path))}\n"
        f"touch {shlex.quote(str(ready_path))}\n"
        "while :; do sleep 1 & wait; done\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def _ready(path: Path, timeout: float = 10.0) -> None:
    """Wait for the stub's ready marker — i.e. for its TERM trap to be
    installed — before signalling."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"stub never became ready: {path}")


def _spawn(path: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_terminate_claude_term_alone_reaps_a_compliant_child(tmp_path: Path) -> None:
    """A child that exits on TERM is reaped inside the grace — no KILL
    ever sent. If KILL came first the stub could not exit 0."""
    pid_path = tmp_path / "pid.out"
    ready = tmp_path / "ready.out"
    stub = tmp_path / "stub.sh"
    _write_term_stub(stub, "exit 0", pid_path, ready)
    proc = _spawn(stub)
    _ready(ready)

    started = time.monotonic()
    terminate_claude(proc, grace_s=5.0)

    assert proc.returncode == 0
    assert time.monotonic() - started < budget(4.0)  # did not ride out the grace


def test_terminate_claude_sends_term_from_a_foreign_process_group(
    tmp_path: Path, monkeypatch
) -> None:
    """sudo in no-pty mode drops a TERM whose sender sits in sudo's own
    process group — and the session-less runner shares it. The TERM must
    therefore go out via ``/bin/kill`` spawned in a NEW session so sudo
    relays it to claude. KILL needs no relay — it reaps sudo directly."""
    pid_path = tmp_path / "pid.out"
    ready = tmp_path / "ready.out"
    stub = tmp_path / "stub.sh"
    _write_term_stub(stub, ":", pid_path, ready)  # swallows TERM, keeps running
    proc = _spawn(stub)
    _ready(ready)

    seen: list[tuple[tuple, dict]] = []

    def fake_run(*args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(_sub.subprocess, "run", fake_run)

    terminate_claude(proc, grace_s=0.3)

    assert seen == [
        (
            (["/bin/kill", "-TERM", str(proc.pid)],),
            {"start_new_session": True, "timeout": 5, "check": False},
        )
    ]
    # TERM was faked away, yet the bounded grace + KILL still reaped it.
    assert proc.returncode is not None
    assert _dead(int(pid_path.read_text(encoding="utf-8").strip()))


def test_terminate_claude_escalates_when_term_is_swallowed(tmp_path: Path) -> None:
    """A child that survives TERM (traps it and keeps running) gets the
    KILL after the grace — and the marker proves TERM went first, not
    KILL."""
    pid_path = tmp_path / "pid.out"
    ready = tmp_path / "ready.out"
    marker = tmp_path / "term.out"
    stub = tmp_path / "stub.sh"
    _write_term_stub(stub, f"echo term >> {shlex.quote(str(marker))}", pid_path, ready)
    proc = _spawn(stub)
    _ready(ready)

    terminate_claude(proc, grace_s=0.3)

    assert marker.read_text(encoding="utf-8").strip() == "term"
    assert proc.returncode is not None
    assert _dead(int(pid_path.read_text(encoding="utf-8").strip()))


def test_run_claude_timeout_terms_then_escalates(tmp_path: Path, monkeypatch) -> None:
    """End to end through run_claude: the timeout path must deliver TERM
    (the stub's trap writes to stderr, which the timeout error carries)
    and still reap the child — a bare KILL would show neither."""
    monkeypatch.setattr(_sub, "_TERM_GRACE_S", 0.3)
    pid_path = tmp_path / "pid.out"
    ready = tmp_path / "ready.out"
    stub = tmp_path / "stub.sh"
    _write_term_stub(stub, "echo saw-term >&2", pid_path, ready)

    with pytest.raises(ClaudeProcessError) as exc:
        run_claude(
            [str(stub)],
            binary=str(stub),
            label="claude -p (test)",
            timeout_s=0.3,
            error_cls=ClaudeProcessError,
            env={"PATH": os.environ.get("PATH", "")},
            bootstrap_oauth=False,
        )

    assert exc.value.timed_out is True
    assert "saw-term" in exc.value.stderr
    assert _dead(int(pid_path.read_text(encoding="utf-8").strip()))


def _write_pipe_hog_stub(path: Path, ready_path: Path) -> None:
    """A stub that dies cleanly on TERM but leaves a grandchild holding the
    stdout/stderr pipe write ends — the shape of a TERM-ignoring claude
    behind a KILLed sudo: the direct child is gone, the pipes never EOF."""
    path.write_text(
        "#!/usr/bin/env bash\n"
        "trap 'exit 0' TERM\n"
        f"touch {shlex.quote(str(ready_path))}\n"
        "sleep 60 &\n"
        "wait\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def test_run_claude_timeout_drain_is_bounded(tmp_path: Path, monkeypatch) -> None:
    """After terminate_claude reaps the direct child, a grandchild can
    still hold the pipe write ends — an unbounded ``communicate()`` then
    wedges the worker thread on an EOF that never comes. The drain must
    give up inside ``_REAP_DRAIN_S`` and still raise ``timed_out``."""
    monkeypatch.setattr(_sub, "_TERM_GRACE_S", 0.3)
    monkeypatch.setattr(_sub, "_REAP_DRAIN_S", 0.5)
    ready = tmp_path / "ready.out"
    stub = tmp_path / "stub.sh"
    _write_pipe_hog_stub(stub, ready)

    started = time.monotonic()
    with pytest.raises(ClaudeProcessError) as exc:
        run_claude(
            [str(stub)],
            binary=str(stub),
            label="claude -p (test)",
            timeout_s=0.3,
            error_cls=ClaudeProcessError,
            env={"PATH": os.environ.get("PATH", "")},
            bootstrap_oauth=False,
        )

    assert exc.value.timed_out is True
    # 0.3 timeout + 0.3 grace + 0.5 drain, plus slack — nowhere near the
    # 60 s the orphaned grandchild keeps the pipes open.
    assert time.monotonic() - started < budget(10.0)


# ── no start_new_session: the worker's process group owns claude ─────────
#
# Reto ruling 2026-10-26: a detached session survives a launchd job exit
# (AbandonProcessGroup defaults to false — the group dies with the job),
# so start_new_session would orphan every in-flight claude on each deploy
# restart — still spending tokens, still making MCP writes, with TERM
# unreachable under the run-user wrapper. The runners must never pass it.


def _popen_spy(monkeypatch) -> list[dict[str, object]]:
    """Record the kwargs of every ``subprocess.Popen`` the code under test
    makes (delegating to the real one)."""
    seen: list[dict[str, object]] = []
    real = subprocess.Popen

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(_sub.subprocess, "Popen", spy)
    return seen


def _write_exit_stub(path: Path) -> None:
    path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def test_run_claude_stays_in_the_worker_process_group(
    tmp_path: Path, monkeypatch
) -> None:
    seen = _popen_spy(monkeypatch)
    stub = tmp_path / "stub.sh"
    _write_exit_stub(stub)

    run_claude(
        [str(stub)],
        binary=str(stub),
        label="claude -p (test)",
        timeout_s=10.0,
        error_cls=ClaudeProcessError,
        env={"PATH": os.environ.get("PATH", "")},
        bootstrap_oauth=False,
    )

    assert seen, "run_claude never spawned a Popen"
    assert all("start_new_session" not in kw for kw in seen)


def test_run_claude_gated_stays_in_the_worker_process_group(
    tmp_path: Path, monkeypatch
) -> None:
    seen = _popen_spy(monkeypatch)
    stub = tmp_path / "stub.sh"
    _write_exit_stub(stub)

    with pytest.raises(ClaudeProcessError):  # gate fails: the stub exits
        _sub.run_claude_gated(
            [str(stub)],
            prompt="hi",
            require_mcp=("precis",),
            gate_deadline_s=5.0,
            binary=str(stub),
            label="claude -p (test)",
            timeout_s=10.0,
            error_cls=ClaudeProcessError,
            env={"PATH": os.environ.get("PATH", "")},
            bootstrap_oauth=False,
        )

    assert seen, "run_claude_gated never spawned a Popen"
    assert all("start_new_session" not in kw for kw in seen)


def test_run_claude_async_stays_in_the_worker_process_group(
    tmp_path: Path, monkeypatch
) -> None:
    _vault(monkeypatch, None)
    seen: list[dict[str, object]] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        seen.append(kwargs)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(_sub.asyncio, "create_subprocess_exec", spy)
    stub = tmp_path / "stub.sh"
    _write_exit_stub(stub)

    asyncio.run(
        run_claude_async(
            [str(stub)],
            binary=str(stub),
            label="claude -p (test)",
            timeout_s=10.0,
            error_cls=ClaudeProcessError,
            env={"PATH": os.environ.get("PATH", "")},
        )
    )

    assert seen, "run_claude_async never spawned a subprocess"
    assert all("start_new_session" not in kw for kw in seen)
