#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Anthropic-Messages shim in front of castor's local server, for
``scripts/claude-local``.

Two upstream modes:

* ``messages`` — pass ``/v1/messages`` through after rewriting turns vLLM's
  schema rejects (system-role turns become tagged user turns, adjacent same
  roles merge, echoed thinking blocks are dropped).
* ``chat`` — translate ``/v1/messages`` to ``/v1/chat/completions`` and back:
  tools become OpenAI function tools, ``tool_use``/``tool_result`` blocks
  become ``tool_calls``/``tool`` messages, and the SSE stream is re-emitted
  as Anthropic events. Needed on SGLang's gpt-oss path, where ``/v1/messages``
  answers every turn after a tool result with a thinking block only
  (gr500583) while ``/v1/chat/completions`` keeps calling tools.

In chat mode the Anthropic ``system`` field goes out per ``--system-role``
(env ``ANTHROPIC_SHIM_SYSTEM_ROLE``): ``system`` sends a system message,
``fold`` prepends it to the first user turn, ``auto`` (default) picks
``system`` for gpt-oss models and ``fold`` otherwise. Folding came from
gr500583; a 2026-10-22 probe on castor (task prompt, Read+Bash tools) called a
tool on turn 1 in 4/5 requests with a system message vs 2/5 folded; after a
tool result both dropped every call (the drop below, not a system-role stall).
Chat mode also retries a turn whose tool call SGLang dropped (gpt-oss wrote
it on the analysis channel; the turn ends on ``<|call|>`` with no
tool_calls) up to ``ANTHROPIC_SHIM_DROPPED_CALL_RETRIES`` (3) times. With
salvage on (``--salvage``, env ``ANTHROPIC_SHIM_SALVAGE``: ``auto`` = on
for gpt-oss) each retry asks for raw harmony and turns the dropped call into
a tool_use. When retries run dry the client gets a non-retryable 502 instead
of an empty end_turn. Every retry/salvage is logged with running counts.
``count_tokens`` is a chars/4 estimate in both modes.

Stdlib only; no dependency on the precis package. Runs anywhere that can
reach castor; ``scripts/claude-local`` starts it on demand on loopback.

    scripts/anthropic-shim.py <listen-port> <upstream-host> <upstream-port> [--mode auto|chat|messages] [--system-role auto|system|fold] [--salvage auto|on|off]

``auto`` (default) probes ``GET /get_model_info``: SGLang answers it (vLLM
does not), so the shim picks ``chat`` for SGLang and ``messages`` otherwise.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "host",
    "content-length",
    "accept-encoding",
}


