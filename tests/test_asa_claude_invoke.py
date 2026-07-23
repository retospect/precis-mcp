"""Tests for asa_bot.claude_invoke.invoke — the router-migration Phase 3 seam.

``invoke()`` now builds an ``LlmRequest`` and awaits
``precis.utils.llm.router.dispatch_async`` instead of hand-rolling
``asyncio.create_subprocess_exec``. These tests stub the ONE remaining real
subprocess boundary — ``precis.utils.claude_agent.run_claude_async`` — so
the full plumbing above it (``dispatch_async`` → the budget/admission/
local-serving gates → ``call_claude_agent_async`` → argv/env building) runs
for real, and only the actual OS-level ``claude`` binary spawn is faked.
This is the same "stub the lowest real boundary" idiom
``tests/test_llm_router.py``'s ``dispatch_async`` tests use, one level
lower (there they stub ``call_claude_agent_async`` itself).

No ``pytest-asyncio`` in this repo yet; ``asyncio.run()`` inside a plain
sync test is the lightest way to drive the coroutine.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

import precis.utils.claude_agent as claude_agent
from asa_bot.claude_invoke import ClaudeResult, invoke
from asa_bot.config import LLMConfig


@pytest.fixture(autouse=True)
def _sandbox_oauth(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep OAuth resolution hermetic: no real ~/.claude_oauth_token, no
    real vault/DB round-trip (asa_bot.oauth.ensure_oauth_token's vault leg
    calls asa_bot.secrets.reveal_secret, which opens a real psycopg
    connection if PRECIS_DATABASE_URL is set — stub it out)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("asa_bot.secrets.reveal_secret", lambda name, **kw: None)


def _cfg(**overrides: Any) -> LLMConfig:
    return dataclasses.replace(LLMConfig(), **overrides)


def _events_stub(
    events: list[dict[str, Any]],
    *,
    env_sink: dict[str, str] | None = None,
) -> Any:
    """Build a fake ``run_claude_async`` that streams ``events`` to
    ``on_event`` (mirroring the real subprocess pump) and, if given,
    records the ``env`` kwarg it was called with into ``env_sink``."""

    async def fake_run_claude_async(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        if env_sink is not None:
            env_sink.update(kwargs.get("env") or {})
        on_event = kwargs.get("on_event")
        if on_event is not None:
            for evt in events:
                await on_event(evt)
        import json

        stdout = "\n".join(json.dumps(e) for e in events)
        return SimpleNamespace(stdout=stdout, stderr="")

    return fake_run_claude_async


def test_invoke_progress_events_env_and_result_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One realistic turn: on_progress fires the three documented event
    shapes in order, ASA_CONV_SLUG (+ cfg.env passthrough) lands in the
    subprocess env, and every ClaudeResult field is populated straight off
    the stream events."""
    events: list[dict[str, Any]] = [
        {"type": "system", "subtype": "init", "session_id": "sess-abc"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp__precis__get",
                        "input": {"kind": "paper", "id": "42"},
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Looking into it now."}]},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": " Here are the details you asked for."}
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "total_cost_usd": 0.08,
            "num_turns": 3,
            "duration_ms": 4567,
            "result": "Looking into it now. Here are the details you asked for.",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 45,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 5,
            },
        },
    ]
    captured_env: dict[str, str] = {}
    monkeypatch.setattr(
        claude_agent, "run_claude_async", _events_stub(events, env_sink=captured_env)
    )

    progress: list[tuple] = []

    async def on_progress(evt: tuple) -> None:
        progress.append(evt)

    cfg = _cfg(env={"FOO": "bar"})
    result = asyncio.run(
        invoke(
            cfg,
            "system prompt",
            "user message",
            conv_slug="conv-1",
            on_progress=on_progress,
        )
    )

    assert isinstance(result, ClaudeResult)

    # ASA_CONV_SLUG lands in the actual subprocess env, alongside cfg.env.
    assert captured_env["ASA_CONV_SLUG"] == "conv-1"
    assert captured_env["FOO"] == "bar"

    # The three documented on_progress event shapes, in arrival order.
    assert progress[0] == (
        "tool_use",
        "mcp__precis__get",
        {"kind": "paper", "id": "42"},
    )
    assert progress[1] == ("text_partial", len("Looking into it now."))
    assert progress[2] == ("first_sentence", "Looking into it now.")
    full_text = "Looking into it now. Here are the details you asked for."
    assert progress[3] == ("text_partial", len(full_text))
    assert len(progress) == 4  # first_sentence only fires once

    # ClaudeResult fields, populated incrementally off the same events.
    assert result.text == full_text
    assert result.session_id == "sess-abc"
    assert result.tool_uses == ["mcp__precis__get"]
    assert result.stop_reason == "success"
    assert result.duration_ms == 4567
    assert result.input_tokens == 120
    assert result.output_tokens == 45
    assert result.cache_read_tokens == 10
    assert result.cache_creation_tokens == 5
    assert result.first_sentence == "Looking into it now."
    assert result.first_sentence_emitted is True
    assert result.error is None


