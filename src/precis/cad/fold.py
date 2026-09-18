"""Boolean CSG fold with node attribution.

The eval never computes the merged solid. Instead it folds per-primitive
results through the boolean ops:

* ``merge``     = ``any``      (union)
* ``subtract``  = ``first ∧ ¬rest``
* ``intersect`` = ``all``

The fold tracks *attribution* so a probe in a carved region reports
``void`` **and names the blocking node** ("empty; removed by ``bolt#1``").
Testing primitives independently would wrongly report material in a
hole; walking the ops is what makes a drilled bore read as a bore.

The expression operates over *instances* — primitives already placed in
the world frame and carrying a display ``label`` (``plate``, ``bolt#3``).
Patterns expand to a union of labelled instances upstream, so the fold
sees only leaves and the three boolean ops.

**Signed distance** lives here too (:func:`expr_sdf` / :func:`expr_sdf_np`
— the scalar and ``(N, 3)``-vectorised folds ``relate.component_sdf`` and
the field-export backend read): union → ``min``, intersect → ``max``,
subtract → ``max(d_base, -d_cutter)``. Sign exact everywhere, magnitude
exact on the governing surface. A :class:`Union` may carry ``blend=k``
(``blend:`` on an ``add`` node): the fold then uses the quadratic
smooth-min (:func:`smooth_min`) instead of ``min``, which adds material in
the concave seam where the two fields are within ``k`` of each other —
a fillet-*like* blend, **not an exact radius** (the contract's stated
limit; an exact concave fillet is the slice-2 field leaf's closing).
Membership (:func:`classify`) follows the same field for a blended union
so a point probe and the exported mesh agree; the ray fold still splits
on leaf boundaries, so a ray through a blend seam is classified at leaf
crossings only (the seam's extra material has no ray endpoint of its own).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from precis.cad.interval import Intervals, merge_intervals
from precis.cad.primitives import Placed
from precis.cad.vec import LINEAR_REL_EPS, Vec3, as_vec3


@dataclass(frozen=True)
class Instance:
    """A placed primitive with a display label and a stable id."""

    iid: str
    placed: Placed
    label: str


class Expr:
    """A CSG expression node."""


@dataclass(frozen=True)
class Leaf(Expr):
    iid: str


@dataclass(frozen=True)
class Union(Expr):
    """``parts`` merged; ``blend > 0`` folds them with :func:`smooth_min`
    (width ``blend``, a length) instead of a hard ``min`` — see the module
    docstring's "not an exact radius" caveat."""

    parts: tuple[Expr, ...]
    blend: float = 0.0


@dataclass(frozen=True)
class Diff(Expr):
    base: Expr
    cutters: tuple[Expr, ...]


@dataclass(frozen=True)
class Inter(Expr):
    parts: tuple[Expr, ...]


@dataclass(frozen=True)
class Class:
    """Point classification against an expression.

    ``inside``    — final membership (after cuts).
    ``additive``  — membership of the *base* skeleton ignoring cuts; tells
                    a carved void (additive, removed) apart from plain air.
    ``owner``     — instance id providing material when ``inside``.
    ``blocker``   — instance id that removed material when additive-not-inside.
    """

    inside: bool
    additive: bool
    owner: str | None
    blocker: str | None


def smooth_min(a: float, b: float, k: float) -> float:
    """Quadratic polynomial smooth-min of two signed distances with blend
    width ``k`` (Quilez): equals ``min(a, b)`` wherever ``|a - b| >= k``,
    and dips below it by at most ``k/4`` in the seam. Never above
    ``min(a, b)``, so it only ever *adds* material — and only inside the
    concave seam between the two bodies."""
    if k <= 0.0:
        return min(a, b)
    h = max(k - abs(a - b), 0.0) / k
    return min(a, b) - h * h * k * 0.25


def smooth_min_np(
    a: NDArray[np.float64], b: NDArray[np.float64], k: float
) -> NDArray[np.float64]:
    """Element-wise :func:`smooth_min`."""
    if k <= 0.0:
        return np.minimum(a, b)
    h = np.maximum(k - np.abs(a - b), 0.0) / k
    return np.minimum(a, b) - h * h * k * 0.25


