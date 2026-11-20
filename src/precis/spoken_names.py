"""Derived descriptions for voice; these are search cues, never stored aliases.

Descriptions can collide or change with the source, so callers retain the
exact handle in the transcript and present resolver candidates for selection.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "is",
        "are",
    ]
)


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[^\W_]+", text.lower()) if w not in _STOPWORDS]


def spoken_name(kind: str, summary: str, meta: Mapping[str, Any]) -> str:
    """Return a short description, tolerating missing bibliographic fields."""
    if kind != "paper":
        return " ".join(_words(summary)[:4]) or kind
    author = meta.get("authors") or meta.get("author") or ""
    if isinstance(author, list):
        author = author[0] if author else ""
    if isinstance(author, dict):
        author = author.get("family") or author.get("name") or ""
    author = " ".join(str(author).split(";")[0].split())
    surname = (
        author.split(",")[0] if "," in author else author.split()[-1] if author else ""
    )
    topic = _words(str(meta.get("title") or summary))[:1]
    parts = [surname, " ".join(str(meta.get("year") or "").split()), *topic]
    return " ".join(p for p in parts if p) or "paper"
