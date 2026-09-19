"""cad rounding at the SDF leaf + the field export backend — slice 1 of
``docs/backlog/cad-sdf-rounding-and-field-export.md``.

Pins the representation contract (leaf shrink + field offset, base kept
at ``z=0``, bounding box unchanged, thin features refused not clamped),
the ``blend:`` union option, the vectorised distance twins, and the
narrow-band marching-cubes export (watertight, outward, Steiner volume,
byte-identical analytic route for sharp designs).
"""

from __future__ import annotations

import math
import struct
import zipfile
import zlib
from pathlib import Path

import numpy as np
import pytest

from precis.cad import export as cad_export
from precis.cad.dsl import (
    ROUNDABLE,
    DslError,
    ShapeSpec,
    build,
    build_config,
    format_spec,
    parse,
)
from precis.cad.fieldmesh import MAX_BAND_CELLS, FieldMeshError, field_mesh
from precis.cad.fold import Union, smooth_min
from precis.cad.primitives import (
    CircularFrustum,
    HalfSpace,
    Placed,
    PolyFrustum,
    Primitive,
    Rounded,
    Sphere,
    Torus,
    box,
    pyramid,
    regular_frustum,
    regular_prism,
)
from precis.cad.printability import orient, process_findings
from precis.cad.relate import _bounds, clearance, component_sdf, component_sdf_np
from precis.cad.scene import (
    NodeSpec,
    SceneError,
    build_design,
    parse_source,
    spec_to_source,
)
from precis.cad.tessellate import _signed_volume
from precis.cad.vec import rotation, translation, vec3

_HAS_MANIFOLD = cad_export.manifold_available()

_RBOX = "component p\nbody add box:w40mmd20mmh10mmrd2mm\n"
_SBOX = "component p\nbody add box:w40mmd20mmh10mm\n"
_RBOX_BORE = (
    "component p\n"
    "body add box:w40mmd20mmh10mmrd2mm\n"
    "bore cut cyl:r3mmh20mm @5mm,0mm,-5mm\n"
)
_BLEND = (
    "component p\n"
    "base add box:w40mmd20mmh10mmrd2mm\n"
    "rib add box:w6mmd6mmh20mmrd1mm @0mm,0mm,5mm blend:3mm\n"
)


def _steiner_box(w: float, d: float, h: float, r: float) -> float:
    """Volume of a ``w×d×h`` box with every edge rounded to ``r`` — the
    Minkowski sum of the shrunk box ``w'×d'×h'`` (``w' = w − 2r`` …) with a
    ball of radius ``r``: ``V = w'd'h' + 2r(w'd' + w'h' + d'h') +
    πr²(w' + d' + h') + 4/3·πr³``."""
    w2, d2, h2 = w - 2 * r, d - 2 * r, h - 2 * r
    return (
        w2 * d2 * h2
        + 2 * r * (w2 * d2 + w2 * h2 + d2 * h2)
        + math.pi * r * r * (w2 + d2 + h2)
        + 4.0 / 3.0 * math.pi * r**3
    )


def _edges_closed(tris: np.ndarray) -> bool:
    """Every undirected edge in exactly two triangles and every directed
    edge exactly once (closed + consistently oriented)."""
    directed = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    _u, counts = np.unique(np.sort(directed, axis=1), axis=0, return_counts=True)
    _d, dcounts = np.unique(directed, axis=0, return_counts=True)
    return bool(np.all(counts == 2) and np.all(dcounts == 1))


# ---------------------------------------------------------------------------
# DSL: the rd key
# ---------------------------------------------------------------------------


def test_rd_parses_after_r_and_d_keys_and_round_trips() -> None:
    spec = parse("box:w40mmd20mmh10mmrd2mm", require_units=True)
    assert spec.params == {"w": 0.04, "d": 0.02, "h": 0.01, "rd": 0.002}
    canon = format_spec(spec)
    assert canon == "box:w0.04d0.02h0.01rd0.002"
    assert parse(canon).params == spec.params
    assert parse(format_spec(spec, units=True), require_units=True) == spec
    # rd beats r: a cylinder keeps its own r and picks up rd
    cyl = parse("cyl:r5mmh10mmrd1mm", require_units=True)
    assert cyl.params == {"r": 0.005, "h": 0.01, "rd": 0.001}


