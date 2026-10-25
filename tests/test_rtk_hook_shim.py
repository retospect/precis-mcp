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

Third defect (2026-10-25, gr515812): Devin CLI hook payloads carry
``tool_name: "exec"`` and no top-level ``cwd`` — the session dir is the hook
process's ``$PWD``. Without a fallback the worktree passthrough silently never
fired for Devin payloads, so worktree ``git`` commands would have gone to rtk.

Same gripe, Reto's "block rn" ruling (2026-10-25, extended same day to any
cluster): in ripgrep ``-r`` is ``--replace <text>``, so any ``rg`` short-flag
cluster containing ``r`` mangles — ``-rn`` rewrites every match to ``n`` and
``-nr`` swallows the pattern as the replacement text. The shim answers a
PreToolUse deny for any ``rg`` command word carrying such a cluster, before
either passthrough or the rtk delegation can run (``rtk rg -rn`` included).
Only a bare ``-r`` followed by a quoted argument (``rg -r 'x'``) and
``--replace``/``--replace=`` stay allowed; ``rg -r x`` is the grep ``-r``
idiom and is denied.

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
RG_R_DENY_REASON = (
    "rg -r is --replace <text>; -rn rewrites every match to 'n'. Use rg -n (gr515812)."
)


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


def _devin_payload(command: str, workdir: str | None = None) -> str:
    """Devin CLI PreToolUse shape (verified against devin 3000.11.205,
    gr515812): tool_name ``exec``, no top-level ``cwd``, plus correlation
    ids. ``workdir`` appears under tool_input only when the call sets it."""
    tool_input: dict[str, str] = {"command": command}
    if workdir is not None:
        tool_input["workdir"] = workdir
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "exec",
            "tool_input": tool_input,
            "tool_use_id": "exec:0#c7685368ed1b40b2b658ad635f8a6c23",
            "session_id": "test-session",
            "prompt_id": "test-prompt",
        }
    )


def _run_devin(
    env: dict[str, str],
    command: str,
    workdir: str | None = None,
    hook_pwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SHIM)],
        input=_devin_payload(command, workdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=hook_pwd,
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
        (PRIMARY_CWD, "rg -rn foo"),
        (PRIMARY_CWD, "rg -rln foo src/"),
        (WORKTREE_CWD, "rg -rni foo src/"),
        (PRIMARY_CWD, "rg -nr foo src/"),  # r at cluster end eats the pattern
        (PRIMARY_CWD, "rg -lnr foo src/"),
        (PRIMARY_CWD, "rg -r text foo"),  # unquoted value: the grep -r idiom
        (PRIMARY_CWD, "rg -r'x' foo"),  # attached value: not a bare -r
        (PRIMARY_CWD, "rg '-rn' foo"),  # quoting does not protect a flag
        (PRIMARY_CWD, "rg -r 'x' -nr foo"),  # allowed -r, then a cluster
        (PRIMARY_CWD, "rg -r"),  # bare -r with no value
        (PRIMARY_CWD, "rtk rg -rn foo"),
        (PRIMARY_CWD, "rtk proxy rg -nr foo"),
        (PRIMARY_CWD, "env FOO=1 rg -rn foo"),
        (PRIMARY_CWD, "cat log | rg -rn foo"),
        (PRIMARY_CWD, "true && rg -rn foo"),
        (PRIMARY_CWD, "false || rg -rn foo"),
        (PRIMARY_CWD, "true; rg -rn foo"),
        (PRIMARY_CWD, "echo hi > out;rg -rn foo"),
    ],
)
def test_rg_replace_cluster_is_denied(
    stub_rtk: dict[str, str], cwd: str, command: str
) -> None:
    res = _run(stub_rtk, cwd, command)
    assert res.returncode == 0
    out = json.loads(res.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == RG_R_DENY_REASON


@pytest.mark.parametrize(
    "command",
    [
        "rg -r 'text' foo src/",  # bare -r with a quoted replacement
        'rg -r "text" foo src/',  # double-quoted replacement also allowed
        "rg -r '' foo src/",  # quoted empty replacement
        "rg --replace text foo src/",
        "rg --replace=text foo src/",
        "rg -n foo src/",
        "rg -iln foo src/",  # no r in the cluster
        "rg -- -rn",  # -rn is the pattern, after --
        "grep -rn foo src/",  # grep, not rg
        "echo rg -rn foo",  # rg is an argument, not the command word
        "echo 'rg -rn foo'",  # quoted string of a different command
        "grep -e '-rn' f",  # '-rn' inside another command's quoted arg
        "xargs rg -rn",  # rg inside xargs args — out of the denied shapes
    ],
)
def test_rg_non_cluster_forms_are_not_denied(
    stub_rtk: dict[str, str], command: str
) -> None:
    res = _run(stub_rtk, PRIMARY_CWD, command)
    assert res.returncode == 0
    assert "permissionDecision" not in res.stdout


def test_devin_payload_rg_cluster_is_denied(stub_rtk: dict[str, str]) -> None:
    res = _run_devin(stub_rtk, "rtk rg -rn foo")
    assert res.returncode == 0
    out = json.loads(res.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == RG_R_DENY_REASON


@pytest.mark.parametrize(
    "command",
    [
        "rg connect_timeout src/ scripts/",
        "grep -rn safe_fetch src/",
        "git grep -n pattern",
    ],
)
def test_devin_payload_search_commands_pass_through(
    stub_rtk: dict[str, str], command: str
) -> None:
    res = _run_devin(stub_rtk, command)
    assert res.returncode == 0
    assert res.stdout == "", "an empty hook reply means: leave the command unrewritten"


def test_devin_payload_workdir_worktree_git_passes_through(
    stub_rtk: dict[str, str],
) -> None:
    res = _run_devin(stub_rtk, "git status --short", workdir=WORKTREE_CWD)
    assert res.returncode == 0
    assert res.stdout == ""


def test_devin_payload_pwd_fallback_worktree_git_passes_through(
    stub_rtk: dict[str, str], tmp_path: Path
) -> None:
    hook_pwd = tmp_path / ".claude" / "worktrees" / "some-tree"
    hook_pwd.mkdir(parents=True)
    env = dict(stub_rtk)
    env.pop("PWD", None)  # sh re-mints PWD from its real cwd
    res = _run_devin(env, "git status --short", hook_pwd=hook_pwd)
    assert res.returncode == 0
    assert res.stdout == ""


def test_devin_payload_nonworktree_git_delegates(
    stub_rtk: dict[str, str], tmp_path: Path
) -> None:
    env = dict(stub_rtk)
    env.pop("PWD", None)
    res = _run_devin(env, "git status --short", hook_pwd=tmp_path)
    assert res.returncode == 0
    assert res.stdout.startswith(STUB_MARK)


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
