"""``scripts/hooks/heal-primary-branch.sh``: the SessionStart hook that puts a
drifted primary checkout back on main, and must leave a factory attempt
folder alone.

An attempt folder is a clone of its own, so it is its own primary checkout,
and freshly cut on ``slice/<todo>`` it is merged and clean. The hook healed
it: every session on the disposable-workspace engine started with its branch
deleted. Host-only: temp repositories, a stub ``scripts/inflight``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: a bash hook"
)

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "hooks" / "heal-primary-branch.sh"

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_ENV},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def _checkout(path: Path, branch: str) -> Path:
    """A repository at ``path`` with the hook and a stub ``inflight`` that
    reports no live session, committed on main, then ``branch`` checked out
    at main's tip with a clean tree."""
    (path / "scripts" / "hooks").mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    shutil.copy(HOOK, path / "scripts" / "hooks" / HOOK.name)
    stub = path / "scripts" / "inflight"
    stub.write_text("#!/bin/sh\necho '{\"worktrees\": []}'\n", encoding="utf-8")
    stub.chmod(0o755)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")
    _git(path, "checkout", "-q", "-b", branch)
    return path


def _run(path: Path, **env: str) -> str:
    base = {k: v for k, v in os.environ.items() if not k.startswith("PRECIS_")}
    proc = subprocess.run(
        ["bash", str(path / "scripts" / "hooks" / HOOK.name)],
        cwd=path,
        env={**base, **_ENV, **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _branch(path: Path) -> str:
    return _git(path, "symbolic-ref", "--short", "HEAD")


def test_a_drifted_primary_is_put_back_on_main(tmp_path: Path) -> None:
    """The hook's own job, and the proof that the cases below reach its end."""
    repo = _checkout(tmp_path / "precis-mcp", "feature")
    out = _run(repo)
    assert "checked out main, deleted feature" in out
    assert _branch(repo) == "main"
    assert _git(repo, "branch", "--list", "feature") == ""


def test_an_attempt_folder_keeps_its_branch(tmp_path: Path) -> None:
    repo = _checkout(tmp_path / "slice-td741766-j746346", "slice/741766")
    assert _run(repo) == ""
    assert _branch(repo) == "slice/741766"


def test_a_factory_session_keeps_its_branch_whatever_the_folder_is_called(
    tmp_path: Path,
) -> None:
    repo = _checkout(tmp_path / "somewhere", "slice/12")
    assert _run(repo, PRECIS_SLICE_ID="td12") == ""
    assert _branch(repo) == "slice/12"


def test_the_escape_hatch_still_works(tmp_path: Path) -> None:
    repo = _checkout(tmp_path / "precis-mcp", "feature")
    assert _run(repo, PRECIS_NO_HEAL_PRIMARY="1") == ""
    assert _branch(repo) == "feature"
