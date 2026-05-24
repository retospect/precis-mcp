"""Tests for :mod:`precis.ingest.blocks` — the reusable block helpers.

Two pure utility groups:

* :func:`classify_density` — three-bucket text classifier.
* :func:`fill_embeddings` — apply an :class:`Embedder` to blocks
  that lack a dim-compatible vector.

The bundle ingest tests this file replaces (``tests/test_ingest.py``)
went away with B7; what remained were the algorithm-level tests
for these two utilities and that's what's here.
"""

from __future__ import annotations

from precis.embedder import MockEmbedder
from precis.ingest.blocks import (
    ParsedBlock,
    classify_density,
    fill_embeddings,
)

# ---------------------------------------------------------------------------
# classify_density — pure heuristic
# ---------------------------------------------------------------------------


class TestClassifyDensity:
    def test_short_is_sparse(self) -> None:
        assert classify_density("hello world") == "sparse"

    def test_long_prose_is_medium(self) -> None:
        assert classify_density(" ".join(["word"] * 80)) == "medium"

    def test_digit_heavy_is_dense(self) -> None:
        assert classify_density(" ".join(["x", "5"] * 30)) == "dense"

    def test_many_newlines_is_sparse(self) -> None:
        text = "\n".join(["a"] * 30)
        assert classify_density(text) == "sparse"

    def test_empty_is_sparse(self) -> None:
        assert classify_density("") == "sparse"


# ---------------------------------------------------------------------------
# fill_embeddings — embed-on-demand
# ---------------------------------------------------------------------------


class TestFillEmbeddings:
    def test_keeps_existing_when_dim_matches(self) -> None:
        e = MockEmbedder(dim=8)
        existing = [0.5] * 8
        blocks = [ParsedBlock(text="x", embedding=existing, density="medium")]
        out = fill_embeddings(blocks, embedder=e)
        # Same object identity is not promised — but the value must be
        # untouched and the embedder must not have been invoked for
        # this block (we'd see a different vector).
        assert out[0].embedding == existing

    def test_fills_missing(self) -> None:
        e = MockEmbedder(dim=8)
        blocks = [
            ParsedBlock(text="alpha", embedding=None, density="medium"),
            ParsedBlock(text="beta", embedding=None, density="medium"),
        ]
        out = fill_embeddings(blocks, embedder=e)
        assert all(b.embedding is not None for b in out)
        assert all(b.embedding is not None and len(b.embedding) == 8 for b in out)

    def test_text_and_density_unchanged(self) -> None:
        e = MockEmbedder(dim=8)
        blocks = [ParsedBlock(text="abc", embedding=None, density="sparse")]
        out = fill_embeddings(blocks, embedder=e)
        assert out[0].text == "abc"
        assert out[0].density == "sparse"

    def test_refills_when_dim_mismatch(self) -> None:
        # Block carries a vector of the wrong dim — must be re-embedded.
        e = MockEmbedder(dim=8)
        wrong = [0.1] * 16
        blocks = [ParsedBlock(text="x", embedding=wrong, density=None)]
        out = fill_embeddings(blocks, embedder=e)
        assert out[0].embedding is not None
        assert len(out[0].embedding) == 8

    def test_empty_input_returns_empty(self) -> None:
        e = MockEmbedder(dim=8)
        assert fill_embeddings([], embedder=e) == []
