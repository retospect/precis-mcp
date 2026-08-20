"""Regression for job 210205 (dark-factory rung 2, 2026-08-16): the shared
``docker/docker-entrypoint.sh`` unconditionally required ``PRECIS_DATABASE_URL``,
but the ``agent`` image stage reuses it for deliberately DB-less ``claude -p``
runs (diagnose_gripe/fix_gripe forward no DSN — see
``workers/executors/agent_container.py::container_env``) — so every
containerized diagnose/fix agent died at the entrypoint before claude started.
The check must gate on the command actually being a ``precis …`` invocation
(every DB-needing stage's CMD starts with ``precis``; the agent stage's runs
start with ``claude``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

#: The entrypoint under test is a POSIX shell script the images run as PID 1;
#: there is no `bash` on a stock Windows runner and no Windows image stage to
#: gate, so the whole module is Linux/macOS-only.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only shell entrypoint (bash, exec)"
)

_ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker" / "docker-entrypoint.sh"

#: A minimal env with NO ``PRECIS_DATABASE_URL`` (and no ``/secrets`` dir in
#: play) — ``PATH`` retained so ``bash`` can find ``echo``/coreutils.
_NO_DSN_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def _run(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_ENTRYPOINT), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def test_precis_invocation_without_dsn_fails() -> None:
    """The original server-image contract stays: ``precis …`` with no DSN is
    a hard error at the entrypoint (fail fast, not deep inside libpq)."""
    proc = _run("precis", "serve", env=_NO_DSN_ENV)
    assert proc.returncode == 1
    assert "PRECIS_DATABASE_URL not set" in proc.stderr


def test_non_precis_invocation_without_dsn_execs() -> None:
    """The regression this test exists for: a DB-less agent-shaped command
    (argv starts with something other than ``precis``, e.g. ``claude -p``)
    must exec straight through — no DSN demanded."""
    proc = _run("echo", "agent-ok", env=_NO_DSN_ENV)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "agent-ok"
    assert "PRECIS_DATABASE_URL" not in proc.stderr


def test_precis_invocation_with_dsn_execs() -> None:
    """Sanity: with the DSN present, a ``precis``-shaped argv passes the
    check and execs (stubbed here — PATH is pinned to system dirs so the real
    ``precis`` venv binary is NOT findable, making exec's 127 the
    deterministic signal that the entrypoint got past validation and actually
    tried to run it — without launching a real server against a fake DSN)."""
    env = {
        "PATH": "/usr/bin:/bin",
        "PRECIS_DATABASE_URL": "postgresql://u@h/db",
    }
    proc = _run("precis", "serve", env=env)
    assert "PRECIS_DATABASE_URL not set" not in proc.stderr
    assert proc.returncode == 127  # exec reached; binary absent in test env
