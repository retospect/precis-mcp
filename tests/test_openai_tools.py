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
    StreamTimeout,
    ToolCall,
    ToolChatClient,
    ToolSpec,
    _parse_tool_calls,
    _UrllibTransport,
    build_tools_param,
    partial_artifact,
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


# ── _UrllibTransport (real stdlib wrapper — HTTPError body capture) ────
#
# The canonical HTTP-POST seam for every OpenAI-shaped client in this
# codebase (:mod:`precis.workers.llm_summarize` re-exports it as
# ``Transport``/``_UrllibTransport`` rather than keeping its own near-
# identical copy — the encapsulation-residuals unification).


def test_urllib_transport_folds_400_body_into_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 from the upstream (e.g. OpenRouter rejecting `reasoning.enabled`
    on some providers) discards its response body if `urlopen`'s HTTPError is
    let through unhandled — the real rejection reason would otherwise never
    reach a caller's error log. `_UrllibTransport.post_json` reads and folds
    that body into the re-raised error's message, while keeping it a
    `urllib.error.HTTPError` with the SAME `.code` so `router.
    _is_unavailability` still classifies a 400 as `paused=False` (semantic,
    not transient) — see `tests/test_llm_router.py::
    test_is_unavailability_table`."""
    import io
    import urllib.error
    from email.message import Message

    body = b'{"error":{"message":"reasoning.enabled is not supported by upstream provider"}}'

    def _fake_urlopen(req: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            "http://x/v1/chat/completions",
            400,
            "Bad Request",
            Message(),
            io.BytesIO(body),
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    transport = _UrllibTransport()
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        transport.post_json(
            "http://x/v1/chat/completions",
            {"model": "m"},
            headers={},
            timeout=1.0,
        )

    exc = excinfo.value
    assert exc.code == 400
    assert "reasoning.enabled is not supported" in str(exc)


def test_urllib_transport_503_still_classifies_as_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx body is folded in too, but the re-raised HTTPError still keeps
    `.code == 503` — `_is_unavailability` keys off the code, not the message,
    so this must stay `paused=True`-eligible."""
    import urllib.error
    from email.message import Message

    def _fake_urlopen(req: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(
            "http://x/v1/chat/completions",
            503,
            "Service Unavailable",
            Message(),
            None,
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    transport = _UrllibTransport()
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        transport.post_json(
            "http://x/v1/chat/completions",
            {"model": "m"},
            headers={},
            timeout=1.0,
        )

    assert excinfo.value.code == 503


# ── streaming (SSE) ────────────────────────────────────────────────────


class _FakeSseTransport:
    """Yields scripted SSE event dicts; records the payloads sent. An entry
    that is an Exception instance is raised mid-stream instead of yielded
    (a socket idle-timeout / connection failure at that point)."""

    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)
        self.sent: list[dict[str, Any]] = []

    def post_sse(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        idle_timeout: float,
    ) -> Any:
        self.sent.append(payload)
        for ev in self._events:
            if isinstance(ev, Exception):
                raise ev
            yield ev

    def post_json(self, *a: Any, **k: Any) -> dict[str, Any]:
        raise AssertionError("streaming client must not fall back to post_json")


def _delta(content: str | None = None, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = dict(extra)
    if content is not None:
        d["content"] = content
    return {"choices": [{"delta": d}]}


def test_streaming_accumulates_content_and_usage() -> None:
    tx = _FakeSseTransport(
        [
            _delta("hel"),
            _delta("lo"),
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 42, "cost": 0.003},
            },
        ]
    )
    client = ToolChatClient(
        url="http://x/v1", api_key="k", model="m", transport=tx, stream=True
    )
    turn = client.chat([{"role": "user", "content": "hi"}])
    assert turn.content == "hello"
    assert turn.tool_calls == []
    assert turn.total_tokens == 42
    assert turn.cost_usd == 0.003
    assert turn.finish_reason == "stop"
    # The request carried the stream flag.
    assert tx.sent[0]["stream"] is True


def test_streaming_merges_tool_call_fragments() -> None:
    tx = _FakeSseTransport(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "search", "arguments": '{"q"'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": ': "x"}'}}
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
    )
    client = ToolChatClient(
        url="http://x/v1", api_key="k", model="m", transport=tx, stream=True
    )
    turn = client.chat([{"role": "user", "content": "hi"}])
    assert turn.tool_calls == [ToolCall("c1", "search", {"q": "x"})]
    # The assembled assistant message is echo-able (carries the raw calls).
    assert turn.message["tool_calls"][0]["function"]["arguments"] == '{"q": "x"}'


def test_streaming_idle_timeout_carries_partials() -> None:
    """A mid-stream socket timeout raises StreamTimeout with everything
    received so far — the whole point: the reasoning is an artifact, not
    collateral of the abort."""
    tx = _FakeSseTransport(
        [
            _delta(None, reasoning="thinking about Pd"),
            _delta("partial ans"),
            TimeoutError("timed out"),
        ]
    )
    client = ToolChatClient(
        url="http://x/v1", api_key="k", model="m", transport=tx, stream=True
    )
    with pytest.raises(StreamTimeout) as exc_info:
        client.chat([{"role": "user", "content": "hi"}])
    exc = exc_info.value
    assert exc.partial_text == "partial ans"
    assert exc.partial_reasoning == "thinking about Pd"
    # TimeoutError subclass → the router's unavailability classifier fires.
    assert isinstance(exc, TimeoutError)


