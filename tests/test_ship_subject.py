"""``scripts/lib/ship-subject.sh``: squash-subject resolution + lint.

The pure functions are driven in a throwaway git repo (no gate, no network);
the last test runs the REAL ``scripts/ship`` far enough to prove a WIP-only
branch dies before any lint/gate work.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib" / "ship-subject.sh"
SHIP = REPO / "scripts" / "ship"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    ).stdout


def _repo(tmp_path: Path, subjects: list[str]) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    for s in subjects:
        _git(repo, "commit", "-q", "--allow-empty", "-m", s)
    return repo


def _resolve(repo: Path, msg: str = "", **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}"; ship_resolve_msg "$1" origin/main', "_", msg],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={k: v for k, v in os.environ.items() if k != "PRECIS_SHIP_SUBJECT_LINT"}
        | env,
    )


def test_single_commit_message_reused_without_trailers(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        [
            "fix(ship): derive subject\n\nWhy: placeholders are useless.\n\n"
            "Co-Authored-By: X <x@example.invalid>\nClaude-Session: https://x"
        ],
    )
    r = _resolve(repo)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "fix(ship): derive subject\n\nWhy: placeholders are useless.\n"


def test_several_commits_newest_subject_plus_also(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        [
            "feat(a): first",
            "wip(branch): end-of-session snapshot",
            "fix(b): second",
            "docs(c): third",
        ],
    )
    r = _resolve(repo)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "docs(c): third\n\nAlso:\n- fix(b): second\n- feat(a): first\n"


def test_only_wip_dies(tmp_path: Path) -> None:
    repo = _repo(tmp_path, ["wip(branch): end-of-session snapshot"])
    r = _resolve(repo)
    assert r.returncode == 1
    assert (
        "no commit message: pass -m 'type(scope): what changed' or commit your "
        "work with a real subject before shipping" in r.stderr
    )


def test_no_commits_dies(tmp_path: Path) -> None:
    assert _resolve(_repo(tmp_path, [])).returncode == 1


def test_bad_subject_via_m_dies(tmp_path: Path) -> None:
    repo = _repo(tmp_path, [])
    for bad in (
        "ship(branch): squash-merge to main",
        "wip(x): snapshot",
        "fixed stuff",
        "feat(Bad Scope): x",
        "fix: " + "x" * 80,
    ):
        r = _resolve(repo, bad)
        assert r.returncode == 1, bad
        assert "PRECIS_SHIP_SUBJECT_LINT=0" in r.stderr


def test_good_subject_via_m_passes(tmp_path: Path) -> None:
    r = _resolve(_repo(tmp_path, []), "fix(ship)!: refuse placeholders")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "fix(ship)!: refuse placeholders\n"


def test_lint_off_via_env(tmp_path: Path) -> None:
    r = _resolve(_repo(tmp_path, []), "whatever", PRECIS_SHIP_SUBJECT_LINT="0")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "whatever\n"


def test_ship_dies_on_wip_only_branch_before_the_gate(tmp_path: Path) -> None:
    """The real script refuses at the WIP step: nothing after it ran."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "w"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "checkout", "-q", "-b", "feat-x")
    scripts = repo / "scripts" / "lib"
    scripts.mkdir(parents=True)
    (repo / "scripts" / "ship").write_bytes(SHIP.read_bytes())
    (scripts / "ship-subject.sh").write_bytes(LIB.read_bytes())
    for lib in (REPO / "scripts" / "lib").glob("*.sh"):
        (scripts / lib.name).write_bytes(lib.read_bytes())
    (repo / ".gitignore").write_text(".claude/\n", encoding="utf-8")
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    r = subprocess.run(
        ["bash", "scripts/ship", "--quick"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no commit message: pass -m" in r.stderr
    assert "syncing origin/main" not in r.stdout
