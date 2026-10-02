"""Unit tests for edit-delegation-nudge.py (Rule F, token-review)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "hooks"
    / "edit-delegation-nudge.py"
)
_spec = importlib.util.spec_from_file_location("edit_delegation_nudge", _PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


@pytest.fixture(autouse=True)
def _tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod.tempfile, "gettempdir", lambda: str(tmp_path))


def _run(monkeypatch, capsys, payload: dict) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _mod.main() == 0
    return capsys.readouterr().out


def _edit(sid="s1", tool="Edit", **kw) -> dict:
    return {"session_id": sid, "tool_name": tool, "tool_input": {}, **kw}


def _agent(kind: str) -> dict:
    return {
        "session_id": "s1",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": kind},
    }


def test_fires_at_threshold_then_rearms(monkeypatch, capsys) -> None:
    n = _mod.THRESHOLD
    outs = [_run(monkeypatch, capsys, _edit()) for _ in range(2 * n)]
    fired = [i + 1 for i, o in enumerate(outs) if o]
    assert fired == [n, 2 * n]
    note = json.loads(outs[n - 1])["hookSpecificOutput"]["additionalContext"]
    assert "`coder`" in note


def test_write_counts_too(monkeypatch, capsys) -> None:
    n = _mod.THRESHOLD
    outs = [
        _run(monkeypatch, capsys, _edit(tool="Edit" if i % 2 else "Write"))
        for i in range(n)
    ]
    assert outs[-1] and not any(outs[:-1])


def test_coder_dispatch_resets(monkeypatch, capsys) -> None:
    for _ in range(_mod.THRESHOLD - 1):
        _run(monkeypatch, capsys, _edit())
    _run(monkeypatch, capsys, _agent("coder"))
    assert _run(monkeypatch, capsys, _edit()) == ""  # would have been the nth


def test_other_agent_does_not_reset(monkeypatch, capsys) -> None:
    for _ in range(_mod.THRESHOLD - 1):
        _run(monkeypatch, capsys, _edit())
    _run(monkeypatch, capsys, _agent("reviewer"))
    assert _run(monkeypatch, capsys, _edit()) != ""


def test_subagent_calls_ignored(monkeypatch, capsys) -> None:
    outs = [
        _run(monkeypatch, capsys, _edit(agent_id="a1"))
        for _ in range(2 * _mod.THRESHOLD)
    ]
    assert not any(outs)


def test_sessions_independent(monkeypatch, capsys) -> None:
    for _ in range(_mod.THRESHOLD - 1):
        _run(monkeypatch, capsys, _edit(sid="a"))
    assert _run(monkeypatch, capsys, _edit(sid="b")) == ""


def test_junk_and_missing_session_silent(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert _mod.main() == 0
    assert _run(monkeypatch, capsys, {"tool_name": "Edit"}) == ""
