"""Nesting and sheet-job emission of :mod:`precis_se.flatpack`, and the
laser SVG / DXF of the 50 mm cube — the generator's end of build 2 of
docs/backlog/flatpack-furniture-generator.md.

``tests/fixtures/flatpack/cube50_laser.svg`` is the golden laser SVG of
``flatpack_box(50, 50, 50)``. Regenerate it, after checking the diff is the
one you meant, with::

    uv run python -c "from precis_se.flatpack import flatpack_box;
    from precis.sheet.svg_laser import export_laser_svg;
    print(export_laser_svg(flatpack_box(50, 50, 50).job).text, end='')" \\
        > tests/fixtures/flatpack/cube50_laser.svg
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from itertools import combinations
from pathlib import Path

import pytest
from shapely.geometry import Polygon, box

from precis.sheet import Polyline
from precis.sheet.dxf import export_dxf
from precis.sheet.svg_laser import export_laser_svg
from precis_se.flatpack import NestingError, flatpack_box
from precis_se.flatpack.generator import placed_polygon
from precis_se.flatpack.nest import nest

GOLDEN = Path(__file__).parent / "fixtures" / "flatpack" / "cube50_laser.svg"
SVG_NS = "{http://www.w3.org/2000/svg}"


def _placed(fp) -> dict[str, Polygon]:
    return {
        pl.part: placed_polygon(fp.cut_polygons[pl.part], pl.x, pl.y, pl.rotated)
        for pl in fp.nesting.placements
    }


def _assert_well_nested(fp) -> None:
    w, h = fp.nesting.sheet
    m = fp.nesting.margin
    usable = box(m, m, w - m, h - m)
    polys = _placed(fp)
    assert set(polys) == {p.id for p in fp.panels}
    for poly in polys.values():
        assert usable.buffer(1e-9).contains(poly), "a part crosses the sheet margin"
    for a, b in combinations(polys.values(), 2):
        assert a.intersection(b).area == 0
        assert a.distance(b) >= m - 1e-9


def test_small_sheet_is_refused_naming_the_shortfall() -> None:
    with pytest.raises(NestingError) as info:
        flatpack_box(50, 50, 50, sheet=(60, 60))
    err = info.value
    assert (err.placed, err.shortfall_mm2) == (1, 10_000)
    assert "short by 10000 mm²" in str(err) and "fits 1 part(s)" in str(err)


def test_given_sheet_fits_without_overlap() -> None:
    fp = flatpack_box(50, 50, 50, sheet=(160, 110))
    assert fp.nesting.sheet == (160, 110) and fp.nesting.sheet_given
    _assert_well_nested(fp)
    assert fp.job.sheet.width_mm == 160


def test_no_sheet_derives_one_from_the_strip() -> None:
    fp = flatpack_box(50, 50, 50, shelves=[22])
    assert not fp.nesting.sheet_given
    w, h = fp.nesting.sheet
    # six 50-ish parts, 1 mm margin and spacing: no worse than one tight column
    assert w * h <= (50 + 2) * (5 * 51 + 47 + 2) + 1e-6
    _assert_well_nested(fp)
    assert 0.8 < fp.utilisation < 1.0


def test_kerf_widens_bounding_boxes_and_spacing() -> None:
    fp = flatpack_box(50, 50, 50, kerf=1.4, cutter_d=0)
    assert fp.nesting.margin == 1.4
    assert all(
        (pl.w, pl.h) == pytest.approx((51.4, 51.4)) for pl in fp.nesting.placements
    )
    _assert_well_nested(fp)


def test_packer_rotates_when_only_the_turned_part_fits() -> None:
    nesting = nest([("bar", 80, 10)], spacing=1, sheet=(30, 100))
    (pl,) = nesting.placements
    assert pl.rotated and (pl.w, pl.h) == (10, 80) and (pl.x, pl.y) == (1, 1)


def test_packer_shortfall_is_the_unplaced_bbox_area() -> None:
    with pytest.raises(NestingError) as info:
        nest([("a", 20, 20), ("b", 20, 20), ("c", 5, 4)], spacing=1, sheet=(22, 22))
    assert info.value.shortfall_mm2 == 420 and info.value.unplaced == ["b", "c"]


def test_sheet_job_has_one_cut_layer_slots_before_outlines() -> None:
    fp = flatpack_box(50, 50, 50, shelves=[22])
    job = fp.job
    assert [(layer.name, layer.op) for layer in job.layers] == [("cut", "cut")]
    assert [p.id for p in job.parts] == [pl.part for pl in fp.nesting.placements]
    for part in job.parts:
        assert part.thickness_mm == 3.0 and part.pose is not None
        assert set(part.pose) == {"origin", "u", "v", "n", "rotated_on_sheet"}
    side = [s for s in job.shapes_on("cut") if s.part == "side-l"]
    assert [s.inner for s in side] == [True, True, False]
    assert all(isinstance(s, Polyline) and s.closed for s in side)
    assert any(n.startswith("fit 0 mm (default") for n in job.notes)


def test_cube_laser_svg_matches_golden() -> None:
    fp = flatpack_box(50, 50, 50)
    text = export_laser_svg(fp.job).text
    assert text == GOLDEN.read_text(encoding="utf-8"), (
        "the 50 mm cube's laser SVG changed; if intended, regenerate the "
        "fixture (recipe in this module's docstring)"
    )
    root = ET.fromstring(text)
    (group,) = root.findall(f"{SVG_NS}g")
    assert group.get("id") == "cut" and group.get("stroke") == "#000000"
    paths = group.findall(f"{SVG_NS}path")
    assert sorted(p.get("data-part", "") for p in paths) == [
        "back",
        "bottom",
        "side-l",
        "side-r",
        "top",
    ]
    assert all(p.get("data-role") == "outer" for p in paths)


def test_cube_dxf_has_the_cut_layer_and_five_closed_polylines() -> None:
    text = export_dxf(flatpack_box(50, 50, 50).job).text
    lines = text.splitlines()
    pairs = [(int(lines[i]), lines[i + 1]) for i in range(0, len(lines), 2)]
    assert pairs.count((0, "POLYLINE")) == 5
    assert pairs.count((8, "cut")) >= 5
    assert (70, "1") in pairs and pairs[-1] == (0, "EOF")
