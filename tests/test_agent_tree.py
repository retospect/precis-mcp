"""scripts/kimi-tree and scripts/devin-tree share scripts/lib/agent-tree.sh.

Only ``--dry-run`` and argument rejection are exercised here (no worktree is
created, no tool launched); the create/lock/tmux path is covered by
tests/test_devin_trust.py against a fake git and tmux.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# test_posix_only_guards.py requires this on a module that spawns bash.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: bash")

REPO = Path(__file__).resolve().parents[1]
KIMI = REPO / "scripts" / "kimi-tree"
DEVIN = REPO / "scripts" / "devin-tree"
LIB = REPO / "scripts" / "lib" / "agent-tree.sh"


def run(
    script: Path, *argv: str, tmux: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    if tmux is not None:
        env["TMUX"] = tmux
    return subprocess.run(
        ["bash", str(script), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def fields(out: str) -> dict[str, str]:
    """Parse the ``k=v k=v`` dry-run line (the trailing tool= value has spaces)."""
    head, _, tool = out.strip().partition(" devin=" if " devin=" in out else " kimi=")
    d = dict(kv.split("=", 1) for kv in head.split())
    d["tool"] = tool
    return d


@pytest.mark.parametrize("script", [KIMI, DEVIN], ids=["kimi", "devin"])
@pytest.mark.parametrize("bad", ["Bad", "_x", "a b", "../x"])
def test_bad_name_rejected(script: Path, bad: str) -> None:
    r = run(script, bad, "--dry-run")
    assert r.returncode == 2
    assert f"{script.name}: bad name: {bad}" in r.stderr


@pytest.mark.parametrize("script", [KIMI, DEVIN], ids=["kimi", "devin"])
def test_no_args_prints_usage(script: Path) -> None:
    r = run(script)
    assert r.returncode == 2
    assert f"scripts/{script.name} NAME" in r.stderr


def test_devin_defaults_to_this_terminal() -> None:
    r = run(DEVIN, "d-x", "--dry-run")
    assert r.returncode == 0, r.stderr
    d = fields(r.stdout)
    assert d["tmux"] == "-"
    assert d["branch"] == "worktree-d-x"
    assert d["tree"].endswith("/.claude/worktrees/d-x")
    assert d["tool"] == "devin --permission-mode dangerous"


def test_devin_tmux_names_session() -> None:
    r = run(DEVIN, "d-x", "--tmux", "ws", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert fields(r.stdout)["tmux"] == "ws"


def test_devin_session_is_alias_for_tmux() -> None:
    r = run(DEVIN, "d-x", "--session", "ws", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert fields(r.stdout)["tmux"] == "ws"


def test_devin_tmux_without_session_outside_tmux_refuses() -> None:
    r = run(DEVIN, "d-x", "--tmux", "--dry-run")
    assert r.returncode == 2
    assert "not in tmux" in r.stderr


def test_devin_keeps_model_and_prompt_file(tmp_path: Path) -> None:
    brief = tmp_path / "brief.txt"
    brief.write_text("x\n", encoding="utf-8")
    r = run(DEVIN, "d-x", "--model", "m1", "--prompt-file", str(brief), "--dry-run")
    assert r.returncode == 0, r.stderr
    tool = fields(r.stdout)["tool"]
    assert "--model m1" in tool
    assert f"--prompt-file {brief.resolve()}" in tool


def test_kimi_dry_run_unchanged() -> None:
    r = run(KIMI, "k-x", "--dry-run")
    assert r.returncode == 0, r.stderr
    d = fields(r.stdout)
    assert d["tmux"] == "-"
    assert d["tool"] == "kimi --yolo"
    r = run(KIMI, "k-x", "--tmux", "ws", "--mode", "auto", "--dry-run")
    d = fields(r.stdout)
    assert d["tmux"] == "ws"
    assert d["tool"] == "kimi --auto"


def test_kimi_bad_mode_rejected() -> None:
    r = run(KIMI, "k-x", "--mode", "nope", "--dry-run")
    assert r.returncode == 2
    assert "--mode must be" in r.stderr


def test_library_is_sourced_not_executed() -> None:
    assert not os.access(LIB, os.X_OK)
    src = LIB.read_text(encoding="utf-8")
    for fn in ("at_init", "at_check_name", "at_ensure_tree", "at_launch"):
        assert f"{fn}()" in src
