"""Phase 3 (slice 4a): the rigid-body shove LP — a global min-|Δx|+|Δy|
separation under the round mesh's no-inversion guard. Corridor-edge
demand rows land with slice 2b's wires; these tests pin the overlap-
driven half: residual same-side pairs to zero, fixed parts immobile,
triangles never inverted mid-step, outline containment."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from precis.pcb.topoplace.design import TopoZone
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.proxy import overlap_area
from precis.pcb.topoplace.shove import shove_pose
from tests.fixtures.pcb.topo_example_a import example_a
from tests.fixtures.pcb.topo_synth import synth_design


def test_shove_separates_stacked_pair() -> None:
    """A2 stacked exactly on A1: the coincident rings' shared vertices
    cap the first round's slide (their interior triangles must not
    invert); the de-coincided second round completes the separation."""
    d = example_a()
    pose = d.pose0.copy()
    pose[1] = pose[0].copy()
    assert overlap_area(d, pose) > 0.0
    res = shove_pose(d, pose)
    assert res.resolved and overlap_area(d, res.pose) == pytest.approx(0.0)
    assert res.moved_mm > 0.4
    assert res.min_tri_area_mm2 > 0.0


def test_shove_partial_overlap_at_lp_bound() -> None:
    """One overlapping pair clears in a single LP round — displacement
    within 5 % of the LP optimum (the spec's bound)."""
    d = example_a()
    pose = d.pose0.copy()
    pose[1, 0] = pose[0, 0] + 0.6
    pose[1, 1] = pose[0, 1] + 0.6
    res = shove_pose(d, pose)
    assert res.resolved and res.rounds == 1
    assert res.lp_optimum_mm > 0.0
    assert res.moved_mm <= 1.05 * res.lp_optimum_mm + 1e-6


def test_shove_fixed_parts_never_move() -> None:
    d = example_a()
    pose = d.pose0.copy()
    pose[1] = pose[0].copy()
    fixed = np.asarray([True] + [False] * (len(d.parts) - 1))
    d1 = dataclasses.replace(d, fixed=fixed)
    res = shove_pose(d1, pose)
    np.testing.assert_allclose(res.pose[0], pose[0])
    assert res.resolved

    # both fixed: unresolvable — reported, pose untouched
    d2 = dataclasses.replace(d, fixed=np.ones(len(d.parts), dtype=bool))
    res2 = shove_pose(d2, pose)
    assert not res2.resolved and res2.moved_mm == 0.0
    assert any((a, b) == (0, 1) for a, b, _ in res2.remaining)


def test_shove_no_triangle_inverts_on_squeezed_board() -> None:
    """A jittered 40-part board: every round's mesh keeps positive
    signed areas under that round's step — the topology a slice-2b wire
    would ride is never inverted mid-motion."""
    d, _inst, _nets = synth_design(seed=0, n_parts=40, side=14.0)
    rng = np.random.default_rng(5)
    pose = d.pose0.copy()
    pose[:, 0] += rng.normal(0.0, 1.0, len(pose))
    pose[:, 1] += rng.normal(0.0, 1.0, len(pose))
    pose[:, 0] = np.clip(pose[:, 0], 1.0, 13.0)
    pose[:, 1] = np.clip(pose[:, 1], 1.0, 13.0)
    assert overlap_area(d, pose) > 0.0
    res = shove_pose(d, pose)
    assert res.min_tri_area_mm2 > 0.0
    assert overlap_area(d, res.pose) < overlap_area(d, pose)


def test_shove_pushes_part_off_keepout() -> None:
    """A part intruding on a keepout zone takes the full MTV itself —
    the zone cannot move. Same-net/layer exemptions are legalize's."""
    d = example_a()
    ring = np.asarray([[2.0, -1.0], [4.0, -1.0], [4.0, 1.0], [2.0, 1.0], [2.0, -1.0]])
    d = dataclasses.replace(
        d, zones=(TopoZone(kind="keepout", ring=ring, layers=(), name="k"),)
    )
    mesh = build_mesh(d, d.pose0)
    assert mesh.zone_conflicts  # B1 at (2,0) sits inside
    res = shove_pose(d, d.pose0)
    end = build_mesh(d, res.pose)
    assert not end.zone_conflicts


def test_shove_centres_stay_on_board() -> None:
    """Outline containment: part centres stay inside their legal box —
    the LP bounds — so no new outline escapes are created."""
    d = example_a()
    pose = d.pose0.copy()
    pose[1] = pose[0].copy()
    res = shove_pose(d, pose)
    end = build_mesh(d, res.pose)
    assert not end.outline_conflicts


def test_shove_is_deterministic() -> None:
    d = example_a()
    pose = d.pose0.copy()
    pose[1] = pose[0].copy()
    a, b = shove_pose(d, pose), shove_pose(d, pose)
    np.testing.assert_array_equal(a.pose, b.pose)
    assert a.moved_mm == b.moved_mm
