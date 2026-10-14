"""Lexical similarity over card texts — the no-embedder similarity leg
shared by taxon dedup refusals and gardener candidates."""

from __future__ import annotations

import re

__all__ = ["lexical_overlap"]

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "with",
        "which",
        "who",
        "whose",
        "this",
        "these",
        "those",
        "than",
        "then",
        "such",
        "per",
        "each",
        "any",
        "not",
        "no",
        "aka",
        "also",
        "can",
        "may",
    ]
)


def _content_words(text: str) -> frozenset[str]:
    return frozenset(
        w for w in _WORD.findall((text or "").lower()) if len(w) > 2 and w not in _STOP
    )


def lexical_overlap(a: str, b: str) -> float:
    """Jaccard overlap of the content words of two card texts (0..1) — the
    shared no-embedder similarity leg (dedup refusals, gardener candidates)."""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)
