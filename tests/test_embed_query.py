"""Unit tests for the safe query-embedding helper.

The helper exists so a misbehaving embedder degrades a search to
lexical-only instead of 500ing (gripes #38684 / #38690). These tests
pin the three branches: no embedder, a working embedder, and a raising
embedder.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.embedder import EmbedderUnavailable
from precis.errors import Upstream
from precis.utils.embed_query import embed_query, query_vec_for


class _OkEmbedder:
    def embed_one(self, q: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _RaisingEmbedder:
    def embed_one(self, q: str) -> list[float]:
        raise RuntimeError("remote embed endpoint down")


def test_none_embedder_degrades_to_lexical() -> None:
    assert embed_query(None, "anything") is None


def test_working_embedder_returns_vector() -> None:
    assert embed_query(_OkEmbedder(), "photocatalysis") == [0.1, 0.2, 0.3]


def test_raising_embedder_degrades_instead_of_propagating() -> None:
    # The whole point: a failing embedder must NOT propagate (which would
    # surface as a 500), it returns None so the lexical leg still answers.
    assert embed_query(_RaisingEmbedder(), "chunk count") is None


def test_degrades_on_any_exception_type() -> None:
    class _WeirdEmbedder:
        def embed_one(self, q: str) -> Any:
            raise ValueError("degenerate query")

    assert embed_query(_WeirdEmbedder(), "*") is None


# --- query_vec_for: the mode='semantic' loudness split (gripe #254606) ---


def test_semantic_mode_raising_embedder_raises_upstream() -> None:
    # An explicit semantic request must not silently degrade to lexical:
    # zero hits there reads as "no matches in the corpus" and has caused
    # agents to mark well-sourced claims UNVERIFIABLE.
    with pytest.raises(Upstream) as exc_info:
        query_vec_for(_RaisingEmbedder(), "conceptual paraphrase", mode="semantic")
    assert "hybrid" in str(exc_info.value.next)


def test_default_mode_raising_embedder_still_degrades() -> None:
    # Hybrid/default keeps the #38684 degrade: the lexical leg answers.
    assert query_vec_for(_RaisingEmbedder(), "chunk count", mode=None) is None
    assert query_vec_for(_RaisingEmbedder(), "chunk count", mode="hybrid") is None


def test_semantic_mode_no_embedder_still_degrades() -> None:
    # Embedder-not-wired is a deployment shape, not a transient fault;
    # the store's tested lexical fallback stays in charge.
    assert query_vec_for(None, "anything", mode="semantic") is None


def test_lexical_and_verbatim_skip_embed_entirely() -> None:
    assert query_vec_for(_RaisingEmbedder(), "kw", mode="lexical") is None
    assert query_vec_for(_RaisingEmbedder(), "kw", mode="verbatim") is None


# --- gripe #450123 option d: capacity (retry-after) surfaces as such ---


class _CapacityEmbedder:
    """Raises the typed capacity failure — the queue timed out, the
    service is not down — carrying a retry-after hint."""

    def embed_one(self, q: str) -> list[float]:
        raise EmbedderUnavailable(
            "embedder at capacity (429 after queueing)",
            retry_after_s=2,
            last_status=429,
        )


def test_semantic_mode_capacity_error_surfaces_retry_after() -> None:
    with pytest.raises(Upstream) as exc_info:
        query_vec_for(_CapacityEmbedder(), "conceptual paraphrase", mode="semantic")
    assert "capacity" in str(exc_info.value)
    assert "~2 s" in str(exc_info.value)
    assert "Capacity, not absence" in str(exc_info.value.next)


class _CapacityBatchEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbedderUnavailable(
            "embedder at capacity (429 after queueing)",
            retry_after_s=2,
            last_status=429,
        )


def test_batch_semantic_mode_capacity_error_surfaces_retry_after() -> None:
    from precis.handlers._paper_search import _embed_query_batch

    with pytest.raises(Upstream) as exc_info:
        _embed_query_batch(_CapacityBatchEmbedder(), ["q", "rephrase"], "semantic")
    assert "capacity" in str(exc_info.value)
    assert "~2 s" in str(exc_info.value)
    assert "Capacity, not absence" in str(exc_info.value.next)


# --- _embed_query_batch: the broad-retrieval door has the same split ---


class _RaisingBatchEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("remote embed endpoint down")


def test_batch_semantic_mode_raising_embedder_raises_upstream() -> None:
    from precis.handlers._paper_search import _embed_query_batch

    with pytest.raises(Upstream):
        _embed_query_batch(_RaisingBatchEmbedder(), ["q", "rephrase"], "semantic")


def test_batch_default_mode_raising_embedder_still_degrades() -> None:
    from precis.handlers._paper_search import _embed_query_batch

    assert _embed_query_batch(_RaisingBatchEmbedder(), ["q"], None) == []
    assert _embed_query_batch(_RaisingBatchEmbedder(), ["q"], "hybrid") == []
    assert _embed_query_batch(None, ["q"], "semantic") == []
