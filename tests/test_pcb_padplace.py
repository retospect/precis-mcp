"""Instance-placed pad geometry (precis.pcb.padplace) — the transform that
turns a footprint's local pads into board-coordinate gerber pads. See the
module docstring for the mirror -> rotate -> translate convention this
pins down.
"""

from __future__ import annotations

import pytest

from precis.pcb import padplace

_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def _pad(**kw: object) -> dict:
    base = {
        "number": "1",
        "shape": "RECT",
        "x": 1.0,
        "y": 0.0,
        "w": 0.6,
        "h": 0.3,
        "rot": 0.0,
        "layer": "F.Cu",
        "drill": None,
    }
    base.update(kw)
    return base


# ── the position transform ──────────────────────────────────────────────
def test_top_side_no_rotation_is_pure_translation():
    inst = {"x": 10.0, "y": 20.0, "rot": 0.0, "layer": "top"}
    bx, by = padplace.place_pad_point(_pad(x=1.0, y=2.0), inst)
    assert bx == pytest.approx(11.0)
    assert by == pytest.approx(22.0)


def test_rotation_is_clockwise_from_north():
    # A pad 1mm to the local "east" (+X, rot=0 orientation), rotated 90°
    # CW, ends up 1mm "south" (-Y) of the instance origin -- the same
    # CW-from-north convention precis.pcb.export.jlc_rotation documents.
    inst = {"x": 0.0, "y": 0.0, "rot": 90.0, "layer": "top"}
    bx, by = padplace.place_pad_point(_pad(x=1.0, y=0.0), inst)
    assert bx == pytest.approx(0.0, abs=1e-9)
    assert by == pytest.approx(-1.0)


def test_rotation_180_negates_both_axes():
    inst = {"x": 0.0, "y": 0.0, "rot": 180.0, "layer": "top"}
    bx, by = padplace.place_pad_point(_pad(x=1.0, y=2.0), inst)
    assert bx == pytest.approx(-1.0)
    assert by == pytest.approx(-2.0)


def test_rotation_270_is_the_mirror_of_90():
    inst = {"x": 0.0, "y": 0.0, "rot": 270.0, "layer": "top"}
    bx, by = padplace.place_pad_point(_pad(x=1.0, y=0.0), inst)
    assert bx == pytest.approx(0.0, abs=1e-9)
    assert by == pytest.approx(1.0)


def test_bottom_side_mirrors_x_before_rotating():
    # rot=0, bottom: local east (+X) flips to local west (-X) -- the part
    # is flipped face-down, top edge (local +Y) stays "up".
    inst = {"x": 5.0, "y": 5.0, "rot": 0.0, "layer": "bottom"}
    bx, by = padplace.place_pad_point(_pad(x=1.0, y=2.0), inst)
    assert bx == pytest.approx(4.0)  # 5 - 1
    assert by == pytest.approx(7.0)  # 5 + 2


def test_bottom_side_mirror_then_rotate_order_matters():
    # Mirror-then-rotate (this module's documented order) vs
    # rotate-then-mirror diverge for any non-0/180 rotation -- this is
    # the exact case a swapped order would silently get wrong.
    inst = {"x": 0.0, "y": 0.0, "rot": 90.0, "layer": "bottom"}
    bx, by = padplace.place_pad_point(_pad(x=1.0, y=0.0), inst)
    # mirror first: (1,0) -> (-1,0); rotate 90 CW: (x*cos+y*sin, -x*sin+y*cos)
    # = (-1*0+0*1, -(-1)*1+0*0) = (0, 1)
    assert bx == pytest.approx(0.0, abs=1e-9)
    assert by == pytest.approx(1.0)
    # the WRONG order (rotate first, mirror second) would give (0, -1) --
    # assert we did not get that.
    assert by != pytest.approx(-1.0)


def test_layer_string_variants_all_read_as_bottom():
    for variant in ("bottom", "Bottom", "bot", "B", "b"):
        inst = {"x": 0.0, "y": 0.0, "rot": 0.0, "layer": variant}
        bx, _ = padplace.place_pad_point(_pad(x=1.0, y=0.0), inst)
        assert bx == pytest.approx(-1.0), variant


# ── shape / size / layer bookkeeping ─────────────────────────────────────
def test_smd_pad_lands_on_the_instances_effective_layer():
    inst = {"x": 0.0, "y": 0.0, "rot": 0.0, "layer": "top"}
    pads, drills = padplace.place_footprint_pads(
        [_pad(layer="F.Cu")], inst, layers=_LAYERS
    )
    assert not drills
    assert len(pads) == 1
    assert pads[0]["layer"] == "F.Cu"


def test_bottom_instance_flips_the_pads_own_layer():
    inst = {"x": 0.0, "y": 0.0, "rot": 0.0, "layer": "bottom"}
    pads, _ = padplace.place_footprint_pads([_pad(layer="F.Cu")], inst, layers=_LAYERS)
    assert pads[0]["layer"] == "B.Cu"


def test_through_hole_pad_flashes_on_every_copper_layer_and_drills():
    inst = {"x": 0.0, "y": 0.0, "rot": 0.0, "layer": "top"}
    pads, drills = padplace.place_footprint_pads(
        [_pad(shape="ELLIPSE", drill=0.3)], inst, layers=_LAYERS
    )
    assert {p["layer"] for p in pads} == set(_LAYERS)
    assert len(pads) == len(_LAYERS)
    assert drills == [{"x": 1.0, "y": 0.0, "dia_mm": 0.3, "plated": True}]


