"""Reciprocal-rank scores and provenance for ranked retrieval legs."""

from collections.abc import Hashable, Iterable


def reciprocal_rank_fusion[Label: Hashable, Key: Hashable](
    legs: Iterable[tuple[Label, Iterable[tuple[Key, float]]]],
    *,
    k: int = 60,
) -> tuple[dict[Key, float], dict[Key, dict[Label, tuple[int, float]]]]:
    """Return fused scores and each hit's zero-based rank and score per leg."""
    totals: dict[Key, float] = {}
    provenance: dict[Key, dict[Label, tuple[int, float]]] = {}
    for label, rows in legs:
        for rank, (key, score) in enumerate(rows):
            sources = provenance.setdefault(key, {})
            if label in sources:
                continue
            totals[key] = totals.get(key, 0.0) + 1.0 / (k + rank + 1)
            sources[label] = (rank, score)
    return totals, provenance
