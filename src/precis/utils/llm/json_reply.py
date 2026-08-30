"""Tolerant JSON-object parsing out of an LLM reply — shared by every cascade
that asks a model for structured JSON and must survive prose/fences around it.

Consolidates ~13 near-identical ``_extract_json`` copies (classify, axis_pass,
classify_topics, bib_parse, paper_glossary, reading/graph, reading/cards,
mail/inject, quest/claims, quest/tick, quest/weave, utils/llm/requirement,
asa_bot/preamble) onto one tolerant parse, unioning their tolerances: a
same-string ``json.loads`` fast path, then the last *balanced* top-level
``{...}``/``[...]`` block (handles surrounding prose, code fences, and a
reasoning model's trailing final-answer block — a naive first-``{``/last-``}``
slice breaks when the reply holds two separate JSON blocks). Two copies
(``workers/classify.py``, ``workers/stub_rank.py``) omitted the ``isinstance``
guard despite their ``-> dict | None`` annotation, letting a JSON-list reply
flow past the type contract — every path here is guarded.
"""

from __future__ import annotations

import json
from typing import Any


def _last_balanced_block(text: str, open_ch: str, close_ch: str) -> str | None:
    """The last complete, bracket-balanced ``open_ch``..``close_ch`` span in
    ``text``, or ``None``. Ignores fences/backticks (they aren't bracket
    chars) and correctly skips a prose aside that itself contains an
    unbalanced bracket, unlike a naive first-open/last-close slice."""
    depth = 0
    start = -1
    candidate: str | None = None
    for i, ch in enumerate(text):
        if ch == open_ch:
            if depth == 0:
                start = i
            depth += 1
        elif ch == close_ch:
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start : i + 1]
    return candidate


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse ``text`` as a JSON object, tolerating surrounding prose/fences.

    Tries the whole string first (the common well-behaved reply), then falls
    back to the last balanced ``{...}`` block. ``None`` on no text, no
    parseable block, or a parse that yields something other than a dict
    (a JSON list, string, or number is never mistaken for the requested
    object).
    """
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    block = _last_balanced_block(text, "{", "}")
    if block is None:
        return None
    try:
        obj = json.loads(block)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def extract_json_array(text: str) -> list[Any] | None:
    """Parse ``text`` as a JSON array, tolerating surrounding prose/fences.

    The array-shaped sibling of :func:`extract_json_object` (same tolerant
    parse, ``[``/``]`` instead of ``{``/``}``) — for the one call site whose
    LLM payload is a JSON list rather than an object
    (``precis.quest.claims``).
    """
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
    except Exception:
        pass
    block = _last_balanced_block(text, "[", "]")
    if block is None:
        return None
    try:
        obj = json.loads(block)
    except Exception:
        return None
    return obj if isinstance(obj, list) else None


__all__ = ["extract_json_array", "extract_json_object"]
