"""Regression tests for docs/backlog/inflight-sessionless-branches.md:
scripts/inflight (and therefore scripts/reap-worktrees and the
migration-collision advisory) used to iterate `git worktree list
--porcelain` only, so a local branch whose worktree was removed but whose
branch survived with unmerged commits was invisible everywhere — no
session, no purpose line, no reap advisory, and its migrations/*.sql never
entered the collision scan either.

Exercises the REAL `scripts/inflight`, `scripts/reap-worktrees`, and
`scripts/migration-check` (copied byte-for-byte into a throwaway repo --
never reimplemented here), mirroring tests/test_worktree_lock_reap.py's
technique but without that file's process-tree/lock machinery -- a
branchless branch has no worktree, so no lock is ever possible.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the shipped bash scripts are POSIX-only",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INFLIGHT_SRC = REPO_ROOT / "scripts" / "inflight"
REAP_SRC = REPO_ROOT / "scripts" / "reap-worktrees"
MIGRATION_CHECK_SRC = REPO_ROOT / "scripts" / "migration-check"


def _test_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PRECIS_NO_AUTOREAP", None)
    env.pop("PRECIS_NO_CI_REF_REAP", None)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    return env


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd, cwd=str(cwd), env=_test_env(), capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"cmd failed: {cmd}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd)


def _entry_for(json_blob: str, branch: str) -> dict:
    data = json.loads(json_blob)
    for wt in data["worktrees"]:
        if wt["branch"] == branch:
            return wt
    raise AssertionError(f"{branch} not found in inflight --json output: {json_blob}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo with `scripts/inflight` and `scripts/reap-worktrees`
    staged (no worktrees at all -- just the primary checkout on `main`),
    ready for a caller to add branchless branches to.
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")

    scripts_dir = primary / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INFLIGHT_SRC, scripts_dir / "inflight")
    shutil.copy2(REAP_SRC, scripts_dir / "reap-worktrees")
    (scripts_dir / "inflight").chmod(0o755)
    (scripts_dir / "reap-worktrees").chmod(0o755)

    (primary / "README.md").write_text("root\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")
    return primary


def _add_unmerged_branch(primary: Path, branch: str) -> None:
    """Create `branch` off main's current tip, checked out nowhere (no
    `git worktree add` at all), carrying one unmerged commit."""
    _git(primary, "branch", branch)
    worktree = primary.parent / f"scratch-{branch}"
    _git(primary, "worktree", "add", "-q", str(worktree), branch)
    (worktree / "feature.txt").write_text("feature work\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", f"{branch} work")
    # Remove the worktree but keep the branch -- exactly the reported shape:
    # a branch whose worktree was removed while unmerged commits survive.
    _git(primary, "worktree", "remove", str(worktree))


def test_inflight_json_reports_unmerged_branchless_branch_with_worktree_null(
    repo: Path,
) -> None:
    _add_unmerged_branch(repo, "feat/stray")

    json_out = _run([str(repo / "scripts" / "inflight"), "--json"], repo).stdout
    entry = _entry_for(json_out, "feat/stray")

    assert entry["worktree"] is None, entry
    assert entry["path"] == "", entry
    assert entry["bucket"] == "stranded", entry
    assert entry["verdict"].startswith("↑"), entry
    assert entry["session"] == "—", entry


def test_inflight_json_marks_squash_merged_branchless_branch_safe_remove(
    repo: Path,
) -> None:
    _add_unmerged_branch(repo, "feat/landed")
    _git(repo, "merge", "-q", "--squash", "feat/landed")
    _git(repo, "commit", "-q", "-m", "feat/landed work (squashed)")

    json_out = _run([str(repo / "scripts" / "inflight"), "--json"], repo).stdout
    entry = _entry_for(json_out, "feat/landed")

    assert entry["worktree"] is None, entry
    assert entry["bucket"] == "safe_remove", entry
    assert entry["verdict"].startswith("in-main"), entry


def test_inflight_skips_ci_branches(repo: Path) -> None:
    _add_unmerged_branch(repo, "ci/some-gate-run")

    json_out = _run([str(repo / "scripts" / "inflight"), "--json"], repo).stdout
    data = json.loads(json_out)
    branches = [wt["branch"] for wt in data["worktrees"]]
    assert "ci/some-gate-run" not in branches, data


def test_reap_worktrees_dry_run_lists_squash_merged_branchless_branch_as_safe(
    repo: Path,
) -> None:
    _add_unmerged_branch(repo, "feat/landed")
    _git(repo, "merge", "-q", "--squash", "feat/landed")
    _git(repo, "commit", "-q", "-m", "feat/landed work (squashed)")

    result = subprocess.run(
        ["bash", str(repo / "scripts" / "reap-worktrees"), "--dry-run"],
        cwd=str(repo),
        env=_test_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "feat/landed" in result.stdout, result.stdout

    branches = _git(repo, "branch", "--format=%(refname:short)").stdout
    assert "feat/landed" in branches, "dry-run must never delete"


def test_reap_worktrees_deletes_safe_branchless_branch(repo: Path) -> None:
    _add_unmerged_branch(repo, "feat/landed")
    _git(repo, "merge", "-q", "--squash", "feat/landed")
    _git(repo, "commit", "-q", "-m", "feat/landed work (squashed)")

    result = subprocess.run(
        ["bash", str(repo / "scripts" / "reap-worktrees")],
        cwd=str(repo),
        env=_test_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "deleted branchless branch feat/landed" in result.stdout, result.stdout

    branches = _git(repo, "branch", "--format=%(refname:short)").stdout
    assert "feat/landed" not in branches.splitlines()


def test_reap_worktrees_reports_stranded_branch_without_deleting(repo: Path) -> None:
    _add_unmerged_branch(repo, "feat/stray")

    result = subprocess.run(
        ["bash", str(repo / "scripts" / "reap-worktrees")],
        cwd=str(repo),
        env=_test_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "stranded: feat/stray" in result.stdout, result.stdout

    branches = _git(repo, "branch", "--format=%(refname:short)").stdout
    assert "feat/stray" in branches, "a real unmerged branch must never be deleted"


def test_migration_check_flags_collision_from_branchless_branch(
    tmp_path: Path,
) -> None:
    """The migration-number advisory (scripts/migration-check) is scoped to
    trees the table knows -- a branchless branch's migrations/*.sql must now
    enter the same collision scan, mirroring the observed 2026-09-16 case
    (feat/se-datum-measure-eval colliding on precis_se/migrations/0011).
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "main")

    scripts_dir = primary / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(MIGRATION_CHECK_SRC, scripts_dir / "migration-check")
    (scripts_dir / "migration-check").chmod(0o755)

    migdir = primary / "src" / "precis" / "migrations"
    migdir.mkdir(parents=True)
    (migdir / "0010_base.sql").write_text("select 1;\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "initial")

    _git(primary, "branch", "feat/stray-migration")
    worktree = tmp_path / "scratch-stray"
    _git(primary, "worktree", "add", "-q", str(worktree), "feat/stray-migration")
    (worktree / "src" / "precis" / "migrations" / "0011_stray_thing.sql").write_text(
        "select 2;\n", encoding="utf-8"
    )
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "stray migration")
    _git(primary, "worktree", "remove", str(worktree))

    (migdir / "0011_main_thing.sql").write_text("select 3;\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "main migration, same number")

    result = subprocess.run(
        ["bash", str(scripts_dir / "migration-check")],
        cwd=str(primary),
        env=_test_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout
    assert "0011" in result.stdout, result.stdout
    assert "feat/stray-migration" in result.stdout, result.stdout
