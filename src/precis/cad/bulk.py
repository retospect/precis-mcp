"""Bulk integrals — volume & centroid (ADR 0041 §8).

Everything else in the kernel is exact under the rigid-only invariant. The
bulk integrals are a numerical *quadrature*, but not a 3-D Monte-Carlo dart
throw: we integrate the **exact** solid extent along a 2-D grid of parallel
rays. Each ray is folded through the CSG expression by :func:`fold.material_intervals`,
which returns the exact ``[t_in, t_out]`` runs of solid material (cuts and
overlaps already honoured), so the only error is the 2-D quadrature over the
ray grid — vastly smaller than sampling a volume fraction, and O(grid²) exact
ray casts instead of hundreds of thousands of pointwise classifications.

The result is still ``sampled`` (a grid quadrature, not closed form), and
``rel_err`` is a Richardson estimate from a coarser grid so the LLM never
mistakes it for exact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from precis.cad.fold import Expr, Instance, Leaf, material_intervals
from precis.cad.graph import Design
from precis.cad.vec import Vec3, as_vec3, vec3


@dataclass(frozen=True)
class BulkResult:
    """A quadrature bulk estimate.

    ``sampled`` is always ``True`` for this tier (a ray-grid quadrature);
    ``rel_err`` is a Richardson relative-error estimate (|V_fine − V_coarse| /
    V_fine). ``samples`` is the number of rays cast (``grid²``).
    """

    volume: float
    centroid: Vec3
    samples: int
    rel_err: float
    sampled: bool = True


def _expr_aabb(design: Design, expr: Expr) -> tuple[Vec3, Vec3]:
    """Union AABB over every leaf reachable from ``expr`` (additive bound)."""
    los: list[Vec3] = []
    his: list[Vec3] = []

    def walk(e: Expr) -> None:
        if isinstance(e, Leaf):
            lo, hi = design.instances[e.iid].placed.aabb()
            if np.all(np.isfinite(lo)) and np.all(np.isfinite(hi)):
                los.append(lo)
                his.append(hi)
        for child in getattr(e, "parts", ()):
            walk(child)
        base = getattr(e, "base", None)
        if base is not None:
            walk(base)
        for c in getattr(e, "cutters", ()):
            walk(c)

    walk(expr)
    if not los:
        raise ValueError("cannot bound an empty / unbounded expression")
    lo = as_vec3(np.min(np.array(los), axis=0))
    hi = as_vec3(np.max(np.array(his), axis=0))
    return lo, hi


def _grid_for(samples: int) -> int:
    """Map the legacy ``samples`` (3-D point budget) to a 2-D ray-grid side.

    Kept so existing callers passing ``samples=`` still tune accuracy the way
    they expect (more budget → finer grid) without casting hundreds of
    thousands of rays. ``grid² ≈ samples/20``, clamped to a sane range.
    """
    return int(min(200, max(48, round(math.sqrt(max(samples, 1)) / 4.5))))


def _integrate(
    expr: Expr,
    instances: dict[str, Instance],
    lo: Vec3,
    hi: Vec3,
    grid: int,
) -> tuple[float, Vec3]:
    """Volume + centroid by integrating exact solid length along a ``grid×grid``
    array of ``+z`` rays over the ``x/y`` cross-section of the AABB."""
    dx = (hi[0] - lo[0]) / grid
    dy = (hi[1] - lo[1]) / grid
    cell_area = dx * dy
    xs = lo[0] + (np.arange(grid) + 0.5) * dx
    ys = lo[1] + (np.arange(grid) + 0.5) * dy
    o_z = float(lo[2]) - 1.0  # start every ray just below the solid
    d = vec3(0.0, 0.0, 1.0)

    length_sum = 0.0
    cx = cy = cz = 0.0
    for x in xs:
        fx = float(x)
        for y in ys:
            o = vec3(fx, float(y), o_z)
            for t0, t1 in material_intervals(expr, o, d, instances):
                if math.isinf(t0) or math.isinf(t1):
                    continue
                length = t1 - t0
                length_sum += length
                cx += fx * length
                cy += float(y) * length
                cz += (o_z + 0.5 * (t0 + t1)) * length

    vol = length_sum * cell_area
    if length_sum > 0:
        centroid = vec3(cx / length_sum, cy / length_sum, cz / length_sum)
    else:
        centroid = vec3(0.0, 0.0, 0.0)
    return vol, centroid


def volume(
    design: Design,
    *,
    component: str | None = None,
    grid: int | None = None,
    samples: int = 200_000,
    seed: int = 0,  # accepted for back-compat; quadrature is deterministic
) -> BulkResult:
    """Volume + centroid by exact ray-interval quadrature.

    Casts a ``grid×grid`` array of ``+z`` rays over the part's AABB and, for
    each ray, integrates the exact solid length returned by
    :func:`fold.material_intervals` (so subtractions and overlaps are honoured
    exactly along the ray). ``grid`` overrides the resolution; otherwise it is
    derived from the legacy ``samples`` budget. ``rel_err`` is a Richardson
    estimate against a coarser grid.
    """
    expr = design.components[component] if component else design.whole()
    lo, hi = _expr_aabb(design, expr)
    n = grid if grid is not None else _grid_for(samples)

    vol, centroid = _integrate(expr, design.instances, lo, hi, n)
    n_coarse = max(8, round(n / 1.5))
    vol_coarse, _ = _integrate(expr, design.instances, lo, hi, n_coarse)
    rel_err = abs(vol - vol_coarse) / vol if vol > 0 else 1.0

    return BulkResult(
        volume=round(vol, 4),
        centroid=centroid,
        samples=n * n,
        rel_err=round(rel_err, 5),
    )
