"""OpenAlex free-metadata enrichment (OPEN-ITEMS OA #G).

The OpenAlex *work* object is free + keyless and far richer than the
CrossRef/S2 metadata we hold — 49 fields incl. citations, topics, funders,
ORCID+ROR affiliations. This slurps it into the ref, independent of (and much
cheaper than) the paid OpenAlex Content PDF pull (``workers/fetch_oa`` /
``precis fetch-openalex``).

What lands where:

* ``meta.openalex`` — a versioned block: OpenAlex id, reconstructed abstract,
  topics/concepts/keywords, funders, ``fwci`` + ``cited_by_count``, SDGs, MeSH,
  ``referenced_works`` (the raw W-ids + count — the *edge materialization*
  into ``links`` is deferred to the scholarly-graph item, roadmap #6), and the
  structured ``authorships`` (name + ORCID + institution ROR + country).
* ``refs.authors`` (byline column) — filled from OpenAlex authorships **only
  when the ref has none** (thin/empty), so a good existing byline is never
  clobbered; ORCID/ROR survive via ``to_author_dicts``.
* ``ref_identifiers`` — the ``openalex:W…`` id registered.

Topics are captured in ``meta.openalex`` rather than written as ``topic:``
``ref_tags`` on purpose — the OPEN-tag namespace is mid-teardown, so
materializing controlled topic tags waits for that axis to settle.

Idempotent: ``meta_patch`` merges ``meta || {openalex: …}`` so a re-run
overwrites the block wholesale. Keyless, fixed host (no SSRF surface); the
polite ``mailto`` routes us to OpenAlex's fast pool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from precis.utils.authors import to_author_dicts

log = logging.getLogger(__name__)

_OPENALEX_WORKS = "https://api.openalex.org/works"
_TIMEOUT_S = 30.0
_UA = "precis-mcp/8.0 (+https://github.com/retostamm/precis-mcp; mailto:{email})"

#: Bump when the shape of the ``meta.openalex`` block changes so a backfill
#: sweep (``meta->openalex->>'v' != current``) can re-claim the corpus.
ENRICH_VERSION = 1


def _ua(email: str) -> str:
    return _UA.format(email=email or "noreply@example.com")


def fetch_openalex_work(
    doi: str, *, email: str = "", timeout: float = _TIMEOUT_S
) -> dict[str, Any] | None:
    """Fetch the free OpenAlex work object for a DOI, or ``None`` if unknown.

    Keyless. A 404 (DOI not in OpenAlex) returns ``None``; other HTTP errors
    raise for the caller to record. ``mailto`` is polite + faster.
    """
    url = f"{_OPENALEX_WORKS}/doi:{doi}"
    params = {"mailto": email} if email else {}
    with httpx.Client(timeout=timeout, headers={"User-Agent": _ua(email)}) as client:
        resp = client.get(url, params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, dict) else None


def _short_id(openalex_url: str | None) -> str:
    """``https://openalex.org/W123`` → ``W123`` (bare id)."""
    if not openalex_url:
        return ""
    return str(openalex_url).rstrip("/").rsplit("/", 1)[-1]


def _reconstruct_abstract(inverted: Any) -> str:
    """Rebuild plain text from OpenAlex's ``abstract_inverted_index``.

    The index maps ``token -> [positions]``; we scatter tokens back to their
    positions and join. Returns ``""`` on anything unexpected (pure, never
    raises).
    """
    if not isinstance(inverted, dict) or not inverted:
        return ""
    slots: dict[int, str] = {}
    for token, positions in inverted.items():
        if not isinstance(positions, list):
            continue
        for p in positions:
            if isinstance(p, int):
                slots[p] = token
    if not slots:
        return ""
    return " ".join(slots[i] for i in range(max(slots) + 1) if i in slots)


def _names(items: Any, key: str = "display_name") -> list[str]:
    """Pluck ``display_name`` (or ``key``) off a list of dicts, skipping blanks."""
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for it in items:
        name = (it.get(key) if isinstance(it, dict) else None) or ""
        name = str(name).strip()
        if name and name not in out:
            out.append(name)
    return out


