"""Query-relative hybrid search policy, shared by rendering and recall."""

from __future__ import annotations

import math
import os
from collections.abc import Iterable

from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR, SEMANTIC_ONLY_FOLD_DISTANCE

# Provisional conservative end of the .03-.15 sweep (gr548264).
# Local 36-query recall snapshots + hash embedder, 2026-11-01:
# fixed .42 and every gap .03-.15 both drop 100% rel / remove 100% noise
# (360 noise distances; all exceed .65). This cannot calibrate bge-m3.
# No paper vectors are in-repo; prod memory/paper loss budgets and 34/36
# recall remain unverified. Remeasure free text with search-floor-calibrate.
GAP_CUT_DELTA = 0.15


def gap_cut_distance(
    distances: Iterable[float | None],
    delta: float = GAP_CUT_DELTA,
    *,
    corpus_floor: float = SEMANTIC_DISTANCE_FLOOR,
) -> float:
    """Inclusive keep boundary: best candidate + delta, capped by corpus floor.

    Pure helper for callers that already have distances; no lexical decision
    belongs here. A caller must always keep lexically corroborated candidates.
    """
    best = min((d for d in distances if d is not None), default=corpus_floor)
    return min(corpus_floor, best + delta)


def fixed_floors_enabled() -> bool:
    return os.environ.get("PRECIS_SEARCH_FIXED_FLOORS", "0") == "1"


def fold_enabled(kind: str) -> bool:
    return not fixed_floors_enabled() or kind in SEMANTIC_ONLY_FOLD_DISTANCE


def semantic_fold_distance(
    kind: str | None, distances: Iterable[float | None]
) -> float | None:
    """Resolve the one-release fallback and the operator's delta override."""
    if fixed_floors_enabled():
        return SEMANTIC_ONLY_FOLD_DISTANCE.get(kind or "")
    try:
        delta = float(os.environ.get("PRECIS_SEARCH_GAP_CUT", GAP_CUT_DELTA))
    except ValueError:
        delta = GAP_CUT_DELTA
    if not math.isfinite(delta) or delta < 0:
        delta = GAP_CUT_DELTA
    return gap_cut_distance(distances, delta)
