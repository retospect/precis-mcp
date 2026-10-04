"""Print-readiness mesh checks (:mod:`precis.cad.mesh_check`): floating
regions (Bambu's SharpTail rule), sub-layer tail lifting, weld/degenerate
cleanup, and 3MF package validation.

The headline fixture is the real failure in miniature: a ledge hanging off
a column whose far bottom corner is pulled 0.04 mm below the rest of its
flat underside. Sliced at 0.2 mm that corner is a small polygon with
nothing under it (an island); lifted, the ledge's first layer grows out of
the column's polygon and the island is gone.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import numpy as np
import pytest

from precis.cad.export import _write_3mf, manifold_available
from precis.cad.mesh_check import (
    CANTILEVER_WARN_MM,
    count_slivers,
    floating_islands,
    lift_rejection,
    lift_sharp_tails,
    lift_sharp_tails_checked,
    package_findings,
    signed_volume,
    slicer_cantilevers,
    weld_and_drop_degenerate,
)

pytestmark = pytest.mark.skipif(
    not manifold_available(), reason="manifold3d not installed"
)

LAYER = 0.2


def _cube(lo: tuple[float, float, float], hi: tuple[float, float, float]):
    """A closed, outward-wound box mesh (verts float64, faces int)."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = np.array(
        [
            (x0, y0, z0),
            (x1, y0, z0),
            (x1, y1, z0),
            (x0, y1, z0),
            (x0, y0, z1),
            (x1, y0, z1),
            (x1, y1, z1),
            (x0, y1, z1),
        ],
        dtype=np.float64,
    )
    f = np.array(
        [
            (0, 2, 1),
            (0, 3, 2),
            (4, 5, 6),
            (4, 6, 7),
            (0, 1, 5),
            (0, 5, 4),
            (1, 2, 6),
            (1, 6, 5),
            (2, 3, 7),
            (2, 7, 6),
            (3, 0, 4),
            (3, 4, 7),
        ],
        dtype=np.int64,
    )
    return v, f


def _union(*boxes: tuple[tuple[float, ...], tuple[float, ...]]):
    """Boolean union of boxes through manifold3d -> welded (verts, faces)."""
    import manifold3d as m3d

    solid = None
    for lo, hi in boxes:
        size = [h - lo_ for lo_, h in zip(lo, hi, strict=True)]
        b = m3d.Manifold.cube(size).translate(list(lo))
        solid = b if solid is None else solid + b
    assert solid is not None
    mesh = solid.to_mesh()
    return (
        np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3],
        np.asarray(mesh.tri_verts, dtype=np.int64),
    )


def _ledge_with_tip():
    """Column on the bed + a ledge off its side (underside z=5.12) whose far
    bottom corner (15, 7) is pulled down to z=5.08."""
    v, f = _union(
        ((0.0, 0.0, 0.0), (10.0, 10.0, 10.0)),
        ((10.0, 3.0, 5.12), (15.0, 7.0, 9.0)),
    )
    corner = np.where(
        (np.abs(v[:, 0] - 15.0) < 1e-4)
        & (np.abs(v[:, 1] - 7.0) < 1e-4)
        & (np.abs(v[:, 2] - 5.12) < 1e-3)
    )[0]
    assert len(corner) == 1
    v = v.copy()
    v[corner[0], 2] = 5.08
    return v, f, int(corner[0])


def _closed_and_positive(v: np.ndarray, f: np.ndarray, tmp_path: Path) -> list[str]:
    """package_findings of the mesh written as 3MF ([] = closed, consistently
    wound, positive volume)."""
    out = tmp_path / "m.3mf"
    _write_3mf(out, [("m", v, f)])
    return package_findings(out.read_bytes())


# -- floating_islands --------------------------------------------------------


def test_box_on_bed_has_no_islands() -> None:
    v, f = _cube((0, 0, 0), (10, 10, 10.03))
    assert floating_islands(v, f, LAYER) == []


