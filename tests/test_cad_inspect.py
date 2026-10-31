"""Inspection probes — ray / section / overlap (docs/backlog/cad-inspect.md).

The headline cases: a ray through a plate reports exact entry/exit with
surface normals; a plane section of a plate-with-bore reports two loops
(outer exact, bore within discretization), one hole, and a 15 mm wall pair;
an rd-rounded box sections through the sampled-field leaf with the exact
offset-math area; and a probe cylinder overlapping the part reports a
quadrature volume against the closed-form value.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from typing import Any

import numpy as np
import pytest

import precis.cad.inspect as ci
from precis.cad.dsl import build_config
from precis.cad.fold import Expr
from precis.cad.graph import Design
from precis.cad.inspect import (
    ThicknessSample,
    _aabb_corners,
    _column_thickness,
    _vec3f,
    cad_field,
    overlap,
    probe_solid,
    ray,
    ray_field,
    section,
    thickness,
    thickness_field,
)
from precis.cad.primitives import Field as FieldPrim
from precis.cad.scene import build_design, parse_source
from precis.cad.vec import as_vec3, translation
from tests._timing import budget


def plate_design() -> tuple[Design, Expr]:
    d = Design()
    plate = d.prim("plate", build_config("box:w20d20h10"))
    return d, plate


def test_ray_through_plate_reports_entry_exit_and_normals() -> None:
    d, plate = plate_design()
    r = ray(d, plate, origin_m=(0, 0, -5), axis_m=(0, 0, 1))
    assert len(r.hits) == 1
    h = r.hits[0]
    assert h.t_entry_m == pytest.approx(5.0, abs=1e-6)
    assert h.t_exit_m == pytest.approx(15.0, abs=1e-6)
    assert h.entry_m == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)
    assert h.exit_m == pytest.approx((0.0, 0.0, 10.0), abs=1e-6)
    assert h.entry_normal_m == pytest.approx((0.0, 0.0, -1.0), abs=1e-6)
    assert h.exit_normal_m == pytest.approx((0.0, 0.0, 1.0), abs=1e-6)
    assert r.min_distance_m == pytest.approx(0.0, abs=1e-9)


def test_ray_miss_reports_closest_approach() -> None:
    d, plate = plate_design()
    r = ray(d, plate, origin_m=(15, 0, -5), axis_m=(0, 0, 1))
    assert r.hits == ()
    # line x=15 stays 5 from the plate's x=10 face
    assert r.min_distance_m == pytest.approx(5.0, abs=0.05)


def test_ray_profile_samples_sdf_along_the_axis() -> None:
    d, plate = plate_design()
    r = ray(d, plate, origin_m=(0, 0, -5), axis_m=(0, 0, 1), profile_samples=5)
    assert r.profile is not None
    ts = [t for t, _ in r.profile]
    assert ts[0] == pytest.approx(0.0) and ts[-1] == pytest.approx(
        r.hits[0].t_exit_m + 25.0, abs=1.0
    )
    sdf = [v for _, v in r.profile]
    assert sdf[0] == pytest.approx(5.0, abs=1e-6)
    assert sdf[1] == pytest.approx(-5.0, abs=1e-6)


def tube_design() -> tuple[Design, Expr]:
    d = Design()
    pl = d.prim("plate", build_config("box:w40d40h10"))
    bore = d.prim("bore", build_config("cyl:r5h12"), translation(0, 0, -1))
    return d, d.subtract(pl, bore)


def test_section_tube_reports_loops_hole_and_wall_pair() -> None:
    d, hub = tube_design()
    s = section(d, hub, origin_m=(0, 0, 5), normal_m=(0, 0, 1), pitch_m=0.02)
    assert len(s.loops) == 2
    assert s.hole_count == 1
    areas = sorted(l.area_m2 for l in s.loops)
    assert areas[0] == pytest.approx(-math.pi * 25.0, abs=0.05)
    assert areas[1] == pytest.approx(1600.0, abs=1.0)
    assert s.loops[0].length_m == pytest.approx(160.0, abs=0.5)
    ((i, j, dist),) = s.wall_pairs_m
    assert {i, j} == {0, 1}
    assert dist == pytest.approx(15.0, abs=0.05)


def test_section_rd_box_is_exact_through_the_field_leaf() -> None:
    d = Design()
    ext = d.prim("ext", build_config("box:w50d100h65rd3"))
    s = section(d, ext, origin_m=(0, 0, 33), normal_m=(0, 0, 1), pitch_m=0.1)
    assert len(s.loops) == 1
    assert s.hole_count == 0
    # 50x100 with four r3 corner arcs: 5000 - (4 - pi) * 9
    assert s.loops[0].area_m2 == pytest.approx(5000.0 - (4.0 - math.pi) * 9.0, abs=1.0)


def test_section_plane_clear_of_the_solid_is_empty() -> None:
    d, hub = tube_design()
    s = section(d, hub, origin_m=(0, 0, 50), normal_m=(0, 0, 1), pitch_m=0.1)
    assert s.loops == ()
    assert s.hole_count == 0
    assert s.wall_pairs_m == ()


def test_section_rejects_an_absurd_grid() -> None:
    d, hub = tube_design()
    with pytest.raises(ValueError, match="coarser pitch"):
        section(d, hub, origin_m=(0, 0, 5), normal_m=(0, 0, 1), pitch_m=1e-9)


def test_overlap_probe_inside_and_outside() -> None:
    d, plate = plate_design()
    probe_in = probe_solid(d, "probe_in", "cyl:r2h6", translation(0, 0, 2))
    o = overlap(d, plate, probe_in, grid=64)
    assert o.hit
    assert o.volume_m3 == pytest.approx(math.pi * 4.0 * 6.0, rel=0.02)
    probe_air = probe_solid(d, "probe_air", "cyl:r2h6", translation(30, 0, 2))
    o = overlap(d, plate, probe_air, grid=64)
    assert not o.hit
    assert o.volume_m3 == pytest.approx(0.0, abs=1e-9)


def test_overlap_honours_units_boundary_configs() -> None:
    d = Design()
    plate = d.prim("plate", build_config("box:w40mmd40mmh10mm", require_units=True))
    probe = probe_solid(d, "probe", "cyl:r1.775mmh13.4mm", translation(0, 0, 0.002))
    o = overlap(d, plate, probe, grid=64)
    assert o.hit
    # the probe pokes out of the 10 mm plate's top: the intersection
    # height is 0.01 - 0.002, not the probe's own 13.4 mm
    expect = math.pi * 0.001775**2 * 0.008
    assert o.volume_m3 == pytest.approx(expect, rel=0.03)


def test_overlap_annulus_is_the_stamp_verification_probe() -> None:
    """The verify_holes.py pattern: a probe slightly larger than a stamped
    hole meets exactly the annulus between hole wall and probe skin."""
    spec = parse_source(
        "component tube\n"
        "wall add box:w40mmd40mmh10mm @0mm,0mm,0mm\n"
        "bore cut cyl:r5mmh12mm @0mm,0mm,-1mm\n"
    )
    d = build_design(spec)
    expr = d.components["tube"]
    probe = probe_solid(d, "probe", "cyl:r5.3mmh10mm", translation(0, 0, 0.0))
    o = overlap(d, expr, probe, grid=128)
    annulus = math.pi * (5.3**2 - 5.0**2) * 10.0
    assert o.hit
    assert o.volume_m3 * 1e9 == pytest.approx(annulus, rel=0.02)


def test_ray_attributes_bore_wall_to_the_cutter() -> None:
    """A - B's new surface is attributed to B (blocker — the boolean-
    created, indirect face), never to the subtract operator; the outer
    faces are A's own (owner — direct)."""
    d, hub = tube_design()
    r = ray(d, hub, origin_m=(7.5, 0, 5), axis_m=(-1, 0, 0))
    assert len(r.hits) == 2
    first, second = r.hits
    assert first.entry_features == (("plate", "owner"),) or all(
        fr.label == "plate" and fr.role == "owner" for fr in first.entry_features
    )
    # exiting material into the bore: the wall is the bore leaf's surface
    assert all(
        fr.label == "bore" and fr.role == "blocker" for fr in first.exit_features
    )
    assert first.exit_features
    # re-entering material out of the bore
    assert all(
        fr.label == "bore" and fr.role == "blocker" for fr in second.entry_features
    )
    assert second.entry_features


