"""Mating geometry shared by the ``flatpack_box`` se op and the
store-aware checks: which panels' slabs share a joint region, the region
in each panel's own (u,v) plane, and a panel's material projected into
its mate's frame.

Every panel frame is axis-aligned (``eu``/``ev``/``en`` are the world
axes, possibly permuted — never rotated otherwise), so a joint region is
an axis-aligned box in world and a rectangle in each panel's (u,v), and
a material column ``rect × n∈[0,t]`` projects to a rectangle in the
mate's plane. That is what makes the exact checks in
:mod:`precis_se.flatpack.checks` cheap: no 3-D solids, only rectangles.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise, product

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from precis_se.flatpack.panels import Frame, Panel

#: A shared volume below this in any axis is a face touch, not a joint.
_EPS = 1e-9


@dataclass(frozen=True)
class Mate:
    """Two panels whose slabs share a positive-volume box (the joint
    region ``lo``..``hi`` in world), with that region as a rectangle in
    each panel's own (u,v) plane."""

    a: Panel
    b: Panel
    region_a: Polygon
    region_b: Polygon
    lo: tuple[float, float, float]
    hi: tuple[float, float, float]


def _slab_bounds(panel: Panel) -> tuple[list[float], list[float]]:
    """World bbox of the panel's slab: its outline bounds × ``n∈[0,t]``."""
    minx, miny, maxx, maxy = panel.polygon.bounds
    pts = [
        panel.frame.to_world(u, v, n)
        for u, v, n in product((minx, maxx), (miny, maxy), (0.0, panel.thickness))
    ]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi


def _box_in_uv(
    lo: tuple[float, ...] | list[float],
    hi: tuple[float, ...] | list[float],
    frame: Frame,
) -> Polygon | None:
    """A world box as a rectangle in the frame's (u,v) plane, or ``None``
    when the box has no volume."""
    if min(hi[i] - lo[i] for i in range(3)) <= _EPS:
        return None
    pts = [
        frame.to_local((x, y, z))
        for x, y, z in product((lo[0], hi[0]), (lo[1], hi[1]), (lo[2], hi[2]))
    ]
    us = [p[0] for p in pts]
    vs = [p[1] for p in pts]
    return box(min(us), min(vs), max(us), max(vs))


def mated_pairs(panels: list[Panel] | tuple[Panel, ...]) -> list[Mate]:
    """Every pair of panels whose extruded slabs overlap in volume — the
    seams where one panel's fingers or tabs enter the other's gaps or
    slots. Face-only contact (a straight/butt joint) does not count."""
    bounds = [_slab_bounds(p) for p in panels]
    out: list[Mate] = []
    for i, a in enumerate(panels):
        lo_a, hi_a = bounds[i]
        for j, b in enumerate(panels[i + 1 :], start=i + 1):
            lo_b, hi_b = bounds[j]
            lo = (
                max(lo_a[0], lo_b[0]),
                max(lo_a[1], lo_b[1]),
                max(lo_a[2], lo_b[2]),
            )
            hi = (
                min(hi_a[0], hi_b[0]),
                min(hi_a[1], hi_b[1]),
                min(hi_a[2], hi_b[2]),
            )
            region_a = _box_in_uv(lo, hi, a.frame)
            if region_a is None:
                continue
            region_b = _box_in_uv(lo, hi, b.frame)
            assert region_b is not None  # same box, same rule
            out.append(Mate(a, b, region_a, region_b, lo, hi))
    return out


def rect_decompose(poly: Polygon) -> list[tuple[float, float, float, float]]:
    """The rectangles tiling a rectilinear polygon, as ``(x0, y0, x1,
    y1)`` bounds — one band of the polygon's own vertex heights at a
    time. Used to mint the panel's cad ``add``/``cut`` boxes and to
    project material into a mate's frame."""
    if poly.is_empty:
        return []
    if not isinstance(poly, Polygon):
        parts: list[tuple[float, float, float, float]] = []
        for g in getattr(poly, "geoms", []):
            if isinstance(g, Polygon):
                parts += rect_decompose(g)
        return parts
    ys = sorted(
        {y for _, y in poly.exterior.coords[:-1]}
        | {y for ring in poly.interiors for _, y in ring.coords[:-1]}
    )
    minx, _, maxx, _ = poly.bounds
    out: list[tuple[float, float, float, float]] = []
    for y0, y1 in pairwise(ys):
        band = poly.intersection(box(minx - 1.0, y0, maxx + 1.0, y1))
        # A band cut can return a GeometryCollection (material rects plus
        # zero-area boundary slivers) — keep the filled parts only.
        geoms = band.geoms if hasattr(band, "geoms") else (band,)
        for g in geoms:
            if not isinstance(g, Polygon) or g.area <= 0:
                continue
            x0, by0, x1, by1 = g.bounds
            out.append((x0, by0, x1, by1))
    return out


def project_into(
    src: Panel, rect: tuple[float, float, float, float], dst: Panel
) -> tuple[float, float, float, float]:
    """The (u,v) bounds in ``dst``'s frame of ``src``'s material column
    ``rect × n∈[0,t]`` — the projection of a slab-aligned box, exact
    because every frame is axis-aligned."""
    x0, y0, x1, y1 = rect
    us: list[float] = []
    vs: list[float] = []
    for u, v, n in product((x0, x1), (y0, y1), (0.0, src.thickness)):
        q = dst.frame.to_local(src.frame.to_world(u, v, n))
        us.append(q[0])
        vs.append(q[1])
    return (min(us), min(vs), max(us), max(vs))


def material_in_mate(src: Panel, dst: Panel, region_src: Polygon) -> Polygon:
    """``src``'s material inside the joint region, seen in ``dst``'s
    (u,v) plane: fingers and tabs reach the polygon they must fit."""
    own = src.polygon.intersection(region_src)
    if own.is_empty:
        return Polygon()
    rects = rect_decompose(own)
    return unary_union([box(*project_into(src, r, dst)) for r in rects])