def test_separate_shell_floating_above_a_base_is_found() -> None:
    v, f = _union(
        ((0.0, 0.0, 0.0), (10.0, 10.0, 3.0)),
        ((0.0, 0.0, 5.0), (10.0, 10.0, 8.0)),
    )
    islands = floating_islands(v, f, LAYER)
    assert len(islands) == 1
    assert islands[0].z == pytest.approx(5.1, abs=1e-6)
    assert islands[0].area == pytest.approx(100.0, rel=1e-3)
    assert (islands[0].x, islands[0].y) == pytest.approx((5.0, 5.0), abs=1e-3)


def test_pendant_with_flat_underside_is_a_real_island_the_lift_cannot_hide() -> None:
    # column on the bed, an arm across its top, a pendant hanging from the
    # arm's far end: the pendant's flat underside (z=5) has nothing below.
    v, f = _union(
        ((0.0, 0.0, 0.0), (2.0, 2.0, 10.0)),
        ((0.0, 0.0, 8.0), (12.0, 2.0, 10.0)),
        ((10.0, 0.0, 5.0), (12.0, 2.0, 8.0)),
    )
    before = floating_islands(v, f, LAYER)
    assert len(before) == 1 and before[0].z == pytest.approx(5.1, abs=1e-6)
    lifted, n = lift_sharp_tails(v, f, tol=LAYER / 2)
    assert n == 0  # a flat underside is not a strict local minimum
    assert np.array_equal(lifted, v)
    assert len(floating_islands(lifted, f, LAYER)) == 1


# -- lift_sharp_tails --------------------------------------------------------


def test_sub_layer_tip_is_an_island_until_lifted(tmp_path: Path) -> None:
    v, f, tip = _ledge_with_tip()
    before = floating_islands(v, f, LAYER)
    assert len(before) == 1
    assert before[0].z == pytest.approx(5.1, abs=1e-6)
    assert before[0].x > 12.0  # out at the far corner, not at the column

    lifted, n = lift_sharp_tails(v, f, tol=LAYER / 2)
    assert n == 1
    assert lifted[tip, 2] == pytest.approx(5.12, abs=1e-3)
    assert np.array_equal(np.delete(lifted, tip, axis=0), np.delete(v, tip, axis=0))
    assert floating_islands(lifted, f, LAYER) == []
    # topology untouched, still a closed, consistently wound, positive solid
    assert _closed_and_positive(lifted, f, tmp_path) == []


def test_lift_is_bounded_by_tol() -> None:
    v, f, tip = _ledge_with_tip()
    v[tip, 2] = 4.5  # 0.62 below the underside: a real stalactite, not a tail
    lifted, n = lift_sharp_tails(v, f, tol=LAYER / 2)
    assert n == 0
    assert np.array_equal(lifted, v)


def test_lift_never_touches_vertices_on_the_bed() -> None:
    v, f = _cube((0, 0, 0), (10, 10, 10))
    v[0, 2] = -0.04  # below the bed line it was told about
    lifted, n = lift_sharp_tails(v, f, bed_z=0.0, tol=LAYER / 2)
    assert n == 0 and lifted[0, 2] == -0.04


def test_lift_takes_a_two_vertex_floor_as_one_tail() -> None:
    # the tip (5.08) beside a vertex already at 5.09: lifting the tip alone
    # would leave a two-vertex floor under the 5.12 underside, still an
    # island at the 5.1 slice - the plateau goes up together.
    v, f, tip = _ledge_with_tip()
    nbr = np.where(
        (np.abs(v[:, 0] - 15.0) < 1e-4)
        & (np.abs(v[:, 1] - 3.0) < 1e-4)
        & (np.abs(v[:, 2] - 5.12) < 1e-3)
    )[0]
    assert len(nbr) == 1
    v[nbr[0], 2] = 5.09
    assert len(floating_islands(v, f, LAYER)) == 1
    lifted, n = lift_sharp_tails(v, f, tol=LAYER / 2)
    assert n == 2
    assert lifted[tip, 2] == pytest.approx(5.12, abs=1e-3)
    assert lifted[nbr[0], 2] == pytest.approx(5.12, abs=1e-3)
    assert floating_islands(lifted, f, LAYER) == []


