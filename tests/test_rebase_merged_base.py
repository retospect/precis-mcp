"""scripts/rebase warns when the requested base already landed (gr518102).

A base that is already an ancestor of origin/main is a stale stack
target: the rebase succeeds yet leaves the branch behind trunk. The
script warns and suggests origin/main; the default base never warns.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: execs the bash script scripts/rebase"
)

REPO = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, encoding="utf-8"
    )


def _repo(tmp_path: Path) -> Path:
    """Clone whose origin/slice sits at the first commit, origin/main one ahead."""
    origin = tmp_path / "origin"
    work = tmp_path / "work"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(
        origin,
        "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-q", "--allow-empty", "-m", "a",
    )  # fmt: skip
    _git(origin, "branch", "slice")
    _git(
        origin,
        "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-q", "--allow-empty", "-m", "b",
    )  # fmt: skip
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(work)], check=True, encoding="utf-8"
    )
    (work / "scripts" / "lib").mkdir(parents=True)
    for rel in ("scripts/rebase", "scripts/lib/quiet-log.sh"):
        (work / rel).write_bytes((REPO / rel).read_bytes())
    _git(work, "checkout", "-q", "-b", "feat", "origin/slice")
    return work


def _run(work: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", "scripts/rebase", *args],
        cwd=work,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "TMPDIR": str(tmp),
        },
        capture_output=True,
        encoding="utf-8",
    )


def test_merged_base_warns(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    r = _run(work, tmp_path, "origin/slice")
    out = r.stdout + r.stderr
    assert "already in origin/main" in out, out
    assert "REBASE_STATUS: uptodate" in out, out
    assert r.returncode == 0


def test_default_base_no_warning(tmp_path: Path) -> None:
    work = _repo(tmp_path)
    r = _run(work, tmp_path)
    out = r.stdout + r.stderr
    assert "already in origin/main" not in out, out
    assert r.returncode == 0, out
