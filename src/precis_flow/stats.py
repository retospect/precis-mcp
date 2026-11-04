"""Statistics over reduced-units trace results.

Ported from cfdtest's ``cost.py``: the first-crossing evenness score. The
weighted annealer cost itself (``w_break``, ``w_holes``, ``w_res``, ``w_even``)
belongs to the annealer slice, not the kernel.

Outlet masks aggregate crossings before computing entropy: equally fed outlets
score zero even when their areas differ. Empty or unfed outlets are refused,
so a missing outlet cannot silently improve an optimisation objective.
"""

from __future__ import annotations

import numpy as np


def nonuniformity(
    points: np.ndarray,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    bins: tuple[int, int] | None = None,
    *,
    outlet_masks: list[np.ndarray] | None = None,
) -> tuple[float, np.ndarray]:
    """Evenness of first-crossing points over a region, as a cost in [0, 1].

    Bins the crossings inside ``x_range`` x ``y_range`` (reduced units) into a
    coarse 2-D histogram and measures ``1 - normalized_entropy``: 0 = a
    perfectly even spread, 1 = all mass in one bin (or no crossings at all).
    Alternatively, omit ``bins`` and pass ``outlet_masks``: disjoint Boolean
    grids of identical shape ``(ny, nx)``, spanning the given ranges. Each
    outlet's total crossing count is one entropy bin, without dividing by
    its area. Grid rows follow y and columns follow x; interior cell edges
    belong to the higher-index cell, and the upper range edges are included.
    One fed outlet scores zero. An empty list or an outlet with no crossings
    raises ``ValueError``; outlet mode also requires finite increasing ranges
    and finite points. Points outside the ranges or mask union are excluded.

    Returns ``(nonuniformity, points kept inside the region or mask union)``
    with the kept points in input order.
    """
    points = np.asarray(points, dtype=float).reshape(-1, 2)
    if outlet_masks is not None:
        if bins is not None:
            raise ValueError("supply either bins or outlet_masks, not both")
        counts, pts = _outlet_counts(points, x_range, y_range, outlet_masks)
        return _entropy_cost(counts), pts
    if bins is None:
        raise ValueError("supply bins or outlet_masks")
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
    return _entropy_cost(counts), pts


def _entropy_cost(counts: np.ndarray) -> float:
    """One minus entropy normalised by the number of bins."""
    total = counts.sum()
    if total <= 0:
        return 1.0
    p = counts[counts > 0] / total
    entropy = -(p * np.log(p)).sum()
    max_entropy = np.log(counts.size)
    norm_entropy = entropy / max_entropy if max_entropy > 0 else 1.0
    return float(1.0 - norm_entropy)


def _outlet_counts(
    points: np.ndarray,
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    outlet_masks: list[np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    """Aggregate the grid histogram and retain only selected crossings."""
    if not outlet_masks:
        raise ValueError("outlet_masks must contain at least one outlet")
    if (
        not np.isfinite([x_range, y_range]).all()
        or x_range[0] >= x_range[1]
        or y_range[0] >= y_range[1]
    ):
        raise ValueError("outlet ranges must be finite and strictly increasing")
    if not np.isfinite(points).all():
        raise ValueError("outlet crossing points must be finite")
    masks = [np.asarray(mask) for mask in outlet_masks]
    shape = masks[0].shape
    if len(shape) != 2 or 0 in shape:
        raise ValueError("outlet masks must be nonempty 2-D grids")
    union = np.zeros(shape, dtype=bool)
    for index, mask in enumerate(masks):
        if mask.dtype != np.dtype(bool) or mask.shape != shape:
            raise ValueError("outlet masks must be Boolean grids with the same shape")
        if (union & mask).any():
            raise ValueError(f"outlet mask {index} overlaps another outlet")
        union |= mask

    keep = (
        (points[:, 0] >= x_range[0])
        & (points[:, 0] <= x_range[1])
        & (points[:, 1] >= y_range[0])
        & (points[:, 1] <= y_range[1])
    )
    pts = points[keep]
    ny, nx = shape
    hist, x_edges, y_edges = np.histogram2d(
        pts[:, 0], pts[:, 1], bins=[nx, ny], range=[list(x_range), list(y_range)]
    )
    counts = np.array([hist.T[mask].sum() for mask in masks])
    for index, count in enumerate(counts):
        if count <= 0:
            raise ValueError(f"outlet mask {index} has zero flow (no crossings)")
    columns = np.minimum(np.searchsorted(x_edges, pts[:, 0], side="right") - 1, nx - 1)
    rows = np.minimum(np.searchsorted(y_edges, pts[:, 1], side="right") - 1, ny - 1)
    return counts, pts[union[rows, columns]]