def test_total_motion_of_any_vertex_stays_within_tol() -> None:
    # a tip 0.09 under the underside is within tol=0.1 and goes all the way;
    # one 0.19 under is a real dip: nothing moves (the cap is tol, not 2*tol)
    v, f, tip = _ledge_with_tip()
    v[tip, 2] = 5.12 - 0.09
    lifted, n = lift_sharp_tails(v, f, tol=LAYER / 2)
    assert n == 1 and lifted[tip, 2] == pytest.approx(5.12, abs=1e-3)
    v[tip, 2] = 5.12 - 0.19
    lifted, n = lift_sharp_tails(v, f, tol=LAYER / 2)
    assert n == 0 and np.array_equal(lifted, v)


def test_lift_is_idempotent() -> None:
    v, f, _tip = _ledge_with_tip()
    once, n1 = lift_sharp_tails(v, f, tol=LAYER / 2)
    twice, n2 = lift_sharp_tails(once, f, tol=LAYER / 2)
    assert (n1, n2) == (1, 0)
    assert np.array_equal(once, twice)


# -- weld / degenerate -------------------------------------------------------


def test_weld_merges_coincident_vertices_and_drops_collapsed_triangles() -> None:
    v, f = _cube((0, 0, 0), (1, 1, 1))
    # duplicate vertex 0 as vertex 8 and use it in one triangle; add a
    # triangle that repeats an index once welded (0, 8, 1) and one sliver
    # (three collinear distinct points).
    v2 = np.vstack([v, v[0], [0.5, 0.0, 0.0]])
    f2 = np.vstack([f, [[0, 8, 1], [0, 9, 1]]])
    f2[0] = (8, 2, 1)  # first bottom triangle now through the duplicate
    wv, wf, dropped = weld_and_drop_degenerate(v2, f2)
    assert dropped == 1
    assert len(wv) == 9  # 8 cube corners + the collinear midpoint
    assert wf.max() < len(wv)
    assert count_slivers(wv, wf) == 1  # (0, mid, 1): distinct, zero area


def test_count_slivers_clean_cube_is_zero() -> None:
    v, f = _cube((0, 0, 0), (1, 1, 1))
    assert count_slivers(v, f) == 0


# -- package_findings --------------------------------------------------------


def _cube_package(tmp_path: Path) -> bytes:
    v, f = _cube((0, 0, 0), (10, 10, 10))
    out = tmp_path / "cube.3mf"
    _write_3mf(out, [("cube", v, f)], title="cube")
    return out.read_bytes()


