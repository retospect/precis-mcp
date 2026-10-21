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

In chat mode the Anthropic ``system`` field is folded into the first user
turn, not sent as a system message: on SGLang's gpt-oss path ANY system-role
message makes the model end its turn after reasoning with no tool call.
``count_tokens`` is a chars/4 estimate in both modes.

Stdlib only; no dependency on the precis package. Runs anywhere that can
reach castor; ``scripts/claude-local`` starts it on demand on loopback.

    scripts/anthropic-shim.py <listen-port> <upstream-host> <upstream-port> [--mode auto|chat|messages]

``auto`` (default) probes ``GET /get_model_info``: SGLang answers it (vLLM
does not), so the shim picks ``chat`` for SGLang and ``messages`` otherwise.
"""

from __future__ import annotations

import http.client
import json
import sys
import uuid
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


def _to_chat_messages(body: dict) -> list[dict]:
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
    if system:
        for msg in out:
            if msg["role"] == "user":
                msg["content"] = f"{system}\n\n{msg['content']}"
                break
        else:
            out.insert(0, {"role": "user", "content": system})
    return out


_TOOL_CHOICE = {"auto": "auto", "any": "required", "none": "none"}


def to_chat_body(body: dict) -> dict:
    """Translate a ``/v1/messages`` request body to ``/v1/chat/completions``."""
    out: dict = {"messages": _to_chat_messages(body)}
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


# --- HTTP plumbing -------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    upstream_host = "127.0.0.1"
    upstream_port = 8000
    mode = "auto"  # auto | chat | messages
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("shim: " + (fmt % args) + "\n")

    def _reply_json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _reply_error(self, status: int, message: str) -> None:
        self._reply_json(
            status,
            {"type": "error", "error": {"type": "api_error", "message": message}},
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
        """Translate ``/v1/messages`` to ``/v1/chat/completions`` and back."""
        chat_body = to_chat_body(body)
        model = str(body.get("model") or "unknown")
        data = json.dumps(chat_body).encode("utf-8")
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["content-length"] = str(len(data))
        headers["content-type"] = "application/json"
        conn = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=600
        )
        try:
            conn.request("POST", "/v1/chat/completions", body=data, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            conn.close()
            self._reply_error(502, str(exc))
            return
        try:
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
                self._reply_error(resp.status, f"upstream {resp.status}: {message}")
                return
            if chat_body.get("stream"):
                self._relay_stream(resp, model)
            else:
                self._reply_json(
                    200, to_anthropic_response(json.loads(resp.read()), model)
                )
        except (OSError, json.JSONDecodeError) as exc:
            try:
                self._reply_error(502, str(exc))
            except OSError:
                pass
        finally:
            conn.close()

    def _relay_stream(self, resp: http.client.HTTPResponse, model: str) -> None:
        """Translate upstream chat/completions SSE to Anthropic SSE, chunked."""
        translator = ChatStreamTranslator(model)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

        def emit(events: list[tuple[str, dict]]) -> None:
            for event, data in events:
                payload = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
                self.wfile.write(
                    f"{len(payload):x}\r\n".encode("ascii") + payload + b"\r\n"
                )
            self.wfile.flush()

        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.strip()
                if not line or not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                emit(translator.feed(chunk))
            emit(translator.close())
        finally:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

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
    srv = ThreadingHTTPServer(("127.0.0.1", listen_port), Handler)
    srv.daemon_threads = True
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