def _authorships(work: dict[str, Any]) -> list[dict[str, str]]:
    """Structured author rows: name + ORCID + first institution ROR + country."""
    out: list[dict[str, str]] = []
    for a in work.get("authorships") or []:
        if not isinstance(a, dict):
            continue
        author = a.get("author") or {}
        name = str(author.get("display_name") or "").strip()
        if not name:
            continue
        row: dict[str, str] = {"name": name}
        orcid = str(author.get("orcid") or "").strip()
        if orcid:
            row["orcid"] = orcid
        insts = a.get("institutions") or []
        if insts and isinstance(insts[0], dict):
            aff = str(insts[0].get("display_name") or "").strip()
            ror = str(insts[0].get("ror") or "").strip()
            cc = str(insts[0].get("country_code") or "").strip()
            if aff:
                row["affiliation"] = aff
            if ror:
                row["ror"] = ror
            if cc:
                row["country"] = cc
        out.append(row)
    return out


@dataclass
class OpenAlexEnrichment:
    """Normalized enrichment ready to write onto a ref."""

    openalex_id: str
    meta: dict[str, Any]
    authorships: list[dict[str, str]] = field(default_factory=list)

    @property
    def byline_authors(self) -> list[dict[str, str]]:
        """Author byline in the canonical storage shape (name+affiliation+ror)."""
        return to_author_dicts(self.authorships)


def normalize(work: dict[str, Any]) -> OpenAlexEnrichment:
    """Turn a raw OpenAlex work object into an :class:`OpenAlexEnrichment`.

    Pure — no I/O, tolerant of missing fields. The ``meta`` dict is the block
    stored under ``meta.openalex``.
    """
    oid = _short_id(work.get("id"))
    authorships = _authorships(work)
    meta: dict[str, Any] = {"v": ENRICH_VERSION}
    if oid:
        meta["id"] = oid

    abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))
    if abstract:
        meta["abstract"] = abstract

    primary = work.get("primary_topic") or {}
    if isinstance(primary, dict) and primary.get("display_name"):
        meta["primary_topic"] = str(primary["display_name"]).strip()
    for src_key, dst_key in (
        ("topics", "topics"),
        ("concepts", "concepts"),
        ("keywords", "keywords"),
        ("funders", "funders"),
        ("sustainable_development_goals", "sdgs"),
        ("mesh", "mesh"),
    ):
        vals = _names(
            work.get(src_key),
            key="descriptor_name" if src_key == "mesh" else "display_name",
        )
        if vals:
            meta[dst_key] = vals

    for num_key in ("fwci", "cited_by_count", "referenced_works_count"):
        v = work.get(num_key)
        if isinstance(v, (int, float)):
            meta[num_key] = v

    refs = work.get("referenced_works")
    if isinstance(refs, list) and refs:
        meta["referenced_works"] = [_short_id(r) for r in refs if r]

    if work.get("is_retracted") is not None:
        meta["is_retracted"] = bool(work["is_retracted"])
    oa = work.get("open_access") or {}
    if isinstance(oa, dict) and oa.get("oa_status"):
        meta["oa_status"] = str(oa["oa_status"])
    if authorships:
        meta["authorships"] = authorships

    return OpenAlexEnrichment(openalex_id=oid, meta=meta, authorships=authorships)


def enrich_ref(
    store: Any,
    ref_id: int,
    *,
    doi: str,
    email: str = "",
    source: str = "openalex-enrich",
) -> OpenAlexEnrichment | None:
    """Fetch + normalize + write OpenAlex metadata onto ``ref_id``.

    Returns the enrichment (for reporting) or ``None`` when OpenAlex has no
    record for the DOI. Fills the ``authors`` byline column only when the ref
    currently has none, so a good existing byline is preserved.
    """
    work = fetch_openalex_work(doi, email=email)
    if work is None:
        return None
    enr = normalize(work)

    # Overwrite the byline only when the ref has no authors today.
    authors: list[dict[str, str]] | None = None
    if enr.byline_authors:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT authors FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()
        current = row[0] if row else None
        if not current:  # None or empty list
            authors = enr.byline_authors

    store.update_paper_fields(
        ref_id,
        authors=authors,
        meta_patch={"openalex": enr.meta},
        source=source,
    )
    if enr.openalex_id:
        store.set_ref_identifier(ref_id, "openalex", enr.openalex_id, source=source)
    return enr


__all__ = [
    "ENRICH_VERSION",
    "OpenAlexEnrichment",
    "enrich_ref",
    "fetch_openalex_work",
    "normalize",
]