def _rewrite(data: bytes, *, drop: str | None = None, model_sub=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(buf, "w") as zout:
        for name in zin.namelist():
            if name == drop:
                continue
            body = zin.read(name)
            if name == "3D/3dmodel.model" and model_sub is not None:
                body = model_sub(body.decode("utf-8")).encode("utf-8")
            zout.writestr(name, body)
    return buf.getvalue()


def test_valid_cube_package_has_no_findings(tmp_path: Path) -> None:
    assert package_findings(_cube_package(tmp_path)) == []


def test_missing_rels_is_reported(tmp_path: Path) -> None:
    bad = _rewrite(_cube_package(tmp_path), drop="_rels/.rels")
    assert any("_rels/.rels" in p for p in package_findings(bad))


def test_open_mesh_is_reported(tmp_path: Path) -> None:
    bad = _rewrite(
        _cube_package(tmp_path),
        model_sub=lambda s: re.sub(r"<triangle [^>]*/>", "", s, count=1),
    )
    assert any("not closed" in p for p in package_findings(bad))


def test_flipped_triangle_is_reported_as_winding(tmp_path: Path) -> None:
    def flip_first(s: str) -> str:
        m = re.search(r'<triangle v1="(\d+)" v2="(\d+)" v3="(\d+)"/>', s)
        assert m is not None
        a, b, c = m.groups()
        return s.replace(m.group(0), f'<triangle v1="{a}" v2="{c}" v3="{b}"/>', 1)

    bad = _rewrite(_cube_package(tmp_path), model_sub=flip_first)
    assert any("winding" in p for p in package_findings(bad))


def test_inside_out_mesh_has_non_positive_volume(tmp_path: Path) -> None:
    v, f = _cube((0, 0, 0), (10, 10, 10))
    out = tmp_path / "inside_out.3mf"
    _write_3mf(out, [("c", v, f[:, ::-1].copy())])
    assert any("not positive" in p for p in package_findings(out.read_bytes()))


def test_build_item_with_missing_object_is_reported(tmp_path: Path) -> None:
    bad = _rewrite(
        _cube_package(tmp_path),
        model_sub=lambda s: s.replace('<item objectid="1"/>', '<item objectid="7"/>'),
    )
    assert any("missing object" in p for p in package_findings(bad))


def test_out_of_range_index_and_missing_unit_are_reported(tmp_path: Path) -> None:
    bad = _rewrite(
        _cube_package(tmp_path),
        model_sub=lambda s: s.replace(' unit="millimeter"', "").replace(
            'v3="7"', 'v3="99"', 1
        ),
    )
    problems = package_findings(bad)
    assert any("unit" in p for p in problems)
    assert any("out of range" in p for p in problems)


def test_not_a_zip() -> None:
    assert package_findings(b"nope") == ["not a zip package"]


# -- the writer --------------------------------------------------------------


def test_write_3mf_metadata_and_no_metadata_dir(tmp_path: Path) -> None:
    v, f = _cube((0, 0, 0), (1, 1, 1))
    out = tmp_path / "t.3mf"
    _write_3mf(out, [("a<b>", v, f)], title="my & part")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        model = zf.read("3D/3dmodel.model").decode("utf-8")
    assert sorted(names) == ["3D/3dmodel.model", "[Content_Types].xml", "_rels/.rels"]
    assert not any(n.startswith("Metadata/") for n in names)
    assert '<metadata name="Title">my &amp; part</metadata>' in model
    assert '<metadata name="Application">precis</metadata>' in model
    assert re.search(
        r'<metadata name="CreationDate">\d{4}-\d{2}-\d{2}</metadata>', model
    )
    assert package_findings(out.read_bytes()) == []  # escaped name still parses
    default = tmp_path / "d.3mf"
    _write_3mf(default, [("a", v, f)])
    with zipfile.ZipFile(default) as zf:
        assert 'name="Title">precis export<' in zf.read("3D/3dmodel.model").decode(
            "utf-8"
        )


def _ledge_with_plateau_dimple():
    """Column (10 x 10 x 100, so the lift is a tiny share of the volume) +
    ledge (underside z=5.62) whose whole far edge is pulled down 0.34 mm to
    z=5.28: a two-vertex floor, 1.7 layers deep at 0.2 mm."""
    v, f = _union(
        ((0.0, 0.0, 0.0), (10.0, 10.0, 100.0)),
        ((10.0, 3.0, 5.62), (15.0, 7.0, 9.0)),
    )
    far = (np.abs(v[:, 0] - 15.0) < 1e-4) & (np.abs(v[:, 2] - 5.62) < 1e-3)
    assert int(far.sum()) == 2
    v = v.copy()
    v[far, 2] = 5.28
    return v, f


def test_dimple_deeper_than_half_a_layer_survives_a_layer_scaled_lift() -> None:
    v, f = _ledge_with_plateau_dimple()
    assert len(floating_islands(v, f, LAYER)) == 1
    lifted, n = lift_sharp_tails(v, f, tol=LAYER / 2)
    assert n == 0
    assert len(floating_islands(lifted, f, LAYER)) == 1  # reported, not hidden


def test_dimple_is_cleared_by_a_voxel_scaled_lift(tmp_path: Path) -> None:
    v, f = _ledge_with_plateau_dimple()
    lifted, n = lift_sharp_tails(v, f, tol=0.5)  # half a 1 mm voxel
    assert n == 2
    assert floating_islands(lifted, f, LAYER) == []
    assert _closed_and_positive(lifted, f, tmp_path) == []


# -- the lift's guards -------------------------------------------------------


def test_thin_slab_is_left_alone() -> None:
    # 0.05 mm slab floating at z=1: lifting its floor to the top would zero
    # the volume. The clearance guard refuses (rise must be < half the
    # material above).
    v, f = _cube((0, 0, 1.0), (10, 10, 1.05))
    res = lift_sharp_tails_checked(v, f, tol=LAYER / 2)
    assert res.n_lifted == 0
    assert res.n_kept_thin == 1
    assert np.array_equal(res.vertices, v)
    assert signed_volume(res.vertices, f) == pytest.approx(signed_volume(v, f))


def test_vertices_within_1e4_of_the_bed_are_the_bed() -> None:
    v, f = _cube((0, 0, 5e-5), (10, 10, 0.05))
    res = lift_sharp_tails_checked(v, f, tol=LAYER / 2)
    assert (res.n_lifted, res.n_kept_thin, res.n_kept_flip) == (0, 0, 0)
    assert np.array_equal(res.vertices, v)


def _prism(poly_xz: list[tuple[float, float]], depth: float = 1.0):
    """Extrude a convex CCW (x, z) polygon along y; fan-triangulated from
    vertex 0 on the front and back faces. Returns outward-wound (v, f)."""
    k = len(poly_xz)
    v = np.array(
        [(x, 0.0, z) for x, z in poly_xz] + [(x, depth, z) for x, z in poly_xz],
        dtype=np.float64,
    )
    tri: list[tuple[int, int, int]] = []
    for i in range(1, k - 1):
        tri.append((0, i, i + 1))  # front
        tri.append((k, k + i + 1, k + i))  # back
    for i in range(k):
        j = (i + 1) % k
        tri.append((i, j, k + j))
        tri.append((i, k + j, k + i))
    f = np.array(tri, dtype=np.int64)
    if signed_volume(v, f) < 0:
        f = f[:, ::-1].copy()
    return v, f


def test_wall_triangle_that_would_flip_keeps_its_plateau_unlifted() -> None:
    # Front wall triangle (P, Q, R) with P below the line through Q and R:
    # raising P to Q's height would carry it over that line and flip the
    # face (xy-collinear: it is a vertical wall). Plenty of material above.
    v, f = _prism([(0, 4.9), (1, 5.05), (2, 5.15), (2, 8.0), (0, 8.0)])
    assert signed_volume(v, f) > 0
    res = lift_sharp_tails_checked(v, f, tol=0.2)
    assert res.n_kept_flip == 1
    assert res.n_lifted == 0
    assert np.array_equal(res.vertices, v)


def test_lift_rejection_backstop() -> None:
    v, f = _cube((0, 0, 0), (10, 10, 10))
    assert lift_rejection(v, v, f) is None
    squashed = v.copy()
    squashed[v[:, 2] > 5, 2] = 5.5  # top dragged down: -45% volume
    why = lift_rejection(v, squashed, f)
    assert why is not None and "volume" in why
    nudged = v.copy()
    nudged[v[:, 2] > 5, 2] -= 0.005  # -0.05%: inside the 0.1% budget
    assert lift_rejection(v, nudged, f) is None


def test_ceiling_clearance_is_the_material_above() -> None:
    from precis.cad.mesh_check import _Ceiling

    v, f = _cube((0, 0, 1.0), (10, 10, 1.05))
    ceil = _Ceiling(v, f, slack=1.0)
    bottom = np.flatnonzero(v[:, 2] < 1.01)
    assert ceil.above(bottom, v) == pytest.approx([0.05] * 4, abs=1e-9)


# -- slicer cantilever (Bambu Studio's floating-cantilever rule) -------------


def _manifold_mesh(solid):
    mesh = solid.to_mesh()
    return (
        np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3],
        np.asarray(mesh.tri_verts, dtype=np.int64),
    )


