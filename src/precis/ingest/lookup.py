"""Metadata lookup cascade: DOI → CrossRef → S2 → embedded fallback."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from precis.ingest.crossref import lookup_crossref
from precis.ingest.pdf_sidecar import (
    candidate_title_from_text,
    extract_doi_from_filename,
    extract_pdf_meta,
    is_garbage_author,
    is_garbage_title,
    is_pii,
)
from precis.ingest.semantic_scholar import get_paper_by_id, lookup_s2
from precis.ingest.verify_metadata import verify_metadata
from precis.secrets import get_secret

log = logging.getLogger(__name__)

# arXiv filename patterns: 2508.20254v1.pdf, 2310.18288v3.pdf, etc.
_ARXIV_FILENAME_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


def lookup(pdf_path: str) -> dict[str, Any]:
    """Full metadata lookup cascade for a PDF.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        Merged metadata dict with 'source' indicating provenance.
    """
    pdf_meta = extract_pdf_meta(pdf_path)
    doi = pdf_meta.get("doi")

    mailto = get_secret("ACATOME_CROSSREF_MAILTO") or ""
    s2_key = get_secret("SEMANTIC_SCHOLAR_API_KEY") or ""

    # Try DOI → CrossRef
    if doi:
        result = lookup_doi(doi, mailto=mailto)
        if result:
            result["pdf_hash"] = pdf_meta["pdf_hash"]
            result["page_count"] = pdf_meta["page_count"]
            result["first_pages_text"] = pdf_meta["first_pages_text"]
            _augment_with_s2_cluster(result, doi=doi, s2_key=s2_key)
            return result

    # Try arxiv ID from filename → S2
    arxiv_id = _extract_arxiv_from_filename(pdf_path)
    if arxiv_id:
        result = get_paper_by_id(f"ARXIV:{arxiv_id}", api_key=s2_key)
        if result:
            result["pdf_hash"] = pdf_meta["pdf_hash"]
            result["page_count"] = pdf_meta["page_count"]
            result["first_pages_text"] = pdf_meta["first_pages_text"]
            if not result.get("arxiv_id"):
                result["arxiv_id"] = arxiv_id
            return result

    # Try publisher-specific filename → DOI (Nature, APS, …).
    #
    # Some PDFs are archival reprints whose DOI isn't recoverable from body
    # text (partial-page scans, out-of-order page reads, concatenated
    # reprints). The filename often encodes the DOI directly — e.g.
    # ``nature01797.pdf`` → ``10.1038/nature01797``. We verify the guess
    # via CrossRef; a wrong guess returns None and the cascade falls through.
    if not doi:
        filename_doi = extract_doi_from_filename(pdf_path)
        if filename_doi:
            result = lookup_doi(filename_doi, mailto=mailto)
            if result:
                result["pdf_hash"] = pdf_meta["pdf_hash"]
                result["page_count"] = pdf_meta["page_count"]
                result["first_pages_text"] = pdf_meta["first_pages_text"]
                result["source"] = "crossref_filename"
                _augment_with_s2_cluster(result, doi=filename_doi, s2_key=s2_key)
                return result

    # Try title → S2 (skip PII strings and known-garbage patterns).
    #
    # Garbage titles (InDesign filenames, manuscript tracking IDs like
    # "nl404795z 1..9", APS revtex boilerplate like "USING STANDARD PRB S")
    # poison S2's fuzz search — it returns a random plausible-but-wrong paper
    # with high confidence. Better to fall through to the embedded-metadata
    # fallback so the downstream text-rescue step can mine the real title
    # from block text and re-query S2 with something meaningful.
    title = pdf_meta.get("info", {}).get("title", "")
    if title and not is_pii(title) and not is_garbage_title(title):
        result = lookup_title(title, s2_key=s2_key)
        if result:
            result["pdf_hash"] = pdf_meta["pdf_hash"]
            result["page_count"] = pdf_meta["page_count"]
            result["first_pages_text"] = pdf_meta["first_pages_text"]
            if doi and not result.get("doi"):
                result["doi"] = doi
            return result

    # Text-rescue step: when the embedded title is missing/garbage and no
    # DOI was found, mine a candidate title from the first-page body text
    # and re-query S2 — accepting the hit ONLY if it verifies against the
    # body. This is the step the embedded-title comment above promised:
    # for scanned / dvips PDFs ("No Job Name"), the real title is still
    # legible in the body even when the Info dict is junk. The verify gate
    # is what makes a fuzzy body-title S2 search safe — a wrong candidate
    # returns a plausible paper, but verify_metadata rejects it.
    first_pages_text = pdf_meta.get("first_pages_text", "")
    body_title = candidate_title_from_text(first_pages_text)
    if body_title:
        result = lookup_title(body_title, s2_key=s2_key)
        if result and verify_metadata(result, first_pages_text)[0]:
            result["pdf_hash"] = pdf_meta["pdf_hash"]
            result["page_count"] = pdf_meta["page_count"]
            result["first_pages_text"] = pdf_meta["first_pages_text"]
            result["source"] = "s2_body_title"
            if doi and not result.get("doi"):
                result["doi"] = doi
            return result

    # Fallback: embedded PDF metadata. Garbage title / authors are dropped
    # (not stored) — a blank title flags the paper for triage rather than
    # poisoning the corpus with "No Job Name" or a tool-stamp author.
    info = pdf_meta.get("info", {})
    raw_title = info.get("title", "")
    return {
        "title": ""
        if (is_pii(raw_title) or is_garbage_title(raw_title))
        else raw_title,
        "authors": _sanitize_authors(_parse_author_string(info.get("author", ""))),
        "year": _parse_year(info.get("creationDate", "")),
        "doi": doi,
        "journal": "",
        "abstract": "",
        "entry_type": "article",
        "source": "embedded",
        "pdf_hash": pdf_meta["pdf_hash"],
        "page_count": pdf_meta["page_count"],
        "first_pages_text": pdf_meta["first_pages_text"],
    }


def lookup_doi(doi: str, mailto: str = "") -> dict[str, Any] | None:
    """Look up metadata by DOI via CrossRef."""
    return lookup_crossref(doi, mailto=mailto)


def _augment_with_s2_cluster(
    result: dict[str, Any], *, doi: str, s2_key: str = ""
) -> None:
    """Mutate *result* in place to add ``external_ids`` + ``s2_id`` from S2.

    Called after a successful CrossRef lookup so the downstream bundle
    carries the full Semantic Scholar ``externalIds`` cluster (DOI,
    ArXiv, PubMed, MAG, DBLP, CorpusId, OpenAlex, …). Without this, the
    most common ingest path — journal PDF with embedded DOI → CrossRef
    hit — drops every alias except the canonical DOI, and precis-mcp
    only learns about the rest after a separate ``enrich-paper-
    identifiers`` sweep.

    Failure modes (S2 down, rate-limited, DOI not in S2's index) are
    silent: ``result`` keeps its CrossRef shape and the cluster will be
    backfilled by the next sweep. Existing keys are NEVER overwritten —
    CrossRef wins on title / authors / year / journal because it's the
    canonical publisher record.
    """
    if not doi:
        return
    try:
        s2 = get_paper_by_id(f"DOI:{doi}", api_key=s2_key)
    except Exception as exc:  # network / unexpected upstream raise
        log.debug("S2 cluster augment failed for DOI %s: %s", doi, exc)
        return
    if not s2:
        return
    ext = s2.get("external_ids") or {}
    if ext:
        result["external_ids"] = ext
    # Adopt s2_id if CrossRef didn't carry one.
    if not result.get("s2_id") and s2.get("s2_id"):
        result["s2_id"] = s2["s2_id"]
    # Adopt arxiv_id if CrossRef didn't carry one (CrossRef rarely does;
    # S2 attaches the arXiv preprint to the journal record routinely).
    if not result.get("arxiv_id") and s2.get("arxiv_id"):
        result["arxiv_id"] = s2["arxiv_id"]


def lookup_title(title: str, s2_key: str = "") -> dict[str, Any] | None:
    """Look up metadata by title via Semantic Scholar."""
    return lookup_s2(title, api_key=s2_key)


def _parse_author_string(author: str | None) -> list[dict[str, str]]:
    """Split a raw PDF author string into individual author dicts.

    Handles semicolon-separated, ' and '-separated, and single-author strings.
    Returns empty list for empty/whitespace-only input.
    """
    if not author or not author.strip():
        return []
    # Semicolon-separated (most common in embedded metadata)
    if ";" in author:
        parts = [p.strip() for p in author.split(";") if p.strip()]
    # " and " separated
    elif " and " in author.lower():
        parts = [
            p.strip()
            for p in re.split(r"\s+and\s+", author, flags=re.IGNORECASE)
            if p.strip()
        ]
    else:
        parts = [author.strip()]
    return [{"name": p} for p in parts]


def _sanitize_authors(authors: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop tool/account-stamp entries from a parsed author list.

    Applied to embedded ``/Author`` strings only — CrossRef / S2 authors
    are authoritative and never pass through here. Filters values like
    ``"Microsoft Office User"`` or bare initials (``"DRP"``) that would
    otherwise become a stored author and a cite_key surname. See
    :func:`precis.ingest.pdf_sidecar.is_garbage_author`.
    """
    return [a for a in authors if not is_garbage_author(a.get("name", ""))]


def _extract_arxiv_from_filename(pdf_path: str) -> str | None:
    """Extract arXiv ID from a PDF filename like '2508.20254v1.pdf'."""
    stem = Path(pdf_path).stem
    # Strip trailing timestamp suffixes like _20260402224204
    stem = re.sub(r"_\d{14}$", "", stem)
    m = _ARXIV_FILENAME_RE.match(stem)
    if m:
        return m.group(1)  # e.g. '2508.20254' without version suffix
    return None


def _parse_year(date_str: str) -> int | None:
    """Extract year from PDF date string like 'D:20240115...'."""
    if not date_str:
        return None
    # PDF dates: D:YYYYMMDDHHmmSS or just YYYY...
    clean = date_str.replace("D:", "").strip()
    if len(clean) >= 4 and clean[:4].isdigit():
        year = int(clean[:4])
        if 1900 <= year <= 2100:
            return year
    return None
