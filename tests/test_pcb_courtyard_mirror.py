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
    courtyards, courtyard_bottom, _rules, _volts = check_run.rule_inputs(
        design.graph, courtyard_local, design.fab_caps
    )
    assert courtyard_bottom == {"B1": True}
    assert sorted(dict(courtyards)["B1"]) == expected

    # and placement DRC flags a same-side pair over that polygon
    findings = check_courtyard_overlap(
        [("B1", world), ("B2", world)],
        bottom_by_refdes={"B1": True, "B2": True},
    )
    assert findings and findings[0].severity == "error"
