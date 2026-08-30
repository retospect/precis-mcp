"""Shared *parsed-blocks → ChunkInsert with embeddings* pipeline.

Two parser families produce parsed blocks today:

- :func:`precis.utils.md_parse.parse_markdown` → :class:`MdChunk`
  (typed structure: heading / paragraph / list / table / code).
- :func:`precis.utils.plaintext_parse.parse_plaintext` →
  :class:`PlaintextChunk` (paragraph splitting only).

Three handlers feed those into the store today:

- :class:`MarkdownHandler` (file → blocks).
- :class:`PlaintextHandler` (file → blocks).
- :class:`_PerplexityBase` (API / imported markdown body → blocks).

Before this module each handler had its own *embed-then-build-
ChunkInsert-list* glue (~30 lines × 4 sites). This helper owns
that step so each call site stays focused on its own parsing +
meta-extraction.

Key choices
-----------

* **Batch embed.** One :py:meth:`Embedder.embed` call across the
  whole batch — the backend can vectorise. Serial ``embed_one``
  calls (the previous markdown shape) gave up that win and made
  CI tests slower for no benefit.

* **Per-kind meta is a callback.** Each parser produces a
  different block dataclass with different per-kind meta
  (markdown carries ``kind``/``heading_level``; plaintext just
  carries line spans). The helper takes a ``meta_for`` closure
  so each caller controls its own meta layout without the helper
  growing kind-aware branches.

* **Slug + pos come from the parsed block.** The helper trusts
  the parser's slug-minting; both md_parse and plaintext_parse
  guarantee idempotent, content-derived slugs (so re-ingesting
  the same file produces the same slugs and the agent's
  selectors keep resolving).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from precis.embedder import Embedder
from precis.store.types import ChunkInsert


class ParsedTextChunk(Protocol):
    """Structural protocol for any parsed-block type that this helper
    can ingest.

    Both :class:`precis.utils.md_parse.MdChunk` and
    :class:`precis.utils.plaintext_parse.PlaintextChunk` satisfy this
    protocol structurally — no explicit inheritance needed. Declared as
    read-only properties (not plain attributes) because both concrete
    block types are frozen dataclasses; a plain-attribute protocol
    demands settability and mypy rejects frozen fields against it.
    """

    @property
    def pos(self) -> int: ...

    @property
    def slug(self) -> str: ...

    @property
    def text(self) -> str: ...


# Bound type param so ``meta_for`` stays in agreement with the concrete
# block type passed in.  Without this, ``Callable[[ParsedTextChunk],
# ...]`` is contravariant on input and *rejects* per-kind closures
# like ``Callable[[MdChunk], ...]`` even though every MdChunk *is* a
# ParsedTextChunk.
def to_chunk_inserts[BlockT: ParsedTextChunk](
    blocks: Sequence[BlockT],
    *,
    embedder: Embedder | None,
    meta_for: Callable[[BlockT], dict[str, Any]] | None = None,
) -> list[ChunkInsert]:
    """Convert parsed text blocks into :class:`ChunkInsert` payloads.

    Args:
        blocks: Sequence of parsed blocks. Each must expose ``pos``,
            ``slug`` and ``text`` (see :class:`ParsedTextChunk`).
        embedder: Active embedder, or ``None``. When ``None``,
            :class:`ChunkInsert` rows are produced with
            ``embedding=None`` so callers / tests that don't need
            vectors can skip the cost.
        meta_for: Optional per-block metadata builder. Called with
            each parsed block; the returned dict lands on
            :attr:`ChunkInsert.meta`. When ``None``, meta is ``{}``.

    Returns:
        A list of :class:`ChunkInsert` rows in the same order as
        the input. Empty input → empty list (no embedder call).
    """
    if not blocks:
        return []

    embeddings: list[list[float]] | None = None
    if embedder is not None:
        # Batch in one call so production bge-m3 can vectorise the
        # whole file. The mock embedder fans this out internally.
        embeddings = embedder.embed([b.text for b in blocks])

    return [
        ChunkInsert(
            ord=b.pos,
            slug=b.slug,
            text=b.text,
            embedding=embeddings[i] if embeddings else None,
            meta=meta_for(b) if meta_for else {},
        )
        for i, b in enumerate(blocks)
    ]


__all__ = ["ParsedTextChunk", "to_chunk_inserts"]
