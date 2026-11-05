"""Exact copper contact for plane pours, including pad and drill voids."""

from __future__ import annotations

import dataclasses
import math
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Point, Polygon, box

from precis.pcb.ir import FootprintPad, footprint_pad_extent, from_graph
from precis.pcb.realize import RealizedTrack, RealizedVia
from precis.pcb.topoplace import proxy, realise
from precis.pcb.topoplace.design import design_from_ir
from tests.test_pcb_topoplace_walls import _board


def _joined(poly: Any, *, pads=None, tracks=(), vias=(), fixed=(), layer=1) -> bool:
    names = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    return realise._pour_piece_joined(
        poly, layer, names[layer], "N0", 0, tracks, vias, pads or {}, fixed, names
    )


@pytest.mark.parametrize("quarter,bottom", [(0, 0), (1, 0), (3, 1)])
def test_obround_corner_is_not_copper(quarter: int, bottom: int) -> None:
    """The bbox corner is 0.06066 mm from a 4x2 stadium's real copper."""
    d = _board(
        [
            {
                "x": 10.0,
                "y": 10.0,
                "net": 0,
                "w": 4.0,
                "h": 2.0,
                "shape": "obround",
                "side": 2,
            }
        ],
        n_nets=1,
    )
    pose = np.asarray([[10.0, 10.0, quarter, bottom]], dtype=float)
    ir = SimpleNamespace(net_name=("N0",))
    pads = realise._pad_bodies(d, ir, 4, pose)
    corner = Polygon(
        proxy.local_to_world(np.asarray(box(1.75, 0.75, 4, 2).exterior.coords), pose[0])
    )
    assert not _joined(corner, pads=pads)
    touching = Polygon(
        proxy.local_to_world(np.asarray(box(1.5, 0.25, 4, 1).exterior.coords), pose[0])
    )
    assert _joined(touching, pads=pads)
    # An enclosing obstacle must still contain the stadium's rounded ends.
    obstacle = realise._pad_polys(d, ir, 4, pose)[1][0][1]
    tip = proxy.local_to_world(
        np.asarray([[1.0 + math.sqrt(0.5), math.sqrt(0.5)]]), pose[0]
    )[0]
    assert obstacle.covers(Point(tip))


@pytest.mark.parametrize("shape", ["rect", "obround"])
@pytest.mark.parametrize("bottom", [0, 1])
def test_raw_oblique_pad_uses_its_copper_instead_of_extent_box(
    shape: str, bottom: int
) -> None:
    raw = {"x": 0.0, "y": 0.0, "w": 4.0, "h": 1.0, "rot": 45.0, "shape": shape}
    ext = footprint_pad_extent(raw)
    assert ext is not None
    d = _board(
        [
            {
                "x": 10.0,
                "y": 10.0,
                "net": 0,
                "w": ext[2],
                "h": ext[3],
                "shape": shape,
                "side": 2,
            }
        ],
        n_nets=1,
    )
    pose = np.asarray([[10.0, 10.0, 1.0, bottom]])
    ir = SimpleNamespace(
        net_name=("N0",),
        footprint_pads=(
            FootprintPad(
                instance=0, number="1", label="1", pin=0, primary=True, raw=raw
            ),
        ),
    )
    pads = realise._pad_bodies(d, ir, 4, pose)
    corner = Polygon(
        proxy.local_to_world(
            np.asarray(box(1.4, 1.4, 1.6, 1.6).exterior.coords), pose[0]
        )
    )
    assert not _joined(corner, pads=pads)
    obstacle = realise._pad_polys(d, ir, 4, pose)[1][0][1]
    assert obstacle.covers(corner)
    actual = Polygon(
        proxy.local_to_world(
            np.asarray(box(0.8, -1.0, 1.0, -0.8).exterior.coords), pose[0]
        )
    )
    assert _joined(actual, pads=pads)


