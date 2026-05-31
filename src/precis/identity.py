"""Identifier derivation for precis-mcp v2.

Pure functions only — no DB, no I/O, no model loads. Everything that
mints a ``paper_id``, ``pub_id``, ``cite_key``, ``pdf_sha256``,
``content_hash``, or ``node_id`` lives here.

Read first:

- ``docs/design/identity.md`` — algorithm choices, edge cases, test plan
- ``docs/decisions/0002-pub-id-and-toon.md`` — pub_id formula (locked)
- ``docs/decisions/0006-tri-identifier-scheme.md`` — cite_key (locked)
- ``docs/decisions/0008-drop-slug-identifier-normalisation.md`` —
  identifiers normalised into ``ref_identifiers``; ``slug`` retired
- ``docs/design/storage-v2.md`` §"Identity & naming"

Stability promise: the algorithms in this module are pinned. ``pub_id``
values appear in LaTeX cites, MCP responses, and external references;
changing the formula would silently invalidate every existing handle.
Add a new function or a new ADR + module rather than mutating these.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

# Surname truncation cap (matches ``precis.utils.slug``). Long enough
# to keep practically every academic surname intact, short enough to
# bound cite_key length.
_SURNAME_MAX = 30

# 27th paper by the same first author in the same year ⇒ overflow.
# All 26 lowercase letters consumed; bump to ``aa`` / ``ab`` is an
# explicit ADR change, not a silent extension.
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CiteKeyOverflow(ValueError):
    """Raised by :func:`make_cite_key` when the base form *and* every
    one-letter suffix ``a`` through ``z`` are taken.

    Attributes:
        base: The ``<surname><yy>`` prefix that overflowed.
        taken: The set of taken keys passed in.
    """

    def __init__(self, base: str, taken: set[str]) -> None:
        super().__init__(
            f"cite_key {base!r} and all 26 letter suffixes are taken; "
            "extend the algorithm or pick a different surname/year"
        )
        self.base = base
        self.taken = taken


# ---------------------------------------------------------------------------
# Private helpers (string folding + first-author extraction)
#
# The first-author logic mirrors ``precis.utils.slug._first_author`` but
# is deliberately duplicated here. Identity is foundational; pinning the
# algorithm in this module insulates ``cite_key`` values from any future
# refinement to slug minting.
# ---------------------------------------------------------------------------


def _ascii_fold(text: str) -> str:
    """NFKD-decompose, drop combining marks, drop non-ASCII bytes."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _surname_from_string(name: str) -> str:
    """Extract the surname from a free-text name string.

    Handles the four shapes we see in the wild:

    - ``"Smith, John"``  → ``smith``  (comma form: take the head)
    - ``"John Smith"``   → ``smith``  (Western order: take last word)
    - ``"Smith"``        → ``smith``  (mononym)
    - ``"A.Clark"`` / ``"A.B.Clark"``  → ``clark``  (glued initials)
    """
    first = (name or "").strip()
    if not first:
        return ""
    if "," in first:
        surname = first.split(",", 1)[0]
    else:
        parts = first.split()
        surname = parts[-1] if parts else ""
    folded = _ascii_fold(surname.lower())
    # "a.clark" → "clark"; "a.b.clark" → "clark". Detect a leading run
    # of one-letter dotted segments (initials) and drop them.
    if "." in folded:
        segments = folded.split(".")
        if (
            len(segments) >= 2
            and segments[-1]
            and all(len(s) == 1 and s.isalpha() for s in segments[:-1])
        ):
            folded = segments[-1]
    return re.sub(r"[^a-z]", "", folded)[:_SURNAME_MAX]


def _first_author_surname(authors: Any) -> str:
    """Return the first author's surname, lowercased ASCII letters only.

    Accepts:

    - ``list[str]``: ``["Smith, John", "Doe, Jane"]``
    - ``list[dict]``: ``[{"family": "Smith", "given": "John"}, …]``
      (CrossRef / Semantic Scholar shape)
    - any iterable yielding the above
    - ``None`` or empty input → ``""``

    Free-text bylines (a single string with comma-separated authors)
    are not handled here — split before calling.
    """
    if not authors:
        return ""
    try:
        first = next(iter(authors))
    except TypeError:
        return ""
    if isinstance(first, Mapping):
        family = first.get("family") or first.get("last") or first.get("name")
        if not family:
            return ""
        return _surname_from_string(str(family))
    if isinstance(first, str):
        return _surname_from_string(first)
    return ""


