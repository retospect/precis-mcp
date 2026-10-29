"""gr530687 — ``scripts/mcp-http-install --apply`` stamps the live install
and refuses to downgrade it.

A stale worktree ran ``--apply`` twice and downgraded the live ensure
script; a copy written in place while ``ensure.sh --recreate`` was running
also crashed that bash (bash reads scripts incrementally). The fixes,
driven here against temp dirs and temp git repos:

- a successful ``--apply`` writes ``<installed>.stamp`` (source commit
  sha, tree path, dirty flag) via the same atomic rename;
- ``--apply`` refuses — naming the stamp's tree and sha — when the
  installed source sha is not an ancestor of the invoking tree's HEAD
  (``git merge-base --is-ancestor``) or when the stamp says the install
  came from a dirty tree; ``--force`` overrides; a missing stamp is a
  first install and is allowed;
- the install itself is a temp file in the target directory + ``mv``, so
  the inode changes and the live path is never written in place.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="mcp-http-install is POSIX-only"
)

ENSURE = "precis-mcp-http-ensure.sh"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.stdout.strip()


def _stage_tree(tmp_path: Path) -> Path:
    """A minimal git repo shaped like a checkout: the script under scripts/
    and its repo-side ensure script under deploy/mcp-http/."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "deploy" / "mcp-http").mkdir(parents=True)
    script = tree / "scripts" / "mcp-http-install"
    shutil.copy2(REPO / "scripts" / "mcp-http-install", script)
    script.chmod(0o755)
    (tree / "deploy" / "mcp-http" / ENSURE).write_text(
        "#!/usr/bin/env bash\n# ensure v1\n", encoding="utf-8"
    )
    _git(tree, "init", "-q", "-b", "main")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "v1")
    return tree


def _env(tmp_path: Path, installed: Path) -> dict[str, str]:
    # Strip ambient PRECIS_* (the dev machine may point at the real
    # install) and give the script temp locations with no canonical
    # main-checkout marker, so the gr515239 gate passes.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRECIS_")}
    env["PRECIS_MCP_ENSURE"] = str(installed)
    env["PRECIS_MCP_HTTP_STATE"] = str(tmp_path / "no-such-state-dir")
    return env


def _run_install(
    tree: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(tree / "scripts" / "mcp-http-install"), *args],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def _repo_copy(tree: Path) -> Path:
    return tree / "deploy" / "mcp-http" / ENSURE


def _commit_bump(tree: Path, version: str) -> str:
    """Change the repo-side ensure copy, commit, return the new HEAD."""
    _repo_copy(tree).write_text(
        f"#!/usr/bin/env bash\n# ensure {version}\n", encoding="utf-8"
    )
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", version)
    return _git(tree, "rev-parse", "HEAD")


def test_first_apply_installs_and_writes_stamp(tmp_path: Path) -> None:
    tree = _stage_tree(tmp_path)
    installed = tmp_path / "installed" / ENSURE
    proc = _run_install(tree, _env(tmp_path, installed), "--apply")
    assert proc.returncode == 0, proc.stderr
    assert installed.read_text(encoding="utf-8") == _repo_copy(tree).read_text(
        encoding="utf-8"
    )
    stamp = installed.parent / (installed.name + ".stamp")
    text = stamp.read_text(encoding="utf-8")
    assert f"sha={_git(tree, 'rev-parse', 'HEAD')}" in text
    assert f"tree={os.path.realpath(tree)}" in text  # root is cd+pwd-resolved
    assert "dirty=false" in text
    # no temp files left behind
    assert list(installed.parent.glob("*.tmp.*")) == []


def test_stale_tree_is_refused(tmp_path: Path) -> None:
    tree = _stage_tree(tmp_path)
    installed = tmp_path / "installed" / ENSURE
    env = _env(tmp_path, installed)
    v1 = _git(tree, "rev-parse", "HEAD")
    v2 = _commit_bump(tree, "v2")
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 0, proc.stderr
    # Rewind the invoking tree to v1: the installed copy now records v2,
    # which is not an ancestor of this tree's HEAD.
    _git(tree, "checkout", "-q", v1)
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 3
    assert "refusing --apply" in proc.stderr
    assert v2 in proc.stderr  # names the stamp's sha
    assert os.path.realpath(tree) in proc.stderr  # names the stamp's tree
    # the live install is untouched
    assert installed.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\n# ensure v2\n"
    )


def test_force_overrides_stale_refusal(tmp_path: Path) -> None:
    tree = _stage_tree(tmp_path)
    installed = tmp_path / "installed" / ENSURE
    env = _env(tmp_path, installed)
    v1 = _git(tree, "rev-parse", "HEAD")
    _commit_bump(tree, "v2")
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 0, proc.stderr
    _git(tree, "checkout", "-q", v1)
    proc = _run_install(tree, env, "--apply", "--force")
    assert proc.returncode == 0, proc.stderr
    assert installed.read_text(encoding="utf-8") == _repo_copy(tree).read_text(
        encoding="utf-8"
    )
    stamp = installed.parent / (installed.name + ".stamp")
    assert f"sha={v1}" in stamp.read_text(encoding="utf-8")


