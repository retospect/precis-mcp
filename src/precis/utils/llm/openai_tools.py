"""OpenAI ``tools=`` agent loop — the OSS path to driving precis tools.

This backs the ``OPENAI_TOOLS`` transport (ADR 0024/0046): an open-source
model driving the precis verbs over the OpenAI ``/v1/chat/completions``
``tools=`` wire, so agentic work (planner ticks, reviewers) can run off a
hosted or local OSS backend instead of the ``claude -p`` binary. ADR 0024
prototyped an in-process litellm-with-``tools=`` loop and reversed it onto
``claude``; this is that loop, rebuilt behind the router's provider port.

Three precis-agnostic seams keep it testable with no live model, network,
or DB:

* :class:`ToolSpec` + :func:`build_tools_param` — a ``(name, description,
  json-schema)`` triple rendered into the OpenAI ``tools=`` array.
* :class:`ToolChatClient` — one ``/v1/chat/completions`` round-trip
  carrying ``tools=``, returning a :class:`ChatTurn` (final text *or* a
  list of tool calls) over the same minimal HTTP-POST transport seam the
  summarizer uses, so a fake transport scripts a whole conversation.
* :func:`run_tool_loop` — the multi-turn engine, pure over an ``execute``
  callback. It never imports precis; the provider wires ``execute`` to the
  in-process verb dispatch and ``tools`` to the live registry.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

# ── tool schema shaping ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One callable tool: its name, a one-line description, and a JSON-Schema
    ``parameters`` object (OpenAI/JSON-Schema ``{"type":"object", ...}``)."""

    name: str
    description: str
    parameters: dict[str, Any]


def build_tools_param(specs: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Render :class:`ToolSpec`s into the OpenAI ``tools=`` array."""
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            },
        }
        for s in specs
    ]


# ── one chat turn ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A parsed tool call from an assistant turn. ``arguments`` is the decoded
    JSON object (``{}`` when the model emitted empty/invalid JSON)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One assistant response. Carries the raw assistant ``message`` dict so
    the loop can echo it back verbatim (preserving tool-call ids), plus the
    parsed :class:`ToolCall`s to dispatch and any final ``content``."""

    message: dict[str, Any]
    content: str | None
    tool_calls: list[ToolCall]
    total_tokens: int | None
    finish_reason: str | None


class HttpTransport(Protocol):
    """Minimal HTTP-POST seam (mirrors the summarizer's) so the client is
    offline-testable with a scripted fake."""

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]: ...


class _UrllibTransport:
    """Default stdlib transport — one POST, JSON in / JSON out."""

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        out: dict[str, Any] = json.loads(raw)
        return out


def _parse_tool_calls(raw: Any) -> list[ToolCall]:
    """Parse an assistant message's ``tool_calls`` array defensively.

    Each entry is ``{"id", "type":"function", "function":{"name","arguments"}}``
    where ``arguments`` is a JSON *string*. A model that emits malformed
    argument JSON yields ``{}`` rather than crashing the loop — the executor
    then reports the miss and the model can retry.
    """
    if not isinstance(raw, list):
        return []
    calls: list[ToolCall] = []
    for i, tc in enumerate(raw):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        raw_args = fn.get("arguments")
        args: dict[str, Any]
        if isinstance(raw_args, dict):
            args = raw_args
        elif isinstance(raw_args, str) and raw_args.strip():
            try:
                decoded = json.loads(raw_args)
                args = decoded if isinstance(decoded, dict) else {}
            except json.JSONDecodeError:
                args = {}
        else:
            args = {}
        # An id is required to correlate the tool result; synthesize one if the
        # backend omitted it (some OSS servers do on single-call turns).
        call_id = tc.get("id") or f"call_{i}"
        calls.append(ToolCall(id=str(call_id), name=str(name), arguments=args))
    return calls


class ToolChatClient:
    """OpenAI ``/v1/chat/completions`` client that carries ``tools=`` and
    returns a parsed :class:`ChatTurn`.

    Decoupled from the summarizer's ``LlmConfig`` on purpose — the provider
    passes ``url`` / ``api_key`` / ``model`` already resolved (base url from
    ``PRECIS_LLM_BASE_URL``, key from the vault), so this module stays free
    of the worker/DB import chain.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        transport: HttpTransport | None = None,
    ) -> None:
        self._url = url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._transport: HttpTransport = transport or _UrllibTransport()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> ChatTurn:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
        }
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        body = self._transport.post_json(
            self._url, payload, headers=headers, timeout=self._timeout
        )
        try:
            choice = body["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"tool-chat returned no choice: {body!r}") from exc
        content = message.get("content")
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
        usage = body.get("usage") or {}
        total = usage.get("total_tokens")
        return ChatTurn(
            message=dict(message),
            content=content if isinstance(content, str) else None,
            tool_calls=tool_calls,
            total_tokens=int(total) if isinstance(total, int) else None,
            finish_reason=choice.get("finish_reason"),
        )


# ── the multi-turn engine ──────────────────────────────────────────────


class ChatClient(Protocol):
    """The one method :func:`run_tool_loop` needs — so a scripted fake (or a
    future streaming client) drives the engine without being a
    :class:`ToolChatClient`."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = ...,
        tool_choice: str = ...,
    ) -> ChatTurn: ...


