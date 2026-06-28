"""Analytic primitives + the membership contract (ADR 0041 §4).

Every admitted primitive answers, **exact under rigid transform**, the
card the LLM's "eyes" depend on:

* ``contains(p)``     — point membership (bool)
* ``ray_hits(o, d)``  — sorted inside-intervals along a ray
* ``distance(p)``     — signed distance (negative = inside)
* ``aabb()``          — axis-aligned bounds (``±inf`` for a half-space)
* ``faces()``         — planar faces + normals (draft analysis)

Primitives store geometry in a canonical *local* frame; a
:class:`Placed` binds a primitive to a rigid :class:`~precis.cad.vec.Transform`
and answers the same queries in world coordinates by inverse-transforming
the inputs (distances and the ray parameter ``t`` are preserved because
the transform is rigid — ADR 0041 §2).

``section`` (plane ∩ solid → loops) is part of the contract but lands in
the section-probe step; it is intentionally absent here.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from precis.cad.interval import (
    POS_INF,
    Intervals,
    intersect,
    merge_intervals,
    quadratic_le,
)
from precis.cad.vec import (
    LINEAR_EPS,
    Transform,
    Vec3,
    as_vec3,
    vec3,
)

NEG_INF = -POS_INF


@dataclass(frozen=True)
class Face:
    """A planar face: an outward unit ``normal`` and a descriptive ``tag``.

    Curved surfaces (sphere, torus, frustum lateral) are *not* enumerated
    here — they have continuously varying normals; draft analysis treats
    them via the primitive's known slant. ``faces`` therefore returns only
    the planar faces (caps, box sides).
    """

    normal: Vec3
    tag: str


class Primitive(ABC):
    """A solid in its canonical local frame."""

    @abstractmethod
    def contains_local(self, p: Vec3) -> bool: ...

    @abstractmethod
    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals: ...

    @abstractmethod
    def distance_local(self, p: Vec3) -> float: ...

    @abstractmethod
    def aabb_local(self) -> tuple[Vec3, Vec3]: ...

    @abstractmethod
    def faces_local(self) -> list[Face]: ...


# ---------------------------------------------------------------------------
# 2-D / 3-D geometry helpers
# ---------------------------------------------------------------------------


def _dedup_ring(ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop consecutive duplicate vertices (cone/pyramid degeneracies)."""
    out: list[tuple[float, float]] = []
    for v in ring:
        if (
            not out
            or abs(v[0] - out[-1][0]) > LINEAR_EPS
            or abs(v[1] - out[-1][1]) > LINEAR_EPS
        ):
            out.append(v)
    if (
        len(out) > 1
        and abs(out[0][0] - out[-1][0]) <= LINEAR_EPS
        and abs(out[0][1] - out[-1][1]) <= LINEAR_EPS
    ):
        out.pop()
    return out


