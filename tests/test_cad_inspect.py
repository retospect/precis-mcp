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

import pytest

from precis.cad.dsl import build_config
from precis.cad.fold import Expr
from precis.cad.graph import Design
from precis.cad.inspect import overlap, probe_solid, ray, section, thickness
from precis.cad.scene import build_design, parse_source
from precis.cad.vec import translation


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
