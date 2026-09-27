"""Off-site lookup links for a paper's external identifier.

Shared by the Papers-Needed queue and the unified ``/items`` list so a
stub (or any paper) carries one-click "go find/get it" links: the
publisher/arXiv page, the University of Limerick Primo discovery search,
Google Scholar, and a direct LibKey full-text link. Input is a
``stub_backlog``-style identifier — a bare DOI (``10.…``),
``arxiv:<id>``, or ``s2:<hash>``.

The pure URL builders (:func:`doi_url`, :func:`uol_url`,
:func:`libkey_url`, ``_search_token``) live in
:mod:`precis.utils.paper_links` — core, no ``precis_web`` import — so the
LaTeX/docx export pipeline can use them without depending on the web
package. Re-exported here so existing imports keep working.
"""

from __future__ import annotations

from urllib.parse import quote

from precis.utils.paper_links import (
    _search_token,
    doi_url,
    libkey_url,
    uol_url,
)

__all__ = [
    "arxiv_pdf_url",
    "doi_url",
    "libkey_url",
    "scholar_title_url",
    "scholar_url",
    "uol_url",
]


def scholar_url(identifier: str) -> str:
    """Google Scholar search for the identifier."""
    token = _search_token(identifier)
    if not token:
        return ""
    q = quote(token, safe="")
    return f"https://scholar.google.com/scholar?hl=en&as_sdt=0%2C5&q={q}&btnG="


def scholar_title_url(title: str) -> str:
    """Google Scholar search on a bare *title* (a Sources/Cited row link).

    :func:`scholar_url` above only accepts an identifier-shaped token
    (DOI / arXiv id, via ``_search_token``) — an S2 neighbour row
    without either (opaque ``s2:`` hash, or no identifier at all) still
    has a title, which is the only usable Scholar query left. Same query
    shape as :func:`scholar_url`, keyed on the title text instead.
    """
    t = (title or "").strip()
    if not t:
        return ""
    q = quote(t, safe="")
    return f"https://scholar.google.com/scholar?hl=en&as_sdt=0%2C5&q={q}&btnG="


def arxiv_pdf_url(identifier: str) -> str:
    """Direct arXiv PDF for an ``arxiv:`` identifier (else '').

    ``doi_url`` already links the identifier itself to the abstract page
    (``arxiv.org/abs/…``); this is the one-click *download* sibling —
    ``arxiv.org/pdf/<id>`` — matching ``libkey_url`` for DOIs so arXiv
    rows join the "open all downloads" batch. Old-style ids
    (``cond-mat/0410550``) work as a two-segment path.

    Note this is only a manual fallback: the ``fetch_oa`` cascade already
    auto-pulls ``arxiv.org/pdf/<id>.pdf`` — an arXiv stub in the backlog
    is one that auto-fetch tried and couldn't land.
    """
    if not identifier.startswith("arxiv:"):
        return ""
    return f"https://arxiv.org/pdf/{identifier.removeprefix('arxiv:')}"