def _as_blocks(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": json.dumps(content)}]


_THINKING = {"thinking", "redacted_thinking"}


def rewrite_messages(messages: list[dict]) -> list[dict]:
    """``system`` turns become tagged ``user`` turns; adjacent same roles merge.

    Echoed ``thinking`` blocks are dropped: SGLang's ``/v1/messages`` answers a
    request carrying one with HTTP 500, and in chat mode they cannot be sent
    at all (chat/completions has no thinking field).
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        blocks = [
            b for b in _as_blocks(m.get("content")) if b.get("type") not in _THINKING
        ]
        if not blocks:
            continue
        if role == "system":
            role = "user"
            blocks = [{"type": "text", "text": "[harness system turn]"}, *blocks]
        if out and out[-1]["role"] == role:
            out[-1]["content"] = [*out[-1]["content"], *blocks]
        else:
            out.append({"role": role, "content": blocks})
    return out


def _estimate_tokens(body: dict) -> int:
    text = json.dumps(body.get("system", "")) + json.dumps(body.get("messages", []))
    text += json.dumps(body.get("tools", []))
    return max(1, len(text) // 4)


# --- Anthropic Messages -> OpenAI chat/completions ---------------------------


def _block_text(blocks: list[dict]) -> str:
    return "\n".join(
        str(b.get("text", "")) for b in blocks if b.get("type") == "text"
    ).strip()


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = _block_text(content)
    else:
        text = "" if content is None else json.dumps(content)
    if block.get("is_error"):
        text = f"[error] {text}"
    return text


SYSTEM_ROLES = ("auto", "system", "fold")
SYSTEM_ROLE = os.environ.get("ANTHROPIC_SHIM_SYSTEM_ROLE", "auto")


def _system_as_message(model: str, system_role: str) -> bool:
    if system_role == "auto":
        return "gpt-oss" in model.lower()
    return system_role == "system"


def _to_chat_messages(body: dict, system_role: str) -> list[dict]:
    """Translate Anthropic ``messages`` to OpenAI chat messages.

    ``tool_use`` blocks become assistant ``tool_calls``; ``tool_result``
    blocks become ``tool`` messages carrying the matching ``tool_call_id``.
    """
    out: list[dict] = []
    for m in rewrite_messages(body.get("messages") or []):
        role, blocks = m["role"], m["content"]
        if role == "assistant":
            msg: dict = {"role": "assistant", "content": _block_text(blocks)}
            calls = [
                {
                    "id": b.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input") or {}),
                    },
                }
                for b in blocks
                if b.get("type") == "tool_use"
            ]
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        # ``tool`` messages must directly follow the assistant ``tool_calls``
        # turn, so all tool_result blocks go first; user text follows them.
        pending: list[str] = []
        tools: list[dict] = []
        for b in blocks:
            if b.get("type") == "tool_result":
                tools.append(
                    {
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id", ""),
                        "content": _tool_result_text(b),
                    }
                )
            elif b.get("type") == "text":
                if b.get("text"):
                    pending.append(str(b["text"]))
            else:
                pending.append(f"[{b.get('type', 'block')} omitted]")
        out.extend(tools)
        if pending:
            out.append({"role": "user", "content": "\n".join(pending)})
    system = _block_text(_as_blocks(body.get("system") or []))
    if system and _system_as_message(str(body.get("model") or ""), system_role):
        out.insert(0, {"role": "system", "content": system})
    elif system:
        for msg in out:
            if msg["role"] == "user":
                msg["content"] = f"{system}\n\n{msg['content']}"
                break
        else:
            out.insert(0, {"role": "user", "content": system})
    return out


_TOOL_CHOICE = {"auto": "auto", "any": "required", "none": "none"}


def to_chat_body(body: dict, system_role: str | None = None) -> dict:
    """Translate a ``/v1/messages`` request body to ``/v1/chat/completions``."""
    out: dict = {"messages": _to_chat_messages(body, system_role or SYSTEM_ROLE)}
    for src, dst in (
        ("model", "model"),
        ("max_tokens", "max_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("top_k", "top_k"),
    ):
        if body.get(src) is not None:
            out[dst] = body[src]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("tools"):
        out["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {},
                },
            }
            for t in body["tools"]
        ]
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        if choice.get("type") == "tool":
            out["tool_choice"] = {
                "type": "function",
                "function": {"name": choice.get("name", "")},
            }
        elif choice.get("type") in _TOOL_CHOICE:
            out["tool_choice"] = _TOOL_CHOICE[choice["type"]]
    if body.get("stream"):
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    return out


# --- OpenAI chat/completions -> Anthropic Messages ----------------------------


def _stop_reason(finish: str | None, tool_calls: bool) -> str:
    if finish == "length":
        return "max_tokens"
    if finish == "tool_calls" or (tool_calls and finish in (None, "stop")):
        return "tool_use"
    if finish == "content_filter":
        return "refusal"
    return "end_turn"


def _content_blocks(msg: dict) -> list[dict]:
    """OpenAI response message -> Anthropic content blocks (in order)."""
    blocks: list[dict] = []
    if msg.get("reasoning_content"):
        blocks.append(
            {
                "type": "thinking",
                "thinking": msg["reasoning_content"],
                "signature": "",
            }
        )
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        raw = (tc.get("function") or {}).get("arguments") or ""
        try:
            args = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            args = {"_unparsed": raw}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": (tc.get("function") or {}).get("name", ""),
                "input": args,
            }
        )
    return blocks or [{"type": "text", "text": ""}]


def to_anthropic_response(chat: dict, model: str) -> dict:
    """Translate a non-streaming chat/completions body to a Messages reply."""
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks = _content_blocks(msg)
    usage = chat.get("usage") or {}
    return {
        "id": chat.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": _stop_reason(
            choice.get("finish_reason"),
            any(b["type"] == "tool_use" for b in blocks),
        ),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# --- dropped tool calls: detect and retry --------------------------------------
#
# gpt-oss on SGLang's gpt-oss tool parser sometimes writes its tool call on
# the harmony analysis channel (``<|channel|>analysis to=functions.X ...
# <|call|>``); the parser reads only commentary, so the call is lost and the
# turn ends finish_reason "stop" on ``<|call|>`` (token 200012) with
# tool_calls null. Passed through, Claude Code ends the run "success" with no
# work done. The shim re-sends such a turn and errors out when retries run dry.

CALL_TOKEN_ID = 200012  # harmony <|call|>
DROPPED_CALL_RETRIES = int(os.environ.get("ANTHROPIC_SHIM_DROPPED_CALL_RETRIES", "3"))


class DroppedToolCall(Exception):
    """Every attempt ended in a dropped tool call; the client gets an error."""


class UpstreamError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def dropped_call_reason(
    finish: str | None,
    matched_stop: object,
    content: str,
    reasoning: bool,
    tool_calls: bool,
) -> str | None:
    """Why a finished turn looks like a dropped tool call, or None if it is fine."""
    if tool_calls or finish != "stop":
        return None
    if matched_stop == CALL_TOKEN_ID:
        return (
            f"matched_stop {CALL_TOKEN_ID} (<|call|>) with no tool_calls, "
            f"{len(content)} chars of text"
        )
    if "<|call|>" in content:
        return f"<|call|> in {len(content)} chars of text, no tool_calls"
    if reasoning and not content.strip():
        return "reasoning only, no text or tool_calls"
    return None


def _chat_dropped_call(chat: dict) -> str | None:
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return dropped_call_reason(
        choice.get("finish_reason"),
        choice.get("matched_stop"),
        msg.get("content") or "",
        bool(msg.get("reasoning_content")),
        bool(msg.get("tool_calls")),
    )


def _sse(event: str, data: dict) -> tuple[str, dict]:
    return (event, data)


class ChatStreamTranslator:
    """OpenAI chat/completions SSE chunks -> Anthropic Messages SSE events.

    Feed each decoded ``data:`` object to :meth:`feed`; collect the returned
    ``(event, data)`` pairs. Call :meth:`close` on ``[DONE]`` or EOF so a
    stream that ends without ``finish_reason`` still gets ``message_stop``.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.started = False
        self.done = False
        self.next_index = 0
        self.open_index: int | None = None
        self.open_kind: str | None = None  # thinking | text | tool_use
        self.open_tc: int | None = None  # upstream tool_calls index of the open block
        self.saw_tool = False
        self.output_tokens = 0
        # what dropped_call() needs once the turn is over
        self.finish: str | None = None
        self.matched_stop: object = None
        self.text = ""
        self.saw_thinking = False

    def dropped_call(self) -> str | None:
        """:func:`dropped_call_reason` for the streamed turn so far."""
        return dropped_call_reason(
            self.finish,
            self.matched_stop,
            self.text,
            self.saw_thinking,
            self.saw_tool,
        )

    def _start(self) -> list[tuple[str, dict]]:
        self.started = True
        return [
            _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
            ),
            _sse("ping", {"type": "ping"}),
        ]

    def _close_block(self) -> list[tuple[str, dict]]:
        if self.open_index is None:
            return []
        out = [
            _sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": self.open_index},
            )
        ]
        self.open_index = self.open_kind = self.open_tc = None
        return out

    def _open_block(self, kind: str, block: dict) -> list[tuple[str, dict]]:
        out = self._close_block()
        index = self.next_index
        self.next_index += 1
        self.open_index, self.open_kind = index, kind
        out.append(
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": block,
                },
            )
        )
        return out

    def _delta(self, delta: dict) -> list[tuple[str, dict]]:
        return [
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.open_index,
                    "delta": delta,
                },
            )
        ]

    def feed(self, chunk: dict) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        if isinstance(chunk.get("id"), str):
            self.msg_id = chunk["id"]
        if not self.started:
            out += self._start()
        usage = chunk.get("usage") or {}
        if usage.get("completion_tokens") is not None:
            self.output_tokens = usage["completion_tokens"]
        for choice in chunk.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if delta.get("reasoning_content"):
                self.saw_thinking = True
                if self.open_kind != "thinking":
                    out += self._open_block(
                        "thinking", {"type": "thinking", "thinking": ""}
                    )
                out += self._delta(
                    {
                        "type": "thinking_delta",
                        "thinking": delta["reasoning_content"],
                    }
                )
            if delta.get("content"):
                self.text += delta["content"]
                if self.open_kind != "text":
                    out += self._open_block("text", {"type": "text", "text": ""})
                out += self._delta({"type": "text_delta", "text": delta["content"]})
            for tc in delta.get("tool_calls") or []:
                # A new tool-call index starts a new content block; further
                # deltas for the open index carry only argument fragments.
                tc_index = tc.get("index", 0)
                fn = tc.get("function") or {}
                if self.open_kind != "tool_use" or self.open_tc != tc_index:
                    out += self._open_block(
                        "tool_use",
                        {
                            "type": "tool_use",
                            "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                            "name": fn.get("name", ""),
                            "input": {},
                        },
                    )
                    self.open_tc = tc_index
                    self.saw_tool = True
                if fn.get("arguments"):
                    out += self._delta(
                        {
                            "type": "input_json_delta",
                            "partial_json": fn["arguments"],
                        }
                    )
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]
                self.matched_stop = choice.get("matched_stop")
                out += self._close_block()
                out.append(
                    _sse(
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {
                                "stop_reason": _stop_reason(
                                    choice["finish_reason"], self.saw_tool
                                ),
                                "stop_sequence": None,
                            },
                            "usage": {"output_tokens": self.output_tokens},
                        },
                    )
                )
                out.append(_sse("message_stop", {"type": "message_stop"}))
                self.done = True
        return out

    def close(self) -> list[tuple[str, dict]]:
        if self.done:
            return []
        out = [] if self.started else self._start()
        out += self._close_block()
        out.append(
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": self.output_tokens},
                },
            )
        )
        out.append(_sse("message_stop", {"type": "message_stop"}))
        self.done = True
        return out


