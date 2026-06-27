"""ORCID Public API client (ADR 0039).

Resolves a researcher iD to a structured record (names, biography,
keywords, employments with ROR/GRID ids) plus their complete works list
(each carrying DOI / arXiv / PMID external-ids). This is the data source
behind ``kind='orcid'``.

Auth: the Public API is **not** open — it needs a read-public bearer
token obtained via the **client-credentials** flow
(``ORCID_CLIENT_ID`` + ``ORCID_CLIENT_SECRET`` → one long-lived
bearer). The token is cached process-wide keyed on the client id and
re-minted on expiry. Missing creds raise :class:`Upstream` at call time;
the handler degrades the kind to disabled at boot (``InitError``) so a
missing secret never blocks the rest of the surface.

DB-free: this module only talks to the ORCID HTTP API and normalises the
JSON. The handler owns all store writes.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from precis.errors import BadInput, NotFound, Upstream
from precis.utils.optional_deps import require_optional

# Public API + OAuth token endpoints. The *public* token endpoint is on
# the main orcid.org host; the data lives on pub.orcid.org/v3.0.
_TOKEN_URL = "https://orcid.org/oauth/token"
_API_BASE = "https://pub.orcid.org/v3.0"

_TIMEOUT_S = 30.0
_USER_AGENT = "precis-mcp/1.0 (+https://orcid.org)"

# An ORCID iD is 16 digits in 4 groups of 4, last char may be 'X'
# (ISO 7064 checksum). Accept bare, dashed, ``orcid:``-prefixed, and
# full-URL forms.
_ID_RE = re.compile(r"(\d{4})-?(\d{4})-?(\d{4})-?(\d{3}[0-9X])", re.IGNORECASE)


# ---------------------------------------------------------------------------
# iD normalisation
# ---------------------------------------------------------------------------


def normalize_orcid_id(raw: str) -> str:
    """Coerce any accepted iD form to the canonical dashed 16-char iD.

    Accepts ``0000-0002-1825-0097``, ``0000000218250097``,
    ``orcid:0000-...``, ``https://orcid.org/0000-...``. Raises
    :class:`BadInput` when no well-formed iD is present (cheap feedback
    before any network call). Validates the ISO 7064 MOD 11-2 checksum.
    """
    s = (raw or "").strip()
    if not s:
        raise BadInput(
            "orcid requires an iD",
            next="get(kind='orcid', id='0000-0002-1825-0097')",
        )
    # Drop a leading ``orcid:`` handle prefix or any URL scheme/host.
    low = s.lower()
    if low.startswith("orcid:"):
        s = s[len("orcid:") :].strip()
    m = _ID_RE.search(s)
    if m is None:
        raise BadInput(
            f"not a valid ORCID iD: {raw!r}",
            next="get(kind='orcid', id='0000-0002-1825-0097')",
        )
    canonical = "-".join(m.groups()).upper()
    if not _checksum_ok(canonical):
        raise BadInput(
            f"ORCID iD {canonical} fails its checksum digit — likely mistyped",
            next="verify the iD on https://orcid.org",
        )
    return canonical


def _checksum_ok(dashed: str) -> bool:
    """ISO 7064 MOD 11-2 check over the first 15 digits → last char."""
    digits = dashed.replace("-", "")
    total = 0
    for ch in digits[:-1]:
        total = (total + int(ch)) * 2
    remainder = total % 11
    result = (12 - remainder) % 11
    expected = "X" if result == 10 else str(result)
    return digits[-1].upper() == expected


def slug_for(orcid_id: str) -> str:
    """The ADR 0036 handle slug for an author node: ``orcid:<iD>``."""
    return f"orcid:{orcid_id}"


# ---------------------------------------------------------------------------
# Token cache (client-credentials)
# ---------------------------------------------------------------------------

_token_lock = threading.Lock()
#: client_id → (access_token, expires_at_epoch). Process-wide; refreshed
#: on expiry with a 60s safety margin.
_token_cache: dict[str, tuple[str, float]] = {}


def _credentials() -> tuple[str, str]:
    cid = (os.environ.get("ORCID_CLIENT_ID") or "").strip()
    secret = (os.environ.get("ORCID_CLIENT_SECRET") or "").strip()
    if not cid or not secret:
        raise Upstream(
            "ORCID client credentials are not configured",
            next="set ORCID_CLIENT_ID and ORCID_CLIENT_SECRET",
        )
    return cid, secret


def has_credentials() -> bool:
    """True iff both client-credential env vars are set (boot gate)."""
    return bool(
        (os.environ.get("ORCID_CLIENT_ID") or "").strip()
        and (os.environ.get("ORCID_CLIENT_SECRET") or "").strip()
    )


def _get_token() -> str:
    """Return a cached read-public bearer, minting a fresh one on expiry."""
    cid, secret = _credentials()
    now = time.time()
    with _token_lock:
        cached = _token_cache.get(cid)
        if cached is not None and cached[1] > now + 60:
            return cached[0]
    httpx = require_optional("httpx", extra="external")
    try:
        with httpx.Client(timeout=_TIMEOUT_S) as client:
            resp = client.post(
                _TOKEN_URL,
                data={
                    "client_id": cid,
                    "client_secret": secret,
                    "grant_type": "client_credentials",
                    "scope": "/read-public",
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise Upstream(f"ORCID token transport error: {exc}") from exc
    if resp.status_code != 200:
        raise Upstream(
            f"ORCID token request failed (HTTP {resp.status_code}): {resp.text[:200]}",
            next="check ORCID_CLIENT_ID / ORCID_CLIENT_SECRET",
        )
    try:
        payload = resp.json()
    except Exception as exc:
        raise Upstream(f"ORCID token endpoint returned non-JSON: {exc}") from exc
    token = payload.get("access_token")
    if not token:
        raise Upstream("ORCID token endpoint returned no access_token")
    expires_in = float(payload.get("expires_in", 3600))
    with _token_lock:
        _token_cache[cid] = (str(token), now + expires_in)
    return str(token)


def _api_get(path: str) -> dict[str, Any]:
    """GET ``{_API_BASE}/{path}`` with a bearer token; return parsed JSON."""
    httpx = require_optional("httpx", extra="external")
    token = _get_token()
    url = f"{_API_BASE}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT_S) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise Upstream(f"ORCID transport error: {exc}") from exc
    if resp.status_code == 404:
        raise NotFound(
            f"ORCID has no record at {path}",
            next="verify the iD on https://orcid.org",
        )
    if resp.status_code == 401:
        # Token may have been revoked mid-flight — drop the cache so the
        # next call re-mints, then surface the failure.
        _token_cache.clear()
        raise Upstream("ORCID rejected the bearer token (HTTP 401)")
    if resp.status_code != 200:
        raise Upstream(f"ORCID HTTP {resp.status_code} for {path}: {resp.text[:200]}")
    try:
        return resp.json()
    except Exception as exc:
        raise Upstream(f"ORCID returned non-JSON for {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _val(node: Any, *keys: str) -> Any:
    """Walk a nested dict by keys, tolerating missing/None levels."""
    cur = node
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _normalize_person(person: dict[str, Any]) -> dict[str, Any]:
    given = _val(person, "name", "given-names", "value") or ""
    family = _val(person, "name", "family-name", "value") or ""
    credit = _val(person, "name", "credit-name", "value") or ""
    full = credit or " ".join(p for p in (given, family) if p)

    keywords = [
        kw.get("content")
        for kw in (_val(person, "keywords", "keyword") or [])
        if isinstance(kw, dict) and kw.get("content")
    ]
    urls = [
        {
            "name": _val(u, "url-name") or "",
            "url": _val(u, "url", "value") or "",
        }
        for u in (_val(person, "researcher-urls", "researcher-url") or [])
        if isinstance(u, dict)
    ]
    countries = [
        _val(addr, "country", "value")
        for addr in (_val(person, "addresses", "address") or [])
        if _val(addr, "country", "value")
    ]
    return {
        "name": full,
        "given": given,
        "family": family,
        "credit_name": credit,
        "biography": _val(person, "biography", "content") or "",
        "keywords": keywords,
        "researcher_urls": urls,
        "country": countries[0] if countries else "",
    }


def _normalize_employments(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract employments (organization + disambiguated ROR/GRID id)."""
    groups = _val(record, "affiliation-group") or []
    out: list[dict[str, Any]] = []
    for grp in groups:
        for summary_wrap in grp.get("summaries", []) if isinstance(grp, dict) else []:
            emp = (
                summary_wrap.get("employment-summary")
                if isinstance(summary_wrap, dict)
                else None
            )
            if not isinstance(emp, dict):
                continue
            org = emp.get("organization") or {}
            disamb = org.get("disambiguated-organization") or {}
            out.append(
                {
                    "organization": org.get("name") or "",
                    "ror": disamb.get("disambiguated-organization-identifier") or "",
                    "ror_source": disamb.get("disambiguation-source") or "",
                    "role": emp.get("role-title") or "",
                    "start_year": _val(emp, "start-date", "year", "value") or "",
                    "end_year": _val(emp, "end-date", "year", "value") or "",
                }
            )
    return out


