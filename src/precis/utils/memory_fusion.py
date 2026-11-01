"""Memory recall fusion: three soft re-ranks on the fused hybrid page (gr548265).

``search_chunks_fused`` ranks by reciprocal rank, so any hit both legs saw
outranks any hit one leg saw, whatever its distance. On the memory graph
that cost recall in three measured ways, each fixed here behind its own
default-on env flag so ``precis memory index --set`` can isolate it:

1. **Semantic-only rescue** (``PRECIS_MEMORY_FUSION_RESCUE``). The semantic
   rank-1 hit, and any hit within :func:`near_best` of it, keeps a slot in
   the top ``k`` even with no lexical match (gr515867: me478000 was
   semantic rank 1 and fused below eight both-leg hits). Only hits that
   clear the semantic-only cut qualify, and the rescue takes at most half
   the page, so a tight semantic cluster cannot evict every lexical hit.
   The window is gr548264's gap cut, so with it on every cut-clearing
   semantic hit is a candidate and the half-page cap is what binds: the
   nearest ``k // 2`` by distance keep their slots.
2. **Recency prior** (``PRECIS_MEMORY_FUSION_RECENCY``). The fused score is
   multiplied by the node's band factor
   (:func:`precis.utils.memory_hubs.band_factors`: ACTIVE 1.15, SETTLED
   1.0, untouched past 60 days 0.9). A factor, never a filter.
3. **Gotcha quota** (``PRECIS_MEMORY_FUSION_QUOTA``). When no
   ``section:gotchas`` node made the top ``k`` but one in the pool clears
   the cut, the best one is appended as row ``k+1``: a flat fused list let
   five threads crowd out the gotcha that would have prevented the mistake.

The functions here are pure over the page the store returned; the handler
(``MemoryHandler._rerank_fused``) reads the band factors and type tags.
The pool is the over-fetched fused page, so a semantic rank-1 hit that
fused below the whole over-fetch is out of reach of the rescue.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from precis.store._search_cut import fixed_floors_enabled, semantic_fold_distance
from precis.utils.env import env_flag

RESCUE_FLAG = "PRECIS_MEMORY_FUSION_RESCUE"
RECENCY_FLAG = "PRECIS_MEMORY_FUSION_RECENCY"
QUOTA_FLAG = "PRECIS_MEMORY_FUSION_QUOTA"

#: Distance window around the best semantic distance that still counts as
#: "rank 1" for the rescue under ``PRECIS_SEARCH_FIXED_FLOORS=1`` — the
#: fallback gr548265 names; by default :func:`near_best` uses gr548264's
#: query-relative gap cut instead.
RESCUE_DELTA = 0.03

#: Type tag the quota admits.
GOTCHAS_TAG = "section:gotchas"

Hit = tuple[Any, Any, float]
FoldInfo = Mapping[int, tuple[float | None, bool]]


@dataclass(frozen=True)
class FusionFlags:
    """Which of the three re-ranks run; all off = the plain fused order."""

    rescue: bool = True
    recency: bool = True
    quota: bool = True

    @classmethod
    def from_env(cls) -> FusionFlags:
        return cls(
            rescue=env_flag(RESCUE_FLAG, default=True),
            recency=env_flag(RECENCY_FLAG, default=True),
            quota=env_flag(QUOTA_FLAG, default=True),
        )

    @property
    def any(self) -> bool:
        return self.rescue or self.recency or self.quota


def near_best(distance: float, best: float) -> bool:
    """Whether ``distance`` sits within the gap cut of the page's ``best``
    semantic distance and so shares its rescue (gr548264's
    ``gap_cut_distance``, honouring ``PRECIS_SEARCH_GAP_CUT``); the
    fixed-floor fallback keeps the :data:`RESCUE_DELTA` window."""
    if fixed_floors_enabled():
        return distance <= best + RESCUE_DELTA
    bound = semantic_fold_distance(None, [best])
    return bound is not None and distance <= bound


def _info(fold_info: FoldInfo, hit: Hit) -> tuple[float | None, bool]:
    return fold_info.get(hit[0].id, (None, True))


def _clears(fold_info: FoldInfo, hit: Hit, cut: float | None) -> bool:
    """The hit survives the semantic-only cut: lexically corroborated,
    no semantic distance (a lexical-only row), or within ``cut``."""
    d, corroborated = _info(fold_info, hit)
    return corroborated or d is None or cut is None or d <= cut


def apply_recency(hits: list[Hit], factors: Mapping[int, float]) -> list[Hit]:
    """``hits`` re-sorted by fused score × band factor (missing = 1.0);
    ties keep the fused order."""
    if not factors:
        return list(hits)
    return sorted(hits, key=lambda h: -float(h[2]) * factors.get(h[1].id, 1.0))


def apply_rescue(
    hits: list[Hit], fold_info: FoldInfo, *, k: int, cut: float | None
) -> list[Hit]:
    """Move the semantic rank-1 hit (and its :func:`near_best` neighbours,
    at most ``k // 2`` of them, at least one) into the top ``k``.

    A rescued hit already in the top ``k`` stays where it is; one below
    takes the tail of the top ``k``, evicting the lowest unrescued hits to
    the head of the remainder, so no hit is lost."""
    if k <= 0 or len(hits) <= k:
        return list(hits)
    dists = [
        (d, i)
        for i, h in enumerate(hits)
        if (d := _info(fold_info, h)[0]) is not None and _clears(fold_info, h, cut)
    ]
    if not dists:
        return list(hits)
    best = min(d for d, _i in dists)
    picked = sorted((d, i) for d, i in dists if near_best(d, best))
    rescue = {i for _d, i in picked[: max(1, k // 2)]}
    missing = sorted(i for i in rescue if i >= k)
    if not missing:
        return list(hits)
    evictable = [i for i in range(k) if i not in rescue]
    evicted = set(evictable[len(evictable) - len(missing) :])
    top = [i for i in range(k) if i not in evicted] + missing
    rest = sorted(evicted) + [i for i in range(k, len(hits)) if i not in rescue]
    return [hits[i] for i in top + rest]


def apply_quota(
    hits: list[Hit],
    fold_info: FoldInfo,
    *,
    k: int,
    cut: float | None,
    gotchas: Collection[int],
) -> tuple[list[Hit], int]:
    """Admit the best cut-clearing gotcha as row ``k+1`` when none made the
    top ``k``. Returns ``(hits, extra)``; ``extra`` is 1 when the first page
    grew by the admitted row, else 0."""
    if k <= 0 or not gotchas or any(h[1].id in gotchas for h in hits[:k]):
        return list(hits), 0
    for i in range(k, len(hits)):
        h = hits[i]
        if h[1].id in gotchas and _clears(fold_info, h, cut):
            return [*hits[:k], h, *hits[k:i], *hits[i + 1 :]], 1
    return list(hits), 0


def rerank(
    hits: list[Hit],
    fold_info: FoldInfo,
    *,
    k: int,
    cut: float | None,
    factors: Mapping[int, float],
    gotchas: Collection[int],
    flags: FusionFlags,
) -> tuple[list[Hit], int]:
    """Recency, then rescue, then quota over one best-chunk-per-ref fused
    list. Returns ``(hits, extra)`` as :func:`apply_quota`; every flag off
    returns ``hits`` unchanged."""
    out = list(hits)
    if flags.recency:
        out = apply_recency(out, factors)
    if flags.rescue:
        out = apply_rescue(out, fold_info, k=k, cut=cut)
    if flags.quota:
        return apply_quota(out, fold_info, k=k, cut=cut, gotchas=gotchas)
    return out, 0


__all__ = [
    "GOTCHAS_TAG",
    "QUOTA_FLAG",
    "RECENCY_FLAG",
    "RESCUE_DELTA",
    "RESCUE_FLAG",
    "FusionFlags",
    "apply_quota",
    "apply_recency",
    "apply_rescue",
    "near_best",
    "rerank",
]
