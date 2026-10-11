"""``flatpack_box`` parameter validation, layers and laser settings (gr478017)."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.sheet.svg_laser import export_laser_svg
from precis_se.flatpack import flatpack_box
from precis_se.flatpack.panels import FlatpackError


def test_cut_layer_carries_cardboard_laser_settings() -> None:
    job = flatpack_box(50, 50, 50).job
    (layer,) = job.layers
    assert layer.op == "cut"
    assert layer.settings["laser"] == {"power_pct": 100, "speed_mm_s": 6, "passes": 1}
    svg = export_laser_svg(job).text
    assert 'data-speed-mm-s="6"' in svg
    assert "laser defaults" not in svg


def test_no_score_layer_because_the_box_has_no_score_geometry() -> None:
    fp = flatpack_box(50, 50, 50, shelves=[22])
    assert [layer.op for layer in fp.job.layers] == ["cut"]
    assert {s.layer for s in fp.job.shapes} == {"cut"}


def test_laser_override_merges_over_material_defaults() -> None:
    fp = flatpack_box(50, 50, 50, laser={"cut": {"speed_mm_s": 9}})
    assert fp.job.layers[0].settings["laser"] == {
        "power_pct": 100,
        "speed_mm_s": 9,
        "passes": 1,
    }


def test_cnc_material_has_no_laser_settings() -> None:
    fp = flatpack_box(80, 80, 80, material="plywood-6mm", cutter_d=3)
    assert fp.job.layers[0].settings == {}


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"fit": 10}, "fit must be at least 0 and below the sheet thickness"),
        ({"fit": 3}, "fit must be"),
        ({"kerf": -1}, "kerf must be at least 0"),
        ({"kerf": 3.5}, "kerf must be"),
        ({"fit": float("nan")}, "finite number"),
        ({"shelves": 22}, "shelves must be a list"),
        ({"shelves": "22"}, "shelves must be a list"),
        ({"shelves": ["a"]}, "shelves entry must be a finite number"),
        ({"shelf_load_n": -5}, "shelf_load_n cannot be negative"),
        ({"laser": {"burn": {"power_pct": 1}}}, "laser op must be one of"),
        ({"laser": {"cut": {"watts": 1}}}, "laser setting must be one of"),
        ({"laser": {"cut": {"power_pct": 0}}}, "must be positive"),
    ],
)
def test_bad_parameters_are_refused_by_name(kwargs: dict, match: str) -> None:
    with pytest.raises(FlatpackError, match=match) as info:
        flatpack_box(50, 50, 50, **kwargs)
    assert isinstance(info.value, BadInput) and isinstance(info.value, ValueError)


def test_valid_edge_values_still_pass() -> None:
    flatpack_box(50, 50, 50, fit=0.2, kerf=0.15, shelves=(22,), shelf_load_n=5)
