"""CrossRef metadata lookup via habanero."""

from __future__ import annotations

from typing import Any

from habanero import Crossref


def lookup_crossref(doi: str, mailto: str = "") -> dict[str, Any] | None:
    """Fetch metadata from CrossRef for a given DOI.

    Args:
        doi: The DOI to look up.
        mailto: Email for CrossRef polite pool (recommended).

    Returns:
        Normalized metadata dict or None if not found.
    """
    cr = Crossref(mailto=mailto) if mailto else Crossref()
    try:
        result = cr.works(ids=doi)
    except Exception:
        return None

    if not result or "message" not in result:
        return None

    msg = result["message"]
    return _normalize(msg, doi)


def _normalize(msg: dict[str, Any], doi: str) -> dict[str, Any]:
    """Normalize CrossRef response to acatome header format.

    Crossref ``author`` entries come in three flavours:

    * ``{"family": "Smith", "given": "John"}`` — typical natural person.
    * ``{"name": "OECD"}`` — corporate / organisational author (no
      family/given split).
    * ``{"name": "Master of Science in Management, ..."}`` — affiliation
      strings mistakenly inserted as author entries by some publishers
      (e.g. some open-access journals). These poison the slug, so we drop
      them.

    The order is preserved so the slug surname comes from the first valid
    natural author when one is present, falling back to a corporate name
    only when no real authors exist. ``editor`` is consulted as a
    last-resort fallback for collected volumes / proceedings whose
    Crossref record carries editors but no authors (e.g.
    ``10.1007/978-3-031-04881-4`` — *Pattern Recognition and Image
    Analysis*).
    """

    def _looks_like_affiliation(s: str) -> bool:
        """Heuristic: affiliation strings have a comma + institutional cue."""
        low = s.lower()
        cues = (
            "university",
            "institute",
            "college",
            "school of",
            "department",
            "faculty",
            "laboratory",
            ", usa",
            ", uk",
            ", germany",
            ", france",
            ", canada",
            ", japan",
            ", india",
            ", china",
            ", australia",
        )
        return "," in s and any(c in low for c in cues)

    authors: list[dict[str, str]] = []
    raw_authors = msg.get("author") or []
    raw_editors = msg.get("editor") or []

    for a in raw_authors:
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        if family or given:
            parts = [p for p in (family, given) if p]
            authors.append({"name": ", ".join(parts)})
            continue
        # Corporate or affiliation-mistaken entry — only the ``name`` field is set.
        name = (a.get("name") or "").strip()
        if not name or _looks_like_affiliation(name):
            continue
        authors.append({"name": name})

    # Editors are a last-resort fallback for edited collections.
    if not authors:
        for e in raw_editors:
            family = (e.get("family") or "").strip()
            given = (e.get("given") or "").strip()
            if family or given:
                parts = [p for p in (family, given) if p]
                authors.append({"name": ", ".join(parts)})
            else:
                name = (e.get("name") or "").strip()
                if name and not _looks_like_affiliation(name):
                    authors.append({"name": name})

    year = None
    for date_field in ("published-print", "published-online", "created"):
        parts = msg.get(date_field, {}).get("date-parts", [[]])
        if parts and parts[0] and parts[0][0]:
            year = parts[0][0]
            break

    title_list = msg.get("title", [])
    title = title_list[0] if title_list else ""

    return {
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "journal": (
            msg.get("container-title", [""])[0] if msg.get("container-title") else ""
        ),
        "abstract": msg.get("abstract", ""),
        "entry_type": msg.get("type", "article"),
        "source": "crossref",
    }
