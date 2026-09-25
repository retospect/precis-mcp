"""precis_surface.curvature — discrete differential-geometry operators
(docs/backlog/precis-surface-kernel.md "Slice 1 — the dual route").

Meshes are hand-built closed forms (icosahedron, a welded torus grid, a
flat patch) — this module owns no geometry code beyond what's needed to
exercise :mod:`precis_surface.curvature` against the interface
contract's bare ``Mesh = (verts, tris)`` tuple; it must not import from
``level_set``/``periodic_mesh`` (a sibling's in-flight files).
"""

from __future__ import annotations

import math

import numpy as np

from precis_surface.curvature import (
    curvature_violations,
    face_gaussian_curvature,
    gaussian_curvature,
    gaussian_curvature_density,
    mean_curvature,
    mixed_voronoi_area,
    principal_curvatures,
    vertex_normals,
)

# --------------------------------------------------------------------------
# hand-built closed-form meshes
# --------------------------------------------------------------------------


def _icosahedron(radius: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    phi = (1.0 + 5.0**0.5) / 2.0
    base = np.array(
        [
            [-1, phi, 0],
            [1, phi, 0],
            [-1, -phi, 0],
            [1, -phi, 0],
            [0, -1, phi],
            [0, 1, phi],
            [0, -1, -phi],
            [0, 1, -phi],
            [phi, 0, -1],
            [phi, 0, 1],
            [-phi, 0, -1],
            [-phi, 0, 1],
        ],
        dtype=np.float64,
    )
    base = base / np.linalg.norm(base[0]) * radius
    # CCW outward winding (verified once, at module import, below).
    faces = np.array(
        [
            [0, 11, 5],
            [0, 5, 1],
            [0, 1, 7],
            [0, 7, 10],
            [0, 10, 11],
            [1, 5, 9],
            [5, 11, 4],
            [11, 10, 2],
            [10, 7, 6],
            [7, 1, 8],
            [3, 9, 4],
            [3, 4, 2],
            [3, 2, 6],
            [3, 6, 8],
            [3, 8, 9],
            [4, 9, 5],
            [2, 4, 11],
            [6, 2, 10],
            [8, 6, 7],
            [9, 8, 1],
        ],
        dtype=np.int64,
    )
    return base, faces


def _subdivide_midpoint(
    verts_list: list[np.ndarray], cache: dict[tuple[int, int], int], a: int, b: int
) -> int:
    """Index of the (unit-sphere-projected) midpoint of ``a``/``b``,
    appending it to ``verts_list`` the first time a given edge is seen
    and reusing the cached index thereafter -- a module-level helper
    (rather than a closure rebuilt every subdivision pass) so ruff's
    B023 loop-variable-capture check has nothing to flag."""
    key = (a, b) if a < b else (b, a)
    if key in cache:
        return cache[key]
    m = (verts_list[a] + verts_list[b]) / 2.0
    m = m / np.linalg.norm(m)
    verts_list.append(m)
    idx = len(verts_list) - 1
    cache[key] = idx
    return idx


def _icosphere(subdiv: int, radius: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Recursively midpoint-subdivided, radius-projected icosahedron —
    same topology (V - E + F == 2 at every level: each split replaces 1
    triangle with 4, adding 3 edges and 2 vertices, chi-preserving), a
    closer sphere approximation at each level."""
    verts, faces = _icosahedron(radius=1.0)
    verts_list = [v for v in verts]
    face_list: list[tuple[int, int, int]] = [
        (int(f[0]), int(f[1]), int(f[2])) for f in faces
    ]
    for _ in range(subdiv):
        cache: dict[tuple[int, int], int] = {}
        new_faces: list[tuple[int, int, int]] = []
        for a, b, c in face_list:
            ab = _subdivide_midpoint(verts_list, cache, a, b)
            bc = _subdivide_midpoint(verts_list, cache, b, c)
            ca = _subdivide_midpoint(verts_list, cache, c, a)
            new_faces += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
        face_list = new_faces
    verts_arr = np.array(verts_list, dtype=np.float64) * radius
    faces_arr = np.array(face_list, dtype=np.int64)
    return verts_arr, faces_arr


def _welded_torus(
    nu: int = 12, nv: int = 8, major: float = 2.0, minor: float = 0.7
) -> tuple[np.ndarray, np.ndarray]:
    """Closed genus-1 torus, welded (each (i, j) grid point is one
    vertex, indices wrap mod nu/nv) — no duplicated seam vertices, so
    the mesh is a proper closed 2-manifold with chi == 0."""

    def idx(i: int, j: int) -> int:
        return (i % nu) * nv + (j % nv)

    verts = np.zeros((nu * nv, 3), dtype=np.float64)
    for i in range(nu):
        u = 2.0 * math.pi * i / nu
        for j in range(nv):
            v = 2.0 * math.pi * j / nv
            rad = major + minor * math.cos(v)
            verts[idx(i, j)] = [
                rad * math.cos(u),
                rad * math.sin(u),
                minor * math.sin(v),
            ]

    tris = []
    for i in range(nu):
        for j in range(nv):
            a, b, c, d = idx(i, j), idx(i + 1, j), idx(i + 1, j + 1), idx(i, j + 1)
            # CCW when viewed from outside the tube (matches the outward
            # gradient of (sqrt(x^2+y^2) - major)^2 + z^2 - minor^2).
            tris.append([a, b, c])
            tris.append([a, c, d])
    return verts, np.array(tris, dtype=np.int64)


def _flat_patch(
    nx: int = 6, ny: int = 6, spacing: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """An nx*ny grid of unit squares (each split into 2 CCW triangles) in
    the z=0 plane — an open mesh with a boundary."""
    verts = np.array(
        [[i * spacing, j * spacing, 0.0] for j in range(ny + 1) for i in range(nx + 1)],
        dtype=np.float64,
    )

    def idx(i: int, j: int) -> int:
        return j * (nx + 1) + i

    tris = []
    for j in range(ny):
        for i in range(nx):
            a, b, c, d = idx(i, j), idx(i + 1, j), idx(i + 1, j + 1), idx(i, j + 1)
            tris.append([a, b, c])
            tris.append([a, c, d])
    return verts, np.array(tris, dtype=np.int64)


def _mesh_euler_characteristic(n_verts: int, tris: np.ndarray) -> int:
    """chi = V - E + F, counted directly from triangle connectivity —
    independent of anything in curvature.py, so it's a real check."""
    edges = set()
    for t in tris:
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            edges.add((a, b) if a < b else (b, a))
    return n_verts - len(edges) + len(tris)


# --------------------------------------------------------------------------
# winding sanity (protects every geometric assertion below)
# --------------------------------------------------------------------------


def test_icosahedron_winding_is_ccw_outward() -> None:
    verts, tris = _icosahedron()
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    normal = np.cross(v1 - v0, v2 - v0)
    centroid = (v0 + v1 + v2) / 3.0
    assert np.all(np.einsum("ij,ij->i", normal, centroid) > 0)


def test_torus_winding_is_ccw_outward() -> None:
    verts, tris = _welded_torus()
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    normal = np.cross(v1 - v0, v2 - v0)
    centroid = (v0 + v1 + v2) / 3.0
    major, minor = 2.0, 0.7

    def implicit_grad(p: np.ndarray) -> np.ndarray:
        eps = 1e-4
        grad = np.zeros_like(p)
        for axis in range(3):
            step = np.zeros(3)
            step[axis] = eps

            def f(q: np.ndarray) -> np.ndarray:
                r_xy = np.sqrt(q[..., 0] ** 2 + q[..., 1] ** 2)
                return (r_xy - major) ** 2 + q[..., 2] ** 2 - minor**2

            grad[:, axis] = (f(p + step) - f(p - step)) / (2 * eps)
        return grad

    assert np.all(np.einsum("ij,ij->i", normal, implicit_grad(centroid)) > 0)


# --------------------------------------------------------------------------
# gaussian_curvature: the combinatorial Gauss-Bonnet identity
# --------------------------------------------------------------------------


def test_icosahedron_total_angle_defect_is_4_pi() -> None:
    verts, tris = _icosahedron()
    chi = _mesh_euler_characteristic(len(verts), tris)
    assert chi == 2
    defect = gaussian_curvature(verts, tris)
    assert math.isclose(defect.sum(), 2.0 * math.pi * chi, rel_tol=1e-12, abs_tol=1e-10)


def test_icosphere_total_angle_defect_is_4_pi_at_every_subdivision() -> None:
    for subdiv in (0, 1, 2):
        verts, tris = _icosphere(subdiv)
        chi = _mesh_euler_characteristic(len(verts), tris)
        assert chi == 2  # splits preserve V - E + F, per the spec's loop invariant
        defect = gaussian_curvature(verts, tris)
        assert math.isclose(defect.sum(), 4.0 * math.pi, rel_tol=1e-10, abs_tol=1e-9)


def test_torus_total_angle_defect_is_zero() -> None:
    verts, tris = _welded_torus()
    chi = _mesh_euler_characteristic(len(verts), tris)
    assert chi == 0
    defect = gaussian_curvature(verts, tris)
    assert math.isclose(defect.sum(), 0.0, abs_tol=1e-9)


def test_flat_patch_interior_vertices_have_zero_gaussian_curvature() -> None:
    verts, tris = _flat_patch()
    defect = gaussian_curvature(verts, tris)
    nx = ny = 6
    interior = np.array(
        [j * (nx + 1) + i for j in range(1, ny) for i in range(1, nx)], dtype=np.int64
    )
    assert interior.size > 0
    assert np.allclose(defect[interior], 0.0, atol=1e-10)
    # the four corners are NOT zero: pi/2 turn missing at each right-angle
    # corner of the flat square (this is a real, expected boundary effect,
    # not a bug — angle defect at a boundary vertex is pi - sum(angles)).
    corner = 0
    assert math.isclose(defect[corner], math.pi / 2.0, rel_tol=1e-9)


# --------------------------------------------------------------------------
# mixed_voronoi_area: partitions the mesh exactly, never negative
# --------------------------------------------------------------------------


def test_mixed_voronoi_area_sums_to_total_mesh_area() -> None:
    verts, tris = _icosphere(2)
    areas = mixed_voronoi_area(verts, tris)
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    total = 0.5 * np.sum(np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1))
    assert math.isclose(areas.sum(), total, rel_tol=1e-10)


def test_mixed_voronoi_area_is_never_negative_with_obtuse_triangles() -> None:
    # a thin sliver triangle forces the obtuse branch.
    verts = np.array(
        [[0, 0, 0], [10, 0, 0], [10, 1, 0], [0.2, 0.5, 0]], dtype=np.float64
    )
    tris = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    areas = mixed_voronoi_area(verts, tris)
    assert np.all(areas >= 0.0)
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    total = 0.5 * np.sum(np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1))
    assert math.isclose(areas.sum(), total, rel_tol=1e-10)


