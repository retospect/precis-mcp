"""Tolerant JSON-object parsing out of an LLM reply — shared by every cascade
that asks a model for structured JSON and must survive prose/fences around it.

Consolidates ~13 near-identical ``_extract_json`` copies (classify, axis_pass,
classify_topics, bib_parse, paper_glossary, reading/graph, reading/flashcards,
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
import logging
from typing import Any

log = logging.getLogger(__name__)

#: Cap on stray-closer deletions per candidate (:func:`_repair_stray_closers`).
_MAX_STRAY_CLOSER_REPAIRS = 2


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


def _repair_stray_closers(candidate: str) -> dict[str, Any] | None:
    """Parse ``candidate`` after deleting up to :data:`_MAX_STRAY_CLOSER_REPAIRS`
    proven-unmatched closers, or ``None``.

    Fires only on the decoder's ``Expecting ',' delimiter`` error when the
    offending character is a ``]`` or ``}``: the parser has just finished a
    value inside an open container and met a closer of the WRONG type, so
    that character matches no opener — deleting it cannot drop content. Any
    other error (truncation leaves ``Expecting value`` / ``Unterminated
    string``) still fails. Each deletion logs a warning so the repair rate
    stays measurable (gr345366 cause B: a stray ``]`` after ``dossier_text``
    in 17 of 17 checked quest-tick failures).
    """
    for _ in range(_MAX_STRAY_CLOSER_REPAIRS):
        try:
            obj = json.loads(candidate, strict=False)
        except json.JSONDecodeError as exc:
            pos = exc.pos
            if not (
                exc.msg == "Expecting ',' delimiter"
                and 0 <= pos < len(candidate)
                and candidate[pos] in "]}"
            ):
                return None
            log.warning(
                "json_reply: dropped stray %r at offset %d (context %r)",
                candidate[pos],
                pos,
                candidate[max(0, pos - 40) : pos + 40],
            )
            candidate = candidate[:pos] + candidate[pos + 1 :]
            continue
        return obj if isinstance(obj, dict) else None
    try:
        obj = json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def extract_json_object(
    text: str, *, repair_stray_closers: bool = False
) -> dict[str, Any] | None:
    """Parse ``text`` as a JSON object, tolerating surrounding prose/fences.

    Tries the whole string first (the common well-behaved reply), then falls
    back to the last balanced ``{...}`` block — both in *strict* mode first.
    A model reply that's otherwise well-formed JSON but carries a raw
    (unescaped) control character inside a string value — a literal
    newline instead of ``\\n``, seen in prod quest-tick output (gr345336) —
    gets a SECOND chance at both candidates with ``json.loads(...,
    strict=False)``, which permits exactly that and nothing more: it is
    NOT a bracket-repair heuristic, so a truncated/unbalanced candidate (no
    balanced block to begin with) or one with genuinely invalid syntax (a
    stray unmatched ``]`` after a string field, also seen in prod) still
    fails and returns ``None`` — loosening either of those would silently
    launder a truncated generation into a "successful" parse instead of
    surfacing the real fault. ``None`` on no text, no parseable block, or a
    parse that yields something other than a dict (a JSON list, string, or
    number is never mistaken for the requested object).

    ``repair_stray_closers=True`` adds a third pass, after both of the above
    fail: :func:`_repair_stray_closers` on each candidate, which drops at
    most two closers the decoder has proven unmatched and logs each drop.
    Opt-in per caller; only the quest tick uses it (Reto, 2026-10-02,
    review item chemistry-1). Every other caller stays strict.
    """
    if not text:
        return None
    block = _last_balanced_block(text, "{", "}")
    candidates = [c for c in (text, block) if c is not None]
    # Pass 1 (strict): the whole string, then the last balanced block —
    # same order/behavior as before this fix. Pass 2 (strict=False) only
    # runs if pass 1 found nothing, retrying the SAME candidates — no new
    # candidate is invented, so a candidate with no balanced block at all
    # (truncated/unbalanced input) never gets a second chance it didn't
    # already have.
    for strict in (True, False):
        for candidate in candidates:
            try:
                obj = json.loads(candidate, strict=strict)
            except Exception:
                continue
            if isinstance(obj, dict):
                return obj
    if repair_stray_closers:
        for candidate in candidates:
            repaired = _repair_stray_closers(candidate)
            if repaired is not None:
                return repaired
    return None


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
