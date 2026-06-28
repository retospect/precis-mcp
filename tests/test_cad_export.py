"""CAD export (ADR 0041 §10): pure OpenSCAD text, manifold3d mesh
(STL/3MF), and exact OpenCASCADE STEP. The two kernel-backed routes are
optional extras, so their tests skip cleanly when the backend is absent."""

from __future__ import annotations

import math
import struct
import zipfile

import numpy as np
import pytest

from precis.cad.export import (
    ExportError,
    export_mesh,
    export_step,
    manifold_available,
    step_available,
    to_openscad,
)
from precis.cad.scene import parse_source
from precis.cad.tessellate import _signed_volume, mesh_config

_FLANGE = """
component flange
plate     add  cyl:r25h8
hub_bore  cut  cyl:r8h10    @0,0,-1
bolts     cut  cyl:r2.5h10  @18,0,-1  polar:n6r18
"""

_HAS_MANIFOLD = manifold_available()
_HAS_STEP = step_available()


# ── pure OpenSCAD text (always available) ─────────────────────────────


def test_to_openscad_structure() -> None:
    scad = to_openscad(parse_source(_FLANGE), name="flange")
    assert "$fn" in scad
    assert "difference()" in scad  # the bore + bolts are cut
    assert "multmatrix(" in scad
    assert scad.count("cylinder(") >= 1 + 1 + 6  # plate + bore + 6 bolts


def test_to_openscad_box_and_torus() -> None:
    scad = to_openscad(parse_source("b add box:w10d20h5\nt add torus:R8r2"))
    assert "cube([10,20,5])" in scad
    assert "rotate_extrude(" in scad and "circle(r=2" in scad


def test_to_openscad_assembly_unions_components() -> None:
    scad = to_openscad(
        parse_source("component a\np add cyl:r3h3\ncomponent b\nq add box:w2d2h2")
    )
    assert scad.count("// component") == 2
    assert scad.strip().startswith("//")


# ── tessellation (numpy-only; validity needs manifold3d) ──────────────


@pytest.mark.parametrize(
    "config",
    [
        "box:w10d20h5",
        "cyl:r5h10",
        "cone:r5h9",
        "tcone:rb5rt2h6",
        "sphere:r4",
        "torus:R10r2",
        "hex:r5h10",
        "ngon:n6r5h10",
        "frustum:n6rb5rt2h6",
        "pyramid:n4r5h8",
    ],
)
def test_tessellation_outward_oriented(config: str) -> None:
    verts, tris = mesh_config(config)
    assert verts.shape[1] == 3 and tris.shape[1] == 3
    # _orient_outward must have produced a positive signed volume.
    assert _signed_volume(verts, tris) > 0.0


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
@pytest.mark.parametrize(
    "config,analytic",
    [
        ("box:w10d20h5", 10 * 20 * 5),
        ("cyl:r5h10", math.pi * 25 * 10),
        ("sphere:r4", 4 / 3 * math.pi * 64),
        ("ngon:n6r5h10", 6 * 0.5 * 25 * math.sin(2 * math.pi / 6) * 10),
    ],
)
def test_tessellation_is_watertight_manifold(config: str, analytic: float) -> None:
    import manifold3d as m3d

    verts, tris = mesh_config(config)
    man = m3d.Manifold(
        m3d.Mesh(
            vert_properties=verts.astype("float32"), tri_verts=tris.astype("int32")
        )
    )
    assert str(man.status()) == "Error.NoError"
    # faceting only ever removes volume; within ~1% of the analytic value.
    assert man.volume() == pytest.approx(analytic, rel=0.02)


# ── manifold3d mesh export: STL + 3MF ─────────────────────────────────


