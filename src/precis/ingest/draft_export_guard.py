"""Refuse precis's own draft-export PDFs at paper ingest (gr634048).

pa553467 / pa553463: draft-export PDFs that landed in the paper corpus
as papers with author "precis" and empty bibtex, then drew title-fix
todos. A draft export is lualatex output whose byline degrades to a
single ``\\author{precis}`` when the draft has no authors
(:func:`precis.export.latex.build_author_block`'s fallback); hyperref
copies that byline into the PDF Info dict's ``Author`` and lualatex
stamps a TeX-family ``Producer``/``Creator``. No real paper carries an
author named exactly "precis" alongside a TeX producer, so the pair is
a deterministic own-output fingerprint — cheap to read (one fitz open,
no Marker), so the ingest doors can check it before any extraction or
drop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: build_author_block's no-author fallback (``precis/export/latex.py``) —
#: hyperref writes it into the PDF Info dict's ``Author``.
DRAFT_EXPORT_AUTHOR = "precis"


def read_pdf_info(path: Path) -> dict[str, Any]:
    """The PDF Info dict (fitz ``doc.metadata``); ``{}`` when unreadable.

    A file whose metadata can't be read is judged by the other ingest
    gates (unreadable is not evidence of own-output), so the failure
    modes here are deliberately silent.
    """
    try:
        import fitz
    except ImportError:
        return {}
    try:
        with fitz.open(str(path)) as doc:
            return dict(doc.metadata or {})
    except Exception:
        return {}


def draft_export_refusal(info: dict[str, Any]) -> str | None:
    """Why ``info`` marks the PDF as precis's own draft export, else ``None``.

    The fingerprint is conjunctive — both halves required:

    * ``Author`` (whitespace/case-normalised) is exactly ``precis``, the
      export pipeline's byline fallback. A draft with real authors
      doesn't match (its home is still the draft, but its metadata is
      indistinguishable from a self-published preprint's, so the
      title/DOI identity gates remain the right check).
    * ``Producer`` or ``Creator`` names a TeX engine (``LuaTeX-…``,
      ``pdfTeX-…``, ``XeTeX …`` — the draft engine is lualatex via
      latexmk). The author half alone could in theory collide with a
      foreign PDF; the pair cannot.
    """
    author = " ".join(str(info.get("author") or "").split()).casefold()
    if author != DRAFT_EXPORT_AUTHOR:
        return None
    engine = f"{info.get('producer') or ''} {info.get('creator') or ''}".casefold()
    if "tex" not in engine:
        return None
    return (
        "PDF Info Author is 'precis' (the draft-export byline fallback) "
        "with a TeX producer — this is precis's own draft export, not a "
        "citable paper"
    )
