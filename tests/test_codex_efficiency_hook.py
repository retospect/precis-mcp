"""Codex efficiency hooks preserve authority, complete searches and raw inputs."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts/hooks/codex-efficiency.py"
spec = importlib.util.spec_from_file_location("codex_efficiency", HOOK)
assert spec is not None and spec.loader is not None
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


@pytest.mark.parametrize(
    "command",
    [
        "rg owner src",
        "git add x",
        "git status; git add x",
        "cat $(pwd)/x.log",
        "rtk git status",
    ],
)
def test_no_mutation_compound_or_exhaustive_search_rewrite(command: str) -> None:
    assert hook.rewrite(command, {}) is None


def test_escalated_command_is_not_rewritten() -> None:
    assert (
        hook.rewrite("git status", {"sandbox_permissions": "require_escalated"}) is None
    )


def test_log_rewrite_preserves_quoted_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook.shutil, "which", lambda _: "/usr/bin/rtk")
    assert hook.rewrite("cat 'my log.log'", {}) == "rtk log 'my log.log'"


def test_missing_rtk_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook.shutil, "which", lambda _: None)
    assert hook.rewrite("git status", {}) is None


def test_git_rewrite_preserves_other_tool_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hook, "scoped", lambda _: True)
    monkeypatch.setattr(hook.shutil, "which", lambda _: "/usr/bin/rtk")
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "rtk git status", ""),
    )
    result = hook.respond(
        {
            "cwd": str(tmp_path),
            "session_id": "test",
            "hook_event_name": "PreToolUse",
            "tool_input": {
                "command": "git status",
                "workdir": "somewhere",
                "timeout": 10,
            },
        }
    )["hookSpecificOutput"]
    assert result["updatedInput"] == {
        "command": "rtk git status",
        "workdir": "somewhere",
        "timeout": 10,
    }
    assert result["permissionDecision"] == "allow"


def test_installed_hook_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_EFFICIENCY_ROOT", "/repo/precis-mcp")
    assert hook.scoped("/repo/precis-mcp/.claude/worktrees/test")
    assert not hook.scoped("/repo/precis-mcp-other")


def test_unrelated_project_gets_no_context() -> None:
    assert (
        hook.respond({"cwd": "/other/project", "hook_event_name": "SessionStart"}) == {}
    )


def test_reminders_are_bounded_and_metrics_contain_no_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hook, "scoped", lambda _: True)
    payload: dict[str, Any] = {
        "cwd": str(tmp_path),
        "session_id": "test",
        "hook_event_name": "PostToolUse",
        "tool_response": "sensitive " * 3000,
    }
    assert hook.respond(payload)
    assert hook.respond(payload) == {}
    text = next((tmp_path / ".scratch/codex-efficiency").glob("*.json")).read_text(
        encoding="utf-8"
    )
    assert "sensitive" not in text
    assert '"large_results": 2' in text


def test_lifecycle_context_is_short() -> None:
    cwd = str(HOOK.parents[2])
    for event in ["SessionStart", "PreCompact", "PostCompact"]:
        result = hook.respond({"cwd": cwd, "hook_event_name": event})
        assert len(result["hookSpecificOutput"]["additionalContext"]) < 500
