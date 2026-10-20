"""scripts/rebase's quiet log must cope with '/' in branch names (gr499974)."""

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


def test_rebase_on_slash_branch(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    work = tmp_path / "work"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(
        origin,
        "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-q", "--allow-empty", "-m", "init",
    )  # fmt: skip
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(work)], check=True, encoding="utf-8"
    )
    (work / "scripts" / "lib").mkdir(parents=True)
    for rel in ("scripts/rebase", "scripts/lib/quiet-log.sh"):
        (work / rel).write_bytes((REPO / rel).read_bytes())
    _git(work, "checkout", "-q", "-b", "hold/g-X-wip")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    r = subprocess.run(
        ["bash", "scripts/rebase"],
        cwd=work,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "TMPDIR": str(tmp),
        },
        capture_output=True,
        encoding="utf-8",
    )
    out = r.stdout + r.stderr
    assert "REBASE_STATUS: uptodate" in out, out
    assert r.returncode == 0
    assert list(tmp.glob("rebase-hold-g-X-wip-*.log"))