def _read_binary_stl_bbox(data: bytes) -> tuple[int, np.ndarray, np.ndarray]:
    ntri = struct.unpack("<I", data[80:84])[0]
    pts = []
    off = 84
    for _ in range(ntri):
        # 12 floats/facet (normal + 3 verts) then a 2-byte attr = 50 bytes;
        # the 3 vertices are floats 3..11 (bytes off+12 .. off+48).
        vals = struct.unpack("<9f", data[off + 12 : off + 48])
        pts.extend([vals[0:3], vals[3:6], vals[6:9]])
        off += 50
    arr = np.array(pts)
    return ntri, arr.min(axis=0), arr.max(axis=0)


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_export_stl_real(tmp_path) -> None:
    out = export_mesh(parse_source(_FLANGE), tmp_path / "flange.stl")
    ntri, lo, hi = _read_binary_stl_bbox(out.read_bytes())
    assert ntri > 0
    # Ø50 plate, 8 mm tall — the bore + bolts are interior, so the bbox
    # is set by the plate.
    assert lo[0] == pytest.approx(-25, abs=0.5) and hi[0] == pytest.approx(25, abs=0.5)
    assert lo[2] == pytest.approx(0, abs=0.01) and hi[2] == pytest.approx(8, abs=0.01)


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_export_3mf_real(tmp_path) -> None:
    out = export_mesh(parse_source(_FLANGE), tmp_path / "flange.3mf")
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"} <= names
        model = zf.read("3D/3dmodel.model").decode()
    assert 'unit="millimeter"' in model
    assert "<vertex " in model and "<triangle " in model


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_export_mesh_cut_reduces_volume(tmp_path) -> None:
    import manifold3d as m3d

    from precis.cad.export import _solid_mesh

    v, t = _solid_mesh(parse_source(_FLANGE))
    man = m3d.Manifold(
        m3d.Mesh(vert_properties=v.astype("float32"), tri_verts=t.astype("int32"))
    )
    plate = math.pi * 25**2 * 8
    assert 0 < man.volume() < plate  # the bore + 6 bolt holes removed material


def test_export_mesh_unknown_format_raises(tmp_path) -> None:
    with pytest.raises(ExportError):
        export_mesh(parse_source("p add cyl:r3h3"), tmp_path / "x.obj")


@pytest.mark.skipif(_HAS_MANIFOLD, reason="manifold3d IS installed")
def test_export_mesh_without_backend_raises(tmp_path) -> None:
    with pytest.raises(ExportError):
        export_mesh(parse_source("p add cyl:r3h3"), tmp_path / "x.stl")


# ── exact STEP (OpenCASCADE) ──────────────────────────────────────────


@pytest.mark.skipif(not _HAS_STEP, reason="OpenCASCADE (cad-step) not installed")
def test_export_step_real(tmp_path) -> None:
    out = export_step(parse_source(_FLANGE), tmp_path / "flange.step")
    assert out.exists() and out.stat().st_size > 0
    head = out.read_text(errors="replace")[:200]
    assert "ISO-10303" in head  # STEP file header


@pytest.mark.skipif(_HAS_STEP, reason="OpenCASCADE IS installed")
def test_export_step_without_backend_raises(tmp_path) -> None:
    with pytest.raises(ExportError):
        export_step(parse_source("p add cyl:r3h3"), tmp_path / "x.step")


# ── assembly: parts kept separate where the format allows ─────────────

_ASM = """
component alpha
ba add box:w10d10h10
component beta
bb add box:w6d6h6 @20,0,0
"""


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_3mf_assembly_keeps_parts_separate(tmp_path) -> None:
    out = export_mesh(parse_source(_ASM), tmp_path / "asm.3mf")
    with zipfile.ZipFile(out) as zf:
        model = zf.read("3D/3dmodel.model").decode()
    assert model.count("<object ") == 2  # one object per component
    assert model.count("<item ") == 2  # build references both
    assert 'name="alpha"' in model and 'name="beta"' in model


@pytest.mark.skipif(not _HAS_MANIFOLD, reason="manifold3d not installed")
def test_stl_assembly_is_one_welded_body(tmp_path) -> None:
    out = export_mesh(parse_source(_ASM), tmp_path / "asm.stl")
    ntri, lo, hi = _read_binary_stl_bbox(out.read_bytes())
    assert ntri > 0
    # STL has no parts: the bbox spans both boxes (alpha at 0, beta at +20).
    assert lo[0] == pytest.approx(-5, abs=0.01) and hi[0] == pytest.approx(23, abs=0.01)


@pytest.mark.skipif(not _HAS_STEP, reason="OpenCASCADE (cad-step) not installed")
def test_step_assembly_named_solids(tmp_path) -> None:
    out = export_step(parse_source(_ASM), tmp_path / "asm.step")
    data = out.read_text(errors="replace")
    assert data.count("MANIFOLD_SOLID_BREP") == 2  # two distinct bodies
    assert "'alpha'" in data and "'beta'" in data  # carried as named solids
