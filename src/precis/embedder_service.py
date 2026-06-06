"""HTTP embedding service — the server side of ADR 0020.

Wraps an in-process :class:`precis.embedder.BgeM3Embedder` (or any
`Embedder`) behind the wire schema in :mod:`precis.embedder_wire`, so
torch-free ``serve`` / ``worker`` processes can embed over HTTP via
:class:`precis.embedder.RemoteEmbedder`.

Deliberately stdlib-only (``http.server``): the embedder image's only
heavy dependency is ``sentence-transformers``; the service adds no web
framework on top. A ``ThreadingHTTPServer`` plus a bounded admission
semaphore gives backpressure — when the in-flight ceiling is hit, the
service returns ``429`` + ``Retry-After`` rather than queueing
unboundedly, and the client's backoff (ADR 0020) does the rest.

Endpoints (paths from :mod:`precis.embedder_wire`):

- ``GET  /healthz`` — process is up (always 200 once serving).
- ``GET  /readyz``  — model weights are loaded (200) or warming (503).
- ``GET  /model``   — :class:`ModelInfo` (name, dim, revision, wire).
- ``POST /embed``   — :class:`EmbedRequest` → :class:`EmbedResponse`.
- ``GET  /metrics`` — plaintext counters for scraping.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from precis.embedder_wire import (
    DEFAULT_PORT,
    PATH_EMBED,
    PATH_HEALTH,
    PATH_METRICS,
    PATH_MODEL,
    PATH_READY,
    EmbedRequest,
    EmbedResponse,
    ModelInfo,
)

if TYPE_CHECKING:
    from precis.embedder import Embedder

log = logging.getLogger(__name__)


class _Metrics:
    """Tiny thread-safe counter bag exposed at ``/metrics``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.embeds = 0
        self.texts = 0
        self.rejected_429 = 0
        self.errors = 0
        self.inflight = 0

    def render(self) -> str:
        with self._lock:
            return (
                f"precis_embedder_requests_total {self.requests}\n"
                f"precis_embedder_embed_total {self.embeds}\n"
                f"precis_embedder_texts_total {self.texts}\n"
                f"precis_embedder_rejected_429_total {self.rejected_429}\n"
                f"precis_embedder_errors_total {self.errors}\n"
                f"precis_embedder_inflight {self.inflight}\n"
            )


class EmbedderService:
    """Holds the embedder, readiness state, backpressure, and metrics.

    Shared (by reference) with every request-handler instance the
    ``ThreadingHTTPServer`` spawns. The embedder is warmed on a
    background thread so ``/readyz`` stays 503 until the first encode
    succeeds — a load-balanced rollout can gate traffic on it.
    """

    def __init__(
        self,
        embedder: Embedder,
        *,
        revision: str | None = None,
        max_inflight: int = 4,
        warm: bool = True,
    ) -> None:
        self._embedder = embedder
        self._revision = revision
        # Admission control: at most ``max_inflight`` concurrent embed
        # calls; beyond that callers get 429 + Retry-After.
        self._sem = threading.BoundedSemaphore(max_inflight)
        # Serialise actual encode calls — the underlying model is not
        # guaranteed thread-safe and a single GPU/MPS stream is the
        # bottleneck anyway.
        self._encode_lock = threading.Lock()
        self._ready = threading.Event()
        self.metrics = _Metrics()
        if warm:
            threading.Thread(target=self._warm, name="embedder-warm", daemon=True).start()
        else:
            self._ready.set()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def model_info(self) -> ModelInfo:
        return ModelInfo(
            model=self._embedder.model,
            dim=self._embedder.dim,
            revision=self._revision,
        )

    def _warm(self) -> None:
        try:
            self._embedder.embed(["warmup"])
            self._ready.set()
            log.info("embedder warm: model=%s dim=%d", self._embedder.model, self._embedder.dim)
        except Exception:  # pragma: no cover - depends on real model
            log.exception("embedder warmup failed; /readyz stays 503")

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Admission-controlled, serialised encode. Raises `Busy` when full."""
        if not self._sem.acquire(blocking=False):
            with self.metrics._lock:
                self.metrics.rejected_429 += 1
            raise Busy
        with self.metrics._lock:
            self.metrics.inflight += 1
        try:
            with self._encode_lock:
                vectors = self._embedder.embed(texts)
            with self.metrics._lock:
                self.metrics.embeds += 1
                self.metrics.texts += len(texts)
            return vectors
        finally:
            with self.metrics._lock:
                self.metrics.inflight -= 1
            self._sem.release()


class Busy(Exception):
    """Raised by :meth:`EmbedderService.embed` when at capacity."""


def _make_handler(service: EmbedderService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # Quieter logs — the access line per request is noise; errors
        # still surface via log.exception below.
        def log_message(self, *args: object) -> None:
            return

        def _send_json(self, status: int, obj: dict, extra_headers: dict | None = None) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, status: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            service.metrics.requests += 1
            if self.path == PATH_HEALTH:
                self._send_text(200, "ok")
            elif self.path == PATH_READY:
                if service.ready:
                    self._send_text(200, "ready")
                else:
                    self._send_text(503, "warming")
            elif self.path == PATH_MODEL:
                self._send_json(200, service.model_info().to_dict())
            elif self.path == PATH_METRICS:
                self._send_text(200, service.metrics.render())
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            service.metrics.requests += 1
            if self.path != PATH_EMBED:
                self._send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                payload = json.loads(raw) if raw else {}
                req = EmbedRequest.from_dict(payload)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": f"bad request: {exc}"})
                return
            try:
                vectors = service.embed(req.texts)
            except Busy:
                self._send_json(
                    429, {"error": "busy"}, extra_headers={"Retry-After": "1"}
                )
                return
            except Exception as exc:  # pragma: no cover - model failure path
                with service.metrics._lock:
                    service.metrics.errors += 1
                log.exception("embed failed")
                self._send_json(500, {"error": f"embed failed: {exc}"})
                return
            info = service.model_info()
            resp = EmbedResponse(model=info.model, dim=info.dim, vectors=vectors)
            self._send_json(200, resp.to_dict())

    return Handler


def make_server(
    service: EmbedderService, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT
) -> ThreadingHTTPServer:
    """Build (but don't start) the HTTP server bound to ``host:port``.

    Pass ``port=0`` for an ephemeral port (tests read the chosen port
    from ``server.server_address[1]``).
    """
    return ThreadingHTTPServer((host, port), _make_handler(service))


def serve(
    embedder: Embedder,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    revision: str | None = None,
    max_inflight: int = 4,
    warm: bool = True,
) -> None:
    """Run the embedding service until interrupted (blocking)."""
    service = EmbedderService(
        embedder, revision=revision, max_inflight=max_inflight, warm=warm
    )
    httpd = make_server(service, host=host, port=port)
    log.info(
        "serving embeddings on http://%s:%d (model=%s, max_inflight=%d)",
        host,
        port,
        embedder.model,
        max_inflight,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        log.info("shutting down embedder service")
    finally:
        httpd.server_close()


__all__ = ["Busy", "EmbedderService", "make_server", "serve"]