# --------------------------------------------------------------------------
# mean/gaussian curvature densities converge to 1/R, 1/R^2 on a sphere
# --------------------------------------------------------------------------


def test_mean_and_gaussian_curvature_converge_to_sphere_values() -> None:
    # subdiv 0/1 are exceptionally symmetric (every vertex has the same
    # local neighbourhood up to icosahedral symmetry) and land the
    # discretisation at near machine precision by construction, which
    # would make a "monotonic improvement" assertion starting there
    # flaky rather than meaningful. subdiv 2+ has two distinct vertex
    # valences (5 and 6) and shows the real O(h^2)-ish trend.
    radius = 3.0
    errors_h = []
    errors_k = []
    for subdiv in (2, 3, 4):
        verts, tris = _icosphere(subdiv, radius=radius)
        h = mean_curvature(verts, tris)
        k = gaussian_curvature_density(verts, tris)
        errors_h.append(float(np.max(np.abs(h - 1.0 / radius))))
        errors_k.append(float(np.max(np.abs(k - 1.0 / radius**2))))
    # converging toward the closed-form sphere value ...
    assert errors_h[-1] < 0.02
    assert errors_k[-1] < 0.02
    # ... and getting strictly better with resolution.
    assert errors_h[-1] < errors_h[0]
    assert errors_k[-1] < errors_k[0]