def test_ray_filter_by_feature_selects_hits_touching_that_leaf() -> None:
    """``feature=`` keeps only hits whose entry/exit attribution names the
    leaf; the report echoes the filter, and whole-solid fields
    (``min_distance_m``, ``profile``) stay unfiltered."""
    d, hub = tube_design()
    # from inside the bore void outward: one span — bore-blocker in,
    # plate-owner out
    r = ray(d, hub, origin_m=(0, 0, 5), axis_m=(1, 0, 0), feature="bore")
    assert r.feature == "bore" and r.role is None
    assert len(r.hits) == 1
    assert all(fr.label == "bore" for fr in r.hits[0].entry_features)
    # retained hits keep their FULL tuples — the filter selects, it
    # does not prune
    assert any(fr.label == "plate" for fr in r.hits[0].exit_features)
    miss = ray(d, hub, origin_m=(25, 0, -5), axis_m=(0, 0, 1), feature="bore")
    assert miss.hits == ()
    # x=25 stays 5 from the plate's x=20 face; the closest approach is a
    # whole-solid property — unaffected by the filter
    assert miss.min_distance_m == pytest.approx(5.0, abs=0.05)


def test_ray_filter_by_role_selects_direct_or_indirect_encounters() -> None:
    """``role='owner'`` keeps direct faces only, ``'blocker'`` boolean-
    created ones only; with ``feature=`` the criteria AND."""
    d, hub = tube_design()
    blocker = ray(d, hub, origin_m=(0, 0, 5), axis_m=(1, 0, 0), role="blocker")
    assert len(blocker.hits) == 1  # entry through the bore wall
    owner = ray(d, hub, origin_m=(0, 0, 5), axis_m=(1, 0, 0), role="owner")
    assert len(owner.hits) == 1  # exit through the plate face
    # the bore never owns a face here: owner ∧ bore selects nothing
    both = ray(
        d, hub, origin_m=(0, 0, 5), axis_m=(1, 0, 0), feature="bore", role="owner"
    )
    assert both.hits == ()


