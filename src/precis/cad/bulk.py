"""Bulk integrals — volume & centroid (ADR 0041 §8, the *sampled* tier).

Everything else in the kernel is exact under the rigid-only invariant; the
bulk integrals are the one place v1 samples (a Monte-Carlo estimate over
the part's bounding box), and the results are **labelled sampled** with a
relative-error estimate so the LLM never mistakes them for exact.

This is the "bulk tier" ADR 0041 §9 anticipates vectorising later behind
the same node-list — the loop estimator here is the free-deferral
placeholder.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from precis.cad.fold import Expr, Leaf
from precis.cad.graph import Design
from precis.cad.vec import Vec3, as_vec3, vec3


@dataclass(frozen=True)
class BulkResult:
    """A sampled bulk estimate.

    ``sampled`` is always ``True`` for this tier; ``rel_err`` is the
    1-sigma relative standard error of the Monte-Carlo volume estimate.
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


def volume(
    design: Design,
    *,
    component: str | None = None,
    samples: int = 200_000,
    seed: int = 0,
) -> BulkResult:
    """Monte-Carlo volume + centroid over the part's bounding box.

    Draws ``samples`` uniform points in the AABB and counts those inside
    the (folded) solid via :meth:`Design.classify_point`, so subtractions
    are honoured. Centroid is the mean of the inside points.
    """
    expr = design.components[component] if component else design.whole()
    lo, hi = _expr_aabb(design, expr)
    rng = np.random.default_rng(seed)
    box_vol = float(np.prod(hi - lo))
    pts = rng.uniform(lo, hi, size=(samples, 3))
    from precis.cad.fold import classify

    inside_mask = np.fromiter(
        (classify(expr, p, design.instances).inside for p in pts),
        dtype=bool,
        count=samples,
    )
    hits = int(inside_mask.sum())
    frac = hits / samples
    vol = box_vol * frac
    if hits:
        centroid = as_vec3(pts[inside_mask].mean(axis=0))
    else:
        centroid = vec3(0.0, 0.0, 0.0)
    # 1-sigma relative error of a binomial fraction estimate.
    rel_err = (
        math.sqrt(max(0.0, frac * (1.0 - frac) / samples)) / frac if frac > 0 else 1.0
    )
    return BulkResult(
        volume=round(vol, 4),
        centroid=centroid,
        samples=samples,
        rel_err=round(rel_err, 5),
    )
