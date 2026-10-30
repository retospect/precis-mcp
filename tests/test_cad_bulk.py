"""Bulk integrals — sampled volume / centroid."""

from __future__ import annotations

import math

import numpy as np

from precis.cad.bulk import _integrate, expr_aabb, volume, volume_field
from precis.cad.dsl import build_config
from precis.cad.fold import Expr, Inter, Leaf, material_intervals
from precis.cad.graph import Design
from precis.cad.scene import build_design, parse_source
from precis.cad.vec import Vec3, translation, vec3


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


# ---------------------------------------------------------------------------
# chamfer — AABB-through-transform regression + volume behaviour
# ---------------------------------------------------------------------------

_UNCHAMFERED_BOX = "component part\nbody add box:w40mmd20mmh10mm\n"
_CHAMFERED_BOX = (
    "component part\nbody  add box:w40mmd20mmh10mm\n"
    "bevel cut chamfer:2mmx45deg @20mm,0mm,10mm\n"
)


def test_chamfered_box_component_aabb_is_finite() -> None:
    # The chamfer half-space leaf is unbounded (±inf local AABB); a naive
    # 8-corner rotate of that box would produce inf*0 = NaN. expr_aabb only
    # unions *finite* leaf AABBs, so a cut/intersect-guarded chamfer (never
    # the base) leaves the component's AABB finite and equal to the
    # unchamfered box's (the bevel only removes material, never grows the
    # bound).
    design = build_design(parse_source(_CHAMFERED_BOX))
    lo, hi = expr_aabb(design, design.components["part"])
    assert math.isfinite(float(lo[0])) and math.isfinite(float(hi[0]))
    assert math.isfinite(float(lo[1])) and math.isfinite(float(hi[1]))
    assert math.isfinite(float(lo[2])) and math.isfinite(float(hi[2]))
    assert not any(math.isnan(x) for x in (*lo, *hi))
    base_design = build_design(parse_source(_UNCHAMFERED_BOX))
    blo, bhi = expr_aabb(base_design, base_design.components["part"])
    assert np.allclose(lo, blo) and np.allclose(hi, bhi)


def test_chamfered_box_volume_is_finite_and_smaller() -> None:
    design = build_design(parse_source(_CHAMFERED_BOX))
    res = volume(design, component="part")
    assert math.isfinite(res.volume)
    base_design = build_design(parse_source(_UNCHAMFERED_BOX))
    base_res = volume(base_design, component="part")
    assert 0 < res.volume < base_res.volume


# ---------------------------------------------------------------------------
# vectorised quadrature — parity with the per-ray reference loop
# ---------------------------------------------------------------------------


def _reference_integrate(
    expr: Expr, instances, lo, hi, grid: int
) -> tuple[float, Vec3]:
    """The pre-vectorisation ``_integrate``: one ``material_intervals``
    call per grid ray, Python-summed. Kept as the parity oracle."""
    dx = (hi[0] - lo[0]) / grid
    dy = (hi[1] - lo[1]) / grid
    cell_area = dx * dy
    xs = lo[0] + (np.arange(grid) + 0.5) * dx
    ys = lo[1] + (np.arange(grid) + 0.5) * dy
    z_span = float(hi[2] - lo[2])
    margin = (
        z_span if z_span > 0.0 else max(float(hi[0] - lo[0]), float(hi[1] - lo[1]), 1.0)
    )
    o_z = float(lo[2]) - margin
    d = vec3(0.0, 0.0, 1.0)
    length_sum = 0.0
    cx = cy = cz = 0.0
    for x in xs:
        for y in ys:
            o = vec3(float(x), float(y), o_z)
            for t0, t1 in material_intervals(expr, o, d, instances):
                if math.isinf(t0) or math.isinf(t1):
                    continue
                length = t1 - t0
                length_sum += length
                cx += float(x) * length
                cy += float(y) * length
                cz += (o_z + 0.5 * (t0 + t1)) * length
    vol = length_sum * cell_area
    centroid = (
        vec3(cx / length_sum, cy / length_sum, cz / length_sum)
        if length_sum > 0
        else vec3(0.0, 0.0, 0.0)
    )
    return vol, centroid