def test_invoke_graceful_timeout_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real wall-clock timeout inside the router path is remapped onto
    asa_bot's original 'turn exceeded Ns timeout' message shape, not
    whatever wording the router transport itself uses."""

    async def fake_run_claude_async(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        timeout_s = kwargs.get("timeout_s")
        raise claude_agent.ClaudeAgentError(
            f"claude -p (agent · async) timed out after {timeout_s}s",
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(claude_agent, "run_claude_async", fake_run_claude_async)

    cfg = _cfg(turn_timeout_seconds=42)
    result = asyncio.run(invoke(cfg, "sys", "hi", conv_slug="conv-2", on_progress=None))

    assert result.error == "turn exceeded 42s timeout"
    assert result.text == ""


def test_invoke_swallows_error_when_text_already_streamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero exit that nonetheless streamed a partial/complete answer
    (recoverable exhaustion, a CLI-teardown quirk) is a silent success —
    mirrors the old hand-rolled path's ``rc != 0 and not result.text`` gate."""
    events: list[dict[str, Any]] = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "partial answer"}]},
        },
    ]

    async def fake_run_claude_async(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        on_event = kwargs.get("on_event")
        if on_event is not None:
            for evt in events:
                await on_event(evt)
        raise claude_agent.ClaudeAgentError(
            "claude -p (agent · async) exited 1: some teardown quirk",
            stdout="partial answer",
            stderr="some teardown quirk",
            returncode=1,
        )

    monkeypatch.setattr(claude_agent, "run_claude_async", fake_run_claude_async)

    cfg = _cfg()
    result = asyncio.run(invoke(cfg, "sys", "hi", conv_slug="conv-3"))

    assert result.text == "partial answer"
    assert result.error is None


def test_invoke_surfaces_error_when_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero exit with nothing streamed IS surfaced as an error."""

    async def fake_run_claude_async(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        raise claude_agent.ClaudeAgentError(
            "claude -p (agent · async) exited 1: boom",
            stdout="",
            stderr="boom",
            returncode=1,
        )

    monkeypatch.setattr(claude_agent, "run_claude_async", fake_run_claude_async)

    cfg = _cfg()
    result = asyncio.run(invoke(cfg, "sys", "hi", conv_slug="conv-4"))

    assert result.text == ""
    assert result.error is not None
    assert "boom" in result.error


def test_invoke_never_raises_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """invoke() must never propagate an exception — bot.py's per-turn queue
    consumer has no fallback reply for a raised invoke()."""

    async def fake_run_claude_async(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        raise RuntimeError("something broke deep in the plumbing")

    monkeypatch.setattr(claude_agent, "run_claude_async", fake_run_claude_async)

    cfg = _cfg()
    result = asyncio.run(invoke(cfg, "sys", "hi", conv_slug="conv-5"))

    assert isinstance(result, ClaudeResult)
    assert result.error is not None


def test_invoke_without_on_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """omitting on_progress (the cron path) still returns a populated result."""
    events: list[dict[str, Any]] = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "cron reply"}]},
        },
        {"type": "result", "subtype": "success", "result": "cron reply"},
    ]
    monkeypatch.setattr(claude_agent, "run_claude_async", _events_stub(events))

    cfg = _cfg()
    result = asyncio.run(
        invoke(cfg, "sys", "payload", conv_slug="conv-6", on_progress=None)
    )

    assert result.text == "cron reply"
    assert result.error is None


def test_invoke_parses_model_and_max_turns_from_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live --model override spliced into cfg.command (bot._llm_with_override)
    and a non-default --max-turns are both honored, not just the tier default."""
    seen: dict[str, Any] = {}

    async def fake_call_claude_agent_async(prompt: str, **kwargs: Any) -> Any:
        seen["model"] = kwargs.get("model")
        seen["max_turns"] = kwargs.get("max_turns")
        from precis.utils.claude_agent import AgentResult

        return AgentResult(final_text="ok", cost_usd=0.01, duration_s=0.1, turns_used=1)

    monkeypatch.setattr(
        "precis.utils.llm.router.call_claude_agent_async",
        fake_call_claude_agent_async,
    )

    cfg = _cfg(
        command=[
            "claude",
            "-p",
            "--max-turns",
            "7",
            "--model",
            "claude-haiku-4-5",
            "--output-format",
            "stream-json",
        ]
    )
    result = asyncio.run(invoke(cfg, "sys", "hi", conv_slug="conv-7"))

    assert seen["model"] == "claude-haiku-4-5"
    assert seen["max_turns"] == 7
    # This fake never calls on_event (unlike the other tests' subprocess-
    # level stub), so ClaudeResult.text is deliberately NOT populated from
    # LlmResult.text here — invoke() sources .text purely from the streamed
    # events, never from the router's aggregated result (see the module
    # docstring). No error either way: the call "succeeded".
    assert result.text == ""
    assert result.error is None
