"""Analytic primitives + the membership contract.

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
the transform is rigid).

``section`` (plane ∩ solid → loops) is part of the contract but lands in
the section-probe step; it is intentionally absent here.

**Vectorised distance.** Every primitive also answers
``distance_local_np(P)`` for an ``(N, 3)`` point array — the same signed
distance as the scalar ``distance_local`` (they agree to float64 round-
off; ``tests/test_cad_rounding.py`` pins it), evaluated once for the
whole array. The field-export backend (:mod:`precis.cad.fieldmesh`)
samples millions of points through it; the probe layer keeps the scalar
form. The base class falls back to a Python loop over the scalar method
so a new primitive is correct before it is fast.

**Rounding** (:class:`Rounded`) is a leaf wrapper, not a field trick:
the wrapped primitive is *built shrunk* by ``r`` on every side and its
exact SDF is offset by ``-r`` — ``d(p) = sd(p, params - r) - r`` — so the
zero set is the Minkowski sum of the shrunk solid with a ball of radius
``r`` (edges → cylinders, corners → sphere caps, planar faces where they
were). See ``docs/backlog/cad-sdf-rounding-and-field-export.md`` for the
contract; :func:`precis.cad.dsl.build` does the per-shape shrink.

**Sampled field** (:class:`Field`) is the one non-analytic leaf: a
float32 signed-distance grid answering the same card through trilinear
lookup inside its box (and box distance + boundary value outside, where
it is not trusted). Its grid-side operations — exact re-distance,
morphology, the SIMP bridge — live in :mod:`precis.cad.fieldops`.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad.interval import (
    POS_INF,
    Intervals,
    intersect,
    merge_intervals,
    quadratic_le,
)
from precis.cad.vec import (
    LINEAR_REL_EPS,
    Transform,
    Vec3,
    aabb_corners,
    as_vec3,
    vec3,
)

NEG_INF = -POS_INF


def _linear_eps(scale: float) -> float:
    """Linear tolerance for a feature of characteristic length ``scale``.

    ``LINEAR_REL_EPS`` fraction of the feature's own governing length —
    the units-policy-cutover replacement for the historical flat
    ``LINEAR_EPS`` (gr335192, gr334785): a per-feature relative band holds
    at Å and at km, where a fixed absolute constant only ever held at the
    one magnitude it was tuned on.
    """
    return LINEAR_REL_EPS * abs(scale)


def _dir_eps(d: Vec3) -> float:
    """Tolerance for "is this ray direction's component ~0" tests.

    A ray direction is caller-supplied and not required to be unit
    (``lengths=False`` at the handler boundary — an agent may hand in
    ``[2, 0, 0]``), so ``n·d`` (or a bare component of ``d``) is a
    *direction-scale*, not feature-scale, quantity: relative to ``d``'s
    own magnitude, not the primitive's governing length. Using the
    primitive's own linear eps here would be dimensionally wrong the same
    way the pre-gr335192 code was — and, post units-policy-cutover, wrong
    in a new way: a nanometre feature's eps would make the ray-parallel
    test never fire for an ordinary unit-length ray direction.
    """
    return LINEAR_REL_EPS * float(np.linalg.norm(as_vec3(d)))


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

    def distance_local_np(self, pts: NDArray[np.float64]) -> NDArray[np.float64]:
        """Signed distance of every row of ``pts`` (``(N, 3)``) — the
        vectorised twin of :meth:`distance_local`. Subclasses override
        with a true array evaluation; this fallback loops."""
        arr = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        return np.array([self.distance_local(row) for row in arr], dtype=np.float64)


# ---------------------------------------------------------------------------
# 2-D / 3-D geometry helpers
# ---------------------------------------------------------------------------


def _dedup_ring(
    ring: list[tuple[float, float]], *, eps: float
) -> list[tuple[float, float]]:
    """Drop consecutive duplicate vertices (cone/pyramid degeneracies)."""
    out: list[tuple[float, float]] = []
    for v in ring:
        if not out or abs(v[0] - out[-1][0]) > eps or abs(v[1] - out[-1][1]) > eps:
            out.append(v)
    if (
        len(out) > 1
        and abs(out[0][0] - out[-1][0]) <= eps
        and abs(out[0][1] - out[-1][1]) <= eps
    ):
        out.pop()
    return out


def _seg_dist_2d(p: np.ndarray, a: np.ndarray, b: np.ndarray, *, eps: float) -> float:
    """Distance from point ``p`` to segment ``ab`` (2-D)."""
    ab = b - a
    denom = float(ab @ ab)
    if denom <= eps * eps:
        return float(np.linalg.norm(p - a))
    t = float((p - a) @ ab) / denom
    t = max(0.0, min(1.0, t))
    proj = a + t * ab
    return float(np.linalg.norm(p - proj))


def signed_dist_convex_poly_2d(
    pt: tuple[float, float],
    poly: list[tuple[float, float]],
    *,
    eps: float | None = None,
) -> float:
    """Signed distance from ``pt`` to a CCW convex polygon (negative inside).

    Used for surfaces of revolution via the meridian half-plane reduction
    (rho, z): a circular frustum's exact signed distance is the 2-D signed
    distance to its trapezoidal cross-section. ``eps`` defaults to
    :func:`_linear_eps` of the polygon's own extent (self-relative) when
    the caller has no better governing length at hand.
    """
    if eps is None:
        extent = max((max(abs(x), abs(y)) for x, y in poly), default=0.0)
        eps = _linear_eps(extent)
    poly = _dedup_ring(poly, eps=eps)
    p = np.array(pt, dtype=np.float64)
    n = len(poly)
    inside = True
    min_edge = math.inf
    for i in range(n):
        a = np.array(poly[i], dtype=np.float64)
        b = np.array(poly[(i + 1) % n], dtype=np.float64)
        edge = b - a
        # Outward normal for a CCW polygon is (edge.y, -edge.x) — same
        # magnitude as ``edge``, i.e. NOT unit. ``outward @ (p - a)`` is
        # therefore |edge| · (true perpendicular signed distance), units
        # length², so it must be compared against ``eps`` scaled by
        # |edge| rather than against the bare (length) epsilon (gr335192).
        outward = np.array([edge[1], -edge[0]], dtype=np.float64)
        elen = float(np.linalg.norm(outward))
        if elen > eps and float(outward @ (p - a)) > eps * elen:
            inside = False
        min_edge = min(min_edge, _seg_dist_2d(p, a, b, eps=eps))
    return -min_edge if inside else min_edge


def signed_dist_frustum_meridian(
    rho: float, z: float, rb: float, rt: float, h: float, *, eps: float | None = None
) -> float:
    """Signed distance of ``(rho, z)`` to a circular frustum's meridian.

    Same idea as :func:`signed_dist_convex_poly_2d`, but the ``rho=0`` edge
    of the trapezoid is the axis of revolution, not a surface - a point on
    the axis is not on the boundary. The inside test uses all four edges
    (the axis half-plane is always satisfied for ``rho >= 0``); the distance
    magnitude is taken only over the three real surfaces (bottom cap,
    lateral wall, top cap). ``eps`` defaults to :func:`_linear_eps` of the
    frustum's own governing length (``max(rb, rt, h)``) — callers that
    already know it (:class:`CircularFrustum`) pass theirs through.
    """
    if eps is None:
        eps = _linear_eps(max(abs(rb), abs(rt), abs(h)))
    poly = [(0.0, 0.0), (rb, 0.0), (rt, h), (0.0, h)]
    p = np.array((rho, z), dtype=np.float64)
    inside = True
    for i in range(len(poly)):
        a = np.array(poly[i], dtype=np.float64)
        b = np.array(poly[(i + 1) % len(poly)], dtype=np.float64)
        edge = b - a
        # Same dimensional hazard as signed_dist_convex_poly_2d above:
        # ``outward`` is edge-magnitude, not unit — normalize the
        # comparison, not just the epsilon's units (gr335192).
        outward = np.array([edge[1], -edge[0]], dtype=np.float64)
        elen = float(np.linalg.norm(outward))
        if elen > eps and float(outward @ (p - a)) > eps * elen:
            inside = False
    real_edges = ((poly[0], poly[1]), (poly[1], poly[2]), (poly[2], poly[3]))
    min_edge = min(
        _seg_dist_2d(
            p, np.array(a, dtype=np.float64), np.array(b, dtype=np.float64), eps=eps
        )
        for a, b in real_edges
    )
    return -min_edge if inside else min_edge


def _seg_dist_2d_np(
    pts: NDArray[np.float64], a: np.ndarray, b: np.ndarray, *, eps: float
) -> NDArray[np.float64]:
    """Row-wise :func:`_seg_dist_2d` — distance from every ``(N, 2)`` row
    of ``pts`` to segment ``ab``."""
    ab = b - a
    denom = float(ab @ ab)
    if denom <= eps * eps:
        return np.linalg.norm(pts - a, axis=1)
    t = np.clip(((pts - a) @ ab) / denom, 0.0, 1.0)
    proj = a + t[:, None] * ab
    return np.linalg.norm(pts - proj, axis=1)


def _signed_dist_frustum_meridian_np(
    rho: NDArray[np.float64],
    z: NDArray[np.float64],
    rb: float,
    rt: float,
    h: float,
    *,
    eps: float,
) -> NDArray[np.float64]:
    """Row-wise :func:`signed_dist_frustum_meridian` — same edges, same
    inside test, same three real surfaces, over ``(N,)`` arrays."""
    poly = [(0.0, 0.0), (rb, 0.0), (rt, h), (0.0, h)]
    pts = np.stack([rho, z], axis=1)
    inside = np.ones(len(pts), dtype=bool)
    for i in range(len(poly)):
        a = np.array(poly[i], dtype=np.float64)
        b = np.array(poly[(i + 1) % len(poly)], dtype=np.float64)
        edge = b - a
        outward = np.array([edge[1], -edge[0]], dtype=np.float64)
        elen = float(np.linalg.norm(outward))
        if elen > eps:
            inside &= ~(((pts - a) @ outward) > eps * elen)
    real_edges = ((poly[0], poly[1]), (poly[1], poly[2]), (poly[2], poly[3]))
    min_edge = np.full(len(pts), np.inf)
    for a2, b2 in real_edges:
        np.minimum(
            min_edge,
            _seg_dist_2d_np(
                pts,
                np.array(a2, dtype=np.float64),
                np.array(b2, dtype=np.float64),
                eps=eps,
            ),
            out=min_edge,
        )
    return np.where(inside, -min_edge, min_edge)


def _dist_point_to_convex_polygon_3d(
    p: Vec3, verts: list[Vec3], normal: Vec3, *, eps: float | None = None
) -> float:
    """Distance from ``p`` to a planar convex polygon (its bounded face).

    Project onto the face plane; if the projection lands inside the
    polygon the answer is the perpendicular distance, otherwise it is the
    nearest-edge distance. Covers edge/vertex-nearest cases, so a min over
    all faces gives the exact distance to a convex polytope. ``eps``
    defaults to :func:`_linear_eps` of the polygon's own vertex extent
    (self-relative) — callers that already have a governing length
    (:class:`PolyFrustum`) pass theirs through.
    """
    if eps is None:
        extent = max((float(np.max(np.abs(as_vec3(v)))) for v in verts), default=0.0)
        eps = _linear_eps(extent)
    a0 = verts[0]
    signed = float(normal @ (p - a0))
    proj = p - signed * normal
    n = len(verts)
    inside = True
    for i in range(n):
        a = verts[i]
        b = verts[(i + 1) % n]
        edge = b - a
        # ``inward_test`` = normal × edge has magnitude |edge| (normal is
        # unit), so the dot below is units length², not length — normalize
        # against the edge's own magnitude before comparing to the linear
        # epsilon (gr335192; same family as signed_dist_frustum_meridian).
        inward_test = np.cross(normal, edge)
        elen = float(np.linalg.norm(inward_test))
        if elen > eps and float(inward_test @ (proj - a)) < -eps * elen:
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
        if denom <= eps * eps:
            best = min(best, float(np.linalg.norm(p - a)))
            continue
        t = max(0.0, min(1.0, float((p - a) @ ab) / denom))
        best = min(best, float(np.linalg.norm(p - (a + t * ab))))
    return best


def _dist_points_to_convex_polygon_3d_np(
    pts: NDArray[np.float64],
    verts: list[Vec3],
    normal: Vec3,
    *,
    eps: float | None = None,
) -> NDArray[np.float64]:
    """Row-wise :func:`_dist_point_to_convex_polygon_3d` over ``(N, 3)``
    ``pts`` — same projection, same inward-edge test, same nearest-edge
    fallback, so the two agree to float64 round-off."""
    if eps is None:
        extent = max((float(np.max(np.abs(as_vec3(v)))) for v in verts), default=0.0)
        eps = _linear_eps(extent)
    a0 = verts[0]
    signed = (pts - a0) @ normal
    proj = pts - signed[:, None] * normal
    n = len(verts)
    inside = np.ones(len(pts), dtype=bool)
    for i in range(n):
        a = verts[i]
        b = verts[(i + 1) % n]
        inward_test = np.cross(normal, b - a)
        elen = float(np.linalg.norm(inward_test))
        if elen > eps:
            inside &= ~(((proj - a) @ inward_test) < -eps * elen)
    out = np.abs(signed)
    if not np.all(inside):
        q = pts[~inside]
        best = np.full(len(q), np.inf)
        for i in range(n):
            a = verts[i]
            b = verts[(i + 1) % n]
            ab = b - a
            denom = float(ab @ ab)
            if denom <= eps * eps:
                d = np.linalg.norm(q - a, axis=1)
            else:
                t = np.clip(((q - a) @ ab) / denom, 0.0, 1.0)
                d = np.linalg.norm(q - (a + t[:, None] * ab), axis=1)
            np.minimum(best, d, out=best)
        out[~inside] = best
    return out


# ---------------------------------------------------------------------------
# Sphere
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sphere(Primitive):
    """A sphere of radius ``r`` centred at the local origin."""

    r: float

    @property
    def _eps(self) -> float:
        return _linear_eps(self.r)

    def contains_local(self, p: Vec3) -> bool:
        p = as_vec3(p)
        return float(p @ p) <= (self.r + self._eps) ** 2

    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals:
        o = as_vec3(o)
        d = as_vec3(d)
        a = float(d @ d)
        b = 2.0 * float(o @ d)
        c = float(o @ o) - self.r * self.r
        spans = quadratic_le(a, b, c)
        return merge_intervals(spans, eps=self._eps)

    def distance_local(self, p: Vec3) -> float:
        return float(np.linalg.norm(as_vec3(p))) - self.r

    def distance_local_np(self, pts: NDArray[np.float64]) -> NDArray[np.float64]:
        arr = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        return np.linalg.norm(arr, axis=1) - self.r

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
    side-face slant *is* the draft angle.
    """

    rb: float
    rt: float
    h: float

    @property
    def _eps(self) -> float:
        return _linear_eps(max(abs(self.rb), abs(self.rt), abs(self.h)))

    def _k(self) -> float:
        return (self.rt - self.rb) / self.h

    def _radius_at(self, z: float) -> float:
        return self.rb + self._k() * z

    def contains_local(self, p: Vec3) -> bool:
        p = as_vec3(p)
        eps = self._eps
        z = float(p[2])
        if z < -eps or z > self.h + eps:
            return False
        rho = math.hypot(float(p[0]), float(p[1]))
        return rho <= self._radius_at(z) + eps

    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals:
        o = as_vec3(o)
        d = as_vec3(d)
        eps = self._eps
        ox, oy, oz = float(o[0]), float(o[1]), float(o[2])
        dx, dy, dz = float(d[0]), float(d[1]), float(d[2])
        k = self._k()
        # z-slab: 0 <= oz + t·dz <= h. ``dz`` is a component of the
        # caller's (not-necessarily-unit) ray direction, so its "is this
        # ~horizontal" test is direction-relative, not feature-relative.
        if abs(dz) <= _dir_eps(d):
            if oz < -eps or oz > self.h + eps:
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
        return merge_intervals(intersect(slab, lateral), eps=eps)

    def distance_local(self, p: Vec3) -> float:
        p = as_vec3(p)
        rho = math.hypot(float(p[0]), float(p[1]))
        z = float(p[2])
        return signed_dist_frustum_meridian(
            rho, z, self.rb, self.rt, self.h, eps=self._eps
        )

    def distance_local_np(self, pts: NDArray[np.float64]) -> NDArray[np.float64]:
        arr = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        rho = np.hypot(arr[:, 0], arr[:, 1])
        return _signed_dist_frustum_meridian_np(
            rho, arr[:, 2], self.rb, self.rt, self.h, eps=self._eps
        )

    def aabb_local(self) -> tuple[Vec3, Vec3]:
        rmax = max(self.rb, self.rt)
        return vec3(-rmax, -rmax, 0.0), vec3(rmax, rmax, self.h)

    def faces_local(self) -> list[Face]:
        faces = [Face(normal=vec3(0.0, 0.0, -1.0), tag="bottom")]
        if self.rt > self._eps:
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
        # The polytope's own governing length (feature size): the largest
        # coordinate magnitude across every raw vertex, plus the height —
        # computed before any eps-dependent culling so it never depends on
        # the tolerance it feeds.
        coords = [
            c for v in (self._bottom + self._top) for c in (float(v[0]), float(v[1]))
        ]
        coords.append(self.h)
        self._scale = max((abs(c) for c in coords), default=0.0)
        self._eps = _linear_eps(self._scale)
        self._planes, self._faces, self._verts = self._build()

    # -- construction ----------------------------------------------------
    def _build(self) -> tuple[list[_Plane], list[Face], list[Vec3]]:
        verts = self._bottom + self._top
        centroid = sum(verts, vec3(0.0, 0.0, 0.0)) / len(verts)
        faces: list[Face] = []
        planes: list[_Plane] = []
        face_polys: list[tuple[Vec3, list[Vec3]]] = []

        eps = self._eps

        def add_face(poly: list[Vec3], tag: str) -> None:
            # Dedup degenerate (pyramid apex) vertices.
            ring: list[Vec3] = []
            for v in poly:
                if not ring or float(np.linalg.norm(v - ring[-1])) > eps:
                    ring.append(v)
            if len(ring) >= 2 and float(np.linalg.norm(ring[0] - ring[-1])) <= eps:
                ring.pop()
            if len(ring) < 3:
                return
            normal = np.cross(ring[1] - ring[0], ring[2] - ring[0])
            nlen = float(np.linalg.norm(normal))
            # nlen is |e1|·|e2|·sin(theta), units length² — compared here
            # against ``eps ** 2`` (also length²), not the bare linear
            # ``eps``, so the admission test is dimensionally sound AND
            # scale-relative (gr335192 fixed both at once for
            # units-policy-cutover: the old ``nlen <= LINEAR_EPS`` compared
            # a length² quantity against an absolute length, which is why
            # a nanometre box's faces — ``nlen ~ 1e-18`` against
            # ``LINEAR_EPS = 1e-6`` — were *always* culled, degenerate or
            # not).
            if nlen <= eps * eps:
                return
            normal = normal / nlen
            if float(normal @ (centroid - ring[0])) > 0:
                # Flipping the normal to face outward turns the ring
                # clockwise about it; reverse the ring too, because the
                # exact-distance routine (_dist_point_to_convex_polygon_3d)
                # assumes CCW-about-normal for its inward-edge test. Left
                # as-is, every cap's interior read as "outside the
                # polygon" and an outside point above/below a box got the
                # distance to the cap's *edge* (8× too large for a point
                # 1 mm under a 36×16 mm box's centre) — masked until
                # rounding made the outside magnitude load-bearing.
                normal = -normal
                ring = list(reversed(ring))
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
        if not planes:
            # Every face was culled as genuinely degenerate (every vertex
            # ring collapsed to <3 distinct points, or every remaining
            # triple was collinear) — a truly zero-volume input, not a
            # scale artefact now that the culling tests are scale-relative
            # (gr335192/units-policy-cutover). Left alone, contains_local()
            # is vacuously True everywhere and distance_local() crashes on
            # min() over nothing — fail loud at construction instead.
            raise ValueError(
                "frustum/box is degenerate: every face collapsed to <3 "
                "distinct vertices or zero area. Check the envelope's "
                "dimensions — a zero or near-zero w/d/h/r param produces "
                "this, at any scale."
            )
        return planes, faces, verts

    # -- contract --------------------------------------------------------
    def contains_local(self, p: Vec3) -> bool:
        p = as_vec3(p)
        return all(float(pl.n @ p) <= pl.d + self._eps for pl in self._planes)

    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals:
        o = as_vec3(o)
        d = as_vec3(d)
        eps = self._eps
        dir_eps = _dir_eps(d)
        t_lo, t_hi = NEG_INF, POS_INF
        for pl in self._planes:
            nd = float(pl.n @ d)
            num = pl.d - float(pl.n @ o)  # constraint: nd·t <= num
            if abs(nd) <= dir_eps:
                if num < -eps:
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

    def distance_local_np(self, pts: NDArray[np.float64]) -> NDArray[np.float64]:
        arr = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        normals = np.array([pl.n for pl in self._planes])  # (F, 3)
        offs = np.array([pl.d for pl in self._planes])  # (F,)
        slack = offs[None, :] - arr @ normals.T  # (N, F): d - n·p
        inside = np.all(slack >= -self._eps, axis=1)
        out = np.empty(len(arr), dtype=np.float64)
        out[inside] = -slack[inside].min(axis=1)
        if not np.all(inside):
            outside = arr[~inside]
            best = np.full(len(outside), np.inf)
            for normal, ring in self._face_polys:
                np.minimum(
                    best,
                    _dist_points_to_convex_polygon_3d_np(outside, ring, normal),
                    out=best,
                )
            out[~inside] = best
        return out

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

    The analytic chamfer — a planar bevel composes as a
    half-space cut via ``subtract`` / ``intersect``. Unbounded, so its
    AABB carries ``±inf``.
    """

    point: Vec3
    normal: Vec3

    def _unit(self) -> Vec3:
        nrm = as_vec3(self.normal)
        return nrm / float(np.linalg.norm(nrm))

    def _eps_at(self, *points: Vec3) -> float:
        """Linear tolerance for a coincidence test with this plane.

        Unbounded (a chamfer cutting tool), so it has no bounded feature
        size of its own — the governing length is the largest offset
        among the plane's own anchor point and whichever query point(s)
        are in play (feature size, else the query's own scale, per the
        units-policy-cutover fallback).
        """
        scale = float(np.linalg.norm(as_vec3(self.point)))
        for pt in points:
            scale = max(scale, float(np.linalg.norm(as_vec3(pt))))
        return _linear_eps(scale)

    def contains_local(self, p: Vec3) -> bool:
        p = as_vec3(p)
        return float(self._unit() @ (p - as_vec3(self.point))) <= self._eps_at(p)

    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals:
        n = self._unit()
        o = as_vec3(o)
        d = as_vec3(d)
        nd = float(n @ d)
        num = float(n @ (as_vec3(self.point) - o))  # n·(o+td-point) <= 0
        if abs(nd) <= _dir_eps(d):
            return (
                [(NEG_INF, POS_INF)]
                if float(n @ (o - as_vec3(self.point))) <= self._eps_at(o)
                else []
            )
        t = num / nd
        return [(NEG_INF, t)] if nd > 0 else [(t, POS_INF)]

    def distance_local(self, p: Vec3) -> float:
        return float(self._unit() @ (as_vec3(p) - as_vec3(self.point)))

    def distance_local_np(self, pts: NDArray[np.float64]) -> NDArray[np.float64]:
        arr = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        return (arr - as_vec3(self.point)) @ self._unit()

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

    @property
    def _eps(self) -> float:
        return _linear_eps(max(abs(self.R), abs(self.r)))

    def contains_local(self, p: Vec3) -> bool:
        p = as_vec3(p)
        rho = math.hypot(float(p[0]), float(p[1]))
        return (rho - self.R) ** 2 + float(p[2]) ** 2 <= (self.r + self._eps) ** 2

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
        # The imaginary-part filter drops numerical-noise roots. ``t``
        # parameterizes ``o + t·d``, so a length-scale tolerance
        # (``self._eps``) translates to ``t``-space by dividing out
        # ``d``'s own magnitude — self-relative the same way ``_dir_eps``
        # is, so the filter holds whether ``d`` is unit or not, and at any
        # torus scale.
        dir_norm = float(np.linalg.norm(d))
        t_eps = self._eps / dir_norm if dir_norm > 0.0 else self._eps
        ts = sorted(float(z.real) for z in roots if abs(z.imag) <= t_eps)
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

    def distance_local_np(self, pts: NDArray[np.float64]) -> NDArray[np.float64]:
        arr = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        rho = np.hypot(arr[:, 0], arr[:, 1])
        return np.hypot(rho - self.R, arr[:, 2]) - self.r

    def aabb_local(self) -> tuple[Vec3, Vec3]:
        outer = self.R + self.r
        return vec3(-outer, -outer, -self.r), vec3(outer, outer, self.r)

    def faces_local(self) -> list[Face]:
        return []


# ---------------------------------------------------------------------------
# Rounded — a convex leaf with every edge/corner rounded to radius r
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rounded(Primitive):
    """A convex primitive with every edge and corner rounded to radius ``r``.

    ``inner`` is the primitive **already built shrunk** by ``r`` on every
    side (the caller — :func:`precis.cad.dsl.build` — does the per-shape
    shrink, because "shrunk by ``r``" means different parameter arithmetic
    for a box, a slanted frustum and an n-gon prism); ``lift`` is the local
    ``z`` offset that puts the shrunk shape where the sharp one's base was
    (``r`` for the base-at-``z=0`` family, ``0`` for a centred shape).
    The rounded solid is then exactly

        d(p) = inner.distance_local(p - (0, 0, lift)) - r

    — the Minkowski sum of ``inner`` with a ball of radius ``r``. Because
    ``inner``'s SDF is an *exact* Euclidean distance the offset is exact
    too: planar faces stay where the sharp solid's were, edges become
    cylinder patches, corners sphere caps, and the bounding box equals
    the sharp solid's (``aabb_local`` reports the *unshrunk* extents).

    Every query goes through :meth:`distance_local` (membership is
    ``d <= eps``); ``ray_hits_local`` exploits convexity — the signed
    distance to a convex body is a convex function along a line, so a
    golden-section minimum followed by two bisections finds the single
    inside interval to the primitive's linear tolerance.
    """

    inner: Primitive
    r: float
    lift: float = 0.0

    def _shift(self) -> Vec3:
        return vec3(0.0, 0.0, self.lift)

    @property
    def _eps(self) -> float:
        lo, hi = self.aabb_local()
        return _linear_eps(float(np.max(np.abs(np.concatenate([lo, hi])))))

    def distance_local(self, p: Vec3) -> float:
        return self.inner.distance_local(as_vec3(p) - self._shift()) - self.r

    def distance_local_np(self, pts: NDArray[np.float64]) -> NDArray[np.float64]:
        arr = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        return self.inner.distance_local_np(arr - self._shift()) - self.r

    def contains_local(self, p: Vec3) -> bool:
        return self.distance_local(p) <= self._eps

    def aabb_local(self) -> tuple[Vec3, Vec3]:
        lo, hi = self.inner.aabb_local()
        s = self._shift()
        return lo + s - self.r, hi + s + self.r

    def faces_local(self) -> list[Face]:
        return self.inner.faces_local()

    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals:
        o = as_vec3(o)
        d = as_vec3(d)
        eps = self._eps
        dir_eps = _dir_eps(d)
        lo, hi = self.aabb_local()
        # Clip the ray to the (padded) bounding box first: outside it the
        # distance is positive by construction, and the box gives the
        # finite bracket the 1-D search below needs.
        t0, t1 = NEG_INF, POS_INF
        for i in range(3):
            oi, di = float(o[i]), float(d[i])
            if abs(di) <= dir_eps:
                if oi < lo[i] - eps or oi > hi[i] + eps:
                    return []
                continue
            ta = (lo[i] - eps - oi) / di
            tb = (hi[i] + eps - oi) / di
            t0 = max(t0, min(ta, tb))
            t1 = min(t1, max(ta, tb))
        if t0 > t1 or not (math.isfinite(t0) and math.isfinite(t1)):
            return []

        def f(t: float) -> float:
            return self.distance_local(o + t * d)

        dnorm = float(np.linalg.norm(d))
        t_tol = eps / dnorm if dnorm > 0.0 else eps
        # Golden-section search for the minimum of the convex f on [t0, t1].
        invphi = (math.sqrt(5.0) - 1.0) / 2.0
        a, b = t0, t1
        c = b - invphi * (b - a)
        e = a + invphi * (b - a)
        fc, fe = f(c), f(e)
        for _ in range(256):
            if b - a <= t_tol:
                break
            if fc < fe:
                b, e, fe = e, c, fc
                c = b - invphi * (b - a)
                fc = f(c)
            else:
                a, c, fc = c, e, fe
                e = a + invphi * (b - a)
                fe = f(e)
        tm = c if fc <= fe else e
        if min(fc, fe) > eps:
            return []
        # Entry root on [t0, tm] (f goes + → ≤0), exit root on [tm, t1].
        if f(t0) <= 0.0:
            t_in = t0
        else:
            lo_t, hi_t = t0, tm
            for _ in range(256):
                if hi_t - lo_t <= t_tol:
                    break
                mid = 0.5 * (lo_t + hi_t)
                if f(mid) > 0.0:
                    lo_t = mid
                else:
                    hi_t = mid
            t_in = hi_t
        if f(t1) <= 0.0:
            t_out = t1
        else:
            lo_t, hi_t = tm, t1
            for _ in range(256):
                if hi_t - lo_t <= t_tol:
                    break
                mid = 0.5 * (lo_t + hi_t)
                if f(mid) > 0.0:
                    hi_t = mid
                else:
                    lo_t = mid
            t_out = lo_t
        return [(t_in, t_out)]


# ---------------------------------------------------------------------------
# Field — a sampled signed-distance grid (the slice-2 leaf)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class Field(Primitive):
    """A sampled signed-distance grid: ``grid[i, j, k]`` is the signed
    distance (negative inside, in the caller's length unit) at local point
    ``origin + (i, j, k) · pitch``; ``pitch`` is isotropic.

    Inside the grid box the distance is the **trilinear interpolant** of
    the eight surrounding samples. Outside it, it is the Euclidean
    distance to the grid box plus the interpolant at the nearest box
    point — positive by construction when the boundary samples are, and
    Lipschitz ≤ √3 either way (an axis-wise slope of at most 1 per sample
    step when the samples are an exact SDF), so the field-export band
    test (:data:`precis.cad.fieldmesh.BAND_SAFETY`) still holds. **The
    field is only trusted inside its own AABB**: its zero set must lie
    inside the grid box (:func:`precis.cad.fieldops.redistance` and
    ``from_density`` pad for this); the outside formula exists so a
    boolean against an analytic leaf is defined everywhere, not to
    extrapolate geometry.

    ``exact`` records whether the samples are an exact Euclidean SDF (the
    output of :func:`precis.cad.fieldops.redistance`) or merely sign-
    correct with the right zero set (an offset, a boolean fold sampled
    onto a grid) — the morphology ops re-distance before offsetting a
    field that is not flagged exact. It is metadata: no query reads it.

    A field is not convex and has no named planar faces (``faces_local``
    is empty — draft analysis sees nothing to report), and rounding it is
    ``fieldops.open``/``close``/``offset``, never ``Rounded`` (refused at
    parse). ``ray_hits_local`` marches the ray through the grid box at
    steps of at most one pitch and bisects each sign change — a feature
    thinner than a pitch between two samples is not resolved, which is
    the same limit the grid itself has.
    """

    grid: NDArray[np.floating[Any]]
    pitch: float
    origin: Vec3
    exact: bool = False

    def __post_init__(self) -> None:
        arr = np.asarray(self.grid)
        if arr.ndim != 3 or min(arr.shape) < 2:
            raise ValueError(
                f"a field grid must be (nx, ny, nz) with every axis >= 2, got "
                f"shape {arr.shape}"
            )
        if not (self.pitch > 0.0 and math.isfinite(self.pitch)):
            raise ValueError(f"field pitch must be a positive length, got {self.pitch}")
        if not np.all(np.isfinite(arr)):
            raise ValueError("a field grid must be finite everywhere")
        object.__setattr__(self, "grid", np.ascontiguousarray(arr, dtype=np.float32))
        object.__setattr__(self, "origin", as_vec3(self.origin))

    @property
    def shape(self) -> tuple[int, int, int]:
        nx, ny, nz = self.grid.shape
        return int(nx), int(ny), int(nz)

    @property
    def _eps(self) -> float:
        lo, hi = self.aabb_local()
        return _linear_eps(float(np.max(np.abs(np.concatenate([lo, hi])))))

    def scaled(self, k: float) -> Field:
        """The same field in another length unit (every length × ``k``)."""
        return Field(
            grid=self.grid * np.float32(k),
            pitch=self.pitch * k,
            origin=self.origin * k,
            exact=self.exact,
        )

    def aabb_local(self) -> tuple[Vec3, Vec3]:
        n = np.array(self.grid.shape, dtype=np.float64) - 1.0
        return self.origin.copy(), self.origin + n * self.pitch

    def faces_local(self) -> list[Face]:
        return []

    def distance_local_np(self, pts: NDArray[np.float64]) -> NDArray[np.float64]:
        arr = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        n = np.array(self.grid.shape, dtype=np.int64)
        q = (arr - self.origin) / self.pitch  # continuous grid coordinates
        qc = np.clip(q, 0.0, (n - 1).astype(np.float64))
        # distance from the point to the grid box (0 inside), in length units
        outside = np.linalg.norm((q - qc) * self.pitch, axis=1)
        i0 = np.minimum(np.floor(qc).astype(np.int64), n - 2)
        f = qc - i0
        g = self.grid
        ix, iy, iz = i0[:, 0], i0[:, 1], i0[:, 2]
        fx, fy, fz = f[:, 0], f[:, 1], f[:, 2]
        c00 = g[ix, iy, iz] * (1 - fx) + g[ix + 1, iy, iz] * fx
        c10 = g[ix, iy + 1, iz] * (1 - fx) + g[ix + 1, iy + 1, iz] * fx
        c01 = g[ix, iy, iz + 1] * (1 - fx) + g[ix + 1, iy, iz + 1] * fx
        c11 = g[ix, iy + 1, iz + 1] * (1 - fx) + g[ix + 1, iy + 1, iz + 1] * fx
        c0 = c00 * (1 - fy) + c10 * fy
        c1 = c01 * (1 - fy) + c11 * fy
        inner = c0 * (1 - fz) + c1 * fz
        return np.asarray(inner, dtype=np.float64) + outside

    def distance_local(self, p: Vec3) -> float:
        return float(self.distance_local_np(as_vec3(p)[None, :])[0])

    def contains_local(self, p: Vec3) -> bool:
        return self.distance_local(p) <= self._eps

    def ray_hits_local(self, o: Vec3, d: Vec3) -> Intervals:
        o = as_vec3(o)
        d = as_vec3(d)
        eps = self._eps
        dir_eps = _dir_eps(d)
        lo, hi = self.aabb_local()
        # Clip to the grid box: outside it the distance is >= the boundary
        # value, which the contract keeps positive, so no material there.
        t0, t1 = NEG_INF, POS_INF
        for i in range(3):
            oi, di = float(o[i]), float(d[i])
            if abs(di) <= dir_eps:
                if oi < lo[i] - eps or oi > hi[i] + eps:
                    return []
                continue
            ta = (lo[i] - eps - oi) / di
            tb = (hi[i] + eps - oi) / di
            t0 = max(t0, min(ta, tb))
            t1 = min(t1, max(ta, tb))
        if t0 > t1 or not (math.isfinite(t0) and math.isfinite(t1)):
            return []
        dnorm = float(np.linalg.norm(d))
        if dnorm <= 0.0:
            return []
        t_tol = eps / dnorm
        # March at <= one pitch per step (in space), all samples at once.
        steps = max(2, math.ceil((t1 - t0) * dnorm / self.pitch) + 1)
        ts = np.linspace(t0, t1, steps)
        vals = self.distance_local_np(o[None, :] + ts[:, None] * d[None, :])
        inside = vals <= 0.0

        def f(t: float) -> float:
            return self.distance_local(o + t * d)

        def bisect(ta: float, tb: float, entering: bool) -> float:
            # f(ta) > 0 >= f(tb) when entering, the reverse when leaving
            lo_t, hi_t = ta, tb
            for _ in range(256):
                if hi_t - lo_t <= t_tol:
                    break
                mid = 0.5 * (lo_t + hi_t)
                if (f(mid) > 0.0) == entering:
                    lo_t = mid
                else:
                    hi_t = mid
            return 0.5 * (lo_t + hi_t)

        spans: Intervals = []
        t_in: float | None = float(ts[0]) if inside[0] else None
        for k in range(1, steps):
            if inside[k] and t_in is None:
                t_in = bisect(float(ts[k - 1]), float(ts[k]), True)
            elif not inside[k] and t_in is not None:
                spans.append((t_in, bisect(float(ts[k - 1]), float(ts[k]), False)))
                t_in = None
        if t_in is not None:
            spans.append((t_in, float(ts[-1])))
        return merge_intervals(spans, eps=t_tol)


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

    def distance_np(self, pts: NDArray[np.float64]) -> NDArray[np.float64]:
        """Signed distance of every row of ``pts`` (``(N, 3)`` world
        points) — the vectorised twin of :meth:`distance`. The rigid
        inverse is applied to the whole array at once
        (``(P - t) @ R`` is ``R.T @ (p - t)`` row-wise)."""
        arr = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
        local = (arr - self.xform.t) @ self.xform.R
        return self.prim.distance_local_np(local)

    def faces(self) -> list[Face]:
        out: list[Face] = []
        for f in self.prim.faces_local():
            out.append(Face(normal=self.xform.apply_dir(f.normal), tag=f.tag))
        return out

    def aabb(self) -> tuple[Vec3, Vec3]:
        lo, hi = self.prim.aabb_local()
        if not (np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))):
            return vec3(NEG_INF, NEG_INF, NEG_INF), vec3(POS_INF, POS_INF, POS_INF)
        corners = np.array(aabb_corners(lo, hi))
        world = (self.xform.R @ corners.T).T + self.xform.t
        return as_vec3(world.min(axis=0)), as_vec3(world.max(axis=0))