# --- salvage: read a dropped call back from raw harmony -------------------------
#
# Asked for raw output (no reasoning split, special tokens kept, stop token
# not trimmed, per-token logprobs), SGLang returns the harmony text that still
# holds the call the parser dropped: ``<|channel|>commentary
# to=functions.read code<|message|>{"file_path": "..."}<|call|>``. With
# Claude Code's full tool set the parser swallows content and reasoning too
# (both empty, ~100 completion tokens), so the text is rebuilt from the
# logprob tokens; content is the fallback. Castor 2026-10-22: the dropped
# calls named real tools in lowercase (``read`` for ``Read``) or invented
# ones (``search``). On a dropped turn the shim's retries ask for that and
# turn the LAST such call into a tool_use, provided its name matches one of
# the request's tools (case-insensitively) and its arguments are a JSON
# object; otherwise the retry counts as dropped. It never invents a tool.

SALVAGE_SETTINGS = ("auto", "on", "off")
SALVAGE = os.environ.get("ANTHROPIC_SHIM_SALVAGE", "auto")
RAW_HARMONY = {
    "separate_reasoning": False,
    "skip_special_tokens": False,
    "no_stop_trim": True,
    "logprobs": True,
}
_HARMONY_CALL = re.compile(
    r"to=functions\.([\w.\-]+)(?:(?!<\|message\|>).)*<\|message\|>(.*?)(?:<\|call\|>|\Z)",
    re.S,
)
_HARMONY_ANALYSIS = re.compile(
    r"<\|channel\|>analysis<\|message\|>(.*?)(?:<\|end\|>|<\|start\|>|\Z)", re.S
)


