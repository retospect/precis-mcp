"""``ring_faces``/``smooth_sheet``/``deviation``/``sheet_mesh`` (gr450675) —
exercised against two real sp² generators (:mod:`precis_se.atomic.
generators.sp2`) so the ring perception and Taubin smoothing are checked
against actual chemistry, not a hand-drawn toy graph."""

from __future__ import annotations

from collections import Counter

import numpy as np

from precis.viz3d.sheetsmooth import deviation, ring_faces, sheet_mesh, smooth_sheet
from precis_se.atomic.generators.sp2 import build_cnt, build_fullerene

_C60 = build_fullerene({"atoms": 60})
_C60_COORDS = _C60.coords
_C60_BONDS = [(i, j) for i, j, _order, _kind in _C60.bonds]

_CNT = build_cnt({"n": 10, "m": 0, "length_A": 20.0})
_CNT_COORDS = _CNT.coords
_CNT_BONDS = [(i, j) for i, j, _order, _kind in _CNT.bonds]


def test_c60_ring_faces_is_12_pentagons_20_hexagons() -> None:
    faces = ring_faces(len(_C60_COORDS), _C60_BONDS, max_ring=8)
    sizes = Counter(len(f) for f in faces)
    assert sizes == {5: 12, 6: 20}
    assert len(faces) == 32


def test_c60_every_atom_is_in_exactly_3_rings() -> None:
    faces = ring_faces(len(_C60_COORDS), _C60_BONDS, max_ring=8)
    counts = [0] * len(_C60_COORDS)
    for ring in faces:
        for atom in ring:
            counts[atom] += 1
    assert counts == [3] * len(_C60_COORDS)


def test_c60_ring_faces_deterministic() -> None:
    a = ring_faces(len(_C60_COORDS), _C60_BONDS, max_ring=8)
    b = ring_faces(len(_C60_COORDS), _C60_BONDS, max_ring=8)
    assert a == b


def test_cnt_interior_rings_are_all_hexagons() -> None:
    """(10, 0) tube, 20 Å long: every ring the perceiver finds within
    ``max_ring`` is a closed hexagon — an open rim bond has no closing
    alternate path short enough to qualify, so it contributes no (partial,
    wrong-sized) ring at all, the honest degrade the module docstring
    promises."""
    faces = ring_faces(len(_CNT_COORDS), _CNT_BONDS, max_ring=8)
    assert faces  # the tube body has real hexagons to find
    assert {len(f) for f in faces} == {6}
    # every ring atom sits on a real hexagon: fully 3-coordinate (an
    # open-rim atom, degree < 3, never survives into a returned ring).
    degree = [0] * len(_CNT_COORDS)
    for i, j in _CNT_BONDS:
        degree[i] += 1
        degree[j] += 1
    for ring in faces:
        assert all(degree[a] == 3 for a in ring)


def test_c60_taubin_smoothing_keeps_the_mean_radius_within_2_percent() -> None:
    smooth = smooth_sheet(_C60_COORDS, _C60_BONDS, iters=20)
    r0 = float(np.linalg.norm(_C60_COORDS, axis=1).mean())
    r1 = float(np.linalg.norm(smooth, axis=1).mean())
    assert abs(r1 - r0) / r0 < 0.02


def test_c60_smoothed_deviation_is_small_and_uniform() -> None:
    """A perfect C60 is already close to its own smoothed shell (every atom
    geometrically equivalent), so the aberration signal should be small in
    absolute terms (< 0.15 Å) and near-uniform across atoms (std/mean <
    0.3) — a real design's local strain would break this uniformity, which
    is exactly the signal the viewer wants to show."""
    smooth = smooth_sheet(_C60_COORDS, _C60_BONDS, iters=20)
    dev = deviation(_C60_COORDS, smooth)
    assert dev.max() < 0.15
    assert dev.std() / dev.mean() < 0.3


def test_a_perturbed_atom_shows_the_largest_deviation() -> None:
    perturbed = _C60_COORDS.copy()
    perturbed[5] = perturbed[5] + np.array([0.5, 0.0, 0.0])
    smooth = smooth_sheet(perturbed, _C60_BONDS, iters=20)
    dev = deviation(perturbed, smooth)
    assert int(np.argmax(dev)) == 5


def test_smooth_sheet_deterministic() -> None:
    a = smooth_sheet(_C60_COORDS, _C60_BONDS, iters=20)
    b = smooth_sheet(_C60_COORDS, _C60_BONDS, iters=20)
    assert np.array_equal(a, b)


def test_sheet_mesh_fan_triangulates_every_ring() -> None:
    faces = ring_faces(len(_C60_COORDS), _C60_BONDS, max_ring=8)
    verts, tris = sheet_mesh(_C60_COORDS, faces)
    assert verts.shape == _C60_COORDS.shape
    # a pentagon fans into 3 triangles, a hexagon into 4.
    expected = sum(len(f) - 2 for f in faces)
    assert tris.shape == (expected, 3)
    # every triangle vertex index is a real atom index.
    assert tris.min() >= 0
    assert tris.max() < len(_C60_COORDS)
