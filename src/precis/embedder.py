"""Embedder abstraction.

Phase 3 needs vectors for blocks at ingest time and for queries at
search time. We define a tiny Protocol so the heavy real model
(``sentence-transformers``) is an optional dep, while tests run against
a deterministic mock that never imports torch.

The Protocol matches the runtime ``isinstance`` semantics provided by
``typing.runtime_checkable`` so handlers can accept ``Embedder`` and
either backend transparently.

Backends (``MockEmbedder`` / ``BgeM3Embedder`` / ``RemoteEmbedder``) come
from :func:`make_embedder`. :class:`BoundedConcurrencyEmbedder` is a
*decorator*, not a backend: it bounds how many embeds one process may
have in flight and sheds past that. Only the request path wears it
(``runtime/factory.py::build_runtime``); workers stay unwrapped and keep
the patient ``embedder_timeout`` budget. See gripe 244419 for why the two
paths must not share one budget.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import struct
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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

    # ``warmup`` is called by the embedder service's background warm
    # thread on boot. It must bypass any ``is_ready``/warming gate the
    # public ``embed`` method has — otherwise the warm thread fast-
    # fails on the very gate it's meant to clear (the 2026-06-15 →
    # 2026-06-16 regression). Backends without a warmup phase return
    # immediately. Added 2026-06-17 as a post-regression fix.
    def warmup(self) -> None: ...

    # ``unload`` is called by the embedder service's idle-unload path
    # (embedder_service.py, §F cycle b) after PRECIS_EMBEDDER_IDLE_S of
    # no ``/embed`` activity — never on the request path. It releases
    # any loaded weights/session state so the process's RSS/accelerator
    # memory drops while idle; a subsequent ``warmup()`` re-loads
    # lazily. Idempotent — safe to call when nothing is loaded. Backends
    # with no persistent load (Mock, Remote — the model lives
    # server-side) return immediately.
    def unload(self) -> None: ...

    # ``backend`` / ``is_production`` are DELIBERATELY not part of this
    # Protocol, even though every real backend below carries them.
    # ``skill_index/index.py::FileCorpusIndex.is_available()`` (and a
    # handful of test doubles across the suite that hand-roll a
    # minimal embedder for one scenario) gate on structural
    # ``isinstance(e, Embedder)`` — adding required members here would
    # silently flip those fakes to "not an Embedder" and mask their
    # test's own semantic search path. Read via
    # ``getattr(embedder, "is_production", True)`` (default **True** —
    # an unrecognised/duck-typed embedder is assumed production-grade
    # so this doesn't manufacture false warnings for every ad-hoc test
    # double) rather than an isinstance/type check, per gripe gr249198.


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

    @property
    def backend(self) -> str:
        return "mock"

    @property
    def is_production(self) -> bool:
        return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]

    def is_ready(self) -> bool:
        # MockEmbedder has no warmup phase — deterministic hashing.
        return True

    def warmup(self) -> None:
        # No-op: deterministic hashing has nothing to load.
        return None

    def unload(self) -> None:
        # No-op: nothing is ever loaded to begin with.
        return None

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

# ---------------------------------------------------------------------------
# Load deadline (embedder-wedge-hardening.md §2) — a hung model load must
# EXIT, not hang.
#
# The 2026-08-08→10 caspar incident: ``SentenceTransformer(...)`` dials
# HuggingFace Hub for revision metadata even with weights already cached;
# when HF is slow/rate-limited that dial hangs indefinitely (plain socket
# I/O the Python level can't interrupt), ``/readyz`` stays unready
# forever, and a restart-based watchdog dutifully kicks the daemon into
# the exact same hang — an infinite, log-silent loop. A restart-based
# auto-clear cannot fix a load path that depends on a remote service; the
# process has to notice its OWN load is stuck and exit so launchd
# ``KeepAlive`` / systemd ``Restart=always`` own the retry instead.
# ---------------------------------------------------------------------------

#: Env override for the load deadline (seconds). Wide default — a cold
#: bge-m3 pull over a slow link is legitimately minutes, not seconds; this
#: bounds a GENUINELY wedged load (the HF-dial-hangs-forever class), not a
#: merely slow one.
PRECIS_EMBEDDER_LOAD_DEADLINE_ENV = "PRECIS_EMBEDDER_LOAD_DEADLINE_S"
_LOAD_DEADLINE_DEFAULT_S = 600.0


def _load_deadline_s() -> float:
    """Read :data:`PRECIS_EMBEDDER_LOAD_DEADLINE_ENV`, else the default.

    A malformed override degrades to the default rather than raising —
    a typo'd env var must not itself become a boot-time crash.
    """
    raw = os.environ.get(PRECIS_EMBEDDER_LOAD_DEADLINE_ENV)
    if raw is None:
        return _LOAD_DEADLINE_DEFAULT_S
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "%s=%r is not a number; using default %.0fs",
            PRECIS_EMBEDDER_LOAD_DEADLINE_ENV,
            raw,
            _LOAD_DEADLINE_DEFAULT_S,
        )
        return _LOAD_DEADLINE_DEFAULT_S


def _exit_process(code: int) -> None:  # pragma: no cover - see test's monkeypatch
    """Terminate the WHOLE process immediately. Not ``sys.exit`` — the
    load can be running on a background thread (the embedder service's
    warm thread), and ``sys.exit`` there only unwinds that one thread,
    leaving the process (and its hung load) running. ``os._exit`` skips
    cleanup/atexit — deliberately: a thread stuck in blocking C-level I/O
    can't be asked nicely to unwind, and this path only runs because
    something is already wedged. Split into its own function so tests can
    monkeypatch it instead of actually killing the test process."""
    os._exit(code)


def _load_with_deadline(
    loader: Callable[[], object], *, deadline_s: float, what: str
) -> object:
    """Run ``loader()`` (blocking) with a hard wall-clock deadline.

    A watchdog ``threading.Timer`` is the only reliable bound here: the
    load can hang inside blocking socket I/O that the calling thread
    can't be interrupted out of. If ``loader`` hasn't returned by
    ``deadline_s``, the timer fires on ITS OWN thread and calls
    :func:`_exit_process` — logging the cause first so the daemon's log
    tells the story instead of just going silent.
    """
    timer = threading.Timer(
        deadline_s, _on_load_deadline_exceeded, args=(what, deadline_s)
    )
    timer.daemon = True
    timer.start()
    try:
        return loader()
    finally:
        timer.cancel()


def _on_load_deadline_exceeded(what: str, deadline_s: float) -> None:
    log.error(
        "embedder load deadline exceeded: %s did not return within %.0fs — "
        "exiting so the process supervisor (launchd KeepAlive / systemd "
        "Restart=always) restarts us; a hang here usually means a network "
        "dial (e.g. HuggingFace Hub) is stuck. Override with %s.",
        what,
        deadline_s,
        PRECIS_EMBEDDER_LOAD_DEADLINE_ENV,
    )
    _exit_process(1)


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

    @property
    def backend(self) -> str:
        return "bge-m3"

    @property
    def is_production(self) -> bool:
        return True

    def _ensure_loaded(self) -> object:
        """Load the model on first use; cached thereafter.

        Raises a clear ``ImportError`` if the optional dep is missing
        — this is the first time we actually need it, so failing here
        is the correct surface.

        The actual construction runs under :func:`_load_with_deadline`
        (embedder-wedge-hardening.md §2) — ``SentenceTransformer(...)``
        dials HuggingFace Hub for revision metadata even with weights
        cached, and that dial can hang indefinitely against a slow/
        rate-limited hub. A hung load here breaches the deadline and
        exits the process rather than leaving ``/readyz`` unready forever
        behind a watchdog that just keeps restarting into the same hang.
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
            self._st = _load_with_deadline(
                lambda: SentenceTransformer(_BGE_M3_HF_ID),
                deadline_s=_load_deadline_s(),
                what=f"SentenceTransformer({_BGE_M3_HF_ID!r})",
            )
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

    def warmup(self) -> None:
        """Load the model + run one encode to JIT-compile MPS kernels.

        Bypasses :meth:`_raise_if_warming` — the warming gate exists to
        fast-fail FOREGROUND callers; the background warm thread is
        what's supposed to clear it. Earlier code routed warmup through
        :meth:`embed` which hits the gate first and raises
        ``Upstream("embedder warming")`` before reaching
        :meth:`_ensure_loaded`, leaving ``self._st`` permanently None
        (the 2026-06-15 → 2026-06-16 regression). Call ``_ensure_loaded``
        directly here and run one tiny encode so the first foreground
        request lands on a fully-JIT'd model.
        """
        st = self._ensure_loaded()
        # One-token encode kicks off MPS kernel compilation. Discard
        # the vector — it's the side effect we want.
        st.encode(["warmup"], normalize_embeddings=True)  # type: ignore[attr-defined]

    def unload(self) -> None:
        """Release the loaded ``SentenceTransformer`` + free accelerator
        cache (§F cycle b idle-unload). Idempotent — a no-op if nothing
        is loaded. Dropping the Python reference alone doesn't return
        MPS/CUDA-allocated memory to the OS/driver; ``gc.collect()``
        clears any reference cycles the ``SentenceTransformer`` object
        graph holds, then the accelerator's own cache-empty call
        releases what it was holding. Guarded by availability so this
        runs harmlessly on CPU-only hosts too. The next ``embed()`` /
        ``warmup()`` call re-loads lazily via ``_ensure_loaded``.
        """
        if self._st is None:
            return
        self._st = None
        import gc

        gc.collect()
        try:
            import torch
        except ImportError:  # pragma: no cover - torch always present here
            return
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            torch.mps.empty_cache()
        elif torch.cuda.is_available():
            torch.cuda.empty_cache()

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
# Remote — HTTP client to a `precis serve-embeddings` service.
# ---------------------------------------------------------------------------


