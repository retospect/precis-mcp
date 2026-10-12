"""Print scale: the checked scaled mesh, the factor formatter, the suggested
factor and the parameter vocabulary shared by the route and the handler.
DB-free; the route cases live in ``test_web_se_print.py``."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from precis.cad.export import format_scale_factor, manifold_available
from precis.cad.mesh_check import package_findings
from precis.cad.scene import parse_source
from precis.cad.vec import as_vec3
from precis_se import printing as se_printing
from precis_se import printscale
from precis_se.printsolid import scale_mismatch_finding

_DOWN = as_vec3([0.0, 0.0, -1.0])
_LAYER_M = 0.0002
_CUBE_1MM = "component part\ncube add box:w1mmd1mmh1mm\n"
_CUBE_5NM = "component part\ncube add box:w5nmd5nmh5nm\n"
_NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}


def _printed(source: str) -> Any:
    return cast(Any, SimpleNamespace(spec=parse_source(source)))


def _extent(built: se_printing.PrintMesh) -> np.ndarray:
    verts = np.vstack([v for _n, v, _t in built.parts])
    return np.ptp(verts, axis=0)


def _model_vertices(path: Path) -> tuple[ET.Element, np.ndarray]:
    with zipfile.ZipFile(path) as zf:
        model = ET.fromstring(zf.read("3D/3dmodel.model"))
    verts = np.array(
        [[float(v.attrib[a]) for a in "xyz"] for v in model.findall(".//m:vertex", _NS)]
    )
    return model, verts


@pytest.mark.parametrize(
    ("scale", "text"),
    [
        (1.0, "1"),
        (100.0, "100"),
        (2e7, "2e7"),
        (1e7, "1e7"),
        (5e7, "5e7"),
        (12e6, "12e6"),
        (1000.0, "1000"),
        (0.5, "0.5"),
    ],
)
def test_format_scale_factor(scale: float, text: str) -> None:
    assert format_scale_factor(scale) == text
    assert float(text) == scale


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_parse_scale_defaults_to_one(raw: Any) -> None:
    assert printscale.parse_scale(raw) == 1.0


@pytest.mark.parametrize("raw", ["abc", "nan", "inf", "-inf", "0", "-3", "1e999"])
def test_parse_scale_rejects_bad_input(raw: str) -> None:
    with pytest.raises(printscale.PrintParamError) as err:
        printscale.parse_scale(raw)
    assert err.value.code == "invalid_scale"


def test_parse_scale_accepts_exponent_form() -> None:
    assert printscale.parse_scale("2e7") == 2e7


@pytest.mark.parametrize(
    ("size_mm", "floor", "expected"),
    [
        ([5e-6, 5e-6, 5e-6], None, 2e7),  # 5 nm -> 100 mm / 5e-6 = 2e7
        ([3e-6, 1e-6, 1e-6], None, 2e7),  # 3.33e7 rounds DOWN to 2e7
        ([7e-6, 1e-6, 1e-6], None, 1e7),  # 1.43e7 -> 1e7
        ([0.4, 0.4, 0.4], None, 200.0),  # 250 -> 200
        ([0.9, 0.1, 0.1], None, 100.0),  # 111 -> 100
        ([1.0, 1.0, 1.0], None, 1.0),  # >= 1 mm prints 1:1
        ([40.0, 20.0, 10.0], None, 1.0),
        ([256.0, 1.0, 1.0], None, 1.0),  # at the bed: still 1:1
        ([500.0, 1.0, 1.0], None, 0.2),  # beyond the bed: fit 100 mm
        ([5e-6, 5e-6, 5e-6], 5e7, 5e7),  # the floor wins over the fit
        ([5e-6, 5e-6, 5e-6], 3.5e7, 3.5e7),  # ...even if not 1-2-5
        ([5e-6, 5e-6, 5e-6], 1e6, 2e7),  # floor below the fit does nothing
        ([0.0, 0.0, 0.0], None, None),
        ([], None, None),
    ],
)
def test_suggest_scale(
    size_mm: list[float], floor: float | None, expected: float | None
) -> None:
    got = printscale.suggest_scale(size_mm, floor)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, rel=1e-12)


def test_scaled_filename() -> None:
    assert printscale.scaled_filename("d", "b", 1.0, "3mf") == "d-b-print.3mf"
    assert printscale.scaled_filename("d", "b", 100.0, "3mf") == "d-b-x100.3mf"
    assert printscale.scaled_filename("d", "b", 2e7, "stl") == "d-b-x2e7.stl"


def test_scale_mismatch_is_judged_at_printed_size() -> None:
    assert scale_mismatch_finding("b", 5e-9) is not None  # 5 nm part
    assert scale_mismatch_finding("b", 5e-9, scale=2e7) is None  # 100 mm print


def test_min_feature_is_judged_at_printed_size() -> None:
    """A 0.2 mm wall is under a 0.8 mm min_wall at 1:1 and fine at x10."""
    printed = _printed("component part\nwall add box:w0.2mmd10mmh10mm\n")
    rules = {"min_wall": 0.0008}
    assert se_printing._min_feature_finding("b", printed, rules) is not None
    assert se_printing._min_feature_finding("b", printed, rules, scale=10.0) is None


@pytest.mark.skipif(not manifold_available(), reason="manifold3d not installed")
class TestScaledMesh:
    def test_cube_at_scale_100_is_a_100_mm_cube(self, tmp_path: Path) -> None:
        printed = _printed(_CUBE_1MM)
        built = se_printing.build_print_mesh(
            printed, _DOWN, pitch=_LAYER_M, scale=100.0
        )
        np.testing.assert_allclose(_extent(built), [100.0] * 3, rtol=1e-9)
        assert min(v[:, 2].min() for _n, v, _t in built.parts) == pytest.approx(0.0)
        out = se_printing.write_mesh(
            printed,
            _DOWN,
            "3mf",
            tmp_path / "c.3mf",
            pitch=_LAYER_M,
            built=built,
            title="cube",
            scale=100.0,
        )
        assert package_findings(out.read_bytes()) == []
        model, verts = _model_vertices(out)
        np.testing.assert_allclose(np.ptp(verts, axis=0), [100.0] * 3, rtol=1e-9)
        assert model.attrib["unit"] == "millimeter"
        meta = {m.attrib["name"]: m.text for m in model.findall("m:metadata", _NS)}
        assert meta["precis:scale_factor"] == "100"
        assert meta["Title"] == "cube"

    def test_write_mesh_rebuild_path_scales_once(self, tmp_path: Path) -> None:
        out = se_printing.write_mesh(
            _printed(_CUBE_1MM),
            _DOWN,
            "3mf",
            tmp_path / "c.3mf",
            pitch=_LAYER_M,
            scale=100.0,
        )
        _model, verts = _model_vertices(out)
        np.testing.assert_allclose(np.ptp(verts, axis=0), [100.0] * 3, rtol=1e-9)

    def test_scale_one_has_no_scale_metadata(self, tmp_path: Path) -> None:
        out = se_printing.write_mesh(
            _printed(_CUBE_1MM), _DOWN, "3mf", tmp_path / "c.3mf", pitch=_LAYER_M
        )
        with zipfile.ZipFile(out) as zf:
            assert b"scale_factor" not in zf.read("3D/3dmodel.model")

    def test_nanometre_block_prints_100_mm_at_2e7(self) -> None:
        built = se_printing.build_print_mesh(
            _printed(_CUBE_5NM), _DOWN, pitch=_LAYER_M, scale=2e7
        )
        np.testing.assert_allclose(_extent(built), [100.0] * 3, rtol=1e-6)

    def test_stl_is_scaled_too(self, tmp_path: Path) -> None:
        out = se_printing.write_mesh(
            _printed(_CUBE_1MM),
            _DOWN,
            "stl",
            tmp_path / "c.stl",
            pitch=_LAYER_M,
            scale=100.0,
        )
        raw = out.read_bytes()
        ntri = int.from_bytes(raw[80:84], "little")
        rec = np.frombuffer(raw[84:], dtype=np.dtype([("f", "<f4", 12), ("a", "<u2")]))
        assert len(rec) == ntri
        pts = rec["f"][:, 3:].reshape(-1, 3)
        np.testing.assert_allclose(np.ptp(pts, axis=0), [100.0] * 3, rtol=1e-5)

    def test_cleanup_tolerance_stays_physical(self) -> None:
        """Layer height is a printer fact: the tail-lift tolerance is the same
        half layer at any scale; the geometry grows by exactly the factor."""
        printed = _printed(_CUBE_1MM)
        small = se_printing.build_print_mesh(printed, _DOWN, pitch=_LAYER_M)
        big = se_printing.build_print_mesh(printed, _DOWN, pitch=_LAYER_M, scale=10.0)
        assert small.tol_mm == big.tol_mm == pytest.approx(0.1)
        np.testing.assert_allclose(_extent(big), _extent(small) * 10.0, rtol=1e-9)

    def test_source_pitch_follows_the_geometry(self) -> None:
        printed = _printed(_CUBE_1MM)
        base = se_printing.build_print_mesh(
            printed, _DOWN, pitch=_LAYER_M, source_pitch=0.0004
        )
        big = se_printing.build_print_mesh(
            printed, _DOWN, pitch=_LAYER_M, source_pitch=0.0004, scale=10.0
        )
        assert base.tol_mm == pytest.approx(0.2)  # 0.4 mm field pitch / 2
        assert big.tol_mm == pytest.approx(2.0)  # the field pitch grew x10

    @pytest.mark.parametrize("scale", [0.0, -1.0, math.nan, math.inf])
    def test_invalid_scale_is_refused(self, scale: float) -> None:
        with pytest.raises(ValueError, match="scale"):
            se_printing.build_print_mesh(_printed(_CUBE_1MM), _DOWN, scale=scale)
