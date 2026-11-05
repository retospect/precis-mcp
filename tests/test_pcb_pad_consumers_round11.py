"""Pad consumers retain authored outlines, hole centres, and bottom poses."""

from __future__ import annotations

import math
from typing import Any

import pytest
from shapely.geometry import LineString, Polygon

from precis.pcb import DEFAULT_STACKUP, drc, silk
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import from_graph


def test_polygon_round_hole_ring_uses_nearest_authored_edge():
    # The sloping edges have y = +/- (0.3 - 0.2*x). At x=-0.55,
    # perpendicular distance to either is 0.41/sqrt(1.04), not h/2;
    # the left edge is farther away at 0.45 mm.
    pad = {
        "shape": "polygon",
        "x": -0.55,
        "y": 0.0,
        "w": 2.0,
        "h": 1.0,
        "poly": [(-1.0, -0.5), (1.0, -0.1), (1.0, 0.1), (-1.0, 0.5)],
        "drill": 0.6,
    }
    cap = capability_for("4layer")
    floor = cap.jlc_min["annular_ring_mm"]
    assert floor is not None
    expected_ring = 0.41 / math.sqrt(1.04) - 0.3
    (finding,) = drc.check_annular_ring({"pads": [pad]}, cap)
    assert finding.severity == "error"
    assert finding.margin_mm == pytest.approx(expected_ring - floor, abs=1e-10)


@pytest.mark.parametrize("shape", ["rect", "circle", "polygon"])
def test_offset_round_hole_ring_uses_drill_centre(shape):
    pad = {
        "shape": shape,
        "x": 0.0,
        "y": 0.0,
        "w": 2.0,
        "h": 2.0,
        "drill_x": 0.6,
        "drill_y": 0.0,
        "drill": 0.6,
    }
    if shape == "polygon":
        pad["poly"] = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]
    cap = capability_for("4layer")
    floor = cap.jlc_min["annular_ring_mm"]
    assert floor is not None
    (finding,) = drc.check_annular_ring({"pads": [pad]}, cap)
    assert finding.severity == "error"
    assert finding.margin_mm == pytest.approx(0.1 - floor, abs=1e-10)


def test_offset_round_hole_in_obround_has_analytic_ring_at_oblique_pose():
    angle = math.radians(37.0)
    pad = {
        "shape": "obround",
        "x": 0.0,
        "y": 0.0,
        "w": 2.0,
        "h": 1.0,
        "oblique_rot": 37.0,
        "drill": 0.2,
        "drill_x": 0.3 * math.sin(angle),
        "drill_y": 0.3 * math.cos(angle),
    }
    cap = capability_for("4layer")
    floor = cap.jlc_min["annular_ring_mm"]
    assert floor is not None
    (finding,) = drc.check_annular_ring({"pads": [pad]}, cap)
    # Offset perpendicular to the centre segment: r - offset - drill/2.
    assert finding.margin_mm == pytest.approx(0.5 - 0.3 - 0.1 - floor, abs=1e-10)


def test_round_hole_outside_polygon_has_negative_ring():
    pad = {
        "shape": "polygon",
        "x": 0.0,
        "y": 0.0,
        "w": 2.0,
        "h": 2.0,
        "drill": 0.2,
        "drill_x": 1.2,
        "drill_y": 0.0,
        "poly": [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)],
    }
    cap = capability_for("4layer")
    floor = cap.jlc_min["annular_ring_mm"]
    assert floor is not None
    (finding,) = drc.check_annular_ring({"pads": [pad]}, cap)
    assert finding.margin_mm == pytest.approx(-0.2 - 0.1 - floor)


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_silk_stroke_and_broad_phase_use_placed_pad_outline(indexed, custom):
    if custom:
        pad: dict[str, Any] = {
            "shape": "polygon",
            "x": 0.0,
            "y": 0.0,
            "w": 2.0,
            "h": 0.5,
            "poly": [(-0.25, -1.0), (0.25, -1.0), (0.1, 1.0), (-0.1, 1.0)],
        }
        stroke = [(-0.04, 0.8), (0.04, 0.8)]
        outline = Polygon(pad["poly"])
    else:
        pad = {
            "shape": "rect",
            "x": 0.0,
            "y": 0.0,
            "w": 2.0,
            "h": 0.5,
            "oblique_rot": 45.0,
        }
        stroke = [(0.55, -0.6), (0.65, -0.6)]
        c = math.sqrt(0.5)
        outline = Polygon(
            [
                (c * (x + y), c * (y - x))
                for x, y in [(-1.0, -0.25), (1.0, -0.25), (1.0, 0.25), (-1.0, 0.25)]
            ]
        )
    assert outline.intersects(LineString(stroke).buffer(0.05))
    obstacles = silk._SideObstacles([pad]) if indexed else [pad]
    near = silk._near_obstacles(obstacles, stroke, 0.05)
    assert near == [pad]
    assert silk._stroke_overlaps_any_pad(stroke, near, 0.1)
    assert silk._circle_overlaps_pad(stroke[0], 0.05, pad)