def test_rect_pad_shape_maps_to_gerber_vocabulary():
    inst = {"x": 0.0, "y": 0.0, "rot": 0.0, "layer": "top"}
    pads, _ = padplace.place_footprint_pads(
        [
            _pad(shape="RECT"),
            _pad(shape="ELLIPSE", number="2"),
            _pad(shape="OVAL", number="3"),
        ],
        inst,
        layers=_LAYERS,
    )
    by_shape = [p["shape"] for p in pads]
    assert by_shape == ["rect", "circle", "obround"]


def test_90_degree_effective_rotation_swaps_width_and_height():
    inst = {"x": 0.0, "y": 0.0, "rot": 90.0, "layer": "top"}
    pads, _ = padplace.place_footprint_pads(
        [_pad(shape="RECT", w=0.6, h=0.3)], inst, layers=_LAYERS
    )
    assert pads[0]["w"] == pytest.approx(0.3)
    assert pads[0]["h"] == pytest.approx(0.6)


def test_0_degree_rotation_keeps_width_and_height():
    inst = {"x": 0.0, "y": 0.0, "rot": 0.0, "layer": "top"}
    pads, _ = padplace.place_footprint_pads(
        [_pad(shape="RECT", w=0.6, h=0.3)], inst, layers=_LAYERS
    )
    assert pads[0]["w"] == pytest.approx(0.6)
    assert pads[0]["h"] == pytest.approx(0.3)


def test_oblique_rotation_keeps_unrotated_wh_not_a_crash():
    inst = {"x": 0.0, "y": 0.0, "rot": 45.0, "layer": "top"}
    pads, _ = padplace.place_footprint_pads(
        [_pad(shape="RECT", w=0.6, h=0.3)], inst, layers=_LAYERS
    )
    assert pads[0]["w"] == pytest.approx(0.6)
    assert pads[0]["h"] == pytest.approx(0.3)


def test_circle_pad_has_no_h_key_and_is_unaffected_by_rotation():
    inst = {"x": 0.0, "y": 0.0, "rot": 90.0, "layer": "top"}
    pads, _ = padplace.place_footprint_pads(
        [_pad(shape="ELLIPSE", w=0.5, h=0.5)], inst, layers=_LAYERS
    )
    assert "h" not in pads[0]
    assert pads[0]["w"] == pytest.approx(0.5)


def test_pin_to_net_resolves_via_pin_map():
    inst = {"x": 0.0, "y": 0.0, "rot": 0.0, "layer": "top"}
    pin_map = {"1": {"name": "VDD", "tags": ["power"]}}
    pads, _ = padplace.place_footprint_pads(
        [_pad(number="1")],
        inst,
        layers=_LAYERS,
        pin_map=pin_map,
        pin_to_net={"VDD": "3V3"},
    )
    assert pads[0]["net"] == "3V3"


def test_missing_pin_to_net_data_defaults_to_empty_string_not_a_crash():
    inst = {"x": 0.0, "y": 0.0, "rot": 0.0, "layer": "top"}
    pads, _ = padplace.place_footprint_pads([_pad()], inst, layers=_LAYERS)
    assert pads[0]["net"] == ""


# ── board_pads: the whole-design assembly ─────────────────────────────────
def test_board_pads_skips_unplaced_instances():
    instances = [{"refdes": "U1", "x": None, "y": None, "part_lcsc": "C1"}]
    footprints = {"C1": {"pads": [_pad()], "pin_map": {}}}
    pads, drills = padplace.board_pads(instances, footprints, layers=_LAYERS)
    assert pads == [] and drills == []


def test_board_pads_skips_instances_with_no_cached_footprint():
    instances = [
        {
            "refdes": "U1",
            "x": 0.0,
            "y": 0.0,
            "rot": 0.0,
            "layer": "top",
            "part_lcsc": "C999",
        }
    ]
    pads, drills = padplace.board_pads(instances, {}, layers=_LAYERS)
    assert pads == [] and drills == []


def test_board_pads_places_two_instances_independently():
    instances = [
        {
            "refdes": "U1",
            "x": 0.0,
            "y": 0.0,
            "rot": 0.0,
            "layer": "top",
            "part_lcsc": "C1",
        },
        {
            "refdes": "U2",
            "x": 10.0,
            "y": 0.0,
            "rot": 0.0,
            "layer": "top",
            "part_lcsc": "C1",
        },
    ]
    footprints = {"C1": {"pads": [_pad(x=0.0, y=0.0)], "pin_map": {}}}
    pads, _ = padplace.board_pads(instances, footprints, layers=_LAYERS)
    xs = sorted(p["x"] for p in pads)
    assert xs == [0.0, 10.0]


def test_board_pads_wires_pin_to_net_per_refdes():
    instances = [
        {
            "refdes": "U1",
            "x": 0.0,
            "y": 0.0,
            "rot": 0.0,
            "layer": "top",
            "part_lcsc": "C1",
        },
    ]
    footprints = {
        "C1": {
            "pads": [_pad(number="1")],
            "pin_map": {"1": {"name": "VDD", "tags": []}},
        }
    }
    pads, _ = padplace.board_pads(
        instances,
        footprints,
        layers=_LAYERS,
        pin_to_net={("U1", "VDD"): "3V3"},
    )
    assert pads[0]["net"] == "3V3"
