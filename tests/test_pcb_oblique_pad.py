"""A rect/obround pad on an obliquely-rotated instance is its true rotated
outline in the model (docs/backlog/pcb-oblique-rotated-pad-is-an-axis-
aligned-rect-in-the-model.md), not the unrotated w x h box.

Every assertion below fails if ``pads_for_ir`` emits the axis-aligned rect:
at 45 degrees the unrotated rect covers a gap the real pad leaves open and
misses the real pad's corner.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from shapely.geometry import Point

from precis.pcb import DEFAULT_STACKUP, maze, realize
from precis.pcb import drc as pcb_drc
from precis.pcb import session as pcb_session
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import from_graph, pin_point
from precis.workers.job_types.pcb_route import _fixed_copper_collisions

_ROT = 45.0
_W, _H = 4.0, 1.0


def _ir_and_footprints(shape: str = "RECT") -> tuple[Any, dict[str, Any]]:
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0, "rot": _ROT},
            {"refdes": "U1", "x": 30.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "A",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U0", "pin": "1"}, {"refdes": "U1", "pin": "1"}],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    footprints = {
        "U0": {
            "pads": [
                {"number": "1", "x": 0.0, "y": 0.0, "w": _W, "h": _H, "shape": shape}
            ],
            "pin_map": {"1": {"name": "1"}},
        }
    }
    return ir, footprints


def _u0_pad(ir: Any, footprints: dict[str, Any]) -> dict[str, Any]:
    layers = [str(layer.get("name")) for layer in ir.stackup]
    pads = realize.pads_for_ir(ir, layers, footprints)
    return next(p for p in pads if p["refdes"] == "U0")


def _via(x: float, y: float) -> dict[str, Any]:
    return {
        "ctype": "via",
        "net": "B",
        "x": x,
        "y": y,
        "dia_mm": 0.4,
        "drill_mm": 0.2,
        "layers": ["F.Cu"],
        "fixed": True,
    }


def _collisions(ir: Any, footprints: dict[str, Any], via: dict[str, Any]) -> list[str]:
    return _fixed_copper_collisions(ir, footprints, [via], capability_for("4layer"))


def test_oblique_rect_pad_is_the_rotated_corners():
    ir, footprints = _ir_and_footprints()
    pad = _u0_pad(ir, footprints)
    assert pad["shape"] == "polygon"
    # Clockwise-from-north rotation (padplace._rotate_cw), computed here
    # independently of the production helper.
    c, s = math.cos(math.radians(_ROT)), math.sin(math.radians(_ROT))
    expected = {
        (round(x * c + y * s, 3), round(-x * s + y * c, 3))
        for x in (-_W / 2, _W / 2)
        for y in (-_H / 2, _H / 2)
    }
    got = {(round(x, 3), round(y, 3)) for x, y in pad["poly"]}
    assert got == expected
    # Not the unrotated rect's corners.
    assert (2.0, 0.5) not in got
    # w/h are the ring's bbox (the polygon pad's informational extent).
    bbox = (_W + _H) * math.sqrt(0.5)  # 45 degrees: both extents are equal
    assert pad["w"] == pytest.approx(bbox, abs=1e-3)
    assert pad["h"] == pytest.approx(bbox, abs=1e-3)


def test_axis_aligned_pad_is_untouched():
    graph = {
        "instances": [{"refdes": "U0", "x": 0.0, "y": 0.0, "rot": 90.0}],
        "nets": [],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    footprints = {
        "U0": {
            "pads": [
                {"number": "1", "x": 0.0, "y": 0.0, "w": _W, "h": _H, "shape": "RECT"}
            ],
            "pin_map": {"1": {"name": "1"}},
        }
    }
    layers = [str(layer.get("name")) for layer in ir.stackup]
    pad = next(
        p for p in realize.pads_for_ir(ir, layers, footprints) if p["refdes"] == "U0"
    )
    assert pad["shape"] == "rect"
    assert (pad["w"], pad["h"]) == (_H, _W)


def test_copper_in_the_gap_the_unrotated_rect_wrongly_covers_passes():
    ir, footprints = _ir_and_footprints()
    # Inside the unrotated rect (|x| <= 2, |y| <= 0.5), 1.34 mm off the
    # rotated pad's axis -> ~0.64 mm clear of the true land.
    assert _collisions(ir, footprints, _via(1.9, 0.0)) == []


def test_copper_on_the_true_corner_the_unrotated_rect_misses_fails():
    ir, footprints = _ir_and_footprints()
    c, s = math.cos(math.radians(_ROT)), math.sin(math.radians(_ROT))
    cx, cy = _W / 2 * c + _H / 2 * s, -_W / 2 * s + _H / 2 * c
    assert abs(cy) > _H / 2  # outside the unrotated rect
    assert _collisions(ir, footprints, _via(cx, cy)) != []


def test_oblique_obround_is_a_rotated_stadium_polygon():
    ir, footprints = _ir_and_footprints("OVAL")
    pad = _u0_pad(ir, footprints)
    assert pad["shape"] == "polygon"
    ring = pad["poly"]
    # Every vertex lies on the stadium: within r of the long-axis segment,
    # and the extreme vertices reach the cap tips (2.0 from centre).
    c, s = math.cos(math.radians(_ROT)), math.sin(math.radians(_ROT))
    r = _H / 2
    half = _W / 2 - r
    ax, ay = half * c, -half * s  # long-axis end, board frame
    for x, y in ring:
        along = max(-1.0, min(1.0, (x * ax + y * ay) / (ax * ax + ay * ay)))
        d = math.hypot(x - along * ax, y - along * ay)
        assert d == pytest.approx(r, abs=1e-3)
    assert max(math.hypot(x, y) for x, y in ring) == pytest.approx(_W / 2, abs=1e-3)


def test_router_claim_contains_the_model_pad():
    ir, footprints = _ir_and_footprints()
    pad = _u0_pad(ir, footprints)
    geoms = realize.pad_geometry(ir, footprints)
    pid = next(
        p
        for p in range(ir.n_pins)
        if str(ir.instance_refdes[int(ir.pin_instance[p])]) == "U0"
    )
    point = pin_point(ir, pid)
    assert point is not None
    claim = realize._pad_shape(geoms[pid], point, float(ir.inst_rot[0]))
    assert isinstance(claim, maze.PadShape)
    assert claim.kind == "circle"
    radius = claim.w_mm / 2.0
    assert radius == pytest.approx(math.hypot(_W, _H) / 2.0)
    for x, y in pad["poly"]:
        # The ring is rounded to 0.1 um; the corner sits ON the circle.
        assert math.hypot(x - point[0], y - point[1]) <= radius + 1e-3


def test_drc_polygon_branch_measures_the_ring():
    ir, footprints = _ir_and_footprints()
    pad = _u0_pad(ir, footprints)
    geom = pcb_drc._copper_item_polygon({**pad, "ctype": "pad"})
    assert geom is not None
    assert geom.area == pytest.approx(_W * _H, rel=1e-3)
    assert not geom.contains(Point(1.9, 0.0))  # the unrotated rect's gap
    assert geom.contains(Point(1.0, -1.0))  # on the true long axis


def test_findings_wrapper_agrees():
    ir, footprints = _ir_and_footprints()
    cap = capability_for("4layer")
    assert (
        pcb_session.fixed_copper_findings(ir, footprints, [_via(1.9, 0.0)], cap) == []
    )


def test_annular_ring_judges_the_true_land_not_the_rotated_bbox() -> None:
    """The polygon pad's w/h are its rotated bbox (~2.5 mm at 45° for a
    4x1 land); the ring rule must keep reading the 1 mm narrow side, or an
    oblique drilled pad passes a ring it does not have."""
    ir, footprints = _ir_and_footprints()
    pad = _u0_pad(ir, footprints)
    assert pad["land_min_mm"] == pytest.approx(1.0)
    assert min(pad["w"], pad["h"]) > 2.0
    drilled = {**pad, "drill": 0.9}  # true ring 0.05 mm; bbox ring > 0.5 mm
    model: dict[str, Any] = {
        "layers": ["F.Cu", "B.Cu"],
        "copper": [],
        "pads": [drilled],
    }
    errors = [
        f
        for f in pcb_drc.check_annular_ring(model, capability_for("4layer"))
        if f.severity == "error"
    ]
    assert errors, "the 0.05 mm ring on the real land must fail"
