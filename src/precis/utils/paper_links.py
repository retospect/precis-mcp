"""Off-site lookup link builders for a paper's external identifier.

Pure ``str -> str`` builders shared by ``precis_web.paper_links`` (the
Papers-Needed queue and unified ``/items`` list) and the LaTeX/docx export
pipeline (``precis.export.latex``/``precis.export.docx``), which append a
compact ``doi`` / library link pair next to each inline citation. Input is
a ``stub_backlog``-style identifier — a bare DOI (``10.…``), ``arxiv:<id>``,
or ``s2:<hash>``.

The library-specific bits (the discovery-search URL template, and the
LibKey library id) are per-install configurable
(:class:`precis.config.PrecisConfig` — ``library_search_url`` /
``libkey_library_id``, both defaulting to the University of Limerick); a
caller passing an explicit keyword overrides the config, which is what lets
unit tests exercise both without touching the environment.

Kept dependency-free of ``precis_web`` (core, so export — which runs
outside the web process — can use it directly); it does depend on
``precis.config``, itself dependency-free.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote

from precis.config import load_config


@lru_cache(maxsize=1)
def _library_settings() -> tuple[str, str, str]:
    """``(library_label, library_search_url, libkey_library_id)`` from
    :func:`precis.config.load_config`, memoized — ``load_config()`` builds a
    fresh :class:`~precis.config.PrecisConfig` (re-parsing env + ``.env``)
    on every call, and the no-override path here (every ``precis_web`` call
    site) would otherwise pay that per request. Config is loaded once at
    process start and never changes underneath a running server, so caching
    it for the process lifetime is safe. An explicit override (either
    builder's keyword arg, or an exporter passing its own once-per-export
    ``load_config()`` read) bypasses this cache entirely.
    """
    cfg = load_config()
    return cfg.library_label, cfg.library_search_url, cfg.libkey_library_id


def doi_url(identifier: str) -> str:
    """Publisher / arXiv URL for a DOI or ``arxiv:`` identifier (else '').

    The id is percent-encoded (``/ : ( )`` left bare so an ordinary DOI
    like ``10.1016/j.cattod.2020.01.001`` or a legacy Wiley one with
    parenthesised segments stays readable; everything else reserved —
    ``< > ; # %``, spaces, ``[ ]`` — is escaped so a legacy DOI's ``<...>``
    span doesn't get interpreted as URL syntax by a downstream consumer).
    """
    if not identifier:
        return ""
    if identifier.startswith("arxiv:"):
        arxiv_id = quote(identifier.removeprefix("arxiv:"), safe="/:()")
        return f"https://arxiv.org/abs/{arxiv_id}"
    if identifier.startswith("10."):
        return f"https://doi.org/{quote(identifier, safe='/:()')}"
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


def library_url(identifier: str, *, search_url_template: str | None = None) -> str:
    """This install's library discovery search (e.g. Primo) for the
    identifier — the canonical name; ``uol_url`` is kept as an alias since
    that's what existing callers (``precis_web.paper_links`` and both
    exporters) already import.

    ``search_url_template`` defaults to
    :attr:`precis.config.PrecisConfig.library_search_url` (University of
    Limerick's Primo search) when omitted. It must contain the literal
    ``{query}`` placeholder — the search term, percent-encoded (``/`` →
    ``%2F``) — is substituted in at build time.
    """
    token = _search_token(identifier)
    if not token:
        return ""
    if search_url_template is None:
        search_url_template = _library_settings()[1]
    q = quote(token, safe="")
    # ``.replace`` (not ``str.format``) — an operator-supplied template may
    # legitimately contain other literal braces (e.g. a second query-string
    # placeholder the operator doesn't want filled), which ``.format`` would
    # raise a ``KeyError``/``IndexError`` on instead of leaving alone.
    return search_url_template.replace("{query}", q)


#: Alias — the name every existing caller (``precis_web.paper_links``, both
#: exporters) already imports.
uol_url = library_url


def libkey_url(identifier: str, *, library_id: str | None = None) -> str:
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

    ``library_id`` defaults to
    :attr:`precis.config.PrecisConfig.libkey_library_id` (University of
    Limerick's id) when omitted — the ``libraries/<id>`` segment that
    overrides any browser-side affiliation so the link resolves via this
    install's entitlements.
    """
    if not identifier.startswith("10."):
        return ""
    if library_id is None:
        library_id = _library_settings()[2]
    doi = quote(identifier, safe="/")
    return f"https://libkey.io/libraries/{library_id}/{doi}"
