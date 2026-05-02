"""Shared *parsed-blocks → BlockInsert with embeddings* pipeline.

Two parser families produce parsed blocks today:

- :func:`precis.utils.md_parse.parse_markdown` → :class:`MdBlock`
  (typed structure: heading / paragraph / list / table / code).
- :func:`precis.utils.plaintext_parse.parse_plaintext` →
  :class:`PlaintextBlock` (paragraph splitting only).

Three handlers feed those into the store today:

- :class:`MarkdownHandler` (file → blocks).
- :class:`PlaintextHandler` (file → blocks).
- :class:`_PerplexityBase` (API / imported markdown body → blocks).

Before this module each handler had its own *embed-then-build-
BlockInsert-list* glue (~30 lines × 4 sites). This helper owns
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
from typing import Any, Protocol, TypeVar

from precis.embedder import Embedder
from precis.store.types import BlockInsert


class ParsedTextBlock(Protocol):
    """Structural protocol for any parsed-block type that this helper
    can ingest.

    Both :class:`precis.utils.md_parse.MdBlock` and
    :class:`precis.utils.plaintext_parse.PlaintextBlock` satisfy this
    protocol structurally — no explicit inheritance needed.
    """

    pos: int
    slug: str
    text: str


# Bound TypeVar so ``meta_for`` stays in agreement with the concrete
# block type passed in.  Without this, ``Callable[[ParsedTextBlock],
# ...]`` is contravariant on input and *rejects* per-kind closures
# like ``Callable[[MdBlock], ...]`` even though every MdBlock *is* a
# ParsedTextBlock.
_BlockT = TypeVar("_BlockT", bound=ParsedTextBlock)


def to_block_inserts(
    blocks: Sequence[_BlockT],
    *,
    embedder: Embedder | None,
    meta_for: Callable[[_BlockT], dict[str, Any]] | None = None,
) -> list[BlockInsert]:
    """Convert parsed text blocks into :class:`BlockInsert` payloads.

    Args:
        blocks: Sequence of parsed blocks. Each must expose ``pos``,
            ``slug`` and ``text`` (see :class:`ParsedTextBlock`).
        embedder: Active embedder, or ``None``. When ``None``,
            :class:`BlockInsert` rows are produced with
            ``embedding=None`` so callers / tests that don't need
            vectors can skip the cost.
        meta_for: Optional per-block metadata builder. Called with
            each parsed block; the returned dict lands on
            :attr:`BlockInsert.meta`. When ``None``, meta is ``{}``.

    Returns:
        A list of :class:`BlockInsert` rows in the same order as
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
        BlockInsert(
            pos=b.pos,
            slug=b.slug,
            text=b.text,
            embedding=embeddings[i] if embeddings else None,
            meta=meta_for(b) if meta_for else {},
        )
        for i, b in enumerate(blocks)
    ]


__all__ = ["ParsedTextBlock", "to_block_inserts"]