@pytest.mark.parametrize(
    "config, dim",
    [
        ("box:w40mmd20mmh10mmrd5mm", "h"),  # rd == h/2 — equality refused too
        ("box:w40mmd6mmh10mmrd3.5mm", "d"),
        ("cyl:r3mmh20mmrd3mm", "r"),
        ("cyl:r5mmh10mmrd5mm", "h"),
        ("tcone:rb4mmrt1mmh8mmrd1mm", "rt"),
        ("hex:r5mmh20mmrd4.4mm", "across-flats"),
        ("frustum:n6rb4mmrt2mmh5mmrd2mm", "rt"),
    ],
)
def test_rd_at_or_over_half_min_dimension_refused_naming_it(
    config: str, dim: str
) -> None:
    with pytest.raises(DslError, match=r"½·min dimension") as ei:
        parse(config, require_units=True)
    msg = str(ei.value)
    assert dim in msg
    assert config.split(":")[0] in msg  # names the primitive


def test_rd_that_closes_a_slanted_shape_is_refused_not_clamped() -> None:
    # cone r=8, h=4: rd=1.9 passes the ½ rule (< r, < h/2) but the offset
    # lateral line meets the base above z = rd — the shrunk cone is empty.
    with pytest.raises(DslError, match="slant"):
        parse("cone:r8mmh4mmrd1.9mm", require_units=True)
    with pytest.raises(DslError, match="slant"):
        parse("pyramid:n4r8mmh4mmrd1.9mm", require_units=True)
    # a tcone/frustum that passes the ½ rule keeps both caps at any slant
    # (rd < min(rb, rt, h/2) ⇒ rb', rt' > 0) — steep or shallow, accepted
    for cfg in (
        "tcone:rb4mmrt2mmh8mmrd1.5mm",
        "tcone:rb10mmrt2mmh4mmrd1.5mm",
        "tcone:rb1mmrt10mmh3mmrd0.9mm",
        "frustum:n5rb1mmrt10mmh3mmrd0.7mm",
    ):
        prim = build_config(cfg, require_units=True)
        assert isinstance(prim, Rounded)
        assert isinstance(prim.inner, CircularFrustum | PolyFrustum)


@pytest.mark.parametrize(
    "config, reason",
    [
        ("sphere:r5mmrd1mm", "already round"),
        ("torus:R10mmr2mmrd1mm", "already round"),
    ],
)
def test_rd_refused_on_already_round_shapes(config: str, reason: str) -> None:
    with pytest.raises(DslError, match=reason):
        parse(config, require_units=True)


def test_rd_must_be_positive() -> None:
    with pytest.raises(DslError, match="rd must be > 0"):
        parse("box:w40mmd20mmh10mmrd0mm", require_units=True)


def test_chamfer_grammar_cannot_carry_rd() -> None:
    with pytest.raises(DslError):
        parse("chamfer:1mmx45degrd1mm", require_units=True)


def test_every_roundable_alias_builds_a_rounded_primitive() -> None:
    configs = {
        "box": "box:w40mmd20mmh10mmrd2mm",
        "cyl": "cyl:r5mmh10mmrd1mm",
        "cone": "cone:r4mmh8mmrd1mm",
        "tcone": "tcone:rb4mmrt2mmh8mmrd0.5mm",
        "hex": "hex:r5mmh10mmrd1mm",
        "ngon": "ngon:n8r5mmh10mmrd1mm",
        "frustum": "frustum:n6rb4mmrt2mmh5mmrd0.5mm",
        "pyramid": "pyramid:n4r5mmh8mmrd1mm",
    }
    assert set(configs) == set(ROUNDABLE)
    for alias, cfg in configs.items():
        prim = build_config(cfg, require_units=True)
        assert isinstance(prim, Rounded), alias
        assert prim.lift == prim.r > 0.0
        lo, _hi = prim.aabb_local()
        assert lo[2] == pytest.approx(0.0, abs=1e-15), alias  # base at z=0


# ---------------------------------------------------------------------------
# kernel: the rounded evaluation
# ---------------------------------------------------------------------------


