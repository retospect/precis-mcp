"""precis_surface.dual -- the dual-route acceptance checks
(docs/backlog/precis-surface-kernel.md "Slice 1 -- the dual route").

Numpy only. Invariants checked mesh-derived (never hardcoded beyond the
topological identities `hexfold.defects.counting_residual` itself states):
every atom (triangle) has exactly 3 bonds in the periodic quotient (an
algebraic consequence of "every triangle has 3 edges", not asserted as a
separate fact so much as confirmed); atom/bond/ring counts match the
welded F/E/V; the ring-size counting law
`counting_residual(ring_histogram, 0, chi) == 0`; `unroll` tiles correctly
and leaves only the supercell boundary under-bonded.
"""

from __future__ import annotations

import numpy as np
import pytest

from hexfold.defects import counting_residual
from precis_surface.dual import dualise, unroll
from precis_surface.level_set import gyroid, gyroid_grad, schwarz_p, schwarz_p_grad
from precis_surface.periodic_mesh import periodic_mesh, welded_euler


def _p_mesh(n: int = 17, a: float = 1.0):
    return periodic_mesh(
        lambda pts: schwarz_p(pts, a),
        a=a,
        n=n,
        grad=lambda pts: schwarz_p_grad(pts, a),
    )


def _gyroid_mesh(n: int = 17, a: float = 1.0):
    return periodic_mesh(
        lambda pts: gyroid(pts, a),
        a=a,
        n=n,
        grad=lambda pts: gyroid_grad(pts, a),
    )


def test_p_every_atom_has_exactly_3_bonds() -> None:
    pm = _p_mesh()
    dnet = dualise(pm)
    n_atoms = len(dnet.atoms)
    deg = np.zeros(n_atoms, dtype=np.int64)
    for i, j, _order in dnet.bonds:
        deg[i] += 1
        deg[j] += 1
    assert n_atoms > 0
    assert np.all(deg == 3), (
        f"degree distribution: {np.unique(deg, return_counts=True)}"
    )


def test_p_atom_bond_ring_counts_match_welded_euler() -> None:
    pm = _p_mesh()
    v, e, f, _chi = welded_euler(pm)
    dnet = dualise(pm)
    assert len(dnet.atoms) == f
    assert len(dnet.bonds) == e
    assert len(dnet.rings) == v


def test_p_counting_residual_zero() -> None:
    pm = _p_mesh()
    _v, _e, _f, chi = welded_euler(pm)
    assert chi == -4
    dnet = dualise(pm)
    hist = dnet.ring_histogram()
    assert counting_residual(hist, 0, chi) == 0


def test_p_ring_size_sum_matches_6_chi() -> None:
    """Sigma_rings (6 - size) == 6*chi -- the backlog's acceptance line,
    checked directly rather than only through counting_residual."""
    pm = _p_mesh()
    _v, _e, _f, chi = welded_euler(pm)
    dnet = dualise(pm)
    total = sum(6 - size for size, _members in dnet.rings)
    assert total == 6 * chi


def test_p_ring_members_all_belong_to_that_ring_atom() -> None:
    """Every ring's member list has exactly `size` distinct atom indices,
    and every one of those triangles genuinely touches the ring's welded
    vertex (sanity on the cyclic-order walk, not just the count)."""
    pm = _p_mesh()
    dnet = dualise(pm)
    for size, members in dnet.rings:
        assert len(members) == size
        assert len(set(members)) == size


def test_unroll_2x1x1_doubles_atoms_and_interior_atoms_stay_3_bonded() -> None:
    pm = _p_mesh()
    dnet = dualise(pm)
    n_atoms = len(dnet.atoms)
    rx = 2

    coords1, bonds1 = unroll(dnet, reps=(1, 1, 1))
    assert coords1.shape == (n_atoms, 3)

    coords2, bonds2 = unroll(dnet, reps=(rx, 1, 1))
    assert coords2.shape == (rx * n_atoms, 3)
    # only an x-axis wrap bond can ever survive when ry=rz=1 (a y/z-wrap
    # bond's target index is always out of the [0,1) range on that axis);
    # it survives exactly once, at the one reference cell whose +1 neighbour
    # is still inside the (rx,1,1) box.
    intra = int(np.sum(np.all(dnet.shifts == 0, axis=1)))
    wrap_x_only = int(
        np.sum(
            (dnet.shifts[:, 0] == 1)
            & (dnet.shifts[:, 1] == 0)
            & (dnet.shifts[:, 2] == 0)
        )
    )
    assert len(bonds1) == intra
    assert len(bonds2) == rx * intra + (rx - 1) * wrap_x_only

    deg = np.zeros(len(coords2), dtype=np.int64)
    for i, j in bonds2:
        deg[i] += 1
        deg[j] += 1
    assert np.all(deg <= 3), "unroll must never over-bond an atom"

    # local atom ids that never appear in a wrap-crossing (nonzero-shift)
    # bond at all -- these have no way of being affected by the supercell
    # boundary and must stay 3-bonded in every one of the rx replicas.
    all_local = set(range(n_atoms))
    never_wrapped = all_local - {
        local
        for (i, j, _order), shift in zip(
            dnet.bonds.tolist(), dnet.shifts.tolist(), strict=True
        )
        if any(shift)
        for local in (i, j)
    }
    assert never_wrapped, "fixture has no interior (never wrap-crossing) atoms to check"
    for ci in range(rx):
        base = ci * n_atoms
        for local in never_wrapped:
            assert deg[base + local] == 3

    assert bool(np.any(deg < 3)), "test did not exercise the supercell boundary"


