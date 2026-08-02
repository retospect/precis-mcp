"""Tests for :mod:`precis.utils._claude_subprocess` — the shared ``run_claude``
harness, specifically ``bootstrap_oauth`` (§H cycle a, finding 1) and the
env-copy-not-mutate contract.

Unlike ``tests/test_claude_agent.py``'s ``test_env_base_skips_oauth_bootstrap``
(which monkeypatches ``run_claude`` itself and never actually calls it), these
tests exercise the REAL ``run_claude`` end to end: only the subprocess LAYER
is faked, via a tiny stub shell script that dumps its own environment to a
file. That's the only way to prove the OAuth-isolation guarantee actually
holds at the process boundary, not just at the mock boundary.
"""

from __future__ import annotations

import os
import shlex
import stat
from pathlib import Path

from precis.utils._claude_subprocess import ClaudeProcessError, run_claude
from precis.utils.claude_oauth import ENV_VAR


def _write_env_dump_stub(path: Path, dump_path: Path) -> None:
    """A stub ``claude`` binary that dumps its OWN process env (exactly what
    ``subprocess.run(env=...)`` handed it — a full replacement, not a merge)
    to ``dump_path``, then exits 0."""
    path.write_text(
        f"#!/usr/bin/env bash\nenv > {shlex.quote(str(dump_path))}\nexit 0\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)


def test_bootstrap_oauth_false_keeps_isolated_env_free_of_real_token(
    tmp_path: Path, monkeypatch
) -> None:
    """An isolated-env caller (fix_gripe's ``env_base``, §H cycle a) passes
    ``bootstrap_oauth=False`` precisely so the worker's REAL OAuth token
    never leaks into a sandboxed run that's meant to auth some other way
    (``ANTHROPIC_API_KEY``). Must hold even when ``~/.claude_oauth_token``
    exists on disk — the bootstrap must not even be consulted."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude_oauth_token").write_text("sk-ant-oat01-REAL-WORKER-TOKEN\n")
    monkeypatch.setattr(Path, "home", lambda: home)

    stub = tmp_path / "claude_stub.sh"
    dump = tmp_path / "env.out"
    _write_env_dump_stub(stub, dump)

    isolated_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "ANTHROPIC_API_KEY": "sk-ant-fake-isolated",
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

    dumped = dump.read_text()
    assert ENV_VAR not in dumped
    # the caller's own dict must be untouched — run_claude copies, never
    # mutates the dict it was handed.
    assert ENV_VAR not in isolated_env


def test_bootstrap_oauth_default_true_injects_token_from_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Contrast case: a caller that does NOT ask for isolation
    (``bootstrap_oauth`` left at its default True) still gets the
    ~/.claude_oauth_token bootstrap — today's behavior, unchanged."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude_oauth_token").write_text("sk-ant-oat01-FROM-FILE\n")
    monkeypatch.setattr(Path, "home", lambda: home)

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

    dumped = dump.read_text()
    assert f"{ENV_VAR}=sk-ant-oat01-FROM-FILE" in dumped
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
    (home / ".claude_oauth_token").write_text("sk-ant-oat01-FROM-FILE-2\n")
    monkeypatch.setattr(Path, "home", lambda: home)
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

    dumped = dump.read_text()
    assert f"{ENV_VAR}=sk-ant-oat01-FROM-FILE-2" in dumped
    assert ENV_VAR not in os.environ
