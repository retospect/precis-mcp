"""First-class correction records for glyph-damaged passages (gr724751).

``chunk_corrections`` (migration 0358) — one row per corrected span on a
paper. A human-confirmed (or context-inferred) fix to OCR damage used to
survive only as a free-text "OCR CORRECTED BY HUMAN" paper note: chunk
reads met the damaged text with no pointer, and the note anchored to
nothing. This table is the durable form, anchored on the PAPER plus the
QUOTED EXTRACTED SPAN — never on the chunk handle alone (gr690836:
re-extraction mints fresh chunk handles, but a chunk whose text still
contains the span still matches, so the record rides a body rebuild).

The body text itself stays as extracted; the record renders under the
passage on chunk reads and the consumers honour it:

- the pin term check reads the passage with each extracted span replaced
  by its corrected span (:func:`augment_text`, :mod:`precis.nanopub.pin_lint`),
  so a claim quoting the corrected value is covered and one quoting the
  superseded value is not;
- the draft glyph-cite hint names the record instead of only nagging
  (:func:`precis.draft.lint.glyph_cite_hint`);
- the measure write path's ``glyph_suspected`` sanity flag stays quiet
  only when a correction covers the measure's own anchored reading
  (:meth:`precis.store._measures_ops.MeasuresMixin._sanity_flags`).

Matching is whitespace-tolerant on both sides (extraction re-flows text),
via :func:`precis.utils.glyph_health.span_in_text`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from precis.store.core import StoreCore
from precis.utils.glyph_health import span_in_text

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from precis.store.store import Store

#: The three outcomes a correction can record (gr724751 instance 3 added
#: the third: a human read can show the EXTRACTION was right and the
#: printed source is wrong — the record then guards the printed value).
TIERS: tuple[str, ...] = ("human-read", "inferred", "confirmed-misprint")


@dataclass(frozen=True)
class ChunkCorrection:
    """One corrected span on one paper."""

    id: int
    ref_id: int
    chunk_id: int | None
    page: int | None
    extracted_span: str
    corrected_span: str
    tier: str
    reader: str | None
    approved_by: str | None
    source_file: str | None
    note: str | None
    created_by: str
    created_at: datetime

    def matches(self, text: str) -> bool:
        """True when this record's extracted span occurs in ``text``."""
        return span_in_text(self.extracted_span, text)


class CorrectionStore:
    """Composed sub-store for ``chunk_corrections`` — reached as
    ``store.corrections``. Holds the shared :class:`StoreCore`; the
    ``host`` back-reference exists only for parity with the
    chunks/drafts carve pattern."""

    def __init__(self, core: StoreCore, *, host: Store) -> None:
        self._core = core
        self._host = host

    @property
    def pool(self) -> ConnectionPool:
        return self._core.pool

    # -- reads ---------------------------------------------------------------

    def for_ref(self, ref_id: int) -> list[ChunkCorrection]:
        """Every correction on the paper, oldest first."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, ref_id, chunk_id, page, extracted_span,"
                " corrected_span, tier, reader, approved_by, source_file,"
                " note, created_by, created_at"
                " FROM chunk_corrections WHERE ref_id = %s"
                " ORDER BY created_at, id",
                (ref_id,),
            ).fetchall()
        return [self._row_to_correction(r) for r in rows]

    # -- writes --------------------------------------------------------------

    def add(
        self,
        *,
        ref_id: int,
        extracted_span: str,
        corrected_span: str,
        tier: str,
        chunk_id: int | None = None,
        page: int | None = None,
        reader: str | None = None,
        approved_by: str | None = None,
        source_file: str | None = None,
        note: str | None = None,
    ) -> ChunkCorrection:
        """Insert one correction row. ``tier`` is checked against
        :data:`TIERS`; the caller (the paper handler's put door) owns the
        richer validation. ``created_by`` falls back to the session's
        ``precis.actor`` setting (revision context), else ''."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO chunk_corrections"
                " (ref_id, chunk_id, page, extracted_span, corrected_span,"
                "  tier, reader, approved_by, source_file, note, created_by)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                "         coalesce(current_setting('precis.actor', true), ''))"
                " RETURNING id, ref_id, chunk_id, page, extracted_span,"
                " corrected_span, tier, reader, approved_by, source_file,"
                " note, created_by, created_at",
                (
                    ref_id,
                    chunk_id,
                    page,
                    extracted_span,
                    corrected_span,
                    tier,
                    reader,
                    approved_by,
                    source_file,
                    note,
                ),
            ).fetchone()
            conn.commit()
        assert row is not None
        return self._row_to_correction(row)

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _row_to_correction(row: Any) -> ChunkCorrection:
        return ChunkCorrection(
            id=int(row[0]),
            ref_id=int(row[1]),
            chunk_id=int(row[2]) if row[2] is not None else None,
            page=int(row[3]) if row[3] is not None else None,
            extracted_span=str(row[4]),
            corrected_span=str(row[5]),
            tier=str(row[6]),
            reader=row[7],
            approved_by=row[8],
            source_file=row[9],
            note=row[10],
            created_by=str(row[11]),
            created_at=row[12],
        )


def _span_pattern(span: str) -> re.Pattern[str]:
    """Whitespace-tolerant pattern for a quoted span."""
    return re.compile(r"\s+".join(re.escape(t) for t in span.split()))


def augment_text(text: str, corrections: list[ChunkCorrection]) -> str:
    """The corrected projection of ``text`` for the pin term check: each
    matching correction's ``extracted_span`` is REPLACED by its
    ``corrected_span``, so a claim quoting the corrected reading is
    covered and one quoting the known-wrong value (a superseded reading,
    or a misprint) is not. The stored body is never touched."""
    out = text
    for c in corrections:
        if c.matches(out) and c.extracted_span.split():
            literal = c.corrected_span.replace("\\", "\\\\")
            out = _span_pattern(c.extracted_span).sub(literal, out)
    return out
