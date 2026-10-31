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
        cmd,
        cwd=str(cwd),
        env=_test_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
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
    # The branchless purpose is the newest branch-unique commit subject.
    assert entry["purpose"] == "feat/stray work"


def test_inflight_json_purpose_is_the_full_first_line(repo: Path) -> None:
    """``purpose`` in --json carries the whole first line of
    .claude/purpose — the table clips it at 58, but a claim check
    (gr509461) needs the slug wherever it sits in the line."""
    wt = repo.parent / "wt-purpose"
    _git(repo, "worktree", "add", "-q", "-b", "feat/purp", str(wt))
    (wt / ".claude").mkdir()
    long_line = "wt-purpose: building docs/backlog/" + "x" * 80 + ".md"
    (wt / ".claude" / "purpose").write_text(
        long_line + "\nsecond line ignored\n", encoding="utf-8"
    )

    json_out = _run([str(repo / "scripts" / "inflight"), "--json"], repo).stdout
    entry = _entry_for(json_out, "feat/purp")

    assert entry["purpose"] == long_line
    assert len(entry["purpose"]) > 58


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
        encoding="utf-8",
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
        encoding="utf-8",
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
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert "stranded: feat/stray" in result.stdout, result.stdout

    branches = _git(repo, "branch", "--format=%(refname:short)").stdout
    assert "feat/stray" in branches, "a real unmerged branch must never be deleted"


def test_for_hook_modes_pay_no_verdict_calls_for_branchless_branches(
    repo: Path, tmp_path: Path
) -> None:
    """gr541365 — ~110 stranded pin branches each paid `git cherry` (plus a
    merge-base / rev-list / merge-tree / `log -1` on the unmerged path)
    inside every SessionStart scan, three hooks deep. In --for-hook modes a
    branchless branch is only ever a COUNT, so those verdict calls must not
    run at all. A `git` shim on PATH logs argv per call; the full --json run
    at the end MUST still pay them — the verdict is its product, pinned by
    the sibling tests above."""
    _add_unmerged_branch(repo, "feat/stray")

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    git_bin = shutil.which("git")
    assert git_bin is not None
    (shim_dir / "git").write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GIT_ARGV_LOG"\nexec "{git_bin}" "$@"\n',
        encoding="utf-8",
    )
    (shim_dir / "git").chmod(0o755)

    argv_log = tmp_path / "git-argv.log"
    env = _test_env()
    env.update(
        {
            "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
            "GIT_ARGV_LOG": str(argv_log),
        }
    )
    verdict_subcmds = {"cherry", "merge-tree", "merge-base", "rev-list", "log"}

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        argv_log.write_text("", encoding="utf-8")
        result = subprocess.run(
            ["bash", *argv],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
        return result

    for argv in (
        ("scripts/inflight", "--for-hook"),
        ("scripts/inflight", "--json", "--skip-branch-probes"),
        ("scripts/reap-worktrees", "--for-hook"),
    ):
        result = run(*argv)
        calls = argv_log.read_text(encoding="utf-8").splitlines()
        bad = [c for c in calls if verdict_subcmds & set(c.split())]
        assert not bad, f"{argv} paid verdict calls for a count: {bad}"
        if argv[0].endswith("reap-worktrees"):
            # --for-hook prints one count, never a per-branch roster.
            assert "1 stranded worktree-less branch" in result.stdout, result.stdout
            assert "stranded: feat/stray" not in result.stdout, result.stdout

    out = run("scripts/inflight", "--for-hook").stdout
    assert "1 worktree-less branch" in out, out

    # Contrast: full --json still computes the real verdict (cherry + the
    # merge-base/rev-list chain a non-ancestor head pays).
    run("scripts/inflight", "--json")
    calls = argv_log.read_text(encoding="utf-8").splitlines()
    assert any("cherry" in c.split() for c in calls), calls


def _primary_with_migrations(tmp_path: Path) -> Path:
    """A primary checkout on `main` with `scripts/migration-check` staged
    and one migration (0010_base.sql) committed."""
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
    return primary


def _run_migration_check(
    primary: Path, *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = _test_env()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(primary / "scripts" / "migration-check"), *args],
        cwd=str(primary),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _branchless_migration(primary: Path, branch: str, filename: str) -> None:
    """Commit `filename` under src/precis/migrations on `branch`, then drop
    the worktree so only the branchless branch carries it."""
    _git(primary, "branch", branch)
    worktree = primary.parent / f"scratch-{branch.replace('/', '-')}"
    _git(primary, "worktree", "add", "-q", str(worktree), branch)
    (worktree / "src" / "precis" / "migrations" / filename).write_text(
        "select 2;\n", encoding="utf-8"
    )
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", f"{branch} migration")
    _git(primary, "worktree", "remove", str(worktree))


def _push_origin_migration(primary: Path, tmp_path: Path, filename: str) -> None:
    """Stand up a bare `origin`, land `filename` on it via a second clone,
    then fetch in `primary` — leaving primary's WORKTREE without the file
    while refs/remotes/origin/main knows it."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(primary, "remote", "add", "origin", str(origin))
    _git(primary, "push", "-q", "-u", "origin", "main")

    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    (other / "src" / "precis" / "migrations" / filename).write_text(
        "select 9;\n", encoding="utf-8"
    )
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", f"{filename} upstream")
    _git(other, "push", "-q", "origin", "main")
    _git(primary, "fetch", "-q", "origin")


def test_migration_check_flags_collision_from_branchless_branch(
    tmp_path: Path,
) -> None:
    """The migration-number advisory (scripts/migration-check) is scoped to
    trees the table knows -- a branchless branch's migrations/*.sql must now
    enter the same collision scan, mirroring the observed 2026-09-16 case
    (feat/se-datum-measure-eval colliding on precis_se/migrations/0011).
    """
    primary = _primary_with_migrations(tmp_path)
    _branchless_migration(primary, "feat/stray-migration", "0011_stray_thing.sql")

    migdir = primary / "src" / "precis" / "migrations"
    (migdir / "0011_main_thing.sql").write_text("select 3;\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "main migration, same number")

    result = _run_migration_check(primary)
    assert result.returncode == 1, result.stdout
    assert "0011" in result.stdout, result.stdout
    assert "feat/stray-migration" in result.stdout, result.stdout


def test_migration_check_flags_number_already_taken_on_origin_main(
    tmp_path: Path,
) -> None:
    """gr514201: a number landed on origin/main must collide even when the
    primary checkout's working tree lacks the file (a ship's update-ref
    fallback, or a fetch newer than the last local merge)."""
    primary = _primary_with_migrations(tmp_path)
    _push_origin_migration(primary, tmp_path, "0011_landed_elsewhere.sql")
    assert not (
        primary / "src" / "precis" / "migrations" / "0011_landed_elsewhere.sql"
    ).exists()

    _branchless_migration(primary, "feat/stale-mint", "0011_stale_claim.sql")

    result = _run_migration_check(primary)
    assert result.returncode == 1, result.stdout
    assert "0011" in result.stdout, result.stdout
    assert "0011_landed_elsewhere.sql" in result.stdout, result.stdout
    assert "feat/stale-mint" in result.stdout, result.stdout


def test_migration_check_taken_lists_claims_and_next_free(tmp_path: Path) -> None:
    """gr514201: `--taken` is the builder-brief view — it must count
    origin/main in main's highest and list unshipped sibling claims."""
    primary = _primary_with_migrations(tmp_path)
    _push_origin_migration(primary, tmp_path, "0011_landed_elsewhere.sql")
    _branchless_migration(primary, "feat/claimed", "0012_sibling_claim.sql")

    result = _run_migration_check(primary, "--taken")
    assert result.returncode == 0, result.stdout
    assert "highest number on main: 0011" in result.stdout, result.stdout
    assert "next free number: 0013" in result.stdout, result.stdout
    assert "0012_sibling_claim.sql" in result.stdout, result.stdout
    assert "feat/claimed" in result.stdout, result.stdout