def test_section_loops_carry_features() -> None:
    d, hub = tube_design()
    s = section(d, hub, origin_m=(0, 0, 5), normal_m=(0, 0, 1), pitch_m=0.02)
    by_sign = {l.area_m2 > 0: l for l in s.loops}
    outer, inner = by_sign[True], by_sign[False]
    assert all(fr.role == "owner" for fr in outer.features)
    assert any(fr.label == "plate" for fr in outer.features)
    assert all(fr.role == "blocker" for fr in inner.features)
    assert any(fr.label == "bore" for fr in inner.features)


def test_overlap_reports_boundary_features() -> None:
    d, hub = tube_design()
    probe = probe_solid(d, "probe", "cyl:r2h4", translation(7.5, 0, 3.0))
    o = overlap(d, hub, probe, grid=64)
    assert o.hit
    assert o.features
    assert all(fr.label in ("plate", "bore") for fr in o.features)


def boss_plate_design() -> tuple[Design, Expr]:
    d = Design()
    plate = d.prim("plate", build_config("box:w40d40h10"))
    boss = d.prim("boss", build_config("cyl:r8h30"), translation(0, 0, 10))
    return d, d.merge(plate, boss)


def _nearest_xy(samples, x: float, y: float):
    return min(
        samples,
        key=lambda s: (s.position_m[0] - x) ** 2 + (s.position_m[1] - y) ** 2,
    )


