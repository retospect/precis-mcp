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

from precis.ingest.pdf_sidecar import related_citation_spans

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


def _evidence_text(text: str) -> str:
    """``text`` minus its related-article citation spans.

    A companion journal's front page (MethodsX, Data in Brief) typesets
    a *related* paper's title and DOI inside a "Related research
    article" box — gr513878's wrong-PDF incident, where the citation
    carried the stub's own DOI and restated its title verbatim. None of
    that is this PDF's identity evidence, so neither the DOI substring
    check nor the title-word overlap may see it.
    """
    pieces, last = [], 0
    for start, end in related_citation_spans(text):
        pieces.append(text[last:start])
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def identity_verdict(
    texts: list[str], *, title: str, doi: str | None
) -> tuple[str, str | None]:
    """``(identity, mismatch_detail)``.

    ``identity`` is ``"doi"`` (stub DOI in the leading pages, outside a
    related-article citation), ``"title"`` (>= :data:`TITLE_OVERLAP` of
    its significant title words occur — likewise outside such a
    citation), ``"unchecked"`` (nothing to compare) or ``"mismatch"``
    with a detail string.
    """
    text = "\n".join(texts)
    evidence = _evidence_text(text)
    first = texts[0] if texts else ""
    sig = sig_words(title)
    if doi and re.sub(r"\s+", "", doi.lower()) in re.sub(r"\s+", "", evidence.lower()):
        return "doi", None
    if len(sig) >= 2:
        have = set(words(evidence))
        overlap = sum(w in have for w in sig) / len(sig)
        if overlap < TITLE_OVERLAP:
            return "mismatch", (
                f"title overlap {overlap:.2f} < {TITLE_OVERLAP}; "
                f"page 1: {first_line(first)!r}"
            )
        return "title", None
    if doi:
        cited_only = re.sub(r"\s+", "", doi.lower()) in re.sub(r"\s+", "", text.lower())
        where = (
            "only inside a related-article citation"
            if cited_only
            else "not in first pages"
        )
        return "mismatch", f"DOI {doi} {where}; page 1: {first_line(first)!r}"
    return "unchecked", None
