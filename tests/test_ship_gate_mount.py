"""``gate_sh`` bind-mounts the tree's .git read-only only when it is a directory.

A standalone clone (a factory attempt) has a real ``.git`` directory, so the
gate container needs it at /app/.git; a linked worktree's ``.git`` is a file
pointing outside the tree, and mounting it would be wrong. The function is
extracted from scripts/ship with ``compose`` replaced by a stub that prints
its arguments, so no container starts.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

SHIP = Path(__file__).resolve().parent.parent / "scripts" / "ship"


def _gate_sh_source() -> str:
    text = SHIP.read_text(encoding="utf-8")
    m = re.search(r"^gate_sh\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "gate_sh not found in scripts/ship"
    return m.group(0)


def _gate_args(tree: Path) -> list[str]:
    script = (
        "set -euo pipefail\n"
        'compose() { printf "%s\\n" "$@"; }\n'
        "TEST_DB_ENV=()\n"
        "GATE_MODE=cold\n"
        f'WORKTREE="{tree}"\n' + _gate_sh_source() + 'gate_sh "true"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return out.stdout.splitlines()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def test_standalone_clone_mounts_its_git_dir_read_only(tmp_path: Path) -> None:
    host = tmp_path / "host"
    _git(tmp_path, "init", "-q", str(host))
    _git(tmp_path, "-C", str(host), "commit", "-q", "--allow-empty", "-m", "init")
    clone = host / ".claude" / "worktrees" / "slice-td7-j9"
    _git(tmp_path, "clone", "-q", str(host), str(clone))
    assert (clone / ".git").is_dir()
    args = _gate_args(clone)
    assert "-v" in args
    assert f"{clone}/.git:/app/.git:ro" in args
    assert f"{clone}:/app" in args


def test_linked_worktree_does_not_mount_a_git_file(tmp_path: Path) -> None:
    host = tmp_path / "host"
    _git(tmp_path, "init", "-q", str(host))
    _git(tmp_path, "-C", str(host), "commit", "-q", "--allow-empty", "-m", "init")
    linked = tmp_path / "linked"
    _git(tmp_path, "-C", str(host), "worktree", "add", "-q", "--detach", str(linked))
    assert (linked / ".git").is_file()
    args = _gate_args(linked)
    assert f"{linked}:/app" in args
    assert not [a for a in args if a.endswith(":/app/.git:ro")]