def expr_sdf(expr: Expr, p: Vec3, instances: dict[str, Instance]) -> float:
    """Exact-sign CSG signed distance of a world point to ``expr``'s
    material (negative inside). Module docstring has the fold rules."""
    p = as_vec3(p)
    if isinstance(expr, Leaf):
        return float(instances[expr.iid].placed.distance(p))
    if isinstance(expr, Union):
        ds = [expr_sdf(part, p, instances) for part in expr.parts]
        if expr.blend > 0.0:
            cur = ds[0]
            for d in ds[1:]:
                cur = smooth_min(cur, d, expr.blend)
            return cur
        return min(ds)
    if isinstance(expr, Inter):
        return max(expr_sdf(part, p, instances) for part in expr.parts)
    if isinstance(expr, Diff):
        d = expr_sdf(expr.base, p, instances)
        for c in expr.cutters:
            d = max(d, -expr_sdf(c, p, instances))
        return d
    raise TypeError(f"unknown expr node: {expr!r}")


def expr_sdf_np(
    expr: Expr, pts: NDArray[np.float64], instances: dict[str, Instance]
) -> NDArray[np.float64]:
    """:func:`expr_sdf` for an ``(N, 3)`` array of world points at once —
    the same fold, evaluated through each leaf's ``distance_np``."""
    if isinstance(expr, Leaf):
        return instances[expr.iid].placed.distance_np(pts)
    if isinstance(expr, Union):
        cur = expr_sdf_np(expr.parts[0], pts, instances)
        for part in expr.parts[1:]:
            d = expr_sdf_np(part, pts, instances)
            cur = (
                smooth_min_np(cur, d, expr.blend)
                if expr.blend > 0.0
                else np.minimum(cur, d)
            )
        return cur
    if isinstance(expr, Inter):
        cur = expr_sdf_np(expr.parts[0], pts, instances)
        for part in expr.parts[1:]:
            cur = np.maximum(cur, expr_sdf_np(part, pts, instances))
        return cur
    if isinstance(expr, Diff):
        cur = expr_sdf_np(expr.base, pts, instances)
        for c in expr.cutters:
            cur = np.maximum(cur, -expr_sdf_np(c, pts, instances))
        return cur
    raise TypeError(f"unknown expr node: {expr!r}")


def _nearest_leaf(expr: Expr, p: Vec3, instances: dict[str, Instance]) -> str | None:
    """The leaf instance whose surface is nearest ``p`` — attribution for
    material a blend seam adds where no leaf contains the point."""
    best: tuple[float, str] | None = None

    def walk(e: Expr) -> None:
        nonlocal best
        if isinstance(e, Leaf):
            d = float(instances[e.iid].placed.distance(p))
            if best is None or d < best[0]:
                best = (d, e.iid)
        for child in getattr(e, "parts", ()):
            walk(child)
        base = getattr(e, "base", None)
        if base is not None:
            walk(base)
        for c in getattr(e, "cutters", ()):
            walk(c)

    walk(expr)
    return None if best is None else best[1]


def classify(expr: Expr, p: Vec3, instances: dict[str, Instance]) -> Class:
    """Classify a world point against a CSG expression."""
    p = as_vec3(p)
    if isinstance(expr, Leaf):
        inside = instances[expr.iid].placed.contains(p)
        return Class(inside, inside, expr.iid if inside else None, None)
    if isinstance(expr, Union):
        owner: str | None = None
        any_in = any_add = False
        for part in expr.parts:
            c = classify(part, p, instances)
            any_add = any_add or c.additive
            if c.inside and not any_in:
                any_in = True
                owner = c.owner
            elif c.inside:
                any_in = True
        if not any_in and expr.blend > 0.0 and expr_sdf(expr, p, instances) <= 0.0:
            # Seam material the smooth-min adds between the parts: inside
            # per the same field the export meshes, attributed to the
            # nearest leaf.
            return Class(True, True, _nearest_leaf(expr, p, instances), None)
        return Class(any_in, any_add, owner, None)
    if isinstance(expr, Inter):
        all_in = all_add = True
        owner = None
        blocker: str | None = None
        for part in expr.parts:
            c = classify(part, p, instances)
            all_in = all_in and c.inside
            all_add = all_add and c.additive
            if owner is None and c.owner is not None:
                owner = c.owner
            if not c.inside and blocker is None:
                blocker = c.owner or c.blocker
        return Class(
            all_in, all_add, owner if all_in else None, None if all_in else blocker
        )
    if isinstance(expr, Diff):
        b = classify(expr.base, p, instances)
        cut_hit: str | None = None
        for cutter in expr.cutters:
            cc = classify(cutter, p, instances)
            if cc.inside:
                cut_hit = cc.owner or cc.blocker
                break
        inside = b.inside and cut_hit is None
        blocker = None
        if b.inside and cut_hit is not None:
            blocker = cut_hit
        elif not b.inside:
            blocker = b.blocker
        return Class(inside, b.additive, b.owner if inside else None, blocker)
    raise TypeError(f"unknown expr node: {expr!r}")


