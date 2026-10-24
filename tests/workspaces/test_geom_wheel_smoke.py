"""Artifact-level companion to the geom import-graph allow-list
(gr514057), the sibling of ``tests/test_wheel_smoke.py`` (gr451360) for
the ``precis-geom`` workspace wheel.

``tests/workspaces/test_geom_wheel_imports.py`` walks the shipped
sources with ``ast`` — it can only be wrong about the *sources*, never
about what actually happens when the wheel is built and installed: a
force-include entry that maps nothing, ``hexfold_top_plans.json`` left
out of the wheel, or ``packaging/geom/precis_se/__init__.py`` losing to
the real server init would pass every AST check and fail only inside a
``--network none`` sandbox. This test runs ``scripts/geom-wheel-smoke``,
which builds the wheel, installs it into a scratch venv, asserts the
stub landed, and builds one small ``hexfold_scene`` from a cwd with no
repo ``src/`` on ``sys.path``.

Slow (a real ``uv build`` + a real ``uv pip install`` against PyPI +
~15 s of scene construction), so it is excluded from
``--fast``/``--impacted`` and from the default ship gate; it runs
whenever the full local suite runs slow tests.
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
        reason="POSIX-only: execs the scripts/geom-wheel-smoke bash script",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
WHEEL_SMOKE = REPO_ROOT / "scripts" / "geom-wheel-smoke"

# Substrings of a uv/pip failure that mean "no route to PyPI", not "the
# wheel is broken" — same detection approach as tests/test_wheel_smoke.py,
# so the test skips instead of failing when the *environment*, not the
# artifact, is the reason the install couldn't complete.
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


def test_geom_wheel_smoke() -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")

    proc = subprocess.run(
        [str(WHEEL_SMOKE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
    )
    if proc.returncode != 0:
        combined = (proc.stdout + proc.stderr).lower()
        if any(marker in combined for marker in _NETWORK_ERROR_MARKERS):
            pytest.skip(
                "scripts/geom-wheel-smoke needs network access to PyPI; "
                f"tail: {(proc.stdout + proc.stderr)[-1000:]}"
            )
        pytest.fail(
            f"scripts/geom-wheel-smoke exited {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    assert "geom-wheel-smoke ok" in proc.stdout
