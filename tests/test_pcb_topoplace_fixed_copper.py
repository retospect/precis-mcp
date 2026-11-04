"""gr562036: authored copper mesh rings enclose the true copper boundary."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

from precis.pcb import local
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace.design import TopoDesign
from tests._pcb_outline import closed_outline


def _design(copper: dict[str, Any]) -> TopoDesign:
    batch = {
        "footprints": [
            {
                "name": "P1",
                "pads": [
                    {
                        "number": "1",
                        "shape": "rect",
                        "x": 0.0,
                        "y": 0.0,
                        "w": 0.5,
                        "h": 0.5,
                    }
                ],
                "pin_map": {"1": "1"},
            }
        ],
        "components": [
            {
                "refdes": refdes,
                "label": "pad",
                "footprint": "P1",
                "x": x,
                "y": 3.0,
                "pins": [{"name": "1"}],
            }
            for refdes, x in (("J1", 3.0), ("J2", 27.0))
        ],
        "nets": [{"name": "S", "current": 0.001}],
        "connections": [
            {"net": "S", "refdes": refdes, "pin": "1"} for refdes in ("J1", "J2")
        ],
        "features": [closed_outline(0.0, 0.0, 30.0, 30.0)],
        "fixed_copper": [copper],
    }
    design = local._design(batch, None)
    ir, footprints, _specs = local._prepare(design)
    return design_from_ir(ir, footprints, fixed_copper=design.fixed_copper)


@pytest.mark.parametrize("width_mm", [0.2, 0.5, 2.0])
@pytest.mark.parametrize(
    "end",
    [(20.0, 10.0), (10.0, 20.0), (18.0, 16.0)],
    ids=["horizontal", "vertical", "oblique"],
)
def test_authored_track_ring_chords_enclose_exact_capsule(
    width_mm: float, end: tuple[float, float]
) -> None:
    """Distance to the spine includes both exact round caps, without buffering."""
    start = (10.0, 10.0)
    td = _design(
        {
            "ctype": "track",
            "net": "S",
            "layer": "F.Cu",
            "width_mm": width_mm,
            "segments": [{"start": start, "end": end}],
        }
    )
    (zone,) = [zone for zone in td.zones if zone.kind == "fixed_copper"]
    assert zone.net == "S"
    assert zone.layers == (0,)
    assert zone.name == "fixed copper track"
    np.testing.assert_array_equal(zone.ring[0], zone.ring[-1])
    spine = LineString([start, end])
    assert Polygon(zone.ring).covers(spine)
    radius = width_mm / 2.0
    for a, b in zip(zone.ring[:-1], zone.ring[1:]):
        # Inscribed circle vertices pass a vertex-only check; their chords fail.
        assert LineString([a, b]).distance(spine) >= radius - 1e-9
    for vertex in zone.ring[:-1]:
        assert Point(vertex).distance(spine) <= radius + 0.02 + 1e-9


def test_disconnected_authored_track_keeps_each_obstacle() -> None:
    """gr562036: a MultiPolygon must not erase every run of a track row."""
    segments = [((5.0, 5.0), (10.0, 5.0)), ((15.0, 5.0), (20.0, 5.0))]
    td = _design(
        {
            "ctype": "track",
            "net": "S",
            "layer": "F.Cu",
            "width_mm": 0.5,
            "segments": [{"start": start, "end": end} for start, end in segments],
        }
    )
    zones = [zone for zone in td.zones if zone.kind == "fixed_copper"]
    assert len(zones) == 2
    polygons = [Polygon(zone.ring) for zone in zones]
    for start, end in segments:
        spine = LineString([start, end])
        (polygon,) = [polygon for polygon in polygons if polygon.covers(spine)]
        for a, b in pairwise(polygon.exterior.coords):
            assert LineString([a, b]).distance(spine) >= 0.25 - 1e-9
    for zone, polygon in zip(zones, polygons):
        assert zone.net == "S"
        assert zone.layers == (0,)
        assert zone.name == "fixed copper track"
        np.testing.assert_array_equal(zone.ring[0], zone.ring[-1])
        assert not polygon.covers(Point(12.5, 5.0))
    assert polygons[0].disjoint(polygons[1])


def test_authored_pour_keeps_net_layer_and_bounds_outward_growth() -> None:
    polygon = [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0)]
    td = _design({"ctype": "pour", "net": "S", "layer": "B.Cu", "polygon": polygon})
    (zone,) = [zone for zone in td.zones if zone.kind == "fixed_copper"]
    assert zone.net == "S"
    assert zone.layers == (td.n_layers - 1,)
    assert zone.name == "fixed copper pour"
    np.testing.assert_array_equal(zone.ring[0], zone.ring[-1])
    copper = Polygon(polygon)
    assert Polygon(zone.ring).covers(copper)
    for vertex in zone.ring[:-1]:
        assert Point(vertex).distance(copper) <= 0.02 + 1e-9
