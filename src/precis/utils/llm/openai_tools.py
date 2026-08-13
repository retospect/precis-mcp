"""OpenAI ``tools=`` agent loop — the OSS path to driving precis tools.

This backs the ``OPENAI_TOOLS`` transport: an open-source
model driving the precis verbs over the OpenAI ``/v1/chat/completions``
``tools=`` wire, so agentic work (planner ticks, reviewers) can run off a
hosted or local OSS backend instead of the ``claude -p`` binary. An earlier
pass prototyped an in-process litellm-with-``tools=`` loop and reversed it
onto ``claude``; this is that loop, rebuilt behind the router's provider port.

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
import time
import urllib.request
from collections.abc import Callable, Iterator, Sequence
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


class StreamTimeout(TimeoutError):
    """A streamed completion stalled (idle timeout) or outran its hard ceiling.

    Subclasses ``TimeoutError`` so the router's ``_is_unavailability`` keeps
    classifying it as a transient (→ ``paused``, skip-and-retry) — but unlike
    the blind blocking-POST timeout it carries everything received before the
    abort, so the caller can persist the partial reasoning/content instead of
    losing the whole generation. ``partial_text`` is the assistant content so
    far; ``partial_reasoning`` the reasoning/thinking deltas (OpenRouter's
    ``delta.reasoning`` / some providers' ``delta.reasoning_content``).

    ``drained`` splits the *third* user of this abort path from the two real
    timeouts: a worker-drain abort (``abort_check``) also raises this class to
    reuse the partial-salvage plumbing, but nothing timed out — the same prompt
    on the same rung would have finished fine under the next worker generation.
    Callers that treat a wall-clock timeout as a *deterministic* failure (the
    quest-tick coordinator's give-up budget — see
    :attr:`~precis.quest.tick.QuestTickOutcome.pause_kind`) must not count a
    drain, so the distinction is carried structurally on the exception rather
    than sniffed out of the message text.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_text: str = "",
        partial_reasoning: str = "",
        drained: bool = False,
    ) -> None:
        super().__init__(message)
        self.partial_text = partial_text
        self.partial_reasoning = partial_reasoning
        self.drained = drained