def test_thickness_map_measures_columns_behind_a_face() -> None:
    d, part = boss_plate_design()
    t = thickness(
        d, part, origin_m=(0, 0, 0), normal_m=(0, 0, -1), pitch_m=2.0, nominal_m=10.0
    )
    assert t.samples
    # off the boss: the 10 mm plate, skin to skin
    corner = _nearest_xy(t.samples, 19, 19)
    assert corner.thickness_m == pytest.approx(10.0, abs=1e-6)
    # under the boss: ONE continuous column — floor fused to boss
    core = _nearest_xy(t.samples, 0, 0)
    assert core.thickness_m == pytest.approx(40.0, abs=1e-6)
    assert any(fr.label == "plate" for fr in core.entry_features)
    assert any(fr.label == "boss" for fr in core.exit_features)


def test_thickness_sink_risks_flag_columns_beyond_ratio_of_nominal() -> None:
    d, part = boss_plate_design()
    t = thickness(
        d, part, origin_m=(0, 0, 0), normal_m=(0, 0, -1), pitch_m=2.0, nominal_m=10.0
    )
    risks = t.sink_risks()
    assert risks
    assert all(s.thickness_m > 1.5 * 10.0 for s in risks)
    # every risk sits over the Ø16 boss footprint (± one pitch)
    assert all(
        math.hypot(s.position_m[0], s.position_m[1]) <= 8.0 + t.pitch_m for s in risks
    )
    # a flat plate maps no risks
    d2, plate = plate_design()
    flat = thickness(
        d2, plate, origin_m=(0, 0, 10), normal_m=(0, 0, 1), pitch_m=2.0, nominal_m=10.0
    )
    assert flat.samples
    assert all(s.thickness_m == pytest.approx(10.0, abs=1e-6) for s in flat.samples)
    assert flat.sink_risks() == ()
    with pytest.raises(ValueError, match="nominal_m"):
        thickness(
            d2, plate, origin_m=(0, 0, 10), normal_m=(0, 0, 1), pitch_m=2.0
        ).sink_risks()


def test_thickness_feature_filter_maps_only_matching_columns() -> None:
    d, part = boss_plate_design()
    t = thickness(
        d,
        part,
        origin_m=(0, 0, 0),
        normal_m=(0, 0, -1),
        pitch_m=2.0,
        nominal_m=10.0,
        feature="boss",
    )
    assert t.samples
    # only columns whose run involves the boss leaf are sampled at all
    assert all(
        any(fr.label == "boss" for fr in s.entry_features + s.exit_features)
        for s in t.samples
    )


def test_ray_keeps_one_run_across_a_coincident_merge_interface() -> None:
    """Merged prims touching face-to-face have sdf ±0.0 on the shared
    face; a run crossing it must not split — the interface is interior
    material, not a boundary."""
    d, part = boss_plate_design()
    r = ray(d, part, origin_m=(0, 0, -2), axis_m=(0, 0, 1))
    assert len(r.hits) == 1
    assert r.hits[0].t_exit_m - r.hits[0].t_entry_m == pytest.approx(40.0, abs=1e-6)
    assert any(fr.label == "boss" for fr in r.hits[0].exit_features)


