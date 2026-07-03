"""Bulk integrals — sampled volume / centroid (ADR 0041 §8)."""

from __future__ import annotations

import math

from precis.cad.bulk import volume
from precis.cad.dsl import build_config
from precis.cad.graph import Design
from precis.cad.vec import translation, vec3


def test_volume_of_box_matches_analytic() -> None:
    d = Design()
    d.add_component("b", d.prim("box", build_config("box:w40d20h10")))
    res = volume(d, samples=200_000)
    analytic = 40 * 20 * 10
    assert res.sampled is True
    assert math.isclose(res.volume, analytic, rel_tol=0.02)


def test_volume_of_cylinder_matches_analytic() -> None:
    d = Design()
    d.add_component("c", d.prim("cyl", build_config("cyl:r10h20")))
    res = volume(d, samples=300_000)
    analytic = math.pi * 100 * 20
    assert math.isclose(res.volume, analytic, rel_tol=0.02)


def test_volume_honours_subtraction() -> None:
    d = Design()
    plate = d.prim("plate", build_config("cyl:r10h10"))
    bore = d.prim("bore", build_config("cyl:r5h12"), translation(0, 0, -1))
    d.add_component("part", d.subtract(plate, bore))
    res = volume(d, samples=300_000)
    analytic = math.pi * (100 - 25) * 10  # annulus × height
    assert math.isclose(res.volume, analytic, rel_tol=0.03)


def test_centroid_of_centered_box_near_origin_xy() -> None:
    d = Design()
    d.add_component("b", d.prim("box", build_config("box:w20d20h10")))
    res = volume(d, samples=200_000)
    assert abs(res.centroid[0]) < 0.2
    assert abs(res.centroid[1]) < 0.2
    assert math.isclose(res.centroid[2], 5.0, abs_tol=0.2)  # base at z=0, h=10


def test_box_volume_is_exact() -> None:
    # Ray-interval quadrature integrates the exact solid length, so an
    # axis-aligned box (constant height over a rectangle) is exact — the old
    # Monte-Carlo estimator could only get within ~2%.
    d = Design()
    d.add_component("b", d.prim("box", build_config("box:w40d20h10")))
    res = volume(d)
    assert math.isclose(res.volume, 40 * 20 * 10, rel_tol=1e-9)
    assert res.rel_err == 0.0


def test_rel_err_reported() -> None:
    # A cylinder fills only ~π/4 of its AABB, so the binomial estimate has
    # a non-zero standard error (a box fills its AABB exactly → rel_err 0).
    d = Design()
    d.add_component("c", d.prim("cyl", build_config("cyl:r5h10")))
    res = volume(d, samples=100_000)
    assert 0 < res.rel_err < 0.05
    _ = vec3  # keep import used
