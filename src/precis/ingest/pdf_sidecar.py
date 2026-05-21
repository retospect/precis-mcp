"""PDF metadata extraction via PyMuPDF (fitz)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import fitz

DOI_REGEX = re.compile(r"10\.\d{4,}/[^\s<\"}\)]+")

# Prefixed-DOI: the publisher's own "this article's DOI is X" typographic
# marker. Far more reliable than a bare DOI match, which might pick up a
# reference-list citation to a different paper. Matches ``doi:X``,
# ``doi.org/X``, ``DOI: X`` forms.
_PREFIXED_DOI_RE = re.compile(
    r"(?:\bdoi[:\s]+|(?:https?://)?(?:dx\.)?doi\.org/|\bDOI\s+)"
    r"(10\.\d{4,}/[^\s<\"}\)]+)",
    re.IGNORECASE,
)

# Heading that marks the start of a reference list. Bare DOIs appearing
# after this heading are citations to other papers, not this paper's DOI.
_REFERENCES_HEADING_RE = re.compile(
    r"\n\s*(?:REFERENCES?|BIBLIOGRAPHY|WORKS\s+CITED|LITERATURE\s+CITED)"
    r"(?:\s*AND\s+NOTES)?\s*\n",
    re.IGNORECASE,
)

# Elsevier PII patterns — appear in title/subject fields of pre-2000 PDFs.
# Formatted: S0009-2614(95)00905-J or 0009-2614(80)80221-1
# The PII *is* the DOI suffix for Elsevier: DOI = 10.1016/{PII}
_PII_RE = re.compile(
    r"(?:PII[:\s]*)?"  # optional "PII:" prefix
    r"(S?\d{4}-\d{3}[\dX]"  # ISSN part: S0009-2614 or 0009-2614
    r"\(\d{2}\)"  # (95)
    r"\d{4,5}-[A-Z\d])"  # 00905-J  (check digit: letter or digit)
)


def extract_pdf_meta(path: str | Path) -> dict[str, Any]:
    """Extract metadata, DOI, and content hash from a PDF.

    Args:
        path: Path to the PDF file.

    Returns:
        Dict with keys: info, xmp, doi, pdf_hash, first_pages_text, page_count.
    """
    path = Path(path)
    doc = fitz.open(str(path))

    info = doc.metadata or {}

    try:
        xmp_raw = doc.get_xml_metadata() or ""
    except Exception:
        xmp_raw = ""

    # Content hash
    pdf_hash = hashlib.sha256(path.read_bytes()).hexdigest()

    # First 5 pages text (for verification + DOI extraction). Five is a
    # compromise: covers short papers (4–6 page Nature letters often have
    # the DOI on the last page — see nature01797) without pulling in the
    # full body of long review articles where the DOI is always on p1.
    first_pages_text = ""
    for i in range(min(5, doc.page_count)):
        try:
            first_pages_text += doc[i].get_text() + "\n"
        except Exception:
            pass

    # DOI extraction cascade: XMP → first-page text → info dict → filename
    doi = _extract_doi(xmp_raw, first_pages_text, info)

    page_count = doc.page_count
    doc.close()

    return {
        "info": info,
        "xmp": xmp_raw,
        "doi": doi,
        "pdf_hash": pdf_hash,
        "first_pages_text": first_pages_text,
        "page_count": page_count,
    }


def _extract_doi(xmp: str, first_pages: str, info: dict[str, Any]) -> str | None:
    """Extract DOI using a confidence-ordered cascade.

    Order (highest → lowest confidence):

    1. Prefixed DOI (``doi:X``, ``doi.org/X``, ``DOI: X``) in body text —
       publisher's own typeset marker. Survives partial-page extracts and
       out-of-order page reads (e.g. nature01797 has its DOI on page 4).
    2. XMP XML metadata.
    3. Bare DOI in body text *before* any References heading. Bare DOIs
       after References are reference-list citations to other papers.
    4. Info dict fields (``doi``, ``subject``, ``keywords``).
    5. Elsevier PII in title/subject, converted to DOI.
    """
    # 1. Prefixed DOI anywhere in body
    if first_pages:
        match = _PREFIXED_DOI_RE.search(first_pages)
        if match:
            return _clean_doi(match.group(1))

    # 2. XMP XML
    if xmp:
        match = DOI_REGEX.search(xmp)
        if match:
            return _clean_doi(match.group())

    # 3. Bare DOI in body text, but only before the References section
    if first_pages:
        pre_refs = _trim_at_references(first_pages)
        match = DOI_REGEX.search(pre_refs)
        if match:
            return _clean_doi(match.group())

    # 4. Info dict fields
    for key in ("doi", "subject", "keywords"):
        val = info.get(key, "")
        if val:
            match = DOI_REGEX.search(val)
            if match:
                return _clean_doi(match.group())

    # 5. PII in title or subject → Elsevier DOI
    title = info.get("title", "")
    subject = info.get("subject", "")
    for field in (title, subject):
        if field:
            pii_doi = _pii_to_doi(field)
            if pii_doi:
                return pii_doi

    return None


def _trim_at_references(text: str) -> str:
    """Return the portion of *text* before any References/Bibliography heading.

    Used by :func:`_extract_doi` so that bare DOI regex matches don't pick
    up reference-list citations (each of which is a DOI to a *different*
    paper, not this paper's DOI).
    """
    m = _REFERENCES_HEADING_RE.search(text)
    return text[: m.start()] if m else text


def _pii_to_doi(text: str) -> str | None:
    """Extract Elsevier DOI from a PII string.

    PII like 'S0009-2614(95)00905-J' → DOI '10.1016/S0009-2614(95)00905-J'.
    """
    m = _PII_RE.search(text)
    if m:
        return f"10.1016/{m.group(1)}"
    return None


def is_pii(text: str) -> bool:
    """Return True if *text* looks like an Elsevier PII string (not a real title)."""
    if not text:
        return False
    return bool(_PII_RE.search(text))


# Garbage title patterns commonly found in PDF embedded metadata.
# These are NOT real paper titles — they're filenames, manuscript tracking
# IDs, or typesetting-template boilerplate that leaked into dc:title.
# Passing them to a title-based search engine (S2, CrossRef title fuzz)
# poisons the lookup and returns an unrelated paper with high confidence.
_GARBAGE_TITLE_RES = [
    # Ends with "N..M" page-range notation (InDesign / Quark XPress page refs).
    # Examples: "nl404795z 1..9", "LQ8388 2..5", "acs_nn_nn-2013-02954e 1..6",
    # "78868 651..703"
    re.compile(r"\s\d+\.\.\d+\s*$"),
    # Ends with a document-source filename extension.
    # Examples: "nmat1849 Geim Progress Article.indd"
    re.compile(r"\.(?:indd|doc|docx|tex|pdf|qxp|qxd|ai|xml|eps)\s*$", re.IGNORECASE),
    # APS/AIP revtex template boilerplate that leaked into dc:title.
    # Example: "USING STANDARD PRB S"
    re.compile(r"^\s*USING\s+STANDARD\b", re.IGNORECASE),
    # Raw LaTeX source leakage.
    re.compile(r"\\(?:documentclass|usepackage|begin\{|end\{)"),
]


def is_garbage_title(text: str) -> bool:
    """Return True if *text* is a known-bad PDF embedded title pattern.

    Distinct from :func:`is_pii`, which detects Elsevier PII identifiers.
    Real paper titles never match these patterns; embedded ``dc:title``
    fields populated by typesetting pipelines frequently do.

    Used to gate S2 title-based fallback lookups so they don't poison
    results with random plausible-but-wrong papers.
    """
    if not text or not text.strip():
        return True
    return any(p.search(text) for p in _GARBAGE_TITLE_RES)


def _clean_doi(doi: str) -> str:
    """Strip trailing punctuation from extracted DOI."""
    return doi.rstrip(".,;:")


# Filename → DOI patterns for archival reprints / partial-page scans where
# the DOI isn't recoverable from body text or embedded metadata. Each entry
# pairs a filename-stem regex with a function that builds the DOI.
#
# The returned DOI is a *guess*. Callers should verify via CrossRef; a
# wrong guess simply produces a 404 and the lookup cascade falls through.
#
# Generalises the existing arXiv-filename heuristic in ``precis.ingest.lookup``
# to Nature and APS archival filenames. Adding new publishers is append-only.
_FILENAME_DOI_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    # Nature Publishing Group — OLD-style manuscript IDs (pre-2019 mostly).
    # DOI form: 10.1038/<id>
    # Covered journals: Nature, Nature Materials (nmat), Nature Physics
    # (nphys), Nature Chemistry (nchem), Nature Nanotechnology (nnano),
    # Nature Methods (nmeth), Nature Geoscience (ngeo), Nature Photonics
    # (nphoton), Nature Climate Change (nclimate), Nature Plants (nplants),
    # Nature Communications (ncomms), Nature Structural & Molecular
    # Biology (nsmb), Nature Reviews {Immunology/Molecular Cell Biology/
    # Genetics/Neuroscience/Cancer/Microbiology/Drug Discovery/Materials}
    # (nri, nrm, nrg, nrn, nrc, nrmicro, nrd, nrmats), Nature Genetics
    # (ng), Nature Immunology (ni), Nature Energy (nenergy), Nature
    # Catalysis (ncatal), Nature Astronomy (nastron), Nature Electronics
    # (nelectronics), Nature Microbiology (nmicrobiol), Nature
    # Sustainability (nsustain), Nature Machine Intelligence (nmachintell).
    # Examples: nature01797, nmat1849, nmat769, nnano.2013.167
    (
        re.compile(
            r"^(n(?:ature|mat|phys|chem|nano|meth|geo|photon|climate|plants"
            r"|comms|smb|rmicro|rmats|ri|rm|rg|rn|rc|rd|energy|catal|astron"
            r"|electronics|microbiol|sustain|machintell|g|i)[\d.]+)"
            r"(?:[\W_]|$)",
            re.IGNORECASE,
        ),
        lambda m: f"10.1038/{m.group(1).lower().rstrip('.')}",
    ),
    # Nature Publishing Group — NEW-style article IDs (post-2019).
    # DOI form: 10.1038/s<journal-id>-<yr>-<seq>-<check>
    #   journal-id: 5 digits (e.g. s41586 = Nature, s41557 = Nature Chemistry,
    #     s41467 = Nature Communications, s41593 = Nature Neuroscience …)
    #   yr:         3 digits encoding year (020 = 2020, 025 = 2025)
    #   seq:        4-6 digits (article sequence within year)
    #   check:      1-2 alphanumerics (check character)
    # Example filenames: s41586-020-2649-2.pdf, s41557-025-01815-x.pdf
    (
        re.compile(r"^(s\d{5}-\d{3}-\d+-[a-z0-9]+)(?:[\W_]|$)", re.IGNORECASE),
        lambda m: f"10.1038/{m.group(1).lower()}",
    ),
    # APS (Physical Review family). DOI form: 10.1103/<id>
    # Families: PhysRev[A-E], PhysRevLett, PhysRevX, PhysRevApplied,
    # PhysRevFluids, PhysRevMaterials, PhysRevResearch, PhysRevSTAB,
    # PhysRevAccelBeams, PhysRevPhysEducRes.
    # Examples: PhysRevLett.89.106801, PhysRevB.63.193409
    (
        re.compile(
            r"^(PhysRev(?:[A-E]|Lett|X|Applied|Fluids|Materials|Research"
            r"|STAB|AccelBeams|PhysEducRes)?\.\d+\.\d+)(?:[\W_]|$)",
            re.IGNORECASE,
        ),
        lambda m: f"10.1103/{m.group(1)}",
    ),
    # Generic "DOI as filename" — common when a user saves a PDF with the
    # DOI in the filename, replacing the ``/`` with ``_`` because the
    # filesystem won't allow it. The DOI prefix (``10.NNNN``) anchors the
    # match so we don't mis-fire on arbitrary underscores.
    #
    # We deliberately do NOT strip a trailing ``-N`` "download-manager
    # version" suffix here because Nature-style DOIs legitimately end in
    # ``-N`` (e.g. ``10.1038/s41560-021-00973-9``) and over-stripping
    # would mangle them. CrossRef returns 404 on a wrongly-suffixed guess,
    # which the cascade in :func:`precis.ingest.lookup.lookup` catches and
    # falls through to title / S2 lookup; that's a cheaper failure than
    # silently returning the wrong paper.
    #
    # Example filenames:
    #   * ``10.30501_jree.2015.70071-4.pdf`` → guess
    #     ``10.30501/jree.2015.70071-4`` (404 → cascade falls through to
    #     title search, which finds the JREE paper).
    #   * ``10.1038_s41560-021-00973-9.pdf`` → guess
    #     ``10.1038/s41560-021-00973-9`` (200, correct).
    (
        re.compile(r"^(10\.\d{3,9})_([\w./\-]+)$"),
        lambda m: f"{m.group(1)}/{m.group(2)}",
    ),
    # Royal Society of Chemistry (RSC). DOI form: 10.1039/<id>
    # Article IDs use a 4-segment fixed scheme:
    #   - decade letter: ``c`` (2000s/2010s) or ``d`` (2020s+)
    #   - 1 digit:       year-of-decade
    #   - 2 letters:     journal code (ee=Energy Environ. Sci., me=Mol. Syst.
    #                    Des. Eng., lc=Lab Chip, sc=Chem. Sci., ta=J. Mater.
    #                    Chem. A, cc=Chem. Commun., cs=Chem. Soc. Rev., …)
    #   - 5 digits:      sequence within journal-year
    #   - 1 letter:      check character
    # Example filenames: c8me00086g, c8ee00122g, d1ee01170g, c0lc00403k.
    (
        re.compile(r"^([cd]\d[a-z]{2}\d{5}[a-z])(?:[\W_]|$)", re.IGNORECASE),
        lambda m: f"10.1039/{m.group(1).lower()}",
    ),
]


def extract_doi_from_filename(path: str | Path) -> str | None:
    """Extract a DOI guess from the filename using publisher-specific patterns.

    Last-resort DOI hint for PDFs whose body text and embedded metadata
    both lack the DOI (archival reprints, partial-page scans, out-of-order
    page layouts that push the DOI past the extractable window).

    The returned DOI is a *guess* derived purely from the filename. The
    caller must verify it against an authoritative source (CrossRef) —
    a wrong guess yields a 404 and the lookup cascade falls through.

    Returns None if no pattern matches.
    """
    stem = Path(path).stem
    for pattern, builder in _FILENAME_DOI_PATTERNS:
        m = pattern.match(stem)
        if m:
            return builder(m)
    return None
