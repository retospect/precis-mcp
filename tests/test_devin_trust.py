"""scripts/lib/devin_trust.py — the trusted_workspaces.json writer.

Every run gets a throwaway HOME (and, where exercised, a
``DEVIN_TRUSTED_WORKSPACES`` or ``--file`` pointing inside it), so the
real ``~/.local/share/devin/cli/trusted_workspaces.json`` is never
touched — even if the helper's file resolution regressed. The launch
tests run the real scripts/devin-tree against a fake git and a fake tmux
first on PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# test_posix_only_guards.py requires this on a module that spawns bash and
# execs a shell script (the launch tests do both).
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: flock and fake bins"
)

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "lib" / "devin_trust.py"
DEVIN_TREE = REPO / "scripts" / "devin-tree"

FAKE_GIT = """#!/usr/bin/env bash
while [ "${1:-}" = "-C" ]; do shift 2; done
case "${1:-}" in
  rev-parse) echo "$FAKE_ROOT/.git" ;;
  fetch) exit 0 ;;
  show-ref) exit 1 ;;
  worktree)
    case "${2:-}" in
      add)
        for a in "$@"; do
          case "$a" in */worktrees/*) mkdir -p "$a/.claude" ;; esac
        done ;;
      list) printf 'worktree %s\\nlocked pid 424242\\n\\n' "$FAKE_WT" ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
"""

FAKE_TMUX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_TMUX_LOG"
"""


def run(
    tmp_path: Path, *argv: str, env_file: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    if env_file is not None:
        env["DEVIN_TRUSTED_WORKSPACES"] = str(env_file)
    return subprocess.run(
        [sys.executable, str(HELPER), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def worktrees(tmp_path: Path) -> Path:
    """A real <repo>/.claude/worktrees under a fake git root."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    d = repo / ".claude" / "worktrees"
    d.mkdir(parents=True)
    return d


def trusted_paths(file: Path) -> list[str]:
    return json.loads(file.read_text(encoding="utf-8"))["trusted_paths"]


def test_adds_entry_and_creates_missing_file(tmp_path: Path) -> None:
    under = worktrees(tmp_path)
    file = tmp_path / "deep" / "trusted_workspaces.json"
    r = run(tmp_path, "--file", str(file), str(under))
    assert r.returncode == 0, r.stderr
    assert trusted_paths(file) == [os.path.realpath(under)]


def test_idempotent_second_run_changes_nothing(tmp_path: Path) -> None:
    under = worktrees(tmp_path)
    file = tmp_path / "trusted_workspaces.json"
    assert run(tmp_path, "--file", str(file), str(under)).returncode == 0
    first = file.read_bytes()
    r = run(tmp_path, "--file", str(file), str(under))
    assert r.returncode == 0, r.stderr
    assert "already trusted" in r.stdout
    assert file.read_bytes() == first


def test_preserves_existing_entries_and_keys(tmp_path: Path) -> None:
    under = worktrees(tmp_path)
    file = tmp_path / "trusted_workspaces.json"
    file.write_text(
        json.dumps(
            {"other_key": {"nested": [1, 2]}, "trusted_paths": ["/existing/dir"]}
        ),
        encoding="utf-8",
    )
    r = run(tmp_path, "--file", str(file), str(under))
    assert r.returncode == 0, r.stderr
    data = json.loads(file.read_text(encoding="utf-8"))
    assert data["other_key"] == {"nested": [1, 2]}
    assert data["trusted_paths"] == ["/existing/dir", os.path.realpath(under)]


def test_refuses_malformed_json_without_overwriting(tmp_path: Path) -> None:
    under = worktrees(tmp_path)
    file = tmp_path / "trusted_workspaces.json"
    file.write_bytes(b"not json{")
    r = run(tmp_path, "--file", str(file), str(under))
    assert r.returncode != 0
    assert "not updating" in r.stderr
    assert file.read_bytes() == b"not json{"


def test_refuses_path_outside_worktrees(tmp_path: Path) -> None:
    under = worktrees(tmp_path)
    file = tmp_path / "trusted_workspaces.json"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    r = run(tmp_path, "--file", str(file), str(under), str(outside))
    assert r.returncode != 0
    assert "outside" in r.stderr
    assert not file.exists()


def test_refuses_path_traversal(tmp_path: Path) -> None:
    under = worktrees(tmp_path)
    file = tmp_path / "trusted_workspaces.json"
    sneaky = under / ".." / ".." / "secret"
    sneaky.mkdir(parents=True)
    r = run(tmp_path, "--file", str(file), str(under), str(sneaky))
    assert r.returncode != 0
    assert not file.exists()


def test_refuses_under_not_named_worktrees(tmp_path: Path) -> None:
    worktrees(tmp_path)
    file = tmp_path / "trusted_workspaces.json"
    bads = [
        "/",
        str(tmp_path / "repo"),
        str(tmp_path / "repo" / ".claude"),
        str(tmp_path / "repo" / ".claude" / "worktrees" / ".." / "other"),
    ]
    for bad in bads:
        r = run(tmp_path, "--file", str(file), bad)
        assert r.returncode != 0, bad
        assert "not a <repo>/.claude/worktrees" in r.stderr
    assert not file.exists()


def test_refuses_under_without_git_root(tmp_path: Path) -> None:
    d = tmp_path / "notrepo" / ".claude" / "worktrees"
    d.mkdir(parents=True)
    r = run(tmp_path, str(d))
    assert r.returncode != 0
    assert "not a git repo root" in r.stderr


def test_refuses_symlinked_under(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    real = repo / "real-worktrees"
    real.mkdir()
    (repo / ".claude").mkdir()
    link = repo / ".claude" / "worktrees"
    link.symlink_to(real)
    r = run(tmp_path, str(link))
    assert r.returncode != 0
    assert "symlink" in r.stderr


def test_refuses_symlinked_claude_component(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    real = tmp_path / "elsewhere" / ".claude"
    (real / "worktrees").mkdir(parents=True)
    (repo / ".claude").symlink_to(real)
    r = run(tmp_path, str(repo / ".claude" / "worktrees"))
    assert r.returncode != 0
    assert "symlink" in r.stderr


def test_accepts_a_tree_inside_worktrees(tmp_path: Path) -> None:
    under = worktrees(tmp_path)
    tree = under / "d-something"
    tree.mkdir()
    file = tmp_path / "trusted_workspaces.json"
    r = run(tmp_path, "--file", str(file), str(under), str(tree))
    assert r.returncode == 0, r.stderr
    assert trusted_paths(file) == [os.path.realpath(tree)]


def test_concurrent_adds_keep_every_entry(tmp_path: Path) -> None:
    under = worktrees(tmp_path)
    file = tmp_path / "trusted_workspaces.json"
    trees = []
    for i in range(4):
        t = under / f"d-{i}"
        t.mkdir()
        trees.append(t)
    procs = [
        subprocess.Popen(
            [sys.executable, str(HELPER), "--file", str(file), str(under), str(t)],
            env={**os.environ, "HOME": str(tmp_path / "home")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for t in trees
    ]
    for p in procs:
        assert p.wait() == 0
    assert sorted(trusted_paths(file)) == sorted(os.path.realpath(t) for t in trees)


def test_env_var_selects_the_file(tmp_path: Path) -> None:
    under = worktrees(tmp_path)
    file = tmp_path / "env" / "trusted_workspaces.json"
    r = run(tmp_path, str(under), env_file=file)
    assert r.returncode == 0, r.stderr
    assert trusted_paths(file) == [os.path.realpath(under)]


def test_default_file_lives_under_home(tmp_path: Path) -> None:
    under = worktrees(tmp_path)
    r = run(tmp_path, str(under))
    assert r.returncode == 0, r.stderr
    file = tmp_path / "home" / ".local/share/devin/cli/trusted_workspaces.json"
    assert trusted_paths(file) == [os.path.realpath(under)]


def launch(tmp_path: Path, env_file: Path | None) -> subprocess.CompletedProcess[str]:
    """Run the real devin-tree with fake git + tmux first on PATH."""
    repo = tmp_path / "fakerepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".claude" / "worktrees").mkdir(parents=True)
    wt = repo / ".claude" / "worktrees" / "d-fake"
    bins = tmp_path / "bin"
    bins.mkdir()
    for name, body in (("git", FAKE_GIT), ("tmux", FAKE_TMUX)):
        f = bins / name
        f.write_text(body, encoding="utf-8")
        f.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{bins}:{env['PATH']}"
    env["FAKE_ROOT"] = str(repo)
    env["FAKE_WT"] = str(wt)
    env["FAKE_TMUX_LOG"] = str(tmp_path / "tmux.log")
    if env_file is not None:
        env["DEVIN_TRUSTED_WORKSPACES"] = str(env_file)
    return subprocess.run(
        ["bash", str(DEVIN_TREE), "d-fake", "--session", "fake"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_launch_registers_trust(tmp_path: Path) -> None:
    file = tmp_path / "trusted_workspaces.json"
    r = launch(tmp_path, file)
    assert r.returncode == 0, r.stderr
    assert "devin-tree: d-fake" in r.stdout
    assert "new-window" in (tmp_path / "tmux.log").read_text(encoding="utf-8")
    assert trusted_paths(file) == [
        os.path.realpath(tmp_path / "fakerepo" / ".claude" / "worktrees")
    ]


def test_failing_trust_step_still_launches(tmp_path: Path) -> None:
    file = tmp_path / "trusted_workspaces.json"
    file.write_bytes(b"not json{")
    r = launch(tmp_path, file)
    assert r.returncode == 0, r.stderr
    assert "trust step failed" in r.stderr
    assert "devin-tree: d-fake" in r.stdout
    assert "new-window" in (tmp_path / "tmux.log").read_text(encoding="utf-8")
    assert file.read_bytes() == b"not json{"
