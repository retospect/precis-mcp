""":mod:`precis.structure.georelax` — the graph-first geometry core lifted
out of `precis_se.atomic.generators.sugars` and generalized for
hybridization-aware angle targets (docs/backlog/se-nanobud-graph.md §1,
slice 1: pure geometry, no store, no se wiring).

Three pieces, tested independently of any ``Scene``/store machinery:
:func:`relax_graph` (the bond-spring + repulsion + VSEPR-angle relax pass,
now hybridization-aware rather than hard-coded sp3), :func:`embed_from_graph`
(spectral coordinate seeding from a bond graph alone — the acceptance
self-test reconstructs C60 from nothing but its adjacency), and
:func:`register` (canonical post-relax rigid framing). The `geo` relax rung
wiring (`relax.py`) is covered in `tests/test_structure_kernel.py`'s relax
section instead, alongside the other rungs.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis.structure.georelax import (
    embed_from_graph,
    kabsch_align,
    register,
    relax_graph,
)
from precis_se.atomic.generators.sp2 import build_fullerene

# ── embed_from_graph: the C60 acceptance self-test ──────────────────────


def _c60_graph() -> tuple[list[str], list[tuple[int, int]], np.ndarray]:
    block = build_fullerene({"atoms": 60})
    bonds = [(i, j) for i, j, _order, _kind in block.bonds]
    return block.elements, bonds, np.asarray(block.coords)


def test_embed_from_graph_reconstructs_c60_from_bonds_alone() -> None:
    """Acceptance criterion (se-nanobud-graph.md): embed-from-graph +
    ``geo`` relax reproduces C60 from its adjacency alone, RMSD to the
    closed-form generator's own coordinates below tolerance — allowing the
    mirror image too (a bare graph carries no chirality information here;
    the spectral seed's sign choice can land on either enantiomer)."""
    elements, bonds, reference = _c60_graph()
    n = len(elements)
    coords = embed_from_graph(n, bonds, bond_length=1.42)
    relax_graph(elements, coords, bonds, set(), hybridizations="sp2", tol=1e-5)

    _aligned, rmsd = kabsch_align(coords, reference)
    mirrored = coords.copy()
    mirrored[:, 2] *= -1.0
    _aligned_m, rmsd_mirror = kabsch_align(mirrored, reference)

    assert min(rmsd, rmsd_mirror) < 0.5


def test_embed_from_graph_is_deterministic() -> None:
    """Same graph, same seed every time — no randomness anywhere (the
    LAPACK eigendecomposition is itself deterministic; the added
    largest-component-positive sign rule fixes the remaining eigenvector
    sign ambiguity)."""
    _elements, bonds, _reference = _c60_graph()
    a = embed_from_graph(60, bonds, bond_length=1.42)
    b = embed_from_graph(60, bonds, bond_length=1.42)
    assert np.array_equal(a, b)


def test_embed_from_graph_median_bond_length_matches_target() -> None:
    _elements, bonds, _reference = _c60_graph()
    coords = embed_from_graph(60, bonds, bond_length=1.42)
    dists = [float(np.linalg.norm(coords[i] - coords[j])) for i, j in bonds]
    assert float(np.median(dists)) == pytest.approx(1.42, abs=1e-6)


def test_embed_from_graph_empty() -> None:
    assert embed_from_graph(0, []).shape == (0, 3)


def test_embed_from_graph_partial_embed_keeps_pinned_coords_exact() -> None:
    """A parent structure supplies known coordinates for a subset (e.g. the
    untouched atoms of a graph-surgery junction); those rows come back
    EXACTLY unchanged, and the freshly spectral-seeded rest lands rigidly
    aligned onto that frame (not the spectral seed's own arbitrary one)."""
    elements, bonds, reference = _c60_graph()
    n = len(elements)
    pinned = frozenset(range(12))  # an arbitrary "already-known" subset
    seed = np.zeros((n, 3))
    seed[list(pinned)] = reference[list(pinned)]

    coords = embed_from_graph(n, bonds, seed_coords=seed, pinned=pinned)
    assert np.allclose(coords[list(pinned)], reference[list(pinned)])


# ── relax_graph: hybridization-aware angle targets ───────────────────────


def _angle_deg(coords: np.ndarray, a: int, b: int, c: int) -> float:
    v1 = coords[a] - coords[b]
    v2 = coords[c] - coords[b]
    cos_t = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))
    return float(np.degrees(np.arccos(np.clip(cos_t, -1.0, 1.0))))