def _overlap_probe_design() -> tuple[Design, Inter]:
    """A small union-and-cut component intersected with a tool — exercises
    Union / Diff / Inter folds over the vectorised (convex) leaf path."""
    d = Design()
    plate = d.prim("plate", build_config("box:w40d40h5"))
    bore = d.prim("bore", build_config("cyl:r4h12"), translation(5, 3, -3))
    post = d.prim("post", build_config("cyl:r6h8"), translation(-10, -8, 0))
    comp = d.merge(d.subtract(plate, bore), post)
    tool = d.prim("tool", build_config("cyl:r3h8"), translation(-8, -8, -1))
    return d, d.intersect(comp, tool)


def test_vectorized_integrate_matches_reference_loop() -> None:
    d, expr = _overlap_probe_design()
    lo, hi = expr_aabb(d, expr)
    ref_vol, ref_c = _reference_integrate(expr, d.instances, lo, hi, 24)
    vol, cen = _integrate(expr, d.instances, lo, hi, 24)
    assert math.isclose(vol, ref_vol, rel_tol=1e-9)
    assert np.allclose(cen, ref_c, atol=1e-9)


def test_vectorized_integrate_scalar_leaf_fallback() -> None:
    # A torus exercises the non-convex scalar ``ray_hits`` fallback path —
    # a radial ray can cross it twice.
    d = Design()
    tor = d.prim("tor", build_config("torus:R8r2"))
    expr = d.merge(tor)
    lo, hi = expr_aabb(d, expr)
    ref_vol, ref_c = _reference_integrate(expr, d.instances, lo, hi, 24)
    vol, cen = _integrate(expr, d.instances, lo, hi, 24)
    assert math.isclose(vol, ref_vol, rel_tol=1e-9)
    assert np.allclose(cen, ref_c, atol=1e-9)


def test_vectorized_integrate_empty_expr_is_zero() -> None:
    d = Design()
    a = d.prim("a", build_config("box:w10d10h10"))
    b = d.prim("b", build_config("box:w10d10h10"), translation(100, 0, 0))
    expr = d.intersect(a, b)
    lo, hi = expr_aabb(d, expr)
    vol, cen = _integrate(expr, d.instances, lo, hi, 16)
    assert vol == 0.0
    assert np.allclose(cen, vec3(0.0, 0.0, 0.0))


def test_disjoint_inter_bound_is_empty_not_inverted() -> None:
    # Parts that never touch: the Inter meet collapses to a zero-size box
    # (lo == hi on the violated axis) rather than leaking lo > hi.
    d = Design()
    a = d.prim("a", build_config("box:w10d10h10"))
    b = d.prim("b", build_config("box:w10d10h10"), translation(100, 0, 0))
    lo, hi = expr_aabb(d, d.intersect(a, b))
    assert np.all(np.asarray(lo) <= np.asarray(hi))
    assert np.any(np.asarray(hi) == np.asarray(lo))


# ---------------------------------------------------------------------------
# auto grid — resolution scales to the probe AABB
# ---------------------------------------------------------------------------


def test_intersect_probe_aabb_is_the_overlap_not_the_union() -> None:
    # The ``Inter`` bound is the *meet* of the parts' boxes: a probe the
    # size of the tool, not the whole component, gets a tool-sized grid.
    d, expr = _overlap_probe_design()
    lo, hi = expr_aabb(d, expr)
    tool = expr.parts[1]  # the second intersect() part is the tool leaf
    assert isinstance(tool, Leaf)
    tlo, thi = d.instances[tool.iid].placed.aabb()
    assert np.all(np.asarray(lo) >= np.asarray(tlo))
    assert np.all(np.asarray(hi) <= np.asarray(thi))


def test_auto_grid_shrinks_for_small_probe_aabb() -> None:
    d, expr = _overlap_probe_design()
    res = volume(d, expr=expr)
    # The probe AABB is the r3 tool's ~6-wide box — far under the old
    # fixed grid — so the auto grid lands well below the ceiling.
    assert res.grid is not None and 12 <= res.grid < 48
    assert res.samples == res.grid * res.grid


