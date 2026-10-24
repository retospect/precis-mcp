"""Regression tests for ``scripts/hooks/rtk-hook.sh``.

The defect (2026-09-16): in a worktree-isolated session (``claude -w``),
Claude Code's built-in isolation guard refuses any rtk-wrapped command that
carries a ``git`` token, and the user-level ``rtk hook claude`` PreToolUse
hook rewrites every ``git …`` to ``rtk git …`` — so every direct git call in
a worktree session was refused, as was a grep/find whose arguments merely
mention ``git``. The shim leaves such commands unrewritten when the session
cwd is under ``.claude/worktrees/`` and delegates everything else to rtk.

Second defect (2026-10-24, gr514332/gr513881/gr514780): rtk's digest rewrites
matched identifiers and collapses match sets in ``rg``/``grep`` output
(``chunk_embeddings`` rendered as ``n``), silently corrupting exactly the
text being searched. Per Reto's ruling the shim never rewrites a command
invoking ``rg`` or ``*grep`` (which also covers ``git grep``) — in any
session, worktree or not.

The real ``rtk`` is replaced by a stub on PATH so the delegation path is
observable and the test does not depend on rtk being installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shim is a POSIX sh script"
)

SHIM = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "rtk-hook.sh"
WORKTREE_CWD = "/Users/x/work/precis-mcp/.claude/worktrees/some-tree"
PRIMARY_CWD = "/Users/x/work/precis-mcp"
STUB_MARK = "STUB-RTK-DELEGATED"


@pytest.fixture
def stub_rtk(tmp_path: Path) -> dict[str, str]:
    stub = tmp_path / "rtk"
    stub.write_text(
        f'#!/bin/sh\nprintf "%s " "{STUB_MARK}"; cat\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run(
    env: dict[str, str], cwd: str, command: str
) -> subprocess.CompletedProcess[str]:
    payload = json.dumps(
        {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}
    )
    return subprocess.run(
        ["sh", str(SHIM)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git log --oneline -3 -- some.README.md",
        'grep -rn "/usr/bin/git" docs/',
        "find . -name x | xargs git add",
    ],
)
def test_worktree_git_mentioning_command_passes_through(
    stub_rtk: dict[str, str], command: str
) -> None:
    res = _run(stub_rtk, WORKTREE_CWD, command)
    assert res.returncode == 0
    assert res.stdout == "", "an empty hook reply means: leave the command unrewritten"


@pytest.mark.parametrize(
    ("cwd", "command"),
    [
        (WORKTREE_CWD, "rg chunk_embeddings"),
        (PRIMARY_CWD, "rg -n 'def ' src/"),
        (WORKTREE_CWD, "grep -rn safe_fetch src/ tests/"),
        (PRIMARY_CWD, "grep -rn safe_fetch src/"),
        (PRIMARY_CWD, "git grep -n pattern"),
        (PRIMARY_CWD, "egrep -r 'a|b' ."),
        (PRIMARY_CWD, "cat log | zgrep WARN | head"),
        (WORKTREE_CWD, "sed -n '1,5p' f | grep foo"),  # grep anywhere in the line
        (PRIMARY_CWD, "pgrep -f pytest"),  # *grep family: also exempt
    ],
)
def test_search_commands_pass_through(
    stub_rtk: dict[str, str], cwd: str, command: str
) -> None:
    res = _run(stub_rtk, cwd, command)
    assert res.returncode == 0
    assert res.stdout == "", "an empty hook reply means: leave the command unrewritten"


@pytest.mark.parametrize(
    ("cwd", "command"),
    [
        (PRIMARY_CWD, "git status --short"),  # not isolated: rtk keeps digesting git
        (WORKTREE_CWD, "find . -name x"),  # no git token
        (
            WORKTREE_CWD,
            "find . -name digital -o -name gitless",
        ),  # substring, not a word
        (PRIMARY_CWD, "cargo build"),  # contains "rgo" letters, no rg/grep word
        (PRIMARY_CWD, "echo org mode"),  # "org" is not the word "rg"
        (PRIMARY_CWD, "echo grepped"),  # "grep" must be at a word's tail
    ],
)
def test_everything_else_is_delegated_to_rtk(
    stub_rtk: dict[str, str], cwd: str, command: str
) -> None:
    res = _run(stub_rtk, cwd, command)
    assert res.returncode == 0
    assert res.stdout.startswith(STUB_MARK)
    assert (
        json.loads(res.stdout[len(STUB_MARK) + 1 :])["tool_input"]["command"] == command
    )