def test_thickness_ball_method_measures_the_inscribed_ball() -> None:
    """Rolling-ball thickness is the medial measure: the flat 10 mm plate
    reads 10 (ball r5), and the Ø16 boss reads 2 × 8 = 16 — pinned by the
    boss wall, not the 40 mm column the ray measures."""
    d, part = boss_plate_design()
    ball = thickness(
        d,
        part,
        origin_m=(0, 0, 0),
        normal_m=(0, 0, -1),
        pitch_m=2.0,
        nominal_m=10.0,
        method="ball",
    )
    # a mid-wall sample of the flat 10 mm plate: ball r5 (z is the nearest
    # face; sampling near an edge would be pinned by the side wall — that
    # is the rolling-ball measure working as intended)
    mid = _nearest_xy(ball.samples, 9, 9)
    assert mid.thickness_m == pytest.approx(10.0, abs=0.2)
    core = _nearest_xy(ball.samples, 0, 0)
    # nearest grid node to the axis is (±1, ±1) — the inscribed ball there
    # is pinned by the Ø16 boss wall at 8 − √2, not at the axis radius
    assert core.thickness_m == pytest.approx(2.0 * (8.0 - math.sqrt(2.0)), abs=0.2)
    # same map, ray method: the column, not the ball
    raymap = thickness(
        d,
        part,
        origin_m=(0, 0, 0),
        normal_m=(0, 0, -1),
        pitch_m=2.0,
        nominal_m=10.0,
    )
    assert _nearest_xy(raymap.samples, 0, 0).thickness_m == pytest.approx(
        40.0, abs=1e-6
    )
    with pytest.raises(ValueError, match="method"):
        thickness(
            d, part, origin_m=(0, 0, 0), normal_m=(0, 0, -1), pitch_m=2.0, method="x"
        )


def _serial_thickness_samples(
    d: Design,
    expr: Expr,
    *,
    origin_m: Any,
    normal_m: Any,
    pitch_m: float,
    method: str = "ray",
    feature: str | None = None,
    role: str | None = None,
) -> tuple[ThicknessSample, ...]:
    """The pre-gr531239 thickness() — one :func:`ray_field` per grid node
    over the grid ``thickness_field`` builds, plus its stall policy
    (nudged retry, then refuse). The batched march's equivalence oracle:
    identical node order, rays, runs, filters."""
    fld = cad_field(d, expr)
    n = np.asarray(as_vec3(normal_m), dtype=np.float64)
    n = n / np.linalg.norm(n)
    ref = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(n, ref)
    u = u / np.linalg.norm(u)
    v = np.cross(n, u)
    origin = np.asarray(as_vec3(origin_m), dtype=np.float64)
    lo, hi = fld.aabb
    rel = np.array(_aabb_corners(lo, hi)) - origin
    us, vs, ws = rel @ u, rel @ v, rel @ n
    u0 = float(us.min()) - pitch_m
    u1 = float(us.max()) + pitch_m
    v0 = float(vs.min()) - pitch_m
    v1 = float(vs.max()) + pitch_m
    span = float(ws.max() - ws.min()) + 2.0 * pitch_m
    nu = max(math.ceil((u1 - u0) / pitch_m) + 1, 2)
    nv = max(math.ceil((v1 - v0) / pitch_m) + 1, 2)
    out: list[ThicknessSample] = []
    for gvy in v0 + (np.arange(nv) + 0.5) * pitch_m:
        for gux in u0 + (np.arange(nu) + 0.5) * pitch_m:
            p = origin + gux * u + gvy * v
            r = ray_field(
                fld,
                origin_m=_vec3f(p + n * pitch_m),
                axis_m=_vec3f(-n),
                t_max_m=span + 2.0 * pitch_m,
                feature=feature,
                role=role,
            )
            if r.stalled:
                # the policy under test: one lateral-nudge retry on the
                # bigger budget, a second stall refuses the node
                r = ray_field(
                    fld,
                    origin_m=_vec3f(
                        p
                        + n * pitch_m
                        + pitch_m
                        * ci._STALL_NUDGE_REL
                        * (ci._STALL_NUDGE_UV[0] * u + ci._STALL_NUDGE_UV[1] * v)
                    ),
                    axis_m=_vec3f(-n),
                    t_max_m=span + 2.0 * pitch_m,
                    feature=feature,
                    role=role,
                    max_steps=ci._STALL_RETRY_MAX_STEPS,
                )
                if r.stalled:
                    continue
            if not r.hits:
                continue
            h = r.hits[0]
            out.append(
                ThicknessSample(
                    position_m=_vec3f(p),
                    thickness_m=_column_thickness(fld, h, method),
                    entry_features=h.entry_features,
                    exit_features=h.exit_features,
                )
            )
    return tuple(out)


