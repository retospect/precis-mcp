"""Statistics over reduced-units trace results.

Ported from cfdtest's ``cost.py``: the first-crossing evenness score. The
weighted annealer cost itself (``w_break``, ``w_holes``, ``w_res``, ``w_even``)
belongs to the annealer slice, not the kernel.
"""

from __future__ import annotations

import numpy as np


def nonuniformity(
    points: np.ndarray,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    bins: tuple[int, int],
) -> tuple[float, np.ndarray]:
    """Evenness of first-crossing points over a region, as a cost in [0, 1].

    Bins the crossings inside ``x_range`` x ``y_range`` (reduced units) into a
    coarse 2-D histogram and measures ``1 - normalized_entropy``: 0 = a
    perfectly even spread, 1 = all mass in one bin (or no crossings at all).
    Returns ``(nonuniformity, points kept inside the region)``.
    """
    points = np.asarray(points, dtype=float).reshape(-1, 2)
    if points.size == 0:
        return 1.0, points
    keep = (
        (points[:, 0] >= x_range[0])
        & (points[:, 0] <= x_range[1])
        & (points[:, 1] >= y_range[0])
        & (points[:, 1] <= y_range[1])
    )
    pts = points[keep]
    if pts.shape[0] == 0:
        return 1.0, pts
    hist, _, _ = np.histogram2d(
        pts[:, 0], pts[:, 1], bins=list(bins), range=[list(x_range), list(y_range)]
    )
    counts = hist.ravel()
    total = counts.sum()
    if total <= 0:
        return 1.0, pts
    p = counts[counts > 0] / total
    entropy = -(p * np.log(p)).sum()
    max_entropy = np.log(counts.size)
    norm_entropy = entropy / max_entropy if max_entropy > 0 else 1.0
    return float(1.0 - norm_entropy), pts
