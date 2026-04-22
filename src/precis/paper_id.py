"""Paper identifier classifier — §13 / Phase 5.

Given a bare string that the agent might hand us as a paper reference,
classify it into one of the canonical identifier schemes and return a
normalised form.  Designed to be a pure function — no I/O, no DB, just
regex + a little string hygiene.

Supported formats (in classification priority order):

- ``paper:<slug>``          — explicit precis paper slug
- ``doi:10.NNNN/…``         — explicit DOI scheme
- ``arxiv:2401.12345``      — explicit arXiv id (new or old form)
- ``pmid:12345678``         — explicit PubMed id
- ``pmcid:PMC1234567``      — explicit PubMed Central id
- ``isbn:9783161484100``    — explicit ISBN
- ``issn:2049-3630``        — explicit ISSN
- bare DOI (``10.XXXX/y``)                        → ``doi:``
- bare arXiv new form (``2401.12345`` + ``v2``)   → ``arxiv:``
- bare arXiv old form (``cs.CL/0701042``)         → ``arxiv:``
- bare PMCID (``PMC1234567``)                     → ``pmcid:``
- bare ISBN-13 / ISBN-10 (10 or 13 digits, opt hyphens, final ``X`` ok)
                                                  → ``isbn:``
- bare ISSN (``NNNN-NNNX``)                       → ``issn:``
- bare DOI / arXiv URL                            → normalised scheme
- everything else                                 → ``paper:`` (slug lookup)

Ambiguity resolution:

- **Bare digits**: treated as a slug first (papers have integer slugs too
  via `make_paper_id`).  The response layer surfaces a hint suggesting
  ``pmid:`` if the lookup misses.  This matches §13.5's "slug lookup
  first; miss → hint toward ``pmid:``/``isbn:``" rule.
- **Hyphenated digits** look like ISSN or ISBN-10; we test ISSN first
  (more restrictive: exactly 4-4 pattern) and fall through.

Regex porting:

- :data:`_DOI_IN_TEXT`, :func:`normalize_doi`, :data:`_ARXIV_ID_RE`,
  :data:`_ARXIV_OLD_RE`, :func:`normalize_arxiv` lifted from
  ``acatome_quest_mcp.models``.  Kept identical shape for wire compat
  with the ingestion layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "PaperIdentifier",
    "classify_paper_id",
    "normalize_arxiv",
    "normalize_doi",
    "normalize_isbn",
    "normalize_issn",
    "normalize_pmcid",
]


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


IdScheme = Literal["paper", "doi", "arxiv", "pmid", "pmcid", "isbn", "issn"]


@dataclass(frozen=True)
class PaperIdentifier:
    """Classified paper identifier.

    Attributes:
        scheme: Canonical scheme name. One of ``paper`` / ``doi`` /
            ``arxiv`` / ``pmid`` / ``pmcid`` / ``isbn`` / ``issn``.
        value: The normalised identifier value (no scheme prefix).  For
            papers this is the raw slug — no normalisation applied since
            slug semantics are defined by the store.
        note: Human-readable explanation attached when the input was
            ambiguous or non-obvious.  The server surfaces this as a
            hint when the downstream lookup misses (§13.5).
    """

    scheme: IdScheme
    value: str
    note: str = ""

    @property
    def uri(self) -> str:
        """Return the URI form (``<scheme>:<value>``)."""
        return f"{self.scheme}:{self.value}"


# ---------------------------------------------------------------------------
# DOI — ported from acatome-quest-mcp/models.py
# ---------------------------------------------------------------------------

#: DOI pattern: 10.NNNN/suffix (registrant code is 4+ digits).
_DOI_BARE_RE = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9]+$")

#: DOI embedded in free text — same pattern without line anchors.
_DOI_IN_TEXT = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
    "DOI:",
)


def normalize_doi(doi: str | None) -> str | None:
    """Strip URL/prefix and lower-case the registrant prefix.

    Per Crossref: DOIs are case-insensitive, but convention lowercases
    them.  Returns ``None`` if the input can't be coerced into a valid
    ``10.NNNN/…`` DOI.
    """
    if not doi:
        return None
    doi = doi.strip()
    for p in _DOI_PREFIXES:
        if doi.lower().startswith(p.lower()):
            doi = doi[len(p) :]
            break
    # Drop trailing punctuation that often leaks from citation text.
    doi = doi.rstrip(".,;)")
    if not doi:
        return None
    if not doi.startswith("10."):
        return None
    if not _DOI_BARE_RE.match(doi):
        return None
    return doi.lower()


# ---------------------------------------------------------------------------
# arXiv — ported from acatome-quest-mcp/models.py
# ---------------------------------------------------------------------------

#: arXiv new-style id, e.g. ``2401.12345`` or ``2401.12345v2``.
_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$", re.IGNORECASE)

#: arXiv old-style id, e.g. ``cs.CL/0701042`` or ``hep-th/9901001v3``.
_ARXIV_OLD_RE = re.compile(r"^[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?$", re.IGNORECASE)

_ARXIV_PREFIXES = (
    "https://arxiv.org/abs/",
    "http://arxiv.org/abs/",
    "https://arxiv.org/pdf/",
    "http://arxiv.org/pdf/",
    "arxiv:",
    "arXiv:",
    "ARXIV:",
)


def normalize_arxiv(arxiv: str | None) -> str | None:
    """Strip URL / prefix / ``.pdf`` / trailing dot and lowercase.

    Returns ``None`` when the input doesn't match either arXiv form.
    """
    if not arxiv:
        return None
    arxiv = arxiv.strip()
    for prefix in _ARXIV_PREFIXES:
        if arxiv.lower().startswith(prefix.lower()):
            arxiv = arxiv[len(prefix) :]
            break
    if arxiv.lower().endswith(".pdf"):
        arxiv = arxiv[:-4]
    arxiv = arxiv.rstrip(".")
    if _ARXIV_ID_RE.match(arxiv) or _ARXIV_OLD_RE.match(arxiv):
        return arxiv.lower()
    return None


# ---------------------------------------------------------------------------
# PMID / PMCID
# ---------------------------------------------------------------------------

#: Bare PubMed id — 1-9 digits.  Conservative cap matches current
#: PubMed allocator (≈39M, well within 8 digits); 9 leaves headroom.
_PMID_RE = re.compile(r"^\d{1,9}$")

#: PMCID.  Official format: ``PMC`` followed by 6-8 digits.  NCBI keeps
#: adding digits, so we allow 5-10 to be safe.
_PMCID_RE = re.compile(r"^PMC\d{5,10}$", re.IGNORECASE)


def normalize_pmcid(pmcid: str | None) -> str | None:
    """Normalise PMCID to upper-case ``PMCNNNNNNN`` form."""
    if not pmcid:
        return None
    raw = pmcid.strip()
    # Strip common URL form: https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/
    m = re.search(r"PMC\d{5,10}", raw, re.IGNORECASE)
    if m:
        candidate = m.group(0).upper()
        if _PMCID_RE.match(candidate):
            return candidate
    if _PMCID_RE.match(raw):
        return raw.upper()
    return None


# ---------------------------------------------------------------------------
# ISBN — 10 and 13 digit forms, optionally hyphenated
# ---------------------------------------------------------------------------

#: ISBN-13: exactly 13 digits, starting with ``978`` or ``979``.
_ISBN13_RE = re.compile(r"^(978|979)\d{10}$")

#: ISBN-10: 9 digits + final checksum char (digit or ``X``).
_ISBN10_RE = re.compile(r"^\d{9}[\dX]$", re.IGNORECASE)


def _isbn10_valid(isbn: str) -> bool:
    """Mod-11 checksum check for ISBN-10."""
    if len(isbn) != 10:
        return False
    total = 0
    for i, ch in enumerate(isbn):
        if ch.upper() == "X":
            if i != 9:
                return False
            val = 10
        elif ch.isdigit():
            val = int(ch)
        else:
            return False
        total += val * (10 - i)
    return total % 11 == 0


def _isbn13_valid(isbn: str) -> bool:
    """Mod-10 EAN-13 checksum check for ISBN-13."""
    if len(isbn) != 13 or not isbn.isdigit():
        return False
    total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(isbn[:12]))
    return (10 - (total % 10)) % 10 == int(isbn[12])


def normalize_isbn(isbn: str | None) -> str | None:
    """Normalise ISBN-10 or ISBN-13, stripping hyphens/spaces.

    Validates the checksum.  Returns the normalised (hyphen-free, upper-X)
    string, or ``None`` if the input doesn't match a valid ISBN.
    """
    if not isbn:
        return None
    raw = isbn.strip().replace("-", "").replace(" ", "").upper()
    if _ISBN13_RE.match(raw) and _isbn13_valid(raw):
        return raw
    if _ISBN10_RE.match(raw) and _isbn10_valid(raw):
        return raw
    return None


# ---------------------------------------------------------------------------
# ISSN — NNNN-NNNX with mod-11 checksum
# ---------------------------------------------------------------------------

#: ISSN: 8 chars total in the form ``NNNN-NNNX`` or 8 bare digits/X.
_ISSN_RE = re.compile(r"^\d{4}-?\d{3}[\dX]$", re.IGNORECASE)


def _issn_valid(issn: str) -> bool:
    """Mod-11 checksum for ISSN (8 chars, digits + optional final X)."""
    if len(issn) != 8:
        return False
    total = 0
    for i, ch in enumerate(issn[:7]):
        if not ch.isdigit():
            return False
        total += int(ch) * (8 - i)
    check = issn[7].upper()
    expected = (11 - (total % 11)) % 11
    if expected == 10:
        return check == "X"
    return check == str(expected)


def normalize_issn(issn: str | None) -> str | None:
    """Normalise ISSN to the canonical ``NNNN-NNNX`` form."""
    if not issn:
        return None
    raw = issn.strip().upper()
    if not _ISSN_RE.match(raw):
        return None
    compact = raw.replace("-", "")
    if not _issn_valid(compact):
        return None
    return f"{compact[:4]}-{compact[4:]}"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

#: Explicit scheme prefixes we honour verbatim.  If the raw input starts
#: with one of these, we bypass auto-detection and just strip + normalise.
_EXPLICIT_PREFIXES: dict[str, IdScheme] = {
    "paper:": "paper",
    "doi:": "doi",
    "arxiv:": "arxiv",
    "pmid:": "pmid",
    "pmcid:": "pmcid",
    "isbn:": "isbn",
    "issn:": "issn",
}


def _strip_explicit_prefix(raw: str) -> tuple[IdScheme, str] | None:
    """Return ``(scheme, value)`` if ``raw`` has an explicit scheme prefix.

    Case-insensitive match on the prefix, but the returned scheme is the
    canonical lowercase form.
    """
    lower = raw.lower()
    for prefix, scheme in _EXPLICIT_PREFIXES.items():
        if lower.startswith(prefix):
            return scheme, raw[len(prefix) :]
    return None


def classify_paper_id(raw: str) -> PaperIdentifier:
    """Classify a bare or prefixed paper reference.

    The heart of Phase 5.  Returns a :class:`PaperIdentifier` with the
    canonical scheme and the normalised value.

    Resolution order:

    1. Explicit scheme prefix (``paper:``, ``doi:``, ``arxiv:``, ``pmid:``,
       ``pmcid:``, ``isbn:``, ``issn:``) — honoured as-is after value
       normalisation.
    2. URL forms of DOI / arXiv / PMCID — normalised to their schemes.
    3. Structural patterns:
       - starts with ``10.`` → DOI
       - matches arXiv new or old form → arXiv
       - starts with ``PMC`` (any case) → PMCID
       - all digits, 8 or 13 long, valid ISBN checksum → ISBN
       - ``NNNN-NNNX`` / ``NNNNNNNX`` ISSN shape + valid checksum → ISSN
       - arXiv-new pure-digit form already matched above
    4. Fallback → ``paper:`` (slug lookup).

    Ambiguous bare digits (no hyphens, no format match) fall through to
    ``paper:``.  The response layer surfaces a hint recommending
    ``pmid:<n>`` or ``isbn:<n>`` if the slug lookup misses (§13.5).
    """
    if not raw or not raw.strip():
        return PaperIdentifier("paper", "")

    raw = raw.strip()

    # 1. Explicit scheme prefix — honour it.
    explicit = _strip_explicit_prefix(raw)
    if explicit is not None:
        scheme, value = explicit
        value = value.strip()
        if scheme == "doi":
            normalised = normalize_doi(value) or value
            return PaperIdentifier("doi", normalised)
        if scheme == "arxiv":
            normalised = normalize_arxiv(value) or value
            return PaperIdentifier("arxiv", normalised)
        if scheme == "pmcid":
            normalised = normalize_pmcid(value) or value
            return PaperIdentifier("pmcid", normalised)
        if scheme == "isbn":
            normalised = normalize_isbn(value) or value
            return PaperIdentifier("isbn", normalised)
        if scheme == "issn":
            normalised = normalize_issn(value) or value
            return PaperIdentifier("issn", normalised)
        # paper: / pmid: — no further normalisation.
        return PaperIdentifier(scheme, value)

    # 2. URL forms.  normalize_doi / normalize_arxiv already strip URLs.
    doi = normalize_doi(raw)
    if doi:
        return PaperIdentifier("doi", doi)

    arxiv = normalize_arxiv(raw)
    if arxiv:
        return PaperIdentifier("arxiv", arxiv)

    pmcid = normalize_pmcid(raw)
    if pmcid:
        return PaperIdentifier("pmcid", pmcid)

    # 3. Bare structural patterns.
    #    ISSN is more restrictive than ISBN-10 (hyphenated 4-4), so
    #    test it first.
    issn = normalize_issn(raw)
    if issn:
        return PaperIdentifier("issn", issn)

    isbn = normalize_isbn(raw)
    if isbn:
        return PaperIdentifier("isbn", isbn)

    # 4. Pure digits — ambiguous between slug / PMID / ISSN-sans-dashes.
    #    §13.5: slug lookup first; miss → hint toward pmid: / isbn:.
    if _PMID_RE.match(raw):
        return PaperIdentifier(
            "paper",
            raw,
            note=(
                "digits-only id — tried as slug first. "
                f"If this is a PubMed id, use pmid:{raw}."
            ),
        )

    # 5. Everything else → slug lookup via paper: scheme.
    return PaperIdentifier("paper", raw)
