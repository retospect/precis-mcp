"""Tests for the embedder abstraction (Protocol + MockEmbedder)."""

from __future__ import annotations

import math

import pytest

from precis.embedder import (
    _BGE_M3_MAX_CHARS,
    BgeM3Embedder,
    Embedder,
    MockEmbedder,
    make_embedder,
)


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

    def test_bge_m3_construction_is_lazy(self) -> None:
        # Constructing the embedder must NOT load torch /
        # sentence-transformers — those are deferred to first embed()
        # to keep MCP startup fast (Windsurf and similar clients have
        # short handshake timeouts). Verifies the change in dba51f23.
        e = make_embedder("bge-m3")
        assert e.dim == 1024  # documented constant; no model load needed
        # ``model`` returns the precis **registry key** (FK target in
        # the ``embedders`` table), not the HuggingFace id. The HF id
        # ``BAAI/bge-m3`` is an internal constant the embedder uses
        # only when actually loading weights.
        assert e.model == "bge-m3"

    def test_bge_m3_clear_error_on_missing_dep(self) -> None:
        # When the optional backend is missing, the agent must get a
        # clear ImportError pointing at the install command — not a
        # confusing AttributeError or ModuleNotFoundError mid-request.
        try:
            import sentence_transformers  # noqa: F401

            pytest.skip("sentence-transformers installed; missing-dep test n/a")
        except ImportError:
            pass
        # Construction itself is lazy (does not import st), but embed()
        # now must surface a clear ImportError.
        e = make_embedder("bge-m3")
        with pytest.raises(ImportError, match="sentence-transformers"):
            e.embed_one("anything")


class _RecordingEncoder:
    """Stand-in for ``sentence_transformers.SentenceTransformer``.

    Records every batch passed to ``encode()`` so tests can assert what
    the real model would have received. Returns deterministic
    zero-vectors so the surrounding code path runs end-to-end.
    """

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def encode(self, texts, normalize_embeddings: bool = True):
        self.calls.append(list(texts))
        return [[0.0] * self.dim for _ in texts]


class TestBgeM3CharTruncation:
    """Defensive char-level truncation in :class:`BgeM3Embedder`.

    Regression for the MPS-OOM failure on a corrupted-OCR table block
    (~192 KiB single string) that bypassed upstream chunking. Without
    truncation, sentence-transformers tried to allocate ~73 GiB on MPS
    before the tokenizer's 8192-token cap kicked in.
    """

    def _embedder_with_fake_encoder(self) -> tuple[BgeM3Embedder, _RecordingEncoder]:
        e = BgeM3Embedder()
        fake = _RecordingEncoder(dim=1024)
        e._st = fake
        return e, fake

    def test_short_text_passes_through_untruncated(self) -> None:
        e, fake = self._embedder_with_fake_encoder()
        e.embed_one("a short paragraph")
        assert fake.calls == [["a short paragraph"]]

    def test_oversized_text_is_truncated(self) -> None:
        e, fake = self._embedder_with_fake_encoder()
        big = "x" * (_BGE_M3_MAX_CHARS * 4)
        e.embed_one(big)
        sent = fake.calls[0][0]
        assert len(sent) == _BGE_M3_MAX_CHARS
        assert sent == big[:_BGE_M3_MAX_CHARS]

    def test_block_count_preserved_in_batch(self) -> None:
        # The store relies on len(texts) == len(returned_vectors). The
        # truncation guard must not split or drop any input.
        e, fake = self._embedder_with_fake_encoder()
        texts = [
            "small one",
            "y" * (_BGE_M3_MAX_CHARS * 2),
            "small two",
            "z" * (_BGE_M3_MAX_CHARS + 1),
        ]
        result = e.embed(texts)
        assert len(result) == len(texts)
        sent = fake.calls[0]
        assert len(sent) == 4
        assert sent[0] == "small one"
        assert sent[2] == "small two"
        assert len(sent[1]) == _BGE_M3_MAX_CHARS
        assert len(sent[3]) == _BGE_M3_MAX_CHARS

    def test_under_cap_inputs_pass_by_identity(self) -> None:
        # Pin the implementation choice that under-cap strings are not
        # copied (hot path stays allocation-free for typical paragraphs).
        e, fake = self._embedder_with_fake_encoder()
        small = "fits easily"
        big = "x" * (_BGE_M3_MAX_CHARS + 100)
        e.embed([small, big])
        sent = fake.calls[0]
        assert sent[0] is small
        assert sent[1] is not big

    def test_default_cap_is_safe_for_bge_m3_attention(self) -> None:
        # Sanity bound: keep the constant in a tight range so future
        # tuning is deliberate.
        assert 8_000 <= _BGE_M3_MAX_CHARS <= 32_000

    def test_empty_batch_short_circuits_before_load(self) -> None:
        # Empty batches must not trigger model load at all — important
        # for slim test environments without sentence-transformers.
        e = BgeM3Embedder()
        assert e.embed([]) == []


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

            def is_ready(self) -> bool:
                return True

        assert isinstance(Minimal(), Embedder)
