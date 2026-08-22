"""Lexical scoring over indexed markdown blocks.

Round-1 scope: substring/term hits on the heading breadcrumb (this
block's own title, if it's a heading, plus every ancestor heading
title) versus hits in the block's body text. Heading matches score
higher than body matches on the theory that a query matching the
section a block lives under is a stronger relevance signal than the
query merely appearing somewhere in the prose — the same intuition
`precis.handlers.python._score_symbol` applies to qualname vs.
docstring hits.

This is intentionally simple: no stemming, no IDF, no fuzzy matching.
It exists to (a) give the `md` kind usable search *before* the
embedder is warm and (b) act as the lexical leg of the RRF fusion
with cosine similarity below.

Scoring, in points:

- whole query (lowercased, as typed) is a substring of the heading
  breadcrumb + own title -> +6
- whole query is a substring of the body text                -> +1.5
- each whitespace-split query term found in the heading text  -> +2 / term
- each whitespace-split query term found in the body text     -> +0.5 / term

A block with no match anywhere scores 0.0 and is not a hit.

`cosine_search` is the semantic leg: cosine similarity between a
query vector and every block that already has a cached vector in an
`MdVectorCache` — blocks still awaiting embedding (a cold or warming
cache) are silently skipped rather than treated as a miss, which is
what lets a caller report partial "semantic: NN% indexed" coverage
without the search itself erroring. `fuse_blocks` combines a lexical
and a semantic hit list into one ranking via reciprocal rank fusion,
the same formula and `k=60` default as
`precis.store._blocks_ops.BlockStore.search_blocks_fused`.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from precis.md_index.types import MdBlockEntry, MdRepoIndex
from precis.md_index.vectors import MdVectorCache

_WEIGHT_HEADING_PHRASE = 6.0
_WEIGHT_BODY_PHRASE = 1.5
_WEIGHT_HEADING_TERM = 2.0
_WEIGHT_BODY_TERM = 0.5


def _heading_text(entry: MdBlockEntry) -> str:
    """The searchable heading-side text: ancestor breadcrumb + own title."""
    parts = list(entry.heading_path)
    if entry.title:
        parts.append(entry.title)
    return " ".join(parts).lower()


def score_block(entry: MdBlockEntry, query: str) -> float:
    """Lexical match score for one block against a query string.

    Higher is better; 0.0 means no match. See module docstring for
    the weight table.
    """
    q = query.strip().lower()
    if not q:
        return 0.0

    heading_text = _heading_text(entry)
    body_text = entry.text.lower()

    score = 0.0
    if q in heading_text:
        score += _WEIGHT_HEADING_PHRASE
    if q in body_text:
        score += _WEIGHT_BODY_PHRASE

    for term in q.split():
        if term in heading_text:
            score += _WEIGHT_HEADING_TERM
        if term in body_text:
            score += _WEIGHT_BODY_TERM

    return score


def search_blocks(
    index: MdRepoIndex, query: str, *, limit: int | None = None
) -> list[tuple[float, str, MdBlockEntry]]:
    """Score every block in `index` against `query`; return the hits
    (score > 0), highest first, as `(score, file, block)` tuples.

    Ties break on `(file, pos)` for deterministic output. `limit`
    truncates the result after sorting; `None` (default) returns
    every hit.
    """
    hits: list[tuple[float, str, MdBlockEntry]] = []
    for file, block in index.all_blocks():
        s = score_block(block, query)
        if s > 0:
            hits.append((s, file, block))

    hits.sort(key=lambda h: (-h[0], h[1], h[2].pos))
    if limit is not None:
        hits = hits[:limit]
    return hits


# ---------------------------------------------------------------------------
# Semantic leg: cosine top-k over cached vectors.
# ---------------------------------------------------------------------------


def cosine_search(
    index: MdRepoIndex,
    query_vec: Sequence[float],
    vectors: MdVectorCache,
    *,
    limit: int | None = None,
) -> list[tuple[float, str, MdBlockEntry]]:
    """Cosine-similarity ranking of `index`'s blocks against `query_vec`.

    Only blocks with a vector already present in `vectors` are
    scored — a block not yet embedded (cold/warming cache) is
    skipped, not scored 0, so it can't drag a true semantic non-match
    lower than an unembedded near-match. Ties break the same way
    `search_blocks` does, on `(file, pos)`.

    Returns `(score, file, block)` triples, highest similarity first.
    An all-zero `query_vec` (degenerate embed) returns no hits rather
    than dividing by zero.
    """
    q = np.asarray(query_vec, dtype=np.float32)
    qn = float(np.linalg.norm(q))
    if qn == 0.0:
        return []
    q = q / qn

    hits: list[tuple[float, str, MdBlockEntry]] = []
    for file, block in index.all_blocks():
        vec = vectors.get(block.sha256)
        if vec is None:
            continue
        vn = float(np.linalg.norm(vec))
        if vn == 0.0:
            continue
        score = float(np.dot(q, vec / vn))
        hits.append((score, file, block))

    hits.sort(key=lambda h: (-h[0], h[1], h[2].pos))
    if limit is not None:
        hits = hits[:limit]
    return hits


# ---------------------------------------------------------------------------
# Fusion: reciprocal rank fusion of the lexical + semantic legs.
# ---------------------------------------------------------------------------


def _fusion_key(file: str, block: MdBlockEntry) -> tuple[str, str]:
    """Stable identity for a block across the two independently-scored
    hit lists — `(file, slug)`, unique within a repo index."""
    return (file, block.slug)


def fuse_blocks(
    lexical: Sequence[tuple[float, str, MdBlockEntry]],
    semantic: Sequence[tuple[float, str, MdBlockEntry]],
    *,
    k: int = 60,
    limit: int | None = None,
) -> list[tuple[float, str, MdBlockEntry]]:
    """Reciprocal-rank-fuse a lexical and a semantic hit list.

    ``score = 1/(k + lex_rank) + 1/(k + sem_rank)`` over 1-indexed
    ranks; a block absent from one leg contributes 0 for that leg —
    same formula and default ``k=60`` as
    `precis.store._blocks_ops.BlockStore.search_blocks_fused`. Either
    input may be empty (e.g. the semantic leg is still warming, or a
    caller passed ``mode='lexical'``): fusion then degrades to a
    straight re-ranking of whichever leg is populated.

    Both inputs are expected pre-sorted (as `search_blocks` and
    `cosine_search` already return); rank is read off list position,
    not the raw score, exactly as the store's SQL does via
    `row_number() OVER (ORDER BY ...)`.
    """
    lex_rank = {_fusion_key(f, b): i + 1 for i, (_, f, b) in enumerate(lexical)}
    sem_rank = {_fusion_key(f, b): i + 1 for i, (_, f, b) in enumerate(semantic)}

    blocks_by_key: dict[tuple[str, str], tuple[str, MdBlockEntry]] = {}
    for _, f, b in lexical:
        blocks_by_key[_fusion_key(f, b)] = (f, b)
    for _, f, b in semantic:
        blocks_by_key.setdefault(_fusion_key(f, b), (f, b))

    fused: list[tuple[float, str, MdBlockEntry]] = []
    for key, (f, b) in blocks_by_key.items():
        score = 0.0
        lr = lex_rank.get(key)
        if lr is not None:
            score += 1.0 / (k + lr)
        sr = sem_rank.get(key)
        if sr is not None:
            score += 1.0 / (k + sr)
        fused.append((score, f, b))

    fused.sort(key=lambda h: (-h[0], h[1], h[2].pos))
    if limit is not None:
        fused = fused[:limit]
    return fused