def test_rounded_box_bounding_dims_equal_sharp_box_and_base_at_zero() -> None:
    sharp = build_config("box:w40mmd20mmh10mm", require_units=True)
    rounded = build_config("box:w40mmd20mmh10mmrd2mm", require_units=True)
    slo, shi = sharp.aabb_local()
    rlo, rhi = rounded.aabb_local()
    assert np.allclose(rlo, slo, atol=1e-9) and np.allclose(rhi, shi, atol=1e-9)
    assert rlo[2] == pytest.approx(0.0, abs=1e-12)
    # face centres are ON the surface (planar faces unchanged) …
    assert rounded.distance_local(vec3(0.0, 0.0, 0.0)) == pytest.approx(0.0, abs=1e-12)
    assert rounded.distance_local(vec3(0.02, 0.0, 0.005)) == pytest.approx(
        0.0, abs=1e-12
    )
    # … the sharp corner is outside by (√3 − 1)·r, the edge by (√2 − 1)·r
    r = 0.002
    assert rounded.distance_local(vec3(0.02, 0.01, 0.01)) == pytest.approx(
        (math.sqrt(3) - 1) * r, abs=1e-12
    )
    assert rounded.distance_local(vec3(0.02, 0.01, 0.005)) == pytest.approx(
        (math.sqrt(2) - 1) * r, abs=1e-12
    )
    # membership follows the field
    assert not rounded.contains_local(vec3(0.02, 0.01, 0.01))
    assert rounded.contains_local(vec3(0.0, 0.0, 0.005))
    # planar faces are still enumerated (draft analysis) with sharp normals
    tags = {f.tag for f in rounded.faces_local()}
    assert {"bottom", "top"} <= tags


def test_shrink_is_in_the_parameters_not_the_field() -> None:
    # The contract's "+r then −r cancels" trap: a box SDF offset by −r is
    # NOT the shrunk-then-grown box. Pin that the rounded corner distance
    # differs from the naive offset of the sharp box.
    sharp = build_config("box:w40mmd20mmh10mm", require_units=True)
    rounded = build_config("box:w40mmd20mmh10mmrd2mm", require_units=True)
    corner_out = vec3(0.021, 0.011, 0.011)
    naive = sharp.distance_local(corner_out)  # already exact for the sharp box
    assert rounded.distance_local(corner_out) > naive + 1e-4


def test_rounded_cone_apex_is_a_sphere_cap_below_the_sharp_apex() -> None:
    prim = build_config("cone:r4mmh8mmrd1mm", require_units=True)
    assert isinstance(prim, Rounded)
    lo, hi = prim.aabb_local()
    assert lo[2] == pytest.approx(0.0)
    assert hi[2] < 0.008  # shorter than the sharp cone
    # the lateral wall is unchanged: a point on the sharp cone's wall,
    # mid-height, is on the rounded surface too
    z = 0.004
    rho = 0.004 * (1 - z / 0.008)
    assert prim.distance_local(vec3(rho, 0.0, z)) == pytest.approx(0.0, abs=1e-9)


def test_rounded_ray_hits_match_the_field() -> None:
    prim = build_config("box:w40mmd20mmh10mmrd2mm", require_units=True)
    # through the middle: the full extent
    (t0, t1), *rest = prim.ray_hits_local(vec3(-1.0, 0.0, 0.005), vec3(1.0, 0.0, 0.0))
    assert not rest
    assert t0 == pytest.approx(0.98, abs=1e-9) and t1 == pytest.approx(1.02, abs=1e-9)
    # vertical through the centre: base at z=0, top at z=h
    ((t0, t1),) = prim.ray_hits_local(vec3(0.0, 0.0, -1.0), vec3(0.0, 0.0, 1.0))
    assert t0 == pytest.approx(1.0, abs=1e-9) and t1 == pytest.approx(1.01, abs=1e-9)
    # through the sharp corner column: rounded away → miss
    assert prim.ray_hits_local(vec3(0.02, 0.01, -1.0), vec3(0.0, 0.0, 1.0)) == []
    # non-unit direction, same geometry
    ((t0, t1),) = prim.ray_hits_local(vec3(0.0, 0.0, -1.0), vec3(0.0, 0.0, 2.0))
    assert t0 == pytest.approx(0.5, abs=1e-9) and t1 == pytest.approx(0.505, abs=1e-9)


def test_rounded_in_a_design_probes_and_bounds_report_unshrunk_extents() -> None:
    design = build_design(parse_source(_RBOX))
    expr = design.components["p"]
    bounds = _bounds(design, expr)
    assert bounds is not None
    lo, hi = bounds
    assert np.allclose(lo, [-0.02, -0.01, 0.0]) and np.allclose(hi, [0.02, 0.01, 0.01])
    # point probes see the round: the sharp corner is void, the face centre solid
    assert not design.classify_point(vec3(0.0199, 0.0099, 0.0099)).inside
    assert design.classify_point(vec3(0.0, 0.0, 0.005)).inside
    # the component SDF at the sharp corner is the exact corner gap
    d = component_sdf(design, expr, vec3(0.02, 0.01, 0.01))
    assert d == pytest.approx((math.sqrt(3) - 1) * 0.002, abs=1e-12)


