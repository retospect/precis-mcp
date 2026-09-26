"""Artifact-level companion to test_wheel_packages_cover_src.py (gr451360).

That test catches a *declared* packaging mismatch (a package under src/
missing from `[tool.hatch.build.targets.wheel] packages`) by reading
pyproject.toml — cheap and deterministic, but it can only be wrong about the
declaration, never about what actually happens when the wheel is built and
installed. This test runs ``scripts/wheel-smoke``, which builds the real
wheel, installs it into a scratch venv, and imports ``precis_web.app`` (plus
``precis_se`` and ``precis.dispatch``) from a cwd with no repo ``src/`` on
``sys.path`` — the exact condition a worktree test run can never reproduce,
and the one that let the 2026-09-26 outage (missing ``src/precis_surface``)
through every test while it was still on main.

Slow (a real ``uv build`` + a real ``uv pip install`` against PyPI — 10s+),
so it is excluded from ``--fast``/``--impacted`` and from the default ship
gate; it runs pre-deploy (scripts/deploy) and whenever the full local suite
runs slow tests.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX-only: execs the scripts/wheel-smoke bash script",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
WHEEL_SMOKE = REPO_ROOT / "scripts" / "wheel-smoke"

# Substrings of a uv/pip failure that mean "no route to PyPI", not "the wheel
# is broken" — detected from the failure text itself (there is no reliable
# network probe that wouldn't itself be a flaky network call), so the test
# skips instead of failing when the *environment*, not the artifact, is the
# reason the install couldn't complete.
_NETWORK_ERROR_MARKERS = (
    "could not connect",
    "network is unreachable",
    "temporary failure in name resolution",
    "failed to fetch",
    "connection refused",
    "connection reset",
    "timed out",
    "no such host",
    "name or service not known",
    "ssl handshake",
    "dns error",
)


def test_wheel_smoke() -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")

    proc = subprocess.run(
        [str(WHEEL_SMOKE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    if proc.returncode != 0:
        combined = (proc.stdout + proc.stderr).lower()
        if any(marker in combined for marker in _NETWORK_ERROR_MARKERS):
            pytest.skip(
                "scripts/wheel-smoke needs network access to PyPI; "
                f"tail: {(proc.stdout + proc.stderr)[-1000:]}"
            )
        pytest.fail(
            f"scripts/wheel-smoke exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    assert "wheel-smoke ok" in proc.stdout
