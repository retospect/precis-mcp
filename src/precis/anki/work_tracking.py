"""Predicate for work-tracking material that must never become an anki card.

A to-do or gap about our own system is not a fact (precis-cloze rule 0). Both
mint paths — `AnkiHandler._create` (agent `put`) and the `card_forge` job —
share these checks so they cannot drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# A named scientific gap (e.g. a miscibility gap) is valid; only the
# work-item phrasing is refused.
WORK_ITEM_RE = re.compile(
    r"\b(?:gap\s+C\d+\b|we\s+lack\b|our\s+spec\s+(?:has|names)\s+no\b"
    r"|missing\s+is\b)",
    re.IGNORECASE,
)
WORK_SOURCE_TAGS = frozenset({"space:repo-dev", "section:threads"})
WORK_SOURCE_TITLE_RE = re.compile(r"\b(?:gap[ -]list|backlog)\b", re.IGNORECASE)


def has_work_source_tag(tags: Iterable[object]) -> bool:
    """True when any tag marks a work-tracking space or section."""
    return bool(WORK_SOURCE_TAGS.intersection(str(t).strip().lower() for t in tags))


def is_work_title(title: str) -> bool:
    """True for a gap/backlog list title or work-item wording."""
    return bool(WORK_ITEM_RE.search(title) or WORK_SOURCE_TITLE_RE.search(title))


def is_work_item_text(text: str) -> bool:
    """True when ``text`` is phrased as a to-do/gap about our own system."""
    return bool(WORK_ITEM_RE.search(text))