def test_relate_clearance_between_rounded_parts_uses_the_round() -> None:
    # Two rounded boxes whose sharp corners would touch at a point: the
    # rounds open a gap of 2·(√3 − 1)·r along the body diagonal … but the
    # closest approach is between the sphere caps, which is exactly
    # |corner gap| − 2r along the diagonal; pin it is > 0 (sharp: 0).
    src = (
        "component a\nx add box:w10mmd10mmh10mmrd2mm\n"
        "component b\ny add box:w10mmd10mmh10mmrd2mm @10mm,10mm,10mm\n"
    )
    design = build_design(parse_source(src))
    res = clearance(design, "a", "b")
    expected = math.sqrt(3) * 0.002 * 2 - 2 * 0.002  # cap-to-cap along the diagonal
    assert res.gap == pytest.approx(expected, rel=1e-3)


def test_polyfrustum_outside_distance_above_and_below_caps_is_perpendicular() -> None:
    # Regression for the cap-ring winding bug the rounding work exposed:
    # the bottom/top face rings were wound clockwise about their outward
    # normal, so a point straight above/below a box read the distance to
    # the cap's EDGE (here ≈ 8.06 mm), not the 1 mm to the cap itself.
    for prim, above in (
        (box(0.036, 0.016, 0.006), 0.006),
        (regular_prism(6, 0.01, 0.01), 0.01),
        (pyramid(4, 0.01, 0.01), None),  # apex: no top cap
        (regular_frustum(6, 0.004, 0.002, 0.005), 0.005),
    ):
        assert prim.distance_local(vec3(0.0, 0.0, -0.001)) == pytest.approx(0.001)
        if above is not None:
            assert prim.distance_local(vec3(0.0, 0.0, above + 0.001)) == pytest.approx(
                0.001
            )
        # side faces were already right and stay right
        assert prim.distance_local(vec3(0.0, 0.0, 0.001)) < 0.0


# ---------------------------------------------------------------------------
# vectorised distance == scalar distance
# ---------------------------------------------------------------------------


def _all_primitives() -> list[tuple[str, Primitive]]:
    prims: list[tuple[str, Primitive]] = [
        ("box", box(0.04, 0.02, 0.01)),
        ("cyl", CircularFrustum(0.005, 0.005, 0.01)),
        ("cone", CircularFrustum(0.004, 0.0, 0.008)),
        ("tcone", CircularFrustum(0.004, 0.002, 0.008)),
        ("hex", regular_prism(6, 0.005, 0.01)),
        ("frustum", regular_frustum(6, 0.004, 0.002, 0.005)),
        ("pyramid", pyramid(4, 0.005, 0.008)),
        ("sphere", Sphere(0.005)),
        ("torus", Torus(0.01, 0.002)),
        ("halfspace", HalfSpace(vec3(0.001, 0.0, 0.002), vec3(0.3, 0.1, 0.9))),
    ]
    for cfg in (
        "box:w40mmd20mmh10mmrd2mm",
        "cyl:r5mmh10mmrd1mm",
        "cone:r4mmh8mmrd1mm",
        "tcone:rb4mmrt2mmh8mmrd0.5mm",
        "hex:r5mmh10mmrd1mm",
        "ngon:n8r5mmh10mmrd1mm",
        "frustum:n6rb4mmrt2mmh5mmrd0.5mm",
        "pyramid:n4r5mmh8mmrd1mm",
    ):
        prims.append((cfg, build_config(cfg, require_units=True)))
    return prims


@pytest.mark.parametrize("label, prim", _all_primitives(), ids=lambda x: str(x)[:24])
def test_distance_local_np_matches_scalar(label: str, prim: Primitive) -> None:
    rng = np.random.default_rng(zlib.crc32(label.encode("utf-8")))
    pts = rng.uniform(-0.03, 0.03, size=(500, 3))
    # include points exactly on axes/planes where branch choices flip
    pts[:10, 0] = 0.0
    pts[10:20, 2] = 0.0
    pts[20:30, :2] = 0.0
    scalar = np.array([prim.distance_local(p) for p in pts])
    vec = prim.distance_local_np(pts)
    assert vec.shape == (500,)
    assert np.max(np.abs(vec - scalar)) <= 1e-12


def test_placed_distance_np_matches_scalar_under_a_pose() -> None:
    prim = build_config("box:w40mmd20mmh10mmrd2mm", require_units=True)
    xf = translation(0.01, -0.02, 0.005).compose(rotation(0.3, -0.7, 1.1))
    placed = Placed(prim=prim, xform=xf)
    pts = np.random.default_rng(7).uniform(-0.05, 0.05, size=(300, 3))
    scalar = np.array([placed.distance(p) for p in pts])
    assert np.max(np.abs(placed.distance_np(pts) - scalar)) <= 1e-12


