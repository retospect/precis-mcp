#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Anthropic-Messages shim in front of castor's vLLM, for `scripts/claude-local`.

vLLM (v0.20.1) serves ``/v1/messages`` but its schema only accepts ``user`` and
``assistant`` turns. Current Claude Code also sends ``system``-role turns inside
``messages`` (hook output, mid-conversation system turns) and calls
``/v1/messages/count_tokens``, which vLLM lacks. This shim:

* rewrites every ``system`` turn into a ``user`` turn tagged
  ``[harness system turn]`` and merges adjacent same-role turns;
* answers ``count_tokens`` with a chars/4 estimate;
* streams everything else through unchanged (SSE passthrough).

Stdlib only; no dependency on the precis package. Runs anywhere that can
reach castor; ``scripts/claude-local`` starts it on demand on loopback.

    scripts/anthropic-shim.py <listen-port> <upstream-host> <upstream-port>
"""

from __future__ import annotations

import http.client
import json
import sys
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


def rewrite_messages(messages: list[dict]) -> list[dict]:
    """``system`` turns become tagged ``user`` turns; adjacent same roles merge."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        blocks = _as_blocks(m.get("content"))
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


class Handler(BaseHTTPRequestHandler):
    upstream_host = "127.0.0.1"
    upstream_port = 8000
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

    def do_GET(self) -> None:  # health passthrough
        conn = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=10
        )
        try:
            conn.request("GET", self.path)
            resp = conn.getresponse()
            data = resp.read()
        except OSError as exc:
            self._reply_json(
                502,
                {"type": "error", "error": {"type": "api_error", "message": str(exc)}},
            )
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
            body["messages"] = rewrite_messages(body["messages"])
            # Claude Code's beta/metadata fields vLLM does not model.
            body.pop("metadata", None)
        self._forward(path, body)

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
                self._reply_json(
                    502,
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": str(exc)},
                    },
                )
            except OSError:
                pass
        finally:
            conn.close()


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    listen_port, Handler.upstream_host, Handler.upstream_port = (
        int(argv[1]),
        argv[2].strip("[]"),
        int(argv[3]),
    )
    srv = ThreadingHTTPServer(("127.0.0.1", listen_port), Handler)
    srv.daemon_threads = True
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
