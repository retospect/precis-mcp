"""Reusable block helpers for ingest pipelines.

Three small utilities that any pipeline (paper, patent, future
formats) can share without dragging in bundle-specific code:

* :class:`ParsedBlock` — a (text, embedding, density) triple, the
  unit of work that travels from extraction to the chunks table.
* :func:`classify_density` — three-bucket text-density heuristic
  (sparse / medium / dense). Cheap and side-effect-free; handlers
  re-run it during sweeps when the algorithm changes.
* :func:`fill_embeddings` — apply an :class:`Embedder` to every
  block that doesn't already have a dim-compatible vector.

Lifted from the legacy ``ingest/_legacy.py`` (B7) where they were
shared with bundle parsing. The bundle parsing dies with B7, but
these utilities are generic enough to keep as their own module —
patent ingest already depends on them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from precis.embedder import Embedder
from precis.store.types import Density

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """One block of text ready for ingest.

    ``embedding`` is ``None`` until either the producer pre-computed
    one or :func:`fill_embeddings` materialises it. ``density``
    classifies the block on the sparse/medium/dense spectrum (see
    :func:`classify_density`); ``None`` means "not classified yet —
    fall back to the heuristic when needed".
    """

    text: str
    embedding: list[float] | None
    density: Density | None


def classify_density(text: str) -> Density:
    """Three-bucket classifier: ``sparse`` / ``medium`` / ``dense``.

    Cheap heuristic:

    * **sparse** — fewer than 20 tokens, or a high newline ratio
      (table-of-contents, headings, single-line list items).
    * **dense** — more than 10 % digit characters per token
      (numeric tables, ID lists, dense data appendices).
    * **medium** — everything else (normal prose).

    Schema doesn't constrain the algorithm: handlers can re-run
    this via a sweep job if the heuristic is refined later. Empty
    input is ``sparse``.
    """
    if not text:
        return "sparse"
    n_tokens = max(len(text.split()), 1)
    n_digits = sum(c.isdigit() for c in text)
    nl_density = text.count("\n") / n_tokens
    if n_tokens < 20 or nl_density > 0.15:
        return "sparse"
    if n_digits / n_tokens > 0.10:
        return "dense"
    return "medium"


def fill_embeddings(
    blocks: Iterable[ParsedBlock],
    *,
    embedder: Embedder,
) -> list[ParsedBlock]:
    """Re-embed blocks that lack a dim-compatible vector.

    Returns a *new* list — input ``ParsedBlock`` instances are
    immutable. Blocks whose ``embedding`` already matches
    ``embedder.dim`` are passed through untouched; the rest are
    batched into one ``embedder.embed`` call so a 1k-block paper
    is one network round-trip, not 1k.
    """
    items = list(blocks)
    todo_indices: list[int] = []
    todo_texts: list[str] = []
    for i, b in enumerate(items):
        if b.embedding is not None and len(b.embedding) == embedder.dim:
            continue
        todo_indices.append(i)
        todo_texts.append(b.text)

    if not todo_indices:
        return items

    log.info("embedding %d blocks", len(todo_indices))
    new_vecs = embedder.embed(todo_texts)
    rebuilt = list(items)
    for i, vec in zip(todo_indices, new_vecs, strict=False):
        b = rebuilt[i]
        rebuilt[i] = ParsedBlock(text=b.text, embedding=vec, density=b.density)
    return rebuilt


__all__ = ["ParsedBlock", "classify_density", "fill_embeddings"]
