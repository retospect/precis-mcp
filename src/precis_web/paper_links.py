"""Off-site lookup links for a paper's external identifier.

Shared by the Papers-Needed queue and the unified ``/items`` list so a
stub (or any paper) carries one-click "go find/get it" links: the
publisher/arXiv page, the University of Limerick Primo discovery search,
and Google Scholar. Input is a ``stub_backlog``-style identifier — a
bare DOI (``10.…``), ``arxiv:<id>``, or ``s2:<hash>``.
"""

from __future__ import annotations

from urllib.parse import quote


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