def test_component_sdf_np_matches_scalar_fold() -> None:
    design = build_design(
        parse_source(_BLEND + "hole cut cyl:r2mmh40mm @8mm,0mm,-1mm\n")
    )
    expr = design.components["p"]
    pts = np.random.default_rng(3).uniform(-0.03, 0.03, size=(400, 3))
    scalar = np.array([component_sdf(design, expr, p) for p in pts])
    assert np.max(np.abs(component_sdf_np(design, expr, pts) - scalar)) <= 1e-12


# ---------------------------------------------------------------------------
# blend: the union-only smooth-min
# ---------------------------------------------------------------------------


def test_blend_parses_stores_and_round_trips() -> None:
    spec = parse_source(_BLEND)
    rib = spec.nodes[1]
    assert rib.blend == pytest.approx(0.003)
    assert spec.nodes[0].blend == 0.0
    assert "blend:0.003m" in spec_to_source(spec)
    assert parse_source(spec_to_source(spec)) == spec
    meta = rib.to_meta()
    assert meta["blend"] == pytest.approx(0.003)
    assert "blend" not in spec.nodes[0].to_meta()
    assert NodeSpec.from_meta("rib", meta) == rib


@pytest.mark.parametrize("op", ["cut", "intersect"])
def test_blend_refused_on_cut_and_intersect_naming_the_rule(op: str) -> None:
    src = f"component p\nbase add box:w40mmd20mmh10mm\nx {op} cyl:r3mmh20mm blend:2mm\n"
    with pytest.raises(SceneError, match="union-only"):
        parse_source(src)


def test_blend_refused_on_a_components_base_node() -> None:
    with pytest.raises(SceneError, match="nothing to blend against"):
        parse_source("component p\nbase add box:w40mmd20mmh10mm blend:2mm\n")


def test_blend_needs_a_unit_and_a_positive_value() -> None:
    from precis.utils.units import UnitRequiredError

    with pytest.raises(UnitRequiredError):
        parse_source(
            "component p\na add box:w4mmd4mmh4mm\nb add box:w4mmd4mmh4mm blend:2\n"
        )
    with pytest.raises(SceneError, match="must be > 0"):
        parse_source(
            "component p\na add box:w4mmd4mmh4mm\nb add box:w4mmd4mmh4mm blend:0mm\n"
        )


def test_smooth_min_only_adds_material_in_the_seam() -> None:
    assert smooth_min(1.0, 5.0, 2.0) == 1.0  # |a−b| ≥ k → plain min
    assert smooth_min(1.0, 1.0, 2.0) == pytest.approx(1.0 - 2.0 / 4)  # dips by k/4
    for a, b in ((0.3, 0.4), (-0.2, 0.1), (2.0, -1.0)):
        assert smooth_min(a, b, 1.0) <= min(a, b)


def test_blend_builds_a_blended_union_and_fills_the_seam() -> None:
    spec = parse_source(_BLEND)
    design = build_design(spec)
    expr = design.components["p"]
    assert isinstance(expr, Union) and expr.blend == pytest.approx(0.003)
    # the inside corner where the rib meets the base top: sharp union has
    # air just outside both bodies there; the blend has material.
    p = vec3(0.0034, 0.0, 0.0104)
    sharp = build_design(parse_source(_BLEND.replace(" blend:3mm", "")))
    assert component_sdf(sharp, sharp.components["p"], p) > 0.0
    assert component_sdf(design, expr, p) < 0.0
    # …and the point probe agrees with the field (attributed to a node)
    cls = design.classify_point(p)
    assert cls.inside and cls.owner is not None
    assert not sharp.classify_point(p).inside


# ---------------------------------------------------------------------------
# field export backend
# ---------------------------------------------------------------------------