def test_migration_check_scans_worktree_outside_dot_claude(tmp_path: Path) -> None:
    """A worktree registered with `git worktree add` at a path OUTSIDE
    .claude/worktrees/ was invisible to the old filesystem glob: its
    committed migrations surfaced only via the branchless ls-tree pass and
    its UNCOMMITTED ones never. `git worktree list --porcelain` sees it.
    """
    primary = _primary_with_migrations(tmp_path)

    migdir = primary / "src" / "precis" / "migrations"
    (migdir / "0011_main_thing.sql").write_text("select 3;\n", encoding="utf-8")
    _git(primary, "add", "-A")
    _git(primary, "commit", "-q", "-m", "main migration")

    outside = tmp_path / "outside-tree"
    _git(primary, "worktree", "add", "-q", "-b", "feat/outside", str(outside))
    # UNCOMMITTED — only a disk walk of the registered path can see this.
    (outside / "src" / "precis" / "migrations" / "0011_outside_claim.sql").write_text(
        "select 4;\n", encoding="utf-8"
    )

    result = _run_migration_check(primary)
    assert result.returncode == 1, result.stdout
    assert "0011" in result.stdout, result.stdout
    assert "0011_outside_claim.sql" in result.stdout, result.stdout
    assert "outside-tree" in result.stdout, result.stdout


def test_migration_check_git_calls_do_not_scale_with_worktrees(tmp_path: Path) -> None:
    """The write-time hook gives this script ~15s; the old shape spawned
    `git -C symbolic-ref` per tree plus `basename|grep` per migration file
    (~30k spawns at 65 trees, ~90s measured). One porcelain call must now
    enumerate every tree — git calls scale with branchless BRANCHES only.
    A `git` shim on PATH counts every invocation the script makes.
    """
    primary = _primary_with_migrations(tmp_path)
    for i in range(6):
        _git(
            primary,
            "worktree",
            "add",
            "-q",
            "-b",
            f"feat/live-{i}",
            str(primary / ".claude" / "worktrees" / f"live-{i}"),
        )
    for i in range(3):
        _branchless_migration(primary, f"feat/stale-{i}", f"001{i + 1}_stale_{i}.sql")

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    git_bin = shutil.which("git")
    assert git_bin is not None
    (shim_dir / "git").write_text(
        f'#!/bin/sh\necho x >> "$GIT_CALL_LOG"\nexec "{git_bin}" "$@"\n',
        encoding="utf-8",
    )
    (shim_dir / "git").chmod(0o755)

    log = tmp_path / "git-calls.log"
    result = _run_migration_check(
        primary,
        env_extra={
            "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
            "GIT_CALL_LOG": str(log),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    n_calls = len(log.read_text(encoding="utf-8").splitlines())
    # 1 rev-parse + 1 symbolic-ref + 1 worktree-list + 1 for-each-ref +
    # 3 branch ls-trees + 2 ref verifies + 1 ref ls-tree (no origin here).
    # The old shape adds ~2 spawns per tree per migration FILE plus a
    # symbolic-ref per tree — ~20+ on this rig, ~30k on the real fleet.
    assert n_calls <= 13, f"{n_calls} git calls — per-tree spawning is back"
