"""precis_surface.deviation -- how far a built structure sits from an
authored surface (docs/backlog/hexfold-ideal-surface-then-tile.md, S1).

A scene is a flat sheet, the plane ``z = 0`` of the caller's frame, carrying
axisymmetric features. Each feature is an authored meridian
(:func:`precis_surface.revolution.authored_meridian`) revolved about a
``+z`` axis through its in-plane ``centre``, and owns the points within its
full authored radial extent about that axis, including a sphere wider than
the meridian's initial radius. A point no feature owns is measured
against the sheet, as ``|z|``. Feature discs must not overlap: one point,
one surface.

This is the judge the tiler is measured by. The surface is fixed and the
atoms are compared to it, so a tiling that bends the target shows up here as
distance, not as a different target. Pure numpy, unit-agnostic.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .revolution import Meridian

_CHUNK = 512


@dataclass(frozen=True)
class Feature:
    name: str
    centre: tuple[float, float]
    meridian: Meridian

    @property
    def reach(self) -> float:
        """Full target extent, not the initial sheet-to-feature radius.

        Lines and convex cosh catenoids attain their maximum at an endpoint;
        circular arcs may also attain it at angle zero within their sweep.
        Use analytic extrema so an overhanging sphere is never clipped by
        its foot, and ownership/overlap checks do not depend on a sample grid.
        """
        reach = 0.0
        for segment in self.meridian.segments:
            reach = max(reach, segment.start[0], segment.end[0])
            if segment.arc is not None:
                cr, _cz, radius, phi0, phi1 = segment.arc
                lo, hi = sorted((phi0, phi1))
                if math.ceil(lo / math.tau) * math.tau <= hi:
                    reach = max(reach, cr + radius)
        return reach


_Foot = tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]


def _polyline_foot(pts: NDArray[np.float64], line: NDArray[np.float64]) -> _Foot:
    """Nearest point on a polyline for each ``(r, z)`` point: (foot, unit
    normal of the segment it lies on, distance)."""
    a, b = line[:-1], line[1:]
    ab = b - a
    ab2 = np.maximum((ab * ab).sum(axis=1), 1e-30)
    seg_n = np.column_stack([-ab[:, 1], ab[:, 0]]) / np.sqrt(ab2)[:, None]
    foot = np.empty_like(pts)
    nrm = np.empty_like(pts)
    dist = np.empty(len(pts))
    for i in range(0, len(pts), _CHUNK):
        p = pts[i : i + _CHUNK, None, :]
        t = np.clip(((p - a) * ab).sum(axis=2) / ab2, 0.0, 1.0)
        q = a + t[..., None] * ab
        d = np.sqrt(((p - q) ** 2).sum(axis=2))
        k = d.argmin(axis=1)
        rows = np.arange(len(k))
        foot[i : i + _CHUNK] = q[rows, k]
        nrm[i : i + _CHUNK] = seg_n[k]
        dist[i : i + _CHUNK] = d[rows, k]
    return foot, nrm, dist


def _arc_foot(
    pts: NDArray[np.float64], arc: tuple[float, float, float, float, float]
) -> _Foot:
    """Nearest point on a circular arc: radial when the point's angle about
    the centre falls inside the arc, else the nearer end."""
    cr, cz, rho, phi0, phi1 = arc
    lo, span = (phi0, phi1 - phi0) if phi1 >= phi0 else (phi1, phi0 - phi1)
    v = pts - np.array([cr, cz])
    ang = np.mod(np.arctan2(v[:, 1], v[:, 0]) - lo, 2.0 * math.pi)
    inside = ang <= span
    # outside the span: the nearer end, by angular distance
    to_end = np.where(ang - span < 2.0 * math.pi - ang, lo + span, lo)
    phi = np.where(inside, lo + ang, to_end)
    nrm = np.column_stack([np.cos(phi), np.sin(phi)])
    foot = np.array([cr, cz]) + rho * nrm
    dist = np.hypot(*(pts - foot).T)
    return foot, nrm, dist


def _meridian_foot(rz: NDArray[np.float64], m: Meridian, ds: float) -> _Foot:
    """Nearest point on the meridian, its unit normal and the distance:
    closed form on lines and arcs, a sampled polyline (step ``ds``) only on
    catenoids."""
    foot = np.zeros_like(rz)
    nrm = np.zeros_like(rz)
    best = np.full(len(rz), np.inf)
    for seg in m.segments:
        if seg.arc is not None:
            f, n, d = _arc_foot(rz, seg.arc)
        elif seg.kind in ("flat", "cylinder", "cone"):
            f, n, d = _polyline_foot(rz, np.array([seg.start, seg.end]))
        else:
            k = max(2, math.ceil(seg.length / ds) + 1)
            f, n, d = _polyline_foot(rz, seg.at(np.linspace(0.0, 1.0, k)))
        win = d < best
        foot[win], nrm[win], best[win] = f[win], n[win], d[win]
    return foot, nrm, best


def _meridian_distance(
    rz: NDArray[np.float64], m: Meridian, ds: float
) -> NDArray[np.float64]:
    """Distance to the meridian (see :func:`_meridian_foot`)."""
    return _meridian_foot(rz, m, ds)[2]


def _check_disjoint(features: Sequence[Feature]) -> None:
    for i, f in enumerate(features):
        for g in features[i + 1 :]:
            gap = math.dist(f.centre, g.centre)
            if gap < f.reach + g.reach:
                raise ValueError(
                    f"features {f.name} and {g.name} overlap: centres {gap:.4g} "
                    f"apart, reaches {f.reach:.4g} + {g.reach:.4g}"
                )


def surface_foot(
    points: NDArray[np.float64],
    features: Sequence[Feature],
    ds: float,
    z_offset: float = 0.0,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Each ``(N, 3)`` point's nearest point on the scene surface and the
    unit surface normal there, both ``(N, 3)`` in the caller's frame: the
    ``hexfold.stick`` tether's input. Same ownership, ``z_offset`` and
    ``ds`` rules as :func:`surface_distance`.
    """
    _check_disjoint(features)
    pts = np.asarray(points, dtype=np.float64)
    foot = pts.copy()
    foot[:, 2] = z_offset
    nrm = np.zeros_like(pts)
    nrm[:, 2] = 1.0
    for f in features:
        dx, dy = pts[:, 0] - f.centre[0], pts[:, 1] - f.centre[1]
        r = np.hypot(dx, dy)
        mine = r <= f.reach
        if not mine.any():
            continue
        rz = np.column_stack([r[mine], pts[mine, 2] - z_offset])
        fr, nr, _ = _meridian_foot(rz, f.meridian, ds)
        safe = np.where(r[mine] > 1e-12, r[mine], 1.0)
        c, s = dx[mine] / safe, dy[mine] / safe
        foot[mine] = np.column_stack(
            [
                f.centre[0] + fr[:, 0] * c,
                f.centre[1] + fr[:, 0] * s,
                fr[:, 1] + z_offset,
            ]
        )
        nrm[mine] = np.column_stack([nr[:, 0] * c, nr[:, 0] * s, nr[:, 1]])
    return foot, nrm


