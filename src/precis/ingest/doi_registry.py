"""DOI registration-agency lookup — the acquire fallback past S2.

Semantic Scholar's index lags fresh records and skips registries it
doesn't crawl (DataCite-minted proceedings like LIPIcs), so an S2 miss
is not evidence a DOI is wrong (gr486090). This resolves the DOI
against the registration agencies directly — Crossref first (the
dominant RA for journal DOIs), then DataCite — before
``PaperHandler.acquire`` refuses it as hallucinated.

The DOI is agent-supplied and rides the URL path, so every fetch goes
through :func:`precis.utils.safe_fetch.safe_get` on a pinning
:func:`precis.utils.http.http_client` (the SSRF guard); both hosts are
fixed.

Miss vs. fault follows the acquire split (gr483930): a registry
*answering* "no such DOI" (404) is a miss; a transport failure or a
non-404 status is a fault. :func:`resolve_registry_doi` returns
``None`` only when every leg answered miss — when a leg faulted and
none hit it raises :class:`RegistryLookupError`, so the caller mints
an unverified stub instead of rejecting a real DOI over a registry
outage.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urlsplit

from precis.utils.http import (
    VendorError,
    http_client,
    polite_user_agent,
    with_backoff,
)
from precis.utils.rate_limit import RateLimited
from precis.utils.safe_fetch import safe_get

log = logging.getLogger(__name__)

_TIMEOUT_S = 15.0
_CROSSREF_WORK = "https://api.crossref.org/works/"
_DATACITE_DOI = "https://api.datacite.org/dois/"


class RegistryLookupError(RuntimeError):
    """A registry leg failed to *answer* at all — transport fault or a
    non-404 status. Distinct from a 404 miss (the registry answered "no
    such DOI") so the caller can mint unverified instead of rejecting a
    real identifier over a registry outage — the same split
    :class:`~precis.ingest.semantic_scholar.S2ResolverError` draws for
    the S2 leg (gr483930, gr486090).
    """


def _get_json(url: str, *, service: str, params: dict[str, Any] | None = None) -> Any:
    """GET ``url`` through the SSRF-pinned client under the politeness core
    (``service`` names the limiter row and backoff policy).

    Returns the decoded JSON body on 200, ``None`` on 404 (the registry
    answered "no such DOI"). Any other status and every transport
    failure raise :class:`RegistryLookupError` — the caller counts that
    as a fault, never a miss. That includes a 429/5xx that outlasted the
    retries and a limiter refusal.
    """
    host = urlsplit(url).netloc
    try:
        with http_client(
            timeout=_TIMEOUT_S,
            user_agent=polite_user_agent((params or {}).get("mailto")),
        ) as client:
            resp = with_backoff(
                lambda: safe_get(client, url, params=params or {}), service=service
            )
    except RateLimited as exc:
        # A refusal is "lookup failed", never ``None`` (= registry said 404).
        raise RegistryLookupError(str(exc)) from exc
    except VendorError as exc:
        raise RegistryLookupError(f"{host}: {exc}") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise RegistryLookupError(f"{host} answered HTTP {resp.status_code}")
    return resp.json()


def _crossref_meta(doi: str, mailto: str = "") -> dict[str, Any] | None:
    """``GET api.crossref.org/works/<doi>`` → normalized meta, or ``None``."""
    # Lazy import: ``ingest.crossref`` needs habanero, a ``[paper]``
    # extra — the same gate the S2 resolver honours.
    from precis.ingest.crossref import _normalize as crossref_normalize

    params = {"mailto": mailto} if mailto else None
    data = _get_json(
        _CROSSREF_WORK + quote(doi, safe=""), service="crossref", params=params
    )
    if not isinstance(data, dict):
        return None
    message = data.get("message")
    if not isinstance(message, dict):
        return None
    return crossref_normalize(message, doi)


def _datacite_meta(doi: str) -> dict[str, Any] | None:
    """``GET api.datacite.org/dois/<doi>`` → normalized meta, or ``None``."""
    data = _get_json(_DATACITE_DOI + quote(doi, safe=""), service="datacite")
    if not isinstance(data, dict):
        return None
    inner = data.get("data")
    attrs = inner.get("attributes") if isinstance(inner, dict) else None
    if not isinstance(attrs, dict):
        return None
    return _normalize_datacite(attrs, doi)


#: DataCite ``resourceTypeGeneral`` values that are genuinely a
#: text/journal article — only these earn ``entry_type='article'``
#: (gr514384: the old code hard-coded ``article`` for every DataCite hit,
#: so a Zenodo software release exported ``@article`` with
#: ``journaltitle={Zenodo}``).
_DATACITE_ARTICLE_TYPES = frozenset({"JournalArticle", "Text"})

#: ``resourceTypeGeneral`` → ``meta.record_type`` — the two whole-record
#: cite types (Software → ``software``, Dataset → ``dataset``); every
#: other resource kind stays untyped.
_DATACITE_RECORD_TYPES = {"Software": "software", "Dataset": "dataset"}

#: ``resourceTypeGeneral`` → ``meta.entry_type`` for the record types —
#: kept populated for old readers (:data:`precis.handlers._paper_format.
#: _EXPORT_TYPE_MAP` knows both).
_DATACITE_ENTRY_TYPES = {
    **{t: "article" for t in _DATACITE_ARTICLE_TYPES},
    "Software": "software",
    "Dataset": "dataset",
}


def _normalize_datacite(attrs: dict[str, Any], doi: str) -> dict[str, Any]:
    """DataCite JSON:API ``attributes`` → the crossref/S2-normalized shape.

    ``creators`` entries come as ``{name, givenName, familyName,
    nameType}``; a person with a given/family split emits the canonical
    structured shape, anything else (consortium, unsplit ``"Family,
    Given"``) the ``{"name": ...}`` form — same contract as
    ``crossref._normalize``. ``container.title`` is the venue (a
    journal or proceedings series) and ``publisher`` stays its own
    field — a record with no container does not get its publisher
    re-labelled as a journal.
    """
    title = ""
    for t in attrs.get("titles") or []:
        if isinstance(t, dict) and str(t.get("title") or "").strip():
            title = str(t["title"]).strip()
            break

    authors: list[dict[str, str]] = []
    for c in attrs.get("creators") or []:
        if not isinstance(c, dict):
            continue
        given = str(c.get("givenName") or "").strip()
        family = str(c.get("familyName") or "").strip()
        if given or family:
            entry: dict[str, str] = {}
            if given:
                entry["given"] = given
            if family:
                entry["family"] = family
            authors.append(entry)
            continue
        name = str(c.get("name") or "").strip()
        if name:
            authors.append({"name": name})

    year: int | None = None
    raw_year = attrs.get("publicationYear")
    if raw_year is not None:
        try:
            year = int(str(raw_year))
        except (TypeError, ValueError):
            year = None

    journal = ""
    container = attrs.get("container")
    if isinstance(container, dict):
        journal = str(container.get("title") or "").strip()

    types = attrs.get("types")
    rtg = (
        str(types.get("resourceTypeGeneral") or "").strip()
        if isinstance(types, dict)
        else ""
    )
    record_type = _DATACITE_RECORD_TYPES.get(rtg, "")

    abstract = ""
    for d in attrs.get("descriptions") or []:
        if isinstance(d, dict) and d.get("descriptionType") == "Abstract":
            abstract = str(d.get("description") or "").strip()
            break

    out: dict[str, Any] = {
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "journal": journal,
        "abstract": abstract,
        "source": "datacite",
    }
    if rtg in _DATACITE_ENTRY_TYPES:
        out["entry_type"] = _DATACITE_ENTRY_TYPES[rtg]
    publisher = str(attrs.get("publisher") or "").strip()
    if publisher:
        out["publisher"] = publisher
    version = str(attrs.get("version") or "").strip()
    if version:
        out["version"] = version
    if record_type:
        out["record_type"] = record_type
    return out


def resolve_registry_doi(doi: str, *, mailto: str = "") -> dict[str, Any] | None:
    """Resolve ``doi`` against the DOI registries: Crossref, then DataCite.

    Returns the normalized metadata dict (``title`` / ``authors`` /
    ``year`` / ``doi`` / ``journal`` / ``abstract`` / ``entry_type`` /
    ``source``) for the first registry that knows the DOI — a Crossref
    hit never reaches the DataCite leg. Returns ``None`` only when
    every registry answered not-found. Raises
    :class:`RegistryLookupError` when no leg hit and at least one
    faulted — a fault is not evidence the identifier is wrong.
    """
    fault: Exception | None = None
    try:
        meta = _crossref_meta(doi, mailto=mailto)
        if meta is not None:
            return meta
    except Exception as exc:
        fault = exc
    try:
        meta = _datacite_meta(doi)
        if meta is not None:
            return meta
    except Exception as exc:
        fault = fault or exc
    if fault is not None:
        raise RegistryLookupError(
            f"DOI registry lookup for {doi!r} failed: {type(fault).__name__}: {fault}"
        ) from fault
    return None


__all__ = ["RegistryLookupError", "resolve_registry_doi"]
