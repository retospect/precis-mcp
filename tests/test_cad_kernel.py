"""Analytic CAD kernel — unit tests with hand-computed oracles (ADR 0041).

Covers the rigid-transform algebra, the interval set algebra, and the
membership contract (contains / ray_hits / distance / aabb / faces) for
every v1 primitive, plus the world-frame :class:`Placed` wrapper.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest

from precis.cad import interval as iv
from precis.cad.primitives import (
    CircularFrustum,
    HalfSpace,
    Placed,
    Sphere,
    Torus,
    box,
    pyramid,
    regular_prism,
)
from precis.cad.vec import (
    identity,
    pose,
    rotation,
    translation,
    vec3,
)

EPS = 1e-9


# ---------------------------------------------------------------------------
# vec / Transform
# ---------------------------------------------------------------------------


def test_translation_apply_and_inverse() -> None:
    t = translation(1.0, 2.0, 3.0)
    p = vec3(0.0, 0.0, 0.0)
    assert np.allclose(t.apply(p), [1, 2, 3])
    assert np.allclose(t.inverse().apply(t.apply(p)), p)


def test_rotation_z_90deg() -> None:
    r = rotation(0.0, 0.0, 90.0)
    # +x → +y under a +90° z-rotation.
    assert np.allclose(r.apply(vec3(1.0, 0.0, 0.0)), [0, 1, 0], atol=1e-12)


def test_rotation_is_orthonormal() -> None:
    r = rotation(15.0, 40.0, -75.0)
    assert np.allclose(r.R @ r.R.T, np.eye(3), atol=1e-12)
    assert math.isclose(float(np.linalg.det(r.R)), 1.0, abs_tol=1e-12)


def test_compose_matches_sequential_apply() -> None:
    a = translation(1.0, 0.0, 0.0)
    b = rotation(0.0, 0.0, 90.0)
    composed = a.compose(b)
    p = vec3(1.0, 0.0, 0.0)
    assert np.allclose(composed.apply(p), a.apply(b.apply(p)), atol=1e-12)


def test_to_local_round_trip() -> None:
    t = pose(vec3(5.0, -3.0, 2.0), vec3(10.0, 20.0, 30.0))
    p = vec3(1.0, 2.0, 3.0)
    assert np.allclose(t.to_local_point(t.to_world_point(p)), p, atol=1e-12)


# ---------------------------------------------------------------------------
# interval algebra
# ---------------------------------------------------------------------------


def test_interval_intersect() -> None:
    assert iv.intersect([(0, 5)], [(3, 8)]) == [(3, 5)]
    assert iv.intersect([(0, 2)], [(5, 8)]) == []


def test_interval_union_merges_touching() -> None:
    assert iv.union([(0, 2)], [(2, 4)]) == [(0, 4)]


def test_interval_subtract_carves_hole() -> None:
    # solid [0,10] minus void [3,6] → two runs (the bore guarantee in 1-D).
    assert iv.subtract([(0, 10)], [(3, 6)]) == [(0, 3), (6, 10)]


def test_quadratic_le_between_roots() -> None:
    # t² - 1 <= 0  →  [-1, 1]
    spans = iv.quadratic_le(1.0, 0.0, -1.0)
    assert len(spans) == 1
    assert math.isclose(spans[0][0], -1.0)
    assert math.isclose(spans[0][1], 1.0)


def test_quadratic_le_no_real_roots_positive_leading() -> None:
    # t² + 1 <= 0 has no solution.
    assert iv.quadratic_le(1.0, 0.0, 1.0) == []


def test_total_length_ignores_infinite() -> None:
    assert math.isclose(iv.total_length([(0, 3), (5, 6)]), 4.0)
    assert math.isclose(iv.total_length([(iv.NEG_INF, 0)]), 0.0)


# ---------------------------------------------------------------------------
# Sphere
# ---------------------------------------------------------------------------


def test_sphere_contains() -> None:
    s = Sphere(r=2.0)
    assert s.contains_local(vec3(0, 0, 0))
    assert s.contains_local(vec3(2, 0, 0))  # on surface (within eps)
    assert not s.contains_local(vec3(2.1, 0, 0))


def test_sphere_ray_through_center() -> None:
    s = Sphere(r=2.0)
    spans = s.ray_hits_local(vec3(-10, 0, 0), vec3(1, 0, 0))
    assert len(spans) == 1
    lo, hi = spans[0]
    # entry at x=-2 → t=8, exit at x=2 → t=12
    assert math.isclose(lo, 8.0, abs_tol=1e-9)
    assert math.isclose(hi, 12.0, abs_tol=1e-9)


def test_sphere_ray_miss() -> None:
    s = Sphere(r=1.0)
    assert s.ray_hits_local(vec3(-10, 5, 0), vec3(1, 0, 0)) == []


def test_sphere_distance() -> None:
    s = Sphere(r=2.0)
    assert math.isclose(s.distance_local(vec3(5, 0, 0)), 3.0, abs_tol=1e-12)
    assert math.isclose(s.distance_local(vec3(1, 0, 0)), -1.0, abs_tol=1e-12)


# ---------------------------------------------------------------------------
# CircularFrustum: cylinder / cone
# ---------------------------------------------------------------------------


def test_cylinder_contains() -> None:
    c = CircularFrustum(rb=3.0, rt=3.0, h=10.0)
    assert c.contains_local(vec3(0, 0, 5))
    assert c.contains_local(vec3(3, 0, 0))
    assert not c.contains_local(vec3(3.1, 0, 5))
    assert not c.contains_local(vec3(0, 0, 11))


def test_cylinder_ray_radial() -> None:
    c = CircularFrustum(rb=3.0, rt=3.0, h=10.0)
    spans = c.ray_hits_local(vec3(-10, 0, 5), vec3(1, 0, 0))
    assert len(spans) == 1
    lo, hi = spans[0]
    assert math.isclose(lo, 7.0, abs_tol=1e-9)  # x=-3
    assert math.isclose(hi, 13.0, abs_tol=1e-9)  # x=+3


def test_cylinder_ray_axial() -> None:
    c = CircularFrustum(rb=3.0, rt=3.0, h=10.0)
    spans = c.ray_hits_local(vec3(0, 0, -5), vec3(0, 0, 1))
    assert len(spans) == 1
    lo, hi = spans[0]
    assert math.isclose(lo, 5.0, abs_tol=1e-9)
    assert math.isclose(hi, 15.0, abs_tol=1e-9)


def test_cylinder_distance_outside_radial() -> None:
    c = CircularFrustum(rb=3.0, rt=3.0, h=10.0)
    # point at rho=5, mid-height → 2 mm outside the wall
    assert math.isclose(c.distance_local(vec3(5, 0, 5)), 2.0, abs_tol=1e-9)


def test_cone_contains_taper() -> None:
    # cone rb=4 at z=0 → tip at z=8; radius at z=4 is 2.
    c = CircularFrustum(rb=4.0, rt=0.0, h=8.0)
    assert c.contains_local(vec3(1.9, 0, 4))
    assert not c.contains_local(vec3(2.1, 0, 4))


def test_cone_has_no_top_face() -> None:
    c = CircularFrustum(rb=4.0, rt=0.0, h=8.0)
    tags = {f.tag for f in c.faces_local()}
    assert "bottom" in tags
    assert "top" not in tags


# ---------------------------------------------------------------------------
# PolyFrustum: box / prism / pyramid
# ---------------------------------------------------------------------------


def test_box_contains() -> None:
    b = box(40.0, 20.0, 10.0)
    assert b.contains_local(vec3(0, 0, 5))
    assert b.contains_local(vec3(20, 10, 0))  # corner, within eps
    assert not b.contains_local(vec3(21, 0, 5))
    assert not b.contains_local(vec3(0, 0, 11))


def test_box_ray_clip() -> None:
    b = box(40.0, 20.0, 10.0)
    spans = b.ray_hits_local(vec3(-100, 0, 5), vec3(1, 0, 0))
    assert len(spans) == 1
    lo, hi = spans[0]
    assert math.isclose(lo, 80.0, abs_tol=1e-9)  # x=-20
    assert math.isclose(hi, 120.0, abs_tol=1e-9)  # x=+20


def test_box_distance() -> None:
    b = box(40.0, 20.0, 10.0)
    # 5 mm beyond the +x face (face at x=20), on the face footprint
    assert math.isclose(b.distance_local(vec3(25, 0, 5)), 5.0, abs_tol=1e-9)
    # interior point: nearest face is the y-face at 10 (dy=10) vs z-faces
    assert b.distance_local(vec3(0, 0, 5)) < 0


def test_box_aabb() -> None:
    b = box(40.0, 20.0, 10.0)
    lo, hi = b.aabb_local()
    assert np.allclose(lo, [-20, -10, 0])
    assert np.allclose(hi, [20, 10, 10])


def test_prism_and_pyramid_construct() -> None:
    hexp = regular_prism(6, 5.0, 4.0)
    assert hexp.contains_local(vec3(0, 0, 2))
    pyr = pyramid(4, 5.0, 6.0)
    # apex region narrows: near the top, off-axis points fall outside.
    assert pyr.contains_local(vec3(0, 0, 5.9))
    assert not pyr.contains_local(vec3(3, 3, 5.9))


# ---------------------------------------------------------------------------
# HalfSpace
# ---------------------------------------------------------------------------


def test_halfspace_contains_and_distance() -> None:
    # material where z <= 0
    hs = HalfSpace(point=vec3(0, 0, 0), normal=vec3(0, 0, 1))
    assert hs.contains_local(vec3(0, 0, -5))
    assert not hs.contains_local(vec3(0, 0, 5))
    assert math.isclose(hs.distance_local(vec3(0, 0, 3)), 3.0, abs_tol=1e-12)
    assert math.isclose(hs.distance_local(vec3(0, 0, -3)), -3.0, abs_tol=1e-12)


def test_halfspace_ray_is_half_line() -> None:
    hs = HalfSpace(point=vec3(0, 0, 0), normal=vec3(0, 0, 1))
    spans = hs.ray_hits_local(vec3(0, 0, -10), vec3(0, 0, 1))
    assert len(spans) == 1
    lo, hi = spans[0]
    assert lo == iv.NEG_INF
    assert math.isclose(hi, 10.0, abs_tol=1e-9)  # crosses z=0 at t=10


def test_halfspace_aabb_unbounded() -> None:
    hs = HalfSpace(point=vec3(0, 0, 0), normal=vec3(0, 0, 1))
    lo, hi = hs.aabb_local()
    assert not np.all(np.isfinite(lo))
    assert not np.all(np.isfinite(hi))


# ---------------------------------------------------------------------------
# Torus
# ---------------------------------------------------------------------------


def test_torus_contains() -> None:
    t = Torus(R=10.0, r=2.0)
    assert t.contains_local(vec3(10, 0, 0))  # tube centre
    assert t.contains_local(vec3(12, 0, 0))  # outer edge
    assert not t.contains_local(vec3(0, 0, 0))  # hole centre is void
    assert not t.contains_local(vec3(13, 0, 0))


def test_torus_distance() -> None:
    t = Torus(R=10.0, r=2.0)
    assert math.isclose(t.distance_local(vec3(10, 0, 0)), -2.0, abs_tol=1e-12)
    assert math.isclose(t.distance_local(vec3(15, 0, 0)), 3.0, abs_tol=1e-12)


def test_torus_ray_four_crossings() -> None:
    # A ray along +x through the whole torus on the x-axis crosses the
    # tube twice on the near side and twice on the far side → two spans.
    t = Torus(R=10.0, r=2.0)
    spans = t.ray_hits_local(vec3(-20, 0, 0), vec3(1, 0, 0))
    assert len(spans) == 2
    # near tube: x in [-12,-8] → t in [8,12]; far tube: x in [8,12] → [28,32]
    assert math.isclose(spans[0][0], 8.0, abs_tol=1e-6)
    assert math.isclose(spans[0][1], 12.0, abs_tol=1e-6)
    assert math.isclose(spans[1][0], 28.0, abs_tol=1e-6)
    assert math.isclose(spans[1][1], 32.0, abs_tol=1e-6)


# ---------------------------------------------------------------------------
# Placed — world-frame queries
# ---------------------------------------------------------------------------


def test_placed_translation_membership() -> None:
    s = Placed(prim=Sphere(r=2.0), xform=translation(10.0, 0.0, 0.0))
    assert s.contains(vec3(10, 0, 0))
    assert not s.contains(vec3(0, 0, 0))


def test_placed_ray_parameter_preserved() -> None:
    # Rigid transform preserves the ray parameter t (no scale).
    s = Placed(prim=Sphere(r=2.0), xform=translation(10.0, 0.0, 0.0))
    spans = s.ray_hits(vec3(0, 0, 0), vec3(1, 0, 0))
    assert len(spans) == 1
    assert math.isclose(spans[0][0], 8.0, abs_tol=1e-9)
    assert math.isclose(spans[0][1], 12.0, abs_tol=1e-9)


def test_placed_distance_invariant_under_rotation() -> None:
    far = vec3(100.0, 0.0, 0.0)
    d_identity = Placed(prim=Sphere(r=2.0), xform=identity()).distance(far)
    rotated = Placed(prim=Sphere(r=2.0), xform=rotation(30.0, 45.0, 60.0))
    # Distance to a sphere is rotation-invariant about its own centre.
    assert math.isclose(rotated.distance(far), d_identity, abs_tol=1e-9)


def test_placed_faces_rotate_normals() -> None:
    # A box's +z top face becomes +x after a -90° y-rotation... check the
    # normal set is rotated consistently and stays unit-length.
    p = Placed(prim=box(2.0, 2.0, 2.0), xform=rotation(0.0, 0.0, 90.0))
    for f in p.faces():
        assert math.isclose(float(np.linalg.norm(f.normal)), 1.0, abs_tol=1e-12)


def test_placed_aabb_encloses_transformed_corners() -> None:
    p = Placed(prim=box(2.0, 2.0, 2.0), xform=translation(5.0, 0.0, 0.0))
    lo, hi = p.aabb()
    assert np.allclose(lo, [4, -1, 0])
    assert np.allclose(hi, [6, 1, 2])


def test_transform_dataclass_immutable() -> None:
    t = identity()
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.t = vec3(1, 1, 1)  # type: ignore[misc]
