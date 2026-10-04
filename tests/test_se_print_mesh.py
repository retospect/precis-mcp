"""``precis_se.printing``'s shipped-mesh checks, DB-free: the mesh the
report judges is the mesh :func:`write_mesh` writes, and a floating region
on it is an ``error`` finding (the SIMP voxel rule and the mesh overhang
rule both miss a marching-cubes tail; Bambu's SharpTail rule does not)."""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from precis.cad.export import manifold_available
from precis.cad.mesh_check import (
    Island,
    LiftResult,
    package_findings,
    weld_and_drop_degenerate,
)
from precis.cad.scene import parse_source
from precis.cad.vec import as_vec3
from precis_se import printing as se_printing

pytestmark = pytest.mark.skipif(
    not manifold_available(), reason="manifold3d not installed"
)

#: Column on the bed, an arm across its top, a pendant hanging off the arm's
#: far end: the pendant's flat underside (z = 5 mm) has nothing below it.
_PENDANT = """
component part
col     add box:w2mmd2mmh10mm   @0mm,0mm,0mm
arm     add box:w12mmd2mmh2mm   @5mm,0mm,8mm
pendant add box:w2mmd2mmh3mm    @10mm,0mm,5mm
"""
_BLOCK = "component part\ncube add box:w10mmd10mmh10mm\n"
_DOWN = as_vec3([0.0, 0.0, -1.0])
_LAYER_M = 0.0002  # the house fdm layer height, metres


def _printed(source: str) -> Any:
    return cast(Any, SimpleNamespace(spec=parse_source(source)))


def test_floating_pendant_is_an_error_finding() -> None:
    built = se_printing.build_print_mesh(_printed(_PENDANT), _DOWN, pitch=_LAYER_M)
    found = [
        f
        for f in se_printing._mesh_findings("part", built, _LAYER_M)
        if f.rule == "floating_island"
    ]
    assert len(found) == 1
    assert found[0].severity == "error"
    assert "z = 5.10 mm" in found[0].detail


def test_box_on_the_bed_has_no_mesh_findings() -> None:
    built = se_printing.build_print_mesh(_printed(_BLOCK), _DOWN, pitch=_LAYER_M)
    assert se_printing._mesh_findings("part", built, _LAYER_M) == []


def test_listed_islands_are_capped_and_the_total_stated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = se_printing.build_print_mesh(_printed(_BLOCK), _DOWN, pitch=_LAYER_M)
    fake = [Island(z=1.0 + i, x=0.0, y=0.0, area=1.0) for i in range(13)]
    monkeypatch.setattr(se_printing, "floating_islands", lambda *a, **k: fake)
    out = se_printing._mesh_findings("part", built, _LAYER_M)
    assert len(out) == se_printing.MAX_LISTED_ISLANDS + 1
    assert "island 1/13" in out[0].detail
    assert "3 further" in out[-1].detail and "13 in total" in out[-1].detail


def test_cleanup_info_line_counts_what_was_done() -> None:
    built = se_printing.PrintMesh(parts=[], n_dropped=3, n_slivers=2, n_lifted=1)
    (info,) = se_printing._mesh_findings("part", built, None)
    assert info.rule == "mesh_cleanup" and info.severity == "info"
    assert "3 degenerate" in info.detail and "1 sub-layer tail" in info.detail


def test_write_mesh_ships_the_mesh_the_report_judged(tmp_path: Path) -> None:
    printed = _printed(_PENDANT)
    built = se_printing.build_print_mesh(printed, _DOWN, pitch=_LAYER_M)
    out = se_printing.write_mesh(
        printed,
        _DOWN,
        "3mf",
        tmp_path / "p.3mf",
        pitch=_LAYER_M,
        built=built,
        title="pendant-part",
    )
    assert package_findings(out.read_bytes()) == []
    with zipfile.ZipFile(out) as zf:
        model = zf.read("3D/3dmodel.model").decode("utf-8")
    assert '<metadata name="Title">pendant-part</metadata>' in model
    # the passed mesh is shipped as-is: rebuilding gives the same vertices
    again = se_printing.build_print_mesh(printed, _DOWN, pitch=_LAYER_M)
    for (_n1, v1, t1), (_n2, v2, t2) in zip(built.parts, again.parts, strict=True):
        assert np.array_equal(v1, v2) and np.array_equal(t1, t2)
    # STL of a single-part mesh reuses it too
    stl = se_printing.write_mesh(
        printed, _DOWN, "stl", tmp_path / "p.stl", pitch=_LAYER_M, built=built
    )
    ntri = int.from_bytes(stl.read_bytes()[80:84], "little")
    assert ntri == len(built.parts[0][2])