def salvage_on(model: str, setting: str) -> bool:
    if setting == "auto":
        return "gpt-oss" in model.lower()
    return setting == "on"


def _tool_lookup(names: Iterable[str]) -> Callable[[str], str | None]:
    exact = set(names)
    folded: dict[str, str] = {}
    for name in sorted(exact):
        folded.setdefault(name.lower(), name)

    def canon(name: str) -> str | None:
        return name if name in exact else folded.get(name.lower())

    return canon


def _json_object(raw: str) -> dict | None:
    try:
        value = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def salvage_call(raw: dict, tool_names: Iterable[str]) -> tuple[dict | None, str]:
    """A raw-harmony chat/completions reply -> (normal tool-call reply, how).

    Returns ``(None, why)`` when nothing safe can be recovered.
    """
    choice = (raw.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    tokens = ((choice.get("logprobs") or {}).get("content")) or []
    text = "".join(str(t.get("token", "")) for t in tokens) or (
        (msg.get("reasoning_content") or "") + (msg.get("content") or "")
    )
    canon = _tool_lookup(tool_names)
    calls: list[tuple[str, dict]] = []
    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            fn = tc.get("function") or {}
            name = canon(str(fn.get("name", "")))
            if name is None:
                return None, f"unknown tool {fn.get('name')!r} in raw tool_calls"
            args = _json_object(fn.get("arguments") or "")
            if args is None:
                return None, f"{name}: raw tool_calls args are not a JSON object"
            calls.append((name, args))
        how = "raw reply carried parsed tool_calls"
    else:
        matches = list(_HARMONY_CALL.finditer(text))
        if not matches:
            return None, (
                f"no harmony tool call in raw reply ({len(text)} chars, "
                f"ends {text[-160:]!r})"
            )
        said, body = matches[-1].group(1), matches[-1].group(2)
        name = canon(said)
        if name is None:
            return None, f"unknown tool {said!r} in harmony text"
        args = _json_object(body)
        if args is None:
            return None, f"{said}: harmony call args are not a JSON object"
        calls.append((name, args))
        how = f"functions.{said} -> {name} from harmony text"
    reasoning = "\n".join(
        m.strip() for m in _HARMONY_ANALYSIS.findall(text) if m.strip()
    ) or ("" if "<|" in text else text.strip())
    chat = {
        "id": raw.get("id"),
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "reasoning_content": reasoning or None,
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for name, args in calls
                    ],
                },
            }
        ],
        "usage": raw.get("usage") or {},
    }
    return chat, how