#: The tool executor: ``(tool_name, arguments) -> result string``. The loop
#: feeds the returned string back to the model as the tool result. A raised
#: exception is caught and its message fed back (so the model can recover)
#: rather than aborting the run.
ToolExecutor = Callable[[str, dict[str, Any]], str]


@dataclass(frozen=True, slots=True)
class AgentLoopResult:
    """The outcome of :func:`run_tool_loop`, normalized like the ``claude``
    agent result so the provider maps it straight onto ``LlmResult``."""

    final_text: str
    turns_used: int
    tool_calls_made: int
    total_tokens: int | None
    #: ``"stop"`` (model answered) · ``"max_turns"`` (turn ceiling) ·
    #: ``"error"`` (transport failure — ``error`` set).
    stop_reason: str
    error: str | None = None


def _tool_result_message(call: ToolCall, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "content": content}


def run_tool_loop(
    client: ChatClient,
    *,
    prompt: str,
    tools: Sequence[ToolSpec],
    execute: ToolExecutor,
    system_prompt: str | None = None,
    max_turns: int = 20,
    max_total_tokens: int | None = None,
    seed_messages: list[dict[str, Any]] | None = None,
) -> AgentLoopResult:
    """Drive ``client`` through a tool-calling conversation until it answers.

    Each turn: send the running transcript + ``tools`` → if the model
    requests tool calls, run each via ``execute`` (errors captured and fed
    back as the tool result) and loop; otherwise return its text. Bounded by
    ``max_turns`` (hard) and, optionally, ``max_total_tokens``. A transport
    error ends the run with ``stop_reason='error'`` and the partial text.

    ``execute`` and ``tools`` are injected — the engine never imports precis,
    so it is unit-testable with a scripted client + a dict-backed executor.
    """
    messages: list[dict[str, Any]] = list(seed_messages or [])
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    tools_param = build_tools_param(tools)
    total_tokens: int | None = None
    calls_made = 0
    last_text = ""

    def _accumulate(turn_tokens: int | None) -> None:
        nonlocal total_tokens
        if turn_tokens is not None:
            total_tokens = (total_tokens or 0) + turn_tokens

    for turn_no in range(1, max_turns + 1):
        try:
            turn = client.chat(messages, tools=tools_param)
        except (RuntimeError, OSError) as exc:
            return AgentLoopResult(
                final_text=last_text,
                turns_used=turn_no - 1,
                tool_calls_made=calls_made,
                total_tokens=total_tokens,
                stop_reason="error",
                error=str(exc),
            )
        _accumulate(turn.total_tokens)
        if turn.content:
            last_text = turn.content

        if not turn.tool_calls:
            return AgentLoopResult(
                final_text=turn.content or last_text,
                turns_used=turn_no,
                tool_calls_made=calls_made,
                total_tokens=total_tokens,
                stop_reason="stop",
            )

        # Echo the assistant's tool-call message verbatim, then answer each
        # call (in order) with a tool-role message.
        messages.append(turn.message)
        for call in turn.tool_calls:
            calls_made += 1
            try:
                result = execute(call.name, call.arguments)
            except Exception as exc:
                result = f"[tool-error] {type(exc).__name__}: {exc}"
            messages.append(_tool_result_message(call, result))

        if max_total_tokens is not None and (total_tokens or 0) >= max_total_tokens:
            return AgentLoopResult(
                final_text=last_text,
                turns_used=turn_no,
                tool_calls_made=calls_made,
                total_tokens=total_tokens,
                stop_reason="max_turns",
            )

    return AgentLoopResult(
        final_text=last_text,
        turns_used=max_turns,
        tool_calls_made=calls_made,
        total_tokens=total_tokens,
        stop_reason="max_turns",
    )


__all__ = [
    "AgentLoopResult",
    "ChatClient",
    "ChatTurn",
    "HttpTransport",
    "ToolCall",
    "ToolChatClient",
    "ToolExecutor",
    "ToolSpec",
    "build_tools_param",
    "run_tool_loop",
]
