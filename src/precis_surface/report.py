"""One measured report over a surface mesh — the invariants, as numbers.

Every quantity here was computed ad-hoc at least once during slice 1's
build, by hand, in a throwaway ``uv run python -c``. That is the reason
this module exists: a claim like "no degenerate triangles" or "zero
misoriented faces" is worth exactly as much as the command that produced
it, and a report nobody can regenerate is not evidence. Collecting the
measurements in one place makes "did it work" a table anyone can re-run
rather than a sentence someone wrote.

It has a second job, and the two are deliberately the same object. The
remesh loop (docs/backlog/precis-surface-kernel.md, slice 1's sequencing
note) may not enable curvature-driven degree optimisation until the
discretisation noise has been cleaned up -- 5-20% of a raw
marching-cubes mesh's vertices carry spurious *positive* angle defect,
up to nearly a full 2*pi, and heptagons placed against that are placed
against noise. :attr:`SurfaceReport.positive_defect_frac` is the
threshold the loop reads. The instrument that catches an over-confident
report and the instrument that drives the algorithm are one and the
same.

House rules as for :mod:`precis.structsolve`: pure functions over
passed-in arrays, no store access, unit-agnostic.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, fields

import numpy as np
from numpy.typing import NDArray

from precis_surface.periodic_mesh import PeriodicMesh, weld_indices, welded_euler

#: A triangle whose area falls below this fraction of the nominal cell
#: area is a sliver. Slivers are ordinary marching-cubes output; the
#: threshold is deliberately far below any real triangle (the measured
#: gap is many orders of magnitude) so the count is unambiguous.
SLIVER_REL_AREA = 1.0e-6

#: Angle defect above this counts as positive. A discrete surface's
#: defects are exactly zero only on a flat patch, so a plain ``> 0`` test
#: would count float noise.
DEFECT_EPS = 1.0e-9

GradFn = Callable[[NDArray[np.float64]], NDArray[np.float64]]


@dataclass(frozen=True)
class SurfaceReport:
    """Measured state of one surface mesh. Every field is a number that
    can be re-derived from the mesh — none is a verdict."""

    # topology
    n_verts: int
    n_edges: int
    n_faces: int
    chi: int
    edge_manifold_closed: bool

    # the rigorous identity: total angle defect == 2*pi*chi
    defect_sum: float
    gauss_bonnet_err: float

    # geometry quality
    sliver_frac: float
    min_area: float
    median_area: float
    edge_len_mean: float
    edge_len_cv: float

    # discretisation noise — the remesh loop's switch reads this
    positive_defect_frac: float
    max_defect: float
    min_defect: float

    # orientation
    misoriented: int
    orientation_fallback_count: int

    # dual preview: vertex degrees become ring sizes
    degree_histogram: dict[int, int]

    def table(self) -> str:
        """Render as one line per field — what a caller pastes into a
        report instead of asserting a verdict."""
        out = []
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, float):
                out.append(f"{f.name:28s} {v:.6g}")
            else:
                out.append(f"{f.name:28s} {v}")
        return "\n".join(out)


def _welded(pm: PeriodicMesh) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """Vertices and triangles with periodic duplicates identified, so a
    per-vertex quantity is counted once per quotient vertex."""
    rep = weld_indices(len(pm.verts), pm.wrap)
    uniq, inv = np.unique(rep[pm.tris], return_inverse=True)
    return pm.verts[uniq], inv.reshape(pm.tris.shape).astype(np.int64)


def _welded_angle_defect(
    verts: NDArray[np.float64],
    tris: NDArray[np.int64],
    welded_tris: NDArray[np.int64],
    n_welded: int,
) -> NDArray[np.float64]:
    """Per-quotient-vertex angle defect, with angles measured on the
    ORIGINAL (correctly-positioned) triangles and accumulated into welded
    slots.

    Calling :func:`~precis_surface.curvature.gaussian_curvature` on welded
    *coordinates* is wrong: a boundary triangle would be measured against
    its representative's position on the far side of the cell. The total
    would still come out at ``2*pi*chi`` -- that total is a purely
    combinatorial identity (``2*pi*V - pi*F == 2*pi*chi`` for any closed
    triangle mesh, whatever its coordinates), so it cannot detect the
    error. The per-vertex *distribution*, which is what the remesh loop
    reads, would be silently wrong."""
    ang_sum = np.zeros(n_welded, dtype=np.float64)
    for i in range(3):
        a = verts[tris[:, i]]
        b = verts[tris[:, (i + 1) % 3]]
        c = verts[tris[:, (i + 2) % 3]]
        u = b - a
        v = c - a
        nu = np.linalg.norm(u, axis=1)
        nv = np.linalg.norm(v, axis=1)
        ok = (nu > 0.0) & (nv > 0.0)
        cos = np.ones(len(tris), dtype=np.float64)
        cos[ok] = np.einsum("ij,ij->i", u[ok], v[ok]) / (nu[ok] * nv[ok])
        np.add.at(ang_sum, welded_tris[:, i], np.arccos(np.clip(cos, -1.0, 1.0)))
    return 2.0 * np.pi - ang_sum


def surface_report(pm: PeriodicMesh, grad: GradFn | None = None) -> SurfaceReport:
    """Measure ``pm``. ``grad`` is the level set's gradient; when given,
    every triangle's normal is checked against it and the disagreement
    count is reported (orientation cannot be checked without knowing
    which way is out)."""
    verts, tris = pm.mesh
    n = round(float(np.max(np.abs(pm.cell))) / _grid_pitch(pm))
    nominal = 0.5 * (float(np.max(np.abs(pm.cell))) / max(n, 1)) ** 2

    e1 = verts[tris[:, 1]] - verts[tris[:, 0]]
    e2 = verts[tris[:, 2]] - verts[tris[:, 0]]
    cross = np.cross(e1, e2)
    area = 0.5 * np.linalg.norm(cross, axis=1)

    wv, wt = _welded(pm)
    defect = _welded_angle_defect(verts, tris, wt, len(wv))
    V, E, F, chi = welded_euler(pm)

    # Edge lengths come from the ORIGINAL coordinates. Welded indices are
    # correct for topology and wrong for geometry: a triangle touching the
    # high face refers to its low-face representative, so measuring across
    # welded coordinates yields cell-spanning edges.
    lens = np.concatenate(
        [
            np.linalg.norm(verts[tris[:, a]] - verts[tris[:, b]], axis=1)
            for a, b in ((0, 1), (1, 2), (2, 0))
        ]
    )
    lens = lens[lens > 0.0]

    deg = np.bincount(wt.ravel(), minlength=len(wv))
    hist = {int(d): int(c) for d, c in zip(*np.unique(deg, return_counts=True))}

    misoriented = -1
    if grad is not None:
        centroid = verts[tris].mean(axis=1)
        misoriented = int(np.sum(np.einsum("ij,ij->i", cross, grad(centroid)) <= 0.0))

    from precis_surface.periodic_mesh import is_edge_manifold_closed

    return SurfaceReport(
        n_verts=V,
        n_edges=E,
        n_faces=F,
        chi=chi,
        edge_manifold_closed=bool(is_edge_manifold_closed(pm)),
        defect_sum=float(defect.sum()),
        gauss_bonnet_err=abs(float(defect.sum()) - 2.0 * math.pi * chi),
        sliver_frac=float(np.mean(area < SLIVER_REL_AREA * nominal)),
        min_area=float(area.min()),
        median_area=float(np.median(area)),
        edge_len_mean=float(lens.mean()),
        edge_len_cv=float(lens.std() / lens.mean()),
        positive_defect_frac=float(np.mean(defect > DEFECT_EPS)),
        max_defect=float(defect.max()),
        min_defect=float(defect.min()),
        misoriented=misoriented,
        orientation_fallback_count=int(pm.orientation_fallback_count),
        degree_histogram=hist,
    )


def _grid_pitch(pm: PeriodicMesh) -> float:
    """Recover the sampling pitch from the mesh's own vertex spacing —
    the report must not be told the grid, or it could be told wrong."""
    verts = pm.verts
    span = float(np.max(np.abs(pm.cell)))
    # marching-cubes vertices lie on grid edges, so the smallest nonzero
    # coordinate gap along an axis is the pitch (or a divisor of it).
    xs = np.unique(np.round(verts[:, 0] / span * 1e9).astype(np.int64))
    if len(xs) < 2:
        return span
    gaps = np.diff(xs) / 1e9 * span
    return float(np.min(gaps[gaps > 1e-12]))
