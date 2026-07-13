"""Roll a run of chunks up into a keyword label (source-backfill slice 2 → 5).

The "no bare counts" rule: a collapsed section / gap shows *what is under it*,
not a bare ``+N``. This is the deterministic (the *fact* glyph family — a rolled
label is trustworthy, not a model guess) labeler over the per-chunk KeyBERT
keywords ``block_views`` already exposes (``chunks.keywords``).

**Ranking is c-TF-IDF** (slice 5 upgrade over the slice-2 frequency union): a
term scores by how much it *characterises this run against the rest of the
document*, not by raw frequency — so sibling sections ("Methods" vs "Results")
don't both roll up to the doc's ambient vocabulary. The background corpus is
already in hand: the ``views`` map the composer passes is the **whole document's**
``block_views``, so document-frequency (how many chunks in the doc carry a term)
is a free by-product. Score = ``tf(term, run) × idf(term, doc)`` with the
sklearn-smoothed ``idf = ln((1+N)/(1+df)) + 1`` — always positive (a run never
labels empty when it has terms) and monotone-decreasing in ``df`` (a
doc-ubiquitous term is damped, a run-distinctive term amplified). On a small doc
where every term spans the whole run this degrades to frequency order (idf is
uniform), so the honest v1 behaviour is preserved on tiny inputs and only the
generic-term suppression is new.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Protocol

#: Default label width — enough to characterise a section, short enough to stay
#: a one-line bookmark.
_DEFAULT_TOP_K = 6


class _Chunk(Protocol):
    handle: str


def _doc_frequencies(views: dict[str, dict[str, str]]) -> tuple[Counter[str], int]:
    """``(df, N)`` over the whole document: ``df[key]`` = number of chunks whose
    keyword set contains the term, ``N`` = number of chunks carrying any keyword.
    Presence-per-chunk (a term counts once per chunk), the granularity the run's
    ``tf`` also uses, so the ratio is well-formed. ``views`` is the composer's
    whole-doc ``block_views`` map — the background corpus, free of a second
    query."""
    df: Counter[str] = Counter()
    n = 0
    for v in views.values():
        kwline = (v or {}).get("keywords", "") or ""
        terms = {raw.strip().lower() for raw in kwline.split(",") if raw.strip()}
        if terms:
            n += 1
            for key in terms:
                df[key] += 1
    return df, n


def rollup_keywords(
    views: dict[str, dict[str, str]],
    chunks: list[_Chunk],
    *,
    top_k: int = _DEFAULT_TOP_K,
) -> list[str]:
    """The top-``top_k`` keywords characterising ``chunks`` (a collapsed run),
    ranked by c-TF-IDF against the whole document, distinctive-first (ties broken
    by first appearance). ``views`` is a ``block_views`` map
    (``{handle: {"keywords": "a, b, c", ...}}``, keyed by the chunk's ``.handle``
    like the rest of the eye machinery) covering the *whole document* — it is both
    the run's per-chunk keyword source and the background df corpus. A chunk
    missing from ``views`` or with no keywords contributes nothing; an all-empty
    run yields ``[]`` (the caller falls back to a bare count)."""
    tf: Counter[str] = Counter()  # run term frequency, presence-per-chunk
    display: dict[str, tuple[int, str]] = {}  # key -> (first-seen index, form)
    for i, chunk in enumerate(chunks):
        kwline = (views.get(chunk.handle, {}) or {}).get("keywords", "") or ""
        seen: set[str] = set()
        for raw in kwline.split(","):
            term = raw.strip()
            if not term:
                continue
            key = term.lower()
            if key not in seen:
                seen.add(key)
                tf[key] += 1
            display.setdefault(key, (i, term))
    if not tf:
        return []
    df, n = _doc_frequencies(views)

    def score(key: str) -> float:
        # sklearn-smoothed idf: always positive, monotone-decreasing in df — a
        # doc-ubiquitous term is damped toward ~tf, a run-distinctive one lifted.
        idf = math.log((1 + n) / (1 + df.get(key, 0))) + 1.0
        return tf[key] * idf

    ranked = sorted(tf, key=lambda key: (-score(key), display[key][0]))
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