def test_silk_polygon_mask_opening_grows_vertices():
    pad = {
        "shape": "polygon",
        "x": 0.0,
        "y": 0.0,
        "w": 2.0,
        "h": 0.5,
        "poly": [(-0.25, -1.0), (0.25, -1.0), (0.25, 1.0), (-0.25, 1.0)],
    }
    opening = silk._mask_opening(pad, 0.1)
    # A 0.1 mm mitred swell gives [-0.35,0.35] x [-1.1,1.1].
    expected = Polygon([(-0.35, -1.1), (0.35, -1.1), (0.35, 1.1), (-0.35, 1.1)])
    actual = silk._obstacle_geom(opening)
    assert actual.hausdorff_distance(expected) < 1e-10
    assert silk._stroke_overlaps_any_pad([(0.32, 0.7), (0.32, 0.9)], [opening], 0.02)


def test_silk_keeps_stroke_in_custom_pad_concavity():
    pad = {
        "shape": "polygon",
        "x": 1.0,
        "y": 1.0,
        "w": 2.0,
        "h": 2.0,
        "poly": [
            (0.0, 0.0),
            (2.0, 0.0),
            (2.0, 0.5),
            (0.5, 0.5),
            (0.5, 2.0),
            (0.0, 2.0),
        ],
    }
    stroke = [(1.0, 1.0), (1.5, 1.0)]
    assert Polygon(pad["poly"]).distance(LineString(stroke)) == pytest.approx(0.5)
    assert not silk._stroke_overlaps_any_pad(stroke, [pad], 0.1)


def test_silk_obround_does_not_obstruct_empty_bounding_box_corner():
    pad = {"shape": "obround", "x": 0.0, "y": 0.0, "w": 2.0, "h": 0.5}
    stroke = [(0.94, 0.21), (0.96, 0.21)]
    # Nearest cap centre is (0.75, 0): even the nearest stroked point
    # remains outside the radius .25 end cap.
    assert math.hypot(0.94 - 0.75, 0.21) > 0.25 + 0.01
    assert not silk._stroke_overlaps_any_pad(stroke, [pad], 0.02)


@pytest.mark.parametrize("angle", [0.0, 37.0, 90.0])
def test_silk_obround_cap_predicates_do_not_clear_between_tessellation_vertices(angle):
    pad = {
        "shape": "obround",
        "x": 0.0,
        "y": 0.0,
        "w": 2.0,
        "h": 0.5,
        "oblique_rot": angle,
    }
    # Halfway between the 64-per-quadrant contour's cap vertices, 1nm
    # INSIDE the real circular aperture. Its tessellated chord is 19nm
    # inside the arc here, so an inscribed polygon misses this overlap.
    theta = math.pi / 256.0
    r = 0.25 - 0.000001
    px, py = 0.75 + r * math.cos(theta), r * math.sin(theta)
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    centre = (c * px + s * py, -s * px + c * py)
    cap = (0.75 * c, -0.75 * s)
    assert math.dist(centre, cap) < 0.25
    stroke = [(centre[0] - 1e-7, centre[1]), (centre[0] + 1e-7, centre[1])]
    assert silk._stroke_overlaps_any_pad(stroke, [pad], 1e-7)
    assert silk._circle_overlaps_pad(centre, 1e-7, pad)


@pytest.mark.parametrize("angle", [0.0, 90.0, 23.7])
def test_furniture_courtyard_mirrors_asymmetric_bottom_geometry(angle):
    ir = from_graph(
        {
            "instances": [
                {"refdes": "U1", "x": 15.0, "y": 9.0, "rot": angle, "layer": "bottom"}
            ],
            "nets": [{"name": "N", "members": [{"refdes": "U1", "pin": "1"}]}],
        },
        stackup=DEFAULT_STACKUP,
    )
    ir.set_pin_offset(0, 6.0, 0.0)
    ir.pin_w[0] = 2.0
    ir.pin_h[0] = 1.0
    (ring,) = silk.world_courtyard_rings(ir)
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    # Footprint-local rectangle x=[5,7], y=[-.5,.5], mirrored BEFORE CW rotation.
    expected = Polygon(
        [
            (15.0 - c * x + s * y, 9.0 + s * x + c * y)
            for x, y in [(5.0, -0.5), (7.0, -0.5), (7.0, 0.5), (5.0, 0.5)]
        ]
    )
    assert Polygon(ring).hausdorff_distance(expected) < 1e-10