def test_icosphere_mean_curvature_sign_matches_outward_normal_convention() -> None:
    verts, tris = _icosphere(2, radius=1.0)
    h = mean_curvature(verts, tris)
    assert np.all(h > 0)  # convex, outward normals -> positive H


# --------------------------------------------------------------------------
# principal curvatures + the reporting checker
# --------------------------------------------------------------------------


def test_principal_curvatures_agree_on_a_sphere() -> None:
    radius = 2.0
    verts, tris = _icosphere(3, radius=radius)
    h = mean_curvature(verts, tris)
    k = gaussian_curvature_density(verts, tris)
    k1, k2 = principal_curvatures(h, k)
    # umbilic everywhere: k1 == k2 == 1/R
    assert np.allclose(k1, k2, atol=1e-2)
    assert np.allclose(k1, 1.0 / radius, atol=1e-2)


def test_principal_curvatures_discriminant_clamped_at_zero() -> None:
    # synthetic H, K where numerical noise would otherwise take H^2 - K
    # slightly negative at an umbilic point.
    h = np.array([1.0, 0.5])
    k = np.array([1.0 + 1e-9, 0.25 + 1e-9])  # H^2 - K is a tiny negative number
    k1, k2 = principal_curvatures(h, k)
    assert np.all(np.isfinite(k1))
    assert np.all(np.isfinite(k2))
    assert np.allclose(k1, k2, atol=1e-3)


