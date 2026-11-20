"""Unit tests for the guard-piped-exit-code hook (PreToolUse, Bash).

Pure — exercises ``evaluate`` on command strings. Loads the hyphenated-name
hook script by path, as ``tests/test_guard_heredoc.py`` does.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "hooks"
    / "guard-piped-exit-code.py"
)
_spec = importlib.util.spec_from_file_location("guard_piped_exit_code", _HOOK)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate


@pytest.mark.parametrize(
    "command",
    [
        'rg "scripts/ship|scripts/round" docs',
        "rg 'a|scripts/ship|b' docs",
        'rg "a|scripts/ship|b"',
        'grep -E "x|y" scripts/ship',
    ],
)
def test_quoted_alternation_not_a_pipe(command: str) -> None:
    assert evaluate(command) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "scripts/ship --quick | tail -30",
        "FOO=1 scripts/test | cat",  # scripts/test is unguarded: allowed
        "FOO=1 scripts/deploy | cat",
        'scripts/ship "a b" | tail',
        "echo hi && ./scripts/bump | tail",
    ],
)
def test_real_pipe_still_denied(command: str) -> None:
    expect_denied = "scripts/test" not in command
    assert (evaluate(command) is not None) is expect_denied, command


def test_real_pipe_with_env_prefix_denied() -> None:
    assert evaluate("FOO=1 scripts/ship | cat") is not None


@pytest.mark.parametrize(
    "command",
    [
        'bash -c "scripts/ship | tail"',
        "sh -c 'scripts/ship --quick | tail -30'",
        'bash -lc "scripts/deploy | cat"',
    ],
)
def test_bash_c_pipe_still_denied(command: str) -> None:
    assert evaluate(command) is not None, command


def test_reason_has_no_chain() -> None:
    reason = evaluate("scripts/ship --quick | tail -30")
    assert reason is not None
    assert ";" not in reason
    assert "&&" not in reason
    assert ".scratch/ship.log" in reason
    assert "ALLOW_PIPED_EXIT" in reason


def test_pipefail_and_pipestatus_still_allowed() -> None:
    assert evaluate("set -o pipefail; scripts/ship | tail") is None
    assert evaluate("scripts/ship | tail; echo ${PIPESTATUS[0]}") is None