class EmbedderUnavailable(RuntimeError):
    """The embedding service is *transiently* unreachable.

    Raised by :meth:`RemoteEmbedder.embed` when every endpoint exhausts
    its retry budget on a *retryable* condition — ``429`` (admission
    control: the service is at capacity), ``5xx`` (the service is
    restarting / its model is still warming and ``/embed`` returns a
    500), or a connection-level failure (refused / timeout while the
    process is down). It is **not** raised for a genuine per-text or
    config error (dim mismatch, wire skew, non-retryable 4xx) — those
    stay plain ``RuntimeError`` / ``ValueError``.

    The distinction lets a worker tell "the embedder is briefly busy /
    bouncing" apart from "this row is bad". On the former a pass should
    *defer* the batch (leave the rows unclaimed, write no failure
    marker, back off) rather than mark every chunk ``failed`` — which
    both lit up the status "FAILED PASSES" panel with noise and, in the
    embed handler's per-row fallback, fired N more single-text requests
    that *deepened* the very overload that caused the 429.
    """


def _urllib_transport(
    method: str, url: str, body: dict | None, timeout: float
) -> tuple[int, dict]:
    """Default :data:`Transport` — a stdlib ``urllib`` round-trip.

    No third-party HTTP dep, so the torch-free serve/worker images stay
    tiny. Returns ``(status, parsed_json)``. HTTP error
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
    corruption.

    ``timeout`` default (300s, embedder-wedge-hardening.md §5): the
    2026-08-10 post-restart caspar/balthazar incident was NOT a load
    wedge — the embedder finished computing a batch, but a 30s client
    timeout hung up first (worker: "embedder unavailable", embedder log:
    ``BrokenPipeError`` writing the response the client had already
    abandoned). A CPU-host batch legitimately takes longer than 30s, so
    every retry recomputed the whole batch server-side and timed out
    again — the retry loop amplified the very overload it was meant to
    ride out. 300s comfortably covers a slow CPU batch; per-call
    override via ``PrecisConfig.embedder_timeout`` / ``PRECIS_EMBEDDER_TIMEOUT``.
    """

    def __init__(
        self,
        url: str,
        *,
        expected_dim: int | None = None,
        timeout: float = 300.0,
        max_retries: int = 5,
        backoff_base: float = 0.5,
        backoff_max: float = 15.0,
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

    @property
    def backend(self) -> str:
        return "remote"

    @property
    def is_production(self) -> bool:
        return True

    @property
    def url(self) -> str:
        """Comma-joined endpoint list — for status/observability display.

        Not part of the ``Embedder`` Protocol (only the remote backend
        has a URL); read via ``getattr(embedder, "url", None)``.
        """
        return ", ".join(self._endpoints)

    def is_ready(self) -> bool:
        # Remote backend has no local warmup phase. The first call
        # pays a small ``/model`` round-trip; subsequent calls don't.
        # Returning True here keeps the dispatch fast-path uncluttered;
        # genuine transport failures still surface via ``embed()``'s
        # existing retry / RuntimeError path.
        return True

    def warmup(self) -> None:
        # No-op: the remote backend's model is loaded server-side.
        return None

    def unload(self) -> None:
        # No-op: idle-unload is the SERVICE's own concern (it wraps the
        # in-process backend directly, never a RemoteEmbedder) — this
        # client has no local weights to release.
        return None

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
        # Every endpoint exhausted its retry budget on a *retryable*
        # condition (429 / 5xx / transport error — non-retryable statuses
        # ``return`` above and never reach here). Surface a typed
        # ``EmbedderUnavailable`` so callers can defer the batch instead
        # of marking rows failed.
        raise EmbedderUnavailable(
            f"all embedder endpoints failed ({self._endpoints})"
        ) from last_err

    def _backoff(self, attempt: int) -> None:
        delay = min(self._backoff_max, self._backoff_base * (2**attempt))
        # Full jitter — spreads retries from concurrent callers.
        self._sleep(random.uniform(0, delay))


# ---------------------------------------------------------------------------
# Bulkhead — bound how many threads one process can park inside an embed
# ---------------------------------------------------------------------------


class BoundedConcurrencyEmbedder:
    """Wraps an `Embedder` so at most ``max_concurrency`` embeds are in
    flight at once in this process, **shedding** rather than queueing past
    that.

    The second half of the gripe-244419 fix. Splitting the interactive
    timeout budget (``PrecisConfig.embedder_interactive_timeout``) bounds
    how *long* one request-path embed can park an anyio worker thread, but
    not how *many* park at once: ``server.py`` registers the seven verbs as
    plain sync callables, so FastMCP runs each in a thread, and nothing sets
    ``current_default_thread_limiter``. Enough simultaneous slow embeds
    still exhaust the default 40-thread pool, after which every call to that
    ``precis serve`` queues for a thread — including a ``get(kind='skill')``
    that touches neither DB nor embedder. The timeout split turned a wedge
    needing ``kill`` + a human ``/mcp`` into a brownout; this bounds the
    brownout's blast radius to the callers that actually wanted an embed.

    **Sheds, never queues.** ``acquire(blocking=False)``: a caller that
    can't get a slot raises immediately instead of waiting for one. Queueing
    would hold the very thread this exists to protect — the wedge
    reintroduced through a friendlier door.

    **Raises ``Upstream``, not ``EmbedderUnavailable``.** Both are caught by
    every request-path embed site, but only ``Upstream`` is *surfaced*:
    ``runtime/search.py`` branches on it to emit a "semantic search degraded
    to lexical-only, retry shortly" hint, so a shed request doesn't read as
    a definitive "no matches" (the R2#2 finding that branch exists for), and
    ``runtime/angle.py`` — purely semantic, no lexical leg — re-raises it as
    a clean retryable error rather than a bare 500. The precedent is
    :meth:`BgeM3Embedder._raise_if_warming`, which picks ``Upstream`` for
    the same reason: a fast-fail guard in front of an embed, signalling
    transient unavailability the agent should retry rather than a
    misconfiguration.

    Request-path only — :func:`precis.runtime.factory.build_runtime` applies
    it. Workers deliberately go unwrapped: a worker pass *wants* to block on
    a busy embedder (that patience is what ``embedder_timeout`` is for), it
    has no thread pool to starve, and shedding there would just churn
    claims.
    """

    def __init__(self, inner: Embedder, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._inner = inner
        self._max_concurrency = max_concurrency
        self._sem = threading.BoundedSemaphore(max_concurrency)

    @property
    def inner(self) -> Embedder:
        """The wrapped embedder (exposed for tests + observability)."""
        return self._inner

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def dim(self) -> int:
        return self._inner.dim

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def backend(self) -> str:
        # ``backend``/``is_production`` are duck-typed extras, not part
        # of the ``Embedder`` Protocol (see the note above the
        # Protocol class) — every real backend carries them, but an
        # ``Embedder``-typed ``self._inner`` doesn't statically
        # guarantee it, hence ``getattr`` with a safe default here
        # instead of a direct attribute access.
        return getattr(self._inner, "backend", type(self._inner).__name__)

    @property
    def is_production(self) -> bool:
        return getattr(self._inner, "is_production", True)

    def is_ready(self) -> bool:
        return self._inner.is_ready()

    def warmup(self) -> None:
        # Ungated, matching ``BgeM3Embedder.warmup``'s bypass of its own
        # warming gate: warmup runs on a background thread that is not the
        # thread pool this protects, and making it sheddable would let the
        # boot warm fail on the very contention it exists to prevent.
        self._inner.warmup()

    def unload(self) -> None:
        # Idle-unload is the service's concern and never runs on the
        # request path — no slot needed.
        self._inner.unload()

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._slot(f"{len(texts)} text(s)"):
            return self._inner.embed(texts)

    def embed_one(self, text: str) -> list[float]:
        # Delegates to the INNER ``embed_one``, not to ``self.embed`` — the
        # latter would take a second slot for one logical embed, so a single
        # caller could shed itself and N concurrent callers would consume
        # 2N slots.
        with self._slot("1 text"):
            return self._inner.embed_one(text)

    @contextmanager
    def _slot(self, what: str) -> Iterator[None]:
        if not self._sem.acquire(blocking=False):
            from precis.errors import Upstream

            raise Upstream(
                f"embedder busy — {self._max_concurrency} embeds already in "
                f"flight in this process, so this one ({what}) was shed "
                "rather than queued; semantic search degraded to "
                "lexical-only for this call",
                next="retry shortly, or pass mode='lexical' for a "
                "deterministic keyword-only pass",
            )
        try:
            yield
        finally:
            self._sem.release()


# ---------------------------------------------------------------------------
# Factory — config-driven selection
# ---------------------------------------------------------------------------


#: Fallback ``RemoteEmbedder`` timeout (seconds) used by :func:`make_embedder`
#: when the caller passes no explicit ``timeout`` — e.g. the bare
#: ``make_embedder(cfg.embedder, dim=...)`` call sites (``cli/ingest.py``,
#: ``cli/patent.py``, ``cli/sim.py``, ``cli/perplexity.py``) that don't
#: thread ``PrecisConfig.embedder_timeout`` through. Read at call time (not
#: baked into the signature default) so a test/daemon setting the env after
#: import still takes effect. embedder-wedge-hardening.md §5: 300s, not the
#: old 30s — a CPU-host embed batch legitimately takes longer than 30s, and
#: a client that hangs up on a still-computing batch just amplifies the
#: retry storm (the 2026-08-10 caspar/balthazar incident).
_EMBEDDER_TIMEOUT_ENV = "PRECIS_EMBEDDER_TIMEOUT"
_EMBEDDER_TIMEOUT_DEFAULT_S = 300.0


def make_embedder(
    name: str,
    *,
    dim: int = 1024,
    url: str | None = None,
    timeout: float | None = None,
    max_retries: int = 3,
) -> Embedder:
    """Return an `Embedder` for the given config name.

    - ``"mock"``    → deterministic ``MockEmbedder(dim=dim)``
    - ``"bge-m3"``  → real ``BgeM3Embedder()`` (loads the model)
    - ``"remote"``  → ``RemoteEmbedder(url, expected_dim=dim)`` (HTTP
      client to a ``precis serve-embeddings`` service; requires ``url``)

    ``timeout`` / ``max_retries`` apply only to ``"remote"``. ``timeout=None``
    (the default) falls back to the ``PRECIS_EMBEDDER_TIMEOUT`` env, else
    :data:`_EMBEDDER_TIMEOUT_DEFAULT_S` (300s) — the same knob
    ``PrecisConfig.embedder_timeout`` / ``cli worker --embedder-timeout``
    read, so a bare call site that forgets to thread it through still gets
    a sane default instead of silently reverting to a too-short one.

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
        eff_timeout = (
            timeout
            if timeout is not None
            else float(
                os.environ.get(_EMBEDDER_TIMEOUT_ENV, str(_EMBEDDER_TIMEOUT_DEFAULT_S))
            )
        )
        return RemoteEmbedder(
            url, expected_dim=dim, timeout=eff_timeout, max_retries=max_retries
        )
    raise ValueError(
        f"unknown embedder name: {name!r} - expected 'mock', 'bge-m3', or 'remote'"
    )


__all__ = [
    "BgeM3Embedder",
    "Embedder",
    "EmbedderUnavailable",
    "MockEmbedder",
    "RemoteEmbedder",
    "make_embedder",
]
