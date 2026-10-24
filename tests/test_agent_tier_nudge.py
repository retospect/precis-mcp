"""Unit tests for agent-tier-nudge.py (gr509449, token-review)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "agent-tier-nudge.py"
)
_spec = importlib.util.spec_from_file_location("agent_tier_nudge", _PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _run(monkeypatch, capsys, payload) -> str:
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)),
    )
    assert _mod.main() == 0
    return capsys.readouterr().out


def _spawn(subagent_type=None, model=None) -> dict:
    ti: dict = {}
    if subagent_type is not None:
        ti["subagent_type"] = subagent_type
    if model is not None:
        ti["model"] = model
    return {"session_id": "s1", "tool_name": "Agent", "tool_input": ti}


@pytest.mark.parametrize("stype", ["general-purpose", "claude", None])
@pytest.mark.parametrize("model", ["fable", "opus", None])
def test_nudges_bare_top_tier(monkeypatch, capsys, stype, model) -> None:
    out = _run(monkeypatch, capsys, _spawn(stype, model))
    note = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "`navigator`" in note
    assert "`cluster-ops`" in note


def test_nudges_task_tool_and_default_model(monkeypatch, capsys) -> None:
    payload = _spawn("general-purpose", "default")
    payload["tool_name"] = "Task"
    assert _run(monkeypatch, capsys, payload)


@pytest.mark.parametrize(
    "stype",
    [
        "navigator",
        "extract",
        "tidy",
        "gripe-filer",
        "cmd-runner",
        "cluster-ops",
        "coder",
    ],
)
def test_silent_for_named_defs(monkeypatch, capsys, stype) -> None:
    assert _run(monkeypatch, capsys, _spawn(stype, "fable")) == ""
    assert _run(monkeypatch, capsys, _spawn(stype)) == ""


@pytest.mark.parametrize("model", ["sonnet", "haiku"])
def test_silent_for_cheap_models(monkeypatch, capsys, model) -> None:
    assert _run(monkeypatch, capsys, _spawn("general-purpose", model)) == ""
    assert _run(monkeypatch, capsys, _spawn(None, model)) == ""


def test_malformed_input_exit_0(monkeypatch, capsys) -> None:
    assert _run(monkeypatch, capsys, "not json") == ""
    assert _run(monkeypatch, capsys, {"tool_name": "Agent"}) == ""
    assert _run(monkeypatch, capsys, {"tool_name": "Agent", "tool_input": "junk"}) == ""
    # non-string subagent_type: treated as a named def — fail open, silent
    assert (
        _run(
            monkeypatch,
            capsys,
            {"tool_name": "Agent", "tool_input": {"subagent_type": 7}},
        )
        == ""
    )


def test_message_uses_live_descriptions(monkeypatch, capsys, tmp_path) -> None:
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "navigator.md").write_text(
        '---\nname: navigator\ndescription: "TEST-DESC marker"\n---\nbody\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    out = _run(monkeypatch, capsys, _spawn("general-purpose", "opus"))
    note = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "TEST-DESC marker" in note
    assert "`extract`" in note  # missing file → name still listed


def test_real_defs_load(monkeypatch, capsys) -> None:
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    out = _run(monkeypatch, capsys, _spawn("claude"))
    note = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    for name in _mod._CHEAP_DEFS:
        assert f"`{name}` (" in note  # every def resolved a description