def _seg_dist_2d(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    """Distance from point ``p`` to segment ``ab`` (2-D)."""
    ab = b - a
    denom = float(ab @ ab)
    if denom <= LINEAR_EPS * LINEAR_EPS:
        return float(np.linalg.norm(p - a))
    t = float((p - a) @ ab) / denom
    t = max(0.0, min(1.0, t))
    proj = a + t * ab
    return float(np.linalg.norm(p - proj))


def signed_dist_convex_poly_2d(
    pt: tuple[float, float], poly: list[tuple[float, float]]
) -> float:
    """Signed distance from ``pt`` to a CCW convex polygon (negative inside).

    Used for surfaces of revolution via the meridian half-plane reduction
    (rho, z): a circular frustum's exact signed distance is the 2-D signed
    distance to its trapezoidal cross-section.
    """
    poly = _dedup_ring(poly)
    p = np.array(pt, dtype=np.float64)
    n = len(poly)
    inside = True
    min_edge = math.inf
    for i in range(n):
        a = np.array(poly[i], dtype=np.float64)
        b = np.array(poly[(i + 1) % n], dtype=np.float64)
        edge = b - a
        # Outward normal for a CCW polygon is (edge.y, -edge.x).
        outward = np.array([edge[1], -edge[0]], dtype=np.float64)
        if float(outward @ (p - a)) > LINEAR_EPS:
            inside = False
        min_edge = min(min_edge, _seg_dist_2d(p, a, b))
    return -min_edge if inside else min_edge


def signed_dist_frustum_meridian(
    rho: float, z: float, rb: float, rt: float, h: float
) -> float:
    """Signed distance of ``(rho, z)`` to a circular frustum's meridian.

    Same idea as :func:`signed_dist_convex_poly_2d`, but the ``rho=0`` edge
    of the trapezoid is the axis of revolution, not a surface - a point on
    the axis is not on the boundary. The inside test uses all four edges
    (the axis half-plane is always satisfied for ``rho >= 0``); the distance
    magnitude is taken only over the three real surfaces (bottom cap,
    lateral wall, top cap).
    """
    poly = [(0.0, 0.0), (rb, 0.0), (rt, h), (0.0, h)]
    p = np.array((rho, z), dtype=np.float64)
    inside = True
    for i in range(len(poly)):
        a = np.array(poly[i], dtype=np.float64)
        b = np.array(poly[(i + 1) % len(poly)], dtype=np.float64)
        edge = b - a
        outward = np.array([edge[1], -edge[0]], dtype=np.float64)
        if float(outward @ (p - a)) > LINEAR_EPS:
            inside = False
    real_edges = ((poly[0], poly[1]), (poly[1], poly[2]), (poly[2], poly[3]))
    min_edge = min(
        _seg_dist_2d(p, np.array(a, dtype=np.float64), np.array(b, dtype=np.float64))
        for a, b in real_edges
    )
    return -min_edge if inside else min_edge


def _dist_point_to_convex_polygon_3d(p: Vec3, verts: list[Vec3], normal: Vec3) -> float:
    """Distance from ``p`` to a planar convex polygon (its bounded face).

    Project onto the face plane; if the projection lands inside the
    polygon the answer is the perpendicular distance, otherwise it is the
    nearest-edge distance. Covers edge/vertex-nearest cases, so a min over
    all faces gives the exact distance to a convex polytope.
    """
    a0 = verts[0]
    signed = float(normal @ (p - a0))
    proj = p - signed * normal
    n = len(verts)
    inside = True
    for i in range(n):
        a = verts[i]
        b = verts[(i + 1) % n]
        edge = b - a
        inward_test = np.cross(normal, edge)
        if float(inward_test @ (proj - a)) < -LINEAR_EPS:
            inside = False
            break
    if inside:
        return abs(signed)
    best = math.inf
    for i in range(n):
        a = verts[i]
        b = verts[(i + 1) % n]
        ab = b - a
        denom = float(ab @ ab)
        if denom <= LINEAR_EPS * LINEAR_EPS:
            best = min(best, float(np.linalg.norm(p - a)))
            continue
        t = max(0.0, min(1.0, float((p - a) @ ab) / denom))
        best = min(best, float(np.linalg.norm(p - (a + t * ab))))
    return best


# ---------------------------------------------------------------------------
# Sphere
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sphere(Primitive):
    """A sphere of radius ``r`` centred at the local origin."""

    r: float

    def contains_local(self, p: Vec3) -> bool:
        p = as_vec3(p)
        return float(p @ p) <= (self.r + LINEAR_EPS) ** 2

    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals:
        o = as_vec3(o)
        d = as_vec3(d)
        a = float(d @ d)
        b = 2.0 * float(o @ d)
        c = float(o @ o) - self.r * self.r
        spans = quadratic_le(a, b, c)
        return merge_intervals(spans)

    def distance_local(self, p: Vec3) -> float:
        return float(np.linalg.norm(as_vec3(p))) - self.r

    def aabb_local(self) -> tuple[Vec3, Vec3]:
        r = self.r
        return vec3(-r, -r, -r), vec3(r, r, r)

    def faces_local(self) -> list[Face]:
        return []


# ---------------------------------------------------------------------------
# Circular frustum: cylinder / cone / truncated cone
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CircularFrustum(Primitive):
    """Axis-aligned (``+z``) circular frustum from ``z=0`` to ``z=h``.

    Radius varies linearly: ``r(z) = rb + (rt - rb)·z/h``. ``rb == rt`` is
    a cylinder, ``rt == 0`` a cone, ``0 < rt < rb`` a truncated cone. The
    side-face slant *is* the draft angle (ADR 0041 §4).
    """

    rb: float
    rt: float
    h: float

    def _k(self) -> float:
        return (self.rt - self.rb) / self.h

    def _radius_at(self, z: float) -> float:
        return self.rb + self._k() * z

    def contains_local(self, p: Vec3) -> bool:
        p = as_vec3(p)
        z = float(p[2])
        if z < -LINEAR_EPS or z > self.h + LINEAR_EPS:
            return False
        rho = math.hypot(float(p[0]), float(p[1]))
        return rho <= self._radius_at(z) + LINEAR_EPS

    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals:
        o = as_vec3(o)
        d = as_vec3(d)
        ox, oy, oz = float(o[0]), float(o[1]), float(o[2])
        dx, dy, dz = float(d[0]), float(d[1]), float(d[2])
        k = self._k()
        # z-slab: 0 <= oz + t·dz <= h
        if abs(dz) <= LINEAR_EPS:
            if oz < -LINEAR_EPS or oz > self.h + LINEAR_EPS:
                return []
            slab: Intervals = [(NEG_INF, POS_INF)]
        else:
            t0 = -oz / dz
            t1 = (self.h - oz) / dz
            slab = [(min(t0, t1), max(t0, t1))]
        # lateral: px² + py² <= (rb + k·z)²
        r0 = self.rb + k * oz
        rd = k * dz
        a = dx * dx + dy * dy - rd * rd
        b = 2.0 * (ox * dx + oy * dy - r0 * rd)
        c = ox * ox + oy * oy - r0 * r0
        lateral = quadratic_le(a, b, c)
        return merge_intervals(intersect(slab, lateral))

    def distance_local(self, p: Vec3) -> float:
        p = as_vec3(p)
        rho = math.hypot(float(p[0]), float(p[1]))
        z = float(p[2])
        return signed_dist_frustum_meridian(rho, z, self.rb, self.rt, self.h)

    def aabb_local(self) -> tuple[Vec3, Vec3]:
        rmax = max(self.rb, self.rt)
        return vec3(-rmax, -rmax, 0.0), vec3(rmax, rmax, self.h)

    def faces_local(self) -> list[Face]:
        faces = [Face(normal=vec3(0.0, 0.0, -1.0), tag="bottom")]
        if self.rt > LINEAR_EPS:
            faces.append(Face(normal=vec3(0.0, 0.0, 1.0), tag="top"))
        return faces


# ---------------------------------------------------------------------------
# Polygonal frustum: box / n-gon prism / pyramid (a convex polytope)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plane:
    n: Vec3  # outward unit normal
    d: float  # material side: n·p <= d


class PolyFrustum(Primitive):
    """Convex polytope frustum between two parallel convex rings.

    ``bottom`` / ``top`` are CCW 2-D vertex rings at ``z=0`` / ``z=h``
    (same vertex count, corresponding indices). ``top`` may collapse to a
    point (pyramid). Represented as the intersection of its face
    half-spaces, so membership and ray-clipping are exact; distance is the
    min over bounded face polygons (exact for a convex polytope).
    """

    def __init__(
        self,
        bottom: list[tuple[float, float]],
        top: list[tuple[float, float]],
        h: float,
    ) -> None:
        if len(bottom) != len(top):
            raise ValueError("bottom and top rings must have equal vertex count")
        self.h = float(h)
        self._bottom = [vec3(x, y, 0.0) for x, y in bottom]
        self._top = [vec3(x, y, h) for x, y in top]
        self._planes, self._faces, self._verts = self._build()

    # -- construction ----------------------------------------------------
    def _build(self) -> tuple[list[_Plane], list[Face], list[Vec3]]:
        verts = self._bottom + self._top
        centroid = sum(verts, vec3(0.0, 0.0, 0.0)) / len(verts)
        faces: list[Face] = []
        planes: list[_Plane] = []
        face_polys: list[tuple[Vec3, list[Vec3]]] = []

        def add_face(poly: list[Vec3], tag: str) -> None:
            # Dedup degenerate (pyramid apex) vertices.
            ring: list[Vec3] = []
            for v in poly:
                if not ring or float(np.linalg.norm(v - ring[-1])) > LINEAR_EPS:
                    ring.append(v)
            if (
                len(ring) >= 2
                and float(np.linalg.norm(ring[0] - ring[-1])) <= LINEAR_EPS
            ):
                ring.pop()
            if len(ring) < 3:
                return
            normal = np.cross(ring[1] - ring[0], ring[2] - ring[0])
            nlen = float(np.linalg.norm(normal))
            if nlen <= LINEAR_EPS:
                return
            normal = normal / nlen
            if float(normal @ (centroid - ring[0])) > 0:
                normal = -normal
            planes.append(_Plane(n=normal, d=float(normal @ ring[0])))
            faces.append(Face(normal=normal, tag=tag))
            face_polys.append((normal, ring))

        n = len(self._bottom)
        add_face(list(self._bottom), "bottom")
        add_face(list(reversed(self._top)), "top")
        for i in range(n):
            j = (i + 1) % n
            add_face(
                [self._bottom[i], self._bottom[j], self._top[j], self._top[i]],
                f"side{i}",
            )
        # Stash the face polygons for exact distance.
        self._face_polys = face_polys
        return planes, faces, verts

    # -- contract --------------------------------------------------------
    def contains_local(self, p: Vec3) -> bool:
        p = as_vec3(p)
        return all(float(pl.n @ p) <= pl.d + LINEAR_EPS for pl in self._planes)

    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals:
        o = as_vec3(o)
        d = as_vec3(d)
        t_lo, t_hi = NEG_INF, POS_INF
        for pl in self._planes:
            nd = float(pl.n @ d)
            num = pl.d - float(pl.n @ o)  # constraint: nd·t <= num
            if abs(nd) <= LINEAR_EPS:
                if num < -LINEAR_EPS:
                    return []  # ray parallel and outside this slab
                continue
            t = num / nd
            if nd > 0:
                t_hi = min(t_hi, t)
            else:
                t_lo = max(t_lo, t)
            if t_lo > t_hi:
                return []
        if t_lo > t_hi:
            return []
        return [(t_lo, t_hi)]

    def distance_local(self, p: Vec3) -> float:
        p = as_vec3(p)
        if self.contains_local(p):
            return -min(pl.d - float(pl.n @ p) for pl in self._planes)
        return min(
            _dist_point_to_convex_polygon_3d(p, ring, normal)
            for normal, ring in self._face_polys
        )

    def aabb_local(self) -> tuple[Vec3, Vec3]:
        arr = np.array(self._verts)
        return as_vec3(arr.min(axis=0)), as_vec3(arr.max(axis=0))

    def faces_local(self) -> list[Face]:
        return list(self._faces)


def box(w: float, d: float, h: float) -> PolyFrustum:
    """A rectangular box ``w × d × h`` (centred in x/y, base at ``z=0``)."""
    hw, hd = w / 2.0, d / 2.0
    ring = [(-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd)]
    return PolyFrustum(ring, list(ring), h)


def regular_prism(n: int, r: float, h: float) -> PolyFrustum:
    """A regular ``n``-gon prism (circumradius ``r``, height ``h``)."""
    ring = _ngon(n, r)
    return PolyFrustum(ring, list(ring), h)


def regular_frustum(n: int, rb: float, rt: float, h: float) -> PolyFrustum:
    """A regular ``n``-gon frustum (bottom circumradius ``rb`` → top ``rt``)."""
    return PolyFrustum(_ngon(n, rb), _ngon(n, rt), h)


def pyramid(n: int, r: float, h: float) -> PolyFrustum:
    """A regular ``n``-gon pyramid (base circumradius ``r`` → apex)."""
    return PolyFrustum(_ngon(n, r), [(0.0, 0.0)] * n, h)


def _ngon(n: int, r: float) -> list[tuple[float, float]]:
    if n < 3:
        raise ValueError("a polygon needs at least 3 sides")
    return [
        (r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Half-space (the chamfer cutting tool)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HalfSpace(Primitive):
    """A half-space: material where ``(p - point)·normal <= 0``.

    The analytic chamfer (ADR 0041 §4) — a planar bevel composes as a
    half-space cut via ``subtract`` / ``intersect``. Unbounded, so its
    AABB carries ``±inf``.
    """

    point: Vec3
    normal: Vec3

    def _unit(self) -> Vec3:
        nrm = as_vec3(self.normal)
        return nrm / float(np.linalg.norm(nrm))

    def contains_local(self, p: Vec3) -> bool:
        return float(self._unit() @ (as_vec3(p) - as_vec3(self.point))) <= LINEAR_EPS

    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals:
        n = self._unit()
        o = as_vec3(o)
        d = as_vec3(d)
        nd = float(n @ d)
        num = float(n @ (as_vec3(self.point) - o))  # n·(o+td-point) <= 0
        if abs(nd) <= LINEAR_EPS:
            return (
                [(NEG_INF, POS_INF)]
                if float(n @ (o - as_vec3(self.point))) <= LINEAR_EPS
                else []
            )
        t = num / nd
        return [(NEG_INF, t)] if nd > 0 else [(t, POS_INF)]

    def distance_local(self, p: Vec3) -> float:
        return float(self._unit() @ (as_vec3(p) - as_vec3(self.point)))

    def aabb_local(self) -> tuple[Vec3, Vec3]:
        return vec3(NEG_INF, NEG_INF, NEG_INF), vec3(POS_INF, POS_INF, POS_INF)

    def faces_local(self) -> list[Face]:
        return [Face(normal=self._unit(), tag="cut")]


# ---------------------------------------------------------------------------
# Torus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Torus(Primitive):
    """A torus: major radius ``R`` (axis ``+z``), minor radius ``r``."""

    R: float
    r: float

    def contains_local(self, p: Vec3) -> bool:
        p = as_vec3(p)
        rho = math.hypot(float(p[0]), float(p[1]))
        return (rho - self.R) ** 2 + float(p[2]) ** 2 <= (self.r + LINEAR_EPS) ** 2

    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals:
        o = as_vec3(o)
        d = as_vec3(d)
        ox, oy, oz = (float(x) for x in o)
        dx, dy, dz = (float(x) for x in d)
        R2, r2 = self.R * self.R, self.r * self.r
        sum_d = dx * dx + dy * dy + dz * dz
        e = ox * ox + oy * oy + oz * oz - (R2 + r2)
        f = ox * dx + oy * dy + oz * dz
        four_r = 4.0 * R2
        c4 = sum_d * sum_d
        c3 = 4.0 * sum_d * f
        c2 = 2.0 * sum_d * e + 4.0 * f * f + four_r * dz * dz
        c1 = 4.0 * f * e + 2.0 * four_r * oz * dz
        c0 = e * e - four_r * (r2 - oz * oz)
        roots = np.roots([c4, c3, c2, c1, c0])
        ts = sorted(float(z.real) for z in roots if abs(z.imag) <= 1e-7)
        if not ts:
            return []
        spans: Intervals = []
        for i in range(len(ts) - 1):
            mid = 0.5 * (ts[i] + ts[i + 1])
            if self.contains_local(o + mid * d):
                spans.append((ts[i], ts[i + 1]))
        return merge_intervals(spans)

    def distance_local(self, p: Vec3) -> float:
        p = as_vec3(p)
        rho = math.hypot(float(p[0]), float(p[1]))
        return math.hypot(rho - self.R, float(p[2])) - self.r

    def aabb_local(self) -> tuple[Vec3, Vec3]:
        outer = self.R + self.r
        return vec3(-outer, -outer, -self.r), vec3(outer, outer, self.r)

    def faces_local(self) -> list[Face]:
        return []


# ---------------------------------------------------------------------------
# Placed — a primitive bound to a world pose
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Placed:
    """A primitive bound to a rigid world :class:`Transform`.

    World queries inverse-transform the input into the primitive's local
    frame. The transform is rigid, so the ray parameter ``t`` and all
    distances carry through unchanged.
    """

    prim: Primitive
    xform: Transform

    def contains(self, p: Vec3) -> bool:
        return self.prim.contains_local(self.xform.to_local_point(as_vec3(p)))

    def ray_hits(self, o: Vec3, d: Vec3) -> Intervals:
        lo = self.xform.to_local_point(as_vec3(o))
        ld = self.xform.to_local_dir(as_vec3(d))
        return self.prim.ray_hits_local(lo, ld)

    def distance(self, p: Vec3) -> float:
        return self.prim.distance_local(self.xform.to_local_point(as_vec3(p)))

    def faces(self) -> list[Face]:
        out: list[Face] = []
        for f in self.prim.faces_local():
            out.append(Face(normal=self.xform.apply_dir(f.normal), tag=f.tag))
        return out

    def aabb(self) -> tuple[Vec3, Vec3]:
        lo, hi = self.prim.aabb_local()
        if not (np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))):
            return vec3(NEG_INF, NEG_INF, NEG_INF), vec3(POS_INF, POS_INF, POS_INF)
        corners = np.array(
            [
                [lo[0], lo[1], lo[2]],
                [hi[0], lo[1], lo[2]],
                [lo[0], hi[1], lo[2]],
                [hi[0], hi[1], lo[2]],
                [lo[0], lo[1], hi[2]],
                [hi[0], lo[1], hi[2]],
                [lo[0], hi[1], hi[2]],
                [hi[0], hi[1], hi[2]],
            ]
        )
        world = (self.xform.R @ corners.T).T + self.xform.t
        return as_vec3(world.min(axis=0)), as_vec3(world.max(axis=0))