def field_design() -> tuple[Design, Expr]:
    """A field-backed design: one sampled-SDF leaf (the trilinear
    ``distance_np`` evaluator gr531239's cost complaint targets) — a
    ball of radius 4 sampled on a 0.5-pitch grid."""
    pitch = 0.5
    origin = np.array([-6.0, -6.0, -5.0])
    pts = np.indices((24, 24, 24)).transpose(1, 2, 3, 0) * pitch + origin
    grid = (np.linalg.norm(pts, axis=-1) - 4.0).astype(np.float32)
    d = Design()
    body = d.prim("body", FieldPrim(grid=grid, pitch=pitch, origin=origin))
    return d, body


def test_thickness_batched_march_matches_the_per_node_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr531239: the batched grid march reports the same samples as one
    ray_field per node — positions, columns and attribution identical."""
    d, part = boss_plate_design()
    kw: dict[str, Any] = {
        "origin_m": (0, 0, 0),
        "normal_m": (0, 0, -1),
        "pitch_m": 2.0,
    }
    serial = _serial_thickness_samples(d, part, **kw)
    assert thickness(d, part, nominal_m=10.0, **kw).samples == serial
    # a tiny march chunk exercises the multi-chunk c0 offsets — every
    # comparison below runs chunked
    monkeypatch.setattr(ci, "_RAY_MARCH_CHUNK", 7)
    assert thickness(d, part, nominal_m=10.0, **kw).samples == serial
    # multi-run rays (the bore void splits the column into two runs) and
    # the feature filter's first-matching-run pick
    d2, hub = tube_design()
    kw2: dict[str, Any] = {
        "origin_m": (24, 0, 0),
        "normal_m": (1, 0, 0),
        "pitch_m": 2.0,
    }
    assert thickness(d2, hub, **kw2).samples == _serial_thickness_samples(
        d2, hub, **kw2
    )
    assert thickness(
        d2, hub, feature="bore", **kw2
    ).samples == _serial_thickness_samples(d2, hub, feature="bore", **kw2)
    # a sampled-field leaf: the trilinear distance_np path
    d3, body = field_design()
    kw3: dict[str, Any] = {
        "origin_m": (0, 0, -10),
        "normal_m": (0, 0, -1),
        "pitch_m": 1.0,
    }
    assert thickness(d3, body, **kw3).samples == _serial_thickness_samples(
        d3, body, **kw3
    )


def test_thickness_evaluates_the_field_once_per_march_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr531239 timing-free check: every ``fld.sdf`` call lands in one of
    four buckets — the per-batch init row, one call per march round, one
    per bisection round inside a crossing episode (≤ 64 rounds each), or
    the single batched crossing-normals call. Never one call per node."""
    d, part = boss_plate_design()
    fld = cad_field(d, part)
    calls = {"total": 0, "bisect": 0, "normals": 0, "episodes": 0, "marches": 0}
    in_bisect = False
    in_normals = False

    def counting(pts: np.ndarray) -> np.ndarray:
        calls["total"] += 1
        if in_bisect:
            calls["bisect"] += 1
        if in_normals:
            calls["normals"] += 1
        return fld.sdf(pts)

    real_bisect, real_normals, real_march = (
        ci._bisect_np,
        ci._normals_np,
        ci._march_rays,
    )

    def wrapped_bisect(*a: Any, **kw: Any) -> np.ndarray:
        nonlocal in_bisect
        calls["episodes"] += 1
        in_bisect = True
        try:
            return real_bisect(*a, **kw)
        finally:
            in_bisect = False

    def wrapped_normals(*a: Any, **kw: Any) -> np.ndarray:
        nonlocal in_normals
        in_normals = True
        try:
            return real_normals(*a, **kw)
        finally:
            in_normals = False

    def wrapped_march(*a: Any, **kw: Any) -> Any:
        calls["marches"] += 1
        return real_march(*a, **kw)

    monkeypatch.setattr(ci, "_bisect_np", wrapped_bisect)
    monkeypatch.setattr(ci, "_normals_np", wrapped_normals)
    monkeypatch.setattr(ci, "_march_rays", wrapped_march)
    t = thickness_field(
        replace(fld, sdf=counting),
        origin_m=(0, 0, 0),
        normal_m=(0, 0, -1),
        pitch_m=2.0,
    )
    assert t.samples
    # each march's first call is its init row; the rest are march rounds
    march_rounds = (
        calls["total"] - calls["bisect"] - calls["normals"] - calls["marches"]
    )
    # at most one batched normals call per march batch
    assert calls["normals"] <= calls["marches"]
    assert calls["bisect"] <= 64 * calls["episodes"]
    # exact decomposition: init + march rounds + bisection rounds + normals
    assert calls["total"] == (
        calls["marches"] + march_rounds + calls["bisect"] + calls["normals"]
    )
    # measured on this fixture: 182 calls = 1 init + 154 march rounds
    # + 26 bisection rounds (11 episodes) + 1 normals — the per-node path
    # would march each of the 484 nodes separately
    assert calls["total"] <= (
        calls["marches"] + march_rounds + 64 * calls["episodes"] + calls["normals"]
    )