def _block_with_fin(rounded_foot: bool):
    """A 10 x 4 x 8 block on the bed with a 1 mm fin sticking 12 mm out of
    its y+ face. The rounded foot is the fin as a voxel field tessellates:
    0.2 mm wide at the bed, full width only at z = 0.7."""
    import manifold3d as m3d

    block = m3d.Manifold.cube([10.0, 4.0, 8.0])
    y0, y1 = 3.9, 16.0
    foot = (
        [(0.4, 0.0), (0.6, 0.0), (0.0, 0.7), (1.0, 0.7)]
        if rounded_foot
        else [
            (0.0, 0.0),
            (1.0, 0.0),
        ]
    )
    prof = [*foot, (0.0, 8.0), (1.0, 8.0)]
    fin = m3d.Manifold.hull_points([(x, y, z) for y in (y0, y1) for x, z in prof])
    return _manifold_mesh(block + fin)


def test_rounded_fin_foot_is_an_unreachable_cantilever_error() -> None:
    v, f = _block_with_fin(rounded_foot=True)
    hits = slicer_cantilevers(v, f, LAYER)
    far = [c for c in hits if c.distance > CANTILEVER_WARN_MM]
    assert far, hits
    worst = far[0]
    assert worst.distance == pytest.approx(12.0, abs=1.0)  # the fin sticks out 12 mm
    assert worst.unreachable  # layer 1: tree support cannot start under it
    assert worst.bottom_z < 0.45


