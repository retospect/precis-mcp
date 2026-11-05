"""gr524239 (topo-route-review C): a plane net drops to its plane by via.
td569572: the plane is then a routed TREE on its layer, not a pour —
the pour (:func:`precis.pcb.topoplace.realise._plane_pours`) stays only
as the fallback for a net the tree cannot connect whole (ruling
me569463).

A board that trees GND on In1 routes no GND wire between pads directly:
every SMD GND pad gets a fanout via and a stub on its own layer, In1 is
barred to every routed net, and the tree joins the vias' In1 landings
into one component. Driven through ``pcb.local``'s route stage, DB-free
— the half the ``placer='topo'`` job shares.

The board: four two-pad parts. GND sits on one pad of each, spread round
the board (R1 west, R2 east); signal S runs R3 → R4 south to north, across
the line a GND wire from R1 to R2 would take on F.Cu. (Two signals that
cross each other do not route here yet — the router's open crossing
resolution, with or without the plane.)
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from precis.pcb import capabilities as pcb_capabilities
from precis.pcb import drc as pcb_drc
from precis.pcb import local
from precis.pcb import realize as pcb_realize
from precis.pcb import session as pcb_session
from precis.pcb import silk as pcb_silk
from precis.pcb.gerber import DEFAULT_SILK_WIDTH_MM
from precis.pcb.realize import RealizedTrack, RealizedVia, RealizeResult
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import realise as topo_realise
from precis.pcb.topoplace import wires as topo_wires
from precis.pcb.topoplace.design import SilkRoom, design_from_ir
from precis.pcb.topoplace.mesh import OWNER_NONE, TopoMesh
from precis.pcb.topoplace.realise import _FANOUT_LADDER_CAP_MM, route_design
from tests._pcb_outline import closed_outline

_P2: dict[str, Any] = {
    "name": "P2",
    "pads": [
        {"number": "1", "shape": "rect", "x": -0.5, "y": 0.0, "w": 0.5, "h": 0.5},
        {"number": "2", "shape": "rect", "x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5},
    ],
    "pin_map": {"1": "1", "2": "2"},
}


def _part(refdes: str, x: float, y: float) -> dict[str, Any]:
    return {
        "refdes": refdes,
        "label": "r",
        "footprint": "P2",
        "x": x,
        "y": y,
        "pins": [{"name": "1"}, {"name": "2"}],
    }


def _batch(*, plane: bool) -> dict[str, Any]:
    features: list[dict[str, Any]] = [closed_outline(0.0, 0.0, 30.0, 20.0)]
    if plane:
        features.append({"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"})
    wiring = {
        "GND": (("R1", "1"), ("R2", "2"), ("R3", "1"), ("R4", "2")),
        "S": (("R3", "2"), ("R4", "1")),
    }
    return {
        "footprints": [_P2],
        "components": [
            _part("R1", 8.0, 10.0),
            _part("R2", 22.0, 10.0),
            _part("R3", 15.0, 4.0),
            _part("R4", 15.0, 16.0),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
            {"name": "S", "current": 0.001},
        ],
        "connections": [
            {"net": net, "refdes": r, "pin": p}
            for net, members in wiring.items()
            for r, p in members
        ],
        "features": features,
    }


@pytest.fixture(scope="module")
def routed() -> tuple[Any, Any, Any, Any]:
    design = local._design(_batch(plane=True), None)
    ir, fps, _specs = local._prepare(design)
    return design, ir, fps, local._route_stage(design, ir, fps)


def test_each_gnd_pad_drops_to_the_plane_by_its_own_via(routed) -> None:
    design, ir, fps, stage = routed
    gnd = list(ir.net_name).index("GND")
    assert stage.plan.fanout_missing == ()
    assert [layer for _net, layer in stage.plan.planes] == [1]
    pads = [
        p
        for p in pcb_realize.pads_for_ir(ir, design.layer_names, fps)
        if p["net"] == "GND"
    ]
    vias = [v for v in stage.result.vias if v.net_id == gnd]
    assert len(pads) == 4
    assert len(vias) >= 4
    # the via sits clear of the part's silk ring — the band's reach off
    # the pad hull (outline_reach + half a stroke + via-to-silk + the
    # draw's sampled slack, realise._fanout_vias's ring_clear) plus its
    # own radius plus the preferred ladder's cap past the band
    room = SilkRoom.from_capability(design.fab_caps)
    bound = (
        room.outline_reach_mm
        + DEFAULT_SILK_WIDTH_MM / 2.0
        + room.via_to_silk_mm
        + 0.05
        + max(v.dia_mm for v in vias) / 2.0
        + _FANOUT_LADDER_CAP_MM
    )
    for pad in pads:
        near = min(math.hypot(v.x - pad["x"], v.y - pad["y"]) for v in vias)
        assert near < bound, pad


def _net_components(tracks: Any, net_id: int) -> list[set[tuple[float, float]]]:
    """Connected components of ``net_id``'s copper, by rounded endpoint
    coincidence — a via's barrel joins every track whose stub ends on
    its XY, so sharing a coordinate is sharing a node."""
    parent: dict[tuple[float, float], tuple[float, float]] = {}

    def find(a: tuple[float, float]) -> tuple[float, float]:
        parent.setdefault(a, a)
        while parent[a] != a:
            a = parent[a]
        return a

    def union(a: tuple[float, float], b: tuple[float, float]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for t in tracks:
        if t.net_id != net_id:
            continue
        pts = [tuple(s["start"]) for s in t.segments] + [tuple(t.segments[-1]["end"])]
        pts = [(round(float(x), 3), round(float(y), 3)) for x, y in pts]
        for a, b in itertools.pairwise(pts):
            union(a, b)
    groups: dict[tuple[float, float], set[tuple[float, float]]] = {}
    for p in list(parent):
        groups.setdefault(find(p), set()).add(p)
    return list(groups.values())


def test_signals_never_route_on_the_plane_layer(routed) -> None:
    """In1 is reserved to the GND tree (td569572) — every OTHER net's
    copper stays off it, exactly as when it was a pour."""
    _design, ir, _fps, stage = routed
    s = list(ir.net_name).index("S")
    assert all(t.layer != 1 for t in stage.result.tracks if t.net_id == s)
    assert stage.rows["S"]["status"] == "realized"


def test_gnd_routes_as_one_tree_on_in1_and_the_plane_never_pours(routed) -> None:
    """td569572: the gauge board's 4 GND pads all fan out clean, so the
    In1 tree (:func:`precis.pcb.topoplace.realise._plane_trees`)
    connects them whole and :func:`_plane_pours` never fires — no
    ``pcb_planes`` feature on the dogfood copy."""
    _design, ir, _fps, stage = routed
    gnd = list(ir.net_name).index("GND")
    assert not any(p["net"] == "GND" for p in stage.result.pours)
    # the fanout stub stays on each pad's own layer; the tree joining
    # the stubs' vias together is the ONLY thing on In1
    assert all(t.layer in (0, 1) for t in stage.result.tracks if t.net_id == gnd)
    assert any(t.layer == 1 for t in stage.result.tracks if t.net_id == gnd)
    landings = {
        (round(v.x, 3), round(v.y, 3)) for v in stage.result.vias if v.net_id == gnd
    }
    comps = _net_components(stage.result.tracks, gnd)
    assert len(comps) == 1
    assert landings <= comps[0]


def test_the_treed_board_is_drc_clean(routed) -> None:
    design, ir, fps, stage = routed
    model = pcb_session._copper_model(
        ir,
        pcb_realize.result_copper_rows(
            stage.result, ir, design.layer_names, derived=True
        ),
        footprints=fps,
    )
    errors = [
        (f.rule, f.where)
        for f in pcb_drc.run_geometric_drc(
            model,
            capability=design.fab_caps,
            outline=[list(p) for p in (design.outline or [])],
            keepouts=pcb_session.keepouts_from_features(design.features),
            net_rules=pcb_session.clearance_net_rules(design.graph, design.fab_caps),
        )
        if f.severity == "error"
    ]
    assert errors == []


def test_gnd_falls_back_to_the_pour_when_the_tree_cannot_connect() -> None:
    """td569572 DECISION 2: both GND pads fan out fine, but an In1-only
    wall spanning the board's full height between them leaves the tree
    two components — :func:`_plane_trees` reports neither net ok, so
    :func:`_plane_pours` stays the fallback, exactly as before this
    slice."""
    batch = {
        "footprints": [_P2],
        "components": [_part("R1", 8.0, 10.0), _part("R2", 22.0, 10.0)],
        "nets": [{"name": "GND", "class": "gnd", "current": 0.1}],
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R2", "pin": "2"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
            # a full-height In1-only wall between the two pads: other
            # layers (the fanout vias' own sites, well clear of it) stay
            # open, but the tree's one candidate layer has no path across
            {
                "ftype": "keepout",
                "geom": {
                    "poly": [[14.0, 0.0], [16.0, 0.0], [16.0, 20.0], [14.0, 20.0]]
                },
                "layers": ["In1.Cu"],
            },
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    gnd = list(ir.net_name).index("GND")
    assert stage.plan.fanout_missing == ()
    vias = [v for v in stage.result.vias if v.net_id == gnd]
    assert len(vias) == 2
    assert any(p["net"] == "GND" and p["layer"] == "In1.Cu" for p in stage.result.pours)


def test_without_a_plane_gnd_routes_as_a_net() -> None:
    """The split only fires on a poured net: the same board without the
    plane feature wires GND like any other net and pours nothing."""
    design = local._design(_batch(plane=False), None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    assert stage.plan.planes == ()
    assert stage.result.pours == ()


@pytest.mark.parametrize("members", [1, 2])
@pytest.mark.parametrize("plane", [False, True])
def test_single_pin_plane_net_finding(members: int, plane: bool) -> None:
    """gr561955: dropping a singleton from routing must not hide its plane intent."""
    batch = _batch(plane=plane)
    batch["components"] = batch["components"][:members]
    batch["nets"] = batch["nets"][:1]
    batch["connections"] = [c for c in batch["connections"] if c["net"] == "GND"][
        :members
    ]
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    td = design_from_ir(ir, fps, route_planes=True)
    plan, result = route_design(td, ir, footprints=fps)
    model = pcb_realize.to_gerber_model(
        result,
        ir,
        layers=design.layer_names,
        outline=design.outline or [],
        footprints=fps,
    )
    all_findings = pcb_drc.run_geometric_drc(
        model, capability=design.fab_caps, outline=design.outline
    )
    findings = [f for f in all_findings if f.rule == "single_pin_plane_net"]
    if members == 1 and plane:
        assert all_findings == findings
        assert len(findings) == 1
        finding = findings[0]
        assert finding.severity == "warn"
        assert finding.where == "pad R1/1 (net GND)"
        assert "GND" in finding.detail and "R1/1" in finding.detail
        assert finding.objects == ({"net": "GND", "refdes": "R1", "pin": "1"},)
        assert td.net_pins == ()
        assert plan.planes == ()
        assert result.tracks == result.vias == result.pours == ()
    else:
        assert findings == []


def test_single_pin_plane_split_land_emits_one_finding() -> None:
    batch = _batch(plane=True)
    batch["components"] = batch["components"][:1]
    batch["components"][0]["pins"] = [{"name": "1"}]
    batch["nets"] = batch["nets"][:1]
    batch["connections"] = batch["connections"][:1]
    batch["footprints"] = [
        {
            **_P2,
            "pads": [_P2["pads"][0], {**_P2["pads"][1], "number": "1"}],
            "pin_map": {"1": "1"},
        }
    ]
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    pads = pcb_realize.pads_for_ir(ir, design.layer_names, fps)
    assert len([p for p in pads if p["net"] == "GND"]) == 2
    findings = pcb_drc.check_single_pin_plane_net({"pads": pads})
    assert len(findings) == 1
    assert findings[0].objects == ({"net": "GND", "refdes": "R1", "pin": "1"},)


def test_unplaced_second_plane_pin_is_not_a_singleton() -> None:
    batch = _batch(plane=True)
    batch["components"] = batch["components"][:2]
    batch["nets"] = batch["nets"][:1]
    batch["connections"] = batch["connections"][:2]
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    ir.inst_x[1] = math.nan
    ir.inst_y[1] = math.nan
    pads = pcb_realize.pads_for_ir(ir, design.layer_names, fps)
    assert len([p for p in pads if p["net"] == "GND"]) == 1
    assert pcb_drc.check_single_pin_plane_net({"pads": pads}) == []


def test_fanout_vias_sit_clear_of_the_silk_rings(routed) -> None:
    """Reto 2026-11-02 (taz-leaf 2x): fanout vias sat next to their pad,
    0-0.2 mm off the part's outline ring, and the silk pass bent every
    ring around them. The preferred siting pass keeps the
    barrel outside every part's ring band, so on a roomy board no via
    reaches any outline's legal clearance — and none falls back."""
    design, ir, _fps, stage = routed
    assert stage.plan.fanout_missing == ()
    assert stage.plan.fanout_fallback == ()
    outlines = pcb_silk.part_outlines(
        ir,
        capability=design.fab_caps,
        outline=[list(p) for p in (design.outline or [])],
    )
    rings = [Polygon(o.ring).exterior for o in outlines.values()]
    legal = DEFAULT_SILK_WIDTH_MM / 2.0 + pcb_silk._via_to_silk_mm(design.fab_caps)
    gnd = list(ir.net_name).index("GND")
    vias = [v for v in stage.result.vias if v.net_id == gnd]
    assert len(vias) >= 4
    for v in vias:
        pt = Point(v.x, v.y)
        assert min(r.distance(pt) for r in rings) >= v.dia_mm / 2.0 + legal - 1e-6