def test_marching_cubes_tables_are_face_consistent_on_random_fields() -> None:
    # Every one of the 256 cases and every ambiguous-face pairing occurs
    # thousands of times in a 40³ noise field; a single inconsistent table
    # row shows up as an edge not shared by exactly two triangles.
    n = 40
    rng = np.random.default_rng(0)
    vals = rng.standard_normal((n + 1, n + 1, n + 1))
    vals[[0, -1], :, :] = 1.0
    vals[:, [0, -1], :] = 1.0
    vals[:, :, [0, -1]] = 1.0
    pitch = 1.0

    def sdf(pts: np.ndarray) -> np.ndarray:
        ijk = np.rint(pts / pitch).astype(int)
        ijk = np.clip(ijk, 0, n)
        # clipped to |d| <= 1 so the (non-Lipschitz) noise never falls
        # outside the coarse band test — every cell is a band cell and
        # the table, not the band logic, is what this exercises.
        return np.clip(vals[ijk[:, 0], ijk[:, 1], ijk[:, 2]], -1.0, 1.0)

    # pitch-aligned AABB so the grid vertices land on the noise samples
    lo = vec3(1.5 * pitch, 1.5 * pitch, 1.5 * pitch)
    hi = vec3((n - 1.5) * pitch, (n - 1.5) * pitch, (n - 1.5) * pitch)
    verts, tris = field_mesh(sdf, lo, hi, pitch, name="noise")
    assert len(tris) > 10_000
    assert _edges_closed(tris)


def test_field_mesh_sphere_volume_and_watertight() -> None:
    r = 1.0

    def sdf(pts: np.ndarray) -> np.ndarray:
        return np.linalg.norm(pts, axis=1) - r

    verts, tris = field_mesh(sdf, vec3(-r, -r, -r), vec3(r, r, r), 0.05)
    assert _edges_closed(tris)
    vol = _signed_volume(verts, tris) / 6.0
    assert vol > 0.0
    assert vol == pytest.approx(4.0 / 3.0 * math.pi * r**3, rel=2e-3)


def test_field_mesh_refuses_over_budget_and_empty_fields() -> None:
    def sdf(pts: np.ndarray) -> np.ndarray:
        return np.linalg.norm(pts, axis=1) - 1.0

    with pytest.raises(FieldMeshError, match="budget"):
        field_mesh(sdf, vec3(-1, -1, -1), vec3(1, 1, 1), 1e-4)
    with pytest.raises(FieldMeshError, match="no zero crossing"):
        field_mesh(sdf, vec3(5, 5, 5), vec3(6, 6, 6), 0.1)
    assert MAX_BAND_CELLS == 50_000_000


def test_needs_field_backend_only_for_rd_or_blend() -> None:
    assert not cad_export.needs_field_backend(parse_source(_SBOX))
    assert cad_export.needs_field_backend(parse_source(_RBOX))
    assert cad_export.needs_field_backend(
        parse_source(
            "component p\na add box:w4mmd4mmh4mm\nb add box:w4mmd4mmh4mm blend:1mm\n"
        )
    )