def test_curvature_violations_counts_never_raises() -> None:
    radius = 1.0
    verts, tris = _icosphere(2, radius=radius)
    h = mean_curvature(verts, tris)
    k = gaussian_curvature_density(verts, tris)
    k1, k2 = principal_curvatures(h, k)

    # a generous bound: nothing on a unit sphere should exceed it.
    assert curvature_violations(k1, k2, kappa_max=10.0) == 0
    # a bound tighter than 1/R: every vertex on a unit sphere violates it.
    assert curvature_violations(k1, k2, kappa_max=0.1) == len(verts)
    # never raises regardless of how absurd kappa_max is.
    assert curvature_violations(k1, k2, kappa_max=0.0) == len(verts)


# --------------------------------------------------------------------------
# per-face Gaussian curvature sign
# --------------------------------------------------------------------------


def test_sphere_face_gaussian_curvature_is_positive_everywhere() -> None:
    verts, tris = _icosphere(2)
    k_face = face_gaussian_curvature(verts, tris)
    assert np.all(k_face > 0)


def test_torus_face_gaussian_curvature_has_both_signs() -> None:
    # outer equator (convex, K > 0) and inner throat (saddle, K < 0) are
    # both present on a torus -- the qualitative check a TPMS wants
    # ("K <= 0 everywhere") on a shape known NOT to satisfy it.
    verts, tris = _welded_torus()
    k_face = face_gaussian_curvature(verts, tris)
    assert np.any(k_face > 0)
    assert np.any(k_face < 0)


# --------------------------------------------------------------------------
# vertex_normals: unit length, outward
# --------------------------------------------------------------------------


def test_vertex_normals_are_unit_and_outward_on_a_sphere() -> None:
    verts, tris = _icosphere(2, radius=2.5)
    normals = vertex_normals(verts, tris)
    assert np.allclose(np.linalg.norm(normals, axis=1), 1.0, atol=1e-12)
    radial = verts / np.linalg.norm(verts, axis=1, keepdims=True)
    assert np.all(np.einsum("ij,ij->i", normals, radial) > 0.99)
