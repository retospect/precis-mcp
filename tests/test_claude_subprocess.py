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
import sys
from pathlib import Path

import pytest

from precis.utils._claude_subprocess import ClaudeProcessError, run_claude
from precis.utils.claude_oauth import ENV_VAR

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
        f"#!/usr/bin/env bash\nenv > {shlex.quote(str(dump_path))}\nexit 0\n"
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
    _vault(monkeypatch, "sk-ant-oat01-REAL-WORKER-TOKEN")

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


def test_bootstrap_oauth_default_true_injects_token_from_vault(
    tmp_path: Path, monkeypatch
) -> None:
    """Contrast case: a caller that does NOT ask for isolation
    (``bootstrap_oauth`` left at its default True) still gets the
    vault bootstrap — today's behavior, unchanged."""
    home = tmp_path / "home"
    home.mkdir()
    _vault(monkeypatch, "sk-ant-oat01-FROM-VAULT")

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
    assert f"{ENV_VAR}=sk-ant-oat01-FROM-VAULT" in dumped
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
    _vault(monkeypatch, "sk-ant-oat01-FROM-VAULT-2")
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
    assert f"{ENV_VAR}=sk-ant-oat01-FROM-VAULT-2" in dumped
    assert ENV_VAR not in os.environ
