"""precis_surface.curvature — discrete differential-geometry operators on
triangle meshes (docs/backlog/precis-surface-kernel.md "Slice 1 — the dual
route", hexfold spec.md §20.4, [S17] Meyer/Desbrun/Schroder/Barr 2003,
"Discrete Differential-Geometry Operators for Triangulated 2-Manifolds").

Takes the interface contract's bare ``Mesh = (verts, tris)`` tuple
(``verts: (N,3) float64``, ``tris: (M,3) int64``, CCW outward) and nothing
else — no import from sibling modules (``level_set.py``,
``periodic_mesh.py``; a different agent owns those, in flight
concurrently). Pure functions over passed-in arrays, no store access,
unit-agnostic (:mod:`precis.structsolve` house rules): callers feed
metres or bond-lengths, the numbers never know.

Two families of "Gaussian curvature" are exported and must not be
confused:

- :func:`gaussian_curvature` — raw angle defect per vertex (radians,
  dimensionless): ``2*pi - sum(incident angles)`` at an interior vertex,
  ``pi - sum(incident angles)`` at a boundary vertex. This is the
  *integrated* curvature over the vertex's mixed Voronoi cell, and its
  sum over a closed mesh equals ``2*pi*chi`` exactly (the combinatorial
  Gauss-Bonnet identity / Descartes' theorem on angular defect) — the
  cheapest whole-mesh self-check available, and independent of any area
  weighting.
- :func:`gaussian_curvature_density` — the angle defect divided by the
  vertex's mixed Voronoi area, i.e. the actual curvature ``K`` in units
  of 1/length^2, comparable to a continuous surface's Gaussian curvature
  and to :func:`mean_curvature`'s density.

:func:`mean_curvature` is the cotangent-Laplacian / mixed-Voronoi-area
mean curvature normal, signed against the angle-weighted vertex normal
(:func:`vertex_normals`) so that a convex shape with outward-pointing
normals (e.g. a sphere) reports positive ``H = 1/R``.
:func:`principal_curvatures` combines the two density forms
(``k = H +/- sqrt(H^2 - K)``, clamping the discriminant at 0 for
numerical noise) and :func:`curvature_violations` *counts* (never
raises) vertices exceeding a ``kappa_max`` bound for reporting — spec
§20.4's default is ``kappa_max = 1/(2*sigma) = 1/2.84`` in whatever
length unit the caller's mesh uses. :func:`face_gaussian_curvature`
averages the incident vertices' curvature densities per triangle, which
is what a TPMS's "K <= 0 everywhere" check reads.

scipy is not a core dependency here (see repo house rules): the
cotangent Laplacian is built with ``np.add.at`` scatter-accumulation,
never a sparse solver.
"""

from __future__ import annotations

import numpy as np

_HALF_PI = np.pi / 2.0