# ---------------------------------------------------------------------------
# DOI / arXiv normalisation
# ---------------------------------------------------------------------------


_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi.org/",
    "doi:",
)


def normalize_doi(s: str | None) -> str | None:
    """Strip URL / ``doi:`` prefixes and lowercase. Empty / None → None.

    DOIs are case-insensitive per the DOI Handbook §2.4. We canonicalise
    to lowercase so ``(id_kind='doi', id_value=…)`` PK collisions aren't
    invented by publisher case inconsistency.
    """
    if not s:
        return None
    raw = s.strip()
    if not raw:
        return None
    lower = raw.lower()
    for prefix in _DOI_PREFIXES:
        if lower.startswith(prefix):
            lower = lower[len(prefix) :]
            break
    lower = lower.lstrip("/")
    return lower or None


_ARXIV_PREFIXES = (
    "https://arxiv.org/abs/",
    "http://arxiv.org/abs/",
    "https://arxiv.org/pdf/",
    "http://arxiv.org/pdf/",
    "arxiv.org/abs/",
    "arxiv.org/pdf/",
    "arxiv:",
)
_ARXIV_VERSION_RE = re.compile(r"v\d+$")


def normalize_arxiv(s: str | None) -> str | None:
    """Strip URL / ``arXiv:`` prefixes and version suffix. Empty → None.

    Version stripping makes the resulting id stable across preprint
    revisions: ``2301.12345v3`` and ``2301.12345v1`` both → ``2301.12345``.

    Old-style ids (``cs.LG/0501001``) preserve their archive prefix
    case; new-style ids (``2301.12345``) are pure digits.
    """
    if not s:
        return None
    raw = s.strip()
    if not raw:
        return None
    # Lowercase only the prefix-matching portion, not the id itself
    # (old-style ``cs.LG`` is case-significant in some catalogues).
    head = raw.lower()
    for prefix in _ARXIV_PREFIXES:
        if head.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    raw = raw.lstrip("/")
    # Drop fragment / query (#abstract, ?context=…)
    for sep in ("#", "?"):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
    # Strip a trailing pdf extension that occasionally leaks in.
    if raw.lower().endswith(".pdf"):
        raw = raw[:-4]
    raw = _ARXIV_VERSION_RE.sub("", raw)
    return raw or None


# ---------------------------------------------------------------------------
# Hashes
# ---------------------------------------------------------------------------


