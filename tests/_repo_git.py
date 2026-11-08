"""Shared ``repo_git`` guard for tests that shell ``git`` at THIS checkout
(gr581556).

``needs_repo_git`` skips where git cannot see the checkout's metadata —
the ship/test gate containers: the warm precis-gate gets ``git archive
HEAD`` (no ``.git`` at all) and the per-run bind fallback mounts a
worktree whose ``.git`` file names a gitdir outside the mount — but
FAILS under ``CI=true``: check.yml runs real checkouts, so an unreadable
gitdir there is a broken runner, and a silent skip would land a blind
spot on a green gate (same contract as ``tests/_node.py``).

A repo the test builds itself — ``git init`` under ``tmp_path``, a stub
``git`` on PATH — needs no guard: the container has the git binary, just
not this checkout's metadata.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

needs_repo_git = pytest.mark.usefixtures("require_repo_git")


def repo_git_visible(root: Path) -> bool:
    """``git rev-parse --git-dir`` answers at ``root`` — the probe every
    fallback in the suite duplicates inline."""
    try:
        return (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "safe.directory=*",
                    "rev-parse",
                    "--git-dir",
                ],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    except OSError:
        return False


@pytest.fixture
def require_repo_git() -> Path:
    """The checkout root; skip where git can't see it, fail under CI."""
    if repo_git_visible(_REPO_ROOT):
        return _REPO_ROOT
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(
            "git cannot see this checkout under CI=true: check.yml runs a "
            "real checkout — an unreadable gitdir is a broken runner (gr581556)"
        )
    pytest.skip("git cannot see this checkout (the gate container has no .git)")