def chat_events(chat: dict, model: str) -> list[tuple[str, dict]]:
    """A complete chat/completions reply as Anthropic SSE events."""
    msg = ((chat.get("choices") or [{}])[0]).get("message") or {}
    chunks: list[dict] = []
    if msg.get("reasoning_content"):
        chunks.append(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": msg["reasoning_content"]},
                    }
                ]
            }
        )
    if msg.get("content"):
        chunks.append({"choices": [{"index": 0, "delta": {"content": msg["content"]}}]})
    calls = [{**tc, "index": i} for i, tc in enumerate(msg.get("tool_calls") or [])]
    if calls:
        chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": calls}}]})
    finish = (chat.get("choices") or [{}])[0].get("finish_reason") or "stop"
    chunks.append(
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": chat.get("usage") or {},
        }
    )
    translator = ChatStreamTranslator(model)
    if isinstance(chat.get("id"), str):
        translator.msg_id = chat["id"]
    out: list[tuple[str, dict]] = []
    for chunk in chunks:
        out += translator.feed(chunk)
    return out + translator.close()


# --- retry driver ---------------------------------------------------------------

RetryLog = Callable[
    [str, str], None
]  # (event: retry | salvaged | salvage_refused, detail)


def _salvage_retry(
    send_raw: Callable[[], dict], tool_names: Iterable[str], log: RetryLog
) -> tuple[dict | None, str]:
    chat, how = salvage_call(send_raw(), tool_names)
    log("salvaged" if chat is not None else "salvage_refused", how)
    return chat, how


