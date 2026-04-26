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


class BgeM3Embedder:
    """``BAAI/bge-m3`` via sentence-transformers. Optional dep.

    Construction loads the model into memory (slow); tests should not
    touch this — use ``MockEmbedder``. ``model`` and ``dim`` are
    introspected from the loaded model at construction time.
    """

    def __init__(self, *, model_name: str = "BAAI/bge-m3") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "sentence-transformers is not installed. "
                "Install with: pip install 'precis-mcp[paper]' "
                "or: pip install sentence-transformers"
            ) from exc

        self._st = SentenceTransformer(model_name)
        self._model_name = model_name
        # Probe with one short input so we know the dim.
        probe = self._st.encode(["dim probe"], normalize_embeddings=True)
        self._dim = int(probe.shape[1])

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model(self) -> str:
        return self._model_name

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        embs = self._st.encode(texts, normalize_embeddings=True)
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
    raise ValueError(f"unknown embedder name: {name!r} — expected 'mock' or 'bge-m3'")


__all__ = ["BgeM3Embedder", "Embedder", "MockEmbedder", "make_embedder"]
