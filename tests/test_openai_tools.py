"""Tests for :mod:`precis.utils.llm.openai_tools` — the OSS tool-calling loop.

Fully offline: the client is exercised with a scripted fake transport, and
the loop engine with a fake chat client + a dict-backed executor, so no live
model, network, or DB is touched.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.utils.llm.openai_tools import (
    AgentLoopResult,
    ChatTurn,
    ToolCall,
    ToolChatClient,
    ToolSpec,
    _parse_tool_calls,
    build_tools_param,
    run_tool_loop,
)

# ── schema shaping ─────────────────────────────────────────────────────


def test_build_tools_param_shape() -> None:
    specs = [
        ToolSpec("search", "find refs", {"type": "object", "properties": {}}),
        ToolSpec("get", "read a ref", {"type": "object", "properties": {}}),
    ]
    out = build_tools_param(specs)
    assert out == [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "find refs",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get",
                "description": "read a ref",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


# ── tool-call parsing (defensive) ──────────────────────────────────────


def test_parse_tool_calls_string_arguments() -> None:
    raw = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "search", "arguments": '{"q": "hi"}'},
        }
    ]
    calls = _parse_tool_calls(raw)
    assert calls == [ToolCall(id="call_1", name="search", arguments={"q": "hi"})]


def test_parse_tool_calls_malformed_json_degrades_to_empty() -> None:
    raw = [{"id": "c", "function": {"name": "get", "arguments": "{not json"}}]
    calls = _parse_tool_calls(raw)
    assert calls == [ToolCall(id="c", name="get", arguments={})]


def test_parse_tool_calls_dict_arguments_and_synth_id() -> None:
    # Some OSS servers pass an object (not a string) and omit the id.
    raw = [{"function": {"name": "get", "arguments": {"id": 42}}}]
    calls = _parse_tool_calls(raw)
    assert calls == [ToolCall(id="call_0", name="get", arguments={"id": 42})]


def test_parse_tool_calls_ignores_non_list_and_nameless() -> None:
    assert _parse_tool_calls(None) == []
    assert _parse_tool_calls([{"function": {"arguments": "{}"}}]) == []


# ── ToolChatClient over a scripted transport ───────────────────────────


class _FakeTransport:
    """Returns queued response bodies in order; records the payloads sent."""

    def __init__(self, bodies: list[dict[str, Any]]) -> None:
        self._bodies = list(bodies)
        self.sent: list[dict[str, Any]] = []

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        self.sent.append(payload)
        return self._bodies.pop(0)


def test_client_parses_content_turn() -> None:
    tx = _FakeTransport(
        [
            {
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 12},
            }
        ]
    )
    client = ToolChatClient(url="http://x/v1", api_key="k", model="m", transport=tx)
    turn = client.chat([{"role": "user", "content": "hi"}])
    assert turn.content == "hello"
    assert turn.tool_calls == []
    assert turn.total_tokens == 12
    assert turn.finish_reason == "stop"


def test_client_sends_tools_and_parses_tool_call_turn() -> None:
    tx = _FakeTransport(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"q": "x"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ]
    )
    client = ToolChatClient(url="http://x/v1", api_key="k", model="m", transport=tx)
    tools = build_tools_param([ToolSpec("search", "d", {"type": "object"})])
    turn = client.chat([{"role": "user", "content": "hi"}], tools=tools)
    assert turn.content is None
    assert turn.tool_calls == [ToolCall("c1", "search", {"q": "x"})]
    # The request carried tools= + tool_choice.
    assert tx.sent[0]["tools"] == tools
    assert tx.sent[0]["tool_choice"] == "auto"


def test_client_raises_on_no_choice() -> None:
    tx = _FakeTransport([{"error": "boom"}])
    client = ToolChatClient(url="http://x/v1", api_key="k", model="m", transport=tx)
    with pytest.raises(RuntimeError, match="no choice"):
        client.chat([{"role": "user", "content": "hi"}])


def test_client_parses_usage_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter's ``usage.cost`` lands on ``ChatTurn.cost_usd``
    (``glm-fleet-flip-safety`` (git-only) Part 2)."""
    tx = _FakeTransport(
        [
            {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 12, "cost": 0.0042},
            }
        ]
    )
    client = ToolChatClient(url="http://x/v1", api_key="k", model="m", transport=tx)
    turn = client.chat([{"role": "user", "content": "hi"}])
    assert turn.cost_usd == 0.0042


