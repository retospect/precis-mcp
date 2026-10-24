"""``scripts/board`` — the fleet status board (gr515152).

One append-only file, ``<main checkout>/.claude/fleet/board.log``
(gitignored), resolved through the common git dir so every worktree posts
to the same board. ``PRECIS_BOARD`` overrides the path. Lines are
``<UTC ISO Z>\\t<window>\\t<text>``; the append is one write under an
exclusive flock. Pinned here: post/read --since/clear, line format, field
sanitising, and that a post from a linked worktree lands in the primary
checkout's board.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "board"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: flock + git worktrees"
)

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\t")


def _env(board: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PRECIS_BOARD"] = str(board)
    return env


def _git(cwd: Path, *args: str) -> None:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _board(args: list[str], env: dict[str, str], cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        check=False,
        timeout=30,
    )


def test_post_appends_stamped_line(tmp_path: Path) -> None:
    board = tmp_path / "board.log"
    env = _env(board)
    cp = _board(["post", "claude-x", "landed abc123 on main"], env)
    assert cp.returncode == 0, cp.stderr
    lines = board.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert TS_RE.match(lines[0])
    assert lines[0].endswith("\tclaude-x\tlanded abc123 on main")


def test_post_sanitises_tabs_and_newlines(tmp_path: Path) -> None:
    board = tmp_path / "board.log"
    env = _env(board)
    cp = _board(["post", "win\tname", "red\nmain"], env)
    assert cp.returncode == 0
    lines = board.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].count("\t") == 2


def test_read_since_filters_on_timestamp(tmp_path: Path) -> None:
    board = tmp_path / "board.log"
    board.write_text(
        "2026-10-24T10:00:00Z\tw1\told\n2026-10-24T12:00:00Z\tw2\tnewer\n",
        encoding="utf-8",
    )
    env = _env(board)
    cp = _board(["read", "--since", "2026-10-24T11:00:00Z"], env)
    assert cp.returncode == 0
    assert "old" not in cp.stdout
    assert "newer" in cp.stdout
    cp = _board(["read"], env)
    assert "old" in cp.stdout and "newer" in cp.stdout


def test_read_missing_board_is_empty_not_an_error(tmp_path: Path) -> None:
    cp = _board(["read"], _env(tmp_path / "nope.log"))
    assert cp.returncode == 0
    assert cp.stdout == ""


def test_clear_empties_the_board(tmp_path: Path) -> None:
    board = tmp_path / "board.log"
    board.write_text("2026-10-24T10:00:00Z\tw1\told\n", encoding="utf-8")
    env = _env(board)
    cp = _board(["clear"], env)
    assert cp.returncode == 0
    assert board.read_text(encoding="utf-8") == ""


def test_default_board_lands_in_the_primary_checkout(tmp_path: Path) -> None:
    """A post from a linked worktree resolves the board through the common
    git dir — the coordinator's sweep in the primary sees it."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / "f").write_text("x\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "base")
    wt = tmp_path / "wt-one"
    _git(main, "worktree", "add", "-q", "-b", "wt-one", str(wt))
    env = dict(os.environ)
    env.pop("PRECIS_BOARD", None)
    cp = _board(["post", "wt-one", "done"], env, cwd=wt)
    assert cp.returncode == 0, cp.stderr
    board = main / ".claude" / "fleet" / "board.log"
    lines = board.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and lines[0].endswith("\twt-one\tdone")


def test_board_log_is_gitignored() -> None:
    """The board's path must be pinned in .gitignore — checked textually
    because the gate container mounts the worktree without its .git."""
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".claude/fleet/board.log" in (ln.strip() for ln in lines)
