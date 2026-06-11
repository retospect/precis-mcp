"""Embedder abstraction.

Phase 3 needs vectors for blocks at ingest time and for queries at
search time. We define a tiny Protocol so the heavy real model
(``sentence-transformers``) is an optional dep, while tests run against
a deterministic mock that never imports torch.

The Protocol matches the runtime ``isinstance`` semantics provided by
``typing.runtime_checkable`` so handlers can accept ``Embedder`` and
either backend transparently.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from precis.embedder_wire import (
    PATH_EMBED,
    PATH_MODEL,
    WIRE_VERSION,
    EmbedRequest,
    EmbedResponse,
    ModelInfo,
)

log = logging.getLogger(__name__)

#: Transport seam for :class:`RemoteEmbedder`. A callable taking
#: ``(method, url, json_body | None, timeout)`` and returning
#: ``(status_code, parsed_json_dict)``. The default uses ``urllib``;
#: tests inject a fake so the client's retry / fallback / verification
#: logic is exercised without a live server.
Transport = Callable[[str, str, "dict | None", float], "tuple[int, dict]"]


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into normalized float vectors."""

    @property
    def dim(self) -> int: ...

    @property
    def model(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]: ...

    # ``is_ready`` lets the dispatcher fast-fail with a retryable
    # "warming" notice when an in-process backend (BgeM3Embedder) is
    # still loading weights, instead of blocking the MCP transport for
    # 30-120 s on a foreground first call. Backends with no warmup
    # phase (Mock, Remote) return True. Default added 2026-06-11 per
    # broad-pass usability finding #7.
    def is_ready(self) -> bool: ...


# ---------------------------------------------------------------------------
# Mock — deterministic, no external deps. Used in all unit tests.
# ---------------------------------------------------------------------------


class MockEmbedder:
    """Deterministic in-process embedder for tests + CI.

    Strategy: SHA-256 of the input text seeds a counter; we walk the
    counter to fill ``dim`` floats, normalize to unit L2. Same text →
    same vector → reproducible search results.

    Carries a settable ``model`` string so tests can pretend to be on
    a particular backend.
    """

    def __init__(self, *, dim: int = 1024, model: str = "mock") -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self._dim = dim
        self._model = model

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model(self) -> str:
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]

    def is_ready(self) -> bool:
        # MockEmbedder has no warmup phase — deterministic hashing.
        return True

    def embed_one(self, text: str) -> list[float]:
        # Fill `dim` floats by hashing the text repeatedly with a
        # 4-byte counter suffix. Each block of SHA-256 output yields
        # 8 little-endian uint32s → mapped to floats in [-1, 1].
        floats: list[float] = []
        counter = 0
        seed = text.encode("utf-8")
        while len(floats) < self._dim:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
            counter += 1
            for i in range(0, 32, 4):
                if len(floats) >= self._dim:
                    break
                (n,) = struct.unpack("<I", digest[i : i + 4])
                # map [0, 2**32) -> [-1, 1)
                floats.append((n / 2**31) - 1.0)
        # L2-normalize so cosine distance is well-defined.
        norm = math.sqrt(sum(f * f for f in floats))
        if norm == 0.0:
            return floats
        return [f / norm for f in floats]


# ---------------------------------------------------------------------------
# Real implementation — optional. Loaded lazily.
# ---------------------------------------------------------------------------


_BGE_M3_DIM = 1024  # documented constant for BAAI/bge-m3

# Two distinct identifiers — keep them separate.
#
# ``_BGE_M3_HF_ID`` is what ``SentenceTransformer(...)`` loads from
# HuggingFace. ``_BGE_M3_REGISTRY_KEY`` is the value stored in
# ``embedders.name`` and the FK target for ``chunk_embeddings.embedder``.
# They look similar but serve different roles: the HF id is a
# vendor-namespaced model URL; the registry key is precis's own short
# label, picked to match the ``--embedder`` CLI flag and the
# ``PRECIS_EMBEDDER`` config knob. Conflating them means the worker
# writes ``BAAI/bge-m3`` into a column that FKs against the ``bge-m3``
# row → ``ForeignKeyViolation`` on every insert. See the 2026-05-24
# incident note in CHANGELOG.md.
_BGE_M3_HF_ID = "BAAI/bge-m3"
_BGE_M3_REGISTRY_KEY = "bge-m3"

