"""cad sampled-field leaf + re-distance + open/close — slice 2 of
``docs/backlog/cad-sdf-rounding-and-field-export.md``.

Pins the :class:`~precis.cad.primitives.Field` leaf (trilinear lookup,
outside-the-box rule, marched ray hits, empty faces), the exact Euclidean
re-distance (Felzenszwalb–Huttenlocher, numpy only) against brute force
and an analytic sphere, the morphology (``open`` ≡ slice-1's ``rd`` within
a pitch and reports what vanished; ``close`` fills a concave seam to an
exact radius), the SIMP bridge, the field's participation in booleans
and the field export backend, the ``field:<sha>`` DSL token with its
injected loader, and the ``chunk_blobs`` store round-trip.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from precis.cad import export as cad_export
from precis.cad.dsl import DslError, build, build_config, format_spec, parse
from precis.cad.fieldmesh import field_mesh
from precis.cad.fieldops import (
    FIELD_MIME,
    OpenResult,
    _edt_1d_pass,
    _edt_sq,
    close,
    decode_field,
    encode_field,
    from_density,
    label_components,
    offset,
    payload_sha256,
    redistance,
)
from precis.cad.fieldops import (
    open as open_field,
)
from precis.cad.primitives import Field, Placed
from precis.cad.relate import (
    _bounds,
    _positive_bounds,
    component_sdf,
    component_sdf_np,
)
from precis.cad.scene import SceneSpec, build_design, parse_source
from precis.cad.tessellate import _signed_volume
from precis.cad.vec import identity, vec3
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.cad import CadHandler
from precis.store._cad_ops import FieldNotFound

_SHA_A = "a" * 64


def _edges_closed(tris: np.ndarray) -> bool:
    directed = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    _u, counts = np.unique(np.sort(directed, axis=1), axis=0, return_counts=True)
    _d, dcounts = np.unique(directed, axis=0, return_counts=True)
    return bool(np.all(counts == 2) and np.all(dcounts == 1))


def _voxel_box(w: float, d: float, h: float, pitch: float) -> tuple[np.ndarray, tuple]:
    """A sharp ``w×d×h`` box (centred x/y, base at z=0) as an occupancy
    whose voxel centres straddle every face symmetrically."""
    nx, ny, nz = round(w / pitch), round(d / pitch), round(h / pitch)
    binary = np.ones((nx, ny, nz), dtype=bool)
    origin = (-w / 2 + pitch / 2, -d / 2 + pitch / 2, pitch / 2)
    return binary, origin


def _voxel_sphere(n: int, R: float, pitch: float) -> tuple[np.ndarray, np.ndarray]:
    centre = np.array([n / 2, n / 2, n / 2]) * pitch
    ijk = np.indices((n, n, n)).reshape(3, -1).T * pitch
    inside = (np.linalg.norm(ijk - centre, axis=1) <= R).reshape(n, n, n)
    return inside, centre


def _sphere_field(
    n: int = 40, R: float = 8.0, pitch: float = 0.5
) -> tuple[Field, np.ndarray]:
    inside, centre = _voxel_sphere(n, R, pitch)
    return redistance(inside, pitch, (0.0, 0.0, 0.0)), centre


# ---------------------------------------------------------------------------
# exact EDT
# ---------------------------------------------------------------------------


def test_edt_1d_pass_matches_brute_force_on_random_lines() -> None:
    rng = np.random.default_rng(0)
    for _ in range(25):
        n, m = int(rng.integers(1, 30)), int(rng.integers(1, 7))
        f = rng.choice([0.0, 1e6, 3.0, 7.5], size=(n, m)).astype(np.float64)
        d = _edt_1d_pass(f)
        q = np.arange(n)
        brute = np.min(
            f[None, :, :] + ((q[:, None] - q[None, :]) ** 2)[:, :, None], axis=1
        )
        assert np.allclose(d, brute)


def test_edt_3d_is_exact_against_brute_force() -> None:
    rng = np.random.default_rng(1)
    sites = rng.random((7, 6, 5)) < 0.1
    sites[3, 3, 2] = True
    d2 = _edt_sq(sites)
    idx = np.argwhere(sites)
    pts = np.indices(sites.shape).reshape(3, -1).T
    brute = np.min(((pts[:, None, :] - idx[None, :, :]) ** 2).sum(-1), axis=1)
    assert np.array_equal(d2, brute.reshape(sites.shape))


def test_redistance_sphere_matches_analytic_within_one_pitch() -> None:
    """Acceptance: |EDT − analytic| ≤ one pitch everywhere in the band, and
    ≤ √3/2·pitch at every voxel centre (the voxelisation error bound)."""
    pitch = 0.5
    fld, centre = _sphere_field(pitch=pitch)
    assert fld.exact
    pts = np.indices(fld.grid.shape).reshape(3, -1).T * pitch + fld.origin
    analytic = np.linalg.norm(pts - centre, axis=1) - 8.0
    err = np.abs(fld.grid.ravel().astype(np.float64) - analytic)
    assert err.max() <= math.sqrt(3) / 2 * pitch + 1e-9
    band = np.abs(analytic) <= 2 * pitch
    assert err[band].max() <= pitch
    # signs agree with the occupancy that made it
    assert np.array_equal(
        fld.grid <= 0,
        np.linalg.norm(pts - centre, axis=1).reshape(fld.grid.shape) <= 8.0,
    )


def test_redistance_refuses_empty_and_full_and_pads_a_bool_input() -> None:
    with pytest.raises(ValueError, match="empty"):
        redistance(np.zeros((4, 4, 4), bool), 1.0, (0, 0, 0))
    with pytest.raises(ValueError, match="full"):
        redistance(np.ones((4, 4, 4), bool), 1.0, (0, 0, 0), pad=0)
    # a full bool box *with* padding is a box — the pad is its outside
    assert redistance(np.ones((4, 4, 4), bool), 1.0, (0, 0, 0)).shape == (8, 8, 8)
    b = np.zeros((4, 4, 4), bool)
    b[1:3, 1:3, 1:3] = True
    f = redistance(b, 1.0, (10.0, 0.0, 0.0), pad=3)
    assert f.shape == (10, 10, 10)
    assert np.allclose(f.origin, [7.0, -3.0, -3.0])
    # boundary samples read +0.5 / -0.5 either side of the surface
    assert f.grid[4, 4, 4] == pytest.approx(-0.5)
    assert f.grid[3, 4, 4] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# the Field primitive
# ---------------------------------------------------------------------------


def test_field_trilinear_lookup_hits_samples_and_interpolates_between() -> None:
    fld, _c = _sphere_field()
    i, j, k = 10, 20, 20
    p = fld.origin + np.array([i, j, k]) * fld.pitch
    assert fld.distance_local(p) == pytest.approx(float(fld.grid[i, j, k]), abs=1e-6)
    mid = p + np.array([0.5, 0.0, 0.0]) * fld.pitch
    expect = 0.5 * (float(fld.grid[i, j, k]) + float(fld.grid[i + 1, j, k]))
    assert fld.distance_local(mid) == pytest.approx(expect, abs=1e-6)
    # vectorised twin agrees with the scalar
    rng = np.random.default_rng(3)
    pts = rng.uniform(-3, 25, size=(200, 3))
    np.testing.assert_allclose(
        fld.distance_local_np(pts), [fld.distance_local(q) for q in pts], atol=1e-9
    )


def test_field_outside_its_box_is_box_distance_plus_boundary_value() -> None:
    fld, _c = _sphere_field()
    lo, hi = fld.aabb_local()
    assert np.allclose(lo, fld.origin)
    assert np.allclose(hi, fld.origin + (np.array(fld.shape) - 1) * fld.pitch)
    face = np.array([hi[0], 10.0, 10.0])
    p = face + np.array([3.0, 0.0, 0.0])
    assert fld.distance_local(p) == pytest.approx(fld.distance_local(face) + 3.0)
    corner = hi + np.array([1.0, 2.0, 2.0])
    assert fld.distance_local(corner) == pytest.approx(fld.distance_local(hi) + 3.0)
    assert not fld.contains_local(corner)
    assert fld.faces_local() == []


def test_field_refuses_bad_grids() -> None:
    with pytest.raises(ValueError, match="nx, ny, nz"):
        Field(grid=np.zeros((4, 4), np.float32), pitch=1.0, origin=vec3(0, 0, 0))
    with pytest.raises(ValueError, match="pitch"):
        Field(grid=np.zeros((4, 4, 4), np.float32), pitch=0.0, origin=vec3(0, 0, 0))
    with pytest.raises(ValueError, match="finite"):
        g = np.zeros((4, 4, 4), np.float32)
        g[0, 0, 0] = np.inf
        Field(grid=g, pitch=1.0, origin=vec3(0, 0, 0))


def test_field_ray_through_voxel_sphere_returns_two_hits_at_the_radii() -> None:
    """Acceptance: a ray through a voxelised sphere enters and leaves at
    the sphere's radius, within a pitch."""
    pitch = 0.5
    fld, c = _sphere_field(pitch=pitch)
    placed = Placed(prim=fld, xform=identity())
    o = vec3(-5.0, c[1], c[2])
    hits = placed.ray_hits(o, vec3(1.0, 0.0, 0.0))
    assert len(hits) == 1
    t_in, t_out = hits[0]
    assert t_in == pytest.approx(c[0] - 8.0 + 5.0, abs=pitch)
    assert t_out == pytest.approx(c[0] + 8.0 + 5.0, abs=pitch)
    # a ray that misses, and one that starts inside
    assert placed.ray_hits(vec3(-5.0, c[1] + 9.0, c[2]), vec3(1.0, 0.0, 0.0)) == []
    # from the centre: the whole line's inside interval (t < 0 included,
    # like every analytic primitive's ray_hits)
    inside = placed.ray_hits(vec3(c[0], c[1], c[2]), vec3(0.0, 0.0, 1.0))
    assert len(inside) == 1 and inside[0][0] == pytest.approx(-8.0, abs=pitch)
    assert inside[0][1] == pytest.approx(8.0, abs=pitch)
    # non-unit direction: t scales, the geometry does not
    hits2 = placed.ray_hits(o, vec3(2.0, 0.0, 0.0))
    assert hits2[0][0] == pytest.approx(t_in / 2, abs=pitch)


