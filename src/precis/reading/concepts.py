"""Shared concept-node logic (reading-prep loop, slice 2).

The `meta` shape, the embeddable card text, and name/definition parsing — used by
both the concept handler (manual put) and the promotion pass (rich write) so they
build byte-identical nodes. Pure (no DB, no I/O). See
docs/design/reading-prep-loop.md.
"""

from __future__ import annotations

import re
from typing import Any

MASTERY_INIT = 0.0
STATE_CANDIDATE = "candidate"
STATE_ACTIVE = "active"
STATE_MASTERED = "mastered"

#: Separator between a concept name and its definition, in priority order:
#: em-dash, en-dash, or a newline. A colon is deliberately NOT a separator —
#: definitions frequently contain them ("REST: an architectural style…").
_NAME_DEF_RE = re.compile(r"\s*[—–]\s*|\n")


def normalize_name(name: str) -> str:
    """Canonical form for name-anchored dedup: lowercased, whitespace-collapsed.
    Stored as ``meta.norm_name`` so promotion matches on it (an SQL btrim/lower
    can't collapse *internal* whitespace, so we normalize once at write time)."""
    return " ".join((name or "").split()).lower()


def split_name_def(text: str) -> tuple[str, str]:
    """Parse concept ``text`` into ``(name, definition)``. Splits on the first
    em/en-dash or newline; with no separator the whole string is the name and
    the definition is empty."""
    if not text:
        return "", ""
    parts = _NAME_DEF_RE.split(text.strip(), maxsplit=1)
    name = parts[0].strip()
    definition = parts[1].strip() if len(parts) > 1 else ""
    return name, definition


def concept_card_text(
    name: str, definition: str, aliases: list[str] | None = None
) -> str:
    """The embeddable ``card_combined`` text for a concept — name + definition
    (+ aliases) so the node is a rich vector in the corpus manifold (per the
    greenlit micro-decision: definition + aliases, kept short)."""
    out = name.strip()
    if definition.strip():
        out += f" — {definition.strip()}"
    cleaned = [a.strip() for a in (aliases or []) if a.strip()]
    if cleaned:
        out += f" (aka {', '.join(cleaned)})"
    return out.strip()


def initial_concept_meta(
    name: str,
    definition: str,
    *,
    aliases: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``meta`` stamped on a new concept ref: the mastery field (starts
    ``candidate`` / 0.0) + the canonical name / definition / aliases. ``extra``
    lets the promotion pass stamp provenance (e.g. source glossary version)."""
    meta: dict[str, Any] = {
        "name": name.strip(),
        "norm_name": normalize_name(name),
        "definition": definition.strip(),
        "aliases": [a.strip() for a in (aliases or []) if a.strip()],
        "mastery": MASTERY_INIT,
        "mastery_updated_at": None,
        "state": STATE_CANDIDATE,
    }
    if extra:
        meta.update(extra)
    return meta


__all__ = [
    "MASTERY_INIT",
    "STATE_ACTIVE",
    "STATE_CANDIDATE",
    "STATE_MASTERED",
    "concept_card_text",
    "initial_concept_meta",
    "normalize_name",
    "split_name_def",
]