# Hard ceiling on the per-text char length passed to bge-m3. The model's
# tokenizer caps at 8192 tokens, but pathological input (corrupted-OCR
# tables, fragmented unicode runs) can balloon attention into 70+ GiB
# allocations on MPS before the tokenizer truncates. We pre-truncate at
# the char level so the encoder never sees more input than it can handle.
#
# 16,000 chars ≈ 4–8K tokens depending on content density, which is
# safely under the 8192 cap even for token-dense markdown / LaTeX. This
# is a pure survival guard — structure-aware splitting (e.g.
# ``acatome_extract.chunker.split_table``) belongs upstream at the
# source so retrieval boundaries stay meaningful.
_BGE_M3_MAX_CHARS = 16_000


class BgeM3Embedder:
    """``BAAI/bge-m3`` via sentence-transformers. Optional dep.

    The model is **lazily** loaded on the first call to ``embed`` /
    ``embed_one``. Construction itself is cheap and does not import
    ``sentence_transformers`` — this matters because MCP clients
    (Windsurf, etc.) spawn the server with a short handshake budget;
    eager-loading bge-m3 takes ~7s and trips a startup timeout. Once
    loaded, the model stays in memory for the life of the process.

    Each input text is truncated to :data:`_BGE_M3_MAX_CHARS` chars
    before being passed to the encoder. This is a defensive guard
    against malformed blocks that escape upstream chunking (e.g. a
    corrupted-OCR table block of 192,000 chars that triggered MPS OOM
    in production). The 1:1 ``len(texts) == len(returned_vectors)``
    contract is preserved — truncation is lossy on the suffix but does
    not change block count, so the store's blocks↔vectors mapping
    stays intact.

    Tests should still prefer ``MockEmbedder`` to avoid the model
    download / weight load entirely.
    """

    def __init__(self, *, model_name: str = _BGE_M3_REGISTRY_KEY) -> None:
        # ``model_name`` is the precis registry key (FK target in
        # ``embedders.name``) — *not* the HuggingFace id. The HF id is
        # an internal constant only ``_ensure_loaded`` reaches for.
        self._model_name = model_name
        self._st: object | None = None  # SentenceTransformer when loaded
        # No imports here — keep startup fast for MCP clients with a
        # short handshake budget. The optional-dep check fires on first
        # ``embed()`` call inside ``_ensure_loaded``.

    @property
    def dim(self) -> int:
        return _BGE_M3_DIM

    @property
    def model(self) -> str:
        return self._model_name

    def _ensure_loaded(self) -> object:
        """Load the model on first use; cached thereafter.

        Raises a clear ``ImportError`` if the optional dep is missing
        — this is the first time we actually need it, so failing here
        is the correct surface.
        """
        if self._st is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise ImportError(
                    "sentence-transformers is not installed. "
                    "Install with: pip install 'precis-mcp[paper]' "
                    "or: pip install sentence-transformers"
                ) from exc
            # Always load from the HF id — that's what the hub serves.
            # ``self._model_name`` is the registry key and is **not**
            # what ``SentenceTransformer`` resolves against.
            self._st = SentenceTransformer(_BGE_M3_HF_ID)
        return self._st

    def is_ready(self) -> bool:
        """True once the bge-m3 weights are loaded.

        Used by the dispatch path to fast-fail foreground search calls
        with a retryable "warming" notice while the background warmup
        thread (server._warm_embedder_background) is still loading the
        model on a cold container — instead of blocking the MCP
        transport for the 30-120 s the load can take and tripping the
        per-call timeout. (Broad-pass usability finding #7.)
        """
        return self._st is not None

    def _raise_if_warming(self) -> None:
        """Fast-fail when the model isn't loaded yet.

        The background warmup thread races every first foreground
        call. Without this guard, an MCP search arriving before the
        thread finishes blocks the transport for the entire model
        load; the wall-clock time is dominated by ``SentenceTransformer``
        construction and the first forward pass to JIT-compile MPS
        kernels, neither of which we can preempt. ``Upstream`` is the
        closest error class — bge-m3 is in-process but warmup is the
        kind of transient-unavailability the agent should retry rather
        than treat as a fatal misconfiguration.
        """
        if self._st is not None:
            return
        from precis.errors import Upstream

        raise Upstream(
            "embedder warming — bge-m3 weights are still loading; "
            "retry in ~30 seconds. "
            "(Lexical-only searches via tags= work now without waiting.)",
            next="retry the same call in ~30s, or scope by tags=[...] for lex-only",
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._raise_if_warming()
        st = self._ensure_loaded()
        # Per-text char truncation — see class docstring + _BGE_M3_MAX_CHARS.
        # Cheap O(n) check; only allocates a new string when over budget.
        safe = [
            t if len(t) <= _BGE_M3_MAX_CHARS else t[:_BGE_M3_MAX_CHARS] for t in texts
        ]
        embs = st.encode(safe, normalize_embeddings=True)  # type: ignore[attr-defined]
        return [list(map(float, e)) for e in embs]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


# ---------------------------------------------------------------------------
# Remote — HTTP client to a `precis serve-embeddings` service (ADR 0020).
# ---------------------------------------------------------------------------


def _urllib_transport(
    method: str, url: str, body: dict | None, timeout: float
) -> tuple[int, dict]:
    """Default :data:`Transport` — a stdlib ``urllib`` round-trip.

    No third-party HTTP dep, so the torch-free serve/worker images stay
    tiny (ADR 0021). Returns ``(status, parsed_json)``. HTTP error
    statuses (``4xx`` / ``5xx``) are returned as a status code with
    their body parsed, so the caller's retry policy can branch on
    ``429`` / ``5xx``. Connection-level failures (refused, timeout,
    DNS) raise ``URLError`` and propagate to the retry/fallback loop.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
    parsed: dict = {}
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                parsed = decoded
        except json.JSONDecodeError:
            parsed = {}
    return status, parsed


class RemoteEmbedder:
    """`Embedder` that delegates encoding to a remote embedding service.

    A drop-in for the in-process embedders: same `Embedder` Protocol,
    no ``torch`` import. Reads an ordered, comma-separated endpoint list
    (``PRECIS_EMBEDDER_URL``); prefers the first healthy endpoint and
    falls back to the next. Per-call retries use exponential backoff
    with jitter; connection failures and ``429`` / ``5xx`` are
    retryable, other 4xx are not.

    On first use it fetches ``/model`` and, when ``expected_dim`` is
    supplied (the corpus's embedding dimension), asserts the served
    model's ``dim`` matches — the boundary check that turns a
    wrong/upgraded model into a loud failure instead of silent vector
    corruption (ADR 0020).
    """

    def __init__(
        self,
        url: str,
        *,
        expected_dim: int | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        backoff_base: float = 0.25,
        backoff_max: float = 8.0,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        endpoints = [e.strip().rstrip("/") for e in url.split(",") if e.strip()]
        if not endpoints:
            raise ValueError("RemoteEmbedder requires at least one URL")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._endpoints = endpoints
        self._expected_dim = expected_dim
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._transport: Transport = transport or _urllib_transport
        self._sleep = sleep
        self._info: ModelInfo | None = None

    @property
    def dim(self) -> int:
        # Trust the corpus-supplied expected dim when we have it (avoids
        # a network round-trip just to answer `.dim`); otherwise fetch.
        if self._expected_dim is not None:
            return self._expected_dim
        return self._model_info().dim

    @property
    def model(self) -> str:
        return self._model_info().model

    def is_ready(self) -> bool:
        # Remote backend has no local warmup phase. The first call
        # pays a small ``/model`` round-trip; subsequent calls don't.
        # Returning True here keeps the dispatch fast-path uncluttered;
        # genuine transport failures still surface via ``embed()``'s
        # existing retry / RuntimeError path.
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Enforce the model/dim boundary check before the first encode
        # (cached thereafter).
        self._model_info()
        req = EmbedRequest(texts=list(texts))
        status, body = self._call("POST", PATH_EMBED, req.to_dict())
        if status != 200:
            raise RuntimeError(f"embedder {PATH_EMBED} returned HTTP {status}")
        resp = EmbedResponse.from_dict(body)
        if len(resp.vectors) != len(texts):
            raise RuntimeError(
                f"embedder returned {len(resp.vectors)} vectors for {len(texts)} texts"
            )
        return resp.vectors

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    # ── internals ──────────────────────────────────────────────────

    def _model_info(self) -> ModelInfo:
        if self._info is None:
            status, body = self._call("GET", PATH_MODEL, None)
            if status != 200:
                raise RuntimeError(f"embedder {PATH_MODEL} returned HTTP {status}")
            info = ModelInfo.from_dict(body)
            if info.wire_version != WIRE_VERSION:
                raise RuntimeError(
                    f"embedder wire version {info.wire_version!r} != "
                    f"client {WIRE_VERSION!r} — upgrade one side"
                )
            if self._expected_dim is not None and info.dim != self._expected_dim:
                raise RuntimeError(
                    f"embedder dim {info.dim} != corpus dim {self._expected_dim} "
                    f"(model {info.model!r}) — refusing to write incompatible vectors"
                )
            self._info = info
        return self._info

    def _call(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        """Try each endpoint in order, retrying retryable failures."""
        last_err: Exception | None = None
        for endpoint in self._endpoints:
            url = endpoint + path
            for attempt in range(self._max_retries + 1):
                try:
                    status, parsed = self._transport(method, url, body, self._timeout)
                except Exception as exc:  # connection-level failure
                    last_err = exc
                    log.debug("embedder transport error on %s: %s", url, exc)
                    self._backoff(attempt)
                    continue
                if status == 429 or 500 <= status < 600:
                    last_err = RuntimeError(f"HTTP {status} from {url}")
                    self._backoff(attempt)
                    continue
                return status, parsed
        raise RuntimeError(
            f"all embedder endpoints failed ({self._endpoints})"
        ) from last_err

    def _backoff(self, attempt: int) -> None:
        delay = min(self._backoff_max, self._backoff_base * (2**attempt))
        # Full jitter — spreads retries from concurrent callers.
        self._sleep(random.uniform(0, delay))


# ---------------------------------------------------------------------------
# Factory — config-driven selection
# ---------------------------------------------------------------------------


def make_embedder(
    name: str,
    *,
    dim: int = 1024,
    url: str | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> Embedder:
    """Return an `Embedder` for the given config name.

    - ``"mock"``    → deterministic ``MockEmbedder(dim=dim)``
    - ``"bge-m3"``  → real ``BgeM3Embedder()`` (loads the model)
    - ``"remote"``  → ``RemoteEmbedder(url, expected_dim=dim)`` (HTTP
      client to a ``precis serve-embeddings`` service; requires ``url``)

    ``timeout`` / ``max_retries`` apply only to ``"remote"``.

    Raises ``ValueError`` for unknown names or a missing remote URL.
    """
    if name == "mock":
        return MockEmbedder(dim=dim)
    if name == "bge-m3":
        return BgeM3Embedder()
    if name == "remote":
        if not url:
            raise ValueError(
                "embedder 'remote' requires a URL (set PRECIS_EMBEDDER_URL)"
            )
        return RemoteEmbedder(
            url, expected_dim=dim, timeout=timeout, max_retries=max_retries
        )
    raise ValueError(
        f"unknown embedder name: {name!r} - expected 'mock', 'bge-m3', or 'remote'"
    )


__all__ = [
    "BgeM3Embedder",
    "Embedder",
    "MockEmbedder",
    "RemoteEmbedder",
    "make_embedder",
]