def test_flat_based_fin_has_no_cantilever() -> None:
    v, f = _block_with_fin(rounded_foot=False)
    assert slicer_cantilevers(v, f, LAYER) == []


def test_mid_height_shelf_is_a_reachable_cantilever_warning() -> None:
    import manifold3d as m3d

    column = m3d.Manifold.cube([4.0, 4.0, 10.0])
    shelf = m3d.Manifold.cube([12.0, 4.0, 0.6]).translate([3.9, 0.0, 5.0])
    v, f = _manifold_mesh(column + shelf)
    hits = slicer_cantilevers(v, f, LAYER)
    far = [c for c in hits if c.distance > CANTILEVER_WARN_MM]
    assert far and not far[0].unreachable
    assert far[0].bottom_z == pytest.approx(5.0, abs=0.25)
    assert far[0].distance == pytest.approx(12.0, abs=1.0)


def test_a_slab_the_slope_allows_is_no_cantilever() -> None:
    import manifold3d as m3d

    column = m3d.Manifold.cube([4.0, 4.0, 10.0])
    # a 45-degree wedge flaring out: each layer overhangs by one layer height
    wedge = m3d.Manifold.hull_points(
        [(4, 0, 4), (4, 4, 4), (9, 0, 9), (9, 4, 9), (4, 0, 9), (4, 4, 9)]
    )
    v, f = _manifold_mesh(column + wedge)
    assert [
        c for c in slicer_cantilevers(v, f, LAYER) if c.distance > CANTILEVER_WARN_MM
    ] == []


def test_long_bridge_anchored_at_both_ends_reports_its_mid_span() -> None:
    import manifold3d as m3d

    left = m3d.Manifold.cube([3.0, 4.0, 10.0])
    right = m3d.Manifold.cube([3.0, 4.0, 10.0]).translate([23.0, 0.0, 0.0])
    beam = m3d.Manifold.cube([26.0, 4.0, 0.6]).translate([0.0, 0.0, 5.0])
    v, f = _manifold_mesh(left + right + beam)
    far = [
        c for c in slicer_cantilevers(v, f, LAYER) if c.distance > CANTILEVER_WARN_MM
    ]
    assert far, "the corners alone sit at the anchors and would hide this"
    assert far[0].distance == pytest.approx(9.7, abs=1.0)  # half the 20 mm gap
    assert not far[0].unreachable


def test_a_floating_shell_is_an_island_not_also_a_cantilever() -> None:
    v, f = _union(
        ((0.0, 0.0, 0.0), (10.0, 10.0, 3.0)),
        ((0.0, 0.0, 5.0), (10.0, 10.0, 8.0)),
    )
    assert len(floating_islands(v, f, LAYER)) == 1
    assert slicer_cantilevers(v, f, LAYER) == []


def test_shared_bed_height_judges_a_resting_part_against_the_whole_plate() -> None:
    # a box whose underside is 3 mm above ITS OWN lowest point: on its own bed
    # it is a clean block; against a plate bed 3 mm lower it floats
    v, f = _cube((0, 0, 3.0), (10, 10, 6.0))
    assert floating_islands(v, f, LAYER, bed_z=3.0) == []
    assert floating_islands(v, f, LAYER, bed_z=0.0) != []
