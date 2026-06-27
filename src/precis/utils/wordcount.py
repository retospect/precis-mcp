"""Word counting for draft documents (ADR: proposal writing).

A proposal section usually carries a word limit (min/max) imposed by the
call-for-proposal. The planner stamps that limit on the section heading
as ``meta.word_target = {"min": …, "max": …}`` and then needs a cheap
"am I over/under?" check it can call mid-write. This module is the pure,
DB-free core of that check:

* :func:`count_words` — count visible prose words in one chunk's text,
  stripping the inline reference markers drafts use (``[¶…]`` / ``[§…]``
  / ``[dc…]`` cross-refs and ``[surface](¶term)`` term links) so a
  citation-dense paragraph isn't inflated by its handles.
* :func:`aggregate_word_counts` — bucket per-chunk counts under each
  enclosing heading (a section's count includes its subsections) and
  render an over/under/ok verdict against each heading's word target.

Kept independent of the store so it can be unit-tested on any object
exposing ``chunk_id`` / ``parent_chunk_id`` / ``chunk_kind`` / ``text`` /
``meta`` (e.g. a :class:`~precis.store._draft_ops.DraftChunk`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

#: Chunk kinds that contribute prose to the word count. Headings (titles),
#: equations, figures/images, tables, code listings, and glossary terms
#: are structural / markup / reference, not body prose, so they are
#: excluded — counting them would make a section's "length" disagree with
#: what a human (or a call-for-proposal's limit) means by word count.
PROSE_CHUNK_KINDS: frozenset[str] = frozenset({"paragraph", "aside", "callout"})

# A draft cross-reference is a bracketed handle (``[¶ab12]`` / ``[§foo~3]``
# / ``[dc42]`` / ``[me7]`` / ``[[surface]]``) or the link-target half of a
# ``[surface](¶term)`` markdown link. We keep the *visible* text of a
# markdown link and drop bare bracket refs entirely.
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BRACKET_REF = re.compile(r"\[[^\]]*\]")
# A "word": a run of alphanumerics, optionally joined by an internal
# apostrophe or hyphen (so "state-of-the-art" and "don't" count as one).
_WORD = re.compile(r"[0-9A-Za-z]+(?:['’-][0-9A-Za-z]+)*")


def count_words(text: str | None) -> int:
    """Return the number of visible prose words in ``text``.

    Markdown links keep their visible text; bare bracketed handle
    references are dropped before counting."""
    if not text:
        return 0
    cleaned = _MD_LINK.sub(r"\1", text)
    cleaned = _BRACKET_REF.sub(" ", cleaned)
    return len(_WORD.findall(cleaned))


class _ChunkLike(Protocol):
    chunk_id: int
    parent_chunk_id: int | None
    chunk_kind: str
    text: str
    meta: dict[str, Any]


def _parse_target(meta: dict[str, Any] | None) -> tuple[int, int] | None:
    """Read ``meta.word_target`` → ``(min, max)`` ints, or ``None``.

    Tolerant of a missing bound: ``{"max": 500}`` ⇒ ``(0, 500)``,
    ``{"min": 200}`` ⇒ ``(200, sys.maxsize-ish)`` via a large sentinel."""
    if not meta:
        return None
    wt = meta.get("word_target")
    if not isinstance(wt, dict):
        return None
    lo_raw = wt.get("min")
    hi_raw = wt.get("max")
    if lo_raw is None and hi_raw is None:
        return None
    lo = int(lo_raw) if lo_raw is not None else 0
    hi = int(hi_raw) if hi_raw is not None else 10**9
    return (lo, hi)


def _verdict(words: int, target: tuple[int, int] | None) -> str:
    if target is None:
        return "none"
    lo, hi = target
    if words < lo:
        return "under"
    if words > hi:
        return "over"
    return "ok"


@dataclass(frozen=True, slots=True)
class SectionCount:
    """Word count + verdict for one heading's section (subtree-inclusive)."""

    chunk_id: int
    title: str
    words: int
    target: tuple[int, int] | None
    verdict: str  # 'under' | 'over' | 'ok' | 'none'


@dataclass(frozen=True, slots=True)
class WordCountReport:
    total: int
    sections: list[SectionCount] = field(default_factory=list)


def aggregate_word_counts(chunks: Sequence[_ChunkLike]) -> WordCountReport:
    """Aggregate per-chunk word counts under each enclosing heading.

    ``chunks`` is a draft's reading-order list (DFS). Each prose chunk's
    words are attributed to **every** ancestor heading, so a section's
    count includes its subsections (matching how a call-for-proposal's
    per-section limit is meant — the whole section, nested parts and
    all). The whole-document total counts each prose chunk once.

    Headings are returned in reading order; each carries its
    ``meta.word_target`` verdict (``none`` when no target is set)."""
    by_id: dict[int, _ChunkLike] = {c.chunk_id: c for c in chunks}
    heading_words: dict[int, int] = {
        c.chunk_id: 0 for c in chunks if c.chunk_kind == "heading"
    }
    total = 0
    for c in chunks:
        if c.chunk_kind not in PROSE_CHUNK_KINDS:
            continue
        w = count_words(c.text)
        total += w
        # Walk ancestors, crediting each enclosing heading.
        pid = c.parent_chunk_id
        seen: set[int] = set()
        while pid is not None and pid in by_id and pid not in seen:
            seen.add(pid)
            parent = by_id[pid]
            if parent.chunk_kind == "heading":
                heading_words[pid] = heading_words.get(pid, 0) + w
            pid = parent.parent_chunk_id

    sections: list[SectionCount] = []
    for c in chunks:
        if c.chunk_kind != "heading":
            continue
        words = heading_words.get(c.chunk_id, 0)
        target = _parse_target(c.meta)
        sections.append(
            SectionCount(
                chunk_id=c.chunk_id,
                title=(c.text or "").strip(),
                words=words,
                target=target,
                verdict=_verdict(words, target),
            )
        )
    return WordCountReport(total=total, sections=sections)


__all__ = [
    "PROSE_CHUNK_KINDS",
    "SectionCount",
    "WordCountReport",
    "aggregate_word_counts",
    "count_words",
]