def test_unroll_reps_1_1_1_bond_count_is_intra_cell_only() -> None:
    pm = _p_mesh()
    dnet = dualise(pm)
    n_intra_cell = int(np.sum(np.all(dnet.shifts == 0, axis=1)))
    _coords, bonds = unroll(dnet, reps=(1, 1, 1))
    assert len(bonds) == n_intra_cell
    assert n_intra_cell < len(dnet.bonds), (
        "test fixture has no wrap-crossing bonds to drop -- not exercising "
        "the open-boundary behaviour"
    )


def test_dualise_requires_both_or_neither_of_f_and_grad() -> None:
    pm = _p_mesh()
    with pytest.raises(ValueError):
        dualise(pm, f=lambda pts: schwarz_p(pts, 1.0))
    with pytest.raises(ValueError):
        dualise(pm, grad=lambda pts: schwarz_p_grad(pts, 1.0))


def test_p_projected_dual_bond_lengths_near_mesh_edge_over_sqrt3() -> None:
    """Slice 1's geometry claim: equilateral triangles of edge `a`
    dualise to atoms `a/sqrt(3)` apart. The raw marching-cubes mesh is not
    equilateral, so only the median (not every bond) is asserted, and the
    spread is reported rather than hidden."""
    a = 1.0
    n = 17
    pm = _p_mesh(n=n, a=a)
    dnet = dualise(
        pm,
        f=lambda pts: schwarz_p(pts, a),
        grad=lambda pts: schwarz_p_grad(pts, a),
    )
    lengths = np.linalg.norm(
        dnet.atoms[dnet.bonds[:, 0]] - dnet.atoms[dnet.bonds[:, 1]], axis=1
    )
    # the mesh's own characteristic edge length: median triangle edge
    v0 = pm.verts[pm.tris[:, 0]]
    v1 = pm.verts[pm.tris[:, 1]]
    v2 = pm.verts[pm.tris[:, 2]]
    mesh_edges = np.concatenate(
        [
            np.linalg.norm(v1 - v0, axis=1),
            np.linalg.norm(v2 - v1, axis=1),
            np.linalg.norm(v0 - v2, axis=1),
        ]
    )
    mesh_edge_median = float(np.median(mesh_edges))
    expected = mesh_edge_median / np.sqrt(3.0)
    median_bond = float(np.median(lengths))
    spread = float(np.std(lengths))
    print(
        f"P n={n}: mesh edge median={mesh_edge_median:.4g}, expected dual "
        f"bond={expected:.4g}, median dual bond={median_bond:.4g}, "
        f"spread(std)={spread:.4g}"
    )
    assert median_bond == pytest.approx(expected, rel=0.35)


def test_gyroid_every_atom_has_exactly_3_bonds_and_residual_zero() -> None:
    pm = _gyroid_mesh()
    _v, _e, _f, chi = welded_euler(pm)
    assert chi == -8
    dnet = dualise(
        pm,
        f=lambda pts: gyroid(pts, 1.0),
        grad=lambda pts: gyroid_grad(pts, 1.0),
    )
    deg = np.zeros(len(dnet.atoms), dtype=np.int64)
    for i, j, _order in dnet.bonds:
        deg[i] += 1
        deg[j] += 1
    assert np.all(deg == 3)
    assert counting_residual(dnet.ring_histogram(), 0, chi) == 0
    total = sum(6 - size for size, _members in dnet.rings)
    assert total == 6 * chi