def _dimple_mesh():
    from tests.test_cad_mesh_check import _ledge_with_plateau_dimple

    return _ledge_with_plateau_dimple()


def test_tail_tolerance_follows_the_source_field_pitch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v, f = _dimple_mesh()
    monkeypatch.setattr(
        se_printing, "_component_meshes", lambda *a, **k: [("part", v, f)]
    )
    printed = _printed(_BLOCK)
    # field-derived (solved at 1 mm): tol = max(0.1, 0.5) = 0.5 -> dimple gone
    field = se_printing.build_print_mesh(
        printed, _DOWN, pitch=_LAYER_M, source_pitch=0.001
    )
    assert field.tol_mm == pytest.approx(0.5)
    assert field.n_lifted == 2
    assert not [
        i
        for i in se_printing._mesh_findings("part", field, _LAYER_M)
        if i.rule == "floating_island"
    ]
    # B-rep (no source pitch): tol = layer/2 = 0.1 -> the island is reported
    brep = se_printing.build_print_mesh(printed, _DOWN, pitch=_LAYER_M)
    assert brep.tol_mm == pytest.approx(0.1)
    assert brep.n_lifted == 0
    errors = [
        i
        for i in se_printing._mesh_findings("part", brep, _LAYER_M)
        if i.rule == "floating_island"
    ]
    assert len(errors) == 1 and errors[0].severity == "error"


def test_source_field_pitch_reads_the_stored_header() -> None:
    class _Store:
        def field_header(self, ref: str) -> dict[str, Any] | None:
            if ref == "boom":
                raise RuntimeError("database is down")
            if ref == "nopitch":
                return {"shape": [1, 1, 1]}
            return None if ref == "gone" else {"pitch_m": 0.001}

    def printed_with(config: str) -> Any:
        node = SimpleNamespace(config=config)
        return cast(Any, SimpleNamespace(spec=SimpleNamespace(nodes=[node])))

    store = cast(Any, _Store())
    for config, want in (
        ("field:abc", (True, 0.001)),
        ("field:gone", (True, None)),  # not stored / ambiguous
        ("field:nopitch", (True, None)),  # header without a pitch
        ("box:w1h1d1", (False, None)),
    ):
        assert se_printing.source_field_pitch(printed_with(config), store) == want
    with pytest.raises(RuntimeError, match="database is down"):
        se_printing.source_field_pitch(printed_with("field:boom"), store)


def test_a_lift_that_fails_the_backstop_ships_the_welded_mesh_unlifted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v, f = _dimple_mesh()
    monkeypatch.setattr(
        se_printing, "_component_meshes", lambda *a, **k: [("part", v, f)]
    )

    def squash(verts, faces, **_kw):
        out = np.array(verts, copy=True)
        out[out[:, 2] > 6.0, 2] = 6.0  # drag the whole top down
        return LiftResult(out, n_lifted=3)

    monkeypatch.setattr(se_printing, "lift_sharp_tails_checked", squash)
    built = se_printing.build_print_mesh(
        _printed(_BLOCK), _DOWN, pitch=_LAYER_M, source_pitch=0.001
    )
    welded, _f, _n = weld_and_drop_degenerate(v, f)
    assert np.array_equal(built.parts[0][1], welded)  # shipped unlifted
    assert built.n_lifted == 0
    assert len(built.lift_abandoned) == 1 and "volume" in built.lift_abandoned[0]
    info = [
        i
        for i in se_printing._mesh_findings("part", built, _LAYER_M)
        if i.rule == "mesh_cleanup" and "abandoned" in i.detail
    ]
    assert len(info) == 1 and info[0].severity == "info"