def test_raw_extra_oblique_pad_and_authored_polygon_preserve_shape() -> None:
    raw = {"x": 0.0, "y": 5.0, "w": 4.0, "h": 1.0, "rot": 45.0, "shape": "obround"}
    ext = footprint_pad_extent(raw)
    assert ext is not None
    d = _board(
        [{"x": 10.0, "y": 10.0, "net": 0, "w": 0.4, "h": 0.4}],
        n_nets=1,
        extras=[
            {
                "part": 0,
                "dx": 0.0,
                "dy": 5.0,
                "w": ext[2],
                "h": ext[3],
                "pin": 0,
                "side": 2,
            }
        ],
    )
    d = dataclasses.replace(d, pad_extra_shape=np.asarray(["obround"]))
    ir = SimpleNamespace(
        net_name=("N0",),
        footprint_pads=(
            FootprintPad(
                instance=0, number="2", label="1", pin=0, primary=False, raw=raw
            ),
        ),
    )
    pads = realise._pad_bodies(d, ir, 4, d.pose0)
    assert not _joined(box(11.4, 16.4, 11.6, 16.6), pads=pads)
    assert _joined(box(10.8, 14.0, 11.0, 14.2), pads=pads)
    # Custom and rounded outlines are authored polygon rings. They keep
    # their clipped corner and ignore a raw rot already baked into poly.
    raw = {
        **raw,
        "shape": "polygon",
        "rot": 70.0,
        "poly": [[-2, 4.5], [1.5, 4.5], [2, 5], [1.5, 5.5], [-2, 5.5]],
    }
    raw.pop("w")
    raw.pop("h")
    ir.footprint_pads = (dataclasses.replace(ir.footprint_pads[0], raw=raw),)
    pads = realise._pad_bodies(d, ir, 4, d.pose0)
    assert not _joined(box(11.9, 15.4, 12.1, 15.6), pads=pads)
    assert _joined(box(11.8, 14.95, 12.1, 15.05), pads=pads)


@pytest.mark.parametrize("shape", ["polygon", "roundrect", "custom"])
def test_unresolved_pad_shape_blocks_routes_without_certifying_pour(shape: str) -> None:
    d = _board(
        [
            {
                "x": 10.0,
                "y": 10.0,
                "net": 0,
                "w": 4.0,
                "h": 2.0,
                "shape": shape,
                "side": 2,
            }
        ],
        n_nets=1,
    )
    ir = SimpleNamespace(net_name=("N0",))
    pads = realise._pad_bodies(d, ir, 4, d.pose0)
    assert not _joined(box(9.9, 9.9, 10.1, 10.1), pads=pads)
    obstacle = realise._pad_polys(d, ir, 4, d.pose0)[1][0][1]
    assert obstacle.covers(Point(10.0, 10.0))


def test_design_snapshot_keeps_exact_raw_pads_without_ir_hydration() -> None:
    ir = from_graph(
        {
            "instances": [
                {"refdes": "P1", "pins": [{"name": "1"}], "x": 10.0, "y": 10.0}
            ],
            "nets": [{"name": "N0", "members": [{"refdes": "P1", "pin": "1"}]}],
        }
    )
    raw = {
        "number": "1",
        "x": 0.0,
        "y": 0.0,
        "w": 4.0,
        "h": 1.0,
        "rot": 45.0,
        "shape": "obround",
        "drill": 0.2,
    }
    footprints = {
        "P1": {
            "pads": [raw, {**raw, "number": "2", "y": 5.0}],
            "pin_map": {"1": {"name": "1"}, "2": {"name": "1"}},
        }
    }
    assert not ir.footprint_pads
    d = design_from_ir(ir, footprints)
    assert not ir.footprint_pads
    pads = realise._pad_bodies(d, ir, 4, d.pose0)
    assert not _joined(box(11.4, 11.4, 11.6, 11.6), pads=pads)
    assert _joined(box(10.8, 9.0, 11.0, 9.2), pads=pads)
    assert not _joined(box(11.4, 16.4, 11.6, 16.6), pads=pads)
    assert _joined(box(10.8, 14.0, 11.0, 14.2), pads=pads)


@pytest.mark.parametrize("slot", [False, True])
def test_pad_drill_void_cannot_certify_a_pour_join(slot: bool) -> None:
    raw = {"x": 0.0, "y": 0.0, "w": 4.0, "h": 2.0, "shape": "obround", "drill": 0.4}
    if slot:
        raw["slot"] = {"w": 1.0, "h": 0.4}
        raw["rot"] = 45.0
    d = _board(
        [
            {
                "x": 10.0,
                "y": 10.0,
                "net": 0,
                "w": 4.0,
                "h": 2.0,
                "shape": "obround",
                "side": 2,
            }
        ],
        n_nets=1,
    )
    ir = SimpleNamespace(
        net_name=("N0",),
        footprint_pads=(
            FootprintPad(
                instance=0, number="1", label="1", pin=0, primary=True, raw=raw
            ),
        ),
    )
    pads = realise._pad_bodies(d, ir, 4, d.pose0)
    assert not _joined(box(9.98, 9.98, 10.02, 10.02), pads=pads)
    assert _joined(box(9.8, 10.3, 10.2, 10.4), pads=pads)
    obstacle = realise._pad_polys(d, ir, 4, d.pose0)[1][0][1]
    assert obstacle.covers(Point(10.0, 10.0))