def test_client_missing_usage_cost_is_none() -> None:
    """A backend that doesn't report ``usage.cost`` (a local/loopback server)
    leaves it ``None`` rather than defaulting to 0 — a real zero-cost call
    must stay distinguishable from "no data"."""
    tx = _FakeTransport(
        [
            {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 12},
            }
        ]
    )
    client = ToolChatClient(url="http://x/v1", api_key="k", model="m", transport=tx)
    turn = client.chat([{"role": "user", "content": "hi"}])
    assert turn.cost_usd is None


# ── run_tool_loop ──────────────────────────────────────────────────────


class _ScriptedClient:
    """A ChatClient that returns queued ChatTurns and records the transcript
    it was asked to send each turn."""

    def __init__(self, turns: list[ChatTurn]) -> None:
        self._turns = list(turns)
        self.seen_messages: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> ChatTurn:
        self.seen_messages.append([dict(m) for m in messages])
        return self._turns.pop(0)


def _content_turn(text: str, *, cost_usd: float | None = None) -> ChatTurn:
    return ChatTurn(
        message={"role": "assistant", "content": text},
        content=text,
        tool_calls=[],
        total_tokens=5,
        finish_reason="stop",
        cost_usd=cost_usd,
    )


def _toolcall_turn(call: ToolCall, *, cost_usd: float | None = None) -> ChatTurn:
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": "{}"},
            }
        ],
    }
    return ChatTurn(
        message=msg,
        content=None,
        tool_calls=[call],
        total_tokens=7,
        finish_reason="tool_calls",
        cost_usd=cost_usd,
    )


def test_loop_immediate_answer() -> None:
    client = _ScriptedClient([_content_turn("done")])
    out = run_tool_loop(
        client, prompt="q", tools=[], execute=lambda n, a: "", max_turns=5
    )
    assert out == AgentLoopResult(
        final_text="done",
        turns_used=1,
        tool_calls_made=0,
        total_tokens=5,
        stop_reason="stop",
    )


def test_loop_executes_tool_then_answers() -> None:
    call = ToolCall("c1", "get", {"id": 7})
    client = _ScriptedClient([_toolcall_turn(call), _content_turn("the answer")])
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name: str, args: dict[str, Any]) -> str:
        executed.append((name, args))
        return "tool-said-hi"

    out = run_tool_loop(
        client,
        prompt="q",
        tools=[ToolSpec("get", "d", {"type": "object"})],
        execute=execute,
        max_turns=5,
        system_prompt="sys",
    )
    assert out.final_text == "the answer"
    assert out.turns_used == 2
    assert out.tool_calls_made == 1
    assert out.total_tokens == 12  # 7 + 5
    assert out.stop_reason == "stop"
    assert executed == [("get", {"id": 7})]
    # Turn 1 transcript = system + user; turn 2 also has assistant + tool result.
    assert client.seen_messages[0][0] == {"role": "system", "content": "sys"}
    assert client.seen_messages[0][1] == {"role": "user", "content": "q"}
    assert client.seen_messages[1][-1] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "tool-said-hi",
    }


def test_loop_feeds_executor_error_back_not_abort() -> None:
    call = ToolCall("c1", "boom", {})
    client = _ScriptedClient([_toolcall_turn(call), _content_turn("recovered")])

    def execute(name: str, args: dict[str, Any]) -> str:
        raise ValueError("nope")

    out = run_tool_loop(
        client,
        prompt="q",
        tools=[ToolSpec("boom", "d", {"type": "object"})],
        execute=execute,
        max_turns=5,
    )
    assert out.stop_reason == "stop"
    assert out.final_text == "recovered"
    # The error was fed back as the tool result, not raised.
    tool_msg = client.seen_messages[1][-1]
    assert tool_msg["role"] == "tool"
    assert "[tool-error] ValueError: nope" in tool_msg["content"]


def test_loop_hits_max_turns_when_model_never_stops() -> None:
    call = ToolCall("c1", "get", {})
    # Always returns a tool call → never answers.
    client = _ScriptedClient([_toolcall_turn(call) for _ in range(10)])
    out = run_tool_loop(
        client,
        prompt="q",
        tools=[ToolSpec("get", "d", {"type": "object"})],
        execute=lambda n, a: "ok",
        max_turns=3,
    )
    assert out.stop_reason == "max_turns"
    assert out.turns_used == 3
    assert out.tool_calls_made == 3


