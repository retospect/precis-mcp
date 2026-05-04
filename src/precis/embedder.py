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
import math
import struct
from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into normalized float vectors."""

    @property
    def dim(self) -> int: ...

    @property
    def model(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]: ...


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

    def __init__(self, *, model_name: str = "BAAI/bge-m3") -> None:
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
            self._st = SentenceTransformer(self._model_name)
        return self._st

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
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
# Factory — config-driven selection
# ---------------------------------------------------------------------------


def make_embedder(name: str, *, dim: int = 1024) -> Embedder:
    """Return an `Embedder` for the given config name.

    - ``"mock"``    → deterministic ``MockEmbedder(dim=dim)``
    - ``"bge-m3"``  → real ``BgeM3Embedder()`` (loads the model)

    Raises ``ValueError`` for unknown names.
    """
    if name == "mock":
        return MockEmbedder(dim=dim)
    if name == "bge-m3":
        return BgeM3Embedder()
    raise ValueError(f"unknown embedder name: {name!r} - expected 'mock' or 'bge-m3'")


__all__ = ["BgeM3Embedder", "Embedder", "MockEmbedder", "make_embedder"]
