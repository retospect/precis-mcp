#!/usr/bin/env python3
"""OpenAI-compatible embeddings shim over precis' bge-m3 embedder.

Repo-dev tooling for the claude-context code-search MCP, NOT the precis
product. Background: ollama (which used to serve ``nomic-embed-text`` for
code search) has been retired in favour of cppllama/llama-swap, which
serves only chat models. Rather than spend a GPU slot on a second
embedding model, code search reuses the bge-m3 embedder that
``precis serve-embeddings`` already runs on every node (loopback
``127.0.0.1:8181``, ADR 0020).

The catch: claude-context speaks the OpenAI embeddings API
(``POST /v1/embeddings``) while the precis embedder speaks its own
``POST /embed`` wire shape (``embedder_wire.py``). This shim is the thin
translator between them:

    claude-context  ──/v1/embeddings──▶  shim  ──/embed──▶  bge-m3 :8181

Runs on the HOST (not a container): the precis embedder binds loopback
only, so a container reaching ``host.docker.internal`` can't see it.
Dependency-free (stdlib only) so it starts under any python3 the
SessionStart hook finds. Started idempotently by
``scripts/hooks/code-search-up.sh``.

Point claude-context at it with (in ``.mcp.json``):
    EMBEDDING_PROVIDER=OpenAI
    OPENAI_BASE_URL=http://127.0.0.1:8182/v1
    OPENAI_API_KEY=unused        # bge-m3 is authless; any value works
    EMBEDDING_MODEL=bge-m3
    EMBEDDING_DIMENSION=1024
"""

from __future__ import annotations

import base64
import json
import os
import struct
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Where this shim listens (loopback) and where the precis embedder lives.
SHIM_HOST = os.environ.get("CODE_SEARCH_SHIM_HOST", "127.0.0.1")
SHIM_PORT = int(os.environ.get("CODE_SEARCH_SHIM_PORT", "8182"))
UPSTREAM = os.environ.get("CODE_SEARCH_BGE_URL", "http://127.0.0.1:8181/embed")
UPSTREAM_TIMEOUT_S = float(os.environ.get("CODE_SEARCH_BGE_TIMEOUT_S", "120"))


def _upstream_embed(texts: list[str]) -> tuple[str, int, list[list[float]]]:
    """Call precis ``/embed`` for a batch; return (model, dim, vectors)."""
    body = json.dumps({"texts": texts}).encode("utf-8")
    req = urllib.request.Request(
        UPSTREAM, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_S) as resp:
        payload = json.loads(resp.read())
    # embedder_wire.EmbedResponse: {"model","dim","vectors":[[...]]}
    return payload["model"], int(payload["dim"]), payload["vectors"]


def _openai_data(
    vectors: list[list[float]], encoding_format: str
) -> list[dict[str, Any]]:
    """Shape vectors as OpenAI ``data[]`` entries, honouring encoding_format.

    The OpenAI Python SDK defaults to ``base64`` (little-endian float32);
    the JS SDK sends floats. Honour whatever the client asked for so both
    parse correctly.
    """
    data: list[dict[str, Any]] = []
    for i, vec in enumerate(vectors):
        if encoding_format == "base64":
            raw = struct.pack(f"<{len(vec)}f", *vec)
            emb: Any = base64.b64encode(raw).decode("ascii")
        else:
            emb = vec
        data.append({"object": "embedding", "index": i, "embedding": emb})
    return data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code: int, obj: dict[str, Any]) -> None:
        blob = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self) -> None:
        if self.path in ("/healthz", "/health"):
            self._send_json(200, {"status": "ok", "upstream": UPSTREAM})
        elif self.path in ("/v1/models", "/models"):
            self._send_json(
                200,
                {"object": "list", "data": [{"id": "bge-m3", "object": "model"}]},
            )
        else:
            self._send_json(404, {"error": {"message": f"not found: {self.path}"}})

    def do_POST(self) -> None:
        if self.path not in ("/v1/embeddings", "/embeddings"):
            self._send_json(404, {"error": {"message": f"not found: {self.path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length)) if length else {}
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": {"message": f"bad JSON: {exc}"}})
            return

        raw_input = req.get("input", [])
        texts = [raw_input] if isinstance(raw_input, str) else list(raw_input)
        if not all(isinstance(t, str) for t in texts):
            self._send_json(
                400,
                {"error": {"message": "input must be a string or list of strings"}},
            )
            return
        encoding_format = req.get("encoding_format", "float")

        try:
            model, _dim, vectors = _upstream_embed(texts)
        except urllib.error.URLError as exc:
            # Upstream down/unreachable — surface as 502 so the client sees it.
            self._send_json(502, {"error": {"message": f"bge-m3 upstream: {exc}"}})
            return
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(502, {"error": {"message": f"bad upstream reply: {exc}"}})
            return

        self._send_json(
            200,
            {
                "object": "list",
                "data": _openai_data(vectors, encoding_format),
                "model": model,
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
        )

    def log_message(self, *args: Any) -> None:
        """Quiet by default; the SessionStart hook owns lifecycle logging."""


def main() -> None:
    server = ThreadingHTTPServer((SHIM_HOST, SHIM_PORT), Handler)
    print(
        f"[embed-shim] OpenAI /v1/embeddings on {SHIM_HOST}:{SHIM_PORT} -> {UPSTREAM}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