def _normalize_work(summary: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one work-summary → {title, year, doi, arxiv, pmid, url}."""
    title = _val(summary, "title", "title", "value") or ""
    year = _val(summary, "publication-date", "year", "value") or None
    work: dict[str, Any] = {
        "title": title,
        "year": int(year) if year and str(year).isdigit() else None,
        "doi": None,
        "arxiv": None,
        "pmid": None,
        "url": _val(summary, "url", "value") or "",
    }
    for ext in _val(summary, "external-ids", "external-id") or []:
        if not isinstance(ext, dict):
            continue
        id_type = (ext.get("external-id-type") or "").lower()
        id_value = (ext.get("external-id-value") or "").strip()
        if not id_value:
            continue
        if id_type == "doi" and work["doi"] is None:
            work["doi"] = id_value.lower()
        elif id_type == "arxiv" and work["arxiv"] is None:
            work["arxiv"] = id_value
        elif id_type == "pmid" and work["pmid"] is None:
            work["pmid"] = id_value
    return work


def _normalize_works(works: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for grp in _val(works, "group") or []:
        summaries = grp.get("work-summary") if isinstance(grp, dict) else None
        if not summaries:
            continue
        # One group = one logical work (possibly several summaries from
        # different sources). The first summary carries the canonical
        # title/date; external-ids merge across the group.
        merged = _normalize_work(summaries[0]) or {}
        for extra in summaries[1:]:
            other = _normalize_work(extra) or {}
            for key in ("doi", "arxiv", "pmid"):
                if not merged.get(key) and other.get(key):
                    merged[key] = other[key]
            if not merged.get("url") and other.get("url"):
                merged["url"] = other["url"]
        out.append(merged)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def fetch_record(orcid_id: str) -> dict[str, Any]:
    """Resolve a (canonical) iD to a full normalised record.

    Pulls ``/person``, ``/employments``, and ``/works`` and folds them
    into one dict. ``orcid_id`` must already be canonical (run
    :func:`normalize_orcid_id` first). Raises :class:`NotFound` for an
    unknown iD, :class:`Upstream` for transport / auth failures.
    """
    person = _api_get(f"{orcid_id}/person")
    record = {"orcid_id": orcid_id}
    record.update(_normalize_person(person))

    try:
        employments = _api_get(f"{orcid_id}/employments")
        record["employments"] = _normalize_employments(employments)
    except Upstream:
        # Employments are enrichment, not load-bearing — a partial
        # outage shouldn't sink the whole resolve.
        record["employments"] = []

    works = _api_get(f"{orcid_id}/works")
    normalized_works = _normalize_works(works)
    record["works"] = normalized_works
    record["work_count"] = len(normalized_works)
    return record


def fetch_works_only(orcid_id: str) -> list[dict[str, Any]]:
    """Pull just ``/works`` — the cheap path for the re-enqueue refresh."""
    return _normalize_works(_api_get(f"{orcid_id}/works"))


__all__ = [
    "fetch_record",
    "fetch_works_only",
    "has_credentials",
    "normalize_orcid_id",
    "slug_for",
]