def test_ray_tangent_slide_gives_up_instead_of_crawling() -> None:
    """A ray sliding along a surface keeps |sdf| under the step floor;
    the march must give up (no run, zero closest approach) instead of
    crawling min_step to the iteration cap. Regression for gr530414:
    thickness-map nodes landing exactly on a wall plane made every
    tangent ray crawl ~100 k evaluations (~1 s each on field designs).
    """
    d = Design()
    ext = d.prim("ext", build_config("box:w50d100h65rd3"))
    start = time.perf_counter()
    r = ray(d, ext, origin_m=(0, 0, 65), axis_m=(1, 0, 0))
    elapsed = time.perf_counter() - start
    assert r.hits == ()
    assert r.min_distance_m == pytest.approx(0.0, abs=1e-6)
    assert elapsed < budget(5.0)
    assert r.stalled


@pytest.mark.timeout(60)  # pre-fix this hung ~90 s on 37 stalled rays; fixed <10 s
def test_thickness_node_on_a_wall_plane_is_nudged_not_hung() -> None:
    """A grid node exactly on a wall plane fires a ray that RIDES the wall:
    sdf ~ 0 at every march step, so every step is the floor and the march
    creeps span/min_step micro-evaluations per node — hours on field-backed
    leaves (gr530414). The bounded march stalls, the node is retried once
    from a laterally nudged origin, measures the wall and lands in
    nudged_m — never hung, never silent, and refused_m stays empty."""
    d = Design()
    wall = d.prim("wall", build_config("box:w50d98h10"))
    # d98 phases the pitch-4 grid so a row lands exactly on y = +49, the
    # outer wall plane (98 mm = 24.5 pitches), and a column on x = -25.
    t = thickness(
        d, wall, origin_m=(0, 0, 10), normal_m=(0, 0, 1), pitch_m=4.0, nominal_m=10.0
    )
    assert t.samples
    assert t.nudged_m
    assert t.refused_m == ()
    # every recovered node sits on a wall plane (the +y row or the -x
    # column) and still carries the node's finite wall measurement
    by_node = {s.position_m: s for s in t.samples}
    for p in t.nudged_m:
        assert min(abs(abs(p[0]) - 25.0), abs(abs(p[1]) - 49.0)) < 1e-9
        assert by_node[p].thickness_m == pytest.approx(10.0, abs=1e-3)
    # ordinary interior nodes still map the 10 mm column
    mid = _nearest_xy(t.samples, 0, 0)
    assert mid.thickness_m == pytest.approx(10.0, abs=1e-6)
    assert mid.position_m[2] == pytest.approx(10.0, abs=1e-9)