def test_field_scaled_changes_every_length_together() -> None:
    fld, _c = _sphere_field()
    mm = fld.scaled(1000.0)
    assert mm.pitch == pytest.approx(fld.pitch * 1000)
    assert np.allclose(mm.origin, fld.origin * 1000)
    assert np.allclose(mm.grid, fld.grid * 1000, rtol=1e-6)
    assert mm.exact == fld.exact
    p = vec3(10.0, 10.0, 10.0)
    assert mm.distance_local(p * 1000) == pytest.approx(
        fld.distance_local(p) * 1000, rel=1e-5
    )


# ---------------------------------------------------------------------------
# morphology
# ---------------------------------------------------------------------------


def test_offset_redistances_a_non_exact_input_first() -> None:
    fld, _c = _sphere_field()
    sign_only = Field(grid=np.sign(fld.grid) * 7.0, pitch=fld.pitch, origin=fld.origin)
    assert not sign_only.exact
    dil = offset(sign_only, -1.0)
    assert not dil.exact
    # the dilated sphere's surface is one unit further out
    p = vec3(_c[0] + 9.0, _c[1], _c[2])
    assert dil.distance_local(p) == pytest.approx(0.0, abs=fld.pitch)
    with pytest.raises(ValueError, match="below half the field pitch"):
        open_field(fld, 0.1)


