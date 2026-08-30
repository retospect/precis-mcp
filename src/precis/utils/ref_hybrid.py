"""Ref-level hybrid search — fuse the title leg with the block leg.

``store.search_refs_lexical`` searches ``refs.title`` with
``websearch_to_tsquery``, which ANDs every stemmed term: one word the target
happens not to contain zeroes the whole query, and there is no semantic recall
at all. Its own docstring says so ("title-level stays lexical-only"). That is
survivable for a kind whose refs are one short line, and badly wrong for
``finding``, where the ref title *is* a claim sentence and the caller is
usually asking "do we already have a claim like this?".

The fix is not a new search primitive — ``search_chunks_fused`` already does
RRF over a lexical and a semantic leg. It was simply never called from the
ref-level paths. This module fuses that block leg back onto the title leg.

**Additive, never substitutive.** The title leg is kept because for some kinds
it is the *only* signal: ``todo`` has ~2.7k refs and ~37 body chunks, so a
block-only search would find essentially no todos. Every leg here can only add
candidates, never remove them.

Three legs, fused by reciprocal rank:

1. **title lexical** — ``search_refs_lexical`` (unchanged behaviour).
2. **block hybrid** — ``blocks.search_chunks_fused`` with a query vector from
   :func:`~precis.utils.embed_query.query_vec_for`, which honours ``mode=``
   (``'lexical'``/``'verbatim'`` skip the embed) and returns ``None`` when the
   embedder is missing or raising. ``search_chunks_fused`` then degrades to
   lexical-only by itself, so an embedder outage costs recall, not a 500.
3. **notation-normalized lexical** — when
   ``normalize_notation(q)`` changes the query, legs 1 and 2 are re-run on the
   canonical form. This is what lets an ASCII ``kOhm`` query reach a claim
   written ``kΩ`` after corpus normalization. It is deliberately an *extra*
   leg rather than a rewrite of ``q``: normalization is not always meaning-
   preserving for a query (``6-311++G**`` is a Pople basis set, not markup),
   and an extra leg can only fail to contribute.

Only the ASCII→canon direction is covered, because that is the direction an
agent types. The reverse (a ``kΩ`` query against a not-yet-normalized ``kOhm``
row) is left to the semantic leg; a symmetric folded index is a separate,
migration-sized change.
"""

from __future__ import annotations

from typing import Any

from precis.store import Ref
from precis.taproot.notation import normalize_notation
from precis.utils.embed_query import query_vec_for

#: Standard RRF damping constant, matching ``search_merge._merge_rrf`` and
#: ``search_chunks_fused``. A hit at rank *r* contributes ``1/(60+r)``.
RRF_K = 60

#: Block-leg over-fetch. One ref can own many matching chunks, so the block
#: query is widened before collapsing to best-chunk-per-ref, or a single
#: chunk-rich ref would crowd out every other candidate.
_BLOCK_OVERFETCH = 5


def _fuse(streams: list[list[Ref]], limit: int) -> list[Ref]:
    """Reciprocal-rank fusion over ref streams, best first.

    Deliberately not ``search_merge._merge_rrf``: that fuses ``SearchHit``
    objects for rendering, and building throwaway hits here just to fuse and
    unwrap them would be more code, not less. Same constant, same shape.
    """
    totals: dict[int, float] = {}
    first_seen: dict[int, Ref] = {}
    order: list[int] = []
    for stream in streams:
        for rank, ref in enumerate(stream, 1):
            totals[ref.id] = totals.get(ref.id, 0.0) + 1.0 / (RRF_K + rank)
            if ref.id not in first_seen:
                first_seen[ref.id] = ref
                order.append(ref.id)
    ranked = sorted(order, key=lambda rid: (-totals[rid], order.index(rid)))
    return [first_seen[rid] for rid in ranked[:limit]]


def _block_leg(
    store: Any,
    *,
    q: str,
    query_vec: list[float] | None,
    kind: str | None,
    tags: list[str] | None,
    limit: int,
    chunk_kinds: list[str] | None,
) -> list[Ref]:
    """Block-level hits collapsed to one ref each, best rank first."""
    raw = store.chunks.search_chunks_fused(
        q=q,
        query_vec=query_vec,
        kind=kind,
        tags=tags,
        limit=limit * _BLOCK_OVERFETCH,
        chunk_kinds=chunk_kinds,
    )
    best: dict[int, tuple[Ref, float]] = {}
    for _block, ref, rank in raw:
        current = best.get(ref.id)
        if current is None or rank > current[1]:
            best[ref.id] = (ref, rank)
    return [ref for ref, _rank in sorted(best.values(), key=lambda t: -t[1])]


def fused_ref_hits(
    store: Any,
    embedder: Any | None,
    *,
    q: str,
    kind: str | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
    mode: str | None = None,
    chunk_kinds: list[str] | None = None,
) -> list[Ref]:
    """Hybrid ref-level search: title lexical + block hybrid + notation leg.

    Returns refs best-first, at most ``limit``. An empty or whitespace ``q``
    returns ``[]`` — callers own the "no query" ergonomics (recency lists,
    ``BadInput``), which differ per handler.

    ``chunk_kinds`` scopes the block leg (``['finding_body']`` for claim hubs,
    so the leg matches the claim sentence rather than a chase-chain card).
    """
    if not (q and q.strip()):
        return []

    query_vec = query_vec_for(embedder, q, mode)

    streams: list[list[Ref]] = [
        [
            ref
            for ref, _rank in store.search_refs_lexical(
                q=q, kind=kind, tags=tags, limit=limit
            )
        ],
        _block_leg(
            store,
            q=q,
            query_vec=query_vec,
            kind=kind,
            tags=tags,
            limit=limit,
            chunk_kinds=chunk_kinds,
        ),
    ]

    canonical, _applied = normalize_notation(q)
    if canonical and canonical != q:
        streams.append(
            [
                ref
                for ref, _rank in store.search_refs_lexical(
                    q=canonical, kind=kind, tags=tags, limit=limit
                )
            ]
        )
        # Lexical-only on the canonical form: the semantic leg already ran on
        # the raw query and embeds notation variants close together, so a
        # second embed would spend a model call for near-duplicate recall.
        streams.append(
            _block_leg(
                store,
                q=canonical,
                query_vec=None,
                kind=kind,
                tags=tags,
                limit=limit,
                chunk_kinds=chunk_kinds,
            )
        )

    return _fuse(streams, limit)
