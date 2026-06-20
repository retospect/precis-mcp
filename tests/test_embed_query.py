"""Unit tests for the safe query-embedding helper.

The helper exists so a misbehaving embedder degrades a search to
lexical-only instead of 500ing (gripes #38684 / #38690). These tests
pin the three branches: no embedder, a working embedder, and a raising
embedder.
"""

from __future__ import annotations

from typing import Any

from precis.utils.embed_query import embed_query


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
