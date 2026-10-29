"""Bottom-side courtyard mirroring, pinned across every consumer
(slice-2a seam item 4): an asymmetric footprint on the bottom mounts
mirrored — the same physical polygon the pads take — and the placer's
keep-out cache (``optimize._world_courtyard``), the cost fallback
(``cost._world_courtyard_polygon``), the offline check
(``check_run.rule_inputs``) and placement DRC
(``world_courtyard_polygons`` → ``check_courtyard_overlap``) all see
THAT polygon, not its unmirrored twin."""

from __future__ import annotations

import precis_geom.pcb as pcb
from precis.pcb import check_run
from precis.pcb import local as kernel
from precis.pcb.cost import COURTYARD_MIN_SEPARATION_MM, _world_courtyard_polygon
from precis.pcb.drc import check_courtyard_overlap
from precis.pcb.ir import (
    COURTYARD_CLEARANCE_MM,
    instance_courtyard_polygons,
    world_courtyard_polygons,
)
from precis.pcb.optimize import OptimizeEngine


def _ir():
    fp = pcb.Footprint(
        "ASY2",
        [
            pcb.pad("1", "rect", -2.0, 0.0, w=0.6, h=0.6),
            pcb.pad("2", "rect", 0.5, 0.0, w=0.6, h=0.6),
        ],
    )
    b = pcb.Board("m", stackup=2, outline=pcb.rect(20.0, 20.0))
    f = b.footprint(fp)
    b.part("B1", footprint=f, pins=["1", "2"]).at(10.0, 10.0, side="bottom")
    design = kernel._design(b, None)
    return design, kernel._build_ir(design)


def test_bottom_courtyard_mirrored_consistently() -> None:
    design, ir = _ir()
    local_rings = instance_courtyard_polygons(
        ir,
        clearance_mm=COURTYARD_CLEARANCE_MM,
        fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
    )
    world = world_courtyard_polygons(
        ir,
        clearance_mm=COURTYARD_CLEARANCE_MM,
        fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
    )[0]
    # local pads at x=-2.0 / +0.5 -> asymmetric hull; mirrored placement
    # puts the long reach on +x (world x > 11), unmirrored would put it
    # on -x (world x < 9).
    xs = [p[0] for p in world]
    assert max(xs) > 11.0 and min(xs) > 8.5
    expected = sorted(world)

    # the placer's cached keep-out (optimize._world_courtyard)
    eng = OptimizeEngine.__new__(OptimizeEngine)
    eng.ir = ir
    eng._keepout_poly = local_rings
    eng._world_poly = {}
    assert sorted(eng._world_courtyard(0)) == expected

    # the cost module's IR-derived fallback
    assert sorted(_world_courtyard_polygon(ir, 0, 10.0, 10.0)) == expected

    # the offline check's rule inputs
    courtyard_local = {
        str(ir.instance_refdes[i]): list(local_rings[i]) for i in range(ir.n_instances)
    }
    courtyards, courtyard_bottom, _rules, _volts, bodies = check_run.rule_inputs(
        design.graph, courtyard_local, design.fab_caps
    )
    assert courtyard_bottom == {"B1": True}
    assert sorted(dict(courtyards)["B1"]) == expected
    # No body_local passed: bodies is None — the "silent as before"
    # contract (gr529963).
    assert bodies is None

    # With body_local: the zero-clearance body travels the SAME mirrored
    # affine path as the courtyard (gr529963's measure must see the
    # physical polygon, not its unmirrored twin).
    body_local = {
        str(ir.instance_refdes[i]): ring
        for i, ring in enumerate(
            instance_courtyard_polygons(
                ir,
                clearance_mm=0.0,
                fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
            )
        )
    }
    world_body = world_courtyard_polygons(
        ir,
        clearance_mm=0.0,
        fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
    )[0]
    _c, _cb, _r, _v, bodies = check_run.rule_inputs(
        design.graph, courtyard_local, design.fab_caps, body_local
    )
    assert sorted(dict(bodies or [])["B1"]) == sorted(world_body)

    # and placement DRC flags a same-side pair over that polygon
    findings = check_courtyard_overlap(
        [("B1", world), ("B2", world)],
        bottom_by_refdes={"B1": True, "B2": True},
    )
    assert findings and findings[0].severity == "error"


def test_disjoint_polygon_pads_bridge_without_hull_at_zero_clearance() -> None:
    """The zero-clearance BODY (gr529963's measure) of a part whose pads
    stay disjoint at 0.0 — a shaped electrode's two pads, a connector's
    far tabs — used to fall back to the corner-HULL of every pad point.
    The hull fills every notch between the clusters: measured on taz-leaf
    2026-10-29, the combs' body swallowed its own zip-hole cut and
    ``check_courtyard_hole`` priced 8.22mm² of copper against the keep-out
    where the drawn union clears it. Bridging keeps the concavity: one
    ring, the pads covered, and nothing like the hull's sweep between
    them."""
    from shapely.geometry import Point
    from shapely.geometry import Polygon as ShapelyPolygon

    fp = pcb.Footprint(
        "TWO",
        [
            pcb.pad(
                "1",
                "polygon",
                0,
                0,
                poly=[
                    [-1.75, -2.0],
                    [-0.75, -2.0],
                    [-0.75, 2.0],
                    [-1.75, 2.0],
                    [-1.75, -2.0],
                ],
            ),
            pcb.pad(
                "2",
                "polygon",
                0,
                0,
                poly=[
                    [0.75, -2.0],
                    [1.75, -2.0],
                    [1.75, 2.0],
                    [0.75, 2.0],
                    [0.75, -2.0],
                ],
            ),
        ],
    )
    b = pcb.Board("m", stackup=2, outline=pcb.rect(20.0, 20.0))
    f = b.footprint(fp)
    b.part("T1", footprint=f, pins=["1", "2"]).at(10.0, 10.0)
    design = kernel._design(b, None)
    ir = kernel._build_ir(design)

    ring = instance_courtyard_polygons(
        ir, clearance_mm=0.0, fallback_half_extent_mm=0.0
    )[0]
    assert len(ring) >= 4 and ring[0] == ring[-1]
    body = ShapelyPolygon(ring[:-1])
    # both pads fully covered (probe each authored corner)
    for corner in ((-1.75, -2.0), (-0.75, 2.0), (0.75, -2.0), (1.75, 2.0)):
        assert body.covers(Point(*corner))
    # the hull of the two pads would cover 3.5x4 = 14mm² AND the deep
    # notch points between the pads; the bridged body keeps both clusters'
    # concavity — the notch between them (away from the thin corridor)
    # stays uncovered, and the area is the pads' ~8mm² plus a whisker,
    # never the hull's 14.
    assert not body.covers(Point(0.0, 1.5))
    assert 8.0 <= body.area < 11.0, body.area