def _sphere_overlap_expr(gap_overlap: float) -> tuple[Design, Inter]:
    """Two r=10 spheres overlapping along x by ``gap_overlap`` mm — a thin
    lens probe (``Inter`` meet ≈ overlap × 20 × 20)."""
    d = Design()
    a = d.prim("a", build_config("sphere:r10"))
    b = d.prim("b", build_config("sphere:r10"), translation(20 - gap_overlap, 0, 0))
    return d, d.intersect(a, b)


def test_auto_grid_retries_thin_overlap_at_max() -> None:
    # A 0.02 mm lens falls between the auto grid's ray columns and found
    # volume 0 where the old fixed grid found ~5e-3 mm³; an auto-picked
    # grid under 1 % of the meet-AABB volume retries once at the ceiling.
    d, expr = _sphere_overlap_expr(0.02)
    res = volume(d, expr=expr)
    assert res.volume > 0.0
    assert res.grid == 48


def test_auto_grid_stays_cheap_when_overlap_is_resolved() -> None:
    # A 0.5 mm lens is well over 1 % of its meet AABB, so the cheap
    # auto grid's answer stands — no retry.
    d, expr = _sphere_overlap_expr(0.5)
    res = volume(d, expr=expr)
    assert res.volume > 0.0
    assert res.grid is not None and res.grid < 48


def test_explicit_grid_is_always_honored() -> None:
    d, expr = _overlap_probe_design()
    res = volume(d, expr=expr, grid=8)
    assert res.grid == 8
    assert res.samples == 64


def test_samples_budget_still_maps_through_grid_for() -> None:
    d = Design()
    d.add_component("b", d.prim("box", build_config("box:w40d20h10")))
    res = volume(d, samples=200_000)
    assert res.grid == 99  # round(sqrt(200000)/4.5), the legacy mapping


# ---------------------------------------------------------------------------
# volume_field — the bounded sign-grid counterpart used on field-backend
# designs, where the ray quadrature is unbounded-cost (gr529278)
# ---------------------------------------------------------------------------


def test_volume_field_of_box_matches_analytic() -> None:
    d = Design()
    d.add_component("b", d.prim("box", build_config("box:w40d20h10")))
    res = volume_field(d)
    assert math.isclose(res.volume, 40 * 20 * 10, rel_tol=0.02)
    assert math.isclose(res.centroid[2], 5.0, abs_tol=0.2)


def test_volume_field_of_rounded_box_matches_steiner() -> None:
    # A Rounded leaf's ray_hits is a per-ray SDF search, so the ray
    # quadrature cannot price it in bounded time — the sign grid answers
    # the same volume from one vectorized expr_sdf_np batch instead.
    # ``rd`` shrinks the inner box by r on every side, so the solid is
    # inner ⊕ ball(r): V = abc + 2r(ab+ac+bc) + πr²(a+b+c) + (4/3)πr³.
    d = Design()
    d.add_component("r", d.prim("rbox", build_config("box:w40d20h10rd2")))
    res = volume_field(d)
    a, b, c, r = 36.0, 16.0, 6.0, 2.0
    steiner = (
        a * b * c
        + 2 * r * (a * b + a * c + b * c)
        + math.pi * r * r * (a + b + c)
        + 4.0 / 3.0 * math.pi * r**3
    )
    assert math.isclose(res.volume, steiner, rel_tol=0.05)


def test_volume_field_box_restricts_the_sampling_region() -> None:
    # The cut probe samples an intersect over the *cutter's* box — the
    # region that bounds what the intersect can hold — not the whole
    # part's additive AABB.
    d = Design()
    plate = d.prim("plate", build_config("box:w40d20h10rd2"))
    bore = d.prim("bore", build_config("cyl:r3h12"), translation(0, 0, -1))
    d.add_component("part", plate)
    hole = d.intersect(d.components["part"], bore)
    bore_lo, bore_hi = expr_aabb(d, bore)
    res = volume_field(d, expr=hole, box=(bore_lo, bore_hi))
    assert math.isclose(res.volume, math.pi * 9 * 10, rel_tol=0.1)