def _read_stl(path: Path) -> tuple[np.ndarray, np.ndarray]:
    raw = path.read_bytes()
    n = struct.unpack("<I", raw[80:84])[0]
    tri = np.frombuffer(raw[84:], dtype=np.uint8).reshape(n, 50)
    pts = np.frombuffer(tri[:, 12:48].tobytes(), dtype="<f4").reshape(3 * n, 3)
    return pts.astype(np.float64), np.arange(3 * n).reshape(n, 3)


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_rounded_box_export_volume_matches_steiner(tmp_path: Path) -> None:
    pitch = 0.25e-3
    spec = parse_source(_RBOX)
    verts, tris = cad_export._solid_mesh(spec, pitch=pitch)
    assert _edges_closed(tris)
    vol = _signed_volume(verts, tris) / 6.0
    exact = _steiner_box(0.04, 0.02, 0.01, 0.002)
    # Marching cubes with linear interpolation is second-order on a smooth
    # surface: the volume error scales like (pitch/r)² · V_round. At
    # pitch = r/8 that is ~1.5 %; the observed error is ~3.5e-4. Bound at
    # 2e-3 relative — pitch-dependent, loose by ~5×, tight against a
    # missing Steiner term (the smallest, 4/3·πr³, is 0.4 % of V).
    assert vol == pytest.approx(exact, rel=2e-3)
    # bounding box of the mesh == the sharp box's, base at z=0
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    assert np.allclose(lo, [-0.02, -0.01, 0.0], atol=1e-9)
    assert np.allclose(hi, [0.02, 0.01, 0.01], atol=1e-9)
    # and the file route (mm) agrees
    out = cad_export.export_mesh(spec, tmp_path / "rbox.stl", pitch=pitch)
    fv, ft = _read_stl(out)
    assert fv.min(axis=0) == pytest.approx([-20.0, -10.0, 0.0], abs=1e-3)
    assert fv.max(axis=0) == pytest.approx([20.0, 10.0, 10.0], abs=1e-3)


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_rounded_box_cut_by_sharp_cylinder_keeps_exact_bore() -> None:
    pitch = 0.2e-3
    verts, tris = cad_export._solid_mesh(parse_source(_RBOX_BORE), pitch=pitch)
    assert _edges_closed(tris)
    assert _signed_volume(verts, tris) > 0.0
    # bore radius measured on the mesh: vertices near mid-height, inside
    # the bore column, sit on r = 3 mm (the sharp cylinder wall is exact
    # in the field, so the mesh vertex lands on it to interpolation error)
    mid = verts[np.abs(verts[:, 2] - 0.005) < 2 * pitch]
    rad = np.hypot(mid[:, 0] - 0.005, mid[:, 1])
    on_bore = rad[rad < 0.004]
    assert len(on_bore) > 50
    assert np.max(np.abs(on_bore - 0.003)) < 1e-5
    # volume = rounded box − bore
    vol = _signed_volume(verts, tris) / 6.0
    exact = _steiner_box(0.04, 0.02, 0.01, 0.002) - math.pi * 0.003**2 * 0.01
    assert vol == pytest.approx(exact, rel=2e-3)


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_two_rounded_boxes_with_blend_export_watertight_and_outward(
    tmp_path: Path,
) -> None:
    pitch = 0.25e-3
    verts, tris = cad_export._solid_mesh(parse_source(_BLEND), pitch=pitch)
    assert _edges_closed(tris)
    vol = _signed_volume(verts, tris) / 6.0
    assert vol > 0.0
    # more than the hard union of the same two rounded boxes (the seam is
    # filled — the only thing blend changes), and not by much: the fill
    # is a ~k/4-deep fillet band around the rib's 24 mm perimeter.
    hard_v, hard_t = cad_export._solid_mesh(
        parse_source(_BLEND.replace(" blend:3mm", "")), pitch=pitch
    )
    hard = _signed_volume(hard_v, hard_t) / 6.0
    assert hard * 1.001 < vol < hard * 1.05
    # 3MF keeps one object per component
    out = cad_export.export_mesh(parse_source(_BLEND), tmp_path / "b.3mf", pitch=pitch)
    with zipfile.ZipFile(out) as zf:
        model = zf.read("3D/3dmodel.model").decode("utf-8")
    assert model.count("<object ") == 1 and 'name="p"' in model


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_near_tangent_spheres_blended_at_pitch_scale_export_watertight() -> None:
    """Stress the narrow-band assumption itself, not the AABB/rounding bug
    above: blend width == pitch is the case ``BAND_SAFETY`` is supposed to
    still catch on a coarse-grid pass. A ``FieldMeshError`` here is the
    signal a reviewer asked to watch for — report it, don't paper over
    it."""
    pitch = 0.05e-3
    blend = pitch
    spec = parse_source(
        "component p\n"
        "a add sphere:r5mm @0mm,0mm,0mm\n"
        f"b add sphere:r5mm @10mm,0mm,0mm blend:{blend * 1e3:g}mm\n"
    )
    verts, tris = cad_export._solid_mesh(spec, pitch=pitch)
    assert _edges_closed(tris)
    vol = _signed_volume(verts, tris) / 6.0
    hard = 2.0 * 4.0 / 3.0 * math.pi * 0.005**3
    assert vol == pytest.approx(hard, rel=1e-3)


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_sharp_design_exports_byte_identical_to_the_analytic_route(
    tmp_path: Path,
) -> None:
    src = (
        "component flange\n"
        "plate     add  cyl:r25mmh8mm\n"
        "hub_bore  cut  cyl:r8mmh10mm    @0mm,0mm,-1mm\n"
        "bolts     cut  cyl:r2.5mmh10mm  @18mm,0mm,-1mm  polar:n6r18mm\n"
        "component rim\n"
        "ring add box:w4mmd4mmh4mm @20mm,0mm,0mm rot:0deg,0deg,45deg\n"
    )
    spec = parse_source(src)
    assert not cad_export.needs_field_backend(spec)
    # the analytic route, called directly (pre-change code path)
    scaled = cad_export._scaled_for_export(spec)
    ref_stl = tmp_path / "ref.stl"
    cad_export._write_binary_stl(
        ref_stl, *cad_export._mesh_of(cad_export._design_solid(scaled))
    )
    ref_3mf = tmp_path / "ref.3mf"
    cad_export._write_3mf(
        ref_3mf,
        [(n, *cad_export._mesh_of(s)) for n, s in cad_export._component_solids(scaled)],
    )
    # the public route with and without a pitch
    out_stl = cad_export.export_mesh(spec, tmp_path / "out.stl")
    out_stl_p = cad_export.export_mesh(spec, tmp_path / "outp.stl", pitch=1e-4)
    assert out_stl.read_bytes() == ref_stl.read_bytes() == out_stl_p.read_bytes()
    out_3mf = cad_export.export_mesh(spec, tmp_path / "out.3mf")

    def model(p: Path) -> bytes:
        with zipfile.ZipFile(p) as zf:
            return zf.read("3D/3dmodel.model")

    assert model(out_3mf) == model(ref_3mf)


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_printability_on_a_rounded_box_reports_no_overhang_at_house_rules() -> None:
    # rd = 0.4 mm on a 0.2 mm layer: the part of the lower round steeper
    # than 45° lies below z = r·(1 − sin 45°) = 0.117 mm — inside the
    # first layer, i.e. bed contact — so the mesh (not the sharp AABB) is
    # what DRC judges, and it passes.
    spec = parse_source("component p\nbody add box:w40mmd20mmh10mmrd0.4mm\n")
    rules = {"max_overhang": 45.0, "layer_height": 0.0002}
    mesh = cad_export._solid_mesh(spec, pitch=0.1e-3)
    policy = {
        "weights": {
            "overhang": 1.0,
            "bed_contact": 1.0,
            "height": 1.0,
            "bridges": 1.0,
            "load_vs_layer": 1.0,
        },
        "sweep_deg": 30,
    }
    cands = orient(mesh, rules, policy, [])
    assert cands
    for down in (cands[0].down, vec3(0.0, 0.0, -1.0)):
        findings = process_findings(mesh, down, rules)
        assert [f.rule for f in findings if f.rule == "overhang"] == []
    # the same box with a big round DOES overhang — proves the check is live
    big = cad_export._solid_mesh(
        parse_source("component p\nbody add box:w40mmd20mmh10mmrd3mm\n"), pitch=0.2e-3
    )
    assert any(
        f.rule == "overhang" for f in process_findings(big, vec3(0.0, 0.0, -1.0), rules)
    )