def test_streaming_hard_ceiling_carries_partials() -> None:
    tx = _FakeSseTransport([_delta("a"), _delta("b"), _delta("c")])
    client = ToolChatClient(
        url="http://x/v1",
        api_key="k",
        model="m",
        transport=tx,
        stream=True,
        timeout=1e-9,  # any elapsed time exceeds the ceiling after event 1
    )
    with pytest.raises(StreamTimeout) as exc_info:
        client.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.partial_text == "a"
    assert "hard ceiling" in str(exc_info.value)


def test_streaming_reasoning_content_variant() -> None:
    """Some providers stream thinking as ``reasoning_content`` rather than
    OpenRouter's normalized ``reasoning`` — both accumulate."""
    tx = _FakeSseTransport(
        [
            _delta(None, reasoning_content="hmm "),
            _delta(None, reasoning_content="ok"),
            TimeoutError("timed out"),
        ]
    )
    client = ToolChatClient(
        url="http://x/v1", api_key="k", model="m", transport=tx, stream=True
    )
    with pytest.raises(StreamTimeout) as exc_info:
        client.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.partial_reasoning == "hmm ok"


def test_stream_flag_falls_back_without_post_sse() -> None:
    """``stream=True`` over a transport with no ``post_sse`` (an older fake /
    custom transport) degrades to the blocking POST rather than crashing."""
    tx = _FakeTransport(
        [
            {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 3},
            }
        ]
    )
    client = ToolChatClient(
        url="http://x/v1", api_key="k", model="m", transport=tx, stream=True
    )
    turn = client.chat([{"role": "user", "content": "q"}])
    assert turn.content == "hi"
    # The blocking path never adds the stream flag to the payload.
    assert "stream" not in tx.sent[0]


def test_streaming_abort_check_aborts_between_chunks() -> None:
    """The injected abort_check (the worker's drain flag) aborts the stream
    between chunks with the partials attached — same salvage path as an
    idle timeout, but immediate (spine slice 2: graceful drain)."""
    seen = {"n": 0}

    def _drain_after_two() -> bool:
        seen["n"] += 1
        return seen["n"] > 2

    tx = _FakeSseTransport([_delta("par"), _delta("tial"), _delta("never")])
    client = ToolChatClient(
        url="http://x/v1",
        api_key="k",
        model="m",
        transport=tx,
        stream=True,
        abort_check=_drain_after_two,
    )
    with pytest.raises(StreamTimeout) as exc_info:
        client.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.partial_text == "partial"
    assert "draining" in str(exc_info.value)