def test_thickness_unrecoverable_stall_is_refused_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A node whose retry stalls too is listed in ``refused_m`` with no
    sample — never silently dropped. A one-step retry budget forces it."""
    d = Design()
    wall = d.prim("wall", build_config("box:w50d98h10"))
    kw: dict[str, Any] = {
        "origin_m": (0, 0, 10),
        "normal_m": (0, 0, 1),
        "pitch_m": 4.0,
    }
    ok = thickness(d, wall, **kw)
    assert ok.nudged_m and ok.refused_m == ()
    monkeypatch.setattr(ci, "_STALL_RETRY_MAX_STEPS", 1)
    t = thickness(d, wall, **kw)
    assert t.refused_m
    assert t.nudged_m == ()
    assert not {s.position_m for s in t.samples} & set(t.refused_m)
    assert len(t.samples) + len(t.refused_m) == len(ok.samples)


def test_ray_riding_a_wall_plane_stalls_instead_of_creeping() -> None:
    """The bounded march: a ray whose path slides along a wall plane
    (sdf ~ 0 every step) bails with ``stalled=True`` after a handful of
    evaluations, not span/min_step of them."""
    d = Design()
    wall = d.prim("wall", build_config("box:w50d98h10"))
    r = ray(d, wall, origin_m=(0, 49, 14), axis_m=(0, 0, -1), t_max_m=26.0)
    assert r.stalled
    assert r.hits == ()
    # a healthy ray is not flagged
    d2, plate = plate_design()
    assert not ray(d2, plate, origin_m=(0, 0, -5), axis_m=(0, 0, 1)).stalled


def test_march_rays_stall_matches_scalar_ray_field() -> None:
    """The batched march reports ``stalled`` where ray_field does — a
    wall-plane rider, its healthy neighbours, and tiny step budgets that
    run out (a crossing opened on the last visit is still recorded)."""
    d = Design()
    wall = d.prim("wall", build_config("box:w50d98h10"))
    fld = cad_field(d, wall)
    lo, hi = fld.aabb
    diag = float(np.linalg.norm(np.asarray(hi) - np.asarray(lo)))
    t_max = 26.0
    eps = ci.LINEAR_REL_EPS * max(diag, t_max, 1e-3)
    dvec = np.array([0.0, 0.0, -1.0])
    origins = [(0.0, 49.0, 14.0), (0.0, 48.0, 14.0), (0.0, 0.0, 14.0)]
    for max_steps in (ci._MAX_RAY_STEPS, 2, 3):
        runs, stalled = ci._march_rays(
            fld, np.array(origins), dvec, t_max, eps, max_steps=max_steps
        )
        for i, o in enumerate(origins):
            s = ray_field(
                fld,
                origin_m=o,
                axis_m=(0.0, 0.0, -1.0),
                t_max_m=t_max,
                max_steps=max_steps,
            )
            assert bool(stalled[i]) == s.stalled, (max_steps, o)
            assert [(a, b) for a, b, _o in runs[i]] == [
                (h.t_entry_m, h.t_exit_m) for h in s.hits
            ], (max_steps, o)
    # the riding ray stalls at the default budget; the centre ray does not
    _, stalled = ci._march_rays(fld, np.array(origins), dvec, t_max, eps)
    assert stalled.tolist() == [True, False, False]


def test_thickness_grid_missing_the_solid_is_empty() -> None:
    """A grid whose rays all miss the solid returns an empty result, not
    an error, and reports no nudged or refused nodes."""
    d, box = plate_design()
    t = thickness(
        d, box, origin_m=(0, 0, 100), normal_m=(0, 0, 1), pitch_m=4.0, nominal_m=10.0
    )
    assert t.samples == ()
    assert t.refused_m == ()
    assert t.nudged_m == ()