def _triangle_geometry(
    verts: np.ndarray, tris: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Per-triangle corner angles, squared opposite-edge lengths and area.

    Returns ``(angles, e01_sq, e12_sq, e20_sq, area, normal)``:

    - ``angles`` is ``(M, 3)``, the angle at ``tris[:, 0]``, ``[:, 1]``
      and ``[:, 2]`` respectively.
    - ``e01_sq``/``e12_sq``/``e20_sq`` are the squared lengths of edges
      ``v0-v1``, ``v1-v2`` and ``v2-v0`` (each opposite the remaining
      corner: ``e01_sq`` is opposite the angle at ``v2``, and so on).
    - ``area`` is the unsigned triangle area.
    - ``normal`` is the *non-unit* CCW face normal
      ``cross(v1 - v0, v2 - v0)``.
    """
    v0 = verts[tris[:, 0]]
    v1 = verts[tris[:, 1]]
    v2 = verts[tris[:, 2]]

    def _angle_at(pa: np.ndarray, pb: np.ndarray, pc: np.ndarray) -> np.ndarray:
        u = pb - pa
        w = pc - pa
        cross_norm = np.linalg.norm(np.cross(u, w), axis=1)
        dot = np.einsum("ij,ij->i", u, w)
        # atan2(|u x w|, u.w) is numerically stable near 0 and pi, unlike
        # arccos(dot/(|u||w|)).
        return np.arctan2(cross_norm, dot)

    a0 = _angle_at(v0, v1, v2)
    a1 = _angle_at(v1, v2, v0)
    a2 = _angle_at(v2, v0, v1)
    angles = np.stack([a0, a1, a2], axis=1)

    e01_sq = np.einsum("ij,ij->i", v1 - v0, v1 - v0)
    e12_sq = np.einsum("ij,ij->i", v2 - v1, v2 - v1)
    e20_sq = np.einsum("ij,ij->i", v0 - v2, v0 - v2)

    normal = np.cross(v1 - v0, v2 - v0)
    area = 0.5 * np.linalg.norm(normal, axis=1)

    return angles, e01_sq, e12_sq, e20_sq, area, normal


def _boundary_vertex_mask(n_verts: int, tris: np.ndarray) -> np.ndarray:
    """True for vertices touching an edge used by exactly one triangle."""
    edges = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]], axis=0)
    edges_sorted = np.sort(edges, axis=1)
    _, inverse, counts = np.unique(
        edges_sorted, axis=0, return_inverse=True, return_counts=True
    )
    edge_counts = counts[inverse.reshape(-1)]
    mask = np.zeros(n_verts, dtype=bool)
    boundary_edges = edges_sorted[edge_counts == 1]
    if boundary_edges.size:
        mask[boundary_edges.ravel()] = True
    return mask


def gaussian_curvature(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Per-vertex angle defect (radians): ``2*pi - sum(incident angles)``
    at interior vertices, ``pi - sum(incident angles)`` on a boundary.

    This is the raw, un-area-weighted discrete Gaussian curvature. On a
    closed mesh (no boundary), ``gaussian_curvature(verts, tris).sum()``
    equals ``2*pi*chi`` exactly, ``chi = V - E + F`` — a combinatorial
    identity, not an approximation.
    """
    n = verts.shape[0]
    angles, *_ = _triangle_geometry(verts, tris)
    angle_sum = np.zeros(n, dtype=np.float64)
    for corner in range(3):
        np.add.at(angle_sum, tris[:, corner], angles[:, corner])
    boundary = _boundary_vertex_mask(n, tris)
    full_turn = np.where(boundary, np.pi, 2.0 * np.pi)
    return full_turn - angle_sum


def mixed_voronoi_area(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Per-vertex mixed Voronoi/barycentric cell area (Meyer et al. §3.3).

    Non-obtuse triangles split by the circumcentric (Voronoi) rule;
    obtuse triangles fall back to half the triangle area at the obtuse
    corner and a quarter at the other two, which keeps every
    contribution non-negative (the reason the circumcentric rule alone
    can go negative on obtuse triangles).
    """
    n = verts.shape[0]
    angles, e01_sq, e12_sq, e20_sq, area, _normal = _triangle_geometry(verts, tris)
    a0, a1, a2 = angles[:, 0], angles[:, 1], angles[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        cot0 = 1.0 / np.tan(a0)
        cot1 = 1.0 / np.tan(a1)
        cot2 = 1.0 / np.tan(a2)

    obtuse0 = a0 > _HALF_PI
    obtuse1 = a1 > _HALF_PI
    obtuse2 = a2 > _HALF_PI
    any_obtuse = obtuse0 | obtuse1 | obtuse2

    voronoi0 = 0.125 * (cot1 * e20_sq + cot2 * e01_sq)
    voronoi1 = 0.125 * (cot2 * e01_sq + cot0 * e12_sq)
    voronoi2 = 0.125 * (cot0 * e12_sq + cot1 * e20_sq)

    contrib0 = np.where(
        ~any_obtuse, voronoi0, np.where(obtuse0, area / 2.0, area / 4.0)
    )
    contrib1 = np.where(
        ~any_obtuse, voronoi1, np.where(obtuse1, area / 2.0, area / 4.0)
    )
    contrib2 = np.where(
        ~any_obtuse, voronoi2, np.where(obtuse2, area / 2.0, area / 4.0)
    )

    out = np.zeros(n, dtype=np.float64)
    np.add.at(out, tris[:, 0], contrib0)
    np.add.at(out, tris[:, 1], contrib1)
    np.add.at(out, tris[:, 2], contrib2)
    return out


def gaussian_curvature_density(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Per-vertex Gaussian curvature ``K`` (1/length^2): angle defect
    divided by the vertex's mixed Voronoi area — comparable to a
    continuous surface's ``K`` and to :func:`mean_curvature`."""
    defect = gaussian_curvature(verts, tris)
    area = mixed_voronoi_area(verts, tris)
    safe_area = np.where(area > 0, area, 1.0)
    return np.where(area > 0, defect / safe_area, 0.0)


def vertex_normals(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Per-vertex unit normals, angle-weighted average of incident face
    normals (Max 1999) — the standard robust choice for irregular
    triangulations, used here only to sign :func:`mean_curvature`."""
    angles, _e01, _e12, _e20, _area, normal = _triangle_geometry(verts, tris)
    face_norm = normal / np.linalg.norm(normal, axis=1, keepdims=True)
    out = np.zeros_like(verts)
    for corner in range(3):
        np.add.at(out, tris[:, corner], angles[:, corner][:, None] * face_norm)
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    safe_norms = np.where(norms > 0, norms, 1.0)
    return out / safe_norms


def _cotangent_laplacian(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """``sum_j (cot(a_ij) + cot(b_ij)) * (x_j - x_i)`` per vertex — the
    un-normalised mesh Laplace-Beltrami operator applied to position,
    before the ``1/(2*A_i)`` mixed-area normalisation."""
    angles, *_ = _triangle_geometry(verts, tris)
    a0, a1, a2 = angles[:, 0], angles[:, 1], angles[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        cot0 = 1.0 / np.tan(a0)  # opposite edge v1-v2
        cot1 = 1.0 / np.tan(a1)  # opposite edge v2-v0
        cot2 = 1.0 / np.tan(a2)  # opposite edge v0-v1

    v0i, v1i, v2i = tris[:, 0], tris[:, 1], tris[:, 2]
    lap = np.zeros_like(verts)

    def _accumulate(ai: np.ndarray, bi: np.ndarray, weight: np.ndarray) -> None:
        diff = verts[bi] - verts[ai]
        np.add.at(lap, ai, weight[:, None] * diff)
        np.add.at(lap, bi, -weight[:, None] * diff)

    _accumulate(v1i, v2i, cot0)
    _accumulate(v2i, v0i, cot1)
    _accumulate(v0i, v1i, cot2)
    return lap


def mean_curvature(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Per-vertex signed mean curvature ``H`` (1/length), via the
    cotangent-Laplacian mean-curvature normal
    ``K_H = Laplacian(x) / (2*A_mixed)`` (Meyer et al. eq. 8), signed by
    projecting onto the angle-weighted vertex normal
    (:func:`vertex_normals`) so a convex outward-normal shape (a sphere)
    reports positive ``H = 1/R``.
    """
    lap = _cotangent_laplacian(verts, tris)
    area = mixed_voronoi_area(verts, tris)
    safe_area = np.where(area > 0, area, 1.0)
    curvature_normal = lap / (2.0 * safe_area[:, None])
    normal = vertex_normals(verts, tris)
    # curvature_normal points inward (toward the medial side) for a convex
    # shape, i.e. opposite the outward vertex normal, hence the minus sign
    # to land on the H = +1/R convention for e.g. a sphere with outward
    # normals.
    h = -0.5 * np.einsum("ij,ij->i", curvature_normal, normal)
    return np.where(area > 0, h, 0.0)


def principal_curvatures(
    mean: np.ndarray, gauss_density: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Principal curvatures ``(k1, k2)``, ``k1 >= k2``, from mean
    curvature ``H`` and Gaussian curvature density ``K``:
    ``k = H +/- sqrt(H^2 - K)``. Both inputs must be curvature
    *densities* (:func:`mean_curvature`, :func:`gaussian_curvature_density`)
    — feeding raw angle defect here silently produces nonsense units.
    The discriminant is clamped at 0 to absorb numerical noise at
    umbilic points, where the analytic value is exactly 0.
    """
    discriminant = np.clip(mean**2 - gauss_density, 0.0, None)
    root = np.sqrt(discriminant)
    return mean + root, mean - root


def curvature_violations(k1: np.ndarray, k2: np.ndarray, kappa_max: float) -> int:
    """Count of vertices where either principal curvature exceeds
    ``kappa_max`` in magnitude (``|k| <= kappa_max`` is the pass
    condition). Always a count for reporting — never raises."""
    return int(np.count_nonzero((np.abs(k1) > kappa_max) | (np.abs(k2) > kappa_max)))


def face_gaussian_curvature(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Per-face Gaussian curvature density: the mean of the three
    incident vertices' :func:`gaussian_curvature_density`. Useful for a
    whole-face sign check (a TPMS is ``K <= 0`` on every face)."""
    density = gaussian_curvature_density(verts, tris)
    return density[tris].mean(axis=1)
