"""scripts/take-agent fast-forwards an agent branch into the caller's branch.

Real git in a throwaway repo: the agent branch must end up an ancestor of
the caller's branch (so scripts/ship's contained-branch reap can see it),
and --discard removes tree and branch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX script")

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "take-agent"


def _env(tmp_path: Path) -> dict[str, str]:
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = T\n\temail = t@example.invalid\n"
        "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    return {**os.environ, "GIT_CONFIG_GLOBAL": str(cfg), "GIT_CONFIG_NOSYSTEM": "1"}


def _g(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _commit(cwd: Path, env: dict[str, str], name: str) -> None:
    (cwd / name).write_text(name, encoding="utf-8")
    _g(cwd, env, "add", "-A")
    _g(cwd, env, "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    env = _env(tmp_path)
    primary = tmp_path / "primary"
    primary.mkdir()
    _g(primary, env, "init", "-q", "-b", "main")
    (primary / ".gitignore").write_text(".claude/\n", encoding="utf-8")
    _commit(primary, env, "base")
    (primary / "scripts").mkdir()
    shutil.copy2(_SCRIPT, primary / "scripts" / "take-agent")
    feat = primary / ".claude" / "worktrees" / "feat"
    _g(primary, env, "worktree", "add", "-q", "-b", "feat", str(feat))
    return primary, feat, env


def _agent(primary: Path, env: dict[str, str], name: str) -> Path:
    tree = primary / ".claude" / "worktrees" / name
    _g(
        primary,
        env,
        "worktree",
        "add",
        "-q",
        "-b",
        f"worktree-{name}",
        str(tree),
        "main",
    )
    _commit(tree, env, f"{name}.txt")
    return tree


def _take(
    feat: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(feat.parent.parent.parent / "scripts" / "take-agent"), *args],
        cwd=feat,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_take_fast_forwards_agent_into_branch(
    repo: tuple[Path, Path, dict[str, str]],
) -> None:
    primary, feat, env = repo
    _commit(feat, env, "mine.txt")  # caller moved on: agent must be rebased
    _agent(primary, env, "agent-x")
    r = _take(feat, env, "agent-x")
    assert r.returncode == 0, r.stderr
    assert (feat / "agent-x.txt").exists() and (feat / "mine.txt").exists()
    merged = _g(feat, env, "branch", "--merged", "HEAD", "--list", "worktree-agent-*")
    assert "worktree-agent-x" in merged
    assert _g(feat, env, "log", "--merges", "--oneline") == ""
    record = Path(_g(feat, env, "rev-parse", "--git-path", "taken-agents"))
    record = record if record.is_absolute() else feat / record
    tip = _g(feat, env, "rev-parse", "worktree-agent-x")
    assert record.read_text(encoding="utf-8") == f"worktree-agent-x {tip}\n"


def test_non_agent_tree_is_refused(repo: tuple[Path, Path, dict[str, str]]) -> None:
    primary, feat, env = repo
    other = primary / ".claude" / "worktrees" / "someone"
    _g(primary, env, "worktree", "add", "-q", "-b", "someone", str(other), "main")
    r = _take(feat, env, "--discard", "someone")
    assert r.returncode != 0 and "not an agent branch" in r.stderr
    assert other.exists()


def test_discard_removes_tree_and_branch(
    repo: tuple[Path, Path, dict[str, str]],
) -> None:
    primary, feat, env = repo
    tree = _agent(primary, env, "agent-y")
    r = _take(feat, env, "--discard", "agent-y")
    assert r.returncode == 0, r.stderr
    assert not tree.exists()
    assert _g(primary, env, "branch", "--list", "worktree-agent-y") == ""


def test_dirty_agent_tree_is_refused(repo: tuple[Path, Path, dict[str, str]]) -> None:
    primary, feat, env = repo
    tree = _agent(primary, env, "agent-z")
    (tree / "loose.txt").write_text("x", encoding="utf-8")
    r = _take(feat, env, "--discard", "agent-z")
    assert r.returncode != 0 and "uncommitted" in r.stderr
    assert tree.exists()
