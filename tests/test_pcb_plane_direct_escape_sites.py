"""Bounded direct fresh sites retain the real planter and whole-stub gates."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from precis.pcb.ir import FootprintPad
from precis.pcb.realize import RealizedVia, RealizeResult
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import realise, wires
from precis.pcb.topoplace.copper_index import CopperIndex
from precis.pcb.topoplace.design import TopoZone
from precis.pcb.topoplace.mesh import build_mesh
from tests.test_pcb_topo_fanout_clearance import _track
from tests.test_pcb_topo_plane_fanout import _escape_case, _escape_mesh
from tests.test_pcb_topoplace_walls import _board


def _direct_case(*, boundary: float = 8.0) -> tuple[Any, ...]:
    td, ir = _escape_case()
    td = replace(
        td,
        zones=(
            TopoZone(
                "keepout",
                np.asarray(
                    [[-20.0, -20.0], [boundary, -20.0], [boundary, 40.0], [-20.0, 40.0]]
                ),
                (3,),
            ),
        ),
    )
    portal = boundary - 1.0
    mesh = _escape_mesh(td, [[5.0, 10.0], [portal, 1.0], [portal, 19.0], [29.0, 10.0]])
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    rules = {name: NetRules(0.15, 0.15, 0.45, 0.2) for name in td.net_name}
    prior = RealizedVia(
        -1, list(ir.net_name).index("GND"), 22.0, 15.0, 0.45, 0.2, 0, 3, "a"
    )
    copper = RealizeResult((), (prior,), ())
    joined = realise._joined_plane_pieces(
        td,
        ir,
        {k: 1},
        [],
        [prior],
        td.pose0,
        rules,
        {},
        [],
        0.0,
        pours=[
            {
                "net": "GND",
                "layer": "In1.Cu",
                "polygon": [[8.1, 1.0], [29.0, 1.0], [29.0, 19.0], [8.1, 19.0]],
            }
        ],
    )
    assert joined[k], "the real prior barrel must join the proposed target pour"
    return td, ir, mesh, k, pin, rules, copper, joined


@pytest.mark.parametrize("smaller_fallback", [False, True])
def test_a_fresh_interior_site_escapes_a_lower_layer_keepout(
    smaller_fallback: bool,
) -> None:
    td, ir, mesh, k, pin, rules, copper, joined = _direct_case()
    tracks, vias, missing, done, need, reasons, spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        rules,
        copper,
        int(ir.n_layers),
        mesh=mesh,
        joined_pours=joined,
        min_via_dia=0.25 if smaller_fallback else None,
        min_via_drill=0.1 if smaller_fallback else None,
    )
    assert missing == [], f"a legal far-triangle direct site was missed: {reasons}"
    assert done == [(k, pin)] and need == reasons == {}
    assert spans == {(k, pin): (0, 3)}
    assert len(tracks) == len(vias) == 1, "a fresh site must emit a new barrel"
    assert vias[0].dia_mm == 0.45 and vias[0].drill_mm == 0.2
    site = Point(vias[0].x, vias[0].y)
    assert Polygon(mesh.verts[mesh.tris[1]]).contains(site)
    assert 8.375225 <= site.x < 8.38 and abs(site.y - 10.0) < 1e-9
    assert tracks[0].segments == (
        {"shape": "line", "start": [5.0, 10.0], "end": [site.x, site.y]},
    )
    assert tracks[0].length_mm < 10.0


def test_a_direct_site_cannot_bypass_a_blocked_whole_stub() -> None:
    td, ir, mesh, k, pin, rules, copper, joined = _direct_case()
    foreign = _track(list(ir.net_name).index("S"), 0, [(6.5, 9.0), (6.5, 11.0)])
    copper = replace(copper, tracks=(foreign,))
    tracks, vias, missing, done, need, reasons, _spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        rules,
        copper,
        int(ir.n_layers),
        mesh=mesh,
        joined_pours=joined,
    )
    assert tracks == vias == done == [] and missing == [(k, pin)]
    assert need == {} and reasons == {(k, pin): "no_site"}


def test_a_direct_site_must_join_an_existing_pour_piece() -> None:
    td, ir, mesh, k, pin, rules, copper, _joined = _direct_case()
    tracks, vias, missing, done, need, reasons, _spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        rules,
        copper,
        int(ir.n_layers),
        mesh=mesh,
        joined_pours={k: []},
    )
    assert tracks == vias == done == [] and missing == [(k, pin)]
    assert need == {} and reasons == {(k, pin): "isolated_pour"}


def test_a_direct_site_past_the_cap_records_its_shorter_needed_length() -> None:
    td, ir, mesh, k, pin, rules, copper, joined = _direct_case(boundary=18.0)
    tracks, vias, missing, done, need, reasons, _spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        rules,
        copper,
        int(ir.n_layers),
        mesh=mesh,
        joined_pours=joined,
    )
    assert tracks == vias == done == [] and missing == [(k, pin)]
    assert reasons == {(k, pin): "over_cap"}
    assert 13.0 < need[(k, pin)] < 13.2


def test_a_smaller_direct_barrel_can_clear_the_unchanged_external_copper_cap() -> None:
    td, ir, mesh, k, pin, rules, copper, joined = _direct_case(boundary=14.9)
    tracks, vias, missing, done, need, reasons, _spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        rules,
        copper,
        int(ir.n_layers),
        mesh=mesh,
        joined_pours=joined,
        min_via_dia=0.25,
        min_via_drill=0.1,
    )
    assert missing == [] and done == [(k, pin)] and need == reasons == {}
    assert len(tracks) == len(vias) == 1
    assert vias[0].dia_mm == 0.25 and vias[0].drill_mm == 0.1
    assert tracks[0].length_mm > 10.0
    assert tracks[0].length_mm - 0.25 < 10.0, "cap excludes the source pad's own copper"


def test_arbitrary_sites_keep_the_own_pad_mask_web() -> None:
    td, _ir = _escape_case()
    mesh = _escape_mesh(td, [[5.0, 10.0], [7.0, 1.0], [7.0, 19.0], [29.0, 10.0]])
    ctx = wires._make_ctx(mesh, td, 4, td.pose0)
    planter = wires._ViaPlanter(ctx, td)
    k = list(td.net_name).index("GND")
    web = planter.mask_web
    assert abs(web - 0.2) < 1e-9
    xy = np.asarray([[5.475 + (0.15 + web) / 2.0, 10.0], [5.475 + web + 0.0003, 10.0]])
    assert planter.plantable_points(xy, 0.225, 0.15, k).tolist() == [False, True]
    assert planter.boundary_points((5.0, 10.0), 0.225, 0.15, k) == [], (
        "outline is not a proposal zone"
    )


@pytest.mark.parametrize(
    ("x_offset", "drill", "expected"),
    [(1.6, 0.3, False), (1.7, 0.3, True), (1.7, 0.5, False)],
)
def test_arbitrary_sites_keep_a_slotted_pad_drill_wall(
    x_offset: float,
    drill: float,
    expected: bool,
) -> None:
    td = _board([dict(x=10.0, y=10.0, net=0, w=1.6, h=1.6, side=2)], n_nets=1)
    td = replace(
        td,
        footprint_pads=(
            FootprintPad(
                instance=0,
                number="1",
                label="1",
                pin=0,
                primary=True,
                raw=dict(
                    x=0.0,
                    y=0.0,
                    w=1.6,
                    h=1.6,
                    shape="rect",
                    drill=0.6,
                    drill_offset={"x": 0.5, "y": 0.0},
                    slot={"w": 1.6, "h": 0.6},
                ),
            ),
        ),
    )
    mesh = build_mesh(td, td.pose0)
    planter = wires._ViaPlanter(wires._make_ctx(mesh, td, 4, td.pose0), td)
    xy = np.asarray([[10.0 + x_offset, 10.0]])
    assert planter.plantable_points(xy, 0.225, 0.15, 0, drill_mm=0.05).tolist() == [
        True
    ]
    assert planter.plantable_points(xy, 0.225, 0.15, 0, drill_mm=drill).tolist() == [
        expected
    ]
    assert planter.plantable_points(xy, 0.225, 0.15, 0, drill_mm=0.0).tolist() == (
        planter.plantable_points(
            xy, 0.225, 0.15, 0, drill_mm=planter.default_drill_mm
        ).tolist()
    )


def test_a_boundary_proposal_inside_an_overlapping_zone_is_rejected() -> None:
    td, ir, mesh, k, pin, rules, copper, joined = _direct_case()
    td = replace(
        td,
        zones=(
            *td.zones,
            TopoZone(
                "keepout",
                np.asarray([[8.2, 9.8], [9.0, 9.8], [9.0, 10.2], [8.2, 10.2]]),
                (3,),
            ),
        ),
    )
    planter = wires._ViaPlanter(wires._make_ctx(mesh, td, 4, td.pose0), td)
    proposals = planter.boundary_points((5.0, 10.0), 0.225, 0.15, k)
    assert len(proposals) == 1
    assert Polygon(td.zones[1].ring).contains(Point(proposals[0]))
    assert planter.plantable_points(np.asarray(proposals), 0.225, 0.15, k).tolist() == [
        False
    ]
    tracks, vias, missing, done, _need, _reasons, _spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        rules,
        copper,
        int(ir.n_layers),
        mesh=mesh,
        joined_pours=joined,
    )
    assert tracks == vias == done == [] and missing == [(k, pin)]


def test_proposals_retain_transformed_zones_and_the_foreign_named_clearance() -> None:
    td, _ir = _escape_case()
    zone = TopoZone(
        "fixed_copper",
        np.asarray([[-2.0, -2.0], [2.0, -2.0], [2.0, 2.0], [-2.0, 2.0]]),
        (3,),
        "S",
        part=0,
        part_local=True,
    )
    td = replace(td, zones=(zone,))
    mesh = _escape_mesh(td, [[5.0, 10.0], [7.0, 1.0], [7.0, 19.0], [29.0, 10.0]])
    k = list(td.net_name).index("GND")
    planter = wires._ViaPlanter(
        wires._make_ctx(mesh, td, 4, td.pose0),
        td,
        net_clearance_by_name={"S": 0.4, "GND": 0.15},
    )
    proposals = planter.boundary_points((5.0, 10.0), 0.225, 0.15, k)
    assert len(proposals) == 1
    assert 2.625225 < Point(proposals[0]).distance(Point(5.0, 10.0)) < 2.63
    assert planter.plantable_points(np.asarray(proposals), 0.225, 0.15, k).tolist() == [
        True
    ]


def test_arbitrary_sites_recheck_dynamic_copper_without_a_stale_verdict() -> None:
    td, _ir = _escape_case()
    mesh = _escape_mesh(td, [[5.0, 10.0], [7.0, 1.0], [7.0, 19.0], [29.0, 10.0]])
    planter = wires._ViaPlanter(wires._make_ctx(mesh, td, 4, td.pose0), td)
    k = list(td.net_name).index("GND")
    xy = np.asarray([[10.0, 10.0], [10.9, 10.0]])
    dyn = CopperIndex([])
    assert planter.plantable_points(xy, 0.225, 0.15, k, dyn).tolist() == [True, True]
    dyn.add_disc(10.0, 10.0, 0.225, clearance=0.4)
    assert planter.plantable_points(xy, 0.225, 0.15, k, dyn).tolist() == [False, True]


def test_an_outside_outline_proposal_does_not_emit_copper() -> None:
    td, ir, mesh, k, pin, rules, copper, joined = _direct_case()
    td = replace(
        td,
        zones=(
            TopoZone(
                "keepout",
                np.asarray([[0.0, 0.0], [30.0, 0.0], [30.0, 20.0], [0.0, 20.0]]),
                (3,),
            ),
        ),
    )
    planter = wires._ViaPlanter(wires._make_ctx(mesh, td, 4, td.pose0), td)
    proposals = planter.boundary_points((5.0, 10.0), 0.225, 0.15, k)
    assert len(proposals) == 1 and proposals[0][0] < 0.0
    assert planter.plantable_points(np.asarray(proposals), 0.225, 0.15, k).tolist() == [
        True
    ]
    tracks, vias, missing, done, _need, _reasons, _spans = realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        rules,
        copper,
        int(ir.n_layers),
        mesh=mesh,
        joined_pours=joined,
    )
    assert tracks == vias == done == [] and missing == [(k, pin)]


def test_boundary_proposals_are_deterministic_deduplicated_and_capped() -> None:
    td, _ir = _escape_case()
    zones = tuple(
        TopoZone(
            "keepout",
            np.asarray(
                [
                    [-20.0, -20.0],
                    [8.0 + i / 10.0, -20.0],
                    [8.0 + i / 10.0, 40.0],
                    [-20.0, 40.0],
                ]
            ),
            (3,),
        )
        for i in range(70)
    )
    td = replace(td, zones=zones + zones)
    mesh = _escape_mesh(td, [[5.0, 10.0], [7.0, 1.0], [7.0, 19.0], [29.0, 10.0]])
    k = list(td.net_name).index("GND")
    planter = wires._ViaPlanter(wires._make_ctx(mesh, td, 4, td.pose0), td)
    first = planter.boundary_points((5.0, 10.0), 0.225, 0.15, k)
    assert len(first) == len(set(first)) == 64
    assert first == sorted(first, key=lambda p: Point(p).distance(Point(5.0, 10.0)))
    reversed_td = replace(td, zones=tuple(reversed(td.zones)))
    reversed_planter = wires._ViaPlanter(
        wires._make_ctx(mesh, reversed_td, 4, td.pose0), reversed_td
    )
    assert reversed_planter.boundary_points((5.0, 10.0), 0.225, 0.15, k) == first