@pytest.mark.parametrize("shape", ["crossing", "width", "arc"])
@pytest.mark.parametrize("authored", [False, True])
def test_pour_joins_complete_track_copper(shape: str, authored: bool) -> None:
    if shape == "arc":
        segments = (
            {
                "shape": "arc",
                "start": [10.0, 0.0],
                "end": [0.0, 10.0],
                "center": [0.0, 0.0],
                "cw": False,
            },
        )
        poly = box(7.04, 7.04, 7.1, 7.1)
    else:
        segments = ({"shape": "line", "start": [0.0, 0.0], "end": [10.0, 0.0]},)
        poly = (
            box(4.0, -0.5, 6.0, 0.5)
            if shape == "crossing"
            else box(4.0, 0.05, 6.0, 0.07)
        )
    track = RealizedTrack(
        seg_id=0,
        net_id=0,
        layer=1,
        segments=segments,
        length_mm=10.0,
        blocked_by=None,
        width_mm=0.15,
    )
    fixed = (
        {
            "ctype": "track",
            "net": "N0",
            "layer": "In1.Cu",
            "segments": segments,
            "width_mm": 0.15,
        },
    )
    assert _joined(poly, fixed=fixed) if authored else _joined(poly, tracks=(track,))
    assert not _joined(poly, fixed=({**fixed[0], "net": "other"},))
    assert not _joined(poly, tracks=(dataclasses.replace(track, layer=0),))
    assert not _joined(box(4, 1, 6, 2), fixed=fixed, tracks=(track,))


@pytest.mark.parametrize("authored", [False, True])
def test_pour_joins_annulus_edge_but_not_drill_void(authored: bool) -> None:
    via = RealizedVia(
        seg_id=0,
        net_id=0,
        x=0.0,
        y=0.0,
        dia_mm=0.45,
        drill_mm=0.2,
        layer_lo=1,
        layer_hi=2,
        endpoint="a",
    )
    fixed = (
        {
            "ctype": "via",
            "net": "N0",
            "x": 0.0,
            "y": 0.0,
            "dia_mm": 0.45,
            "drill_mm": 0.2,
            "span": ["In1.Cu", "In2.Cu"],
        },
    )
    kwargs = {"fixed": fixed} if authored else {"vias": (via,)}
    assert _joined(box(0.1, -0.5, 1.0, 0.5), **kwargs)
    assert not _joined(box(-0.02, -0.02, 0.02, 0.02), **kwargs)
    assert not _joined(box(0.3, -0.5, 1.0, 0.5), **kwargs)
    assert not _joined(box(0.1, -0.5, 1.0, 0.5), layer=0, **kwargs)


@pytest.mark.parametrize("authored", [False, True])
def test_pour_disconnected_parts_do_not_invent_annulus_contact(authored: bool) -> None:
    via = RealizedVia(
        seg_id=0,
        net_id=0,
        x=0.0,
        y=0.0,
        dia_mm=0.45,
        drill_mm=0.2,
        layer_lo=1,
        layer_hi=2,
        endpoint="a",
    )
    fixed = (
        {
            "ctype": "via",
            "net": "N0",
            "x": 0.0,
            "y": 0.0,
            "dia_mm": 0.45,
            "drill_mm": 0.2,
            "span": ["In1.Cu", "In2.Cu"],
        },
    )
    kwargs = {"fixed": fixed} if authored else {"vias": (via,)}
    inside_and_far = MultiPolygon([box(-0.01, -0.01, 0.01, 0.01), box(2, 2, 3, 3)])
    assert not _joined(inside_and_far, **kwargs)
    inside_and_touching = MultiPolygon(
        [box(-0.01, -0.01, 0.01, 0.01), box(0.15, -0.01, 0.3, 0.01)]
    )
    assert _joined(inside_and_touching, **kwargs)
