"""Title-match merge gate shared by ingest dedup and the store (gr264748).

Lives in ``utils`` because ``Store.upsert_stub_paper`` needs it and the store
may not import ``precis.ingest``. ``content_tokens`` is the shared title
tokenizer (``ingest.verify_metadata`` re-exports it).
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

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


def content_tokens(text: str) -> frozenset[str]:
    """Lowercase alphanumeric tokens (≥4 chars), stopwords dropped.

    The shared tokenizer for :func:`title_overlap` (gr353804) — deliberately
    a plainer cut than :func:`_word_overlap_score`'s substring matching:
    that function tolerates pluralisation/prefix variants on purpose (a
    *real* paper's title vs its own PDF text), while ``title_overlap``
    exists to catch the opposite case — a registry hit with NO genuine
    relationship to the PDF at all — where exact-token intersection is
    the cleaner signal (no risk of a short substring like "the" tucked
    inside an unrelated word inflating the count; ``\\w`` already excludes
    that here since stopwords are filtered post-tokenization, not via
    substring).
    """
    return frozenset(
        w for w in re.findall(r"[a-z0-9]{4,}", text.lower()) if w not in _STOPWORDS
    )


def _author_list(authors: Any) -> list[Any]:
    """``authors`` as a list: a list, a JSON-encoded list, else ``[]``."""
    if isinstance(authors, list):
        return authors
    if isinstance(authors, str) and authors:
        try:
            parsed = json.loads(authors)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _surname(name: str) -> str:
    """Surname of one name string: ``"Last, First"`` or ``"First Last"``."""
    name = name.strip()
    if "," in name:
        return name.split(",", 1)[0].strip()
    parts = name.split()
    return parts[-1] if parts else ""


#: Minimum distinctive title tokens an automatic title merge needs: a short
#: title ("Defects in Carbon Nanotubes") is shared by unrelated papers.
_MIN_TITLE_TOKENS = 5


def _first_family(authors: Any) -> str:
    """Comparable family name of the first author ('' when none usable).

    Accent-folded (NFKD), lowercased, punctuation dropped, and only the last
    token kept, so ``van der Berg`` matches ``Berg`` and ``Müller`` matches
    ``Muller``.
    """
    items = _author_list(authors)
    if not items:
        return ""
    first = items[0]
    if isinstance(first, dict):
        fam = str(first.get("family") or "").strip()
        if not fam:
            fam = _surname(str(first.get("name") or ""))
    else:
        fam = _surname(str(first))
    fam = unicodedata.normalize("NFKD", fam)
    fam = "".join(ch for ch in fam if not unicodedata.combining(ch)).lower()
    toks = re.sub(r"[^a-z0-9\s]", " ", fam).split()
    return toks[-1] if toks else ""


def title_merge_refusal(
    stub_title: str | None,
    stub_year: int | None,
    stub_authors: Any,
    held_title: str | None,
    held_year: int | None,
    held_authors: Any,
    *,
    sim: float | None = None,
    unknown_year_min_sim: float | None = None,
) -> str | None:
    """Why a high-similarity title match must NOT auto-merge (``None`` = ok).

    A title match alone is a weak signal (gr264748: namesakes, short generic
    titles). An automatic merge needs all of: >=5 distinctive title tokens
    (:func:`content_tokens`) on both sides, a
    known year on BOTH sides within +/-1 (preprint vs published; a NULL year
    never passes), and agreeing first-author family names whenever both sides
    carry authors. Shared by ``ingest.dedup.reconcile_by_title_similarity`` and
    ``Store.upsert_stub_paper``.

    ``unknown_year_min_sim`` (with ``sim``) relaxes only the unknown-year
    rule: a missing year on either side passes when ``sim`` reaches it. The
    mint-time guard uses this so a year-less title-only re-acquire of a held
    paper stays idempotent; the batch reconcile leaves it unset, so a
    year-less match goes to review. A known year mismatch always refuses.
    """
    if (
        len(content_tokens(stub_title or "")) < _MIN_TITLE_TOKENS
        or len(content_tokens(held_title or "")) < _MIN_TITLE_TOKENS
    ):
        return "short-title"
    if stub_year is None or held_year is None:
        if not (
            unknown_year_min_sim is not None
            and sim is not None
            and sim >= unknown_year_min_sim
        ):
            return "year-unknown"
    elif abs(int(stub_year) - int(held_year)) > 1:
        return "year-mismatch"
    a, b = _first_family(stub_authors), _first_family(held_authors)
    if a and b and a != b:
        return "author-mismatch"
    return None
