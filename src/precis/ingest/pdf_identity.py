"""Does a PDF's text look like the paper a stub describes?

The identity half of the pdf_url door's gate
(:func:`precis.workers.job_types.paper_pull.check_pdf`), kept below the
workers layer so ingest can run the same check before a ``pa<id>``-named
file folds into a stub (gr479094: a paper that merely cited the stub's paper
was folded into it). Cheap: reads the first pages with PyMuPDF, no Marker.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Pages of text the identity check reads.
IDENTITY_PAGES = 3
#: Fraction of the stub title's significant words that must occur.
TITLE_OVERLAP = 0.8
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "via",
        "using",
        "use",
        "its",
        "their",
        "this",
        "that",
    ]
)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def sig_words(title: str) -> list[str]:
    return [w for w in words(title) if len(w) > 2 and w not in _STOPWORDS]


def first_line(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 8), "")
    return line[:120]


def read_leading_pages(path: Path) -> tuple[int, list[str]]:
    """``(page_count, text of the first IDENTITY_PAGES pages)``; fitz errors propagate."""
    import fitz  # PyMuPDF

    with fitz.open(str(path)) as doc:
        n_pages = doc.page_count
        return n_pages, [doc[i].get_text() for i in range(min(IDENTITY_PAGES, n_pages))]


def identity_verdict(
    texts: list[str], *, title: str, doi: str | None
) -> tuple[str, str | None]:
    """``(identity, mismatch_detail)``.

    ``identity`` is ``"doi"`` (stub DOI in the leading pages), ``"title"``
    (>= :data:`TITLE_OVERLAP` of its significant title words occur),
    ``"unchecked"`` (nothing to compare) or ``"mismatch"`` with a detail
    string.
    """
    text = "\n".join(texts)
    first = texts[0] if texts else ""
    sig = sig_words(title)
    if doi and re.sub(r"\s+", "", doi.lower()) in re.sub(r"\s+", "", text.lower()):
        return "doi", None
    if len(sig) >= 2:
        have = set(words(text))
        overlap = sum(w in have for w in sig) / len(sig)
        if overlap < TITLE_OVERLAP:
            return "mismatch", (
                f"title overlap {overlap:.2f} < {TITLE_OVERLAP}; "
                f"page 1: {first_line(first)!r}"
            )
        return "title", None
    if doi:
        return (
            "mismatch",
            f"DOI {doi} not in first pages; page 1: {first_line(first)!r}",
        )
    return "unchecked", None