def test_open_on_a_sharp_voxel_box_equals_rd_box_within_a_pitch() -> None:
    """Acceptance: ``open(r)`` on a sharp voxel box ≡ slice-1's ``rd=r`` box
    within a pitch — SDF samples and exported volumes."""
    pitch = 0.5
    binary, origin = _voxel_box(40, 20, 10, pitch)
    sharp = redistance(binary, pitch, origin, pad=6)
    res = open_field(sharp, 2.0)
    assert isinstance(res, OpenResult)
    assert res.vanished == [] and res.findings == [] and not res.empty
    rd = build_config("box:w40d20h10rd2")
    rng = np.random.default_rng(1)
    pts = rng.uniform([-22, -12, -2], [22, 12, 12], size=(20000, 3))
    diff = np.abs(res.field.distance_local_np(pts) - rd.distance_local_np(pts))
    assert diff.max() <= pitch
    lo, hi = res.field.aabb_local()
    v1, t1 = field_mesh(res.field.distance_local_np, lo, hi, pitch)
    v2, t2 = field_mesh(
        rd.distance_local_np, vec3(-20, -10, 0), vec3(20, 10, 10), pitch
    )
    assert _edges_closed(t1)
    vol1 = _signed_volume(v1, t1) / 6.0
    vol2 = _signed_volume(v2, t2) / 6.0
    # a surface-area × sub-pitch offset; observed ~0.9 % at pitch = r/4
    assert vol1 == pytest.approx(vol2, rel=0.02)
    assert np.allclose(v1.min(axis=0), [-20, -10, 0], atol=pitch)
    assert np.allclose(v1.max(axis=0), [20, 10, 10], atol=pitch)