def partial_artifact(exc: BaseException) -> str:
    """Format a caught exception's partial stream (if any) for persistence.

    Duck-typed off ``partial_text`` / ``partial_reasoning`` (not an
    ``isinstance`` on :class:`StreamTimeout`) so any transport that captures
    partials participates. Returns ``""`` when there is nothing to keep, so
    callers can fall back to their previous text unconditionally.
    """
    text = str(getattr(exc, "partial_text", "") or "")
    reasoning = str(getattr(exc, "partial_reasoning", "") or "")
    parts: list[str] = []
    if reasoning:
        parts.append(
            "[partial reasoning — stream aborted before completion]\n" + reasoning
        )
    if text:
        parts.append("[partial content — stream aborted before completion]\n" + text)
    return "\n\n".join(parts)


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
    #: A provider-reported USD cost for this turn (OpenRouter's
    #: ``usage.cost``), when the backend returns one — ``None`` otherwise
    #: (``glm-fleet-flip-safety`` (git-only) Part 2; mirrors how
    #: ``total_tokens`` is read off the same ``usage`` block).
    cost_usd: float | None = None


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

    def post_sse(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        idle_timeout: float,
    ) -> Iterator[dict[str, Any]]:
        """One POST with ``stream: true``, yielding each SSE ``data:`` event.

        ``idle_timeout`` is the *socket* timeout, which for a streamed body is
        an inter-read cap: a connection that goes silent for that long raises
        ``TimeoutError`` mid-iteration, while a model actively emitting tokens
        never trips it. Keep-alive comments, blank lines, and malformed chunks
        are skipped; the OpenAI ``[DONE]`` sentinel ends the stream.
        """
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=idle_timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:") :].strip()
                if chunk == "[DONE]":
                    return
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event


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
        temperature: float | None = 0.0,
        extra_body: dict[str, Any] | None = None,
        transport: HttpTransport | None = None,
        stream: bool = False,
        idle_timeout: float = 120.0,
        abort_check: Callable[[], bool] | None = None,
    ) -> None:
        self._url = url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        #: ``stream=True`` switches :meth:`chat` to the SSE path: deltas are
        #: accumulated as they arrive, ``timeout`` becomes the *hard wall
        #: ceiling* for the whole turn, and ``idle_timeout`` (inter-chunk
        #: silence) is what detects a dead connection — so a model actively
        #: reasoning keeps its connection for as long as ``timeout`` allows,
        #: while a hung one fails in ``idle_timeout`` seconds, and an abort
        #: raises :class:`StreamTimeout` carrying the partial output instead
        #: of losing it. Falls back to the blocking POST when the transport
        #: has no ``post_sse`` (an older fake / custom transport).
        self._stream = stream
        self._idle_timeout = idle_timeout
        self._max_tokens = max_tokens
        #: ``None`` omits ``temperature`` from the wire entirely (the
        #: provider's own default) — the capability tiers + placement chains gen-param passthrough's
        #: MEDIUM/BIG/FRONTIER-tier default, threaded in by
        #: :func:`~precis.utils.llm.router.run_oss_tool_loop`. The class
        #: default (``0.0``) reproduces this client's previous unconditional
        #: ``temperature: 0`` for a caller that doesn't override it.
        self._temperature = temperature
        #: Extra request-body keys merged verbatim onto every turn's payload
        #: (e.g. OpenRouter's ``reasoning: {"enabled": false}`` — see
        #: :func:`~precis.utils.llm.router.openrouter_routing`). ``None`` ⇒
        #: no-op, today's behaviour.
        self._extra_body = extra_body
        #: Polled between SSE events on the streaming path (injected, so this
        #: module stays precis-agnostic — the worker passes
        #: ``precis.liveness.drain_requested``). When it flips True the turn
        #: aborts with a :class:`StreamTimeout` carrying the partials, riding
        #: the exact salvage path an idle-timeout abort already takes
        #: (partial kept → ``paused`` → retry by the next worker generation).
        #: ``None`` (the default) = never aborts, today's behaviour.
        self._abort_check = abort_check
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
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if self._extra_body:
            payload.update(self._extra_body)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        sse = getattr(self._transport, "post_sse", None) if self._stream else None
        if sse is not None:
            return self._chat_streaming(payload, headers, sse)
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
        cost = usage.get("cost")
        return ChatTurn(
            message=dict(message),
            content=content if isinstance(content, str) else None,
            tool_calls=tool_calls,
            total_tokens=int(total) if isinstance(total, int) else None,
            finish_reason=choice.get("finish_reason"),
            cost_usd=float(cost) if isinstance(cost, int | float) else None,
        )

    def _chat_streaming(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        sse: Callable[..., Iterator[dict[str, Any]]],
    ) -> ChatTurn:
        """Drive one turn over SSE, accumulating deltas into a :class:`ChatTurn`.

        Content / reasoning / tool-call fragments are folded as they arrive
        (tool-call ``arguments`` stream as string fragments keyed by ``index``;
        ``usage`` rides the final chunk when the backend reports it). Two
        aborts, both raising :class:`StreamTimeout` with the partials attached:
        the transport's idle timeout (silence — a dead connection), and the
        hard wall ceiling ``self._timeout`` checked between events (so the
        overrun past the ceiling is bounded by one idle-timeout, not infinite).
        A non-timeout transport failure (connection reset, HTTP error mapped by
        urllib) propagates unchanged — wrapping it here would misclassify a
        semantic 4xx as a transient.
        """
        payload = dict(payload)
        payload["stream"] = True
        started = time.monotonic()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls_by_index: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        total: int | None = None
        cost: float | None = None

        def _abort(reason: str, *, drained: bool = False) -> StreamTimeout:
            return StreamTimeout(
                reason,
                partial_text="".join(content_parts),
                partial_reasoning="".join(reasoning_parts),
                drained=drained,
            )

        try:
            for event in sse(
                self._url, payload, headers=headers, idle_timeout=self._idle_timeout
            ):
                if self._abort_check is not None and self._abort_check():
                    raise _abort(
                        "worker draining: stream aborted between chunks (partial kept)",
                        drained=True,
                    )
                usage = event.get("usage") or {}
                if isinstance(usage.get("total_tokens"), int):
                    total = usage["total_tokens"]
                if isinstance(usage.get("cost"), int | float):
                    cost = float(usage["cost"])
                choices = event.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    choice = choices[0]
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, str):
                        content_parts.append(piece)
                    thought = delta.get("reasoning") or delta.get("reasoning_content")
                    if isinstance(thought, str):
                        reasoning_parts.append(thought)
                    for tc in delta.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index")
                        idx = idx if isinstance(idx, int) else 0
                        slot = calls_by_index.setdefault(
                            idx,
                            {
                                "id": None,
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if isinstance(fn.get("name"), str):
                            slot["function"]["name"] += fn["name"]
                        if isinstance(fn.get("arguments"), str):
                            slot["function"]["arguments"] += fn["arguments"]
                if self._timeout and (time.monotonic() - started) > self._timeout:
                    raise _abort(
                        f"streamed completion exceeded the {self._timeout:.0f}s "
                        "hard ceiling (still generating — partial kept)"
                    )
        except StreamTimeout:
            raise
        except TimeoutError as exc:
            raise _abort(
                f"stream went silent for {self._idle_timeout:.0f}s: {exc}"
            ) from exc

        raw_calls = [
            calls_by_index[i]
            for i in sorted(calls_by_index)
            if calls_by_index[i]["function"]["name"]
        ]
        content = "".join(content_parts)
        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if raw_calls:
            message["tool_calls"] = raw_calls
        return ChatTurn(
            message=message,
            content=content or None,
            tool_calls=_parse_tool_calls(raw_calls),
            total_tokens=total,
            finish_reason=finish_reason,
            cost_usd=cost,
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
    #: ``True`` when ``error`` is a transport *unavailability* (a request
    #: timeout, connection failure, or HTTP 5xx/429) rather than a genuine
    #: semantic failure (a malformed/unauthorized 4xx request) — mirrors
    #: :attr:`~precis.utils.llm.router.LlmResult.paused`, which
    #: :func:`~precis.utils.llm.router._dispatch_openai_tools` threads this
    #: onto so a pinned pass backs off and retries instead of recording a
    #: dispatch failure that can park the todo. ``False`` (the default) for a
    #: clean run, a semantic error, or any exception not recognized as a known
    #: transient signal.
    paused: bool = False
    #: ``True`` when the run ended on a *wall-clock timeout* — the streamed
    #: hard ceiling or the idle timeout (:class:`StreamTimeout`), or a blocking
    #: POST's socket timeout — as opposed to the other unavailabilities that
    #: also set :attr:`paused` (a 5xx/429, a connection failure, a worker
    #: drain). A strict refinement of ``paused``: every ``timed_out`` run is
    #: also ``paused``, never the reverse.
    #:
    #: Threaded onto :attr:`~precis.utils.llm.router.LlmResult.timed_out` so a
    #: caller can tell the two apart *structurally*. The distinction matters
    #: because they retry differently: a 429 or a drain clears on its own, but
    #: re-sending an identical prompt to an identical rung that just ran out of
    #: wall clock burns the identical wall clock again (2026-08-13: the
    #: ``quest_tick`` BIG chain's cloud rung tripped the 900s ceiling 10× in 7
    #: days, always after generating 12k–33k chars, and never once succeeded on
    #: the retry).
    timed_out: bool = False
    #: Summed :attr:`ChatTurn.cost_usd` across every turn — ``None`` when no
    #: turn reported one (a backend that doesn't return ``usage.cost``, e.g. a
    #: local/loopback server). Mirrors :attr:`total_tokens`'s accumulation so
    #: the ``openai_tools`` transport can meter real spend
    #: (``glm-fleet-flip-safety`` (git-only) Part 2).
    cost_usd: float | None = None


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
    abort_check: Callable[[], bool] | None = None,
) -> AgentLoopResult:
    """Drive ``client`` through a tool-calling conversation until it answers.

    Each turn: send the running transcript + ``tools`` → if the model
    requests tool calls, run each via ``execute`` (errors captured and fed
    back as the tool result) and loop; otherwise return its text. Bounded by
    ``max_turns`` (hard) and, optionally, ``max_total_tokens``. A transport
    error ends the run with ``stop_reason='error'`` and the partial text.

    ``execute`` and ``tools`` are injected — the engine never imports precis,
    so it is unit-testable with a scripted client + a dict-backed executor.

    ``abort_check`` (injected, same seam as :class:`ToolChatClient`'s) is
    polled before each turn: a draining worker stops starting new turns and
    returns the text so far as a ``paused`` unavailability (``stop_reason=
    'error'``) — the job retries under the next worker generation instead of
    holding this one past its stop timeout.
    """
    messages: list[dict[str, Any]] = list(seed_messages or [])
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    tools_param = build_tools_param(tools)
    total_tokens: int | None = None
    total_cost: float | None = None
    calls_made = 0
    last_text = ""

    def _accumulate(turn_tokens: int | None, turn_cost: float | None) -> None:
        nonlocal total_tokens, total_cost
        if turn_tokens is not None:
            total_tokens = (total_tokens or 0) + turn_tokens
        if turn_cost is not None:
            total_cost = (total_cost or 0.0) + turn_cost

    for turn_no in range(1, max_turns + 1):
        if abort_check is not None and abort_check():
            return AgentLoopResult(
                final_text=last_text,
                turns_used=turn_no - 1,
                tool_calls_made=calls_made,
                total_tokens=total_tokens,
                stop_reason="error",
                error="worker draining: agent loop aborted before next turn",
                paused=True,
                cost_usd=total_cost,
            )
        try:
            turn = client.chat(messages, tools=tools_param)
        except (RuntimeError, OSError) as exc:
            # Classify via the router's shared taxonomy: a
            # timeout / connection failure / 5xx-or-429 is unavailability
            # (skip-and-retry), a 4xx-non-429 is a genuine semantic failure.
            # Local import — `router` is the module that calls into this loop
            # (via `run_oss_tool_loop`), so it's already fully imported by the
            # time any exception reaches here; importing it at module level
            # would pull router's heavier import chain into this
            # precis-agnostic, offline-testable module for every caller, not
            # just the ones that hit this branch.
            from precis.utils.llm.router import _is_unavailability

            # A streamed turn that timed out mid-generation carries its
            # partial output (StreamTimeout) — keep it over the previous
            # turn's text so the caller can persist what the model produced
            # before the abort instead of losing the whole generation.
            return AgentLoopResult(
                final_text=partial_artifact(exc) or last_text,
                turns_used=turn_no - 1,
                tool_calls_made=calls_made,
                total_tokens=total_tokens,
                stop_reason="error",
                error=str(exc),
                paused=_is_unavailability(exc),
                # The wall-clock-timeout refinement of ``paused`` (see
                # :attr:`AgentLoopResult.timed_out`): every timeout is a
                # ``TimeoutError`` subclass, but a drain abort borrows
                # :class:`StreamTimeout` purely for the partial salvage and is
                # NOT a timeout — it flags itself ``drained`` so it's excluded
                # here without anyone parsing the message text.
                timed_out=isinstance(exc, TimeoutError)
                and not getattr(exc, "drained", False),
                cost_usd=total_cost,
            )
        _accumulate(turn.total_tokens, turn.cost_usd)
        if turn.content:
            last_text = turn.content

        if not turn.tool_calls:
            return AgentLoopResult(
                final_text=turn.content or last_text,
                turns_used=turn_no,
                tool_calls_made=calls_made,
                total_tokens=total_tokens,
                stop_reason="stop",
                cost_usd=total_cost,
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
                cost_usd=total_cost,
            )

    return AgentLoopResult(
        final_text=last_text,
        turns_used=max_turns,
        tool_calls_made=calls_made,
        total_tokens=total_tokens,
        stop_reason="max_turns",
        cost_usd=total_cost,
    )


__all__ = [
    "AgentLoopResult",
    "ChatClient",
    "ChatTurn",
    "HttpTransport",
    "StreamTimeout",
    "ToolCall",
    "ToolChatClient",
    "ToolExecutor",
    "ToolSpec",
    "build_tools_param",
    "partial_artifact",
    "run_tool_loop",
]
