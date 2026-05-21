"""PDF metadata extraction for the v2 ingest pipeline.

Vendored from ``acatome_extract.pdf_metadata`` during B4b. Two
substantial diffs vs. the upstream module:

1. **Bundle paths gone.** The ``.acatome`` bundle format is being
   retired (see ``docs/design/pip-merge.md``). Bundle reads,
   ``_find_acatome_bundle()``, ``_update_bundle_hash_history()``,
   ``get_valid_hashes_for_bundle()``, and the
   ``DoiProvenance.ACATOME_BUNDLE`` source are dropped. Cached
   "have we seen this DOI?" lookups now live in
   ``ref_identifiers`` and are probed by
   :func:`precis.ingest.db_writer.probe_existing` upstream of this
   module — pdf_metadata's job here is purely metadata extraction
   from the PDF + (optional) sidecar.

2. **PDF enrichment workflow gone.** ``write_pdf_metadata()``,
   ``enrich_single_pdf()``, ``enrich_pdfs()`` and the supporting
   exiftool / backup helpers are dropped. v2 stores extracted
   metadata as DB rows; we don't patch the PDF file in place.
   ``EnrichmentResult`` and the JSONL audit-log writer go with
   them.

What survives is the *extraction* subset: parse a PDF, find DOI
candidates from filename / embedded metadata / sidecar / pdf2doi,
validate via the lookup cascade, return a populated
:class:`PdfMetadata` dataclass for ``precis_add()`` to consume.

DOI provenance order (highest trust first):
    1. ``.meta.json`` sidecar
    2. Validated DOI lookup (CrossRef / S2)
    3. PDF embedded metadata (XMP / Info dict)
    4. Filename / arXiv ID heuristics
    5. (Optional) ``pdf2doi`` package fallback
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from precis.ingest.lookup import lookup, lookup_doi
from precis.ingest.pdf_sidecar import extract_doi_from_filename, extract_pdf_meta

log = logging.getLogger(__name__)


def _compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hash of file contents.

    Args:
        path: Path to file.

    Returns:
        Hex-encoded SHA-256 hash string.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class DoiProvenance(Enum):
    """Source of a DOI candidate."""

    EXISTING_PDF_METADATA = "existing_pdf_metadata"
    SIDECAR_META = "sidecar_meta"
    INTERNAL_EXTRACTOR = "internal_extractor"
    SECONDARY_VALIDATOR = "secondary_validator"
    PDF2DOI_FALLBACK = "pdf2doi_fallback"
    FILENAME_PATTERN = "filename_pattern"


@dataclass
class DoiCandidate:
    """A DOI candidate with provenance and validation status."""

    doi: str
    provenance: DoiProvenance
    validated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Normalize DOI: strip whitespace, lowercase prefix
        self.doi = self.doi.strip().lower()
        if self.doi.startswith("doi:"):
            self.doi = self.doi[4:]


@dataclass
class PdfMetadata:
    """Complete metadata model for a PDF.

    Returned by :func:`extract_metadata_from_sources`. The ingest
    writer (``precis_add()``) consumes this dataclass to populate
    refs / ref_identifiers / chunks rows.
    """

    # Identification
    pdf_path: Path
    pdf_hash: str = ""

    # Core bibliographic metadata
    title: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    doi_provenance: DoiProvenance | None = None
    year: int | None = None
    journal: str = ""
    publisher: str = ""
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)

    # Source tracking
    sidecar_meta: dict[str, Any] = field(default_factory=dict)

    # Status
    verified: bool = False
    verify_warnings: list[str] = field(default_factory=list)

    def get_citation_string(self) -> str:
        """Build a citation-style string for Subject field."""
        parts: list[str] = []
        if self.journal:
            parts.append(self.journal)
        if self.year:
            parts.append(str(self.year))
        return ", ".join(parts)


def _normalize_doi(doi: str) -> str:
    """Normalize DOI: strip prefix, whitespace, lowercase."""
    doi = doi.strip().lower()
    if doi.startswith("doi:"):
        doi = doi[4:]
    return doi


def _is_valid_doi_format(doi: str) -> bool:
    """Check if string looks like a valid DOI format."""
    if not doi:
        return False
    # DOI pattern: 10.{registrant}/{suffix}
    return bool(re.match(r"^10\.\d{4,}/[^\s<>\"}]+$", doi))


def _read_existing_pdf_metadata(pdf_path: Path) -> dict[str, Any]:
    """Read current metadata from a PDF using exiftool.

    Returns dict with keys like Title, Author, Subject, Keywords, DOI, etc.
    """
    if not shutil.which("exiftool"):
        log.warning("exiftool not found in PATH")
        return {}

    try:
        result = subprocess.run(
            [
                "exiftool",
                "-json",
                "-Title",
                "-Author",
                "-Subject",
                "-Keywords",
                "-Identifier",
                "-Creator",
                "-Publisher",
                "-Date",
                str(pdf_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("exiftool failed for %s: %s", pdf_path, result.stderr)
            return {}

        data = json.loads(result.stdout)
        if data and isinstance(data, list):
            return data[0]
        return {}
    except subprocess.TimeoutExpired:
        log.warning("exiftool timeout for %s", pdf_path)
        return {}
    except json.JSONDecodeError as e:
        log.warning("exiftool JSON parse error for %s: %s", pdf_path, e)
        return {}
    except Exception as e:
        log.warning("exiftool error for %s: %s", pdf_path, e)
        return {}


def _extract_doi_candidates(pdf_path: Path) -> list[DoiCandidate]:
    """Extract all possible DOI candidates from a PDF.

    Tries multiple sources and returns a prioritized list.
    """
    candidates: list[DoiCandidate] = []

    # 1. Existing PDF metadata
    existing = _read_existing_pdf_metadata(pdf_path)
    for field_name in ["Identifier", "Keywords", "Subject"]:
        val = existing.get(field_name, "")
        if val and isinstance(val, str):
            # Look for DOI pattern
            match = re.search(r"(?:doi:)?(10\.\d{4,}/[^\s<>\"},]+)", val, re.I)
            if match:
                doi = _normalize_doi(match.group(1))
                if _is_valid_doi_format(doi):
                    candidates.append(
                        DoiCandidate(
                            doi=doi,
                            provenance=DoiProvenance.EXISTING_PDF_METADATA,
                        )
                    )

    # 2. Internal extractor (PyMuPDF-based)
    try:
        pdf_meta = extract_pdf_meta(pdf_path)
        if pdf_meta.get("doi"):
            doi = _normalize_doi(pdf_meta["doi"])
            if _is_valid_doi_format(doi):
                candidates.append(
                    DoiCandidate(
                        doi=doi,
                        provenance=DoiProvenance.INTERNAL_EXTRACTOR,
                    )
                )
    except Exception as e:
        log.debug("Internal DOI extraction failed for %s: %s", pdf_path, e)

    # 3. Filename pattern (Nature, APS, etc.)
    try:
        filename_doi = extract_doi_from_filename(pdf_path)
        if filename_doi:
            doi = _normalize_doi(filename_doi)
            if _is_valid_doi_format(doi):
                candidates.append(
                    DoiCandidate(
                        doi=doi,
                        provenance=DoiProvenance.FILENAME_PATTERN,
                    )
                )
    except Exception as e:
        log.debug("Filename DOI extraction failed for %s: %s", pdf_path, e)

    return candidates


def _try_pdf2doi(pdf_path: Path) -> DoiCandidate | None:
    """Try pdf2doi as a fallback DOI finder.

    Returns None if pdf2doi is not installed or fails.
    """
    try:
        import pdf2doi

        result = pdf2doi.get_identifier(str(pdf_path), trygoogle=False)
        if result and result.get("identifier"):
            doi = _normalize_doi(result["identifier"])
            if _is_valid_doi_format(doi):
                return DoiCandidate(
                    doi=doi,
                    provenance=DoiProvenance.PDF2DOI_FALLBACK,
                )
    except ImportError:
        log.debug("pdf2doi not installed, skipping fallback")
    except Exception as e:
        log.debug("pdf2doi failed for %s: %s", pdf_path, e)
    return None


def _validate_doi(doi: str) -> tuple[bool, dict[str, Any]]:
    """Validate a DOI by looking it up.

    Returns (validated, metadata_dict).
    """
    if not doi:
        return False, {}

    try:
        result = lookup_doi(doi)
        if result and result.get("title"):
            return True, result
    except Exception as e:
        log.debug("DOI validation failed for %s: %s", doi, e)

    return False, {}


def _select_best_doi(
    candidates: list[DoiCandidate],
    use_pdf2doi: bool = False,
    pdf_path: Path | None = None,
) -> DoiCandidate | None:
    """Select the best DOI from candidates.

    Priority:
    1. Already validated candidates (from acatome bundle/lookup)
    2. Validate candidates and pick first that succeeds
    3. If use_pdf2doi and pdf_path provided, try pdf2doi as last resort
    """
    # First, check already-validated candidates
    for c in candidates:
        if c.validated:
            return c

    # Try to validate each candidate in order of provenance trust
    provenance_order = [
        DoiProvenance.SIDECAR_META,
        DoiProvenance.SECONDARY_VALIDATOR,
        DoiProvenance.INTERNAL_EXTRACTOR,
        DoiProvenance.EXISTING_PDF_METADATA,
        DoiProvenance.FILENAME_PATTERN,
    ]

    by_provenance: dict[DoiProvenance, list[DoiCandidate]] = {
        p: [] for p in provenance_order
    }
    for c in candidates:
        if c.provenance in by_provenance:
            by_provenance[c.provenance].append(c)

    for prov in provenance_order:
        for c in by_provenance[prov]:
            validated, metadata = _validate_doi(c.doi)
            if validated:
                c.validated = True
                c.metadata = metadata
                return c

    # Try pdf2doi as final fallback
    if use_pdf2doi and pdf_path:
        pdf2doi_candidate = _try_pdf2doi(pdf_path)
        if pdf2doi_candidate:
            validated, metadata = _validate_doi(pdf2doi_candidate.doi)
            if validated:
                pdf2doi_candidate.validated = True
                pdf2doi_candidate.metadata = metadata
            return pdf2doi_candidate

    # Return first candidate even if not validated (best effort)
    for prov in provenance_order:
        if by_provenance[prov]:
            return by_provenance[prov][0]

    return None


def _read_sidecar_meta(pdf_path: Path) -> dict[str, Any]:
    """Read optional .meta.json sidecar alongside PDF."""
    sidecar = pdf_path.with_suffix(".meta.json")
    if sidecar.is_file():
        try:
            return json.loads(sidecar.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def extract_metadata_from_sources(
    pdf_path: Path, use_pdf2doi: bool = False
) -> PdfMetadata:
    """Build complete metadata from all available sources.

    Sources (in order of priority):
    1. ``.meta.json`` sidecar
    2. Validated DOI lookup (CrossRef / S2)
    3. PDF embedded metadata (XMP / Info dict)
    4. Filename / arXiv ID heuristics
    5. (Optional) ``pdf2doi`` fallback

    The B4b strip removed an upstream "1. ``.acatome`` bundle"
    branch that read previously-extracted metadata from a sibling
    ``.acatome`` file. The v2 equivalent ("have we seen this paper
    before?") is the
    :func:`precis.ingest.db_writer.probe_existing` query against
    ``ref_identifiers``, which the caller (``precis_add()``)
    invokes upstream of this function.
    """
    pdf_path = Path(pdf_path).resolve()

    # Initialize with PDF hash
    pdf_meta = extract_pdf_meta(pdf_path)
    metadata = PdfMetadata(
        pdf_path=pdf_path,
        pdf_hash=pdf_meta.get("pdf_hash", ""),
    )

    # Collect DOI candidates from various sources
    candidates: list[DoiCandidate] = []

    # 1. Check for sidecar meta
    sidecar = _read_sidecar_meta(pdf_path)
    if sidecar:
        metadata.sidecar_meta = sidecar
        if sidecar.get("doi"):
            candidates.append(
                DoiCandidate(
                    doi=sidecar["doi"],
                    provenance=DoiProvenance.SIDECAR_META,
                )
            )
        if sidecar.get("title"):
            metadata.title = sidecar["title"]
        if sidecar.get("author"):
            if isinstance(sidecar["author"], str):
                metadata.authors = [sidecar["author"]]
            elif isinstance(sidecar["author"], list):
                metadata.authors = sidecar["author"]

    # 2. Extract DOI candidates from PDF itself
    pdf_candidates = _extract_doi_candidates(pdf_path)
    candidates.extend(pdf_candidates)

    # Select best DOI
    best_doi = _select_best_doi(candidates, use_pdf2doi=use_pdf2doi, pdf_path=pdf_path)
    if best_doi:
        metadata.doi = best_doi.doi
        metadata.doi_provenance = best_doi.provenance

        # If we got metadata from validation, enrich our record
        if best_doi.metadata:
            if not metadata.title and best_doi.metadata.get("title"):
                metadata.title = best_doi.metadata["title"]
            if not metadata.authors and best_doi.metadata.get("authors"):
                metadata.authors = [
                    a.get("name", "")
                    for a in best_doi.metadata["authors"]
                    if a.get("name")
                ]
            if not metadata.year and best_doi.metadata.get("year"):
                metadata.year = best_doi.metadata["year"]
            if not metadata.journal and best_doi.metadata.get("journal"):
                metadata.journal = best_doi.metadata["journal"]

    # 3. If still no title, try full lookup cascade
    if not metadata.title:
        try:
            lookup_result = lookup(str(pdf_path))
            if lookup_result.get("title"):
                metadata.title = lookup_result["title"]
            if lookup_result.get("authors"):
                metadata.authors = [
                    a.get("name", "") for a in lookup_result["authors"] if a.get("name")
                ]
            if lookup_result.get("year"):
                metadata.year = lookup_result["year"]
            if lookup_result.get("journal"):
                metadata.journal = lookup_result["journal"]
            if lookup_result.get("doi") and not metadata.doi:
                metadata.doi = lookup_result["doi"]
                metadata.doi_provenance = DoiProvenance.SECONDARY_VALIDATOR
        except Exception as e:
            log.debug("Lookup cascade failed for %s: %s", pdf_path, e)

    # 5. Fallback: embedded PDF metadata
    if not metadata.title:
        info = pdf_meta.get("info", {})
        metadata.title = info.get("title", "")
    if not metadata.authors:
        info = pdf_meta.get("info", {})
        author_str = info.get("author", "")
        if author_str:
            # Split on semicolons or " and "
            if ";" in author_str:
                metadata.authors = [
                    a.strip() for a in author_str.split(";") if a.strip()
                ]
            elif " and " in author_str.lower():
                metadata.authors = [
                    a.strip()
                    for a in re.split(r"\s+and\s+", author_str, flags=re.I)
                    if a.strip()
                ]
            else:
                metadata.authors = [author_str.strip()]

    return metadata