def test_relax_graph_sp2_converges_to_120_not_10947() -> None:
    """The new capability this move exists for: an sp2-labeled centre
    restores toward the 120 degree trigonal-planar ideal, not sp3's
    109.47 degree tetrahedral one — `sugars.py`'s original ``_relax`` had
    no way to ask for this (hard-coded sp3 throughout)."""
    elements = ["C", "C", "C", "C"]
    coords = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.6, 0.3, 0.05],
            [-0.7, 1.5, -0.1],
            [-0.6, -1.3, 0.05],
        ]
    )
    bonds = [(0, 1), (0, 2), (0, 3)]
    trace = relax_graph(elements, coords, bonds, set(), hybridizations="sp2", tol=1e-6)
    assert trace.converged
    for a, c in ((1, 2), (1, 3), (2, 3)):
        assert _angle_deg(coords, a, 0, c) == pytest.approx(120.0, abs=2.0)
    # bond lengths approach the C-C covalent-radii-sum target too
    for i, j in bonds:
        assert 1.4 < float(np.linalg.norm(coords[i] - coords[j])) < 1.65


def test_relax_graph_sp3_still_converges_to_10947() -> None:
    """The pre-existing (default) behavior — `sugars.py`'s exact original
    target — still holds when a caller asks for sp3 explicitly or omits
    ``hybridizations=`` entirely."""
    elements = ["C", "C", "C", "C", "C"]
    coords = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.6, 0.2, 0.1],
            [-0.5, 1.5, 0.3],
            [-0.5, -1.4, 0.2],
            [0.1, -0.2, 1.6],
        ]
    )
    bonds = [(0, 1), (0, 2), (0, 3), (0, 4)]
    trace = relax_graph(elements, coords, bonds, set(), tol=1e-6)  # default sp3
    assert trace.converged
    pairs = [(1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)]
    for a, c in pairs:
        assert _angle_deg(coords, a, 0, c) == pytest.approx(109.47, abs=5.0)


def test_relax_graph_pinned_atoms_never_move() -> None:
    elements = ["C", "C", "C"]
    coords = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [-1.0, 0.3, 0.0]])
    bonds = [(0, 1), (0, 2)]
    before = coords.copy()
    relax_graph(elements, coords, bonds, {0}, tol=1e-6)
    assert np.array_equal(coords[0], before[0])
    assert not np.array_equal(coords[1], before[1])


def test_relax_graph_tol_none_runs_full_iters_no_break() -> None:
    """`sugars.py` never passes ``tol=`` — confirm the default (``None``)
    genuinely runs the whole ``iters`` budget rather than stopping early,
    the exact original control flow (no early-stop existed before this
    move)."""
    elements = ["C", "C"]
    coords = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 0.0]])
    bonds = [(0, 1)]
    trace = relax_graph(elements, coords, bonds, set(), iters=10)
    assert trace.n_steps == 10
    assert trace.converged is False


# ── register: canonical rigid framing ────────────────────────────────────


def _sp2_frame_coords() -> tuple[list[str], np.ndarray]:
    elements = ["C", "C", "C", "C"]
    coords = np.array(
        [
            [2.0, -1.0, 3.0],  # anchor, deliberately not at the origin
            [3.6, -0.7, 2.95],
            [1.3, 0.5, 2.9],
            [1.4, -2.3, 3.05],
        ]
    )
    return elements, coords


def test_register_places_anchor_at_origin_and_neighbor_on_x() -> None:
    _elements, coords = _sp2_frame_coords()
    reg = register(coords, anchor=0, x_neighbor=1, anchor_neighbors=[1, 2, 3])
    assert np.allclose(reg[0], [0.0, 0.0, 0.0], atol=1e-9)
    assert reg[1][1] == pytest.approx(0.0, abs=1e-9)
    assert reg[1][2] == pytest.approx(0.0, abs=1e-9)
    assert reg[1][0] > 0.0


