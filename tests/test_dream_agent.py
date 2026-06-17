"""Tests for the dream_agent worker (Slice-3 dispatch unification)."""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.store import Store
from precis.utils.claude_agent import AgentResult, ClaudeAgentError
from precis.workers.dream_agent import _gate_enabled, run_dream_pass


def test_gate_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_DREAM_AGENT", raising=False)
    assert _gate_enabled() is False


def test_pass_skips_when_gate_off(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_DREAM_AGENT", raising=False)
    result = run_dream_pass(store)
    assert result.claimed == 0
    assert result.ok == 0


def test_pass_skips_when_prompt_path_missing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_DREAM_AGENT", "1")
    monkeypatch.delenv("PRECIS_DREAM_PROMPT_PATH", raising=False)
    result = run_dream_pass(store)
    assert result.claimed == 0
    assert result.ok == 0


def test_happy_path_dispatches_with_files(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = tmp_path / "dream-prompt.md"
    prompt.write_text("DREAM CYCLE — do dream things.")
    soul = tmp_path / "soul.md"
    soul.write_text("you are asa.")
    mcp = tmp_path / "mcp.json"
    mcp.write_text("{}")

    monkeypatch.setenv("PRECIS_DREAM_AGENT", "1")
    monkeypatch.setenv("PRECIS_DREAM_PROMPT_PATH", str(prompt))
    monkeypatch.setenv("PRECIS_DREAM_SOUL_PATH", str(soul))
    monkeypatch.setenv("PRECIS_MCP_CONFIG", str(mcp))

    captured: dict = {}

    def _fake(*args, **kw) -> AgentResult:
        captured["prompt"] = args[0] if args else kw.get("prompt")
        captured["system_prompt"] = kw.get("system_prompt")
        captured["mcp_config"] = kw.get("mcp_config")
        captured["disallowed"] = kw.get("disallowed_tools")
        return AgentResult(
            final_text="dreamed.", cost_usd=0.02, duration_s=10, turns_used=5
        )

    monkeypatch.setattr("precis.workers.dream_agent.call_claude_agent", _fake)
    result = run_dream_pass(store)
    assert result.claimed == 1
    assert result.ok == 1
    assert result.failed == 0
    # Prompt body landed.
    assert "DREAM CYCLE" in captured["prompt"]
    # system_prompt / mcp_config came through as Path objects.
    assert isinstance(captured["system_prompt"], Path)
    assert isinstance(captured["mcp_config"], Path)
    # WebFetch / WebSearch disabled.
    assert "WebFetch" in captured["disallowed"]
    assert "WebSearch" in captured["disallowed"]


def test_pass_counts_failure_on_dispatch_error(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = tmp_path / "dream-prompt.md"
    prompt.write_text("dream.")
    monkeypatch.setenv("PRECIS_DREAM_AGENT", "1")
    monkeypatch.setenv("PRECIS_DREAM_PROMPT_PATH", str(prompt))

    def _err(*a, **kw):
        raise ClaudeAgentError("bad", stdout="", stderr="model died")

    monkeypatch.setattr("precis.workers.dream_agent.call_claude_agent", _err)
    result = run_dream_pass(store)
    assert result.claimed == 1
    assert result.failed == 1
