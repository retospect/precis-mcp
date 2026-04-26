"""Tests for the embedder abstraction (Protocol + MockEmbedder)."""

from __future__ import annotations

import math

import pytest

from precis.embedder import Embedder, MockEmbedder, make_embedder


class TestMockEmbedder:
    def test_default_dim(self) -> None:
        e = MockEmbedder()
        assert e.dim == 1024
        assert e.model == "mock"

    def test_custom_dim(self) -> None:
        e = MockEmbedder(dim=64)
        v = e.embed_one("hello")
        assert len(v) == 64

    def test_zero_dim_raises(self) -> None:
        with pytest.raises(ValueError):
            MockEmbedder(dim=0)

    def test_deterministic(self) -> None:
        e = MockEmbedder(dim=32)
        a = e.embed_one("the quick brown fox")
        b = e.embed_one("the quick brown fox")
        assert a == b

    def test_different_text_different_vectors(self) -> None:
        e = MockEmbedder(dim=32)
        a = e.embed_one("alpha")
        b = e.embed_one("beta")
        assert a != b

    def test_unit_norm(self) -> None:
        e = MockEmbedder(dim=128)
        v = e.embed_one("normalize me")
        norm = math.sqrt(sum(x * x for x in v))
        assert abs(norm - 1.0) < 1e-6

    def test_batch_matches_one_by_one(self) -> None:
        e = MockEmbedder(dim=64)
        texts = ["a", "b", "c"]
        batch = e.embed(texts)
        one_by_one = [e.embed_one(t) for t in texts]
        assert batch == one_by_one

    def test_empty_batch(self) -> None:
        e = MockEmbedder(dim=32)
        assert e.embed([]) == []


class TestMakeEmbedder:
    def test_mock_factory(self) -> None:
        e = make_embedder("mock", dim=64)
        assert isinstance(e, MockEmbedder)
        assert e.dim == 64

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown embedder"):
            make_embedder("nonsense")  # type: ignore[arg-type]

    def test_bge_m3_lazy_loads(self) -> None:
        # The real backend isn't installed in CI; constructing it must
        # raise ImportError with an actionable hint, *not* succeed
        # silently or fail with a confusing AttributeError.
        try:
            import sentence_transformers  # noqa: F401

            pytest.skip("sentence-transformers installed; lazy-load smoke test n/a")
        except ImportError:
            pass
        with pytest.raises(ImportError, match="sentence-transformers"):
            make_embedder("bge-m3")


class TestProtocol:
    def test_mock_satisfies_protocol(self) -> None:
        e = MockEmbedder(dim=8)
        assert isinstance(e, Embedder)

    def test_protocol_accepts_minimal_impl(self) -> None:
        class Minimal:
            @property
            def dim(self) -> int:
                return 4

            @property
            def model(self) -> str:
                return "tiny"

            def embed(self, texts: list[str]) -> list[list[float]]:
                return [[0.0] * 4 for _ in texts]

            def embed_one(self, text: str) -> list[float]:
                return [0.0] * 4

        assert isinstance(Minimal(), Embedder)