def complete_with_retry(
    send: Callable[[], dict],
    model: str,
    retries: int,
    log: RetryLog,
    send_raw: Callable[[], dict] | None = None,
    tool_names: Iterable[str] = (),
) -> dict:
    """Non-streaming turn: retry while the reply is a dropped tool call.

    ``send`` returns one upstream chat/completions body. With ``send_raw``
    (a raw-harmony request) each retry salvages from it instead of re-sending
    ``send``. Raises :class:`DroppedToolCall` once ``retries`` retries failed.
    """
    reason = ""
    for attempt in range(retries + 1):
        if attempt:
            log("retry", f"{attempt}/{retries}: {reason}")
            if send_raw is not None:
                salvaged, reason = _salvage_retry(send_raw, tool_names, log)
                if salvaged is not None:
                    return to_anthropic_response(salvaged, model)
                continue
        chat = send()
        dropped = _chat_dropped_call(chat)
        if dropped is None:
            return to_anthropic_response(chat, model)
        reason = dropped
    raise DroppedToolCall(_dropped_message(retries, reason))


def stream_with_retry(
    open_stream: Callable[[], Iterable[dict]],
    model: str,
    retries: int,
    log: RetryLog,
    send_raw: Callable[[], dict] | None = None,
    tool_names: Iterable[str] = (),
) -> Iterator[tuple[str, dict]]:
    """Streaming turn: Anthropic SSE events, retrying dropped tool calls.

    A turn's events are held back until it either streams a tool call (then
    it is good and the rest passes straight through) or finishes; a finished
    turn that dropped its call is discarded and retried (a salvage from
    ``send_raw`` when given, else ``open_stream`` again). Nothing is yielded
    before that decision, so :class:`DroppedToolCall` (and any error from
    ``open_stream``) raises before the first event.
    """
    reason = ""
    for attempt in range(retries + 1):
        if attempt:
            log("retry", f"{attempt}/{retries}: {reason}")
            if send_raw is not None:
                salvaged, reason = _salvage_retry(send_raw, tool_names, log)
                if salvaged is not None:
                    yield from chat_events(salvaged, model)
                    return
                continue
        translator = ChatStreamTranslator(model)
        held: list[tuple[str, dict]] = []
        live = False
        for chunk in open_stream():
            events = translator.feed(chunk)
            if live:
                yield from events
            elif translator.saw_tool:
                live = True
                yield from held + events
                held = []
            else:
                held += events
        tail = translator.close()
        dropped = None if live else translator.dropped_call()
        if dropped is None:
            yield from held + tail
            return
        reason = dropped
    raise DroppedToolCall(_dropped_message(retries, reason))


def _dropped_message(retries: int, reason: str) -> str:
    return (
        f"upstream dropped the model's tool call on {retries + 1} attempts "
        f"(last: {reason}). gpt-oss wrote the call on the harmony analysis "
        "channel, which SGLang's gpt-oss tool parser ignores; see "
        "docs/backlog/castor-agent-lane.md (slice 0a)"
    )