def test_field_pitch_default_and_refusal_plumbing() -> None:
    design = build_design(parse_source(_RBOX))
    auto = cad_export._field_pitch(design, None)
    diag = math.sqrt(0.04**2 + 0.02**2 + 0.01**2)
    assert auto == pytest.approx(diag * cad_export.FIELD_PITCH_DIAG_FRACTION)
    with pytest.raises(cad_export.ExportError, match="positive"):
        cad_export._field_pitch(design, 0.0)
    with pytest.raises(cad_export.ExportError, match="budget"):
        cad_export._solid_mesh(parse_source(_RBOX), pitch=1e-6)


def test_field_pitch_ignores_an_oversized_cutter() -> None:
    # gr: an oversized subtractive tool inflated the auto-pitch AABB (via
    # _bounds walking Diff cutters too) and dropped the pitch from ~0.18 mm
    # to ~6.8 mm on this exact shape, making the rd unresolvable — silently.
    design = build_design(parse_source(_RBOX))
    bare = cad_export._field_pitch(design, None)
    bore = build_design(
        parse_source(_RBOX + "bore cut cyl:r500mmh1000mm @0mm,0mm,-1mm\n")
    )
    cut = cad_export._field_pitch(bore, None)
    assert cut == pytest.approx(bare, abs=1e-12)


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_rounded_box_cut_by_oversized_cylinder_still_resolves_the_round() -> None:
    # r=3mm matches _RBOX_BORE's real bore exactly — only h is oversized
    # (1 m vs the box's 10 mm), which used to dominate the auto-pitch AABB
    # via _bounds; the extra length past the box is otherwise inert, so
    # the removed volume is still the plain cylindrical bore.
    spec = parse_source(_RBOX + "bore cut cyl:r3mmh1m @5mm,0mm,-5mm\n")
    verts, tris = cad_export._solid_mesh(spec, pitch=None)
    assert _edges_closed(tris)
    vol = _signed_volume(verts, tris) / 6.0
    exact = _steiner_box(0.04, 0.02, 0.01, 0.002) - math.pi * 0.003**2 * 0.01
    assert vol == pytest.approx(exact, rel=2e-3)


def test_build_of_a_stored_canonical_rd_config_matches_boundary_parse() -> None:
    # storage mode re-parses bare metres — the same primitive comes back
    a = build(parse("box:w0.04d0.02h0.01rd0.002"))
    b = build(ShapeSpec("box", {"w": 0.04, "d": 0.02, "h": 0.01, "rd": 0.002}))
    p = vec3(0.019, 0.009, 0.009)
    assert a.distance_local(p) == b.distance_local(p)
