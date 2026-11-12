"""Refuse precis's own draft-export PDFs at paper ingest (gr634048).

pa553467 / pa553463: draft-export PDFs that landed in the paper corpus
as papers with author "precis" and empty bibtex, then drew title-fix
todos. A draft export is lualatex output; the checked-in preamble stamps
``pdfauthor={precis}`` into the PDF Info dict on every export
(``templates/draft/preamble.tex`` — hyperref never copies the byline on
its own) and lualatex stamps a TeX-family ``Producer``/``Creator``. No
real paper carries an author named exactly "precis" alongside a TeX
producer, so the pair is a deterministic own-output fingerprint — cheap
to read (one fitz open, no Marker), so the ingest doors can check it
before any extraction or drop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: The preamble's deliberate ``pdfauthor`` stamp
#: (``templates/draft/preamble.tex``) — every draft export's PDF Info
#: ``Author``, whatever the printed byline says.
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

    * ``Author`` (whitespace/case-normalised) is exactly ``precis`` — the
      preamble's ``pdfauthor`` stamp, present on every draft export
      regardless of the printed byline: the export pipeline, not the
      byline, is the author of the file. A draft with real authors
      stamps ``precis`` too and is refused here like any own output.
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
        "PDF Info Author is 'precis' (the draft-export pdfauthor stamp) "
        "with a TeX producer — this is precis's own draft export, not a "
        "citable paper"
    )