def test_close_on_two_touching_boxes_fills_the_concave_seam_to_radius_r() -> None:
    """Acceptance: a step (box on box) closed by ``r`` gains a fillet whose
    surface is the arc of radius ``r`` tangent to both faces — probed at the
    seam midpoint (the concave corner), on the arc, and just outside it."""
    pitch = 0.5
    binary = np.zeros((40, 40, 40), dtype=bool)
    binary[:, :, :20] = True  # lower box: x in [-10, 10], z in [0, 10]
    binary[:20, :, 20:] = True  # upper box: x in [-10, 0], z in [10, 20]
    origin = (-10 + pitch / 2, -10 + pitch / 2, pitch / 2)
    step = redistance(binary, pitch, origin, pad=10)
    r = 3.0
    closed = close(step, r)
    corner = vec3(0.0, 0.0, 10.0)  # the seam's midpoint (mid-y)
    assert step.distance_local(corner) == pytest.approx(0.0, abs=pitch)
    assert closed.distance_local(corner) == pytest.approx(
        -r * (math.sqrt(2) - 1), abs=pitch
    )
    k = r * (1 - 1 / math.sqrt(2))
    on_arc = vec3(k, 0.0, 10.0 + k)
    assert closed.distance_local(on_arc) == pytest.approx(0.0, abs=pitch)
    outside = vec3(1.5, 0.0, 11.5)  # inside the tangent ball → stays outside
    assert closed.distance_local(outside) == pytest.approx(
        r - math.hypot(1.5, 1.5), abs=pitch
    )
    # convex features untouched
    assert closed.distance_local(vec3(12.0, 0.0, 5.0)) == pytest.approx(2.0, abs=pitch)
    with pytest.raises(ValueError, match="reaches the field's box"):
        close(step, 6.0)