def test_loop_transport_error_returns_partial() -> None:
    class _BoomClient:
        def chat(
            self, messages: Any, *, tools: Any = None, tool_choice: str = "auto"
        ) -> ChatTurn:
            raise RuntimeError("connection reset")

    out = run_tool_loop(
        _BoomClient(), prompt="q", tools=[], execute=lambda n, a: "", max_turns=5
    )
    assert out.stop_reason == "error"
    assert out.error is not None and "connection reset" in out.error
    assert out.turns_used == 0
    # A bare RuntimeError (not a recognized transport-unavailability signal)
    # is not flagged paused — same as today's plain-error behavior.
    assert out.paused is False


def test_loop_timeout_is_flagged_paused() -> None:
    """A request timeout classifies as unavailability (ADR 0066 §5a) — the
    loop's ``AgentLoopResult.paused`` rides through to
    ``LlmResult.paused`` at the router seam so a pinned pass backs off and
    retries instead of recording a hard failure."""

    class _TimeoutClient:
        def chat(
            self, messages: Any, *, tools: Any = None, tool_choice: str = "auto"
        ) -> ChatTurn:
            raise TimeoutError("timed out after 120.0s")

    out = run_tool_loop(
        _TimeoutClient(), prompt="q", tools=[], execute=lambda n, a: "", max_turns=5
    )
    assert out.stop_reason == "error"
    assert out.paused is True


def test_loop_4xx_stays_unpaused() -> None:
    """A 4xx (non-429) transport failure is a genuine semantic error — it
    will fail identically on retry, so it must NOT be flagged paused."""
    from email.message import Message
    from urllib.error import HTTPError

    class _BadRequestClient:
        def chat(
            self, messages: Any, *, tools: Any = None, tool_choice: str = "auto"
        ) -> ChatTurn:
            raise HTTPError("http://x", 400, "Bad Request", Message(), None)

    out = run_tool_loop(
        _BadRequestClient(), prompt="q", tools=[], execute=lambda n, a: "", max_turns=5
    )
    assert out.stop_reason == "error"
    assert out.paused is False


# ── cost accumulation (Part 2 — meter OpenRouter spend) ────────────────


def test_loop_sums_usage_cost_across_turns() -> None:
    """A turn carrying ``usage.cost`` sums into ``AgentLoopResult.cost_usd``,
    mirroring ``total_tokens``'s accumulation
    (``glm-fleet-flip-safety`` (git-only) Part 2)."""
    call = ToolCall("c1", "get", {"id": 7})
    client = _ScriptedClient(
        [
            _toolcall_turn(call, cost_usd=0.01),
            _content_turn("the answer", cost_usd=0.002),
        ]
    )
    out = run_tool_loop(
        client,
        prompt="q",
        tools=[ToolSpec("get", "d", {"type": "object"})],
        execute=lambda n, a: "tool-said-hi",
        max_turns=5,
    )
    assert out.cost_usd == pytest.approx(0.012)


def test_loop_no_cost_reported_stays_none() -> None:
    """No turn reports ``usage.cost`` (a local backend) → ``cost_usd`` stays
    ``None``, not a false zero."""
    client = _ScriptedClient([_content_turn("done")])
    out = run_tool_loop(
        client, prompt="q", tools=[], execute=lambda n, a: "", max_turns=5
    )
    assert out.cost_usd is None


def test_loop_transport_error_still_reports_partial_cost() -> None:
    """A turn that reported cost before the transport failed keeps that
    partial cost on the error result — same partial-preservation discipline
    as ``final_text``."""

    class _OneThenBoom:
        def __init__(self) -> None:
            self._turns = [_toolcall_turn(ToolCall("c1", "get", {}), cost_usd=0.05)]

        def chat(
            self, messages: Any, *, tools: Any = None, tool_choice: str = "auto"
        ) -> ChatTurn:
            if self._turns:
                return self._turns.pop(0)
            raise RuntimeError("connection reset")

    out = run_tool_loop(
        _OneThenBoom(),
        prompt="q",
        tools=[ToolSpec("get", "d", {"type": "object"})],
        execute=lambda n, a: "ok",
        max_turns=5,
    )
    assert out.stop_reason == "error"
    assert out.cost_usd == pytest.approx(0.05)
