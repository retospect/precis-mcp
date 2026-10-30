"""Bulk integrals — volume & centroid.

Everything else in the kernel is exact under the rigid-only invariant. The
bulk integrals are a numerical *quadrature*, but not a 3-D Monte-Carlo dart
throw: we integrate the **exact** solid extent along a 2-D grid of parallel
rays. Each ray is folded through the CSG expression — per-leaf boundary
crossings split it into runs, and the folded signed distance (sign-exact
everywhere, see :mod:`precis.cad.fold`) classifies each run's midpoint —
so cuts and overlaps are honoured exactly and the only error is the 2-D
quadrature over the ray grid, vastly smaller than sampling a volume
fraction, and O(grid²) exact ray casts instead of hundreds of thousands of
pointwise classifications.

The whole grid is evaluated vectorised: for each leaf the boundary
crossings of all ``grid²`` rays come from one batched search (a min-search
+ bisection on ``distance_np`` for the convex primitives, whose ray
section is a single interval; the scalar ``ray_hits`` for the rest), and
every run midpoint is classified by a single folded ``expr_sdf_np`` call —
the folded field's zero level-set is exactly the material boundary, so the
batched sign test returns the same inside/air verdicts the old per-ray
:class:`~precis.cad.fold.classify` loop did (up to the per-leaf epsilon
band, far below the quadrature error).

The result is still ``sampled`` (a grid quadrature, not closed form), and
``rel_err`` is a Richardson estimate from a coarser grid so the LLM never
mistakes it for exact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from precis.cad.fold import Diff, Expr, Instance, Inter, Leaf, Union, expr_sdf_np
from precis.cad.graph import Design
from precis.cad.primitives import (
    CircularFrustum,
    HalfSpace,
    Placed,
    PolyFrustum,
    Sphere,
)
from precis.cad.vec import LINEAR_REL_EPS, Vec3, as_vec3, vec3


@dataclass(frozen=True)
class BulkResult:
    """A quadrature bulk estimate.

    ``sampled`` is always ``True`` for this tier (a ray-grid quadrature);
    ``rel_err`` is a Richardson relative-error estimate (|V_fine − V_coarse| /
    V_fine). ``samples`` is the number of rays cast (``grid²``); ``grid``
    is the grid side actually used (explicit or auto-derived), ``None``
    for results built outside :func:`volume`.
    """

    volume: float
    centroid: Vec3
    samples: int
    rel_err: float
    grid: int | None = None
    sampled: bool = True


def _expr_bound(
    e: Expr, design: Design
) -> tuple[NDArray[np.float64], NDArray[np.float64]] | None:
    """AABB of the material ``e`` can produce, or ``None`` when no finite
    bound exists (every reachable leaf unbounded).

    ``Union`` unions its parts' bounds; ``Diff`` keeps its base's (a cut
    only removes material); ``Inter`` *intersects* its parts' bounds —
    the result sits inside every part, so the meet is a strictly tighter
    additive bound than the leaf union (an ``intersect(component, tool)``
    probe's rays only ever cover the tool-sized overlap). A part with no
    finite bound contributes no constraint to an ``Inter`` and is skipped
    in a ``Union``, matching the old leaf-level rule of ignoring
    non-finite AABBs.
    """
    if isinstance(e, Leaf):
        lo, hi = design.instances[e.iid].placed.aabb()
        if np.all(np.isfinite(lo)) and np.all(np.isfinite(hi)):
            return np.asarray(lo, dtype=np.float64), np.asarray(hi, dtype=np.float64)
        return None
    if isinstance(e, Union):
        boxes = [b for part in e.parts if (b := _expr_bound(part, design)) is not None]
        if not boxes:
            return None
        return (
            np.min(np.array([b[0] for b in boxes]), axis=0),
            np.max(np.array([b[1] for b in boxes]), axis=0),
        )
    if isinstance(e, Inter):
        boxes = [b for part in e.parts if (b := _expr_bound(part, design)) is not None]
        if not boxes:
            return None
        lo = np.max(np.array([b[0] for b in boxes]), axis=0)
        hi = np.min(np.array([b[1] for b in boxes]), axis=0)
        # Parts that never touch meet empty — the raw meet inverts
        # (lo > hi). Collapse each violated axis to its midpoint so
        # callers see an explicit zero-size box, never an inverted one;
        # the quadrature then spans zero material and reports volume 0.
        mid = 0.5 * (lo + hi)
        return np.minimum(lo, mid), np.maximum(hi, mid)
    if isinstance(e, Diff):
        return _expr_bound(e.base, design)
    raise TypeError(f"unknown expr node: {e!r}")


def expr_aabb(design: Design, expr: Expr) -> tuple[Vec3, Vec3]:
    """AABB bounding the material of ``expr`` — the tight additive bound.

    A ``Diff``'s cutters are NOT walked: a cut only removes material, so
    the base's bound already bounds the result, and letting an over-long
    cutting tool (a bolt hole drilled through from outside the part)
    widen the box shifted :func:`volume`'s ray grid off the part's own
    edges — a 40 mm tool through a 20 mm part mis-measured the PART by
    ±1 % (one grid column at each face), which read as a ±30 % error on
    the hole it was cutting. An ``Inter`` intersects its parts' bounds
    for the same reason — the material sits inside every part; parts that
    never touch give a zero-size box at the would-be meet, never an
    inverted ``lo > hi`` box.
    """
    bound = _expr_bound(expr, design)
    if bound is None:
        raise ValueError("cannot bound an empty / unbounded expression")
    lo, hi = bound
    return as_vec3(lo), as_vec3(hi)


def _grid_for(samples: int) -> int:
    """Map the legacy ``samples`` (3-D point budget) to a 2-D ray-grid side.

    Kept so existing callers passing ``samples=`` still tune accuracy the way
    they expect (more budget → finer grid) without casting hundreds of
    thousands of rays. ``grid² ≈ samples/20``, clamped to a sane range.
    """
    return int(min(200, max(48, round(math.sqrt(max(samples, 1)) / 4.5))))


#: Legacy point budget for ``volume()`` calls that pass neither ``grid=``
#: nor ``samples=`` — only ``expr=`` probes auto-scale (a component volume
#: is a measurement, not a presence check, so it keeps the finer grid).
_DEFAULT_SAMPLES = 200_000

#: Ray cells across the measured AABB per unit of the smallest feature —
#: ~24 cells on the smallest part keeps percent-level volume accuracy while
#: a small probe stops paying for the full grid. ``_auto_grid`` clamps to
#: [``_AUTO_GRID_MIN``, ``_AUTO_GRID_MAX``].
_AUTO_CELLS_PER_FEATURE = 24
_AUTO_GRID_MIN = 12
_AUTO_GRID_MAX = 48


def _expr_leaf_iids(expr: Expr) -> list[str]:
    """Every unique leaf instance id reachable from ``expr``, stable order."""
    iids: list[str] = []
    seen: set[str] = set()

    def walk(e: Expr) -> None:
        if isinstance(e, Leaf):
            if e.iid not in seen:
                seen.add(e.iid)
                iids.append(e.iid)
        for child in getattr(e, "parts", ()):
            walk(child)
        base = getattr(e, "base", None)
        if base is not None:
            walk(base)
        for c in getattr(e, "cutters", ()):
            walk(c)

    walk(expr)
    return iids


def _auto_grid(design: Design, expr: Expr, lo: Vec3, hi: Vec3) -> int:
    """Default ray-grid side scaled to the measured AABB.

    The pitch is the smallest reachable leaf's x/y extent divided by
    ``_AUTO_CELLS_PER_FEATURE`` — the smallest feature gets ~24 cells, so
    relative quadrature resolution is scale-invariant — and the grid is
    that pitch over the probe AABB's x/y extent, clamped to
    [``_AUTO_GRID_MIN``, ``_AUTO_GRID_MAX``]. A probe AABB smaller than
    its smallest feature (a partial overlap) slides to the floor. When no
    finite leaf bound exists the old default is kept.
    """
    extent = max(float(hi[0] - lo[0]), float(hi[1] - lo[1]))
    feat = math.inf
    for iid in _expr_leaf_iids(expr):
        alo, ahi = design.instances[iid].placed.aabb()
        if np.all(np.isfinite(alo)) and np.all(np.isfinite(ahi)):
            feat = min(feat, float(ahi[0] - alo[0]), float(ahi[1] - alo[1]))
    if not (math.isfinite(feat) and feat > 0.0 and extent > 0.0):
        return _AUTO_GRID_MAX
    grid = math.ceil(extent * _AUTO_CELLS_PER_FEATURE / feat)
    return int(min(_AUTO_GRID_MAX, max(_AUTO_GRID_MIN, grid)))


#: Primitives whose signed distance is *convex along any line*: the
#: inside-set on a ray is then a single interval, so a vectorised
#: min-search + bisection on ``distance_np`` recovers both boundary
#: crossings exactly — the same roots ``ray_hits`` solves analytically —
#: without a per-ray Python call. Everything else (torus: two intervals on
#: a radial ray; field / relief / rounded: arbitrary profiles) keeps the
#: scalar ``ray_hits`` path, culled to rays inside the leaf's x/y AABB.
_CONVEX_LEAVES = (Sphere, CircularFrustum, PolyFrustum, HalfSpace)


def _convex_ray_bounds(
    placed: Placed,
    xs: NDArray[np.float64],
    ys: NDArray[np.float64],
    o_z: float,
    ta: float,
    tb: float,
) -> tuple[NDArray[np.intp], NDArray[np.float64], NDArray[np.float64]]:
    """Boundary crossings of vertical rays through a *convex* leaf.

    ``xs``/``ys`` are the ray origins' x/y columns; the rays run ``+z``
    from ``z = o_z`` over the window ``t ∈ [ta, tb]``. Returns
    ``(sel, t_in, t_out)``: the indices of rays whose distance dips to
    ``<= 0`` inside the window, and each such ray's lower / upper crossing
    (the window edge itself where the interval extends past it — the ray
    is inside at the edge, so the bound sits there rather than being
    dropped).

    For a convex signed distance the inside-set on a line is one
    interval: find any inside point (the window midpoint, else a
    golden-section minimum), then bisect each side for the single root —
    the same roots ``ray_hits`` solves analytically, batched over rays.
    """
    n = int(xs.size)
    order = np.arange(n)

    def f(sel: NDArray[np.intp], ts: NDArray[np.float64]) -> NDArray[np.float64]:
        pts = np.empty((sel.size, 3), dtype=np.float64)
        pts[:, 0] = xs[sel]
        pts[:, 1] = ys[sel]
        pts[:, 2] = o_z + ts
        return placed.distance_np(pts)

    fa = f(order, np.full(n, ta))
    fb = f(order, np.full(n, tb))
    mid = np.full(n, 0.5 * (ta + tb))
    fm = f(order, mid)
    tmin = mid.copy()
    fmin = fm.copy()

    sub = np.flatnonzero(fmin > 0.0)
    if sub.size:
        # Midpoint is outside — a tilted leaf can still dip below zero
        # off-centre; golden-section the minimum before giving up.
        lg = np.full(sub.size, ta)
        hg = np.full(sub.size, tb)
        for _ in range(50):
            span = hg - lg
            c = hg - span * 0.6180339887498949
            d = lg + span * 0.6180339887498949
            go_right = f(sub, c) > f(sub, d)
            lg = np.where(go_right, c, lg)
            hg = np.where(go_right, hg, d)
        tg = 0.5 * (lg + hg)
        fg = f(sub, tg)
        better = fg < fmin[sub]
        hit = sub[better]
        tmin[hit] = tg[better]
        fmin[hit] = fg[better]

    inside = fmin <= 0.0
    t_in = np.full(n, np.nan)
    t_out = np.full(n, np.nan)

    # A ray inside at a window edge crossed at-or-below it: for a bounded
    # leaf ``[ta, tb]`` is its own z-AABB so the crossing *is* the edge
    # (a bottom cap reads ``f(ta) == 0``); for an unbounded leaf the real
    # crossing is below the window and ``ta`` is the earliest bound that
    # can matter. Either way the edge itself is the emitted bound.
    t_in[inside & (fa <= 0.0)] = ta
    t_out[inside & (fb <= 0.0)] = tb

    lsel = np.flatnonzero(inside & (fa > 0.0))
    if lsel.size:
        lo_t = np.full(lsel.size, ta)
        hi_t = tmin[lsel].copy()
        for _ in range(48):
            m = 0.5 * (lo_t + hi_t)
            pos = f(lsel, m) > 0.0
            lo_t = np.where(pos, m, lo_t)
            hi_t = np.where(pos, hi_t, m)
        t_in[lsel] = 0.5 * (lo_t + hi_t)

    rsel = np.flatnonzero(inside & (fb > 0.0))
    if rsel.size:
        lo_t = tmin[rsel].copy()
        hi_t = np.full(rsel.size, tb)
        for _ in range(48):
            m = 0.5 * (lo_t + hi_t)
            pos = f(rsel, m) > 0.0
            lo_t = np.where(pos, lo_t, m)
            hi_t = np.where(pos, m, hi_t)
        t_out[rsel] = 0.5 * (lo_t + hi_t)

    return np.flatnonzero(inside), t_in, t_out


#: Folded-SDF evaluations per chunk in ``_integrate`` — a pathological
#: expr (hundreds of leaves) could otherwise materialise a very large
#: midpoint array in one call.
_SDF_CHUNK = 500_000


def _integrate(
    expr: Expr,
    instances: dict[str, Instance],
    lo: Vec3,
    hi: Vec3,
    grid: int,
) -> tuple[float, Vec3]:
    """Volume + centroid by integrating exact solid length along a ``grid×grid``
    array of ``+z`` rays over the ``x/y`` cross-section of the AABB."""
    if hi[0] <= lo[0] or hi[1] <= lo[1] or hi[2] <= lo[2]:
        # Degenerate bound — an ``Inter`` of parts that never touch meets
        # empty, so the expr holds no material.
        return 0.0, vec3(0.0, 0.0, 0.0)
    dx = (hi[0] - lo[0]) / grid
    dy = (hi[1] - lo[1]) / grid
    cell_area = dx * dy
    xs = lo[0] + (np.arange(grid) + 0.5) * dx
    ys = lo[1] + (np.arange(grid) + 0.5) * dy
    xs_col = np.repeat(xs, grid)
    ys_col = np.tile(ys, grid)
    # Start every ray just below the solid — by one z-span, not a fixed
    # ``1.0``: an absolute offset swamps the coordinates' own float64
    # precision once the solid is nanometre-scale (``1.0 + 1e-9`` keeps
    # only ~7 significant digits), and silently degrades the quadrature.
    # Self-relative to the AABB's own extent instead, so the offset stays
    # the same *order of magnitude* as the coordinates whatever the scale.
    z_span = float(hi[2] - lo[2])
    margin = (
        z_span if z_span > 0.0 else max(float(hi[0] - lo[0]), float(hi[1] - lo[1]), 1.0)
    )
    o_z = float(lo[2]) - margin
    z_top = float(hi[2])
    d = vec3(0.0, 0.0, 1.0)

    # Per-ray boundary candidates: every leaf crossing in the z window.
    # Crossings below ``o_z`` are dropped — a folded-solid run starting
    # below the window pairs with the first real bound and classifies air
    # anyway, so it never contributed to the sum.
    ray_idx_parts: list[NDArray[np.intp]] = []
    t_parts: list[NDArray[np.float64]] = []
    for iid in _expr_leaf_iids(expr):
        placed = instances[iid].placed
        alo, ahi = placed.aabb()
        in_xy = (
            (xs_col >= float(alo[0]))
            & (xs_col <= float(ahi[0]))
            & (ys_col >= float(alo[1]))
            & (ys_col <= float(ahi[1]))
        )
        idx = np.flatnonzero(in_xy)
        if idx.size == 0:
            continue
        if isinstance(placed.prim, _CONVEX_LEAVES):
            ta = max(float(alo[2]), o_z) - o_z
            tb = min(float(ahi[2]), z_top) - o_z
            if tb <= ta:
                continue
            sel, t_in, t_out = _convex_ray_bounds(
                placed, xs_col[idx], ys_col[idx], o_z, ta, tb
            )
            for ts in (t_in[sel], t_out[sel]):
                finite = np.isfinite(ts)
                if np.any(finite):
                    ray_idx_parts.append(idx[sel[finite]])
                    t_parts.append(ts[finite])
        else:
            extra_idx: list[int] = []
            extra_t: list[float] = []
            for i in idx:
                o = vec3(float(xs_col[i]), float(ys_col[i]), o_z)
                for a, b in placed.ray_hits(o, d):
                    for t in (a, b):
                        if math.isfinite(t) and t >= 0.0:
                            extra_idx.append(int(i))
                            extra_t.append(float(t))
            if extra_idx:
                ray_idx_parts.append(np.asarray(extra_idx))
                t_parts.append(np.asarray(extra_t))

    if not t_parts:
        return 0.0, vec3(0.0, 0.0, 0.0)

    r_all = np.concatenate(ray_idx_parts)
    t_all = np.concatenate(t_parts)
    order = np.lexsort((t_all, r_all))
    r_s = r_all[order]
    t_s = t_all[order]
    same_ray = r_s[1:] == r_s[:-1]
    r_run = r_s[:-1][same_ray]
    ta_run = t_s[:-1][same_ray]
    tb_run = t_s[1:][same_ray]
    tol = LINEAR_REL_EPS * np.maximum(np.abs(ta_run), np.abs(tb_run))
    keep = (tb_run - ta_run) > tol
    r_run = r_run[keep]
    ta_run = ta_run[keep]
    tb_run = tb_run[keep]

    # Folded classification of every run midpoint in one (or chunked)
    # ``expr_sdf_np`` — sign-exact, so ``<= 0`` is the same inside verdict
    # the scalar ``classify`` fold returned.
    t_mid = 0.5 * (ta_run + tb_run)
    inside = np.zeros(t_mid.size, dtype=bool)
    for start in range(0, t_mid.size, _SDF_CHUNK):
        sl = slice(start, start + _SDF_CHUNK)
        pts = np.empty((t_mid[sl].size, 3), dtype=np.float64)
        pts[:, 0] = xs_col[r_run[sl]]
        pts[:, 1] = ys_col[r_run[sl]]
        pts[:, 2] = o_z + t_mid[sl]
        inside[sl] = expr_sdf_np(expr, pts, instances) <= 0.0

    if not np.any(inside):
        return 0.0, vec3(0.0, 0.0, 0.0)

    r_in = r_run[inside]
    lengths = (tb_run - ta_run)[inside]
    z_mid = o_z + t_mid[inside]
    length_sum = float(lengths.sum())

    vol = length_sum * cell_area
    cx = float((xs_col[r_in] * lengths).sum()) / length_sum
    cy = float((ys_col[r_in] * lengths).sum()) / length_sum
    cz = float((z_mid * lengths).sum()) / length_sum
    return vol, vec3(cx, cy, cz)


def volume(
    design: Design,
    *,
    component: str | None = None,
    expr: Expr | None = None,
    grid: int | None = None,
    samples: int | None = None,
    seed: int = 0,  # accepted for back-compat; quadrature is deterministic
) -> BulkResult:
    """Volume + centroid by exact ray-interval quadrature.

    Casts a ``grid×grid`` array of ``+z`` rays over the part's AABB and, for
    each ray, integrates the exact solid length of the folded expression
    (so subtractions and overlaps are honoured exactly along the ray).
    ``grid`` overrides the resolution — always honoured; otherwise a
    ``samples`` budget maps through :func:`_grid_for`, and with neither
    given an ``expr=`` probe's grid auto-scales to the probe AABB and
    smallest feature (:func:`_auto_grid`) — presence/absence and
    percent-level overlap volumes do not need the measurement grid a
    named component still gets. An auto-picked grid that finds no
    material (or under 1 % of the probe-AABB volume — a thin lens can sit
    between the coarse grid lines) retries once at ``_AUTO_GRID_MAX``.
    ``rel_err`` is a Richardson estimate against a coarser grid.

    ``expr`` measures an arbitrary expression over ``design``'s instances
    (a probe such as ``intersect(component, tool)``) instead of a named
    component or the whole; the two selectors are mutually exclusive.
    """
    if expr is not None and component is not None:
        raise ValueError("volume(): pass component= or expr=, not both")
    probe = expr is not None
    if expr is None:
        expr = design.components[component] if component else design.whole()
    lo, hi = expr_aabb(design, expr)
    auto = False
    if grid is not None:
        n = grid
    elif samples is not None:
        n = _grid_for(samples)
    elif probe:
        n = _auto_grid(design, expr, lo, hi)
        auto = True
    else:
        n = _grid_for(_DEFAULT_SAMPLES)

    vol, centroid = _integrate(expr, design.instances, lo, hi, n)
    if auto and n < _AUTO_GRID_MAX:
        aabb_vol = float(hi[0] - lo[0]) * float(hi[1] - lo[1]) * float(hi[2] - lo[2])
        if vol == 0.0 or vol < 0.01 * aabb_vol:
            # A thin overlap lens falls between the auto grid's ray
            # columns: rerun once at the ceiling so the auto grid never
            # reports 0 where the old fixed grid found material.
            n = _AUTO_GRID_MAX
            vol, centroid = _integrate(expr, design.instances, lo, hi, n)
    n_coarse = max(8, round(n / 1.5))
    vol_coarse, _ = _integrate(expr, design.instances, lo, hi, n_coarse)
    rel_err = abs(vol - vol_coarse) / vol if vol > 0 else 1.0

    return BulkResult(
        # NOT rounded to a fixed decimal count: that was an absolute
        # mm³-scale noise trim (round(vol, 4) silently zeroed any SI-metre
        # volume below 1e-4 m³ = 100,000 mm³ post units-policy-cutover,
        # i.e. every physically-normal small part). `rel_err` stays rounded
        # — it's dimensionless, so a fixed decimal count is scale-safe.
        volume=vol,
        centroid=centroid,
        samples=n * n,
        rel_err=round(rel_err, 5),
        grid=n,
    )


#: Cells in the sign-sampling grid :func:`volume_field` integrates over —
#: the bounded counterpart of the ray quadrature for designs whose leaves
#: price ``ray_hits`` iteratively (a ``Rounded`` leaf runs a golden-section
#: minimum plus two bisections per ray, ~100+ scalar ``distance_local``
#: calls each, so :func:`volume`'s 200² + 134² ray grids cost millions of
#: SDF evaluations — minutes, and uninterruptible mid-cast: the residual
#: unbounded work behind gr529278's ``view='validate'`` hang). One
#: vectorized :func:`~precis.cad.fold.expr_sdf_np` batch is bounded by
#: construction.
FIELD_VOLUME_CELLS = 250_000


def _sign_integrate(
    expr: Expr,
    instances: dict[str, Instance],
    lo: Vec3,
    hi: Vec3,
    cells: int,
) -> tuple[float, Vec3, int]:
    """Inside-fraction volume + centroid over ``cells`` sign samples on a
    uniform grid spanning ``[lo, hi]`` — :func:`volume_field`'s quadrature
    core, run once at ``cells`` and once coarser for the ``rel_err``
    estimate."""
    span = np.asarray(hi, dtype=np.float64) - np.asarray(lo, dtype=np.float64)
    box_vol = float(np.prod(span))
    pitch = (box_vol / cells) ** (1.0 / 3.0)
    n = np.maximum(np.ceil(span / pitch).astype(np.int64), 1)
    axes = [lo[i] + (np.arange(int(n[i])) + 0.5) * (span[i] / n[i]) for i in range(3)]
    pts = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).reshape(-1, 3)
    inside = expr_sdf_np(expr, pts, instances) <= 0.0
    count = int(np.count_nonzero(inside))
    centroid = as_vec3(pts[inside].mean(axis=0)) if count else vec3(0.0, 0.0, 0.0)
    return count * (box_vol / len(pts)), centroid, len(pts)


def volume_field(
    design: Design,
    *,
    component: str | None = None,
    expr: Expr | None = None,
    box: tuple[Vec3, Vec3] | None = None,
    cells: int = FIELD_VOLUME_CELLS,
) -> BulkResult:
    """Volume + centroid by inside-fraction on a bounded sign grid — the
    field-backend counterpart of :func:`volume`.

    :func:`volume` is exact-per-ray and cheap on analytic leaves, but on a
    leaf whose ``ray_hits`` is itself a search — ``Rounded`` (golden
    section + two bisections) or a sampled ``Field`` — each of its ~58k
    rays costs ~100+ scalar SDF evaluations: minutes of work no
    caller-side deadline can interrupt (gr529278's residual
    ``view='validate'`` hang, found after the mesh pitch was capped). This
    estimator instead samples the folded sign field once, vectorized
    (:func:`~precis.cad.fold.expr_sdf_np`), at the centres of a uniform
    ``cells``-capped grid spanning ``box`` — the expr's additive AABB by
    default, or a tighter caller-chosen region (a cut probe passes the
    *cutter's* box: it bounds the material the intersect can hold). The
    volume is the inside fraction times the box volume; the centroid is
    the inside samples' mean.

    Boundary error is ~half a surface cell — the right tier for
    "is there material" gates and display volumes, not for micron-exact
    quadrature. ``rel_err`` is the same fine-vs-coarse estimate
    :func:`volume` reports. ``expr``/``component`` select as in
    :func:`volume`."""
    if expr is not None and component is not None:
        raise ValueError("volume_field(): pass component= or expr=, not both")
    if expr is None:
        expr = design.components[component] if component else design.whole()
    lo, hi = expr_aabb(design, expr) if box is None else box
    lo = as_vec3(lo)
    hi = as_vec3(hi)
    if float(np.prod(np.asarray(hi) - np.asarray(lo))) <= 0.0:
        return BulkResult(
            volume=0.0, centroid=vec3(0.0, 0.0, 0.0), samples=0, rel_err=1.0
        )
    vol, centroid, samples = _sign_integrate(expr, design.instances, lo, hi, cells)
    vol_coarse, _, _ = _sign_integrate(
        expr, design.instances, lo, hi, max(64, cells // 8)
    )
    rel_err = abs(vol - vol_coarse) / vol if vol > 0 else 1.0
    return BulkResult(
        volume=vol, centroid=centroid, samples=samples, rel_err=round(rel_err, 5)
    )
