"""Classic citation string -> DOI candidates via Crossref.

``Phys. Rev. B 47, 558 (1993)`` -> ranked candidates, each with a
``confidence`` (share of the string's year/volume/page/journal the Crossref
record matches). Only one candidate at full confidence may be auto-stubbed
(:func:`pick_unambiguous`); anything weaker is returned to the caller.

Known Wiley eLocators get a DOI lookup before the bibliographic query.
The bibliographic query goes through ``safe_get`` (see ``utils/safe_fetch.py``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from precis.utils.http import polite_user_agent, with_backoff
from precis.utils.rate_limit import RateLimited

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
_WILEY_LOCATOR_RE = re.compile(r"e?(\d{2})(\d{5})", re.IGNORECASE)
_WILEY_JOURNAL_CODES = {
    "adv energy mater": "aenm",
    "advanced energy materials": "aenm",
    "adv funct mater": "adfm",
    "advanced functional materials": "adfm",
    "adv mater": "adma",
    "advanced materials": "adma",
    "adv sci": "advs",
    "advanced science": "advs",
    "small": "smll",
}
_FRONT_MATTER_TITLES = {"masthead", "issue publication information"}

Fetch = Callable[[str, str], list[dict[str, Any]]]
FetchDOI = Callable[[str, str], dict[str, Any] | None]


def parse_citation(citation: str) -> dict[str, str]:
    """Pull ``year`` / ``volume`` / ``page`` / ``journal`` out of a citation
    string. Keys are absent when not found; never raises."""
    out: dict[str, str] = {}
    text = citation.strip()
    years = list(_YEAR_RE.finditer(text))
    if years:
        year = years[-1]
        out["year"] = year.group()
        text = text[: year.start()] + " " + text[year.end() :]
    m = _VOL_PAGE_RE.search(text)
    if m:
        out["volume"], out["page"] = m.group(1), m.group(2)
        text = text[: m.start()]
    out["journal"] = re.sub(r"[()\[\]]", " ", text).strip(" ,;.")
    return out


def _year(item: dict[str, Any], field: str) -> int | None:
    parts = ((item.get(field) or {}).get("date-parts") or [[None]])[0]
    return parts[0] if parts else None


def _first_page(item: dict[str, Any]) -> str:
    return re.split(r"[-–]", str(item.get("page") or ""), maxsplit=1)[0].strip()


def _article_number(item: dict[str, Any]) -> str:
    return str(item.get("article-number") or "").strip()


def _title(item: dict[str, Any]) -> str:
    return (item.get("title") or [""])[0]


def _is_front_matter(item: dict[str, Any]) -> bool:
    return (
        _title(item).strip().lower() in _FRONT_MATTER_TITLES
        or item.get("type") == "journal-issue"
        or not (item.get("author") or _first_page(item) or _article_number(item))
    )


def _confidence(parsed: dict[str, str], item: dict[str, Any]) -> float:
    """Fraction of parsed citation features the Crossref item matches; 0.0 if
    nothing was parsed."""
    checks: list[bool] = []
    if "year" in parsed:
        checks.append(
            any(
                str(_year(item, field)) == parsed["year"]
                for field in ("issued", "published-print", "published-online")
            )
        )
    if "volume" in parsed:
        checks.append(str(item.get("volume", "")) == parsed["volume"])
    if "page" in parsed:
        checks.append(
            parsed["page"].lower()
            in {_first_page(item).lower(), _article_number(item).lower()}
        )
    jwords = set(_WORD_RE.findall(parsed.get("journal", "").lower()))
    if jwords:
        titles = " ".join(
            (item.get("container-title") or [])
            + (item.get("short-container-title") or [])
        )
        cwords = _WORD_RE.findall(titles.lower())
        # Abbreviations: "Phys. Rev." must prefix-match "Physical Review".
        checks.append(all(any(c.startswith(j) for c in cwords) for j in jwords))
    confidence = sum(checks) / len(checks) if checks else 0.0
    return confidence / 2 if "page" in parsed and _is_front_matter(item) else confidence


def _wiley_item(
    parsed: dict[str, str], mailto: str, fetch_doi: FetchDOI
) -> dict[str, Any] | None:
    journal = " ".join(_WORD_RE.findall(parsed.get("journal", "").lower()))
    code = _WILEY_JOURNAL_CODES.get(journal)
    locator = _WILEY_LOCATOR_RE.fullmatch(parsed.get("page", ""))
    if not code or not locator:
        return None

    # The locator embeds the DOI year, which can precede the issue year.
    doi = f"10.1002/{code}.20{locator[1]}{locator[2]}"
    try:
        item = fetch_doi(doi, mailto)
    except RateLimited:
        return None
    if (
        item
        and str(item.get("DOI", "")).lower() == doi
        and item.get("author")
        and _title(item)
        and _confidence(parsed, item) >= ACCEPT_CONFIDENCE
    ):
        return item
    return None


def _fetch_items(citation: str, mailto: str) -> list[dict[str, Any]]:
    """``query.bibliographic`` lookup via ``safe_get``; ``[]`` on any failure."""
    from precis.utils.http import http_client, request_with_deadline
    from precis.utils.safe_fetch import safe_get

    params: dict[str, Any] = {
        "query.bibliographic": citation,
        "rows": _ROWS,
        "select": "DOI,title,author,issued,container-title,"
        "short-container-title,volume,page,article-number,published-print,"
        "published-online,type,score",
    }
    if mailto:
        params["mailto"] = mailto

    def _send() -> Any:
        # One client per attempt: an expired deadline closes it for good.
        with http_client(
            timeout=_TIMEOUT_S, user_agent=polite_user_agent(mailto)
        ) as client:
            # Per-attempt deadline: per-phase timeouts alone can't stop a peer
            # trickling bytes forever (gr486823).
            return request_with_deadline(
                client,
                2 * _TIMEOUT_S,
                lambda: safe_get(client, _CROSSREF_WORKS_URL, params=params),
                label=f"GET {_CROSSREF_WORKS_URL}",
            )

    try:
        # Service "crossref-list": one shared token per attempt, Retry-After
        # honoured; an exhausted 429 raises -> ``[]`` like any failure.
        resp = with_backoff(_send, service="crossref-list")
        if resp.status_code != 200:
            return []
        return list(resp.json()["message"]["items"])
    except Exception:
        return []


def resolve_citation(
    citation: str,
    mailto: str = "",
    *,
    fetch: Fetch | None = None,
    fetch_doi: FetchDOI | None = None,
) -> list[dict[str, Any]]:
    """Resolve a citation string to DOI candidates, best first.

    Each candidate: ``doi``, ``title``, ``journal``, ``year``, ``volume``,
    ``page``, ``first_author``, ``crossref_score``, ``confidence`` (0-1).
    ``fetch`` overrides the bibliographic query. When supplied, it also
    disables the DOI lookup unless ``fetch_doi`` is supplied.
    """
    parsed = parse_citation(citation)
    if fetch is None and fetch_doi is None:
        from precis.ingest.crossref import fetch_message

        fetch_doi = fetch_message
    exact = _wiley_item(parsed, mailto, fetch_doi) if fetch_doi else None
    items = [exact] if exact else (fetch or _fetch_items)(citation, mailto)
    cands: list[dict[str, Any]] = []
    for it in items:
        doi = it.get("DOI")
        if not doi:
            continue
        authors = it.get("author") or []
        cands.append(
            {
                "doi": str(doi).lower(),
                "title": _title(it),
                "journal": (it.get("container-title") or [""])[0],
                "year": _year(it, "published-print") or _year(it, "issued"),
                "volume": it.get("volume"),
                "page": it.get("page") or it.get("article-number"),
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
