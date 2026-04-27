"""Slug minting for paper refs.

Pattern: ``<surname><year><content-word>`` — ASCII-only, lowercase, no
separators. ``wang2020state``, ``kim2024electrocatalytic``.

Collision handling: if the base slug is already taken, append ``-2``,
``-3``, ... until a free one is found. Probes through a caller-supplied
``existing(slug) -> bool`` predicate so the same logic runs in tests
(in-memory set) and against the live DB.

Pure logic; no DB, no I/O.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable

# Stopwords skipped when picking the first content word from a title.
# Kept small and conservative — anything ambiguous (e.g. ``via``) is
# allowed through so papers titled "Via X" still slug as "via", not as
# the second word.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "on",
        "in",
        "and",
        "or",
        "for",
        "with",
        "to",
        "by",
        "is",
        "are",
        "from",
        "into",
        "as",
        "at",
        "new",
    }
)

_SURNAME_MAX = 30
_KEYWORD_MAX = 20


def _ascii_fold(text: str) -> str:
    """Strip diacritics and drop non-ASCII bytes."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _first_author(authors: list[str]) -> str:
    """Return the first author's surname chunk, lowercased ASCII letters.

    Accepts ``"Smith, John"``, ``"John Smith"``, ``"Smith"`` shapes.
    Empty input → ``""``.
    """
    if not authors:
        return ""
    first = (authors[0] or "").strip()
    if not first:
        return ""
    # Comma form: "Last, First"
    if "," in first:
        surname = first.split(",", 1)[0]
    else:
        # Space-separated: take last token
        parts = first.split()
        surname = parts[-1] if parts else ""
    folded = _ascii_fold(surname.lower())
    return re.sub(r"[^a-z]", "", folded)[:_SURNAME_MAX]


def _content_word(title: str) -> str:
    """Return the first non-stopword content word of `title`.

    Falls back to a short SHA-256 hash of the original title when the
    title contains no Latin letters. Empty title → ``"untitled"``.
    """
    folded = _ascii_fold(title or "").lower()
    words = re.findall(r"[a-z]+", folded)
    if words:
        for w in words:
            if w not in _STOPWORDS:
                return w[:_KEYWORD_MAX]
        # All words are stopwords — keep the first.
        return words[0][:_KEYWORD_MAX]
    if title.strip():
        return hashlib.sha256(title.encode("utf-8")).hexdigest()[:6]
    return "untitled"


def mint_slug(
    *,
    authors: list[str],
    year: int | None,
    title: str,
    existing: Callable[[str], bool] | None = None,
) -> str:
    """Mint a deterministic ``<surname><year><word>`` slug.

    Args:
        authors: First-author surname is taken from authors[0].
        year:    Falls back to ``"0000"`` when None.
        title:   First content word is picked, stopwords skipped.
        existing: Optional predicate. If provided and returns True for
                 the candidate, append ``-2``, ``-3`` … until free.

    Returns:
        The minted slug — guaranteed free if ``existing`` is provided.
    """
    surname = _first_author(authors) or "anon"
    yr = str(year) if year is not None else "0000"
    word = _content_word(title)

    base = f"{surname}{yr}{word}"
    if existing is None or not existing(base):
        return base

    # Collision: add a numeric suffix. Cap at a sane maximum to fail
    # loudly rather than spin forever on a buggy `existing` callable.
    for n in range(2, 1000):
        candidate = f"{base}-{n}"
        if not existing(candidate):
            return candidate
    raise RuntimeError(
        f"slug minting exceeded 1000 collisions for base={base!r} — "
        "is the existence predicate buggy?"
    )


def slug_from_text(text: str, *, max_len: int = 60) -> str:
    """Derive a readable slug from arbitrary text.

    Lowercase, ASCII-fold diacritics, replace runs of non-alphanumeric
    characters with hyphens, trim leading/trailing hyphens, cap at
    ``max_len`` characters. Returns an empty string if nothing usable
    survives — caller decides on the fallback.

    Used by cache-backed handlers (perplexity, web bookmarks) to derive
    a human-meaningful slug from a query / URL / title without going
    through the full author+year+word minter.
    """
    folded = _ascii_fold(text or "").lower()
    # Replace any run of non-[a-z0-9] with a single hyphen.
    s = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    if not s:
        return ""
    return s[:max_len].rstrip("-")


__all__ = ["mint_slug", "slug_from_text"]
