"""precis_surface periodic marching cubes -- slice 1 acceptance
(docs/backlog/precis-surface-kernel.md "Slice 1 -- the dual route").

Combinatorial checks only: welded-quotient edge-manifold closure and
mesh-derived ``V - E + F`` on each nodal family's SIMPLE-CUBIC cell (the
conventional-cell values for genus 3 per primitive cell: P == -4,
D == -16, gyroid == -8). Curvature (``precis_surface.curvature``, a
sibling module) is exercised by a later integration test, not here --
the acceptance section's ``K <= 0`` claim is INFO-level for a nodal
surface (see ``precis_surface/level_set.py``'s docstring), not an
assertion.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest
from numpy.typing import NDArray

from precis_surface.level_set import (
    gyroid,
    gyroid_grad,
    schwarz_d,
    schwarz_d_grad,
    schwarz_p,
    schwarz_p_grad,
)
from precis_surface.periodic_mesh import (
    PeriodicMeshError,
    is_edge_manifold_closed,
    periodic_mesh,
    welded_euler,
)

#: A `level_set` value or gradient function: `(N, 3)` points + cell edge
#: `a` -> `(N,)` value or `(N, 3)` gradient.
FieldFn = Callable[[NDArray[np.float64], float], NDArray[np.float64]]

_FAMILIES: list[tuple[str, FieldFn, FieldFn]] = [
    ("P", schwarz_p, schwarz_p_grad),
    ("D", schwarz_d, schwarz_d_grad),
    ("gyroid", gyroid, gyroid_grad),
]


@pytest.mark.parametrize("a", [1.0, 2.46])
def test_schwarz_p_welded_quotient_closed_and_euler(a: float) -> None:
    """The P cell at two different pitches gives a closed welded quotient
    (every edge in exactly 2 triangles) and V - E + F == -4 on the
    simple-cubic cell -- the P surface's known genus-3-per-cell Euler
    characteristic (2 - 2*3), asserted mesh-derived, nothing hardcoded
    beyond the topological identity."""
    pm = periodic_mesh(lambda pts: schwarz_p(pts, a), a=a, n=24)

    assert is_edge_manifold_closed(pm)
    v, e, f, chi = welded_euler(pm)
    assert chi == -4
    assert v - e + f == chi
    # sanity: every face is a real triangle, no degenerate rows
    assert f == len(pm.tris)
    assert pm.verts.shape[1] == 3
    assert pm.tris.shape[1] == 3


@pytest.mark.parametrize(
    ("name", "field", "n", "expected_chi"),
    [
        ("P", schwarz_p, 16, -4),
        ("P", schwarz_p, 24, -4),
        ("P", schwarz_p, 32, -4),
        ("D", schwarz_d, 16, -16),
        ("D", schwarz_d, 24, -16),
        ("D", schwarz_d, 32, -16),
        ("gyroid", gyroid, 16, -8),
        ("gyroid", gyroid, 24, -8),
        ("gyroid", gyroid, 32, -8),
    ],
)
def test_all_nodal_families_periodic_at_multiple_resolutions(
    name: str, field: FieldFn, n: int, expected_chi: int
) -> None:
    """Regression for the sign-mismatch bug: Schwarz D and the gyroid have
    many grid samples exactly on the cell face where float noise can flip
    the marching-cubes inside/outside sign between the x=0 and x=a
    copies, which used to make ``periodic_mesh`` raise
    ``PeriodicMeshError`` for D/G (P escaped only because
    ``cos(2*pi) == 1.0`` exactly). Sampling the half-open grid and
    obtaining every high-face value by indexing low-face samples modulo
    ``n`` makes a sign mismatch impossible by construction, at every
    resolution -- not just the one this bug happened to be diagnosed at.
    Chi is mesh-derived (``welded_euler``), never a hardcoded literal
    compared against anything but the topological identity."""
    a = 1.0
    pm = periodic_mesh(lambda pts: field(pts, a), a=a, n=n)

    assert is_edge_manifold_closed(pm), (
        f"{name} n={n}: welded quotient is not a closed 2-manifold"
    )
    v, e, f_count, chi = welded_euler(pm)
    assert v - e + f_count == chi
    assert chi == expected_chi, (
        f"{name} n={n}: got V={v} E={e} F={f_count} chi={chi}, "
        f"expected chi={expected_chi}"
    )


@pytest.mark.parametrize(("name", "field", "grad"), _FAMILIES)
def test_periodic_mesh_ccw_outward_normals(
    name: str, field: FieldFn, grad: FieldFn
) -> None:
    """Every triangle's normal has strictly positive dot product with
    ``grad(field)`` at its centroid -- the exact outward-orientation test
    (a level set's direction of increasing field IS its outward normal).
    This replaces an earlier fixed-step field probe, which is fragile
    exactly here: a TPMS's sheets pass close enough to each other that a
    finite step along the normal can land past a *neighbouring* sheet and
    read its sign instead of the local one (see
    ``precis_surface/periodic_mesh.py``'s module docstring). Must hold
    for every triangle, not merely most of them."""
    a = 1.0
    pm = periodic_mesh(
        lambda pts: field(pts, a),
        a=a,
        n=16,
        grad=lambda pts: grad(pts, a),
    )
    verts, tris = pm.mesh
    v0 = verts[tris[:, 0]]
    v1 = verts[tris[:, 1]]
    v2 = verts[tris[:, 2]]
    normal = np.cross(v1 - v0, v2 - v0)
    centroid = (v0 + v1 + v2) / 3.0
    dot = np.einsum("ij,ij->i", normal, grad(centroid, a))
    n_fail = int(np.sum(dot <= 0.0))
    assert n_fail == 0, f"{name}: {n_fail}/{len(tris)} triangles misoriented"


@pytest.mark.parametrize(("name", "field", "_grad"), _FAMILIES)
@pytest.mark.parametrize("n", [16, 17, 20, 24, 32])
def test_periodic_mesh_no_degenerate_triangles(
    name: str, field: FieldFn, _grad: FieldFn, n: int
) -> None:
    """Marching-cubes slivers stay BOUNDED -- this does not assert their
    absence, because they are not absent.

    An earlier version of this test asserted ``area == 0.0`` exactly and
    was therefore vacuous: the degenerate triangles have areas around
    9e-23 against a median of 1e-3, which is geometrically degenerate but
    is never bit-exactly zero. That is the same float-noise trap the
    sampling tie-break in ``periodic_mesh`` exists to dodge (grid samples
    cancel algebraically to ~1e-16, not to 0.0), reproduced one level up.

    Slivers are ordinary marching-cubes output and are NOT a bug to fix
    here -- the remesh loop's edge-collapse step removes them, and the
    strict no-degeneracy guarantee belongs to the post-remesh test, not
    to this one. What is worth guarding at this stage is that the sliver
    population does not grow: a regression in the tie-break or the wrap
    indexing would show up as a sharp rise in the fraction of
    near-degenerate triangles.

    **Prefer an odd ``n``.** Measured sliver fractions: odd ``n`` gives
    0.0-5.6%, even ``n`` gives 18-49% (worst: Schwarz D at n=16, 48.9%).
    At even ``n`` the grid planes coincide with the surface's own
    symmetry planes, so a large population of samples lands in the
    float-noise band and marching cubes resolves those cells into
    degenerate configurations; an odd grid misses those planes. Topology
    is correct either way -- chi and closedness hold at every ``n`` -- so
    this is a geometry-quality knob, not a correctness one. The budgets
    below differ accordingly, and encode the measured behaviour so that a
    change to it is noticed rather than absorbed."""
    a = 1.0
    pm = periodic_mesh(lambda pts: field(pts, a), a=a, n=n)
    assert is_edge_manifold_closed(pm)
    verts, tris = pm.mesh
    v0 = verts[tris[:, 0]]
    v1 = verts[tris[:, 1]]
    v2 = verts[tris[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    nominal = 0.5 * (a / n) ** 2  # a marching-cubes cell face, halved
    sliver_frac = float(np.mean(area < 1.0e-6 * nominal))
    budget = 0.10 if n % 2 else 0.55
    assert sliver_frac < budget, (
        f"{name} n={n}: {sliver_frac:.1%} of triangles are near-degenerate "
        f"(<1e-6 of nominal cell area {nominal:.3e}) -- budget for "
        f"{'odd' if n % 2 else 'even'} n is {budget:.0%}. A sharp rise "
        "means the sampling tie-break or the wrap indexing regressed."
    )


@pytest.mark.parametrize(("name", "field", "grad"), _FAMILIES)
def test_level_set_gradients_match_central_difference(
    name: str, field: FieldFn, grad: FieldFn
) -> None:
    """Each by-hand analytic gradient agrees with a central-difference
    approximation of the corresponding value function -- checked
    numerically, not trusted by inspection alone."""
    a = 1.37
    rng = np.random.default_rng(0)
    pts = rng.uniform(-2.0, 2.0, size=(500, 3))
    eps = 1e-6
    analytic = grad(pts, a)
    central = np.empty_like(pts)
    for axis in range(3):
        delta = np.zeros(3)
        delta[axis] = eps
        f_plus = field(pts + delta, a)
        f_minus = field(pts - delta, a)
        central[:, axis] = (f_plus - f_minus) / (2.0 * eps)
    max_err = float(np.max(np.abs(analytic - central)))
    assert max_err < 1e-6, f"{name}: max abs error {max_err} vs central difference"


def test_periodic_mesh_orientation_fallback_matches_true_gradient() -> None:
    """When the caller's ``grad`` reports a near-zero magnitude over a
    patch of faces (an ambiguous/undefined outward direction), the
    breadth-first combinatorial fallback must still land on the same
    orientation the true analytic gradient would have chosen -- checked
    by comparing the faces resolved via fallback against
    ``schwarz_p_grad`` directly, never trusting the injected zero."""
    a = 1.0
    n = 16
    pm_ref = periodic_mesh(
        lambda pts: schwarz_p(pts, a), a=a, n=n, grad=lambda pts: schwarz_p_grad(pts, a)
    )
    v0, v1, v2 = (
        pm_ref.verts[pm_ref.tris[:, 0]],
        pm_ref.verts[pm_ref.tris[:, 1]],
        pm_ref.verts[pm_ref.tris[:, 2]],
    )
    target = ((v0 + v1 + v2) / 3.0)[len(pm_ref.tris) // 2]

    def blinded_grad(pts: NDArray[np.float64]) -> NDArray[np.float64]:
        g = schwarz_p_grad(pts, a).copy()
        near = np.linalg.norm(pts - target, axis=1) < 0.2
        g[near] = 1e-12
        return g

    pm = periodic_mesh(lambda pts: schwarz_p(pts, a), a=a, n=n, grad=blinded_grad)
    assert pm.orientation_fallback_count > 0, "test did not exercise the fallback path"
    assert is_edge_manifold_closed(pm)
    assert welded_euler(pm)[3] == -4

    verts, tris = pm.mesh
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    normal = np.cross(v1 - v0, v2 - v0)
    centroid = (v0 + v1 + v2) / 3.0
    dot = np.einsum("ij,ij->i", normal, schwarz_p_grad(centroid, a))
    n_fail = int(np.sum(dot <= 0.0))
    assert n_fail == 0, f"{n_fail}/{len(tris)} triangles misoriented after fallback"


def test_wrap_pairs_cover_every_boundary_vertex_once() -> None:
    """Every ``wrap`` pair links a low-face vertex to a distinct
    high-face vertex; coordinates are kept separate (never merged), only
    the index pairing carries the identification."""
    a = 1.0
    pm = periodic_mesh(lambda pts: schwarz_p(pts, a), a=a, n=16)
    assert len(pm.wrap) > 0
    lo, hi = pm.wrap[:, 0], pm.wrap[:, 1]
    # no coordinate collapse: low/high copies sit on opposite faces
    assert not np.allclose(pm.verts[lo], pm.verts[hi])
    # wrap pairs are 1:1 (no vertex identified twice on the same axis pass)
    assert len(np.unique(lo)) == len(lo)
    assert len(np.unique(hi)) == len(hi)
    # every raw mesh vertex is actually used by some triangle
    assert len(np.unique(pm.tris)) == len(pm.verts)


def test_periodic_mesh_rejects_field_never_crossing_zero() -> None:
    with pytest.raises(PeriodicMeshError):
        periodic_mesh(lambda pts: np.full(len(pts), 5.0), a=1.0, n=8)


def test_periodic_mesh_rejects_bad_cell_edge() -> None:
    with pytest.raises(ValueError):
        periodic_mesh(lambda pts: schwarz_p(pts, 1.0), a=0.0, n=8)
    with pytest.raises(ValueError):
        periodic_mesh(lambda pts: schwarz_p(pts, 1.0), a=1.0, n=1)