def test_stl_of_a_multi_part_block_judges_the_welded_union() -> None:
    two = "component a\nba add box:w10mmd10mmh10mm\ncomponent b\nbb add box:w6mmd6mmh6mm @20mm,0mm,0mm\n"
    printed = _printed(two)
    parts = se_printing.build_print_mesh(printed, _DOWN, pitch=_LAYER_M)
    assert len(parts.parts) == 2
    report = se_printing.BlockPrintReport(
        block="x",
        mode="fdm/pla",
        printed=printed,
        candidates=[],
        chosen_down=_DOWN,
        chosen_score=None,
        pinned=False,
        best_other=None,
        pitch=_LAYER_M,
        print_mesh=parts,
    )
    built, judged = se_printing.mesh_for_export(report, "stl")
    assert built is not None and len(built.parts) == 1 and built is not parts
    assert judged is not None  # the union was judged, not the components
    same, none = se_printing.mesh_for_export(report, "3mf")
    assert same is parts and none is None


# -- flat bed contact (field space) ------------------------------------------

_SHA = "ab" * 32
_PITCH_M = 0.001  # the 1 mm voxel the SIMP solve ran at
_DOWN_NEG_Y = as_vec3([0.0, -1.0, 0.0])


def _field_printed(rho: np.ndarray) -> Any:
    """A printed solid whose only node is the stored field built from the
    density grid (metres, 1 mm voxels)."""
    from precis.cad.fieldops import from_density
    from precis.cad.scene import NodeSpec, SceneSpec

    fld = from_density(rho, 0.5, pitch=_PITCH_M, origin=(0.0, 0.0, 0.0))
    spec = SceneSpec(
        nodes=[NodeSpec(name="f", op="add", config=f"field:{_SHA}", component="part")],
        components=["part"],
        field_loader={_SHA: fld}.__getitem__,
    )
    return cast(Any, SimpleNamespace(spec=spec))


def _fin_density() -> np.ndarray:
    """A 1-voxel fin standing on the build-down (-y) end: voxel x=5,
    y 0..7, z 0..9."""
    rho = np.zeros((11, 8, 10))
    rho[5, :, :] = 1.0
    return rho


def _width_at(built: Any, z: float, length: float = 0.0) -> float:
    """The fin's thickness (mm) at height ``z``: slice area over the slice
    area high up, times the fin's true 1 mm (so its rounded ends cancel)."""
    from precis.cad.mesh_check import _manifold_of

    (_n, v, t) = built.parts[0]
    m = _manifold_of(v, t)
    return float(m.slice(z).area()) / float(m.slice(2.0).area())


def test_flat_bed_makes_a_one_voxel_fin_stand_square_on_the_bed() -> None:
    from precis.cad.mesh_check import signed_volume

    printed = _field_printed(_fin_density())
    kw: dict[str, Any] = {"pitch": _LAYER_M, "source_pitch": _PITCH_M}
    before = se_printing.build_print_mesh(
        printed, _DOWN_NEG_Y, flat_bed_contact=False, **kw
    )
    after = se_printing.build_print_mesh(printed, _DOWN_NEG_Y, **kw)
    z0 = 0.5 * 0.2  # half the first layer
    assert _width_at(before, z0, 10.0) < 0.42  # the slicer drops this layer
    assert _width_at(after, z0, 10.0) >= 0.9
    assert after.flat_bed is not None and "-y" in after.flat_bed
    (_n, v1, t1), (_n2, v0, t0) = after.parts[0], before.parts[0]
    assert v1[:, 2].min() == pytest.approx(0.0, abs=1e-6)  # still on the bed
    grew = signed_volume(v1, t1) - signed_volume(v0, t0)
    assert 0.0 < grew <= 1.0 * 10.0 * 1.0 / 2 + 1e-6  # footprint x pitch/2


def test_flat_bed_works_on_the_high_side_too() -> None:
    printed = _field_printed(_fin_density())
    up = as_vec3([0.0, 1.0, 0.0])
    built = se_printing.build_print_mesh(
        printed, up, pitch=_LAYER_M, source_pitch=_PITCH_M
    )
    assert built.flat_bed is not None and "+y" in built.flat_bed
    assert _width_at(built, 0.1, 10.0) >= 0.9


