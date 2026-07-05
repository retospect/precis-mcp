"""Unit tests for the fixer quick-gate auto-fix (ADR 0048).

A full opus-built slice was discarded when the quick gate ran
``ruff format --check`` on unformatted build output and reported NEEDS_YOU —
stricter than ``scripts/ship``, which auto-fixes ruff *before* checking.
``_autofix_lint`` now mirrors ship (``ruff check --fix`` + ``ruff format``) and
commits the mechanical fixes via ``_commit_if_dirty`` so a formatting nit never
sinks a build. This covers the commit-if-dirty floor; the ruff invocation
itself mirrors ship and is exercised live by the fixer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from precis.fixer.tick import _commit_if_dirty


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)


def test_commit_if_dirty_commits_and_reports(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "new.py").write_text("x = 1\n")

    assert _commit_if_dirty(tmp_path, "style: ruff autofix (fixer)") is True

    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True
    )
    assert porcelain.stdout.strip() == ""  # working tree clean after commit
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True
    )
    assert "style: ruff autofix (fixer)" in log.stdout


def test_commit_if_dirty_noop_when_clean(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert _commit_if_dirty(tmp_path, "style: ruff autofix (fixer)") is False