def test_streaming_abort_check_false_is_noop() -> None:
    tx = _FakeSseTransport(
        [_delta("ok"), {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    )
    client = ToolChatClient(
        url="http://x/v1",
        api_key="k",
        model="m",
        transport=tx,
        stream=True,
        abort_check=lambda: False,
    )
    assert client.chat([{"role": "user", "content": "hi"}]).content == "ok"


def test_partial_artifact_formats_both_sections() -> None:
    exc = StreamTimeout("t", partial_text="ans", partial_reasoning="why")
    art = partial_artifact(exc)
    assert "partial reasoning" in art and "why" in art
    assert "partial content" in art and "ans" in art
    # A plain exception (no partials) yields "" so callers can `or`-fallback.
    assert partial_artifact(RuntimeError("x")) == ""


def test_loop_surfaces_stream_partials_on_error() -> None:
    """A StreamTimeout mid-loop lands its partial artifact in ``final_text``
    with ``stop_reason='error'`` + ``paused=True`` — the router then carries
    it to ``LlmResult.text`` so the caller can persist it."""

    class _StreamTimeoutClient:
        def chat(
            self, messages: Any, *, tools: Any = None, tool_choice: str = "auto"
        ) -> ChatTurn:
            raise StreamTimeout(
                "stream went silent for 120s",
                partial_text="half an answer",
                partial_reasoning="half a thought",
            )

    out = run_tool_loop(
        _StreamTimeoutClient(),
        prompt="q",
        tools=[],
        execute=lambda n, a: "",
        max_turns=5,
    )
    assert out.stop_reason == "error"
    assert out.paused is True
    assert "half a thought" in out.final_text
    assert "half an answer" in out.final_text
    # …and it is flagged as a *wall-clock* pause, the strict refinement of
    # `paused` a bounded-budget caller needs (see AgentLoopResult.timed_out).
    assert out.timed_out is True


def test_loop_drain_abort_is_paused_but_not_timed_out() -> None:
    """A drain abort borrows :class:`StreamTimeout` purely for the partial
    salvage — nothing timed out, and the same call succeeds under the next
    worker generation, so it must NOT be flagged ``timed_out`` (the quest-tick
    give-up budget would otherwise charge a deploy bounce as a rung failure)."""

    class _DrainingClient:
        def chat(
            self, messages: Any, *, tools: Any = None, tool_choice: str = "auto"
        ) -> ChatTurn:
            raise StreamTimeout(
                "worker draining: stream aborted between chunks (partial kept)",
                partial_text="half an answer",
                drained=True,
            )

    out = run_tool_loop(
        _DrainingClient(), prompt="q", tools=[], execute=lambda n, a: "", max_turns=5
    )
    assert out.stop_reason == "error"
    assert out.paused is True  # still a skip-and-retry unavailability…
    assert out.timed_out is False  # …but not a deterministic wall-clock loss


def test_loop_semantic_error_is_neither_paused_nor_timed_out() -> None:
    class _BadRequestClient:
        def chat(
            self, messages: Any, *, tools: Any = None, tool_choice: str = "auto"
        ) -> ChatTurn:
            raise RuntimeError("tool-chat returned no choice: {}")

    out = run_tool_loop(
        _BadRequestClient(), prompt="q", tools=[], execute=lambda n, a: "", max_turns=5
    )
    assert out.paused is False and out.timed_out is False


def test_streaming_drain_abort_flags_drained_on_the_exception() -> None:
    """The drain/timeout split is carried on the exception itself, so no layer
    above has to string-match ``"draining"`` out of the message."""
    tx = _FakeSseTransport([_delta("par"), _delta("tial"), _delta("never")])
    seen = {"n": 0}

    def _drain_after_two() -> bool:
        seen["n"] += 1
        return seen["n"] > 2

    client = ToolChatClient(
        url="http://x/v1",
        api_key="k",
        model="m",
        transport=tx,
        stream=True,
        abort_check=_drain_after_two,
    )
    with pytest.raises(StreamTimeout) as exc_info:
        client.chat([{"role": "user", "content": "hi"}])
    assert exc_info.value.drained is True

    # The two genuine aborts (hard ceiling here) are NOT drains.
    ceiling = ToolChatClient(
        url="http://x/v1",
        api_key="k",
        model="m",
        transport=_FakeSseTransport([_delta("a"), _delta("b")]),
        stream=True,
        timeout=1e-9,
    )
    with pytest.raises(StreamTimeout) as ceil_info:
        ceiling.chat([{"role": "user", "content": "hi"}])
    assert ceil_info.value.drained is False


def test_client_max_tokens_rides_the_wire_payload() -> None:
    """``max_tokens`` is a real generation-time stop on this transport — and
    ``None`` omits the key entirely (byte-identical to the payload sent before
    the knob was reachable from ``LlmRequest``)."""
    body = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 3},
    }
    tx = _FakeTransport([body])
    ToolChatClient(
        url="http://x/v1", api_key="k", model="m", transport=tx, max_tokens=4096
    ).chat([{"role": "user", "content": "q"}])
    assert tx.sent[0]["max_tokens"] == 4096

    tx2 = _FakeTransport([body])
    ToolChatClient(url="http://x/v1", api_key="k", model="m", transport=tx2).chat(
        [{"role": "user", "content": "q"}]
    )
    assert "max_tokens" not in tx2.sent[0]


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
    """A request timeout classifies as unavailability — the
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


def test_loop_abort_check_stops_before_next_turn() -> None:
    """A draining worker stops the agent loop before the NEXT turn starts
    (spine slice 2): the text so far is kept, and the result is a ``paused``
    unavailability so the job retries under the next worker generation."""
    call = ToolCall("c1", "get", {"id": 7})
    client = _ScriptedClient([_toolcall_turn(call), _content_turn("never sent")])
    drained = {"on": False}

    def _execute(name: str, args: Any) -> str:
        drained["on"] = True  # SIGTERM lands while the tool runs
        return "ok"

    out = run_tool_loop(
        client,
        prompt="q",
        tools=[ToolSpec("get", "d", {"type": "object"})],
        execute=_execute,
        max_turns=5,
        abort_check=lambda: drained["on"],
    )
    assert out.stop_reason == "error"
    assert out.error is not None and "draining" in out.error
    assert out.paused is True
    assert out.turns_used == 1


def test_worker_sigterm_flips_the_drain_flag() -> None:
    """The worker's signal handler both stops the batch loop AND flips the
    process-wide drain flag ``run_oss_tool_loop`` injects as abort_check."""
    import signal as _signal

    from precis import liveness
    from precis.cli.worker import _install_signal_handlers

    old_int = _signal.getsignal(_signal.SIGINT)
    old_term = _signal.getsignal(_signal.SIGTERM)
    liveness._DRAIN.clear()
    try:
        flag = _install_signal_handlers()
        assert liveness.drain_requested() is False
        handler = _signal.getsignal(_signal.SIGTERM)
        assert callable(handler)
        handler(_signal.SIGTERM, None)
        assert flag["stop"] is True
        assert liveness.drain_requested() is True
    finally:
        _signal.signal(_signal.SIGINT, old_int)
        _signal.signal(_signal.SIGTERM, old_term)
        liveness._DRAIN.clear()


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
