"""precis_surface.revolution -- smooth targets as surfaces of revolution
(docs/backlog/precis-surface-kernel.md "Slice -- smooth drum").

A meridian is a chain of analytic segments in the ``(r, z)`` half-plane,
revolved about the ``z`` axis. Each segment's shape comes from bending
energy, not authoring:

- **concave bends are catenoids**, ``r = a cosh(u / a)``. Mean curvature is
  zero, so bending energy is zero, and the neck radius ``a`` fixes the
  whole shape. A catenoid turns the normal by a quarter turn between its
  neck and infinity, so it is truncated where the remaining tilt is
  ``cut_quanta`` defects (default half of one) and meets a flat there;
- **convex corners are torus fillets.** ``K > 0`` makes ``H = 0``
  impossible and bending energy falls as the fillet radius ``rho`` grows,
  so the corner takes the largest candidate radius the kept flats allow,
  and refuses if that radius breaks the curvature bound;
- flats and cylinders join them.

**Defect rows.** A lattice with ``q`` disclinations per quarter turn of the
normal (hexagonal: q = 6) can only tilt in steps: after ``j`` defects the
facet's normal makes ``cos alpha = 1 - j / q`` with the axis (the cone
rule). Facet ``j`` is centred where the smooth tilt equals state ``j``, so
the defect row between facets ``j - 1`` and ``j`` sits at ``cos alpha =
1 - (j - 1/2) / q``. On a catenoid ``sin alpha = a / r``; rows land at
``r / a`` = 2.50, 1.51, 1.23, 1.10, 1.03, 1.00 for ``q = 6``. Concave rows
are negative (heptagons), convex rows positive (pentagons). The first row
of a truncated catenoid is the truncation itself.

Pure functions, unit-agnostic (package house rule): every length is in the
caller's unit. Candidate fillet radii and the curvature bound are inputs;
the carbon tables live in :mod:`hexfold.radii`.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

#: ``t in [0, 1]`` -> ``(r, z)`` arrays.
_Param = Callable[
    [NDArray[np.float64]], tuple[NDArray[np.float64], NDArray[np.float64]]
]

_DENSE = 2049


@dataclass(frozen=True)
class Segment:
    """One analytic meridian piece, traversed from ``start`` to ``end``."""

    kind: str  # flat | catenoid | cylinder | fillet
    name: str
    param: _Param = field(repr=False, compare=False)
    #: defect sign along this piece: -1 concave, +1 convex, 0 none
    sign: int = 0

    def at(self, t: NDArray[np.float64]) -> NDArray[np.float64]:
        r, z = self.param(np.asarray(t, dtype=np.float64))
        return np.stack([r, z], axis=-1)

    @property
    def start(self) -> tuple[float, float]:
        p = self.at(np.array([0.0]))[0]
        return float(p[0]), float(p[1])

    @property
    def end(self) -> tuple[float, float]:
        p = self.at(np.array([1.0]))[0]
        return float(p[0]), float(p[1])

    @property
    def length(self) -> float:
        p = self.at(np.linspace(0.0, 1.0, _DENSE))
        return float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())


@dataclass(frozen=True)
class DefectRow:
    """Where row ``k`` of a bend sits on the smooth target."""

    segment: str
    k: int
    sign: int
    r: float
    z: float
    tilt_deg: float


@dataclass(frozen=True)
class Meridian:
    segments: tuple[Segment, ...]
    rows: tuple[DefectRow, ...]
    fillet_radius: float | None = None
    #: largest ``k1 + k2`` over the convex pieces (1/length)
    max_curvature_sum: float = 0.0

    def sample(
        self, ds: float
    ) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.int64]]:
        """Points ``(M, 2)`` at about ``ds`` arc-length spacing, their
        normal tilt from the axis (radians), and the segment index.
        Shared segment ends appear once."""
        pts: list[NDArray[np.float64]] = []
        segs: list[NDArray[np.int64]] = []
        for i, seg in enumerate(self.segments):
            n = max(2, math.ceil(seg.length / ds) + 1)
            t = _arc_uniform(seg, n)
            p = seg.at(t)
            if pts:
                p, t = p[1:], t[1:]
            pts.append(p)
            segs.append(np.full(len(p), i, dtype=np.int64))
        points = np.concatenate(pts)
        return points, _tilt(points), np.concatenate(segs)


def _arc_uniform(seg: Segment, n: int) -> NDArray[np.float64]:
    """Parameters ``t`` that space ``n`` points evenly in arc length."""
    t = np.linspace(0.0, 1.0, _DENSE)
    p = seg.at(t)
    s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))])
    return np.asarray(np.interp(np.linspace(0.0, s[-1], n), s, t), dtype=np.float64)


def _tilt(points: NDArray[np.float64]) -> NDArray[np.float64]:
    """Normal tilt from the axis: 0 on a flat, pi/2 on a cylinder."""
    d = np.gradient(points, axis=0)
    return np.asarray(np.arctan2(np.abs(d[:, 1]), np.abs(d[:, 0])), dtype=np.float64)


def row_cosines(q: int = 6) -> list[float]:
    """``cos alpha`` of defect rows ``k = 1..q``, counted from the flat side."""
    return [1.0 - (k - 0.5) / q for k in range(1, q + 1)]


def catenoid_row_radii(a: float, q: int = 6, cut_quanta: float = 0.5) -> list[float]:
    """Radii of the defect rows on a catenoid of neck ``a``, flat side first.
    Rows outside the truncation (tilt below ``cut_quanta``) are dropped."""
    out = []
    for c in row_cosines(q):
        if c > 1.0 - cut_quanta / q + 1e-12:
            continue
        out.append(a / math.sqrt(1.0 - c * c))
    return out


def catenoid_cut_radius(a: float, q: int = 6, cut_quanta: float = 0.5) -> float:
    """Radius where a catenoid of neck ``a`` meets its flat."""
    c = 1.0 - cut_quanta / q
    return a / math.sqrt(1.0 - c * c)


# ---------- segment constructors ----------


def _line(
    kind: str, name: str, p0: tuple[float, float], p1: tuple[float, float]
) -> Segment:
    def f(t: NDArray[np.float64]) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        return p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t

    return Segment(kind, name, f)


def _catenoid(name: str, a: float, z_neck: float, u0: float, u1: float) -> Segment:
    """``r = a cosh(u / a), z = z_neck + u`` for ``u`` from ``u0`` to ``u1``."""

    def f(t: NDArray[np.float64]) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        u = u0 + (u1 - u0) * t
        return a * np.cosh(u / a), z_neck + u

    return Segment("catenoid", name, f, sign=-1)


def _fillet(
    name: str, centre: tuple[float, float], rho: float, phi0: float, phi1: float
) -> Segment:
    def f(t: NDArray[np.float64]) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        phi = phi0 + (phi1 - phi0) * t
        return centre[0] + rho * np.cos(phi), centre[1] + rho * np.sin(phi)

    return Segment("fillet", name, f, sign=+1)


# ---------- the drum ----------


def drum_meridian(
    *,
    neck: float,
    wall_radius: float,
    stalk_length: float,
    wall_height: float,
    sheet_radius: float,
    fillet_candidates: Sequence[float],
    curvature_sum_max: float,
    q: int = 6,
    cut_quanta: float = 0.5,
    min_flat: float = 0.0,
) -> Meridian:
    """A drum on a stalk on a sheet, from the sheet edge to the lid centre.

    Sheet (flat) -> foot (catenoid, neck ``neck``) -> stalk (cylinder) ->
    flare (catenoid) -> floor (flat) -> bottom fillet -> wall (cylinder)
    -> top fillet -> lid (flat). ``wall_height`` runs floor to lid. The
    fillet radius is the largest of ``fillet_candidates`` that leaves at
    least ``min_flat`` of floor and of wall; raises ``ValueError`` when none
    fits or when the chosen corner, the wall or the neck exceeds
    ``curvature_sum_max`` on ``k1 + k2``.
    """
    a, big_r = neck, wall_radius
    r_cut = catenoid_cut_radius(a, q, cut_quanta)
    if sheet_radius <= r_cut:
        raise ValueError(f"sheet radius {sheet_radius:g} inside the foot cut {r_cut:g}")
    for label, r in (("neck", a), ("wall", big_r)):
        if 1.0 / r > curvature_sum_max:
            raise ValueError(f"{label} radius {r:g} exceeds the curvature bound")

    fits = [
        rho
        for rho in fillet_candidates
        if big_r - rho >= r_cut + min_flat and wall_height >= 2.0 * rho + min_flat
    ]
    if not fits:
        raise ValueError(
            f"no fillet fits: floor needs wall radius >= {r_cut + min_flat:g} + rho"
        )
    rho = max(fits)
    h_corner = 1.0 / rho + 1.0 / big_r
    if h_corner > curvature_sum_max:
        raise ValueError(
            f"largest fitting fillet {rho:g} gives k1+k2 {h_corner:.4g} "
            f"> bound {curvature_sum_max:.4g}; widen the drum"
        )

    u_cut = a * math.acosh(r_cut / a)
    z_foot = u_cut  # foot neck height: the sheet sits at z = 0
    z_stalk_top = z_foot + stalk_length
    z_floor = z_stalk_top + u_cut
    z_top = z_floor + wall_height
    cx = big_r - rho
    half = math.pi / 2.0

    segs = (
        _line("flat", "sheet", (sheet_radius, 0.0), (r_cut, 0.0)),
        _catenoid("foot", a, z_foot, -u_cut, 0.0),
        _line("cylinder", "stalk", (a, z_foot), (a, z_stalk_top)),
        _catenoid("flare", a, z_stalk_top, 0.0, u_cut),
        _line("flat", "floor", (r_cut, z_floor), (cx, z_floor)),
        _fillet("bottom", (cx, z_floor + rho), rho, -half, 0.0),
        _line("cylinder", "wall", (big_r, z_floor + rho), (big_r, z_top - rho)),
        _fillet("top", (cx, z_top - rho), rho, 0.0, half),
        _line("flat", "lid", (cx, z_top), (0.0, z_top)),
    )

    rows: list[DefectRow] = []
    cat_r = catenoid_row_radii(a, q, cut_quanta)
    for k, r in enumerate(cat_r, start=1):
        u = a * math.acosh(max(1.0, r / a))
        tilt = math.degrees(math.asin(min(1.0, a / r)))
        rows.append(DefectRow("foot", k, -1, r, z_foot - u, tilt))
        rows.append(DefectRow("flare", k, -1, r, z_stalk_top + u, tilt))
    for k, c in enumerate(row_cosines(q), start=1):
        alpha = math.acos(c)
        tilt = math.degrees(alpha)
        # bottom: tilt 0 at the floor (phi = -90 deg); top: tilt 0 at the lid
        rows.append(
            DefectRow(
                "bottom",
                k,
                +1,
                cx + rho * math.sin(alpha),
                z_floor + rho - rho * math.cos(alpha),
                tilt,
            )
        )
        rows.append(
            DefectRow(
                "top",
                k,
                +1,
                cx + rho * math.sin(alpha),
                z_top - rho + rho * math.cos(alpha),
                tilt,
            )
        )
    rows.sort(key=lambda d: ([s.name for s in segs].index(d.segment), d.k))
    return Meridian(segs, tuple(rows), rho, max(h_corner, 1.0 / a))


# ---------- revolve ----------


def revolve(
    points: NDArray[np.float64], n_theta: int
) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """Revolve a meridian ``(M, 2)`` of ``(r, z)`` about ``z`` into a mesh.

    A point with ``r == 0`` becomes one pole vertex with a triangle fan.
    Triangles wind so normals point away from the side the meridian keeps
    on its left (the drum's outside, traversed sheet edge to lid centre).
    """
    theta = np.linspace(0.0, 2.0 * np.pi, n_theta, endpoint=False)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    verts: list[NDArray[np.float64]] = []
    ring_ids: list[NDArray[np.int64] | int] = []
    nv = 0
    for r, z in points:
        if r <= 1e-12:
            verts.append(np.array([[0.0, 0.0, z]]))
            ring_ids.append(nv)
            nv += 1
        else:
            verts.append(np.stack([r * cos_t, r * sin_t, np.full(n_theta, z)], axis=1))
            ring_ids.append(np.arange(nv, nv + n_theta, dtype=np.int64))
            nv += n_theta
    tris: list[tuple[int, int, int]] = []
    for a_ids, b_ids in itertools.pairwise(ring_ids):
        for j in range(n_theta):
            jn = (j + 1) % n_theta
            if isinstance(b_ids, int):
                if isinstance(a_ids, int):
                    continue
                tris.append((int(a_ids[j]), b_ids, int(a_ids[jn])))
            elif isinstance(a_ids, int):
                tris.append((a_ids, int(b_ids[j]), int(b_ids[jn])))
            else:
                tris.append((int(a_ids[j]), int(b_ids[j]), int(a_ids[jn])))
                tris.append((int(a_ids[jn]), int(b_ids[j]), int(b_ids[jn])))
    return np.concatenate(verts), np.asarray(tris, dtype=np.int64)
