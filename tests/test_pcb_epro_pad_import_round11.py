"""Independent pad-origin and hole-offset regressions for Pro import."""

import math

import pytest

from precis.pcb import epro, padplace, rows


def test_polygon_reader_supplies_dimensions_without_moving_origin() -> None:
    pad, warnings = epro._pad_to_precis(
        {
            "num": "1",
            "centerX": 0,
            "centerY": 0,
            "padAngle": 0,
            "hole": {"holeType": "ROUND", "width": 20, "height": 20},
            "defaultPad": {
                "padType": "POLYGON",
                "path": ["M", -10, -20, "L", 50, -20, "L", 50, 20, "L", -10, 5, "Z"],
            },
        },
        "test polygon",
    )
    assert not warnings
    assert pad is not None
    assert (pad["x"], pad["y"]) == (0.0, 0.0)
    assert (pad["w"], pad["h"]) == (1.524, 1.016)
    placed, drills = padplace.place_footprint_pads(
        [pad],
        {"x": 5, "y": 7, "rot": 90, "layer": "bottom"},
        layers=["F.Cu", "B.Cu"],
    )
    assert (drills[0]["x"], drills[0]["y"]) == (5.0, 7.0)
    assert (placed[0]["w"], placed[0]["h"]) == (1.016, 1.524)


def test_pro_reader_preserves_hole_offset_in_pad_frame() -> None:
    pad, warnings = epro._pad_to_precis(
        {
            "num": "1",
            "centerX": 100,
            "centerY": 200,
            "padAngle": 37,
            "hole": {"holeType": "ROUND", "width": 20, "height": 20},
            "padOffsetX": 10,
            "padOffsetY": -5,
            "defaultPad": {"padType": "RECT", "width": 80, "height": 60},
        },
        "test offset",
    )
    assert not warnings
    assert pad is not None
    assert (pad["x"], pad["y"]) == (2.54, -5.08)
    assert pad["drill_offset"] == {"x": 0.254, "y": 0.127}


def test_authored_polygon_hole_stays_fixed_when_imported_and_placed() -> None:
    """The original bottom-90 asymmetric-pad repro keeps its hole at (5, 5)."""
    pad = rows.normalize_pad(
        {
            "pin": "1",
            "shape": "polygon",
            "x": 0,
            "y": 0,
            "poly": [[-0.3, -0.5], [1.3, -0.5], [1.3, 0.5], [-0.3, 0.2]],
            "drill": 0.6,
        }
    )
    placed, drills = padplace.place_footprint_pads(
        [pad],
        {"x": 5, "y": 5, "rot": 90, "layer": "bottom"},
        layers=["F.Cu", "B.Cu"],
    )
    assert (drills[0]["x"], drills[0]["y"]) == (5.0, 5.0)
    expected = [[4.5, 4.7], [4.5, 6.3], [5.5, 6.3], [5.2, 4.7]]
    assert placed[0]["poly"] == expected


@pytest.mark.parametrize("bottom", [False, True])
@pytest.mark.parametrize("angle", [0.0, 90.0, 23.7])
def test_imported_pro_offset_drill_follows_independent_transform(bottom, angle) -> None:
    pad, warnings = epro._pad_to_precis(
        {
            "num": "1",
            "centerX": 0,
            "centerY": 0,
            "padAngle": 37,
            "hole": {"holeType": "ROUND", "width": 20, "height": 20},
            "padOffsetX": 10,
            "padOffsetY": -5,
            "defaultPad": {"padType": "RECT", "width": 80, "height": 60},
        },
        "test offset",
    )
    assert pad is not None and not warnings
    placed, drills = padplace.place_footprint_pads(
        [rows.normalize_pad(pad)],
        {"x": 5, "y": 7, "rot": angle, "layer": "bottom" if bottom else "top"},
        layers=["F.Cu", "B.Cu"],
    )
    # Clockwise pad rotation, then X reflection, then clockwise instance
    # rotation. This expected point does not call any production transform.
    theta = math.radians(37)
    x = 0.254 * math.cos(theta) + 0.127 * math.sin(theta)
    y = -0.254 * math.sin(theta) + 0.127 * math.cos(theta)
    x = -x if bottom else x
    theta = math.radians(angle)
    expected = (
        5 + x * math.cos(theta) + y * math.sin(theta),
        7 - x * math.sin(theta) + y * math.cos(theta),
    )
    assert (drills[0]["x"], drills[0]["y"]) == pytest.approx(expected, abs=0.00005)
    assert (placed[0]["x"], placed[0]["y"]) == (5.0, 7.0)
