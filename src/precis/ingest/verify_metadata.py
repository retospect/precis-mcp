"""Metadata verification against PDF text."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

from precis.ingest.literature import surname_from_name

# Unicode dashes / hyphens that should all be treated as ASCII hyphen-minus
_DASH_RE = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uFE58\uFE63\uFF0D]")
# HTML tags (e.g. <sub>, <sup>, <i>) sometimes present in S2 / CrossRef titles
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Greek letters → closest Latin equivalent. Applied only during normalisation
# for comparison; original text is preserved in the stored header.
# Example: CrossRef title "High-κ dielectrics" vs PDF body "High-k dielectrics"
# scores 56% with partial_ratio (the κ/k mismatch is catastrophic for fuzzy
# alignment), but 100% after this fold.
_GREEK_FOLD = str.maketrans(
    {
        "α": "a",
        "β": "b",
        "γ": "g",
        "δ": "d",
        "ε": "e",
        "ζ": "z",
        "η": "h",
        "θ": "th",
        "ι": "i",
        "κ": "k",
        "λ": "l",
        "μ": "u",
        "ν": "n",
        "ξ": "x",
        "ο": "o",
        "π": "p",
        "ρ": "r",
        "σ": "s",
        "ς": "s",
        "τ": "t",
        "υ": "y",
        "φ": "f",
        "χ": "ch",
        "ψ": "ps",
        "ω": "o",
        "Α": "A",
        "Β": "B",
        "Γ": "G",
        "Δ": "D",
        "Ε": "E",
        "Ζ": "Z",
        "Η": "H",
        "Θ": "Th",
        "Ι": "I",
        "Κ": "K",
        "Λ": "L",
        "Μ": "M",
        "Ν": "N",
        "Ξ": "X",
        "Ο": "O",
        "Π": "P",
        "Ρ": "R",
        "Σ": "S",
        "Τ": "T",
        "Υ": "Y",
        "Φ": "F",
        "Χ": "Ch",
        "Ψ": "Ps",
        "Ω": "O",
    }
)

# Common English stopwords — excluded from word-overlap scoring so it
# focuses on distinctive content words.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "by",
        "with",
        "and",
        "or",
        "but",
        "not",
        "from",
        "into",
        "onto",
        "upon",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "its",
        "it",
        "via",
        "using",
        "based",
        "new",
        "novel",
        "study",
        "studies",
        "review",
    }
)

# Prefixed-DOI patterns recognised as "this paper's DOI" typographic markers.
# Bare DOIs in reference lists are deliberately NOT matched — only forms the
# publisher typesets to identify this specific article.
_DOI_PREFIX_TOKENS = (
    r"\bdoi[:\s]+",
    r"(?:https?://)?(?:dx\.)?doi\.org/",
    r"\bDOI\s+",
)


def _normalize(text: str) -> str:
    """Normalize text for fuzzy comparison.

    - Strip HTML tags (e.g. ``<sub>`` from S2/CrossRef titles)
    - NFKC unicode normalization (folds ligatures, sub/superscripts)
    - Fold Greek letters to Latin equivalents (κ→k, β→b, etc.)
    - Fold all dash/hyphen variants to ASCII hyphen-minus
    - Rejoin chemical formulas split across whitespace (``CO 2`` → ``CO2``)
    - Collapse whitespace, lowercase
    """
    text = _HTML_TAG_RE.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_GREEK_FOLD)
    text = _DASH_RE.sub("-", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"([A-Za-z])\s(\d)", r"\1\2", text)
    return text.lower()


def _title_score(title: str, text: str) -> float:
    """Best partial_ratio of title against text, trying subtitle variants."""
    score = fuzz.partial_ratio(title, text)
    # If full title didn't match well, try the main title (before : or —)
    for sep in (":", " - ", " — ", " – "):
        if sep in title:
            main = title.split(sep, 1)[0].strip()
            if len(main) >= 10:
                score = max(score, fuzz.partial_ratio(main, text))
    return score


def _word_overlap_score(norm_title: str, norm_text: str) -> float:
    """Percentage of normalized title content-words present in normalized text.

    Content words = ≥4 chars, not a stopword. Matches by substring so
    pluralisation (``correction`` ↔ ``corrections``) and prefix variants
    (``YBa`` ↔ ``YBa2Cu3O7``) still count.

    Forgiving to reorderings, column-break line wrapping, and character-
    boundary artefacts from PDF text extraction that sink ``partial_ratio``.
    Both arguments must already be normalized via :func:`_normalize`.
    """
    title_words = {w for w in re.findall(r"\w{4,}", norm_title) if w not in _STOPWORDS}
    if not title_words:
        return 0.0
    hits = sum(1 for w in title_words if w in norm_text)
    return 100.0 * hits / len(title_words)


def _doi_prefix_in_text(doi: str, text: str) -> bool:
    """True if a prefixed form of *doi* appears in *text*.

    A prefixed DOI (``doi:X``, ``https://doi.org/X``, ``DOI: X``) is the
    publisher's own typeset marker saying "this article's DOI is X". If it
    matches the header DOI, that's strong confirmation we have the right
    paper — stronger than title fuzzing, which fails on partial-page
    extracts, scanned reprints, or multi-column layouts pymupdf reads in
    the wrong order.
    """
    if not doi or not text:
        return False
    doi_escaped = re.escape(doi.rstrip(".,;:"))
    pattern = re.compile(
        f"(?:{'|'.join(_DOI_PREFIX_TOKENS)}){doi_escaped}",
        re.IGNORECASE,
    )
    return bool(pattern.search(text))


def verify_metadata(
    header: dict,
    first_pages_text: str,
    threshold: int = 80,
    word_overlap_threshold: int = 60,
) -> tuple[bool, list[str]]:
    """Verify looked-up metadata against PDF text.

    Title passes if ANY of:

    - ``partial_ratio`` ≥ ``threshold`` (classic fuzzy match), OR
    - word-overlap ≥ ``word_overlap_threshold`` (tolerant of reordering
      and extraction artefacts that break partial_ratio), OR
    - ``header['doi']`` appears in the body text with a ``doi:`` /
      ``doi.org/`` / ``DOI:`` prefix (publisher's own typeset DOI marker,
      authoritative — used when the title block itself isn't in the
      extractable first pages, e.g. partial-page reprints).

    Authors pass if at least one surname fuzz-matches the body.

    Args:
        header: Metadata dict with ``title``, ``authors``, optional ``doi``.
        first_pages_text: Text from the first pages of the PDF.
        threshold: Minimum ``partial_ratio`` for title/author (0-100).
        word_overlap_threshold: Minimum word-overlap for title (0-100).

    Returns:
        Tuple of (verified, warnings).
    """
    warnings: list[str] = []
    norm_text = _normalize(first_pages_text)

    title = header.get("title", "")
    if title:
        norm_title = _normalize(title)
        partial = _title_score(norm_title, norm_text)
        overlap = _word_overlap_score(norm_title, norm_text)
        doi_confirmed = _doi_prefix_in_text(header.get("doi") or "", first_pages_text)
        title_ok = (
            partial >= threshold or overlap >= word_overlap_threshold or doi_confirmed
        )
        if not title_ok:
            warnings.append(
                f"Title mismatch: '{title[:60]}...' "
                f"partial={partial:.0f} overlap={overlap:.0f} doi_in_text={doi_confirmed}"
            )

    authors = header.get("authors", [])
    if authors:
        author_pass = 0
        author_checked = 0
        author_warnings: list[str] = []
        for author in authors:
            surname = surname_from_name(author.get("name", ""))
            if surname:
                author_checked += 1
                norm_surname = _normalize(surname)
                score = fuzz.partial_ratio(norm_surname, norm_text)
                # Short surnames (≤4 chars) are inherently noisy with partial_ratio
                effective_threshold = 60 if len(norm_surname) <= 4 else threshold
                if score >= effective_threshold:
                    author_pass += 1
                else:
                    author_warnings.append(
                        f"Author surname '{surname}' scored {score} < {effective_threshold}"
                    )
        # Fail only if we checked authors and NONE matched
        if author_checked > 0 and author_pass == 0:
            warnings.extend(author_warnings)

    verified = len(warnings) == 0
    return verified, warnings
