"""Classic citation string -> DOI candidates via Crossref ``query.bibliographic``.

``Phys. Rev. B 47, 558 (1993)`` -> ranked candidates, each with a
``confidence`` (share of the string's year/volume/page/journal the Crossref
record matches). Only one candidate at full confidence may be auto-stubbed
(:func:`pick_unambiguous`); anything weaker is returned to the caller.

Outbound fetch goes through ``safe_get`` only (SSRF guard, see
``utils/safe_fetch.py``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from precis.utils.http import retry_transient

_CROSSREF_WORKS_URL = "https://api.crossref.org/works"
_TIMEOUT_S = 15.0
_ROWS = 5
# A candidate at or above this confidence, and alone at it, may be auto-stubbed.
ACCEPT_CONFIDENCE = 0.99

_YEAR_RE = re.compile(r"\b(1[6-9]\d\d|20\d\d)\b")
_VOL_PAGE_RE = re.compile(
    r"\b(\d{1,4})\s*[,:;]?\s+([A-Za-z]?\d{1,7})(?:\s*[-–]\s*\d+)?\b"
)
_WORD_RE = re.compile(r"[a-z]{2,}")

Fetch = Callable[[str, str], list[dict[str, Any]]]


def parse_citation(citation: str) -> dict[str, str]:
    """Pull ``year`` / ``volume`` / ``page`` / ``journal`` out of a citation
    string. Keys are absent when not found; never raises."""
    out: dict[str, str] = {}
    text = citation.strip()
    years = _YEAR_RE.findall(text)
    if years:
        out["year"] = years[-1]
        text = text.replace(years[-1], " ", 1)
    m = _VOL_PAGE_RE.search(text)
    if m:
        out["volume"], out["page"] = m.group(1), m.group(2)
        text = text[: m.start()]
    out["journal"] = re.sub(r"[()\[\]]", " ", text).strip(" ,;.")
    return out


def _issued_year(item: dict[str, Any]) -> int | None:
    parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
    return parts[0] if parts else None


def _confidence(parsed: dict[str, str], item: dict[str, Any]) -> float:
    """Fraction of parsed citation features the Crossref item matches; 0.0 if
    nothing was parsed."""
    checks: list[bool] = []
    if "year" in parsed:
        checks.append(str(_issued_year(item)) == parsed["year"])
    if "volume" in parsed:
        checks.append(str(item.get("volume", "")) == parsed["volume"])
    if "page" in parsed:
        first = str(item.get("page", "")).split("-")[0].strip().lower()
        checks.append(first == parsed["page"].lower())
    jwords = set(_WORD_RE.findall(parsed.get("journal", "").lower()))
    if jwords:
        titles = " ".join(
            (item.get("container-title") or [])
            + (item.get("short-container-title") or [])
        )
        cwords = _WORD_RE.findall(titles.lower())
        # Abbreviations: "Phys. Rev." must prefix-match "Physical Review".
        checks.append(all(any(c.startswith(j) for c in cwords) for j in jwords))
    return sum(checks) / len(checks) if checks else 0.0


def _fetch_items(citation: str, mailto: str) -> list[dict[str, Any]]:
    """``query.bibliographic`` lookup via ``safe_get``; ``[]`` on any failure."""
    from precis.utils.http import http_client
    from precis.utils.safe_fetch import safe_get

    params: dict[str, Any] = {
        "query.bibliographic": citation,
        "rows": _ROWS,
        "select": "DOI,title,author,issued,container-title,"
        "short-container-title,volume,page,score",
    }
    if mailto:
        params["mailto"] = mailto
    try:
        with http_client(timeout=_TIMEOUT_S) as client:
            resp = retry_transient(
                lambda: safe_get(client, _CROSSREF_WORKS_URL, params=params),
                host="api.crossref.org",
            )
        if resp.status_code != 200:
            return []
        return list(resp.json()["message"]["items"])
    except Exception:
        return []


def resolve_citation(
    citation: str, mailto: str = "", *, fetch: Fetch | None = None
) -> list[dict[str, Any]]:
    """Resolve a citation string to DOI candidates, best first.

    Each candidate: ``doi``, ``title``, ``journal``, ``year``, ``volume``,
    ``page``, ``first_author``, ``crossref_score``, ``confidence`` (0-1).
    ``fetch`` is the injectable transport (tests).
    """
    parsed = parse_citation(citation)
    items = (fetch or _fetch_items)(citation, mailto)
    cands: list[dict[str, Any]] = []
    for it in items:
        doi = it.get("DOI")
        if not doi:
            continue
        authors = it.get("author") or []
        cands.append(
            {
                "doi": str(doi).lower(),
                "title": (it.get("title") or [""])[0],
                "journal": (it.get("container-title") or [""])[0],
                "year": _issued_year(it),
                "volume": it.get("volume"),
                "page": it.get("page"),
                "first_author": authors[0].get("family") if authors else None,
                "crossref_score": it.get("score"),
                "confidence": round(_confidence(parsed, it), 2),
            }
        )
    cands.sort(key=lambda c: (-c["confidence"], -(c["crossref_score"] or 0)))
    return cands


def pick_unambiguous(cands: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one candidate safe to auto-stub: exactly one at full confidence."""
    top = [c for c in cands if c["confidence"] >= ACCEPT_CONFIDENCE]
    return top[0] if len(top) == 1 else None
