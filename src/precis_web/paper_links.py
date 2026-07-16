"""Off-site lookup links for a paper's external identifier.

Shared by the Papers-Needed queue and the unified ``/items`` list so a
stub (or any paper) carries one-click "go find/get it" links: the
publisher/arXiv page, the University of Limerick Primo discovery search,
Google Scholar, and a direct LibKey full-text link. Input is a
``stub_backlog``-style identifier — a bare DOI (``10.…``),
``arxiv:<id>``, or ``s2:<hash>``.
"""

from __future__ import annotations

from urllib.parse import quote

#: Our LibKey (Third Iron) library id — University of Liverpool, the
#: institution behind the ``uol.primo`` proxy. Found at
#: ``libkey.io/choose-library``; it is the ``libraries/<id>`` segment in
#: the captured full-text-file link. Injecting it overrides any
#: browser-side affiliation so the link resolves via UoL's entitlements.
_LIBKEY_LIBRARY_ID = "2545"


def doi_url(identifier: str) -> str:
    """Publisher / arXiv URL for a DOI or ``arxiv:`` identifier (else '')."""
    if not identifier:
        return ""
    if identifier.startswith("arxiv:"):
        return f"https://arxiv.org/abs/{identifier.removeprefix('arxiv:')}"
    if identifier.startswith("10."):
        return f"https://doi.org/{identifier}"
    return ""


def _search_token(identifier: str) -> str:
    """Bare term to feed a library / scholar search box.

    DOIs and arXiv numbers search cleanly; an opaque S2 hash does not, so
    it returns ``""`` (the UoL / Scholar links are then suppressed). The
    ``arxiv:`` prefix is stripped so the bare number is searched.
    """
    if not identifier:
        return ""
    if identifier.startswith("arxiv:"):
        return identifier.removeprefix("arxiv:")
    if identifier.startswith("10."):
        return identifier
    return ""


def uol_url(identifier: str) -> str:
    """University of Limerick Primo discovery search for the identifier.

    The tenant/view (``vid``), the institution-plus-central-index scope,
    and the ``any,contains,<term>`` query are the load-bearing parts; the
    term is percent-encoded (``/`` → ``%2F``).
    """
    token = _search_token(identifier)
    if not token:
        return ""
    q = quote(token, safe="")
    return (
        "https://uol.primo.exlibrisgroup.com/discovery/search"
        "?vid=353UOL_INST:353UOL_VU1&search_scope=MyInst_and_CI"
        f"&lang=en&sortby=rank&tab=TAB1&query=any,contains,{q}"
    )


def scholar_url(identifier: str) -> str:
    """Google Scholar search for the identifier."""
    token = _search_token(identifier)
    if not token:
        return ""
    q = quote(token, safe="")
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


def libkey_url(identifier: str) -> str:
    """Direct LibKey full-text link for a DOI (else '').

    LibKey's documented library-specific form is
    ``libkey.io/libraries/<id>/<DOI-or-PMID>`` — appending a raw DOI (or
    PMID) resolves straight to the article's full-text-file / speedbump,
    skipping the Primo keyword search. Only DOIs qualify: arXiv preprints
    have their own free PDF (``doi_url``) and an opaque S2 hash is not a
    LibKey key, so both return ``""``.

    The DOI's own ``/`` stays a path separator (multi-segment DOIs are
    normal); every other reserved char (``<>();:`` in legacy Wiley DOIs)
    is percent-encoded so the URL is well-formed, and LibKey decodes it
    back to the DOI.
    """
    if not identifier.startswith("10."):
        return ""
    doi = quote(identifier, safe="/")
    return f"https://libkey.io/libraries/{_LIBKEY_LIBRARY_ID}/{doi}"