def surface_distance(
    points: NDArray[np.float64],
    features: Sequence[Feature],
    ds: float,
    z_offset: float = 0.0,
) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """Distance from each ``(N, 3)`` point to the scene surface, and the index
    of the feature that owns it (-1: the flat sheet).

    ``z_offset`` is subtracted from every ``z`` first: a rigid shift is the
    only alignment the judge does -- no fitted rotation or scale, which
    would let it co-optimise the surface again. ``ds`` samples catenoid
    segments only (error O(ds^2 / a)); lines and arcs are exact.

    Raises ``ValueError`` when two features' discs overlap.
    """
    _check_disjoint(features)
    pts = np.asarray(points, dtype=np.float64) - np.array([0.0, 0.0, z_offset])
    dist = np.abs(pts[:, 2]).copy()
    owner = np.full(len(pts), -1, dtype=np.int64)
    for k, f in enumerate(features):
        r = np.hypot(pts[:, 0] - f.centre[0], pts[:, 1] - f.centre[1])
        mine = r <= f.reach
        if not mine.any():
            continue
        dist[mine] = _meridian_distance(
            np.column_stack([r[mine], pts[mine, 2]]), f.meridian, ds
        )
        owner[mine] = k
    return dist, owner


def summary(
    dist: NDArray[np.float64],
    owner: NDArray[np.int64],
    features: Sequence[Feature],
) -> dict[str, dict[str, float | int]]:
    """Per region (``"sheet"`` and each feature): atom count, mean, 95th
    percentile and max distance."""
    out: dict[str, dict[str, float | int]] = {}
    for k, name in [(-1, "sheet")] + [(i, f.name) for i, f in enumerate(features)]:
        d = dist[owner == k]
        if d.size == 0:
            continue
        out[name] = {
            "atoms": int(d.size),
            "mean": float(d.mean()),
            "p95": float(np.percentile(d, 95)),
            "max": float(d.max()),
        }
    return out
