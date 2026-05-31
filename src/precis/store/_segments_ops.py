"""Persistent segment + sentence reads. Mixin on :class:`precis.store.Store`.

Counterpart to :mod:`precis.workers.segment_toc` (which writes the
``ref_segments`` and ``ref_segment_sentences`` tables). This module
owns the read paths the TOC renderer and search-result composer use
to serve segments and excerpts from the DB instead of recomputing
at request time.

Three primary readers:

* :meth:`list_segments_for_ref` — every segment row for a ref,
  ordered by ``segment_idx``. Used by the TOC renderer.
* :meth:`segment_containing_chunk` — find the one segment whose
  ``[pos_lo, pos_hi]`` covers a given chunk position. Used by the
  search-result navigator (``sub-TOC of the segment containing the
  top hit``).
* :meth:`top_sentences_for_segment` — top-K central sentences for
  a segment, with an optional ``query_embedding`` that triggers
  query-aligned reranking via pgvector cosine.

All readers return plain dataclasses (not psycopg rows) so consumers
don't depend on the cursor's lifetime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool


@dataclass(frozen=True, slots=True)
class SegmentRow:
    """One row of ``ref_segments``, denormalised for callers."""

    segment_id: int
    segment_idx: int
    pos_lo: int
    pos_hi: int
    heading: str | None
    mode: str
    section_class: str | None
    segmentation_version: str
    extractor_version: str
    embedder_name: str
    keywords: list[dict[str, Any]]
    forms: list[str]
    status: str


@dataclass(frozen=True, slots=True)
class SentenceRow:
    """One row of ``ref_segment_sentences`` (without the embedding)."""

    sentence_id: int
    segment_id: int
    sentence_idx: int
    text: str
    chunk_pos: int
    char_offset: int
    centroid_score: float


_SEGMENT_COLS = (
    "segment_id, segment_idx, pos_lo, pos_hi, heading, mode, section_class, "
    "segmentation_version, extractor_version, embedder_name, "
    "keywords, forms, status"
)

_SENTENCE_COLS_NO_EMBEDDING = (
    "sentence_id, segment_id, sentence_idx, text, chunk_pos, char_offset, "
    "centroid_score"
)


def _row_to_segment(row: tuple[Any, ...]) -> SegmentRow:
    return SegmentRow(
        segment_id=int(row[0]),
        segment_idx=int(row[1]),
        pos_lo=int(row[2]),
        pos_hi=int(row[3]),
        heading=row[4],
        mode=str(row[5]),
        section_class=row[6],
        segmentation_version=str(row[7]),
        extractor_version=str(row[8]),
        embedder_name=str(row[9]),
        keywords=list(row[10]) if row[10] is not None else [],
        forms=list(row[11]) if row[11] is not None else [],
        status=str(row[12]),
    )


def _row_to_sentence(row: tuple[Any, ...]) -> SentenceRow:
    return SentenceRow(
        sentence_id=int(row[0]),
        segment_id=int(row[1]),
        sentence_idx=int(row[2]),
        text=str(row[3]),
        chunk_pos=int(row[4]),
        char_offset=int(row[5]),
        centroid_score=float(row[6]),
    )


class SegmentsMixin:
    """Reads against ``ref_segments`` + ``ref_segment_sentences``.

    Mixed into :class:`precis.store.Store`. All methods take and
    release a pool connection — no caller-managed transactions
    required because reads are stateless.
    """

    pool: ConnectionPool  # provided by the concrete Store

    # ------------------------------------------------------------------
    # ref_segments
    # ------------------------------------------------------------------

    def list_segments_for_ref(self, ref_id: int) -> list[SegmentRow]:
        """Every segment for ``ref_id`` ordered by ``segment_idx``.

        Returns ``[]`` when the worker hasn't populated rows yet
        (the renderer treats this as a "compute pending" state).
        """
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT {_SEGMENT_COLS}
                  FROM ref_segments
                 WHERE ref_id = %s
                   AND status = 'ok'
                 ORDER BY segment_idx
                """,
                (ref_id,),
            ).fetchall()
        return [_row_to_segment(r) for r in rows]

    def segment_containing_chunk(
        self,
        ref_id: int,
        chunk_pos: int,
    ) -> SegmentRow | None:
        """Find the segment whose ``[pos_lo, pos_hi]`` covers ``chunk_pos``.

        Returns ``None`` when no segment covers the position (the
        chunk was filtered as boilerplate / references, or the ref
        hasn't been segmented yet). Index-supported via the GiST
        ``ref_segments_range_idx``.
        """
        with self.pool.connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_SEGMENT_COLS}
                  FROM ref_segments
                 WHERE ref_id = %s
                   AND status = 'ok'
                   AND int4range(pos_lo, pos_hi, '[]') @> %s
                 LIMIT 1
                """,
                (ref_id, chunk_pos),
            ).fetchone()
        return _row_to_segment(row) if row is not None else None

    # ------------------------------------------------------------------
    # ref_segment_sentences
    # ------------------------------------------------------------------

    def top_sentences_for_segment(
        self,
        segment_id: int,
        *,
        limit: int = 2,
        query_embedding: list[float] | None = None,
    ) -> list[SentenceRow]:
        """Top sentences for a segment, optionally reranked by query.

        Without ``query_embedding`` → ordered by ``centroid_score``
        descending (segment-prototypical, used for the TOC excerpt
        sub-line). With ``query_embedding`` → ordered by cosine
        similarity to the query (query-aligned, used for search
        result excerpts). Both paths return at most ``limit`` rows.

        pgvector cosine is ``<=>`` (lower is closer). Sentences with
        a NULL embedding (failed compute) are excluded from the
        query-aligned path; the centroid path includes them since
        ``centroid_score`` is populated independently.
        """
        if limit <= 0:
            return []
        with self.pool.connection() as conn:
            if query_embedding is None:
                rows = conn.execute(
                    f"""
                    SELECT {_SENTENCE_COLS_NO_EMBEDDING}
                      FROM ref_segment_sentences
                     WHERE segment_id = %s
                       AND status = 'ok'
                     ORDER BY centroid_score DESC
                     LIMIT %s
                    """,
                    (segment_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT {_SENTENCE_COLS_NO_EMBEDDING}
                      FROM ref_segment_sentences
                     WHERE segment_id = %s
                       AND status = 'ok'
                       AND embedding IS NOT NULL
                     ORDER BY embedding <=> %s
                     LIMIT %s
                    """,
                    (segment_id, query_embedding, limit),
                ).fetchall()
        return [_row_to_sentence(r) for r in rows]

    def count_segments_for_ref(self, ref_id: int) -> int:
        """Lightweight presence check used by the renderer to decide
        between "serve from DB" and "compute on demand" fallbacks."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM ref_segments WHERE ref_id = %s AND status = 'ok'",
                (ref_id,),
            ).fetchone()
        return int(row[0]) if row else 0


__all__ = [
    "SegmentRow",
    "SegmentsMixin",
    "SentenceRow",
]
