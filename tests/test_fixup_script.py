"""scripts/fixup: fold staged changes into an earlier commit, non-interactively
(gr514887).

Runs real git repos under tmp_path — the behaviour under test IS the
commit/--fixup + GIT_SEQUENCE_EDITOR=: autosquash-rebase dance.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: runs the bash script"
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fixup"


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A three-commit repo: A (file a), B (file b), C (file c)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    for name in ("a", "b", "c"):
        (repo / f"{name}.txt").write_text(f"{name} v1\n", encoding="utf-8")
        _git(repo, "add", f"{name}.txt")
        _git(repo, "commit", "-m", f"commit {name}")
    return repo


def _fixup(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def _log_subjects(repo: Path) -> list[str]:
    return _git(repo, "log", "--format=%s").splitlines()


def _commit_sha(repo: Path, subject: str) -> str:
    return _git(repo, "rev-list", "-1", "--all", f"--grep=^{subject}$", "HEAD")


def test_folds_the_staged_fix_into_the_target(repo: Path) -> None:
    b = _commit_sha(repo, "commit b")
    (repo / "b.txt").write_text("b v2\n", encoding="utf-8")
    _git(repo, "add", "b.txt")

    r = _fixup(repo, b)
    assert r.returncode == 0, r.stderr
    subjects = _log_subjects(repo)
    # No fixup! marker left, still three commits, history order preserved.
    assert subjects == ["commit c", "commit b", "commit a"]
    # The folded content is there and C is untouched.
    assert (repo / "b.txt").read_text(encoding="utf-8") == "b v2\n"
    assert "fixup!" not in _git(repo, "log", "--format=%s")


def test_fixup_into_head(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "c.txt").write_text("c v2\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    r = _fixup(repo, head)
    assert r.returncode == 0, r.stderr
    assert _log_subjects(repo) == ["commit c", "commit b", "commit a"]
    assert (repo / "c.txt").read_text(encoding="utf-8") == "c v2\n"


def test_fixup_into_the_root_commit(repo: Path) -> None:
    a = _commit_sha(repo, "commit a")
    (repo / "a.txt").write_text("a v2\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    r = _fixup(repo, a)
    assert r.returncode == 0, r.stderr
    assert _log_subjects(repo) == ["commit c", "commit b", "commit a"]
    assert (repo / "a.txt").read_text(encoding="utf-8") == "a v2\n"


def test_carries_unstaged_changes_across(repo: Path) -> None:
    """--autostash keeps an unrelated dirty file out of the fold."""
    b = _commit_sha(repo, "commit b")
    (repo / "b.txt").write_text("b v2\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    (repo / "c.txt").write_text("c scratch\n", encoding="utf-8")  # unstaged
    r = _fixup(repo, b)
    assert r.returncode == 0, r.stderr
    assert (repo / "c.txt").read_text(encoding="utf-8") == "c scratch\n"


def test_nothing_staged_refuses(repo: Path) -> None:
    b = _commit_sha(repo, "commit b")
    (repo / "b.txt").write_text("b v2\n", encoding="utf-8")  # not staged
    r = _fixup(repo, b)
    assert r.returncode == 2
    assert "nothing staged" in r.stderr
    assert _log_subjects(repo) == ["commit c", "commit b", "commit a"]


def test_non_ancestor_refuses(repo: Path) -> None:
    other = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side", "HEAD~1")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-m", "side commit")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    (repo / "b.txt").write_text("b v2\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    r = _fixup(repo, side)
    assert r.returncode == 3
    assert "not an ancestor" in r.stderr
    assert other == _git(repo, "rev-parse", "HEAD")


def test_bad_commit_refuses(repo: Path) -> None:
    r = _fixup(repo, "definitely-not-a-commit")
    assert r.returncode == 1
    assert "not a commit" in r.stderr


def test_usage(repo: Path) -> None:
    r = _fixup(repo)
    assert r.returncode == 1
    assert "usage" in r.stderr