def _horseshoe_keepout(
    cx: float, cy: float, r_in: float, r_out: float, gap_deg: float
) -> dict[str, Any]:
    """An annulus sector keepout: the board covered from ``r_in`` to
    ``r_out`` around ``(cx, cy)`` except a ``+-gap_deg`` wedge toward +x
    — every fanout site at radius >= ``r_in`` dies in it, while the
    wedge leaves the pad's own part reachable."""
    n = 48
    outer = [
        [
            cx + r_out * math.cos(math.radians(t)),
            cy + r_out * math.sin(math.radians(t)),
        ]
        for t in [gap_deg + (360 - 2 * gap_deg) * i / (n - 1) for i in range(n)]
    ]
    inner = [
        [
            cx + r_in * math.cos(math.radians(t)),
            cy + r_in * math.sin(math.radians(t)),
        ]
        for t in [360 - gap_deg - (360 - 2 * gap_deg) * i / (n - 1) for i in range(n)]
    ]
    return {"ftype": "keepout", "geom": {"poly": outer + inner}}


def _inner_layer_horseshoe(
    cx: float, cy: float, r_in: float, r_out: float, gap_deg: float
) -> dict[str, Any]:
    """The horseshoe keepout of :func:`_horseshoe_keepout` restricted to
    In1/In2/B.Cu: bars every barrel (a through via spans all layers)
    while the F.Cu wire carries the escape out."""
    ko = _horseshoe_keepout(cx, cy, r_in, r_out, gap_deg)
    ko["layers"] = ["In1.Cu", "In2.Cu", "B.Cu"]
    return ko


