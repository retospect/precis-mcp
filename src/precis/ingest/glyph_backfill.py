"""Corpus backfill for ``meta['glyph_health']`` (gr228594).

:mod:`precis.ingest.glyph_health` runs at write time, so only papers
ingested after it shipped carry a record. This walks the held PDFs of the
older papers, re-reads their **fonts** through the same
:func:`~precis.ingest.glyph_health.analyze_pdf` (no re-extraction, no OCR)
and stamps ``meta['glyph_health']`` on a ref only when ``suspected`` fires —
the same sparse shape the ingest path writes. Body chunks are never touched.

The text-derived counts (Greek codepoints, "micron" prose) are computed from
the ref's live body chunks, because the backfill has no extractor output.

Dry-run by default. Progress is a ``ref_id`` cursor (clean papers write
nothing, so the DB cannot record "already checked").
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from precis.corpus_layout import resolve_local_pdf
from precis.ingest.glyph_health import analyze_pdf

log = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    scanned: int = 0
    no_pdf: int = 0
    errored: int = 0
    clean: int = 0
    flagged: list[int] = field(default_factory=list)
    written: int = 0
    last_ref_id: int = 0


def _body_text(store: Any, ref_id: int) -> str:
    """Live body text of ``ref_id`` (for the text-derived counts only)."""
    with store.pool.connection() as c:
        rows = c.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return "\n".join(str(r[0] or "") for r in rows)


def backfill_glyph_health(
    store: Any,
    corpus_dirs: tuple[Path, ...],
    *,
    apply: bool = False,
    limit: int = 500,
    after_ref_id: int = 0,
    analyze: Callable[[Path, str], dict[str, Any]] = analyze_pdf,
    body_text: Callable[[Any, int], str] = _body_text,
) -> BackfillResult:
    """Scan up to ``limit`` unrecorded held-PDF papers after ``after_ref_id``."""
    res = BackfillResult(last_ref_id=after_ref_id)
    refs = store.refs_for_glyph_backfill(after_ref_id=after_ref_id, limit=limit)
    for ref in refs:
        res.scanned += 1
        res.last_ref_id = ref.ref_id
        pdf = resolve_local_pdf(corpus_dirs, ref.storage_path, ref.cite_keys)
        if pdf is None:
            res.no_pdf += 1
            continue
        try:
            record = analyze(pdf, body_text(store, ref.ref_id))
        except Exception:
            res.errored += 1
            log.warning("glyph backfill: ref %d failed", ref.ref_id, exc_info=True)
            continue
        if record.get("font_analysis_error"):
            # An unparseable PDF yields only weak text signals; not a verdict.
            res.errored += 1
            continue
        if not record.get("suspected"):
            res.clean += 1
            continue
        res.flagged.append(ref.ref_id)
        if apply:
            store.stamp_ref_meta(ref.ref_id, {"glyph_health": record})
            res.written += 1
    return res


__all__ = ["BackfillResult", "backfill_glyph_health"]
