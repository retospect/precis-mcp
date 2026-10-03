"""precis_surface.deviation -- how far a built structure sits from an
authored surface (docs/backlog/hexfold-ideal-surface-then-tile.md, S1).

A scene is a flat sheet, the plane ``z = 0`` of the caller's frame, carrying
axisymmetric features. Each feature is an authored meridian
(:func:`precis_surface.revolution.authored_meridian`) revolved about a
``+z`` axis through its in-plane ``centre``, and owns the points within its
meridian's start radius of that axis. A point no feature owns is measured
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
        return self.meridian.segments[0].start[0]


def _polyline_distance(
    pts: NDArray[np.float64], line: NDArray[np.float64]
) -> NDArray[np.float64]:
    """Exact distance from each ``(r, z)`` point to a polyline."""
    a, b = line[:-1], line[1:]
    ab = b - a
    ab2 = np.maximum((ab * ab).sum(axis=1), 1e-30)
    out = np.empty(len(pts))
    for i in range(0, len(pts), _CHUNK):
        p = pts[i : i + _CHUNK, None, :]
        t = np.clip(((p - a) * ab).sum(axis=2) / ab2, 0.0, 1.0)
        d = p - (a + t[..., None] * ab)
        out[i : i + _CHUNK] = np.sqrt((d * d).sum(axis=2)).min(axis=1)
    return out


def _arc_distance(
    pts: NDArray[np.float64], arc: tuple[float, float, float, float, float]
) -> NDArray[np.float64]:
    """Exact distance from ``(r, z)`` points to a circular arc: radial when
    the point's angle about the centre falls inside the arc, else the
    nearer end."""
    cr, cz, rho, phi0, phi1 = arc
    lo, span = (phi0, phi1 - phi0) if phi1 >= phi0 else (phi1, phi0 - phi1)
    v = pts - np.array([cr, cz])
    ang = np.mod(np.arctan2(v[:, 1], v[:, 0]) - lo, 2.0 * math.pi)
    radial = np.abs(np.hypot(v[:, 0], v[:, 1]) - rho)
    ends = np.array(
        [[math.cos(lo), math.sin(lo)], [math.cos(lo + span), math.sin(lo + span)]]
    )
    ends = np.array([cr, cz]) + rho * ends
    end_d = np.min(np.linalg.norm(pts[:, None, :] - ends[None, :, :], axis=2), axis=1)
    return np.where(ang <= span, radial, end_d)


def _meridian_distance(
    rz: NDArray[np.float64], m: Meridian, ds: float
) -> NDArray[np.float64]:
    """Distance to the meridian: closed form on lines and arcs, a sampled
    polyline (step ``ds``) only on catenoids."""
    best = np.full(len(rz), np.inf)
    for seg in m.segments:
        if seg.arc is not None:
            d = _arc_distance(rz, seg.arc)
        elif seg.kind in ("flat", "cylinder", "cone"):
            d = _polyline_distance(rz, np.array([seg.start, seg.end]))
        else:
            n = max(2, math.ceil(seg.length / ds) + 1)
            d = _polyline_distance(rz, seg.at(np.linspace(0.0, 1.0, n)))
        best = np.minimum(best, d)
    return best


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
    for i, f in enumerate(features):
        for g in features[i + 1 :]:
            gap = math.dist(f.centre, g.centre)
            if gap < f.reach + g.reach:
                raise ValueError(
                    f"features {f.name} and {g.name} overlap: centres {gap:.4g} "
                    f"apart, reaches {f.reach:.4g} + {g.reach:.4g}"
                )
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