def test_open_reports_vanished_struts_and_blobs_not_silently() -> None:
    pitch = 1.0
    binary = np.zeros((30, 14, 14), dtype=bool)
    binary[2:12, 3:11, 3:11] = True  # a 10×8×8 block
    binary[12:20, 7, 7] = True  # a 1-voxel strut sticking out of it
    binary[24:26, 6:8, 6:8] = True  # a separate 2-voxel blob
    labels, n = label_components(binary)
    assert n == 2
    fld = redistance(binary, pitch, (0.0, 0.0, 0.0))
    res = open_field(fld, pitch)
    assert not res.empty
    assert len(res.vanished) == 2 and len(res.findings) == 2
    assert all(
        f.rule == "open-vanished-component" and f.severity == "warn"
        for f in res.findings
    )
    by_x = sorted(res.vanished, key=lambda c: c.centroid[0])
    strut, blob = by_x
    # centroids in the field's frame: origin (0,0,0) named voxel [0,0,0]'s
    # centre, so world x == the occupancy index (the pad shifts the origin)
    assert strut.voxels >= 8 and strut.centroid[0] == pytest.approx(15.5, abs=1.5)
    assert blob.voxels == 8 and blob.centroid[0] == pytest.approx(24.5, abs=1e-6)
    assert sum(c.volume for c in res.vanished) == pytest.approx(
        sum(c.voxels for c in res.vanished) * pitch**3
    )
    # the block survives, rounded
    assert res.field.distance_local(vec3(7.0, 7.0, 7.0)) < 0
    # everything thinner than 2r → empty result, every component reported at error
    thin = np.zeros((10, 10, 10), dtype=bool)
    thin[2:8, 5, 2:8] = True
    thin[5, 2:8, 9] = True  # not touching the first sheet
    res2 = open_field(redistance(thin, pitch, (0.0, 0.0, 0.0)), pitch)
    assert res2.empty and len(res2.vanished) == 2
    assert all(f.severity == "error" for f in res2.findings)
    assert not np.any(res2.field.grid <= 0)


# ---------------------------------------------------------------------------
# SIMP bridge + export
# ---------------------------------------------------------------------------