def make_pdf_sha256(pdf_bytes: bytes) -> str:
    """Hex SHA-256 of the raw PDF file bytes. 64 chars."""
    return hashlib.sha256(pdf_bytes).hexdigest()


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text_for_hash(text: str | None) -> str:
    """Canonical form used by :func:`make_content_hash`.

    Steps: NFKD-fold, lowercase, collapse runs of whitespace into a
    single ASCII space, strip leading/trailing whitespace.

    Idempotent: ``f(f(x)) == f(x)``.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text)
    folded = folded.lower()
    folded = _WHITESPACE_RE.sub(" ", folded).strip()
    return folded


def make_content_hash(text: str | None) -> str:
    """Hex SHA-256 of :func:`normalize_text_for_hash`. 64 chars.

    Dedup key for "same paper, different bytes" — re-OCR and re-typeset
    versions of the same content fold to the same hash. Companion to
    :func:`make_pdf_sha256` which catches byte-equality.
    """
    canonical = normalize_text_for_hash(text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Primary identifiers
# ---------------------------------------------------------------------------


def make_paper_id(
    *,
    arxiv: str | None = None,
    doi: str | None = None,
    pdf_sha256: str | None = None,
) -> str:
    """Build the synthetic ``paper_id`` for a ref.

    Priority: ``arxiv`` > ``doi`` > ``sha256``. Inputs are normalised
    first (caller may pass URL or prefix forms); the chosen source
    decides which "kind:" prefix prefixes the result.

    Returns one of:

    - ``"arxiv:<id>"``   (e.g. ``"arxiv:2301.12345"``)
    - ``"doi:<id>"``     (e.g. ``"doi:10.1234/foo"``)
    - ``"sha256:<hex>"`` (e.g. ``"sha256:abc123…"``)

    Raises:
        ValueError: when all three kwargs are None / empty, i.e. the
            ref has no source identifier whatsoever.
    """
    a = normalize_arxiv(arxiv)
    if a:
        return f"arxiv:{a}"
    d = normalize_doi(doi)
    if d:
        return f"doi:{d}"
    s = (pdf_sha256 or "").strip().lower() or None
    if s:
        return f"sha256:{s}"
    raise ValueError("make_paper_id requires at least one of arxiv / doi / pdf_sha256")


def make_finding_paper_id(
    body_text: str,
    scope: Mapping[str, Any] | None,
    initial_cite_pub_id: str,
) -> str:
    """Build the synthetic ``paper_id`` for a finding ref.

    Findings have no external ID (no DOI, no arXiv, no PDF). Their
    identity is derived from the empirical content the agent created:
    the claim text plus its setup envelope plus the initial citation
    that anchored the chase. Same inputs → same ``paper_id`` → same
    ``pub_id`` (via :func:`make_pub_id`), which makes the skill-side
    "search before create" rule self-correcting: two agents recording
    the same finding under the same setup collide on insert at the
    UNIQUE constraint on ``ref_identifiers (id_kind='pub_id')``
    rather than spawning duplicate chases.

    Returns ``f"finding:{hex}"`` where ``hex`` is the SHA-256 of a
    canonical key derived from the three inputs. The ``finding:``
    prefix slots into the same name space as ``arxiv:``, ``doi:``,
    ``sha256:`` so :func:`make_pub_id` does not need to know what
    kind of ref it's minting for.

    Canonicalisation:

    - ``body_text``: :func:`normalize_text_for_hash` (NFKD-fold +
      lowercase + whitespace collapse). Idempotent under cosmetic
      re-edits ("2.4 kV" vs "2.4 kV" with extra spaces).
    - ``scope``: ``json.dumps`` with ``sort_keys=True`` and
      no-whitespace separators. ``None`` and ``{}`` collapse to the
      same canonical ``"{}"``.
    - ``initial_cite_pub_id``: stripped, lowercased.

    Args:
        body_text: The claim text the agent supplied
            (`finding_body` chunk). Required non-empty.
        scope: Optional structured slice of the setup envelope, e.g.
            ``{"electrode": "Cu", "ambient": "N2"}``. Empty / None
            allowed (some findings carry only prose context).
        initial_cite_pub_id: ``pub_id`` of the ref the agent cited
            as the starting frontier (the ``cited_in`` argument on
            ``put(kind='finding', …)``). Required non-empty — a
            finding without an initial cite is not a finding.

    Raises:
        ValueError: when ``body_text`` or ``initial_cite_pub_id`` is
            empty / whitespace-only.

    Example:
        >>> pid = make_finding_paper_id(
        ...     "2.4 kV held for 30 s on Si/SiO2 MOSCAPs",
        ...     {"electrode": "Cu", "ambient": "N2"},
        ...     "ab12c3",
        ... )
        >>> pid.startswith("finding:")
        True
        >>> len(pid) == len("finding:") + 64
        True
    """
    if not body_text or not body_text.strip():
        raise ValueError("make_finding_paper_id requires a non-empty body_text")
    if not initial_cite_pub_id or not initial_cite_pub_id.strip():
        raise ValueError(
            "make_finding_paper_id requires a non-empty initial_cite_pub_id"
        )
    body_canonical = normalize_text_for_hash(body_text)
    # Sort keys so {"a":1,"b":2} and {"b":2,"a":1} hash identically; no
    # whitespace so cosmetic JSON formatting changes don't churn the id.
    # dict(scope) coerces Mapping/None into a plain dict json can serialise.
    scope_canonical = json.dumps(
        dict(scope) if scope else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    cite_canonical = initial_cite_pub_id.strip().lower()
    key = f"finding|body={body_canonical}|scope={scope_canonical}|cite={cite_canonical}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"finding:{digest}"


def make_pub_id(paper_id: str) -> str:
    """6-character base32 lowercase handle derived from ``paper_id``.

    Formula (locked, ADR 0002 §"pub_id"):

    .. code-block:: python

        digest = sha256(paper_id.encode("utf-8"))
        pub_id = base32(digest)[:6].lower()

    Charset is ``[a-z2-7]``. Pinned at first ingest; deterministic.

    Raises:
        ValueError: when ``paper_id`` is empty.
    """
    if not paper_id:
        raise ValueError("make_pub_id requires a non-empty paper_id")
    digest = hashlib.sha256(paper_id.encode("utf-8")).digest()
    return base64.b32encode(digest)[:6].decode("ascii").lower()


def make_cite_key(
    authors: Iterable[Any] | None,
    year: int | None,
    *,
    taken: Iterable[str] = (),
) -> str:
    """Mint a LaTeX-friendly citation handle.

    Format: ``<surname><yy>[<letter>]`` — first-author surname, 2-digit
    year, optional one-letter collision suffix.

    Examples (with ``taken`` empty unless noted):

    .. code-block:: pycon

        >>> make_cite_key([{"family": "Miller"}], 2023)
        'miller23'
        >>> make_cite_key(["Miller, John"], 2023, taken={"miller23"})
        'miller23a'
        >>> make_cite_key(["Miller"], 2023, taken={"miller23", "miller23a"})
        'miller23b'
        >>> make_cite_key([], 2023)
        'anon23'
        >>> make_cite_key(["Miller"], None)
        'miller00'

    Args:
        authors: Iterable of strings or ``{"family", "given"}`` dicts.
            Only the first element matters.
        year: Integer year; ``None`` falls back to ``"00"``.
        taken: Set / iterable of cite_keys already in the corpus that
            share the prefix. Pass the result of
            ``SELECT id_value FROM ref_identifiers WHERE
            id_kind='cite_key' AND id_value LIKE :prefix || '%'``.

    Raises:
        CiteKeyOverflow: when the base form *and* every ``a``-``z``
            suffix are taken.
    """
    surname = _first_author_surname(authors) or "anon"
    yy = f"{year % 100:02d}" if year is not None else "00"
    base = f"{surname}{yy}"
    taken_set = set(taken)
    if base not in taken_set:
        return base
    for letter in _LETTERS:
        candidate = base + letter
        if candidate not in taken_set:
            return candidate
    raise CiteKeyOverflow(base, taken_set)


def make_node_id(paper_id: str, page: int | None, block_index: int) -> str:
    """8-character opaque handle for a chunk-like node within a paper.

    Stable across DB rebuilds: the same ``(paper_id, page, block_index)``
    triple always produces the same ``node_id``. Useful as a chunk
    handle that survives re-ingest renumbering of ``BIGSERIAL chunk_id``.

    Algorithm:

    .. code-block:: python

        key = f"{paper_id}:p{page}:b{block_index}"
        node_id = base32(sha256(key))[:8].lower()

    ``page = None`` is encoded literally as ``pNone`` so non-paginated
    refs (notes, code symbols) still get a stable id space.
    """
    if not paper_id:
        raise ValueError("make_node_id requires a non-empty paper_id")
    key = f"{paper_id}:p{page}:b{block_index}".encode()
    digest = hashlib.sha256(key).digest()
    return base64.b32encode(digest)[:8].decode("ascii").lower()


__all__ = [
    "CiteKeyOverflow",
    "make_cite_key",
    "make_content_hash",
    "make_finding_paper_id",
    "make_node_id",
    "make_paper_id",
    "make_pdf_sha256",
    "make_pub_id",
    "normalize_arxiv",
    "normalize_doi",
    "normalize_text_for_hash",
]
