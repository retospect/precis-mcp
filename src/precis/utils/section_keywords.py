"""Roll a run of chunks up into a keyword label (source-backfill slice 2).

The "no bare counts" rule: a collapsed section / gap shows *what is under it*,
not a bare ``+N``. This is the **ship-first** labeler — a frequency-ranked union
of the run's per-chunk KeyBERT keywords (``chunks.keywords`` via
``block_views``). It is deterministic (the *fact* glyph family — a rolled label
is trustworthy, not a model guess).

The design's later upgrade is **c-TF-IDF** — terms *distinctive* to this subtree
vs. the rest of the doc, so sibling sections ("Methods" vs "Results") don't roll
up to the same bag. That needs the per-keyword scores in ``keywords_meta`` and
the doc-wide term stats; this frequency union is the honest v1 over the data
``block_views`` already exposes.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Protocol

#: Default label width — enough to characterise a section, short enough to stay
#: a one-line bookmark.
_DEFAULT_TOP_K = 6


class _Chunk(Protocol):
    handle: str


def rollup_keywords(
    views: dict[str, dict[str, str]],
    chunks: list[_Chunk],
    *,
    top_k: int = _DEFAULT_TOP_K,
) -> list[str]:
    """The top-``top_k`` keywords across ``chunks``, most-frequent first (ties
    broken by first appearance). ``views`` is a ``block_views`` map
    (``{handle: {"keywords": "a, b, c", ...}}``, keyed by the chunk's ``.handle``
    like the rest of the eye machinery). A chunk missing from ``views`` or with
    no keywords contributes nothing; an all-empty run yields ``[]`` (the caller
    falls back to a bare count)."""
    counts: Counter[str] = Counter()
    display: dict[str, tuple[int, str]] = {}  # key -> (first-seen index, form)
    for i, chunk in enumerate(chunks):
        kwline = (views.get(chunk.handle, {}) or {}).get("keywords", "") or ""
        for raw in kwline.split(","):
            term = raw.strip()
            if not term:
                continue
            key = term.lower()
            counts[key] += 1
            display.setdefault(key, (i, term))
    ranked = sorted(counts, key=lambda key: (-counts[key], display[key][0]))
    return [display[key][1] for key in ranked[:top_k]]


def rollup_label(
    views: dict[str, dict[str, str]],
    chunks: list[Any],
    *,
    top_k: int = _DEFAULT_TOP_K,
    sep: str = " · ",
) -> str:
    """A ``rollup_keywords`` join, or ``""`` when the run has no keywords — the
    ready-to-append label for a collapsed run / section."""
    return sep.join(rollup_keywords(views, chunks, top_k=top_k))
