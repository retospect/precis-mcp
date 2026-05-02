"""Tests for the shared parsed-blocks → BlockInsert pipeline.

The helper is a thin glue layer: it batches embedding, attaches per-
block metadata, and produces ``BlockInsert`` rows in input order.
These tests pin each axis (empty input, no embedder, with embedder,
meta_for callback) so future refactors can't silently drop a feature
the four call sites rely on.
"""

from __future__ import annotations

from typing import Any

from precis.embedder import MockEmbedder
from precis.utils.block_ingest import to_block_inserts
from precis.utils.md_parse import block_meta, parse_markdown
from precis.utils.plaintext_parse import parse_plaintext


def test_empty_input_returns_empty_list() -> None:
    """Empty input → no embedder calls, no allocations beyond the empty list."""
    embedder = MockEmbedder(dim=4)
    out = to_block_inserts([], embedder=embedder)
    assert out == []


def test_no_embedder_leaves_embeddings_none() -> None:
    blocks = parse_markdown("# Title\n\nFirst paragraph.\n\nSecond.\n")
    inserts = to_block_inserts(blocks, embedder=None)
    assert len(inserts) == len(blocks)
    assert all(ins.embedding is None for ins in inserts)


def test_with_embedder_attaches_one_vector_per_block() -> None:
    """Production path: embedder is wired → every block carries a vector."""
    embedder = MockEmbedder(dim=8)
    blocks = parse_markdown(
        "# Title\n\nFirst paragraph about whales.\n\nSecond about ducks.\n"
    )
    inserts = to_block_inserts(blocks, embedder=embedder)
    assert len(inserts) == len(blocks)
    for ins in inserts:
        assert ins.embedding is not None
        assert len(ins.embedding) == 8


def test_embedder_called_in_one_batch(monkeypatch: Any) -> None:
    """The helper must batch — serial embed_one calls lose the prod
    bge-m3 vectorisation win."""
    embedder = MockEmbedder(dim=4)
    n_calls = 0

    real_embed = embedder.embed

    def counting_embed(texts: list[str]) -> list[list[float]]:
        nonlocal n_calls
        n_calls += 1
        return real_embed(texts)

    monkeypatch.setattr(embedder, "embed", counting_embed)

    blocks = parse_markdown("# A\n\nFirst.\n\nSecond.\n\nThird.\n")
    to_block_inserts(blocks, embedder=embedder)
    assert n_calls == 1, f"expected one batched embed call, got {n_calls}"


def test_pos_slug_text_propagate_from_parsed_block() -> None:
    blocks = parse_markdown("# Title\n\nBody text here.\n")
    inserts = to_block_inserts(blocks, embedder=None)
    assert [ins.pos for ins in inserts] == [b.pos for b in blocks]
    assert [ins.slug for ins in inserts] == [b.slug for b in blocks]
    assert [ins.text for ins in inserts] == [b.text for b in blocks]


def test_meta_for_callback_runs_per_block() -> None:
    """The meta builder is invoked for every block; default is empty dict."""
    blocks = parse_markdown("# Title\n\nFirst.\n")
    # Default — no meta_for → meta is {}.
    inserts = to_block_inserts(blocks, embedder=None)
    assert all(ins.meta == {} for ins in inserts)

    # With meta_for → each row carries the builder's dict.
    inserts = to_block_inserts(blocks, embedder=None, meta_for=block_meta)
    for ins, b in zip(inserts, blocks):
        assert ins.meta["kind"] == b.kind
        assert ins.meta["line_start"] == b.line_start
        assert ins.meta["line_end"] == b.line_end


def test_works_for_plaintext_blocks_too() -> None:
    """The protocol matches PlaintextBlock structurally — no inheritance,
    no adapter. This is the win: one pipeline, two parser families."""
    blocks = parse_plaintext("first paragraph.\n\nsecond paragraph.\n")
    inserts = to_block_inserts(
        blocks,
        embedder=None,
        meta_for=lambda pb: {"line_start": pb.line_start, "line_end": pb.line_end},
    )
    assert len(inserts) == 2
    assert inserts[0].text == "first paragraph."
    assert inserts[1].text == "second paragraph."
    assert inserts[0].meta == {"line_start": 1, "line_end": 1}
    assert inserts[1].meta == {"line_start": 3, "line_end": 3}