def test_a_pad_in_an_inner_layer_keepout_escapes_by_wire() -> None:
    """G2d: a plane pad a zone leaves no via site for becomes a routed
    two-pin branch on its own layer to the nearest point the planter
    admits outside the via-barred zones. A keepout on In1/In2/B bars
    every barrel the ring could site — a through via spans all layers —
    but not the F.Cu wire that carries the pad out."""
    pad_c = (11.5, 10.0)  # R1's pad 1, west of its (12, 10) centre
    batch = {
        "footprints": [_P2],
        "components": [
            _part("R1", 12.0, 10.0),
            _part("R2", 24.0, 10.0),
            _part("R3", 14.0, 10.0),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
            {"name": "S", "current": 0.001},
            {"name": "T", "current": 0.001},
        ],
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R2", "pin": "2"},
            {"net": "S", "refdes": "R1", "pin": "2"},
            {"net": "S", "refdes": "R2", "pin": "1"},
            {"net": "T", "refdes": "R3", "pin": "1"},
            {"net": "T", "refdes": "R3", "pin": "2"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
            # annulus sector barring the via layers on every ring-clear
            # site around R1.1; the +-10deg gap east is blanketed by
            # R3's pad keep-outs, so the escape wires out through it
            # and plants just past — inside the 5.0 mm escape cap
            _inner_layer_horseshoe(*pad_c, 0.8, 4.0, 10.0),
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    assert stage.plan.fanout_missing == ()
    assert len(stage.plan.fanout_escape) == 1
    gnd = list(ir.net_name).index("GND")
    vias = [v for v in stage.result.vias if v.net_id == gnd]
    near = min(vias, key=lambda v: math.hypot(v.x - pad_c[0], v.y - pad_c[1]))
    # the branch reaches past the keepout on the pad's own layer — and
    # stays inside the escape cap
    stub = min(
        (
            t
            for t in stage.result.tracks
            if t.net_id == gnd
            and math.dist(tuple(t.segments[0]["start"]), pad_c) < 1e-3
        ),
        key=lambda t: t.length_mm,
        default=None,
    )
    assert stub is not None and stub.layer == 0
    assert stub.length_mm < 5.0
    assert math.dist(tuple(stub.segments[-1]["end"]), (near.x, near.y)) < 1e-3


def test_a_pad_wider_than_the_fanout_reach_fans_out_past_its_copper() -> None:
    """G2d round 4, no via-in-pad: a thermal slug every fixed rung sits
    inside fans out to a pad-scaled rung site just off its own keep-out
    ring — a fanout (``fanout_fallback``), not a capped escape, and the
    site is really emitted."""
    big = {
        "name": "SLUG",
        "pads": [
            {"number": "1", "shape": "rect", "x": 0.0, "y": 0.0, "w": 5.0, "h": 5.0}
        ],
        "pin_map": {"1": "1"},
    }
    batch = {
        "footprints": [big, _P2],
        "components": [
            {
                "refdes": "U1",
                "label": "u",
                "footprint": "SLUG",
                "x": 12.0,
                "y": 10.0,
                "pins": [{"name": "1"}],
            },
            _part("R1", 26.0, 10.0),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
            {"name": "S", "current": 0.001},
        ],
        "connections": [
            {"net": "GND", "refdes": "U1", "pin": "1"},
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "S", "refdes": "R1", "pin": "2"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 34.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    assert stage.plan.fanout_missing == ()
    gnd = list(ir.net_name).index("GND")
    # the slug is sited by the pad-scaled rung — a fanout, never an
    # escape: were the rung dropped the pad could only report on
    # fanout_escape (or stay missing)
    slug = next((k, p) for k, p in stage.plan.fanout_fallback if k == gnd)
    assert slug not in stage.plan.fanout_escape
    assert slug not in stage.plan.fanout_missing_len
    slug_poly = Polygon([(9.5, 7.5), (14.5, 7.5), (14.5, 12.5), (9.5, 12.5)])
    vias = [v for v in stage.result.vias if v.net_id == gnd]
    near = min(vias, key=lambda v: math.hypot(v.x - 12.0, v.y - 10.0))
    web = pcb_capabilities.via_mask_web_mm(design.fab_caps)
    # just outside the pad's own copper plus the soldermask web, within
    # the scaled ladder's longest reach — and joined by a straight stub
    gap = slug_poly.distance(Point(near.x, near.y))
    assert gap >= near.dia_mm / 2.0 + web - 1e-6
    assert math.hypot(near.x - 12.0, near.y - 10.0) < 7.0
    stub = min(
        (
            t
            for t in stage.result.tracks
            if t.net_id == gnd
            and math.dist(tuple(t.segments[0]["start"]), (12.0, 10.0)) < 1e-3
        ),
        key=lambda t: t.length_mm,
        default=None,
    )
    assert stub is not None and len(stub.segments) == 1


def test_an_escape_past_the_cap_is_the_recorded_finding() -> None:
    """G2d: an escape whose only legal corridor is longer than
    ``_ESCAPE_LEN_CAP_MM`` — measured from where the stub leaves the
    pad's copper, not its centre — is no fanout: the pad is the named
    finding, recording the length it would have needed."""
    pad_c = (11.5, 10.0)  # R1's pad 1, west of its (12, 10) centre
    batch = {
        "footprints": [_P2],
        "components": [
            _part("R1", 12.0, 10.0),
            _part("R2", 26.0, 10.0),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
        ],
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R2", "pin": "2"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
            # a full annulus around R1.1 barring the via layers to
            # r=13: the F.Cu wire is free, but the nearest plantable
            # point is a ~13 mm stub — past the 10.0 mm cap. R2 sits
            # clear of the annulus (its pads are >=14 mm off R1.1).
            _inner_layer_horseshoe(*pad_c, 0.8, 13.0, 0.0),
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    gnd = list(ir.net_name).index("GND")
    missing = [p for k, p in stage.plan.fanout_missing if k == gnd]
    assert len(missing) == 1
    assert stage.plan.fanout_escape == ()
    assert stage.plan.fanout_missing_len[(gnd, missing[0])] > 10.0


def test_an_escape_onto_an_isolated_pour_is_the_named_finding() -> None:
    """G2d round 6: an escape counts only if its via joins the net's
    plane. Four rect keepouts wall the whole board off from one pocket
    east of R1's pad on the via layers, so the In1 pour inside it is a
    floating sliver the bare-island drop removes — a stub ending there
    anchors an island, it does not reach GND. The pad stays the named
    finding (``isolated_pour``) and no copper is emitted."""
    pad_c = (11.5, 10.0)  # R1's pad 1, west of its (12, 10) centre
    vias_barred = ["In1.Cu", "In2.Cu", "B.Cu"]

    def band(x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
        """A rect keepout on the via layers — a through via spans all
        layers, so these walls bar every plantable point they cover
        while the F.Cu escape wire stays free."""
        return {
            "ftype": "keepout",
            "geom": {"poly": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]},
            "layers": vias_barred,
        }

    batch = {
        "footprints": [_P2],
        "components": [
            _part("R1", 12.0, 10.0),
            _part("R2", 24.0, 10.0),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
        ],
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R2", "pin": "2"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
            # pocket x∈[15,18] × y∈[8.5,11.5] — the only place a via
            # may sit (3-6 mm stubs), and the pour inside it holds
            # nothing: every site an escape plants only anchors the
            # island
            band(0.0, 0.0, 30.0, 8.5),
            band(0.0, 11.5, 30.0, 20.0),
            band(0.0, 8.5, 15.0, 11.5),
            band(18.0, 8.5, 30.0, 11.5),
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    gnd = list(ir.net_name).index("GND")
    missing = [p for k, p in stage.plan.fanout_missing if k == gnd]
    assert missing
    assert stage.plan.fanout_escape == ()
    assert {stage.plan.fanout_missing_reason[(gnd, p)] for p in missing} == {
        "isolated_pour"
    }


def test_a_pad_caged_on_its_own_layer_is_the_named_finding() -> None:
    """G2d's residue: a keepout that bars the pad's own layer too leaves
    the escape no seed — the pad stays the named finding."""
    batch = {
        "footprints": [_P2],
        "components": [
            _part("R1", 8.0, 10.0),
            _part("R2", 24.0, 10.0),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
            {"name": "S", "current": 0.001},
        ],
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R2", "pin": "2"},
            {"net": "S", "refdes": "R1", "pin": "2"},
            {"net": "S", "refdes": "R2", "pin": "1"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
            # every layer barred — no ring site AND no wire out
            {
                "ftype": "keepout",
                "geom": {
                    "poly": [
                        [4.0, 6.0],
                        [12.0, 6.0],
                        [12.0, 14.0],
                        [4.0, 14.0],
                        [4.0, 6.0],
                    ]
                },
            },
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    assert len(stage.plan.fanout_missing) == 1
    assert stage.plan.fanout_escape == ()
    assert stage.rows["GND"]["status"] == "failed"


def test_only_a_ring_site_falls_back_and_is_counted() -> None:
    """A pad whose whole ring-clear annulus is keepout can only be
    sited by the nearest-first fallback — taken (a ring opened at the
    via beats no via) and counted on ``plan.fanout_fallback``."""
    pad_c = (11.5, 10.0)  # R1's pad 1, west of its (12, 10) centre
    batch = {
        "footprints": [_P2],
        "components": [
            _part("R1", 12.0, 10.0),
            _part("R2", 24.0, 10.0),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
            {"name": "S", "current": 0.001},
        ],
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R2", "pin": "2"},
            {"net": "S", "refdes": "R1", "pin": "2"},
            {"net": "S", "refdes": "R2", "pin": "1"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
            # every ring-clear site around R1.1 (r >= ~1.15) inside the
            # keepout; the +-10deg wedge points at R1's own body so its
            # other pad — and the stub through it — stay reachable
            _horseshoe_keepout(*pad_c, 1.25, 4.0, 10.0),
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    assert stage.plan.fanout_missing == ()
    assert len(stage.plan.fanout_fallback) == 1
    gnd = list(ir.net_name).index("GND")
    vias = [v for v in stage.result.vias if v.net_id == gnd]
    near = min(math.hypot(v.x - pad_c[0], v.y - pad_c[1]) for v in vias)
    assert near < 1.0


def test_the_escape_keeps_clearance_off_routed_copper() -> None:
    """G2d round 3: a routed signal track between the pad and its
    nearest via site is copper the escape must keep off — the stub
    routes around it or not at all, never through it. T's wire crosses
    the wedge corridor's east exit outright (round 6 — before it, this
    test passed with every routed-copper check disabled: the track was
    never in the corridor)."""
    pad_c = (11.5, 10.0)  # R1's pad 1, west of its (12, 10) centre
    batch = {
        "footprints": [_P2],
        "components": [
            _part("R1", 12.0, 10.0),
            _part("R2", 24.0, 5.0),
            _part("R3", 14.5, 8.5),
            _part("R4", 16.5, 11.5),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
            {"name": "S", "current": 0.001},
            {"name": "T", "current": 0.001},
        ],
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R2", "pin": "2"},
            {"net": "S", "refdes": "R1", "pin": "2"},
            {"net": "S", "refdes": "R2", "pin": "1"},
            {"net": "T", "refdes": "R3", "pin": "1"},
            {"net": "T", "refdes": "R4", "pin": "1"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
            # same horseshoe as the escape test; T runs (14,8.5)->
            # (16,11.5) straight through the wedge's east exit — an
            # unguarded stub cuts through it (measured: distance 0)
            _inner_layer_horseshoe(*pad_c, 0.8, 4.0, 10.0),
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    assert stage.plan.fanout_missing == ()
    assert len(stage.plan.fanout_escape) == 1
    names = list(ir.net_name)
    gnd, t_net = names.index("GND"), names.index("T")
    stub = min(
        (
            t
            for t in stage.result.tracks
            if t.net_id == gnd
            and math.dist(tuple(t.segments[0]["start"]), pad_c) < 1e-3
        ),
        key=lambda t: t.length_mm,
        default=None,
    )
    assert stub is not None
    rules = pcb_session.route_net_rules(design.graph, design.fab_caps)
    need = rules["GND"].clearance_mm
    spts = [tuple(s["start"]) for s in stub.segments] + [
        tuple(stub.segments[-1]["end"])
    ]
    from shapely.geometry import LineString

    stub_ln = LineString(spts)
    for t in stage.result.tracks:
        if t.net_id != t_net or t.layer != stub.layer:
            continue
        tpts = [tuple(s["start"]) for s in t.segments] + [tuple(t.segments[-1]["end"])]
        gap = stub_ln.distance(LineString(tpts)) - stub.width_mm / 2 - t.width_mm / 2
        assert gap >= need - 1e-6, f"stub vs T track: gap {gap:.4f} < {need}"


def _fcu_walls(
    x_dead: float,
) -> list[dict[str, Any]]:
    """F.Cu keepouts leaving only the y∈[8.5,11.5] channel between a
    west wall at x=10.8 and a dead end at ``x_dead`` — the escape's
    corridor and every plantable point live inside it."""

    def band(x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
        return {
            "ftype": "keepout",
            "geom": {"poly": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]},
            "layers": ["F.Cu"],
        }

    return [
        band(0.0, 0.0, 22.0, 8.5),
        band(0.0, 11.5, 22.0, 20.0),
        band(0.0, 8.5, 10.8, 11.5),
        band(x_dead, 8.5, 22.0, 11.5),
    ]


def test_an_other_layer_leg_does_not_wall_the_escape() -> None:
    """G2d round 8 (review A): the routed-copper checks price only legs
    on the escape's own layer. T wires B.Cu straight across the
    corridor's single exit — with the ``llay == lay`` filter stripped
    the stub cannot cross it, and every site this side of the leg is
    inside the annulus or its shadow, so the pad goes missing."""
    pad_c = (11.5, 10.0)  # R1's pad 1, west of its (12, 10) centre
    batch = {
        "footprints": [_P2],
        "components": [
            _part("R1", 12.0, 10.0),
            _part("R2", 26.0, 10.0),
            {**_part("R3", 16.3, 4.0), "layer": "bottom"},
            {**_part("R4", 16.3, 16.0), "layer": "bottom"},
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
            {"name": "T", "current": 0.001},
        ],
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R2", "pin": "2"},
            {"net": "T", "refdes": "R3", "pin": "1"},
            {"net": "T", "refdes": "R4", "pin": "1"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
            # full annulus to r=4.8 on the via layers: every ring site
            # and every pocket site short of x≈16.75 is barred — the
            # only sites sit past T's B.Cu leg at ~16.8, reachable only
            # by crossing it on F.Cu
            _inner_layer_horseshoe(*pad_c, 0.8, 4.8, 0.0),
            *_fcu_walls(18.5),
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    assert stage.plan.fanout_missing == ()
    assert len(stage.plan.fanout_escape) == 1
    gnd = list(ir.net_name).index("GND")
    stub = min(
        (
            t
            for t in stage.result.tracks
            if t.net_id == gnd
            and math.dist(tuple(t.segments[0]["start"]), pad_c) < 1e-3
        ),
        key=lambda t: t.length_mm,
        default=None,
    )
    assert stub is not None
    assert float(stub.segments[-1]["end"][0]) > 17.0


def _reuse_pocket_batch() -> dict[str, Any]:
    """Only the authored barrel fits the narrow pocket east of R1."""
    pad_c = (11.5, 10.0)  # R1's pad 1, west of its (12, 10) centre
    batch = {
        "footprints": [_P2],
        "components": [
            _part("R1", 12.0, 10.0),
            _part("R2", 26.0, 10.0),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
        ],
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R2", "pin": "2"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
            _inner_layer_horseshoe(*pad_c, 0.8, 4.0, 0.0),
            # pocket around the authored GND via at (16.1,10): the dead
            # band ends the channel at x=16.5 and two via-layer strips
            # bar every fresh site except a +-0.2mm lane at y=10 —
            # inside the authored barrel's own drill-wall shadow, so
            # ending ON it is the only answer the escape can give
            {
                "ftype": "keepout",
                "geom": {"poly": [[15.5, 8.5], [16.5, 8.5], [16.5, 9.8], [15.5, 9.8]]},
                "layers": ["In1.Cu", "In2.Cu", "B.Cu"],
            },
            {
                "ftype": "keepout",
                "geom": {
                    "poly": [
                        [15.5, 10.2],
                        [16.5, 10.2],
                        [16.5, 11.5],
                        [15.5, 11.5],
                    ]
                },
                "layers": ["In1.Cu", "In2.Cu", "B.Cu"],
            },
            *_fcu_walls(16.5),
        ],
        "fixed_copper": [
            {
                "ctype": "via",
                "net": "GND",
                "x": 16.1,
                "y": 10.0,
                "dia_mm": 0.6,
                "drill_mm": 0.3,
            }
        ],
    }
    return batch


def test_an_escape_may_end_on_an_existing_same_net_via() -> None:
    """The escape ends on the pocket's authored via without a new drill."""
    pad_c = (11.5, 10.0)
    design = local._design(_reuse_pocket_batch(), None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    assert stage.plan.fanout_missing == ()
    assert len(stage.plan.fanout_escape) == 1
    gnd = list(ir.net_name).index("GND")
    stub = min(
        (
            t
            for t in stage.result.tracks
            if t.net_id == gnd
            and math.dist(tuple(t.segments[0]["start"]), pad_c) < 1e-3
        ),
        key=lambda t: t.length_mm,
        default=None,
    )
    assert stub is not None
    # the stub ends ON the authored barrel — no second via beside it
    assert math.dist(tuple(stub.segments[-1]["end"]), (16.1, 10.0)) < 1e-3
    assert not [
        v for v in stage.result.vias if math.hypot(v.x - 16.1, v.y - 10.0) < 0.5
    ]
    model = pcb_session._copper_model(
        ir,
        pcb_realize.result_copper_rows(
            stage.result, ir, design.layer_names, derived=True
        )
        + [{**row, "net": row.get("net") or ""} for row in design.fixed_copper],
        footprints=fps,
    )
    errors = [
        f.detail
        for f in pcb_drc.run_geometric_drc(
            model,
            capability=design.fab_caps,
            outline=[list(p) for p in (design.outline or [])],
            net_rules=pcb_session.clearance_net_rules(design.graph, design.fab_caps),
        )
        if f.rule == "via_via_keepout" and f.severity == "error"
    ]
    assert errors == []


@pytest.mark.parametrize(
    "span,joined",
    [
        pytest.param(["F.Cu", "B.Cu"], True, id="through"),
        pytest.param(["F.Cu", "In1.Cu"], False, id="misses-pour"),
        pytest.param(["In1.Cu", "In2.Cu"], False, id="misses-stub"),
    ],
)
def test_reuse_must_span_the_stub_and_the_joined_pour(
    span: list[str], joined: bool
) -> None:
    """XY contact with In2 copper cannot connect a blind or buried via.

    Exercise the emitter directly so final-pour verification cannot mask
    either span guard. The pocket is independently joined on In2; the
    through control proves a legal reuse exists at precisely the same XY.
    """
    batch = _reuse_pocket_batch()
    batch["features"][1]["layer"] = "In2.Cu"
    batch["fixed_copper"][0]["span"] = span
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    td = design_from_ir(
        ir,
        fps,
        features=design.features,
        fixed_copper=design.fixed_copper,
        route_planes=True,
    )
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    out = topo_realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        {"GND": NetRules(0.15, 0.15, 0.45, 0.2)},
        RealizeResult((), (), ()),
        int(ir.n_layers),
        joined_pours={
            k: [(2, Polygon([(15.5, 9.8), (16.5, 9.8), (16.5, 10.2), (15.5, 10.2)]))]
        },
        fixed_copper=design.fixed_copper,
    )
    tracks, vias, missing, done, _need, reasons, spans = out
    assert vias == []
    if joined:
        assert missing == []
        assert done == [(k, pin)]
        assert math.dist(tuple(tracks[0].segments[-1]["end"]), (16.1, 10.0)) < 1e-6
        assert spans[(k, pin)] == (0, 3)
    else:
        assert tracks == []
        assert done == []
        assert missing == [(k, pin)]
        assert reasons[(k, pin)] in {"isolated_pour", "no_site"}


def _escape_case(
    positions: tuple[tuple[float, float], ...] = (
        (5.0, 10.0),
        (27.0, 16.0),
        (25.0, 3.0),
        (28.0, 3.0),
    ),
) -> tuple[Any, Any]:
    """A synthetic GND pad at (5,10), with every other pad far away."""
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
            {**_part(ref, x, y), "footprint": "P1", "pins": [{"name": "1"}]}
            for ref, (x, y) in zip(("R1", "R2", "R3", "R4"), positions, strict=True)
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.001},
            {"name": "S", "current": 0.001},
        ],
        "connections": [
            {"net": net, "refdes": ref, "pin": "1"}
            for net, ref in [("GND", "R1"), ("GND", "R2"), ("S", "R3"), ("S", "R4")]
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    return design_from_ir(ir, fps, features=design.features, route_planes=True), ir


def _escape_mesh(td: Any, verts: list[list[float]]) -> TopoMesh:
    """Two triangles sharing portal 1–2; pin 0 is the start vertex."""
    return TopoMesh(
        verts=np.asarray(verts),
        tris=np.asarray([[0, 1, 2], [1, 3, 2]], np.int32),
        edges=np.asarray([[0, 1], [0, 2], [1, 2], [1, 3], [2, 3]], np.int32),
        tri_edges=np.asarray([[0, 2, 1], [3, 4, 2]], np.int32),
        edge_tris=np.asarray([[0, -1], [0, -1], [0, 1], [1, -1], [1, -1]], np.int32),
        constrained=np.zeros(5, dtype=bool),
        pad_vertex={},
        pin_vertex=np.zeros(len(td.pin_net), np.int32),
        edge_owner=np.full((5, 2), OWNER_NONE, np.int32),
        edge_side=np.zeros((5, 2), np.int8),
    )


def test_an_over_cap_reuse_does_not_hide_a_shorter_fresh_site() -> None:
    """A 12 mm reuse in the starting triangle is tried before its portal.

    The portal has a legal fresh site under 10 mm away. Rejecting the
    reuse after fanout_escape returns loses that site; rejecting it in
    verify lets the actual search continue through the same triangle.
    """
    td, ir = _escape_case()
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    mesh = _escape_mesh(td, [[5.0, 10.0], [8.0, 6.0], [20.0, 8.0], [20.0, 4.0]])
    prior = RealizedVia(
        -1, list(ir.net_name).index("GND"), 17.0, 8.0, 0.45, 0.2, 0, 3, "a"
    )
    tracks, vias, missing, done, need, _reasons, _spans = topo_realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        {"GND": NetRules(0.1, 0.1, 0.45, 0.2)},
        RealizeResult((), (prior,), ()),
        int(ir.n_layers),
        mesh=mesh,
    )
    assert missing == []
    assert done == [(k, pin)]
    assert need == {}
    assert len(vias) == 1
    assert tracks[0].length_mm < 10.0
    assert math.dist((vias[0].x, vias[0].y), (prior.x, prior.y)) > 0.75


def test_escape_barrels_keep_the_pairwise_maximum_clearance() -> None:
    """0.45 mm barrels 0.60 mm apart need 0.75 mm at 0.10/0.30 rules.

    The portal's vertex insets leave only a 0.05 mm planting interval.
    Every candidate there clears the narrower stub but
    violates the barrel's pairwise clearance. Dropping the prior via's
    clearance from the emitter's body admits a colliding new barrel.
    """
    td, ir = _escape_case()
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    mesh = _escape_mesh(td, [[5.0, 10.0], [8.0, 9.75], [8.0, 10.25], [11.0, 10.0]])
    prior = RealizedVia(
        -1, list(ir.net_name).index("S"), 8.0, 10.6, 0.45, 0.2, 0, 3, "a"
    )
    tracks, vias, missing, done, _need, reasons, _spans = topo_realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        {"GND": NetRules(0.1, 0.1, 0.45, 0.2), "S": NetRules(0.1, 0.3, 0.45, 0.2)},
        RealizeResult((), (prior,), ()),
        int(ir.n_layers),
        mesh=mesh,
    )
    assert tracks == []
    assert vias == []
    assert done == []
    assert missing == [(k, pin)]
    assert reasons[(k, pin)] == "no_site"


def test_an_interval_endpoint_can_escape_when_its_three_samples_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disc blocks midpoint/quarter rays, leaving the portal ends free.

    Fix the planting candidate near an endpoint to exercise interval
    admission independently of the planter's midpoint-first ordering.
    The emitter's real geometry checks still judge the complete stub.
    """
    td, ir = _escape_case()
    k = list(td.net_name).index("GND")
    pin = int(td.net_pins[k][0])
    mesh = _escape_mesh(td, [[5.0, 10.0], [8.0, 6.0], [8.0, 14.0], [11.0, 10.0]])
    prior = RealizedVia(
        -1, list(ir.net_name).index("S"), 6.5, 10.0, 0.45, 0.2, 0, 3, "a"
    )

    def point(_self: Any, edge: int, *_args: Any, **_kw: Any) -> Any:
        return (8.0, 6.1, 0.0125) if edge == 2 else None

    monkeypatch.setattr(topo_wires._ViaPlanter, "point", point)
    tracks, vias, missing, done, _need, _reasons, _spans = topo_realise._emit_escapes(
        td,
        ir,
        [(k, pin)],
        td.pose0,
        {"GND": NetRules(0.1, 0.1, 0.45, 0.2), "S": NetRules(0.1, 0.75, 0.45, 0.2)},
        RealizeResult((), (prior,), ()),
        int(ir.n_layers),
        mesh=mesh,
    )
    assert missing == []
    assert done == [(k, pin)]
    assert len(vias) == 1
    assert math.dist(tuple(tracks[0].segments[-1]["end"]), (8.0, 6.1)) < 1e-6


@pytest.mark.parametrize("empty", [True, False], ids=["empty-pour", "severed-neck"])
def test_final_pour_rechecks_escapes_after_later_antipads(
    monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    """Two escapes join initially, then their antipads destroy those joins.

    Use the real pour and join code; control only the fanout candidates
    to isolate final verification from candidate-search heuristics. Thin
    1.3×0.15 mm regions disappear below the 0.05 mm² fragment cutoff.
    Extending their ends leaves islands with severed necks instead.
    """
    td, ir = _escape_case(((13.0, 10.0), (27.0, 16.0), (7.0, 10.0), (28.0, 3.0)))
    g, s = list(td.net_name).index("GND"), list(td.net_name).index("S")
    gi, si = list(ir.net_name).index("GND"), list(ir.net_name).index("S")
    ir.net_plane_layers[si] = 1 << 2
    left, right = (9.35, 11.3) if empty else (9.0, 11.65)
    gx, sx = (10.65, 10.0) if empty else (10.9, 9.75)
    ir.plane_regions[(gi, 1)] = (
        [[left, 9.925], [gx, 9.925], [gx, 10.075], [left, 10.075]],
        0,
    )
    ir.plane_regions[(si, 2)] = (
        [[sx, 9.925], [right, 9.925], [right, 10.075], [sx, 10.075]],
        0,
    )
    fixed = [
        {
            "ctype": "track",
            "net": net,
            "layer": lay,
            "width_mm": 0.05,
            "segments": [{"shape": "line", "start": [x, 10.0], "end": [x2, 10.0]}],
        }
        for net, lay, x, x2 in [
            ("GND", "In1.Cu", left, left + 0.1),
            ("S", "In2.Cu", right - 0.1, right),
        ]
    ]
    deferred = [(g, int(td.net_pins[g][0])), (s, int(td.net_pins[s][0]))]
    sites = [(gx, 10.0), (sx, 10.0)]
    tracks = [
        RealizedTrack(
            -1,
            ni,
            0,
            ({"shape": "line", "start": start, "end": list(site)},),
            math.dist(start, site),
            None,
            0.1,
        )
        for ni, start, site in [
            (gi, [13.0, 10.0], sites[0]),
            (si, [7.0, 10.0], sites[1]),
        ]
    ]
    vias = [
        RealizedVia(-1, ni, *site, 0.45, 0.2, 0, 3, "a")
        for ni, site in [(gi, sites[0]), (si, sites[1])]
    ]
    rules = {"GND": NetRules(0.1, 0.15, 0.45, 0.2), "S": NetRules(0.1, 0.15, 0.45, 0.2)}
    pours = topo_realise._plane_pours(
        ir, tracks, vias, td, td.pose0, rules, {}, fixed, 0.0
    )
    assert bool(pours) is not empty
    if not empty:
        # Both far-side islands survive and contain the emitted vias;
        # neither is connected to its original authored anchor anymore.
        assert all(
            any(
                row["net"] == net
                and pcb_realize._pour_polygon(row).intersects(Point(site))
                for row in pours
            )
            for net, site in zip(("GND", "S"), sites, strict=True)
        )
    # In the neck case a far-side escape anchors its own island. It
    # must not serve as the primitive proving that same island joined.
    joined = topo_realise._joined_plane_pieces(
        td,
        ir,
        {g: 1, s: 2},
        [],
        [],
        td.pose0,
        rules,
        {},
        fixed,
        0.0,
        pours=pours,
    )
    assert all(
        not any(poly.intersects(Point(site)) for _lay, poly in joined[k])
        for (k, _pin), site in zip(deferred, sites, strict=True)
    )

    def ring_candidates(*_args: Any, **_kw: Any) -> Any:
        return [], [], [], deferred

    def escape_candidates(*_args: Any, **kw: Any) -> Any:
        # Both targets are joined before either escape exists.
        assert all(
            any(poly.intersects(Point(site)) for _lay, poly in kw["joined_pours"][k])
            for (k, _pin), site in zip(deferred, sites, strict=True)
        )
        return (
            tracks,
            vias,
            [],
            deferred.copy(),
            {},
            {},
            {kp: (0, 3) for kp in deferred},
        )

    monkeypatch.setattr(topo_realise, "_fanout_vias", ring_candidates)
    monkeypatch.setattr(topo_realise, "_emit_escapes", escape_candidates)
    plan, result = topo_realise.route_design(
        td, ir, net_rules=rules, footprints={}, fixed_copper=fixed
    )
    assert set(plan.fanout_missing) == set(deferred)
    assert plan.fanout_escape == ()
    assert set(plan.fanout_missing_reason.values()) == {"isolated_pour"}
    assert result.tracks == ()
    assert result.vias == ()
    # Removing the rejected escapes restores the original anchored strips.
    assert {row["net"] for row in result.pours} == {"GND", "S"}


def test_an_escape_may_end_on_an_emitted_same_net_via() -> None:
    """G2d round 8 (Fable A's round-7 guard + the reuse ruling on an
    emitted barrel): a via the fanout pass planted rides the emit's
    collision view and the reuse offer alike. R5's pad faces west, so
    its fanout via lands at (16.05,10) — squarely on the wedge exit's
    cheapest escape site. The escape ends ON it: no second barrel. With
    the fanout copper stripped from the emit's rres (the honest-removal
    check) a fresh barrel plants 0.049 mm off it — a real drill-wall
    collision."""
    pad_c = (11.5, 10.0)  # R1's pad 1, west of its (12, 10) centre
    # Keep the direct reuse corridor open. The second, unwired R1 land
    # otherwise bars that corridor; outward frozen rings changed which
    # mesh portal wins before the winding route reaches the old barrel.
    single = {**_P2, "name": "P1-west", "pads": _P2["pads"][:1], "pin_map": {"1": "1"}}
    batch = {
        "footprints": [_P2, single],
        "components": [
            {
                **_part("R1", 12.0, 10.0),
                "footprint": "P1-west",
                "pins": [{"name": "1"}],
            },
            _part("R5", 18.5, 10.0),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
        ],
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R5", "pin": "1"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
            _inner_layer_horseshoe(*pad_c, 0.8, 4.0, 0.0),
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    assert stage.plan.fanout_missing == ()
    assert len(stage.plan.fanout_escape) == 1
    gnd = list(ir.net_name).index("GND")
    vias = [v for v in stage.result.vias if v.net_id == gnd]
    # reuse: exactly one barrel — the escape ended on R5's fanout via
    assert len(vias) == 1
    stub = min(
        (
            t
            for t in stage.result.tracks
            if t.net_id == gnd
            and math.dist(tuple(t.segments[0]["start"]), pad_c) < 1e-3
        ),
        key=lambda t: t.length_mm,
        default=None,
    )
    assert stub is not None
    assert math.dist(tuple(stub.segments[-1]["end"]), (vias[0].x, vias[0].y)) < 1e-3


def test_a_pour_piece_counts_a_pad_only_by_its_real_copper() -> None:
    """G2d round 8 (Codex #2): a fragment near a pad's centre but off
    its real copper does not join the piece — the half-diagonal reach
    admitted a 4x1 pad's corners' worth of air."""
    from precis.pcb.topoplace.realise import _pour_piece_joined

    pad = Polygon([(-2.0, -0.5), (2.0, -0.5), (2.0, 0.5), (-2.0, 0.5)])
    rows = {0: [("GND", pad, 0)]}
    joined = _pour_piece_joined
    touching = Point(0.0, 0.6).buffer(0.3)  # overlaps the rect's top edge
    floating = Point(0.0, 1.4).buffer(
        0.3
    )  # 1.4mm off centre — inside 2.06 reach, off the rect
    assert joined(touching, 0, "F.Cu", "GND", 0, [], [], rows, [], ["F.Cu"])
    assert not joined(floating, 0, "F.Cu", "GND", 0, [], [], rows, [], ["F.Cu"])


def test_the_escape_keeps_clearance_off_a_foreign_pad() -> None:
    """G2d round 8 (Codex #4): the whole-polyline verify checks foreign
    pads too, at the PAIR's clearance — the edge-interval cover prices
    every land at the escape's own rule only, so a pad whose net asks
    more is a wall the corridor cannot see. S's class clearance is
    0.5 mm against GND's 0.15: with the pad check stripped the emit
    produces a stub 0.40 mm off the land (measured), a real violation
    the corridor's own-rule model calls legal."""
    pad_c = (11.5, 10.0)  # R1's pad 1, west of its (12, 10) centre
    batch = {
        "footprints": [_P2],
        "components": [
            _part("R1", 12.0, 10.0),
            _part("R2", 24.0, 10.0),
            _part("R3", 13.6, 10.4),
            _part("R4", 25.0, 3.0),
        ],
        "nets": [
            {"name": "GND", "class": "gnd", "current": 0.1},
            {"name": "S", "net_class": "wide", "current": 0.001},
        ],
        "net_classes": {"wide": {"clearance_mm": 0.5}},
        "connections": [
            {"net": "GND", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "R2", "pin": "2"},
            {"net": "S", "refdes": "R3", "pin": "1"},
            {"net": "S", "refdes": "R4", "pin": "1"},
        ],
        "features": [
            closed_outline(0.0, 0.0, 30.0, 20.0),
            {"ftype": "plane_net", "layer": "In1.Cu", "net": "GND"},
            _inner_layer_horseshoe(*pad_c, 0.8, 4.0, 10.0),
        ],
    }
    design = local._design(batch, None)
    ir, fps, _specs = local._prepare(design)
    stage = local._route_stage(design, ir, fps)
    gnd = list(ir.net_name).index("GND")
    rules = pcb_session.route_net_rules(design.graph, design.fab_caps)
    need = rules["GND"].track_width_mm / 2.0 + max(
        rules["GND"].clearance_mm, rules["S"].clearance_mm
    )
    stub = min(
        (
            t
            for t in stage.result.tracks
            if t.net_id == gnd
            and math.dist(tuple(t.segments[0]["start"]), pad_c) < 1e-3
        ),
        key=lambda t: t.length_mm,
        default=None,
    )
    if stub is None:
        assert stage.plan.fanout_missing
        return
    spts = [tuple(s["start"]) for s in stub.segments] + [
        tuple(stub.segments[-1]["end"])
    ]
    from shapely.geometry import LineString

    stub_ln = LineString(spts)
    s_pad = Point(13.1, 10.4).buffer(0.25)  # R3 pad 1, 0.5x0.5 at (13.6-0.5)
    assert stub_ln.distance(s_pad) >= need - 1e-6


@pytest.mark.parametrize("rot_q", [0, 1, 2, 3])
@pytest.mark.parametrize("side", [0.0, 1.0])
def test_world_to_part_local_inverts_local_to_world(rot_q: int, side: float) -> None:
    """td569572: the synthetic via-landing pin's placement depends on
    this being an exact inverse, not an approximation — every quarter
    turn and both part sides round-trip."""
    from precis.pcb.topoplace import proxy as topo_proxy
    from precis.pcb.topoplace.realise import _world_to_part_local

    pose_row = np.asarray([5.0, -3.0, float(rot_q), side])
    for lx, ly in [(0.0, 0.0), (1.5, -2.25), (-4.0, 3.0)]:
        wx, wy = topo_proxy.local_to_world(np.asarray([[lx, ly]]), pose_row)[0]
        rx, ry = _world_to_part_local((float(wx), float(wy)), pose_row)
        assert math.isclose(rx, lx, abs_tol=1e-9)
        assert math.isclose(ry, ly, abs_tol=1e-9)
