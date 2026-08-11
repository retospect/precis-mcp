"""CrossRef metadata lookup via habanero."""

from __future__ import annotations

from typing import Any

from habanero import Crossref


def fetch_message(doi: str, mailto: str = "") -> dict[str, Any] | None:
    """Raw CrossRef ``message`` dict for a DOI, or ``None`` if not found.

    :func:`_normalize` (via :func:`lookup_crossref`) drops several fields
    a caller may still want off the same response — ``ISSN``,
    ``update-to`` retraction/correction notices — so this is exposed
    separately rather than folded into the normalized shape (which stays
    stable for existing callers). Used by
    :mod:`precis.ingest.paper_meta_enrich` (the corpus-wide backfill
    pass) to get metadata + retraction signal from one fetch instead of
    two.
    """
    cr = Crossref(mailto=mailto) if mailto else Crossref()
    try:
        result = cr.works(ids=doi)
    except Exception:
        return None
    if not result or "message" not in result:
        return None
    return result["message"]


def lookup_crossref(doi: str, mailto: str = "") -> dict[str, Any] | None:
    """Fetch metadata from CrossRef for a given DOI.

    Args:
        doi: The DOI to look up.
        mailto: Email for CrossRef polite pool (recommended).

    Returns:
        Normalized metadata dict or None if not found.
    """
    msg = fetch_message(doi, mailto)
    if msg is None:
        return None
    return _normalize(msg, doi)


def orcids_for_doi(doi: str, mailto: str = "") -> list[dict[str, Any]]:
    """Extract author ORCIDs inline in a DOI's Crossref record.

    Returns a list of ``{"orcid": "0000-...", "name": "Given Family",
    "position": i, "n_authors": N}`` for each author whose Crossref entry
    carries an ``ORCID`` field. The cheapest corpus-wide enricher — needs
    no extra auth — that back-fills ORCIDs onto papers we already hold.

    Crossref stores the ORCID as a URL (``http://orcid.org/0000-...``);
    we strip to the bare iD. Returns ``[]`` on any lookup failure or when
    no author carries an ORCID (older papers frequently lack them).
    """
    cr = Crossref(mailto=mailto) if mailto else Crossref()
    try:
        result = cr.works(ids=doi)
    except Exception:
        return []
    if not result or "message" not in result:
        return []
    raw_authors = result["message"].get("author") or []
    n = len(raw_authors)
    out: list[dict[str, Any]] = []
    for i, a in enumerate(raw_authors):
        orcid_url = (a.get("ORCID") or "").strip()
        if not orcid_url:
            continue
        bare = orcid_url.rsplit("/", 1)[-1].strip()
        name = ", ".join(
            p
            for p in ((a.get("family") or "").strip(), (a.get("given") or "").strip())
            if p
        )
        out.append(
            {
                "orcid": bare,
                "name": name,
                "position": i,
                "n_authors": n,
            }
        )
    return out


def _normalize(msg: dict[str, Any], doi: str) -> dict[str, Any]:
    """Normalize CrossRef response to acatome header format.

    Crossref ``author`` entries come in three flavours:

    * ``{"family": "Smith", "given": "John"}`` — typical natural person;
      emitted here as the canonical structured shape (see
      ``precis.utils.authors``), unchanged.
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
            # Crossref hands us the split natively — emit the canonical
            # structured shape rather than joining into a string that
            # normalize_authors would just have to re-split.
            entry: dict[str, str] = {}
            if given:
                entry["given"] = given
            if family:
                entry["family"] = family
            authors.append(entry)
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
                entry = {}
                if given:
                    entry["given"] = given
                if family:
                    entry["family"] = family
                authors.append(entry)
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

    issn_list = msg.get("ISSN") or []
    issn = str(issn_list[0]).strip() if issn_list else ""

    return {
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "journal": (
            msg.get("container-title", [""])[0] if msg.get("container-title") else ""
        ),
        "issn": issn,
        "abstract": msg.get("abstract", ""),
        "entry_type": msg.get("type", "article"),
        "source": "crossref",
    }
