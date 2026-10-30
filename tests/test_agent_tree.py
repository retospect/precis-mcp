"""scripts/kimi-tree and scripts/devin-tree share scripts/lib/agent-tree.sh.

Only ``--dry-run`` and argument rejection are exercised here (no worktree is
created, no tool launched); the create/lock/tmux path is covered by
tests/test_devin_trust.py against a fake git and tmux.
"""

from __future__ import annotations

import json
import os
import pty
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


# --- kimi-tree trust-prompt guard (gr537789) -------------------------------
#
# kimi has no --trust/non-interactive flag, so an untrusted folder's
# interactive "Trust this folder?" prompt reads stdin — launched without a
# tty it waits forever. kimi-tree must refuse that launch (naming the
# manual equivalent), while --prompt one-shots still run, bounded by
# KIMI_TREE_TIMEOUT. Fakes below mirror tests/test_devin_trust.py's: keep
# FAKE_GIT in step with that file's.

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

FAKE_KIMI = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$KIMI_LOG"
"""

# Logs the bound it was given, then execs the wrapped command.
FAKE_TIMEOUT = """#!/usr/bin/env bash
printf 'timeout %s\\n' "$*" >> "$TIMEOUT_LOG"
shift
exec "$@"
"""


def kimi_env(tmp_path: Path, bins: Path) -> dict[str, str]:
    """Env for a kimi-tree launch: fake bins first on PATH, kimi-setup's
    writes aimed at throwaway paths (a precis entry with a bearer env var,
    so no secrets file is touched), a recognizable KIMI_TREE_TIMEOUT."""
    repo = tmp_path / "fakerepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".claude" / "worktrees").mkdir(parents=True)
    bins.mkdir()
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "precis": {
                        "url": "https://mcp.invalid",
                        "bearerTokenEnvVar": "PRECIS_MCP_TOKEN",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    for name, body in (
        ("git", FAKE_GIT),
        ("kimi", FAKE_KIMI),
        ("timeout", FAKE_TIMEOUT),
    ):
        f = bins / name
        f.write_text(body, encoding="utf-8")
        f.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["PATH"] = f"{bins}:{env['PATH']}"
    env["FAKE_ROOT"] = str(repo)
    env["FAKE_WT"] = str(repo / ".claude" / "worktrees" / "k-fake")
    env["KIMI_LOG"] = str(tmp_path / "kimi.log")
    env["TIMEOUT_LOG"] = str(tmp_path / "timeout.log")
    env["KIMI_CODE_HOME"] = str(tmp_path / "kimi-home")
    env["CLAUDE_JSON"] = str(claude_json)
    env["KIMI_TREE_TIMEOUT"] = "45"
    return env


def read_log(tmp_path: Path, name: str) -> str:
    f = tmp_path / name
    return f.read_text(encoding="utf-8") if f.exists() else ""


def test_interactive_launch_without_tty_is_refused(tmp_path: Path) -> None:
    env = kimi_env(tmp_path, tmp_path / "bin")
    r = subprocess.run(
        ["bash", str(KIMI), "k-fake"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert r.returncode == 1
    assert "Trust this folder?" in r.stderr
    assert "kimi --yolo" in r.stderr  # the manual equivalent is named
    # kimi never ran, and the refuse fired before a tree was cut.
    assert read_log(tmp_path, "kimi.log") == ""
    worktrees = Path(env["FAKE_ROOT"]) / ".claude" / "worktrees"
    assert list(worktrees.iterdir()) == []


def test_prompt_launch_without_tty_runs_bounded(tmp_path: Path) -> None:
    env = kimi_env(tmp_path, tmp_path / "bin")
    r = subprocess.run(
        ["bash", str(KIMI), "k-fake", "--prompt", "say ok"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "--prompt say ok" in read_log(tmp_path, "kimi.log")
    assert "timeout 45 kimi --prompt say ok" in read_log(tmp_path, "timeout.log")


def test_interactive_launch_with_tty_runs_unbounded(tmp_path: Path) -> None:
    env = kimi_env(tmp_path, tmp_path / "bin")
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["bash", str(KIMI), "k-fake"],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    finally:
        os.close(slave)
    try:
        _, err = proc.communicate(timeout=120)
    finally:
        os.close(master)
    assert proc.returncode == 0, err.decode("utf-8", "replace")
    assert "--yolo" in read_log(tmp_path, "kimi.log")
    # Interactive sessions are not bounded — a bound would kill them.
    assert read_log(tmp_path, "timeout.log") == ""


def test_exec_refuses_interactive_launch_without_tty(tmp_path: Path) -> None:
    env = kimi_env(tmp_path, tmp_path / "bin")
    tree = tmp_path / "tree"
    tree.mkdir()
    r = subprocess.run(
        ["bash", str(KIMI), "--exec", str(tree), "--yolo"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert r.returncode == 1
    assert "Trust this folder?" in r.stderr
    assert read_log(tmp_path, "kimi.log") == ""