def test_register_is_right_handed() -> None:
    _elements, coords = _sp2_frame_coords()
    reg = register(coords, anchor=0, x_neighbor=1, anchor_neighbors=[1, 2, 3])
    x_hat = reg[1] / np.linalg.norm(reg[1])
    # reconstruct y/z from the registered neighbor plane and confirm x.y=z
    z_hat = np.array([0.0, 0.0, 1.0])
    y_hat = np.cross(z_hat, x_hat)
    assert np.allclose(np.cross(x_hat, y_hat), z_hat, atol=1e-6)


def test_register_inward_normal_points_toward_whole_centroid() -> None:
    """The deterministic sign rule: whatever direction the rest of the
    structure sits in (here, a single far-away point standing in for
    "the rest of the molecule"), the registered +z always points that
    way — "the bud always pops the same way", independent of which side
    of the anchor's local plane the rest of the structure started on."""
    coords_a = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.5, 0.0, -0.1],
            [-0.7, 1.3, -0.1],
            [-0.7, -1.3, -0.1],
            [0.0, 0.0, 10.0],
        ]
    )
    coords_b = coords_a.copy()
    coords_b[4] = [0.0, 0.0, -10.0]

    reg_a = register(coords_a, anchor=0, x_neighbor=1, anchor_neighbors=[1, 2, 3])
    reg_b = register(coords_b, anchor=0, x_neighbor=1, anchor_neighbors=[1, 2, 3])
    assert reg_a[4][2] > 0.0
    assert reg_b[4][2] > 0.0
    assert reg_a[4][2] == pytest.approx(reg_b[4][2], abs=1e-6)


def test_register_is_idempotent() -> None:
    _elements, coords = _sp2_frame_coords()
    once = register(coords, anchor=0, x_neighbor=1, anchor_neighbors=[1, 2, 3])
    twice = register(once, anchor=0, x_neighbor=1, anchor_neighbors=[1, 2, 3])
    assert np.allclose(once, twice, atol=1e-8)


def test_register_explicit_plane_ref_reorthogonalizes_against_x() -> None:
    _elements, coords = _sp2_frame_coords()
    # An explicit +z that is deliberately NOT perpendicular to the
    # anchor->x_neighbor bond -- still comes out exactly perpendicular
    # after Gram-Schmidt.
    reg = register(coords, anchor=0, x_neighbor=1, plane_ref=np.array([0.3, 0.1, 1.0]))
    x_hat = reg[1] / np.linalg.norm(reg[1])
    assert x_hat == pytest.approx([1.0, 0.0, 0.0], abs=1e-9)


def test_register_rejects_degenerate_frame() -> None:
    _elements, coords = _sp2_frame_coords()
    x_hat = (coords[1] - coords[0]) / np.linalg.norm(coords[1] - coords[0])
    with pytest.raises(ValueError, match="parallel"):
        register(coords, anchor=0, x_neighbor=1, plane_ref=x_hat)


def test_register_needs_at_least_two_neighbors_for_inward() -> None:
    _elements, coords = _sp2_frame_coords()
    with pytest.raises(ValueError, match="anchor_neighbors"):
        register(coords, anchor=0, x_neighbor=1, anchor_neighbors=[1])


# ── kabsch_align ──────────────────────────────────────────────────────────


def test_kabsch_align_recovers_a_known_rotation() -> None:
    rng = np.random.default_rng(0)
    points = rng.normal(size=(20, 3))
    theta = 0.7
    rot = np.array(
        [
            [np.cos(theta), -np.sin(theta), 0.0],
            [np.sin(theta), np.cos(theta), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    target = points @ rot.T + np.array([5.0, -2.0, 1.0])
    aligned, rmsd = kabsch_align(points, target)
    assert rmsd < 1e-8
    assert np.allclose(aligned, target, atol=1e-6)