# --- HTTP plumbing -------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    upstream_host = "127.0.0.1"
    upstream_port = 8000
    mode = "auto"  # auto | chat | messages
    system_role = SYSTEM_ROLE  # auto | system | fold
    salvage = SALVAGE  # auto | on | off
    protocol_version = "HTTP/1.1"
    # dropped-call outcomes since start: retry | salvaged | salvage_refused | exhausted
    stats: Counter[str] = Counter()
    stats_lock = threading.Lock()

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("shim: " + (fmt % args) + "\n")

    def _log_dropped(self, event: str, detail: str) -> None:
        with Handler.stats_lock:
            Handler.stats[event] += 1
            counts = " ".join(
                f"{k}={Handler.stats[k]}"
                for k in ("retry", "salvaged", "salvage_refused", "exhausted")
            )
        self.log_message("dropped call %s: %s [%s]", event, detail, counts)

    def _reply_json(self, status: int, obj: dict, retry: bool | None = None) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        if retry is not None:
            # Anthropic SDK clients honour this over their own status rules.
            self.send_header("x-should-retry", "true" if retry else "false")
        self.end_headers()
        self.wfile.write(data)

    def _reply_error(
        self, status: int, message: str, retry: bool | None = None
    ) -> None:
        self._reply_json(
            status,
            {"type": "error", "error": {"type": "api_error", "message": message}},
            retry,
        )

    def _chat_mode(self) -> bool:
        if Handler.mode != "auto":
            return Handler.mode == "chat"
        Handler.mode = "chat" if _upstream_is_sglang() else "messages"
        self.log_message("upstream mode: %s", Handler.mode)
        return Handler.mode == "chat"

    def do_GET(self) -> None:  # health passthrough
        conn = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=10
        )
        try:
            conn.request("GET", self.path)
            resp = conn.getresponse()
            data = resp.read()
        except OSError as exc:
            self._reply_error(502, str(exc))
            return
        finally:
            conn.close()
        self.send_response(resp.status)
        self.send_header("content-type", resp.getheader("content-type") or "text/plain")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        path = self.path.split("?", 1)[0]
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._reply_json(
                400,
                {
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "bad json"},
                },
            )
            return

        if path == "/v1/messages/count_tokens":
            self._reply_json(200, {"input_tokens": _estimate_tokens(body)})
            return
        if path == "/v1/messages" and isinstance(body.get("messages"), list):
            if self._chat_mode():
                self._forward_chat(body)
                return
            body["messages"] = rewrite_messages(body["messages"])
            # Claude Code's beta/metadata fields vLLM does not model.
            body.pop("metadata", None)
        self._forward(path, body)

    def _forward_chat(self, body: dict) -> None:
        """Translate ``/v1/messages`` to ``/v1/chat/completions`` and back.

        A turn whose tool call the upstream dropped is retried up to
        :data:`DROPPED_CALL_RETRIES` times (as a raw-harmony salvage when
        salvage is on for the model); after that the client gets a 502 naming
        the cause instead of an empty end_turn.
        """
        chat_body = to_chat_body(body, Handler.system_role)
        model = str(body.get("model") or "unknown")
        base_headers = {
            k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP
        }

        def encode(payload: dict) -> tuple[bytes, dict]:
            data = json.dumps(payload).encode("utf-8")
            return data, {
                **base_headers,
                "content-length": str(len(data)),
                "content-type": "application/json",
            }

        data, headers = encode(chat_body)
        send_raw: Callable[[], dict] | None = None
        if salvage_on(model, Handler.salvage):
            raw_body = {**chat_body, **RAW_HARMONY, "stream": False}
            raw_body.pop("stream_options", None)
            raw_data, raw_headers = encode(raw_body)

            def _send_raw() -> dict:
                return self._chat_json(raw_data, raw_headers)

            send_raw = _send_raw

        tool_names = [
            str((t.get("function") or {}).get("name", ""))
            for t in chat_body.get("tools") or []
        ]
        try:
            if chat_body.get("stream"):
                events = stream_with_retry(
                    lambda: self._chat_chunks(data, headers),
                    model,
                    DROPPED_CALL_RETRIES,
                    self._log_dropped,
                    send_raw,
                    tool_names,
                )
                first = next(events)
                self._relay_stream(first, events)
            else:
                reply = complete_with_retry(
                    lambda: self._chat_json(data, headers),
                    model,
                    DROPPED_CALL_RETRIES,
                    self._log_dropped,
                    send_raw,
                    tool_names,
                )
                self._reply_json(200, reply)
        except UpstreamError as exc:
            self._reply_error(exc.status, str(exc))
        except DroppedToolCall as exc:
            self._log_dropped("exhausted", str(exc))
            # The shim already retried; a client retry loop on top would
            # multiply load on a shared upstream for the same outcome.
            self._reply_error(502, str(exc), retry=False)
        except OSError as exc:
            try:
                self._reply_error(502, str(exc))
            except OSError:
                pass

    def _chat_response(
        self, conn: http.client.HTTPConnection, data: bytes, headers: dict
    ) -> http.client.HTTPResponse:
        """POST to chat/completions; a non-200 raises :class:`UpstreamError`."""
        try:
            conn.request("POST", "/v1/chat/completions", body=data, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            raise UpstreamError(502, str(exc)) from exc
        if resp.status != 200:
            raw = resp.read()
            try:
                detail = json.loads(raw)
                message = (
                    (detail.get("error") or {}).get("message")
                    or detail.get("message")
                    or raw.decode("utf-8", "replace")[:500]
                )
            except (json.JSONDecodeError, AttributeError):
                message = raw.decode("utf-8", "replace")[:500]
            raise UpstreamError(resp.status, f"upstream {resp.status}: {message}")
        return resp

    def _chat_json(self, data: bytes, headers: dict) -> dict:
        conn = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=600
        )
        try:
            raw = self._chat_response(conn, data, headers).read()
            return json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise UpstreamError(502, str(exc)) from exc
        finally:
            conn.close()

    def _chat_chunks(self, data: bytes, headers: dict) -> Iterator[dict]:
        """One upstream SSE stream as decoded ``data:`` objects."""
        conn = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=600
        )
        try:
            resp = self._chat_response(conn, data, headers)
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.strip()
                if not line or not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue
        finally:
            conn.close()

    def _relay_stream(
        self, first: tuple[str, dict], events: Iterator[tuple[str, dict]]
    ) -> None:
        """Write Anthropic SSE events to the client, chunked."""
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

        def emit(event: str, data: dict) -> None:
            payload = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
            self.wfile.write(
                f"{len(payload):x}\r\n".encode("ascii") + payload + b"\r\n"
            )
            self.wfile.flush()

        try:
            emit(*first)
            for event, data in events:
                emit(event, data)
        except (OSError, UpstreamError, DroppedToolCall) as exc:
            # The turn was already live (a tool call streamed); just end it.
            self.log_message("stream ended early: %s", exc)
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass

    def _forward(self, path: str, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["content-length"] = str(len(data))
        headers["content-type"] = "application/json"
        conn = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=600
        )
        try:
            conn.request("POST", path, body=data, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            ctype = resp.getheader("content-type") or "application/json"
            self.send_header("content-type", ctype)
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(
                    f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n"
                )
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError as exc:
            try:
                self._reply_error(502, str(exc))
            except OSError:
                pass
        finally:
            conn.close()


def _upstream_is_sglang() -> bool:
    """SGLang answers ``/get_model_info``; vLLM does not."""
    try:
        conn = http.client.HTTPConnection(
            Handler.upstream_host, Handler.upstream_port, timeout=5
        )
        conn.request("GET", "/get_model_info")
        status = conn.getresponse().status
        conn.close()
        return status == 200
    except OSError:
        return False


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    flags = {a.split("=", 1)[0]: a for a in argv[1:] if a.startswith("--")}
    if len(args) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    listen_port, Handler.upstream_host, Handler.upstream_port = (
        int(args[0]),
        args[1].strip("[]"),
        int(args[2]),
    )
    if "--mode" in flags:
        mode = flags["--mode"].partition("=")[2]
        if mode not in ("auto", "chat", "messages"):
            print(__doc__, file=sys.stderr)
            return 2
        Handler.mode = mode
    if "--system-role" in flags:
        role = flags["--system-role"].partition("=")[2]
        if role not in SYSTEM_ROLES:
            print(__doc__, file=sys.stderr)
            return 2
        Handler.system_role = role
    if "--salvage" in flags:
        setting = flags["--salvage"].partition("=")[2]
        if setting not in SALVAGE_SETTINGS:
            print(__doc__, file=sys.stderr)
            return 2
        Handler.salvage = setting
    srv = ThreadingHTTPServer(("127.0.0.1", listen_port), Handler)
    srv.daemon_threads = True
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
