"""scripts/branch-audit — the landed/unlanded/partial verdicts (gr497896).

A bare ``origin.git`` + a ``primary`` clone give a real ``origin/main`` to
audit against; the script itself runs via the interpreter (no network, no
fetch — it reads origin/main as cloned).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX shell + git plumbing"
)

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "branch-audit"


def _g(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _commit(cwd: Path, files: dict[str, str], msg: str) -> str:
    for rel, body in files.items():
        p = cwd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    _g(cwd, "add", "-A")
    _g(cwd, "commit", "-q", "-m", msg)
    return _g(cwd, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """origin.git + a primary clone on main with one base commit. A local
    user.name/email (not env or global config) so the container's identity-
    free git commits and rebases."""
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(tmp_path / "origin.git")],
        check=True,
    )
    primary = tmp_path / "primary"
    _g(tmp_path, "init", "-q", "-b", "main", "primary")
    _g(primary, "remote", "add", "origin", str(tmp_path / "origin.git"))
    _g(primary, "config", "user.email", "t@example.invalid")
    _g(primary, "config", "user.name", "t")
    _commit(primary, {"base.txt": "base\n"}, "base")
    _g(primary, "push", "-q", "origin", "main")
    return primary


def _branch(repo: Path, name: str) -> str:
    _g(repo, "checkout", "-q", "-b", name)
    return name


def _back_to_main(repo: Path) -> None:
    _g(repo, "checkout", "-q", "main")


def _audit(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _land_on_main(repo: Path, files: dict[str, str], msg: str) -> None:
    """A squash-land: same end state, a different commit on main."""
    _back_to_main(repo)
    _commit(repo, files, msg)
    _g(repo, "push", "-q", "origin", "main")


def test_unlanded_branch(repo: Path) -> None:
    _branch(repo, "feat-new")
    _commit(repo, {"new.txt": "work\n"}, "feat(x): new work")
    _back_to_main(repo)
    out = _audit(repo, "feat-new")
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("feat-new: unlanded  1 commit"), out.stdout


def test_landed_by_tree_equality(repo: Path) -> None:
    """The g-N-retarget shape: landed under different shas; tree diff empty."""
    _branch(repo, "feat-sq")
    _commit(repo, {"a.txt": "a\n"}, "fix(a): part one")
    _commit(repo, {"b.txt": "b\n"}, "fix(b): part two")
    _land_on_main(repo, {"a.txt": "a\n", "b.txt": "b\n"}, "fix: squashed land")
    out = _audit(repo, "feat-sq")
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("feat-sq: landed  2 commits"), out.stdout


def test_subject_match_alone_is_not_landed(repo: Path) -> None:
    """Every branch commit's subject appears on main, but the content
    differs — landed needs a content signal, so this is partial at most
    (the fleet-wave close refusal uses the same verdict)."""
    _branch(repo, "feat-subj")
    _commit(repo, {"s.txt": "branch side\n"}, "fix(x): same subject")
    _land_on_main(repo, {"other.txt": "main side\n"}, "fix(x): same subject")
    out = _audit(repo, "feat-subj")
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("feat-subj: partial"), out.stdout
    assert "landed" not in out.stdout.split()[1], out.stdout


def test_partial_when_one_commit_landed(repo: Path) -> None:
    _branch(repo, "feat-part")
    sha_a = _commit(repo, {"a.txt": "a\n"}, "fix(a): first")
    _commit(repo, {"b.txt": "b\n"}, "fix(b): second")
    # Only the first commit's patch lands on main.
    _back_to_main(repo)
    _g(repo, "cherry-pick", "-n", sha_a)
    _g(repo, "commit", "-q", "-m", "fix(a): landed")
    _g(repo, "push", "-q", "origin", "main")
    out = _audit(repo, "feat-part")
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("feat-part: partial  2 commits"), out.stdout
    assert "(1/2 on origin/main)" in out.stdout, out.stdout


def test_empty_commit_is_unlanded(repo: Path) -> None:
    """An --allow-empty commit has an empty diff against everything — it
    proves nothing and must not read as landed (fleet-wave close's refusal
    is the same test)."""
    _branch(repo, "feat-empty")
    _g(repo, "commit", "-q", "--allow-empty", "-m", "wip")
    _back_to_main(repo)
    out = _audit(repo, "feat-empty")
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("feat-empty: unlanded"), out.stdout


def test_no_args_lists_only_worktreeless_branches(repo: Path) -> None:
    _branch(repo, "feat-list")
    _commit(repo, {"l.txt": "x\n"}, "feat: listed")
    _back_to_main(repo)
    # feat-list has no worktree now; wt-held one does.
    _g(repo, "worktree", "add", "-q", "-b", "feat-held", str(repo / "wt-held"))
    out = _audit(repo)
    assert out.returncode == 0, out.stderr
    assert "feat-list" in out.stdout
    assert "feat-held" not in out.stdout
    assert "main" not in out.stdout


def test_unknown_ref_is_an_error_line(repo: Path) -> None:
    out = _audit(repo, "no-such-branch")
    assert out.returncode == 1
    assert "no-such-branch: error" in out.stdout
