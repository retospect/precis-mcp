"""Shared literature helpers: author parsing + the SKIP_EMBED_TYPES set.

Canonical home for primitives used by ``acatome-extract``, ``acatome-store``,
and ``precis-mcp`` — eliminates duplication across packages.

Public API:
  * :data:`SKIP_EMBED_TYPES` — block types that should not be embedded.
  * :func:`first_author_key` — raw citation-key chunk for slug fingerprinting.
  * :func:`first_author_surname` — display-friendly surname.

The legacy ``make_slug`` helper that lived here was removed during B3a
of the v2 storage rewrite (per ADR 0008). For human-readable citation
handles, call :func:`precis.identity.make_cite_key` instead.

A ``build_embedder`` factory used to live here too — chroma + sentence-
transformers wrappers — but it was only ever called by its own tests.
Production code uses :class:`precis.embedder.BgeM3Embedder` directly.
Removed 2026-06-05.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

# ---------------------------------------------------------------------------
# Block-type filter (shared between extract's enrichment and store's re-embed)
# ---------------------------------------------------------------------------

SKIP_EMBED_TYPES: frozenset[str] = frozenset(
    {"section_header", "title", "author", "equation", "junk"}
)
"""Block types that are skipped when computing or re-computing embeddings.

These block types either carry no semantic payload (``junk``), are structural
markers (``section_header``, ``title``, ``author``), or are formulas whose
LaTeX/MathML content does not embed well with text models (``equation``).
"""


# ---------------------------------------------------------------------------
# Author-name parsing helpers
# ---------------------------------------------------------------------------


def _coerce_authors(authors: Any) -> list[Any]:
    """Normalise ``authors`` into a list.

    Accepts:
      * a ``list`` (returned unchanged),
      * a JSON-encoded string of a list (decoded),
      * ``None`` or anything else (coerced to ``[]``).

    Never raises — invalid input yields an empty list.
    """
    if isinstance(authors, list):
        return authors
    if isinstance(authors, str) and authors:
        try:
            parsed = json.loads(authors)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _first_name_field(authors: Any) -> str:
    """Return the raw ``name`` field of the first author, stripped.

    Handles list-of-dicts, list-of-strings, JSON strings, and empty/missing
    inputs. Splits semicolon-packed multi-author strings (``"A; B; C"``) and
    returns the first chunk.
    """
    items = _coerce_authors(authors)
    if not items:
        return ""
    first = items[0]
    if isinstance(first, dict):
        name = first.get("name", "")
    else:
        name = str(first)
    if ";" in name:
        name = name.split(";", 1)[0]
    return name.strip()


def first_author_key(authors: Any) -> str:
    """Return the slug-fingerprint chunk for the first author.

    This is the substring *before the first comma* of the first author's name.
    Used by :func:`make_slug` to build deterministic citation keys.

    Examples:
      * ``"Smith, John"`` → ``"Smith"``
      * ``"Daniel S. Levine"`` → ``"Daniel S. Levine"``
      * ``"Daniel S. Levine; Nicholas Liesen"`` → ``"Daniel S. Levine"``
      * ``[]`` / ``None`` / malformed → ``""``
    """
    name = _first_name_field(authors)
    if not name:
        return ""
    return name.split(",", 1)[0].strip()


def surname_from_name(name: str) -> str:
    """Extract the display surname from a single author name string.

    Understands both "Last, First" and "First Last" conventions:
      * ``"Smith, John"`` → ``"Smith"``
      * ``"John Smith"`` → ``"Smith"``
      * ``"Daniel S. Levine"`` → ``"Levine"``

    Returns ``""`` for empty input. Preserves case and diacritics.
    """
    if not name:
        return ""
    name = name.strip()
    if not name:
        return ""
    if "," in name:
        return name.split(",", 1)[0].strip()
    parts = name.split()
    return parts[-1] if parts else ""


def first_author_surname(authors: Any) -> str:
    """Return a display-friendly surname for the first author.

    See :func:`surname_from_name` for the parsing rules; this helper simply
    picks the first author out of a list/JSON/etc.

    Returns ``""`` when no usable author is present.
    """
    return surname_from_name(_first_name_field(authors))


# Letters that are NOT NFKD-decomposable but have well-established ASCII
# replacements in citation keys. NFKD alone would silently drop these,
# producing slugs like ``nrskov2009towards`` (Nørskov) or ``mller2024quantum``
# (Müller-Plathe — never happens, but the same drop). We fold them
# explicitly so surname-only slugs are stable across sources.
_ASCII_FALLBACKS = str.maketrans(
    {
        "ø": "o",
        "Ø": "O",
        "æ": "ae",
        "Æ": "AE",
        "œ": "oe",
        "Œ": "OE",
        "ß": "ss",
        "ł": "l",
        "Ł": "L",
        "ð": "d",
        "Ð": "D",
        "þ": "th",
        "Þ": "Th",
    }
)


def _ascii_fold(text: str) -> str:
    """NFKD-normalise and drop non-ASCII characters.

    Also folds Scandinavian / Eastern-European letters that NFKD does not
    decompose (``ø``, ``æ``, ``ß``, ``ł`` etc.) to ASCII equivalents so the
    citation key stays useful — without this, ``Nørskov`` would slug as
    ``nrskov`` rather than ``norskov``.
    """
    return (
        unicodedata.normalize("NFKD", text.translate(_ASCII_FALLBACKS))
        .encode("ascii", "ignore")
        .decode()
    )


# NOTE: ``make_slug`` was removed during B3a per ADR 0008
# (drop slug; identifiers normalised into ref_identifiers).
# The replacement is ``precis.identity.make_cite_key`` (per ADR 0006),
# which is a different algorithm — ``miller23a`` style rather than
# ``miller2023dopamine``. Call sites have switched accordingly.


__all__ = [
    "SKIP_EMBED_TYPES",
    "first_author_key",
    "first_author_surname",
    "surname_from_name",
]