def test_dirty_stamp_is_refused(tmp_path: Path) -> None:
    tree = _stage_tree(tmp_path)
    installed = tmp_path / "installed" / ENSURE
    env = _env(tmp_path, installed)
    # Install from a dirty tree: the content lands, the stamp says so.
    _repo_copy(tree).write_text(
        "#!/usr/bin/env bash\n# ensure v1 dirty edit\n", encoding="utf-8"
    )
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 0, proc.stderr
    stamp = installed.parent / (installed.name + ".stamp")
    assert "dirty=true" in stamp.read_text(encoding="utf-8")
    # Commit that content, advance the tree to v3: ancestry now passes,
    # but the dirty stamp still refuses.
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "v2")
    _commit_bump(tree, "v3")
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 3
    assert "dirty tree" in proc.stderr
    assert os.path.realpath(tree) in proc.stderr
    assert installed.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\n# ensure v1 dirty edit\n"
    )
    proc = _run_install(tree, env, "--apply", "--force")
    assert proc.returncode == 0, proc.stderr
    assert installed.read_text(encoding="utf-8") == _repo_copy(tree).read_text(
        encoding="utf-8"
    )


def test_apply_replaces_installed_by_rename(tmp_path: Path) -> None:
    tree = _stage_tree(tmp_path)
    installed = tmp_path / "installed" / ENSURE
    env = _env(tmp_path, installed)
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 0, proc.stderr
    first_ino = os.stat(installed).st_ino
    _commit_bump(tree, "v2")
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 0, proc.stderr
    second_ino = os.stat(installed).st_ino
    assert first_ino != second_ino  # a new file was renamed over the path
    assert installed.read_text(encoding="utf-8") == _repo_copy(tree).read_text(
        encoding="utf-8"
    )
    assert list(installed.parent.glob("*.tmp.*")) == []


def test_differs_line_names_newer_side(tmp_path: Path) -> None:
    tree = _stage_tree(tmp_path)
    installed = tmp_path / "installed" / ENSURE
    env = _env(tmp_path, installed)
    # Nothing installed yet: the base DIFFERS line has no stamp verdict.
    proc = _run_install(tree, env)
    assert "DIFFERS" in proc.stdout
    v1 = _git(tree, "rev-parse", "HEAD")
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 0, proc.stderr
    # Repo ahead of the install → safe to apply.
    _commit_bump(tree, "v2")
    proc = _run_install(tree, env)
    assert "install is behind" in proc.stdout
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 0, proc.stderr
    # Tree rewound behind the install → do NOT apply from here.
    _git(tree, "checkout", "-q", v1)
    proc = _run_install(tree, env)
    assert "worktree is behind the installed copy" in proc.stdout


def test_success_line_names_short_sha(tmp_path: Path) -> None:
    tree = _stage_tree(tmp_path)
    installed = tmp_path / "installed" / ENSURE
    proc = _run_install(tree, _env(tmp_path, installed), "--apply")
    assert proc.returncode == 0, proc.stderr
    short = hashlib.sha256(_repo_copy(tree).read_bytes()).hexdigest()[:8]
    assert short in proc.stdout
    assert "cut:" not in proc.stderr  # the cut range typo stays fixed


def test_unknown_stamp_sha_refused_with_fetch_hint(tmp_path: Path) -> None:
    tree = _stage_tree(tmp_path)
    installed = tmp_path / "installed" / ENSURE
    env = _env(tmp_path, installed)
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 0, proc.stderr
    _commit_bump(tree, "v2")  # files differ, so the stamp gate is reached
    # A stamp whose sha this clone has never seen (not fetched yet): a
    # distinct refusal from "not an ancestor" — the recovery is git fetch.
    stamp = installed.parent / (installed.name + ".stamp")
    lines = stamp.read_text(encoding="utf-8").splitlines()
    lines[0] = "sha=" + "f" * 40
    stamp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 3
    assert "unknown to this clone" in proc.stderr
    assert "git fetch" in proc.stderr
    assert "f" * 40 in proc.stderr
    assert os.path.realpath(tree) in proc.stderr
    # the live install is untouched
    assert installed.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\n# ensure v1\n"
    )


def test_dirty_flag_scopes_to_ensure_dir(tmp_path: Path) -> None:
    tree = _stage_tree(tmp_path)
    installed = tmp_path / "installed" / ENSURE
    env = _env(tmp_path, installed)
    # An untracked file outside deploy/mcp-http must not mark the stamp.
    (tree / "untracked-elsewhere.txt").write_text("x\n", encoding="utf-8")
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 0, proc.stderr
    stamp = installed.parent / (installed.name + ".stamp")
    assert "dirty=false" in stamp.read_text(encoding="utf-8")
    # …while an uncommitted edit inside deploy/mcp-http does.
    _repo_copy(tree).write_text(
        "#!/usr/bin/env bash\n# ensure v1 dirty edit\n", encoding="utf-8"
    )
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert "dirty=true" in stamp.read_text(encoding="utf-8")


def test_unresolvable_head_refuses_before_installing(tmp_path: Path) -> None:
    tree = _stage_tree(tmp_path)
    installed = tmp_path / "installed" / ENSURE
    env = _env(tmp_path, installed)
    os.rename(tree / ".git", tmp_path / "hidden-git")
    proc = _run_install(tree, env, "--apply")
    assert proc.returncode != 0
    assert "refusing --apply" in proc.stderr
    assert not installed.exists()
    assert not (installed.parent / (installed.name + ".stamp")).exists()
