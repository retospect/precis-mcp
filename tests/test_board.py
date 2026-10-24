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

import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "board"

_loader = importlib.machinery.SourceFileLoader("board_script", str(SCRIPT))
_spec = importlib.util.spec_from_loader("board_script", _loader)
assert _spec is not None and _spec.loader is not None
board_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(board_script)

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
    assert ".claude/fleet/board.cursor.*" in (ln.strip() for ln in lines)


def test_post_state_prepends_validated_keyword(tmp_path: Path) -> None:
    board = tmp_path / "board.log"
    env = _env(board)
    cp = _board(["post", "w1", "--state", "done", "abc1234 all green"], env)
    assert cp.returncode == 0, cp.stderr
    line = board.read_text(encoding="utf-8").rstrip("\n")
    assert line.endswith("\tw1\tdone abc1234 all green")


def test_post_rejects_an_unknown_state(tmp_path: Path) -> None:
    board = tmp_path / "board.log"
    cp = _board(["post", "w1", "--state", "bogus", "x"], _env(board))
    assert cp.returncode == 2
    assert "invalid choice" in cp.stderr
    assert not board.exists()


def test_post_free_text_still_accepted(tmp_path: Path) -> None:
    board = tmp_path / "board.log"
    env = _env(board)
    cp = _board(["post", "w1", "not a state just words"], env)
    assert cp.returncode == 0
    assert board.read_text(encoding="utf-8").endswith("\tw1\tnot a state just words\n")


def test_post_refuses_a_line_over_120_chars(tmp_path: Path) -> None:
    board = tmp_path / "board.log"
    env = _env(board)
    cp = _board(["post", "w1", "x" * 200], env)
    assert cp.returncode == 2
    assert "120" in cp.stderr and "handoff" in cp.stderr
    assert not board.exists()


def test_read_n_prints_only_the_last_n_lines(tmp_path: Path) -> None:
    board = tmp_path / "board.log"
    board.write_text(
        "2026-10-24T10:00:00Z\tw1\tfirst\n"
        "2026-10-24T10:01:00Z\tw2\tsecond\n"
        "2026-10-24T10:02:00Z\tw3\tthird\n",
        encoding="utf-8",
    )
    cp = _board(["read", "-n", "2"], _env(board))
    assert cp.returncode == 0
    assert "first" not in cp.stdout
    assert "second" in cp.stdout and "third" in cp.stdout


def test_read_new_prints_only_unread_lines_per_reader(tmp_path: Path) -> None:
    board = tmp_path / "board.log"
    board.write_text("2026-10-24T10:00:00Z\tw1\tone\n", encoding="utf-8")
    env = _env(board)
    cp = _board(["read", "--new", "--as", "alice"], env)
    assert cp.returncode == 0
    assert "one" in cp.stdout
    cp = _board(["read", "--new", "--as", "alice"], env)
    assert cp.returncode == 0 and cp.stdout == ""
    cp = _board(["read", "--new", "--as", "bob"], env)
    assert "one" in cp.stdout  # independent cursor
    with board.open("a", encoding="utf-8") as fh:
        fh.write("2026-10-24T10:01:00Z\tw2\ttwo\n")
    cp = _board(["read", "--new", "--as", "alice"], env)
    assert "two" in cp.stdout and "one" not in cp.stdout


def test_read_new_needs_a_reader(tmp_path: Path) -> None:
    cp = _board(["read", "--new"], _env(tmp_path / "board.log"))
    assert cp.returncode == 2
    assert "--as" in cp.stderr


def test_read_new_resets_a_cursor_past_eof(tmp_path: Path) -> None:
    board = tmp_path / "board.log"
    env = _env(board)
    board.write_text(
        "2026-10-24T10:00:00Z\tw1\tone\n2026-10-24T10:01:00Z\tw1\ttwo\n",
        encoding="utf-8",
    )
    assert _board(["read", "--new", "--as", "alice"], env).returncode == 0
    board.write_text("2026-10-24T10:02:00Z\tw1\tnew\n", encoding="utf-8")
    cp = _board(["read", "--new", "--as", "alice"], env)
    assert cp.returncode == 0
    assert "new" in cp.stdout  # rewound to the top of the truncated log
    assert "reset" in cp.stderr.lower()


def test_read_shows_old_format_lines_unchanged(tmp_path: Path) -> None:
    """Lines posted before the state format (no leading keyword) still
    read verbatim."""
    board = tmp_path / "board.log"
    board.write_text(
        "2026-10-24T10:00:00Z\tw1\tlanded abc1234 on main\n"
        "2026-10-24T10:01:00Z\tw2\twt-x at 3d528db: spec done\n",
        encoding="utf-8",
    )
    cp = _board(["read"], _env(board))
    assert cp.returncode == 0
    assert cp.stdout.splitlines() == board.read_text(encoding="utf-8").splitlines()


def test_watch_format_line_aligns_and_colours_by_state() -> None:
    line = "2026-10-24T10:00:00Z\tw1\tlanded abc1234 note"
    plain = board_script.format_line(line, color=False)
    assert plain == "10:00:00  w1              landed   abc1234 note"
    coloured = board_script.format_line(line, color=True)
    assert coloured.startswith("\x1b[32m") and coloured.endswith("\x1b[0m")
    for state, code in (
        ("landed", "32"),
        ("green", "32"),
        ("red", "31"),
        ("wedged", "31"),
        ("blocked", "33"),
        ("waiting", "33"),
        ("done", "36"),
    ):
        out = board_script.format_line(
            f"2026-10-24T10:00:00Z\tw\t{state} n", color=True
        )
        assert out.startswith(f"\x1b[{code}m"), state


def test_watch_format_line_free_text_gets_no_state_column() -> None:
    out = board_script.format_line(
        "2026-10-24T10:00:00Z\tw1\told free text", color=True
    )
    assert "\x1b[" not in out
    assert out.endswith("old free text")