def _cantilever_density(max_iter: int = 20) -> np.ndarray:
    from precis.structsolve.simp import simp_optimize

    nx, ny, nz = 16, 4, 4
    domain = np.ones((nx, ny, nz), dtype=bool)
    share = -1.0 / (ny + 1)
    loads = [((nx, j, nz // 2), (0.0, 0.0, share)) for j in range(ny + 1)]
    supports = [((0, j, k), "xyz") for j in range(ny + 1) for k in range(nz + 1)]
    return simp_optimize(
        domain, loads, supports, volfrac=0.4, max_iter=max_iter
    ).density


def test_from_density_on_the_cantilever_exports_watertight_and_open_reports(
    tmp_path: Path,
) -> None:
    """Acceptance: ``from_density`` on the engine's own cantilever fixture
    exports watertight through the slice-1 backend; ``open(pitch)`` reports
    the vanished-component count."""
    dens = _cantilever_density()
    h = 1e-3  # 1 mm elements, metres
    fld = from_density(dens, pitch=h, origin=(h / 2, h / 2, h / 2))
    assert fld.exact and fld.shape == (20, 8, 8)
    assert np.array_equal(fld.grid <= 0, np.pad(dens >= 0.5, 2))
    with pytest.raises(ValueError, match="threshold"):
        from_density(dens, threshold=1.5, pitch=h, origin=(0, 0, 0))
    # export through the field backend, as a design (metres → mm on the way)
    spec = parse_source(f"component beam\nbody add field:{_SHA_A}\n")
    spec.field_loader = {_SHA_A: fld}.__getitem__
    assert cad_export.needs_field_backend(spec)
    # a 1-element sheet's zero set sits exactly between its samples: sample
    # finer than the element so the marching grid cannot straddle it
    out = cad_export.export_mesh(spec, tmp_path / "beam.stl", pitch=h / 2)
    raw = out.read_bytes()
    n_tri = int.from_bytes(raw[80:84], "little")
    tri = np.frombuffer(raw[84:], dtype=np.uint8).reshape(n_tri, 50)
    pts = np.frombuffer(tri[:, 12:48].tobytes(), dtype="<f4").reshape(3 * n_tri, 3)
    assert n_tri > 0
    assert pts[:, 0].min() >= -0.5 and pts[:, 0].max() <= 16.5  # mm
    verts, tris = cad_export._solid_mesh(spec, pitch=h / 2)
    assert _edges_closed(tris) and _signed_volume(verts, tris) > 0
    # open(pitch): the 4×4 section at volfrac 0.4 is all 1-element sheets,
    # so every component vanishes — reported, not raised
    res = open_field(fld, h)
    assert res.empty
    assert len(res.vanished) == label_components(dens >= 0.5)[1] >= 1
    # STEP has no route for a field
    with pytest.raises(cad_export.ExportError, match="field"):
        cad_export.export_step(spec, tmp_path / "beam.step")


def test_field_cut_by_analytic_cyl_exports_bore_at_the_exact_radius() -> None:
    """Acceptance: a field ``cut`` by an analytic ``cyl`` meshes with the
    bore at the cylinder's radius — measured on the mesh."""
    pitch = 0.5
    binary, origin = _voxel_box(30, 30, 10, pitch)
    fld = redistance(binary, pitch, origin)
    spec = parse_source(
        f"component p\nbody add field:{_SHA_A}\nbore cut cyl:r4mh20m @0m,0m,-5m\n"
    )
    spec.field_loader = {_SHA_A: fld}.__getitem__
    design = build_design(spec)
    expr = design.whole()
    # bounds: the positive material is the field's own box; the (taller)
    # cutter widens only the tight union of every leaf
    pos = _positive_bounds(design, expr)
    box = _bounds(design, expr)
    assert pos is not None and box is not None
    flo, fhi = fld.aabb_local()
    assert np.allclose(pos[0], flo) and np.allclose(pos[1], fhi)
    assert np.allclose(box[0][:2], flo[:2]) and box[0][2] == pytest.approx(-5.0)
    assert box[1][2] == pytest.approx(15.0)
    # the fold: min/max over the field and the cylinder, scalar == vectorised
    p = vec3(0.0, 0.0, 5.0)
    assert component_sdf(design, expr, p) == pytest.approx(4.0, abs=pitch)
    pts = np.array([[0.0, 0.0, 5.0], [10.0, 0.0, 5.0], [20.0, 0.0, 5.0]])
    np.testing.assert_allclose(
        component_sdf_np(design, expr, pts),
        [component_sdf(design, expr, q) for q in pts],
        atol=1e-9,
    )
    verts, tris = cad_export._solid_mesh(spec, pitch=pitch)
    assert _edges_closed(tris)
    rho = np.hypot(verts[:, 0], verts[:, 1])
    wall = (verts[:, 2] > 2.0) & (verts[:, 2] < 8.0) & (rho < 6.0)
    assert wall.sum() > 100
    assert np.abs(rho[wall] - 4.0).max() < 0.05 * pitch
    vol = _signed_volume(verts, tris) / 6.0
    assert vol == pytest.approx(30 * 30 * 10 - math.pi * 16 * 10, rel=0.01)
    # ray probe through the bore: two material spans either side of it
    spans = design.ray(vec3(-20.0, 0.0, 5.0), vec3(1.0, 0.0, 0.0))
    solid = [s for s in spans if s.state == "solid"]
    void = [s for s in spans if s.state == "void"]
    assert len(solid) == 2 and len(void) == 1
    assert void[0].t_in == pytest.approx(16.0, abs=pitch)
    assert void[0].t_out == pytest.approx(24.0, abs=pitch)


# ---------------------------------------------------------------------------
# DSL token + loader
# ---------------------------------------------------------------------------


def test_field_token_parses_prefix_or_full_sha_and_round_trips() -> None:
    spec = parse(f"field:{_SHA_A[:12]}", require_units=True)
    assert spec.alias == "field" and spec.params == {} and spec.ref == "a" * 12
    assert format_spec(spec) == f"field:{'a' * 12}"
    assert format_spec(spec, units=True) == f"field:{'a' * 12}"
    full = parse(f"field:{_SHA_A.upper()}")
    assert full.ref == _SHA_A
    for bad in ("field:abc", "field:" + "g" * 12, "field:" + "a" * 65, "field:"):
        with pytest.raises(DslError, match="sha256"):
            parse(bad, require_units=True)


def test_rd_on_a_field_is_refused_at_parse_pointing_at_fieldops() -> None:
    with pytest.raises(DslError, match="fieldops.open"):
        parse(f"field:{_SHA_A}rd2mm", require_units=True)


def test_field_builds_only_through_a_loader_and_names_an_unknown_ref() -> None:
    spec = parse(f"field:{_SHA_A}")
    with pytest.raises(DslError, match="needs a field loader"):
        build(spec)
    empty: dict[str, Field] = {}
    with pytest.raises(DslError, match=_SHA_A[:12]):
        build(spec, field_loader=empty.__getitem__)
    fld, _c = _sphere_field()
    assert build(spec, field_loader={_SHA_A: fld}.__getitem__) is fld
    # a design source validates the token without a loader and builds with one
    scene = parse_source(f"component p\nbody add field:{_SHA_A}\n")
    assert scene.nodes[0].config == f"field:{_SHA_A}"
    with pytest.raises(DslError, match="needs a field loader"):
        build_design(scene)
    design = build_design(scene, field_loader={_SHA_A: fld}.__getitem__)
    assert len(design.instances) == 1
    scene.field_loader = {_SHA_A: fld}.__getitem__
    assert build_design(scene).instances.keys() == design.instances.keys()
    # the loader is runtime plumbing: not part of equality, not in meta
    other = SceneSpec(
        nodes=list(scene.nodes),
        components=list(scene.components),
        meta=dict(scene.meta),
    )
    assert other == scene and "field_loader" not in scene.meta


def test_encode_decode_field_round_trips_bytes_and_header() -> None:
    fld, _c = _sphere_field()
    payload = encode_field(fld, {"source": "test"})
    header, back = decode_field(payload)
    assert header["shape"] == list(fld.shape) and header["exact"] is True
    assert header["dtype"] == "float32" and header["byteorder"] == "<"
    assert header["provenance"] == {"source": "test"}
    assert np.array_equal(back.grid, fld.grid) and back.pitch == fld.pitch
    assert np.allclose(back.origin, fld.origin) and back.exact
    assert len(payload_sha256(payload)) == 64
    with pytest.raises(ValueError, match="magic"):
        decode_field(b"nope" + payload[4:])
    with pytest.raises(ValueError, match="bytes"):
        decode_field(payload[:-4])


# ---------------------------------------------------------------------------
# store round-trip + handler
# ---------------------------------------------------------------------------


@pytest.fixture
def cad(store):
    return CadHandler(hub=Hub(store=store))


def _chunk_rows(store, ref_id: int) -> list[tuple]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT c.chunk_id, c.chunk_kind, c.ord, b.sha256, b.mime, b.size_bytes "
            "FROM chunks c JOIN chunk_blobs b ON b.chunk_id = c.chunk_id "
            "WHERE c.ref_id = %s ORDER BY c.chunk_id",
            (ref_id,),
        ).fetchall()


def test_store_put_get_field_round_trip_dedupes_and_never_updates(cad, store) -> None:
    cad.put(id="seat", text="base add box:w40mmd20mmh10mm")
    ref = store.get_ref(kind="cad", id="seat")
    fld, _c = _sphere_field()
    fld = fld.scaled(1e-3)  # metres, like every stored grid
    sha = store.put_field(ref.id, fld, provenance={"source": "test-sphere"})
    assert len(sha) == 64
    header, back = store.get_field(sha)
    assert np.array_equal(back.grid, fld.grid)  # byte-identical samples
    assert (
        back.pitch == fld.pitch and np.allclose(back.origin, fld.origin) and back.exact
    )
    assert header["shape"] == [44, 44, 44] and header["provenance"] == {
        "source": "test-sphere"
    }
    assert header["sha256"] == sha
    rows = _chunk_rows(store, ref.id)
    assert len(rows) == 1
    assert rows[0][1] == "field" and rows[0][2] >= 0 and rows[0][3] == sha
    assert rows[0][4] == FIELD_MIME
    # the summary line is the chunk text; the header is on meta without the bytes
    head = store.field_header(sha[:12])
    assert head is not None and head["pitch_m"] == pytest.approx(0.5e-3)
    # same grid + same provenance → same address, no second row
    assert store.put_field(ref.id, fld, provenance={"source": "test-sphere"}) == sha
    assert len(_chunk_rows(store, ref.id)) == 1
    # a changed grid → a new chunk; the old one untouched and still loadable
    fld2 = Field(grid=fld.grid + np.float32(1e-4), pitch=fld.pitch, origin=fld.origin)
    sha2 = store.put_field(ref.id, fld2)
    assert sha2 != sha
    rows2 = _chunk_rows(store, ref.id)
    assert len(rows2) == 2 and rows2[0] == rows[0]
    assert np.array_equal(store.get_field(sha)[1].grid, fld.grid)
    # prefix resolution, unknown, ambiguous
    assert store.field_sha(sha[:12]) == sha
    with pytest.raises(FieldNotFound, match="deadbeef"):
        store.get_field("deadbeefdead")
    with pytest.raises(NotFound, match="not a sha256"):
        store.field_sha("xyz")
    assert store.field_header("deadbeefdead") is None


def test_cad_load_rebuilds_a_field_leaf_and_the_handler_renders_it(
    cad, store, tmp_path
) -> None:
    cad.put(id="seat", text="base add box:w40mmd20mmh10mm")
    ref = store.get_ref(kind="cad", id="seat")
    pitch = 0.5e-3
    binary, origin = _voxel_box(30e-3, 30e-3, 10e-3, pitch)
    fld = redistance(binary, pitch, origin)
    sha = store.put_field(ref.id, fld, provenance={"source": "voxel box"})
    # a 12-char prefix on the boundary; the stored config carries the full sha
    resp = cad.put(
        id="seat",
        text=f"component p\nbody add field:{sha[:12]}\nbore cut cyl:r4mmh20mm @0mm,0mm,-5mm\n",
    )
    assert "updated" in resp.body
    assert f"field:{sha[:12]} 64×64×24 @500 µm" in resp.body
    assert "trusted only inside its own box" in resp.body
    spec, _handles = store.cad_load(ref.id)
    assert spec.nodes[0].config == f"field:{sha}"
    assert spec.field_loader is not None
    design = build_design(spec)
    assert isinstance(design.instances["i1"].placed.prim, Field)
    # the probes see the field: the bore reads as void, carved by `bore`
    ray = cad.get(
        id="seat", view="ray", args={"o": ["-20mm", "0mm", "5mm"], "d": [1, 0, 0]}
    )
    assert "void" in ray.body and "bore" in ray.body
    # stl export through the field backend, at the caller's pitch
    out = tmp_path / "seat.stl"
    resp = cad.get(id="seat", view="stl", args={"path": str(out), "pitch": "0.5mm"})
    assert out.exists() and out.stat().st_size > 84 and "STL" in resp.body
    # an unknown reference is refused on put, naming it, before anything is saved
    with pytest.raises(BadInput, match="deadbeefdead"):
        cad.put(id="seat2", text="body add field:deadbeefdead")
    assert store.get_ref(kind="cad", id="seat2") is None


def test_field_chunks_are_skipped_by_the_embed_and_summarize_cascades() -> None:
    """A field chunk carries a binary grid behind a one-line blurb — it must
    never reach the embedder or the summarizer (reviewer finding, slice 2):
    an iterative optimiser mints one per changed grid."""
    from precis.workers.embed import EmbedHandler
    from precis.workers.summarize import RakeLemmaHandler

    assert "field" in EmbedHandler.skip_chunk_kinds
    assert "field" in RakeLemmaHandler.skip_chunk_kinds