def _instance_endpoints(
    expr: Expr, o: Vec3, d: Vec3, instances: dict[str, Instance]
) -> list[float]:
    """All finite ray-parameter boundaries from every leaf in ``expr``."""
    ts: list[float] = []
    seen: set[str] = set()

    def walk(e: Expr) -> None:
        if isinstance(e, Leaf):
            if e.iid in seen:
                return
            seen.add(e.iid)
            for lo, hi in instances[e.iid].placed.ray_hits(o, d):
                for t in (lo, hi):
                    if t not in (float("inf"), float("-inf")):
                        ts.append(t)
        elif isinstance(e, (Union, Inter)):
            for p in e.parts:
                walk(p)
        elif isinstance(e, Diff):
            walk(e.base)
            for c in e.cutters:
                walk(c)

    walk(expr)
    return ts


@dataclass(frozen=True)
class Span:
    """A classified run along a ray."""

    t_in: float
    t_out: float
    state: str  # 'solid' | 'void'
    feature: str | None  # instance label


def ray_spans(
    expr: Expr,
    o: Vec3,
    d: Vec3,
    instances: dict[str, Instance],
    *,
    eps: float | None = None,
) -> list[Span]:
    """Classify a ray into material / carved-void spans, best-effort labelled.

    Plain air (additive-false) outside the solid is **not** emitted — only
    material runs and the voids carved out of material, each attributed to
    the providing / removing instance. ``eps`` (a ray-parameter tolerance
    for "is this run thin enough to be numerical noise") defaults to a
    fraction (:data:`~precis.cad.vec.LINEAR_REL_EPS`) of each pair's own
    ``t`` magnitude, self-relative since the ray parameter's scale is
    whatever the caller's geometry put it in.
    """
    bounds = sorted(set(_instance_endpoints(expr, o, d, instances)))
    if len(bounds) < 2:
        return []
    raw: list[Span] = []
    for i in range(len(bounds) - 1):
        ta, tb = bounds[i], bounds[i + 1]
        tol = eps if eps is not None else LINEAR_REL_EPS * max(abs(ta), abs(tb))
        if tb - ta <= tol:
            continue
        tm = 0.5 * (ta + tb)
        c = classify(expr, as_vec3(o) + tm * as_vec3(d), instances)
        if c.inside:
            raw.append(Span(ta, tb, "solid", _label(c.owner, instances)))
        elif c.additive:
            raw.append(Span(ta, tb, "void", _label(c.blocker, instances)))
        # else: plain air — skip.
    return _coalesce(raw)


def _label(iid: str | None, instances: dict[str, Instance]) -> str | None:
    return instances[iid].label if iid is not None and iid in instances else None


def _coalesce(spans: list[Span]) -> list[Span]:
    """Merge adjacent runs.

    Contiguous *solid* runs fuse into one material span regardless of which
    primitive provides each sub-run (a fused part reads as one solid);
    the surviving feature is the first provider. Adjacent
    *void* runs merge only when attributed to the same blocking node, so two
    touching holes stay distinct.
    """
    out: list[Span] = []
    for s in spans:
        if (
            out
            and out[-1].state == s.state
            and abs(out[-1].t_out - s.t_in)
            <= LINEAR_REL_EPS * max(abs(out[-1].t_out), abs(s.t_in))
            and (s.state == "solid" or out[-1].feature == s.feature)
        ):
            prev = out[-1]
            out[-1] = Span(prev.t_in, s.t_out, prev.state, prev.feature)
        else:
            out.append(s)
    return out


def material_intervals(
    expr: Expr, o: Vec3, d: Vec3, instances: dict[str, Instance]
) -> Intervals:
    """Just the solid intervals along the ray (no attribution)."""
    return merge_intervals(
        [
            (s.t_in, s.t_out)
            for s in ray_spans(expr, o, d, instances)
            if s.state == "solid"
        ]
    )