def test_flat_bed_is_skipped_with_a_reason_when_down_is_oblique() -> None:
    printed = _field_printed(_fin_density())
    oblique = as_vec3([0.0, -0.8, -0.6])
    built = se_printing.build_print_mesh(
        printed, oblique, pitch=_LAYER_M, source_pitch=_PITCH_M
    )
    assert built.flat_bed is None
    assert built.flat_bed_skipped and "axis-aligned" in built.flat_bed_skipped
    notes = [
        i.detail
        for i in se_printing._mesh_findings("part", built, _LAYER_M)
        if i.rule == "mesh_cleanup"
    ]
    assert any("flat bed contact was not applied" in n for n in notes)


def test_flat_bed_ignores_a_non_field_block() -> None:
    built = se_printing.build_print_mesh(_printed(_BLOCK), _DOWN, pitch=_LAYER_M)
    assert built.flat_bed is None and built.flat_bed_skipped is None


def test_slicer_cantilever_findings_error_on_the_foot_warn_on_a_shelf() -> None:
    import manifold3d as m3d

    from tests.test_cad_mesh_check import _block_with_fin, _manifold_mesh

    def findings(v: np.ndarray, f: np.ndarray) -> list[Any]:
        built = se_printing.PrintMesh(parts=[("p", v, f)])
        return [
            i
            for i in se_printing._mesh_findings("part", built, _LAYER_M)
            if i.rule == "slicer_cantilever"
        ]

    rounded = findings(*_block_with_fin(rounded_foot=True))
    assert [i.severity for i in rounded] == ["error"]
    assert "cannot reach this layer" in rounded[0].detail
    assert findings(*_block_with_fin(rounded_foot=False)) == []

    column = m3d.Manifold.cube([4.0, 4.0, 10.0])
    shelf = m3d.Manifold.cube([12.0, 4.0, 0.6]).translate([3.9, 0.0, 5.0])
    warned = findings(*_manifold_mesh(column + shelf))
    assert [i.severity for i in warned] == ["warn"]


def test_flat_bed_removes_the_cantilever_a_voxel_fin_foot_causes() -> None:
    # a 1-voxel fin 12 voxels long sticking out of a wide base, on the bed
    rho = np.zeros((16, 8, 22))
    rho[0:16, :, 0:4] = 1.0  # base block
    rho[7, :, 4:22] = 1.0  # the fin (x = 7), 18 voxels out of the base
    printed = _field_printed(rho)
    kw: dict[str, Any] = {"pitch": _LAYER_M, "source_pitch": _PITCH_M}

    def cantilevers(flat: bool) -> list[Any]:
        built = se_printing.build_print_mesh(
            printed, _DOWN_NEG_Y, flat_bed_contact=flat, **kw
        )
        return [
            i
            for i in se_printing._mesh_findings("part", built, _LAYER_M)
            if i.rule == "slicer_cantilever"
        ]

    assert [i.severity for i in cantilevers(False)] == ["error"]
    assert cantilevers(True) == []


def test_a_failing_cantilever_step_keeps_the_island_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = se_printing.build_print_mesh(_printed(_BLOCK), _DOWN, pitch=_LAYER_M)
    fake = [Island(z=1.0, x=0.0, y=0.0, area=1.0)]
    monkeypatch.setattr(se_printing, "floating_islands", lambda *a, **k: fake)

    def boom(*a: Any, **k: Any) -> list[Any]:
        raise RuntimeError("geos exploded")

    monkeypatch.setattr(se_printing, "slicer_cantilevers", boom)
    out = se_printing._mesh_findings("part", built, _LAYER_M)
    assert [i.rule for i in out if i.severity == "error"] == ["floating_island"]
    skipped = [i for i in out if i.rule == "slicer_cantilever"]
    assert len(skipped) == 1 and skipped[0].severity == "info"
    assert "slicer-cantilever check skipped: geos exploded" in skipped[0].detail


def test_shipped_mesh_rules_name_every_mesh_judged_rule() -> None:
    assert set(se_printing.SHIPPED_MESH_RULES) == {
        "floating_island",
        "slicer_cantilever",
        "mesh_cleanup",
    }
