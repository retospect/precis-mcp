"""Slice 5 (phase 4): the topo realiser — per-branch layer assignment
(Decision 6's pad-side forcing + crossing yield), via sites at
``(edge_id, t)``, and copper emitted through the normal
``RealizeResult``/``to_gerber_model`` path. The sanity board's
acceptance is the spec's: every wire gets a layer, every via a site,
and the emitted copper passes DRC with 0 errors."""

from __future__ import annotations

import dataclasses
import itertools
import math
import time
from types import SimpleNamespace

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

import precis_geom.pcb as pcb
from precis.pcb import drc
from precis.pcb import local as kernel
from precis.pcb import realize as pcb_realize
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import PcbIR, from_graph
from precis.pcb.session import clearance_net_rules, route_net_rules
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace import wires as wires_mod
from precis.pcb.topoplace.design import TopoDesign, TopoZone
from precis.pcb.topoplace.mesh import TopoMesh, build_mesh
from precis.pcb.topoplace.realise import (
    RoutePiece,
    RoutePlan,
    _guarded_nets,
    _run_bounds,
    _severed_nets,
    _site_via,
    assign_layers,
    plan_route,
    realize_result,
    route_design,
    sketch_rows,
    widen_power,
    widen_targets,
)
from precis.pcb.topoplace.wires import WirePath, Wires, build_wires
from tests.fixtures.pcb.topo_example_a import example_a


def _sanity_board() -> pcb.Board:
    """Three two-pin nets on a 30x20 board: two top-SMD nets and one
    F↔B net (C2 mounts bottom) — the F↔B branch must mint a mid-wire
    via site; nothing crosses, so 0 DRC is the honest expectation."""
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    b = pcb.Board("sanity", stackup=4, outline=pcb.rect(30.0, 20.0))
    f = b.footprint(fp)
    a1 = b.part("A1", footprint=f, pins=["1", "2"]).at(5.0, 5.0)
    a2 = b.part("A2", footprint=f, pins=["1", "2"]).at(15.0, 5.0)
    c1 = b.part("C1", footprint=f, pins=["1", "2"]).at(5.0, 15.0)
    c2 = b.part("C2", footprint=f, pins=["1", "2"]).at(15.0, 15.0, side="bottom")
    e1 = b.part("E1", footprint=f, pins=["1", "2"]).at(25.0, 5.0)
    e2 = b.part("E2", footprint=f, pins=["1", "2"]).at(25.0, 15.0)
    # inner pins: the wire must not pass the part's unwired outer pad
    # (a bottom-side part mirrors pin x — C2's inner pad is "2")
    b.wire(a1["2"], a2["1"], name="N1")
    b.wire(c1["2"], c2["2"], name="N2")
    b.wire(e1["1"], e2["1"], name="N3")
    return b


def test_sanity_board_every_wire_layered_every_via_sited_zero_drc() -> None:
    board = _sanity_board()
    design = kernel._design(board, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints, net_classes=design.graph.get("net_classes"))
    mesh = build_mesh(td, td.pose0)
    wires = build_wires(mesh, td, ir.n_layers)
    plan = plan_route(td, mesh, wires, ir.n_layers)

    # every wire a layer
    assert len(plan.layers) == len(wires.branches)
    assert {p.branch for p in plan.pieces} == set(range(len(wires.branches)))
    # the F↔B branch mints a mid-wire via, split between the two outers
    assert len(plan.vias) == 1
    (via,) = plan.vias
    assert 0.0 <= via.t <= 1.0 and via.edge < len(mesh.edges)
    fb = [p for p in plan.pieces if p.branch == via.branch]
    assert {p.layer for p in fb} == {0, ir.n_layers - 1}
    assert not plan.crossings and not plan.unreachable

    # emitted copper through the normal realise path — 0 DRC errors
    res = realize_result(plan, td, ir, ir.n_layers)
    layers = [str(layer.get("name") or "") for layer in ir.stackup]
    model = pcb_realize.to_gerber_model(
        res, ir, layers=layers, outline=[list(p) for p in (ir.outline or [])]
    )
    findings = drc.run_geometric_drc(
        model, capability=capability_for("4layer"), outline=model["outline"]
    )
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], [f"{f.rule}: {f.detail}" for f in errors]


def test_returned_plan_carries_no_corridor_overflow_counter() -> None:
    """gr520904 S6 follow-up: ``RoutePlan.corridor_overflow`` was always
    0 — ``route_design`` refuses a non-empty cut record before a plan
    exists, so the field could only ever misreport. The plan drops it;
    the bench column reads the refusal count off the job meta instead."""
    board = _sanity_board()
    design = kernel._design(board, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints, net_classes=design.graph.get("net_classes"))
    plan, _res = route_design(td, ir)
    assert not hasattr(plan, "corridor_overflow")


def test_route_planes_admits_plane_class_nets() -> None:
    """``route_planes=True`` (the slice-5 ruling: GND/power route as
    normal nets) admits a plane-classed net to ``net_pins``; the default
    placer design still drops it so rail airwires never tax the proxy."""
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    b = pcb.Board("plane", stackup=4, outline=pcb.rect(20.0, 10.0))
    f = b.footprint(fp)
    g1 = b.part("G1", footprint=f, pins=["1", "2"]).at(5.0, 5.0)
    g2 = b.part("G2", footprint=f, pins=["1", "2"]).at(15.0, 5.0)
    b.net("GND", cls="gnd").tie(g1["1"], g2["1"])
    s1 = b.part("S1", footprint=f, pins=["1", "2"]).at(5.0, 8.0)
    s2 = b.part("S2", footprint=f, pins=["1", "2"]).at(15.0, 8.0)
    b.wire(s1["2"], s2["1"], name="SIG")

    design = kernel._design(b, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints)
    assert "GND" not in td.net_name

    td_r = design_from_ir(ir, footprints, route_planes=True)
    gnd_k = td_r.net_name.index("GND")
    assert len(td_r.net_pins[gnd_k]) == 2
    mesh = build_mesh(td_r, td_r.pose0)
    wires = build_wires(mesh, td_r, ir.n_layers)
    plan = plan_route(td_r, mesh, wires, ir.n_layers)
    assert gnd_k in {p.net for p in plan.pieces}


def _plane_board() -> pcb.Board:
    """A gnd-classed rail plus a signal net, spaced so the rail CAN
    widen — the widen stage's honest ``where clearance allows`` case."""
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    b = pcb.Board("widen", stackup=4, outline=pcb.rect(30.0, 10.0))
    f = b.footprint(fp)
    g1 = b.part("G1", footprint=f, pins=["1", "2"]).at(8.0, 5.0)
    g2 = b.part("G2", footprint=f, pins=["1", "2"]).at(22.0, 5.0)
    b.net("GND", cls="gnd").tie(g1["2"], g2["1"])
    s1 = b.part("S1", footprint=f, pins=["1", "2"]).at(5.0, 8.5)
    s2 = b.part("S2", footprint=f, pins=["1", "2"]).at(15.0, 8.5)
    # inner pins — the wire must not cross the part's unwired outer pad
    b.wire(s1["2"], s2["1"], name="SIG")
    return b


def _drc_findings(
    result: pcb_realize.RealizeResult,
    ir: PcbIR,
    layers: list[str],
    footprints: dict | None = None,
) -> list:
    model = pcb_realize.to_gerber_model(
        result,
        ir,
        layers=layers,
        outline=[list(p) for p in (ir.outline or [])],
        footprints=footprints,
    )
    return [
        f
        for f in drc.run_geometric_drc(
            model, capability=capability_for("4layer"), outline=model["outline"]
        )
        if f.severity == "error"
    ]


def _drc_errors(result: pcb_realize.RealizeResult, ir: PcbIR, layers: list[str]) -> int:
    return len(_drc_findings(result, ir, layers))


def test_widen_oracle_judges_class_min_clearance(tmp_path) -> None:
    """gr520904 S6 follow-up: ``topo_place``'s widen oracle must run the
    FULL per-net rule map, not the capability floor alone — a rail that
    fits the fab's 0.15 mm floor but breaks a class's hard 0.8 mm
    ``min_clearance_mm`` is refused, not committed. The rail's wire runs
    0.94 mm below a pad of the hv-classed net: fine at the born 0.15 mm
    width (0.865 gap), an oracle without net_rules would still take the
    1.0 mm ladder step (0.44 gap ≥ floor); the class floor forces it
    down to the first step that clears 0.8."""
    fp = pcb.Footprint(
        "SIP2W",
        [
            pcb.pad("1", "rect", -1.0, 0.0, w=0.8, h=1.0),
            pcb.pad("2", "rect", 1.0, 0.0, w=0.8, h=1.0),
        ],
    )
    b = pcb.Board("widen-cls", stackup=4, outline=pcb.rect(30.0, 15.0))
    f = b.footprint(fp)
    g1 = b.part("G1", footprint=f, pins=["1", "2"]).at(8.0, 5.0)
    g2 = b.part("G2", footprint=f, pins=["1", "2"]).at(22.0, 5.0)
    b.net("GND", cls="gnd").tie(g1["2"], g2["1"])
    # hv class: a hard 0.8 mm floor — the pad NEAREST the rail is the
    # classed copper the widened track's gap is judged against.
    s1 = b.part("S1", footprint=f, pins=["1", "2"]).at(15.0, 6.44)
    s2 = b.part("S2", footprint=f, pins=["1", "2"]).at(27.0, 6.44)
    b.net_class("hv", min_clearance_mm=0.8)
    b.net("SIG", cls="hv").tie(s1["2"], s2["1"])

    run = kernel.topo_place(b, effort=200, seed=1, out=str(tmp_path / "topo"))
    assert run.route is not None, run.files.get("route.json")
    widened = run.files["route.json"]["widened"]
    assert widened["GND"] == pytest.approx(0.25)


def test_widen_power_widens_gnd_where_clearance_allows() -> None:
    """The post-route stage's live case: the gnd rail claims its
    current-derived target where the board's own DRC scores the trial
    clean; the signal net keeps its width verbatim."""
    board = _plane_board()
    design = kernel._design(board, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints, route_planes=True)
    _plan, res = route_design(td, ir)
    layers = [str(layer.get("name") or "") for layer in ir.stackup]
    gnd_ir = int(list(ir.net_name).index("GND"))

    res2, widths = widen_power(
        res,
        {gnd_ir},
        drc_findings=lambda r: _drc_findings(r, ir, layers),
        targets={gnd_ir: 1.0},
    )
    assert widths[gnd_ir] > 0.15  # left the default rule width
    assert {t.width_mm for t in res2.tracks if t.net_id == gnd_ir} == {widths[gnd_ir]}
    assert _drc_errors(res2, ir, layers) == 0
    sig = [t for t in res2.tracks if t.net_id != gnd_ir]
    assert sig and all(t.width_mm == 0.15 for t in sig)


def test_widen_power_keeps_the_rule_width_when_the_oracle_refuses() -> None:
    """The contract without a board: an accepted step mints no new error
    finding, and a net the oracle always refuses keeps its rule width
    verbatim."""
    tracks = tuple(
        pcb_realize.RealizedTrack(
            seg_id=i,
            net_id=i,
            layer=0,
            segments=(),
            length_mm=1.0,
            blocked_by=None,
            width_mm=0.15,
        )
        for i in range(3)
    )
    res = pcb_realize.RealizeResult(tracks, (), ())

    def errs(r: pcb_realize.RealizeResult) -> list[drc.DrcFinding]:
        # net 1 clears only at <=0.5; net 2 clears nothing above its rule
        return [
            drc.DrcFinding(
                rule="clearance",
                severity="error",
                where=f"synthetic N{t.net_id}",
                detail="",
                objects=({"ctype": "track", "net": f"N{t.net_id}", "layer": "F.Cu"},),
            )
            for t in r.tracks
            if (t.net_id == 1 and t.width_mm > 0.5)
            or (t.net_id == 2 and t.width_mm > 0.15)
        ]

    res2, widths = widen_power(
        res, {0, 1, 2}, drc_findings=errs, targets={0: 1.0, 1: 0.5, 2: 0.2}
    )
    assert widths == {0: 1.0, 1: 0.5, 2: 0.15}
    assert {t.net_id: t.width_mm for t in res2.tracks} == widths


def test_widen_power_net_without_target_takes_the_oracles_widest() -> None:
    """gr524239 review: a net in the widen set whose annotation is
    absent — an unannotated plane-classed rail, the only member
    :func:`widen_targets` admits without a target — keeps the legacy
    ruling: the widest ladder step the oracle accepts."""
    tracks = tuple(
        pcb_realize.RealizedTrack(
            seg_id=i,
            net_id=i,
            layer=0,
            segments=(),
            length_mm=1.0,
            blocked_by=None,
            width_mm=0.15,
        )
        for i in range(2)
    )
    res = pcb_realize.RealizeResult(tracks, (), ())

    def errs(r: pcb_realize.RealizeResult) -> list[drc.DrcFinding]:
        return [
            drc.DrcFinding(
                rule="clearance",
                severity="error",
                where=f"synthetic N{t.net_id}",
                detail="",
                objects=({"ctype": "track", "net": f"N{t.net_id}", "layer": "F.Cu"},),
            )
            for t in r.tracks
            if t.net_id == 1 and t.width_mm > 0.5
        ]

    res2, widths = widen_power(res, {0, 1}, drc_findings=errs, targets={0: 0.4})
    # net 0 claims its annotated target; net 1 (no target) takes the
    # widest step the oracle accepts — 0.5, under the always-failing
    # 1.0/0.8/0.6 steps.
    assert widths == {0: 0.4, 1: 0.5}
    assert [t.width_mm for t in res2.tracks] == [0.4, 0.5]


def test_widen_targets_marks_only_nets_whose_current_needs_more() -> None:
    """gr524239 selection: the widen set is the plane/power-class rails
    plus the nets whose current-derived IPC-2221 width beats the width
    they were born at — a 1 mA signal net asks ~0.09 mm and is never a
    candidate, an unannotated SIGNAL net is never a candidate, a 1.5 A
    rail is (a plane net with an estimate gets only its current-driven
    target — the annotation outranks the class)."""
    caps = capability_for("4layer")
    design = {
        "nets": [
            {"name": "SIG", "est_current_a": 0.001},
            {"name": "NC"},
            {"name": "GND", "net_class": "gnd"},
            {"name": "PWR", "net_class": "power", "est_current_a": 1.5},
            {"name": "PWR1MA", "net_class": "power", "est_current_a": 0.001},
        ],
        "net_classes": {},
    }
    born_rules = route_net_rules(design, caps)
    target_rules = clearance_net_rules(design, caps)
    ir = SimpleNamespace(
        n_nets=5,
        net_name=np.asarray(["SIG", "NC", "GND", "PWR", "PWR1MA"], dtype=object),
        net_class=np.asarray(["", "", "gnd", "power", "power"], dtype=object),
        net_domain=np.asarray(["electrical"] * 5, dtype=object),
        net_current_a=np.asarray([0.001, math.nan, math.nan, 1.5, 0.001]),
    )
    power_ids, targets = widen_targets(ir, born_rules, target_rules)
    assert power_ids == (2, 3, 4)
    assert targets[3] > born_rules["PWR"].track_width_mm
    # the 1 mA net got a target (it IS annotated) but the target is
    # below its born width — it stays out of the widen set
    assert targets[0] <= born_rules["SIG"].track_width_mm
    assert 1 not in targets
    # the unannotated gnd rail enters the set on class alone — no
    # target, the legacy ladder ruling applies
    assert 2 not in targets
    # the 1 mA power net is in the set on class but its annotated
    # target is below its born width — it keeps its rule width
    assert targets[4] <= born_rules["PWR1MA"].track_width_mm


def test_widen_unannotated_gnd_rail_still_widens_end_to_end() -> None:
    """gr524239 review: the standing rule — an unannotated GND rail
    routes as a normal net and is widened in the final stage, claiming
    the widest step the DRC oracle accepts (the legacy ruling a
    plane-class net keeps when it carries no current estimate)."""
    board = _plane_board()  # GND cls="gnd", no est_current_a
    design = kernel._design(board, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints, route_planes=True)
    born_rules = route_net_rules(design.graph, design.fab_caps)
    target_rules = clearance_net_rules(design.graph, design.fab_caps)
    _plan, res = route_design(td, ir, net_rules=born_rules)
    layers = [str(layer.get("name") or "") for layer in ir.stackup]
    gnd_ir = int(list(ir.net_name).index("GND"))

    power_ids, targets = widen_targets(ir, born_rules, target_rules)
    assert gnd_ir in power_ids and gnd_ir not in targets
    res2, widths = widen_power(
        res,
        power_ids,
        drc_findings=lambda r: _drc_findings(r, ir, layers),
        targets=targets,
    )
    assert widths[gnd_ir] > born_rules["GND"].track_width_mm
    assert _drc_errors(res2, ir, layers) == 0


def test_widen_targets_rolls_pin_currents_up_to_the_net() -> None:
    """gr524239 review: a net with no ``est_current_a`` of its own still
    has a figure when member pins declare theirs — the resolved
    exchanges roll up to the worst exchange on the net (the undeclared
    source pin carries the residual of the two 1 A draws, so the net
    reads 2.0 A, not one pin's 1.0). The rail then prices its
    current-derived widen target off that figure."""
    members = [
        {"refdes": "U1", "pin": "2", "tags": ["power_out"]},
        {"refdes": "L1", "pin": "1", "meta": {"est_current_a": 1.0}},
        {"refdes": "L2", "pin": "1", "meta": {"est_current_a": 1.0}},
    ]
    graph = {
        "instances": [{"refdes": r} for r in ("U1", "L1", "L2")],
        "nets": [{"name": "VOUT", "net_class": "power", "members": members}],
    }
    ir = from_graph(graph)
    vout = int(list(ir.net_name).index("VOUT"))
    assert ir.net_current_a[vout] == pytest.approx(2.0)

    caps = capability_for("4layer")
    design = {"nets": graph["nets"], "net_classes": {}}
    born_rules = route_net_rules(design, caps)
    target_rules = clearance_net_rules(design, caps)
    power_ids, targets = widen_targets(ir, born_rules, target_rules)
    assert vout in power_ids
    # the rolled 2.0 A prices the IPC-2221 target — wider than born
    assert targets[vout] > born_rules["VOUT"].track_width_mm


def test_widen_follows_estimated_current_end_to_end() -> None:
    """gr524239's reported failure inverted: a board with a 1 mA signal
    net and a real-current rail — after the widen stage the signal net
    keeps its born width and the rail is wider. (The report's 1 mA
    nets reached 1.0 mm because the widen set admitted every net with
    ANY annotation and the ladder served available space, not the
    estimated current's need.)"""
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    b = pcb.Board("widen-cur", stackup=4, outline=pcb.rect(30.0, 10.0))
    f = b.footprint(fp)
    g1 = b.part("G1", footprint=f, pins=["1", "2"]).at(8.0, 5.0)
    g2 = b.part("G2", footprint=f, pins=["1", "2"]).at(22.0, 5.0)
    b.net("GND", cls="gnd", current=1.5).tie(g1["2"], g2["1"])
    s1 = b.part("S1", footprint=f, pins=["1", "2"]).at(5.0, 8.5)
    s2 = b.part("S2", footprint=f, pins=["1", "2"]).at(15.0, 8.5)
    b.net("SIG", current=0.001).tie(s1["2"], s2["1"])

    design = kernel._design(b, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints, route_planes=True)
    born_rules = route_net_rules(design.graph, design.fab_caps)
    target_rules = clearance_net_rules(design.graph, design.fab_caps)
    _plan, res = route_design(td, ir, net_rules=born_rules)
    layers = [str(layer.get("name") or "") for layer in ir.stackup]
    gnd_ir = int(list(ir.net_name).index("GND"))
    sig_ir = int(list(ir.net_name).index("SIG"))

    power_ids, targets = widen_targets(ir, born_rules, target_rules)
    res2, widths = widen_power(
        res,
        power_ids,
        drc_findings=lambda r: _drc_findings(r, ir, layers),
        targets=targets,
    )
    assert widths[gnd_ir] > born_rules["GND"].track_width_mm
    assert sig_ir not in widths  # never a candidate
    assert all(t.width_mm == 0.15 for t in res2.tracks if t.net_id == sig_ir)
    assert _drc_errors(res2, ir, layers) == 0


def test_widen_power_target_at_or_below_rule_skips_widening_and_the_oracle() -> None:
    """gr524239: a rail whose current-derived target sits at or below
    the width it was born at widens nothing — and must not even ask the
    DRC oracle (the space-driven ladder tried every net; the target gate
    skips before the first trial)."""
    tracks = (
        pcb_realize.RealizedTrack(
            seg_id=0,
            net_id=0,
            layer=0,
            segments=(),
            length_mm=1.0,
            blocked_by=None,
            width_mm=0.15,
        ),
    )
    res = pcb_realize.RealizeResult(tracks, (), ())
    calls = 0

    def errs(r: pcb_realize.RealizeResult) -> list[drc.DrcFinding]:
        nonlocal calls
        calls += 1
        return []

    res2, widths = widen_power(res, {0}, drc_findings=errs, targets={0: 0.1})
    assert widths == {0: 0.15}
    assert res2.tracks[0].width_mm == 0.15
    assert calls == 1  # only the baseline measurement


def test_widen_power_refuses_step_that_trades_connectivity_for_clearance() -> None:
    """gr524392 (reopened): judged on the error COUNT, a widening step
    that bridges its net's own broken pieces (a connectivity error gone)
    and eats the clearance to a foreign pad (a clearance error minted)
    kept the total level and was accepted — on prod, VIN_DC widened to
    0.5 mm and came out touching pad U2/VSS at 0.000 mm. The oracle now
    keys findings: a step is kept only when it mints NO new error.

    Net A here is two collinear tracks whose ends sit 0.3 mm apart —
    one connectivity error until a step bridges them; a foreign pad's
    edge sits 0.3 mm off the track centreline, inside JLC-min clearance
    of any track wider than 0.42 mm (the first erroring ladder step is
    0.5 mm). On the count oracle the 1.0 mm step traded one finding for
    another and committed; the keyed oracle refuses every step that
    mints the pad finding."""
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    b = pcb.Board("widen-trade", stackup=4, outline=pcb.rect(30.0, 10.0))
    f = b.footprint(fp)
    a1 = b.part("A1", footprint=f, pins=["1", "2"]).at(4.0, 5.0)
    a2 = b.part("A2", footprint=f, pins=["1", "2"]).at(26.0, 5.0)
    # foreign (unwired) copper above the gap: a 0.9x0.4 pad centred 0.5
    # mm off the track centreline — its bottom edge 0.3 mm off.
    b.part(
        "B1",
        footprint=b.footprint(
            pcb.Footprint("STUB", [pcb.pad("1", "rect", 0.0, 0.0, w=0.9, h=0.4)])
        ),
        pins=["1"],
    ).at(14.15, 5.5)
    b.net("A").tie(a1["2"], a2["1"])

    design = kernel._design(b, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    layers = [str(layer.get("name") or "") for layer in ir.stackup]
    a_net = int(list(ir.net_name).index("A"))

    def track(seg_id: int, x0: float, x1: float) -> pcb_realize.RealizedTrack:
        return pcb_realize.RealizedTrack(
            seg_id=seg_id,
            net_id=a_net,
            layer=0,
            segments=({"shape": "line", "start": [x0, 5.0], "end": [x1, 5.0]},),
            length_mm=abs(x1 - x0),
            blocked_by=None,
            width_mm=0.15,
        )

    res = pcb_realize.RealizeResult((track(0, 5.0, 14.0), track(1, 14.3, 25.5)), (), ())
    findings = lambda r: _drc_findings(r, ir, layers, footprints=footprints)

    # the fixture IS the trade: net A is broken until a step bridges it
    assert {f.rule for f in findings(res)} == {"connectivity"}

    res2, widths = widen_power(
        res, {a_net}, drc_findings=findings, targets={a_net: 1.0}
    )
    # refused at 1.0/0.8/0.6/0.5 — each mints track[A] <-> pad[B1]: the
    # kept width stays below the first step that adds it, and the kept
    # board carries no clearance finding (the connectivity error may go;
    # nothing new may come).
    assert widths[a_net] < 0.5
    assert not [f for f in findings(res2) if f.rule == "clearance"]


def _via_fixture(
    *,
    pin_side: tuple[int, ...] = (0, 1, 0, 0),
    constrained: tuple[bool, ...],
    zones: tuple[TopoZone, ...] = (),
    net_layers: tuple[frozenset[int], ...] | None = None,
) -> tuple[TopoDesign, TopoMesh, Wires]:
    """gr520901/gr524239 — a two-branch synthetic: b0 (net 0) crosses
    e0→e2, b1 (net 1) crosses e1→e3, and the two shared edges carry
    opposite order so the branches cross. ``constrained`` marks ring
    edges (kept so tests can still read the mesh's own record); the
    keepout bodies a via must respect are expressed as ``zones`` — the
    same shape ``design_from_ir`` builds from the board's features, and
    what the point-based siting walk actually tests against."""
    verts = np.asarray(
        [
            [0.0, 0.0],  # pad vertex — no edge endpoint
            [0.6, 0.0],
            [0.6, 0.6],  # e0: the pad's ring edge (0.6 mm)
            [2.0, 0.0],
            [2.0, 3.0],  # e1: free channel (3.0 mm)
            [5.0, 0.5],
            [5.0, 3.0],  # e2: free channel (2.5 mm)
            [7.0, 0.0],
            [7.0, 4.0],  # e3: free (4.0 mm)
        ]
    )
    edges = np.asarray([[1, 2], [3, 4], [5, 6], [7, 8]], np.int32)
    n_e = len(edges)
    mesh = TopoMesh(
        verts=verts,
        tris=np.zeros((0, 3), np.int32),
        edges=edges,
        tri_edges=np.zeros((0, 3), np.int32),
        edge_tris=np.zeros((n_e, 2), np.int32),
        constrained=np.asarray(constrained, dtype=bool),
        pad_vertex={},
    )
    b0 = WirePath(
        net=0,
        pin_a=0,
        pin_b=1,
        tris=(),
        edges=(0, 1, 2),
        t=(0.5, 0.5, 0.5),
        points=((0.0, 0.0), (0.6, 0.3), (2.0, 1.5), (5.0, 1.75), (8.0, 2.0)),
    )
    b1 = WirePath(
        net=1,
        pin_a=2,
        pin_b=3,
        tris=(),
        edges=(1, 2, 3),
        t=(0.4, 0.4, 0.5),
        points=((1.0, -1.0), (2.0, 1.2), (5.0, 1.5), (7.0, 2.0), (9.0, 1.0)),
    )
    order: list[tuple[int, ...]] = [() for _ in range(n_e)]
    order[1] = (0, 1)
    order[2] = (1, 0)  # order flips between the shared edges → a crossing
    n_layers = 4
    outers = frozenset({0, n_layers - 1})
    wires = Wires(
        branches=(b0, b1),
        order=tuple(order),
        capacity_mm=np.zeros(n_e),
        net_layers=net_layers or (outers, outers),
        unreachable=(),
        gap_free_mm=np.full((n_e, n_layers), -1.0),
        gap_cap=np.full((n_e, n_layers), -1),
    )
    d = TopoDesign(
        parts=("P1", "P2", "P3", "P4"),
        pin_xy=np.zeros((4, 2)),
        pin_part=np.asarray([0, 1, 2, 3]),
        pin_side=np.asarray(pin_side, dtype=np.int8),
        pin_label=np.asarray(["1"] * 4, dtype=object),
        pin_net=np.asarray([0, 0, 1, 1], dtype=np.int32),
        net_pins=(np.asarray([0, 1]), np.asarray([2, 3])),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(4, dtype=bool),
        freedoms=np.full(4, 3, dtype=np.uint8),
        outline=np.asarray([[-1.0, -1.0], [10.0, -1.0], [10.0, 5.0], [-1.0, 5.0]]),
        pose0=np.zeros((4, 4)),
        net_name=("N0", "N1"),
        zones=zones,
    )
    return d, mesh, wires


def _decrowd_b1(wires: Wires) -> Wires:
    """b1's polyline moved 0.6+ mm off b0's candidate via sites — the
    foreign-track veto (s4d) is exercised by its own tests; tests for a
    different keepout decrowd so their sites still mint."""
    return dataclasses.replace(
        wires,
        branches=(
            wires.branches[0],
            dataclasses.replace(
                wires.branches[1],
                points=((1.0, -1.0), (2.0, 0.9), (5.5, 0.9), (9.0, 1.0)),
            ),
        ),
    )


def test_end_via_never_sites_on_a_pad_ring_edge() -> None:
    """gr520901's acceptance unit, generalized by gr524239: a branch
    yielded to the other outer layer with BOTH pads top-SMD needs a via
    at EACH end; the near end's walk starts on the pad's own ring-edge
    keepout zone and must come out with the barrel keep-out clear of it.
    """
    ring = Polygon([(-0.5, -0.5), (0.6, -0.5), (0.6, 0.6), (-0.5, 0.6)])
    d, mesh, wires = _via_fixture(
        pin_side=(0, 0, 0, 0),
        constrained=(True, False, False, False),
        zones=(
            TopoZone(
                kind="keepout",
                ring=np.asarray([[-0.5, -0.5], [0.6, -0.5], [0.6, 0.6], [-0.5, 0.6]]),
                layers=(),
                name="pad ring",
            ),
        ),
    )
    # b1's polyline decrowded — the foreign-track veto (s4d) has its
    # own tests; this one exercises the keepout zone, which needs its
    # sites mintable.
    plan = plan_route(d, mesh, _decrowd_b1(wires), 4)

    assert sorted(v.branch for v in plan.vias) == [0, 0]
    near, _far = sorted(plan.vias, key=lambda v: v.x)
    # the near via walked off the ring edge: the barrel keep-out
    # (0.6/2 + 0.15 = 0.45 mm) clears the pad-ring zone
    assert ring.distance(Point(near.x, near.y)) >= 0.45 - 1e-9
    assert plan.no_via == ()
    # both ends stitched: three runs F.Cu -> B.Cu -> F.Cu
    b0_runs = sorted(
        (p for p in plan.pieces if p.branch == 0), key=lambda p: p.points[0][0]
    )
    assert [p.layer for p in b0_runs] == [0, 3, 0]


def test_via_site_rejects_a_spot_crowding_another_via() -> None:
    """The point walk refuses a site within via_dia + clearance of an
    already placed via: b1's mid via must come out at least the
    0.75 mm keep-out away from the via b0's crossing anchor took."""
    d, mesh, wires = _via_fixture(
        pin_side=(0, 1, 0, 1), constrained=(True, False, False, False)
    )
    wires = dataclasses.replace(
        wires,
        branches=(
            wires.branches[0],
            dataclasses.replace(
                wires.branches[1],
                t=(0.4, 0.4, 0.15),
                points=((1.0, -1.0), (2.0, 1.2), (5.5, 0.9), (9.0, 1.0)),
            ),
        ),
    )
    plan = plan_route(d, mesh, wires, 4)

    assert len(plan.vias) == 2
    via0 = next(v for v in plan.vias if v.branch == 0)
    via1 = next(v for v in plan.vias if v.branch == 1)
    assert not mesh.constrained[via0.edge]
    # b0's mid via wants the e1 anchor — (2.0, 1.5) sits 0.3 mm off
    # b1's polyline, inside the 0.525 mm track keep-out — so the point
    # walk takes the first spot honouring both keep-outs: off the
    # foreign track AND off the other minted via.
    b1_line = LineString(wires.branches[1].points)
    assert b1_line.distance(Point(via0.x, via0.y)) >= 0.525 - 1e-9
    assert (via0.x, via0.y) != pytest.approx((2.0, 1.5))
    assert math.dist((via0.x, via0.y), (via1.x, via1.y)) >= 0.75 - 1e-9


def test_via_site_walks_off_a_track_crowded_edge() -> None:
    """s4d: the siting walk clears other nets' TRACKS — the via barrel
    spans every layer, so a candidate within the via clearance of a
    foreign polyline (any layer, inners included) is refused and the
    walk continues. b1 crowds b0's e1-adjacent stretch: both joins
    still mint, each landing somewhere the track keep-out holds."""
    d, mesh, wires = _via_fixture(
        pin_side=(0, 0, 0, 0),
        constrained=(False,) * 4,
        net_layers=(frozenset({0, 1, 2, 3}),) * 2,
    )
    wires = dataclasses.replace(
        wires,
        branches=(
            wires.branches[0],
            dataclasses.replace(
                wires.branches[1],
                points=((1.0, -1.0), (2.0, 1.2), (4.5, 0.8), (9.0, 1.0)),
            ),
        ),
    )
    plan = plan_route(d, mesh, wires, 4)

    vias = [v for v in plan.vias if v.branch == 0]
    b1_line = LineString(wires.branches[1].points)
    assert len(vias) == 2
    assert all(b1_line.distance(Point(v.x, v.y)) >= 0.525 - 1e-9 for v in vias)
    assert plan.no_via == ()


def test_via_sites_clear_foreign_tracks_or_report_no_via() -> None:
    """The default fixture crowds BOTH of b0's anchor-adjacent
    stretches — (2.0, 1.5) sits 0.30 mm and (5.0, 1.75) 0.24 mm off
    b1's polyline, inside the 0.525 mm via-to-track keep-out. The
    first join walks back to a clean point (every minted site keeps
    the track keep-out); the second finds nothing and lands on
    ``no_via`` — the pieces still split at the intended joins (the
    failed net's honest geometry)."""
    d, mesh, wires = _via_fixture(
        pin_side=(0, 0, 0, 0),
        constrained=(False,) * 4,
        net_layers=(frozenset({0, 1, 2, 3}),) * 2,
    )
    plan = plan_route(d, mesh, wires, 4)

    b1_line = LineString(wires.branches[1].points)
    assert all(b1_line.distance(Point(v.x, v.y)) >= 0.525 - 1e-9 for v in plan.vias)
    assert plan.no_via == (0,)
    assert len([p for p in plan.pieces if p.branch == 0]) == 3


def test_no_clean_point_reports_no_via_site_instead_of_emitting() -> None:
    """Every point on the branch sits inside a keepout zone: no via is
    minted and the net lands on ``pcb_routes`` as ``failed: no via
    site`` — a report, never keepout-breaking copper."""
    d, mesh, wires = _via_fixture(
        constrained=(True, True, True, False),
        zones=(
            TopoZone(
                kind="keepout",
                ring=np.asarray([[-1.0, -1.0], [9.0, -1.0], [9.0, 3.0], [-1.0, 3.0]]),
                layers=(),
                name="corridor",
            ),
        ),
    )
    plan = plan_route(d, mesh, wires, 4)

    assert plan.vias == ()
    assert plan.no_via == (0,)
    # the branch still emits its two run pieces — the failed net's
    # honest geometry — it is the VIA that is not minted.
    assert {p.branch for p in plan.pieces} == {0, 1}

    ir = SimpleNamespace(
        n_pins=4,
        n_nets=2,
        pin_net=np.asarray([0, 0, 1, 1]),
        pin_instance=np.asarray([0, 1, 2, 3]),
        pin_label=np.asarray(["1"] * 4, dtype=object),
        instance_refdes=np.asarray(list(d.parts), dtype=object),
        net_name=np.asarray(list(d.net_name), dtype=object),
        net_plane_layers=np.zeros(2, dtype=np.uint16),
    )
    rows = sketch_rows(plan, d, ir, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    assert rows["N0"]["status"] == "failed"
    assert rows["N0"]["fail"]["problems"][0]["reason"] == "no via site"
    assert "no via site" in rows["N0"]["note"]
    assert rows["N1"]["status"] == "realized"


def _guard_fixture() -> tuple[TopoDesign, SimpleNamespace]:
    """One part at (10, 0) whose single pad sits at pin-local (5, 0) —
    deliberately far from the part origin, so a guard that builds pad
    rects around (0, 0) (the gr524239 first-cut bug) is detectable."""
    d = TopoDesign(
        parts=("P1",),
        pin_xy=np.asarray([[5.0, 0.0]]),
        pin_part=np.asarray([0]),
        pin_side=np.asarray([0], dtype=np.int8),
        pin_label=np.asarray(["1"], dtype=object),
        pin_net=np.asarray([0], dtype=np.int32),
        net_pins=(np.asarray([0]),),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(1, dtype=bool),
        freedoms=np.full(1, 3, dtype=np.uint8),
        outline=np.asarray([[-5.0, -5.0], [25.0, -5.0], [25.0, 5.0], [-5.0, 5.0]]),
        pose0=np.asarray([[10.0, 0.0, 0.0, 0.0]]),
        net_name=("N0", "N1"),
        pin_wh=np.asarray([[0.8, 1.0]]),
    )
    ir = SimpleNamespace(net_name=np.asarray(["N0", "N1"], dtype=object))
    return d, ir


def test_emit_guard_reads_pads_at_their_pin_centres() -> None:
    """gr524239 regression: pad polygons are centred on the pin's local
    x/y, not the part origin. A foreign piece skimming the part origin
    must NOT be withdrawn — the pad is 5 mm away."""
    d, ir = _guard_fixture()
    plan = RoutePlan(
        pieces=(
            RoutePiece(net=1, branch=0, layer=0, points=((8.0, 0.3), (12.0, 0.3))),
        ),
        vias=(),
        layers=(0,),
    )
    assert _guarded_nets(plan, d, ir, 2, np.asarray(d.pose0), {}) == set()


def test_emit_guard_withdraws_a_piece_through_a_foreign_pad() -> None:
    """The same fixture's positive control: a foreign piece running
    straight through the pad (world (15, 0)) is withdrawn whole."""
    d, ir = _guard_fixture()
    plan = RoutePlan(
        pieces=(
            RoutePiece(net=1, branch=0, layer=0, points=((14.0, 0.0), (16.0, 0.0))),
        ),
        vias=(),
        layers=(0,),
    )
    assert _guarded_nets(plan, d, ir, 2, np.asarray(d.pose0), {}) == {1}


def test_emit_guard_sees_nonprimary_lands() -> None:
    """gr524239: the guard's pad roster must be the SAME pad set the
    DRC flashes — including the footprint's non-primary lands
    (:attr:`TopoDesign.pad_extra_*`), which ``pin_xy`` does not
    describe. An unpinned NC land is foreign to every net; a piece
    across it (world (17.5, 0)) is committed short copper the pin-only
    roster passed. A pinned extra keeps its pin's net: an own-net
    piece across it is legal and stays."""
    d, ir = _guard_fixture()

    def with_extra(pin: int) -> TopoDesign:
        return dataclasses.replace(
            d,
            pad_extra_xy=np.asarray([[7.5, 0.0]]),  # world (17.5, 0)
            pad_extra_wh=np.asarray([[1.0, 1.0]]),
            pad_extra_part=np.asarray([0]),
            pad_extra_pin=np.asarray([pin]),
            pad_extra_side=np.asarray([0], dtype=np.int8),
        )

    piece = RoutePiece(net=1, branch=0, layer=0, points=((16.0, 0.0), (19.0, 0.0)))

    # unpinned land — foreign to every net
    d_nc = with_extra(-1)
    plan = RoutePlan(pieces=(piece,), vias=(), layers=(0,))
    assert _guarded_nets(plan, d_nc, ir, 2, np.asarray(d.pose0), {}) == {1}

    # pinned to a foreign-net pin (pin 0's net is N0) — still foreign to N1
    d_foreign = with_extra(0)
    assert _guarded_nets(plan, d_foreign, ir, 2, np.asarray(d.pose0), {}) == {1}

    # same-net extra land — the piece's own copper, not a short
    own = RoutePiece(net=0, branch=1, layer=0, points=((16.0, 0.0), (19.0, 0.0)))
    plan = RoutePlan(pieces=(own,), vias=(), layers=(0,))
    assert _guarded_nets(plan, d_foreign, ir, 2, np.asarray(d.pose0), {}) == set()


def test_guard_withdraws_crossing_wires_the_order_lists_never_saw() -> None:
    """gr524239 — the emit guard's case the order-based crossing
    detector structurally misses: two branches whose taut polylines
    cross mid-cell on DISJOINT mesh edges. No shared edge means no
    order inversion, so ``plan.crossings`` is empty and both pieces
    are emitted on the same layer — geometrically an X, a real short.
    The guard must withdraw BOTH nets and ``sketch_rows`` report them
    ``failed: short-risk``; nothing shorting may be committed."""
    n_layers = 4
    outers = frozenset({0, n_layers - 1})
    verts = np.asarray([[0.0, 0.0], [4.0, 0.0], [0.0, 4.0], [4.0, 4.0]])
    edges = np.asarray([[0, 1], [2, 3]], np.int32)
    mesh = TopoMesh(
        verts=verts,
        tris=np.zeros((0, 3), np.int32),
        edges=edges,
        tri_edges=np.zeros((0, 3), np.int32),
        edge_tris=np.zeros((2, 2), np.int32),
        constrained=np.zeros(2, dtype=bool),
        pad_vertex={},
    )
    b0 = WirePath(
        net=0,
        pin_a=0,
        pin_b=1,
        tris=(),
        edges=(0,),
        t=(0.5,),
        points=((0.0, 0.0), (4.0, 4.0)),
    )
    b1 = WirePath(
        net=1,
        pin_a=2,
        pin_b=3,
        tris=(),
        edges=(1,),
        t=(0.5,),
        points=((0.0, 4.0), (4.0, 0.0)),
    )
    wires = Wires(
        branches=(b0, b1),
        order=((), ()),
        capacity_mm=np.zeros(2),
        net_layers=(outers, outers),
        unreachable=(),
        gap_free_mm=np.full((2, n_layers), -1.0),
        gap_cap=np.full((2, n_layers), -1),
    )
    d = TopoDesign(
        parts=("P1", "P2", "P3", "P4"),
        pin_xy=np.zeros((4, 2)),
        pin_part=np.asarray([0, 1, 2, 3]),
        pin_side=np.asarray([0, 0, 0, 0], dtype=np.int8),
        pin_label=np.asarray(["1"] * 4, dtype=object),
        pin_net=np.asarray([0, 0, 1, 1], dtype=np.int32),
        net_pins=(np.asarray([0, 1]), np.asarray([2, 3])),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(4, dtype=bool),
        freedoms=np.full(4, 3, dtype=np.uint8),
        outline=np.asarray([[-1.0, -1.0], [5.0, -1.0], [5.0, 5.0], [-1.0, 5.0]]),
        pose0=np.zeros((4, 4)),
        net_name=("N0", "N1"),
        pin_wh=np.zeros((4, 2)),
    )
    plan = plan_route(d, mesh, wires, n_layers)
    # the order detector saw nothing — the pieces are emitted on the
    # same layer, physically crossing at (2, 2)
    assert plan.crossings == ()
    assert {p.layer for p in plan.pieces} == {0}

    ir = SimpleNamespace(
        n_pins=4,
        n_nets=2,
        pin_net=np.asarray([0, 0, 1, 1]),
        pin_instance=np.asarray([0, 1, 2, 3]),
        pin_label=np.asarray(["1"] * 4, dtype=object),
        instance_refdes=np.asarray(list(d.parts), dtype=object),
        net_name=np.asarray(list(d.net_name), dtype=object),
        net_plane_layers=np.zeros(2, dtype=np.uint16),
    )
    guarded = _guarded_nets(plan, d, ir, n_layers, np.asarray(d.pose0), {})
    assert guarded == {0, 1}

    # the route_design emit path: withdraw the flagged nets, then the
    # emitted copper carries NO track<->track violation
    emit = dataclasses.replace(
        plan,
        pieces=tuple(p for p in plan.pieces if p.net not in guarded),
        vias=tuple(v for v in plan.vias if v.net not in guarded),
    )
    plan = dataclasses.replace(plan, guarded=tuple(sorted(guarded)))
    res = realize_result(emit, d, ir, n_layers)
    assert res.tracks == ()  # no short copper committed

    rows = sketch_rows(plan, d, ir, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    for name in ("N0", "N1"):
        assert rows[name]["status"] == "failed"
        assert rows[name]["fail"]["problems"][0]["reason"] == "short-risk"


def test_forced_crossing_board_commits_no_track_short() -> None:
    """gr524239 end-to-end: two top-SMD nets whose pads sit in an X —
    whatever the yielder/span-flip/guard does with them, the committed
    copper must carry no track<->track clearance error. Where the
    crossing cannot be resolved the net is withdrawn and reported
    failed instead."""
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    b = pcb.Board("cross", stackup=4, outline=pcb.rect(30.0, 20.0))
    f = b.footprint(fp)
    a1 = b.part("A1", footprint=f, pins=["1", "2"]).at(5.0, 3.0)
    a2 = b.part("A2", footprint=f, pins=["1", "2"]).at(25.0, 17.0)
    b1 = b.part("B1", footprint=f, pins=["1", "2"]).at(5.0, 17.0)
    b2 = b.part("B2", footprint=f, pins=["1", "2"]).at(25.0, 3.0)
    b.wire(a1["2"], a2["1"], name="NA")
    b.wire(b1["2"], b2["1"], name="NB")

    design = kernel._design(b, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(
        ir, footprints, net_classes=design.graph.get("net_classes"), route_planes=True
    )
    plan, res = route_design(td, ir)
    layers = [str(layer.get("name") or "") for layer in ir.stackup]
    findings = _drc_findings(res, ir, layers, footprints=footprints)
    track_shorts = [f for f in findings if f.rule == "clearance" and "track" in f.where]
    assert track_shorts == [], [f"{f.rule}: {f.where}" for f in track_shorts]
    rows = sketch_rows(plan, td, ir, layers)
    for name in ("NA", "NB"):
        assert rows[name]["status"] in ("realized", "failed")
        if rows[name]["status"] == "failed":
            assert rows[name]["fail"]["problems"]


def _stall_fixture() -> tuple[TopoDesign, TopoMesh, Wires]:
    """gr524239 — the yield-stall synthetic: branch A (TH, rank 0) sits
    on F.Cu and inverts with B (also F.Cu), C and D (both B.Cu) on
    disjoint edge pairs. Only (A,B) is live initially; the rank-preferred
    yield (flip A) moves A onto B.Cu where its inverted orders against
    C and D turn TWO dormant pairs live — the pre-pass-based yielder
    broke right there and returned the stall. Flipping B instead
    resolves the live pair and touches nothing else."""
    n_layers = 4
    outers = frozenset({0, n_layers - 1})
    verts = np.asarray([[float(i), 0.0] for i in range(12)])
    edges = np.asarray([[0, 1], [2, 3], [4, 5], [6, 7], [8, 9], [10, 11]], np.int32)
    mesh = TopoMesh(
        verts=verts,
        tris=np.zeros((0, 3), np.int32),
        edges=edges,
        tri_edges=np.zeros((0, 3), np.int32),
        edge_tris=np.zeros((len(edges), 2), np.int32),
        constrained=np.zeros(len(edges), dtype=bool),
        pad_vertex={},
    )
    branches = (
        WirePath(
            net=0,
            pin_a=0,
            pin_b=1,
            tris=(),
            edges=(0, 1, 2, 3, 4, 5),
            t=(0.5,) * 6,
            points=(
                (0.0, 0.0),
                (1.0, 0.0),
                (2.0, 0.0),
                (3.0, 0.0),
                (4.0, 0.0),
                (5.0, 0.0),
                (6.0, 0.0),
            ),
        ),
        WirePath(
            net=1,
            pin_a=2,
            pin_b=3,
            tris=(),
            edges=(0, 1),
            t=(0.5, 0.5),
            points=((0.0, 1.0), (1.0, 1.0), (2.0, 1.0)),
        ),
        WirePath(
            net=2,
            pin_a=4,
            pin_b=5,
            tris=(),
            edges=(2, 3),
            t=(0.5, 0.5),
            points=((2.0, 2.0), (3.0, 2.0), (4.0, 2.0)),
        ),
        WirePath(
            net=3,
            pin_a=6,
            pin_b=7,
            tris=(),
            edges=(4, 5),
            t=(0.5, 0.5),
            points=((4.0, 3.0), (5.0, 3.0), (6.0, 3.0)),
        ),
    )
    order: list[tuple[int, ...]] = [() for _ in range(6)]
    order[0] = (0, 1)
    order[1] = (1, 0)  # (A, B) invert
    order[2] = (0, 2)
    order[3] = (2, 0)  # (A, C) invert
    order[4] = (0, 3)
    order[5] = (3, 0)  # (A, D) invert
    wires = Wires(
        branches=branches,
        order=tuple(order),
        capacity_mm=np.zeros(6),
        net_layers=(outers, outers, outers, outers),
        unreachable=(),
        gap_free_mm=np.full((6, n_layers), -1.0),
        gap_cap=np.full((6, n_layers), -1),
    )
    d = TopoDesign(
        parts=("PA", "PB", "PC", "PD"),
        pin_xy=np.zeros((8, 2)),
        pin_part=np.repeat(np.arange(4), 2),
        pin_side=np.asarray([2, 2, 0, 0, 1, 1, 1, 1], dtype=np.int8),
        pin_label=np.asarray(["1"] * 8, dtype=object),
        pin_net=np.asarray([0, 0, 1, 1, 2, 2, 3, 3], dtype=np.int32),
        net_pins=tuple(np.asarray([2 * i, 2 * i + 1]) for i in range(4)),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(4, dtype=bool),
        freedoms=np.full(4, 3, dtype=np.uint8),
        outline=np.asarray([[-5.0, -5.0], [15.0, -5.0], [15.0, 15.0], [-5.0, 15.0]]),
        pose0=np.zeros((4, 4)),
        net_name=("NA", "NB", "NC", "ND"),
    )
    return d, mesh, wires


def test_assign_layers_pass_based_yield_unstalls_the_rank_trap() -> None:
    """The pre-gr524239 greedy yielded the rank-preferred branch of the
    minimum pair and stopped when that flip failed to improve: here the
    rank move (flip TH branch A onto B.Cu) turns two dormant crossings
    live, so the old loop stalled at 1 live pair. The pass-based yielder
    trials BOTH branches' flips and takes the one that empties the
    live set — B to B.Cu leaves every other pair layer-split."""
    d, mesh, wires = _stall_fixture()
    wires, runs, cuts, crossings = assign_layers(d, mesh, wires, 4)
    assert crossings == ()
    # B yields to B.Cu wholesale — its forced pads are stitched back
    # to F.Cu, so the flip shows up as a via-bracketed mid-piece.
    assert runs[0] == [0]
    assert runs[1] == [0, 3, 0]


def test_via_siting_falls_back_to_the_fab_minimum_dia() -> None:
    """gr524334: a corridor that fits the fab-minimum barrel but not the
    comfortable house via still gets a site — qualified at, and recorded
    with, the fallback diameter. A pad strip holds every candidate at
    0.40 mm: inside the 0.6/2 + 0.15 = 0.45 house keep-out, outside the
    0.45/2 + 0.15 = 0.375 fab-minimum keep-out."""
    mesh = TopoMesh(
        verts=np.asarray([[0.0, 0.0], [10.0, 0.0]]),
        tris=np.zeros((0, 3), np.int32),
        edges=np.asarray([[0, 1]], np.int32),
        tri_edges=np.zeros((0, 3), np.int32),
        edge_tris=np.zeros((1, 2), np.int32),
        constrained=np.zeros(1, dtype=bool),
        pad_vertex={},
    )
    branch = WirePath(
        net=0,
        pin_a=0,
        pin_b=1,
        tris=(),
        edges=(0,),
        t=(0.5,),
        points=((0.0, 0.0), (10.0, 0.0)),
    )
    # one pad strip spanning the corridor, 0.40 mm off the polyline
    row = (0, 0.0, 0.55, 10.0, 0.15)
    pose = np.zeros((1, 4))
    assert _site_via(mesh, branch, 0, 4, 0.6, 0.15, [], ((row,), (row,)), pose) is None
    site = _site_via(
        mesh,
        branch,
        0,
        4,
        0.6,
        0.15,
        [],
        ((row,), (row,)),
        pose,
        fallback_dia=0.45,
    )
    assert site is not None
    _e, _t, x, y, dia = site
    assert (x, y) == (5.0, 0.0)
    assert dia == 0.45


def _span_fixture() -> tuple[TopoDesign, TopoMesh, Wires]:
    """gr524239 — the span-flip synthetic: A (top SMD) crosses B (top
    SMD) on e0/e1; A also inverts with C (bottom SMD) on e2/e3, and B
    with D (bottom SMD) on e4/e5. The whole-branch yielder stalls on
    (A,B) — flipping A turns (A,C) live, flipping B turns (B,D) live —
    but a span flip of A's shared run with B touches neither (A,C) nor
    (B,D) and resolves the pair."""
    n_layers = 4
    outers = frozenset({0, n_layers - 1})
    verts = np.asarray([[float(i), 0.0] for i in range(12)])
    edges = np.asarray([[0, 1], [2, 3], [4, 5], [6, 7], [8, 9], [10, 11]], np.int32)
    mesh = TopoMesh(
        verts=verts,
        tris=np.zeros((0, 3), np.int32),
        edges=edges,
        tri_edges=np.zeros((0, 3), np.int32),
        edge_tris=np.zeros((len(edges), 2), np.int32),
        constrained=np.zeros(len(edges), dtype=bool),
        pad_vertex={},
    )
    branches = (
        WirePath(
            net=0,
            pin_a=0,
            pin_b=1,
            tris=(),
            edges=(0, 1, 2, 3),
            t=(0.5,) * 4,
            points=((0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0), (4.0, 0.0)),
        ),
        WirePath(
            net=1,
            pin_a=2,
            pin_b=3,
            tris=(),
            edges=(0, 1),
            t=(0.5, 0.5),
            points=((0.0, 1.0), (1.0, 1.0), (2.0, 1.0)),
        ),
        WirePath(
            net=2,
            pin_a=4,
            pin_b=5,
            tris=(),
            edges=(2, 3),
            t=(0.5, 0.5),
            points=((2.0, 2.0), (3.0, 2.0), (4.0, 2.0)),
        ),
        WirePath(
            net=3,
            pin_a=6,
            pin_b=7,
            tris=(),
            edges=(4, 5),
            t=(0.5, 0.5),
            points=((4.0, 3.0), (5.0, 3.0), (6.0, 3.0)),
        ),
    )
    order: list[tuple[int, ...]] = [() for _ in range(6)]
    order[0] = (0, 1)
    order[1] = (1, 0)  # (A, B) invert
    order[2] = (0, 2)
    order[3] = (2, 0)  # (A, C) invert
    order[4] = (1, 3)
    order[5] = (3, 1)  # (B, D) invert
    wires = Wires(
        branches=branches,
        order=tuple(order),
        capacity_mm=np.zeros(6),
        net_layers=(outers, outers, outers, outers),
        unreachable=(),
        gap_free_mm=np.full((6, n_layers), -1.0),
        gap_cap=np.full((6, n_layers), -1),
    )
    d = TopoDesign(
        parts=("PA", "PB", "PC", "PD"),
        pin_xy=np.zeros((8, 2)),
        pin_part=np.repeat(np.arange(4), 2),
        pin_side=np.asarray([0, 0, 0, 0, 1, 1, 1, 1], dtype=np.int8),
        pin_label=np.asarray(["1"] * 8, dtype=object),
        pin_net=np.asarray([0, 0, 1, 1, 2, 2, 3, 3], dtype=np.int32),
        net_pins=tuple(np.asarray([2 * i, 2 * i + 1]) for i in range(4)),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(4, dtype=bool),
        freedoms=np.full(4, 3, dtype=np.uint8),
        outline=np.asarray([[-5.0, -5.0], [15.0, -5.0], [15.0, 15.0], [-5.0, 15.0]]),
        pose0=np.zeros((4, 4)),
        net_name=("NA", "NB", "NC", "ND"),
    )
    return d, mesh, wires


def test_span_flip_resolves_the_whole_branch_stall() -> None:
    """The pair (A,B) is unresolvable by whole-branch flips (either flip
    trades it for a new crossing with C or D). The segment model changes
    A's layer only on the shared, order-inverting run: the pair resolves
    inside :func:`assign_layers`, nothing else moves, and A emerges
    via-bracketed with three runs F.Cu -> B.Cu -> F.Cu."""
    d, mesh, wires = _span_fixture()
    wires, runs, cuts, crossings = assign_layers(d, mesh, wires, 4)
    assert crossings == ()
    # B's bracketed span IS its whole branch (edges 0..1), so the
    # via-bracketed yield lands on B; A keeps its single F piece.
    assert runs[1] == [0, 3, 0]
    assert runs[0] == [0]
    plan = plan_route(d, mesh, wires, 4)
    assert plan.crossings == ()
    assert plan.no_via == ()
    vias_b = [v for v in plan.vias if v.branch == 1]
    assert len(vias_b) == 2
    runs_b = sorted(
        (p for p in plan.pieces if p.branch == 1), key=lambda p: p.points[0][0]
    )
    # the forced-end stubs are zero-length copper at the vias — the
    # emitted pieces are the F run and the via-bracketed B run
    assert [p.layer for p in runs_b] == [0, 3]


def test_spoke_offsets_fan_shared_gap_and_shared_corner() -> None:
    """gr520900 (topo S2): three wires through one 2-pad gap plus wires
    wrapping one pad corner — the taut funnel bends every wire at the
    same ring-corner vertex and threads the shared edges at the same
    parameter, so the realiser drew coincident copper (0.000 mm
    track-over-track). The spoke offsets rank each wrap from
    ``wires.order``, shift each bend off the pad obstacle by its
    inner-slot sum, and spread interior crossings inside the edge's
    free stretch — the emitted copper keeps clearance."""
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    gap_fp = pcb.Footprint(
        "GATE",
        [pcb.pad("1", "rect", 0.0, 0.0, w=1.4, h=1.2, drill=0.6)],
    )
    b = pcb.Board("spokes", stackup=4, outline=pcb.rect(30.0, 20.0))
    f = b.footprint(fp)
    g = b.footprint(gap_fp)
    # a keepout strip walls the whole board at y=10 flush with two
    # unwired gate pads (foreign copper to every net) — the only
    # passage is the 1.6 mm channel between their facing edges
    # (14.5..16.1); three 0.15/0.15 tracks need 1.05 of it
    b.keepout([[0.0, 9.4], [13.1, 9.4], [13.1, 10.6], [0.0, 10.6]])
    b.keepout([[17.5, 9.4], [30.0, 9.4], [30.0, 10.6], [17.5, 10.6]])
    g1 = b.part("G1", footprint=g, pins=["1"]).at(13.8, 10.0)
    g2 = b.part("G2", footprint=g, pins=["1"]).at(16.8, 10.0)
    # three nets whose straight lines all cross the wall through the
    # gap — the outer wires wrap the same gate-pad corners
    a1 = b.part("A1", footprint=f, pins=["1", "2"]).at(8.0, 3.0)
    a2 = b.part("A2", footprint=f, pins=["1", "2"]).at(8.0, 5.0)
    a3 = b.part("A3", footprint=f, pins=["1", "2"]).at(8.0, 7.0)
    c1 = b.part("C1", footprint=f, pins=["1", "2"]).at(22.0, 17.0)
    c2 = b.part("C2", footprint=f, pins=["1", "2"]).at(22.0, 15.0)
    c3 = b.part("C3", footprint=f, pins=["1", "2"]).at(22.0, 13.0)
    b.wire(a1["2"], c3["1"], name="N1")
    b.wire(a2["2"], c2["1"], name="N2")
    b.wire(a3["2"], c1["1"], name="N3")

    design = kernel._design(b, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints, route_planes=True, features=design.features)
    plan, res = route_design(td, ir)
    layers = [str(layer.get("name") or "") for layer in ir.stackup]
    findings = _drc_findings(res, ir, layers, footprints=footprints)
    track_hits = [f for f in findings if f.rule == "clearance" and "track" in f.where]
    assert track_hits == [], [f"{f.rule}: {f.where}" for f in track_hits]
    # gr536410/gr537726: the spurious braid is gone and the shared-gap
    # unwind seeds the coupled three-pair squeeze untangled, so every
    # net routes on one layer with no vias and nothing is withdrawn —
    # the routed count did not drop, every net still emits copper.
    assert plan.crossings == ()
    assert plan.vias == ()
    assert {p.layer for p in plan.pieces} == {0}
    assert {p.net for p in plan.pieces} == set(
        td.net_name.index(n) for n in ("N1", "N2", "N3")
    )
    assert not getattr(plan, "guarded", ())


def test_fan_slit_fixture_is_planar_no_braid_no_vias() -> None:
    """gr536410: pair (0,1) here used to be flagged a braid — b0's
    corridor passes above the ring vertex (17.9, 11), b1's below it,
    and a side flip around ANY shared vertex read as a crossing. That
    vertex is FREE (not a pad, not a keepout/outline corner): a side
    choice around a vertex that blocks nothing is a fan, not a
    crossing. The fixture is planar — no crossing pairs, every net on
    one layer, zero vias."""
    from precis.pcb.topoplace.realise import _cdirs, _crossing_pairs
    from precis.pcb.topoplace.wires import obstacle_vertices

    design = kernel._design(
        _slit_board([("A1", "C3"), ("A2", "C2"), ("A3", "C1")]), None
    )
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints, route_planes=True, features=design.features)
    mesh = build_mesh(td, td.pose0)
    wires = build_wires(mesh, td, ir.n_layers)
    crossed = _crossing_pairs(
        wires,
        _cdirs(mesh, wires),
        obstacle_vertices(mesh, td, 4),
        [(int(u), int(v)) for u, v in mesh.edges],
    )
    assert crossed == set()
    wires2, runs, cuts, remaining = assign_layers(td, mesh, wires, 4)
    assert remaining == ()
    assert {r for run in runs for r in run} == {0}
    assert not any(cuts)
    plan = plan_route(td, mesh, wires, 4)
    assert plan.crossings == ()
    assert plan.vias == ()
    assert {p.layer for p in plan.pieces} == {0}


def test_crossing_pairs_flip_at_free_vertex_is_a_fan_not_a_braid() -> None:
    """gr536410 unit: two wires sharing consecutive edges (common vertex
    1) whose order inverts — around a FREE vertex that is a fan
    (excluded); around an OBSTACLE vertex the side choice is locked and
    the braid stands. A flip between edges sharing NO vertex stays a
    crossing regardless (the corridors genuinely interleave — the
    _span_fixture shape)."""
    from precis.pcb.topoplace.realise import _crossing_pairs

    def wires_with(edges_a, edges_b, order) -> Wires:
        return Wires(
            branches=(
                WirePath(
                    net=0,
                    pin_a=0,
                    pin_b=1,
                    tris=(),
                    edges=edges_a,
                    t=(0.5,) * len(edges_a),
                    points=((0.0, 0.0),) * (len(edges_a) + 1),
                ),
                WirePath(
                    net=1,
                    pin_a=2,
                    pin_b=3,
                    tris=(),
                    edges=edges_b,
                    t=(0.5,) * len(edges_b),
                    points=((0.0, 0.0),) * (len(edges_b) + 1),
                ),
            ),
            order=order,
            capacity_mm=np.zeros(2),
            net_layers=(frozenset({0, 3}), frozenset({0, 3})),
            unreachable=(),
            gap_free_mm=np.full((2, 4), -1.0),
            gap_cap=np.full((2, 4), -1),
        )

    # consecutive shared edges (common vertex 1), order inverts
    w = wires_with((0, 1), (0, 1), ((0, 1), (1, 0)))
    assert (
        _crossing_pairs(w, [[1.0, 1.0], [1.0, 1.0]], frozenset(), [(0, 1), (1, 2)])
        == set()
    )
    assert _crossing_pairs(
        w, [[1.0, 1.0], [1.0, 1.0]], frozenset({1}), [(0, 1), (1, 2)]
    ) == {(0, 1)}
    # disjoint shared edges: no vertex between the witnesses — a real
    # braid either way (the _span_fixture interleaving)
    w = wires_with((0, 1), (0, 1), ((0, 1), (1, 0)))
    assert _crossing_pairs(
        w, [[1.0, 1.0], [1.0, 1.0]], frozenset(), [(0, 1), (2, 3)]
    ) == {(0, 1)}
    # the two branches take DIFFERENT runs between the witnesses: a's
    # passes only free vertices, b's passes a pin — the union scan must
    # catch the pin on b's side and class the flip as a braid.
    # edges: 0=(0,1) 1=(1,4) 2=(4,2) 3=(2,3) 9=(1,2); witnesses 0 & 3.
    ends = [
        (0, 1),
        (1, 4),
        (4, 2),
        (2, 3),
        (9, 9),
        (9, 9),
        (9, 9),
        (9, 9),
        (9, 9),
        (1, 2),
    ]
    w = wires_with((0, 9, 3), (0, 1, 2, 3), ((0, 1), (), (), (1, 0)))
    cdirs = [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0, 1.0]]
    assert _crossing_pairs(w, cdirs, frozenset(), ends) == set()
    assert _crossing_pairs(w, cdirs, frozenset({4}), ends) == {(0, 1)}


def _slit_board(pairs):
    """The shared-gap board of ``test_spoke_offsets_fan_shared_gap_and_shared_corner``
    restricted to ``pairs`` (list of ``(part_a, part_c)`` wire endpoints) —
    keepout strip + two gate pads leave only the 14.5..16.1 channel."""
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    gap_fp = pcb.Footprint(
        "GATE",
        [pcb.pad("1", "rect", 0.0, 0.0, w=1.4, h=1.2, drill=0.6)],
    )
    b = pcb.Board("spokes", stackup=4, outline=pcb.rect(30.0, 20.0))
    f = b.footprint(fp)
    g = b.footprint(gap_fp)
    b.keepout([[0.0, 9.4], [13.1, 9.4], [13.1, 10.6], [0.0, 10.6]])
    b.keepout([[17.5, 9.4], [30.0, 9.4], [30.0, 10.6], [17.5, 10.6]])
    b.part("G1", footprint=g, pins=["1"]).at(13.8, 10.0)
    b.part("G2", footprint=g, pins=["1"]).at(16.8, 10.0)
    a1 = b.part("A1", footprint=f, pins=["1", "2"]).at(8.0, 3.0)
    a2 = b.part("A2", footprint=f, pins=["1", "2"]).at(8.0, 5.0)
    a3 = b.part("A3", footprint=f, pins=["1", "2"]).at(8.0, 7.0)
    c1 = b.part("C1", footprint=f, pins=["1", "2"]).at(22.0, 17.0)
    c2 = b.part("C2", footprint=f, pins=["1", "2"]).at(22.0, 15.0)
    c3 = b.part("C3", footprint=f, pins=["1", "2"]).at(22.0, 13.0)
    src = {"A1": a1, "A2": a2, "A3": a3}
    dst = {"C1": c1, "C2": c2, "C3": c3}
    for k, (pa, pc) in enumerate(pairs):
        b.wire(src[pa]["2"], dst[pc]["1"], name=f"N{k + 1}")
    return b


def _slit_spoke_paths(pairs):
    from precis.pcb.topoplace.realise import _spoke_offsets

    design = kernel._design(_slit_board(pairs), None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints, route_planes=True, features=design.features)
    mesh = build_mesh(td, td.pose0)
    wires = build_wires(mesh, td, ir.n_layers)
    return _spoke_offsets(td, mesh, wires, ir.n_layers)


# gate-pad boxes (G1 13.8±0.7 / G2 16.8±0.7, h=1.2 at y=10)
_GL = Polygon([(13.1, 9.4), (14.5, 9.4), (14.5, 10.6), (13.1, 10.6)])
_GR = Polygon([(16.1, 9.4), (17.5, 9.4), (17.5, 10.6), (16.1, 10.6)])


def _assert_slit_margins(paths, pair_idxs) -> None:
    from shapely.geometry import LineString

    lines = [LineString(p) for p in paths]
    for line in lines:
        for k in range(len(line.coords) - 1):
            leg = LineString([line.coords[k], line.coords[k + 1]])
            assert leg.distance(_GL) >= 0.225 - 1e-3
            assert leg.distance(_GR) >= 0.225 - 1e-3
    for i, j in pair_idxs:
        # the relaxer's hinge lands exactly on the 0.3 boundary —
        # assert the slot is held within fp tolerance
        assert lines[i].distance(lines[j]) >= 0.3 - 1e-3


def test_spoke_offsets_slit_3wires_direct_margins() -> None:
    """gr520900/gr537726: direct check on ``_spoke_offsets`` for the
    3-wire slit — every leg holds s+w/2=0.225 off the gate pads and the
    non-braided pairs keep the 0.3 slot. The pair-slot half covers
    (0,2)/(1,2) as before; gr537726's shared-gap unwind lifts the
    coupled local minimum that used to leave the wires touching at
    0.0/0.105/0.0."""
    paths = _slit_spoke_paths([("A1", "C3"), ("A2", "C2"), ("A3", "C1")])
    assert len(paths) == 3
    _assert_slit_margins(paths, [(0, 2), (1, 2)])


def test_spoke_offsets_slit_2wires_control() -> None:
    """gr520900 control: 2 wires through the same slit — the joint
    relaxation must not degrade a passage that fits comfortably.
    (N2+N3: the N1+N3 pair is a real crossing the order lists see, and
    N1+N2 is the spurious braid — both are the wrong kind of control.)"""
    paths = _slit_spoke_paths([("A2", "C2"), ("A3", "C1")])
    assert len(paths) == 2
    _assert_slit_margins(paths, [(0, 1)])


def _many_branch_slit_fixture(n_far: int = 150) -> tuple[TopoDesign, TopoMesh, Wires]:
    """gr520900 fix round 1 — the scalability synthetic. The 3-wire slit
    in the same geometry as ``_slit_board`` (A pads at x=8, C pads at
    x=22, gate boxes G1 13.1..14.5 / G2 16.1..17.5 at y 9.4..10.6), plus
    ``n_far`` quiet branches in the far field (x >= 26.5). Every far
    wire carries one COLLINEAR interior vertex, so a whole-board
    relaxation would face ~n_far free vertices and a per-evaluation
    sweep over all of them — the minutes-long single solve this fix
    replaces. The slit wires run from 2 mm-spaced pads, fan in below
    the gate boxes, and thread a tight slot 0.14 apart before climbing
    over G2 to their pads — every cross-wire leg sits just inside the
    0.3 pair gap at the squeeze, the y-order is preserved at every path
    vertex end to end (nothing crosses), and every leg holds its pad
    margins. Hand-built like ``_span_fixture``: no tris, empty order
    lists (no braids — every slit pair is live in the cost), and the
    gate/own pads land as ``pad_extra_*`` rects, which need no mesh fan
    (``guard_pad_rects`` reads their placed corners directly)."""
    n_layers = 4
    outers = frozenset({0, n_layers - 1})
    cols = 10
    pin_xy: list[tuple[float, float]] = [
        (7.2, 3.0),  # A1.1
        (8.8, 3.0),  # A1.2
        (7.2, 5.0),  # A2.1
        (8.8, 5.0),  # A2.2
        (7.2, 7.0),  # A3.1
        (8.8, 7.0),  # A3.2
        (21.2, 13.0),  # C1.1
        (22.8, 13.0),  # C1.2
        (21.2, 15.0),  # C2.1
        (22.8, 15.0),  # C2.2
        (21.2, 17.0),  # C3.1
        (22.8, 17.0),  # C3.2
    ]
    pin_net = [0, 0, 1, 1, 2, 2, 0, 0, 1, 1, 2, 2]
    branches: list[WirePath] = [
        WirePath(
            net=i,
            pin_a=1 + 2 * i,
            pin_b=6 + 2 * i,
            tris=(),
            edges=(i,),
            t=(0.5,),
            points=(
                (8.8, 3.0 + 2.0 * i),
                (15.3, 8.9 + 0.14 * i),
                (15.6, 11.2 + 0.2 * i),
                (21.2, 13.0 + 2.0 * i),
            ),
        )
        for i in range(3)
    ]
    for i in range(n_far):
        col, row = i % cols, i // cols
        x0, y0 = 26.5 + 3.3 * col, 1.2 + 1.25 * row
        pin_xy.append((x0, y0))
        pin_xy.append((x0 + 2.0, y0))
        pin_net += [3 + i, 3 + i]
        branches.append(
            WirePath(
                net=3 + i,
                pin_a=12 + 2 * i,
                pin_b=13 + 2 * i,
                tris=(),
                edges=(3 + i,),
                t=(0.5,),
                points=((x0, y0), (x0 + 1.0, y0), (x0 + 2.0, y0)),
            )
        )
    n_br = len(branches)
    n_pins = len(pin_xy)
    mesh = TopoMesh(
        verts=np.asarray(pin_xy, dtype=float),
        tris=np.zeros((0, 3), np.int32),
        edges=np.asarray([[br.pin_a, br.pin_b] for br in branches], np.int32),
        tri_edges=np.zeros((0, 3), np.int32),
        edge_tris=np.zeros((n_br, 2), np.int32),
        constrained=np.zeros(n_br, dtype=bool),
        pad_vertex={},
    )
    wires = Wires(
        branches=tuple(branches),
        order=((),) * n_br,
        capacity_mm=np.zeros(n_br),
        net_layers=tuple(outers for _ in range(3 + n_far)),
        unreachable=(),
        gap_free_mm=np.full((n_br, n_layers), -1.0),
        gap_cap=np.full((n_br, n_layers), -1),
    )
    net_pins: list[np.ndarray] = [
        np.asarray([0, 10]),
        np.asarray([2, 8]),
        np.asarray([4, 6]),
    ] + [np.asarray([12 + 2 * k, 13 + 2 * k]) for k in range(n_far)]
    d = TopoDesign(
        parts=("SLIT", "FAR"),
        pin_xy=np.asarray(pin_xy, dtype=float),
        pin_part=np.asarray([0] * 12 + [1] * (2 * n_far)),
        pin_side=np.asarray([0] * n_pins, dtype=np.int8),
        pin_label=np.asarray(["1"] * n_pins, dtype=object),
        pin_net=np.asarray(pin_net, dtype=np.int32),
        net_pins=tuple(net_pins),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(2, dtype=bool),
        freedoms=np.full(2, 3, dtype=np.uint8),
        outline=np.asarray([[-1.0, -1.0], [60.0, -1.0], [60.0, 20.0], [-1.0, 20.0]]),
        pose0=np.zeros((2, 4)),
        net_name=("N1", "N2", "N3") + tuple(f"M{i}" for i in range(n_far)),
        pin_wh=np.zeros((n_pins, 2)),
        # pads with no mesh fan must come via pad_extra_*: the two gate
        # pads (unpinned — foreign to every net) plus the 12 slit pads
        # (pinned — own-net aware, as on the real board).
        pad_extra_xy=np.asarray(
            [(13.8, 10.0), (16.8, 10.0)] + pin_xy[:12], dtype=float
        ),
        pad_extra_wh=np.asarray([(1.4, 1.2)] * 2 + [(0.9, 1.0)] * 12, dtype=float),
        pad_extra_part=np.asarray([0] * 14),
        pad_extra_pin=np.asarray([-1, -1] + list(range(12)), dtype=np.int32),
        pad_extra_side=np.asarray([2, 2] + [0] * 12, dtype=np.int8),
    )
    return d, mesh, wires


def test_spoke_offsets_many_branch_slit_cluster_scales() -> None:
    """gr520900 fix round 1: 153 branches, one tight slit. The
    per-cluster relaxation must solve only the slit cluster — finishing
    in seconds where the single whole-board solve ran one
    N-in-the-hundreds Powell (minutes) — and must hold the slit margins.
    A second run pins the fixed cluster order (determinism)."""
    from precis.pcb.topoplace.realise import _spoke_offsets

    d, mesh, wires = _many_branch_slit_fixture()
    assert len(wires.branches) == 153
    t0 = time.perf_counter()
    paths = _spoke_offsets(d, mesh, wires, 4)
    wall = time.perf_counter() - t0
    assert wall < 10.0, f"relaxation took {wall:.1f}s on a 153-branch board"
    assert len(paths) == 153
    _assert_slit_margins(paths, [(0, 1), (0, 2), (1, 2)])

    t0 = time.perf_counter()
    paths_again = _spoke_offsets(d, mesh, wires, 4)
    wall_again = time.perf_counter() - t0
    assert paths_again == paths
    assert wall_again < 10.0


def test_inner_layer_piece_resolves_a_both_ends_f_crossing() -> None:
    """gr520902 (a): two both-ends-F branches crossing on a 4-layer
    board. The whole-branch flip could only send the yielder to B.Cu
    and pay end vias back to its F-forced pads; the segment model puts
    the piece bracketed by the two crossing edges on a free layer —
    inner or B — with TWO vias and both terminal runs still on F."""
    d, mesh, wires = _via_fixture(
        pin_side=(0, 0, 0, 0),
        constrained=(False, False, False, False),
        net_layers=(frozenset({0, 1, 2, 3}),) * 2,
    )
    plan = plan_route(d, mesh, _decrowd_b1(wires), 4)

    assert plan.crossings == ()
    # the yielder (lowest _yield_rank tie-break — b0) pays two vias
    assert {v.branch for v in plan.vias} == {0}
    assert len(plan.vias) == 2
    pcs = [p for p in plan.pieces if p.branch == 0]
    assert len(pcs) == 3
    assert pcs[0].layer == 0 and pcs[2].layer == 0  # forced pads stay on F
    assert pcs[1].layer != 0  # the bracketed piece left the crossed layer
    # the other branch keeps its single F piece
    assert [p.layer for p in plan.pieces if p.branch == 1] == [0]


def _straight_branch_fixture() -> tuple[TopoDesign, TopoMesh, Wires]:
    """s4d: the both-ends-F crossing fixture with b0's taut polyline
    reduced to the straight two-point run — vertices only at bends is
    the real case a vertex-nearest split cannot express."""
    d, mesh, wires = _via_fixture(
        pin_side=(0, 0, 0, 0),
        constrained=(False,) * 4,
        net_layers=(frozenset({0, 1, 2, 3}),) * 2,
    )
    b0 = dataclasses.replace(
        wires.branches[0], t=(0.0, 1.0 / 6.0, 0.3), points=((0.0, 0.0), (8.0, 2.0))
    )
    # b1's only meeting with b0's line is at (3.4, 0.85) — arc s = 0.425,
    # INSIDE the bracket the two via sites cut: the layer-3 piece owns
    # that stretch, the layer-0 pieces never reach it. The polyline also
    # keeps 0.57+ mm off both candidate via sites — the foreign-track
    # veto stays out of THIS test's way.
    b1 = dataclasses.replace(
        wires.branches[1], points=((1.0, -1.0), (3.4, 0.85), (9.0, 0.0))
    )
    return d, mesh, dataclasses.replace(wires, branches=(b0, b1))


def test_pieces_split_at_the_via_point_not_the_nearest_vertex() -> None:
    """s4d coincident copper: a taut polyline keeps vertices only at
    bends — a straight branch is TWO points, so splitting a piece at
    the vertex nearest each via site re-issued the neighbours' geometry
    on the piece's own layer. The split is at the via's own point on
    the path: here the bracketed run is the straight segment between
    its two via sites — ``((2,0.5),(5,1.25))``, never the whole line."""
    d, mesh, wires = _straight_branch_fixture()
    plan = plan_route(d, mesh, wires, 4)

    bracket = [p for p in plan.pieces if p.branch == 0 and p.layer != 0]
    assert len(bracket) == 1
    # the bracketed run is bounded by its two via sites' own points —
    # never a neighbouring vertex (a straight branch has none)
    vs = sorted((v for v in plan.vias if v.branch == 0), key=lambda v: v.x)
    assert bracket[0].points == ((vs[0].x, vs[0].y), (vs[1].x, vs[1].y))


def _arc_s(pts: tuple[tuple[float, ...], ...], xy: tuple[float, float]) -> float:
    """``xy``'s closest point on the polyline as an arc coordinate —
    ``i + f`` for fraction ``f`` of segment ``i``."""
    best = (math.inf, 0.0)
    for i, (p, q) in enumerate(itertools.pairwise(pts)):
        dx, dy = q[0] - p[0], q[1] - p[1]
        d2 = dx * dx + dy * dy
        f = (
            0.0
            if d2 == 0
            else max(0.0, min(1.0, ((xy[0] - p[0]) * dx + (xy[1] - p[1]) * dy) / d2))
        )
        dist = math.dist(xy, (p[0] + f * dx, p[1] + f * dy))
        if dist < best[0]:
            best = (dist, i + f)
    return best[1]


def _segs_cross(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    e: tuple[float, float],
) -> bool:
    """Proper segment intersection (endpoint touches count — coincident
    copper is the defect under test)."""

    def xside(
        o: tuple[float, float], p: tuple[float, float], q: tuple[float, float]
    ) -> float:
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    d1, d2 = xside(c, e, a), xside(c, e, b)
    d3, d4 = xside(a, b, c), xside(a, b, e)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    for p, lo, hi in ((a, c, e), (b, c, e), (c, a, b), (e, a, b)):
        if (
            min(lo[0], hi[0]) - 1e-9 <= p[0] <= max(lo[0], hi[0]) + 1e-9
            and min(lo[1], hi[1]) - 1e-9 <= p[1] <= max(lo[1], hi[1]) + 1e-9
            and abs(xside(lo, hi, p)) <= 1e-9
        ):
            return True
    return False


def test_piece_points_stay_inside_their_bracketing_via_sites() -> None:
    """The general form: each piece's points lie inside the arc-length
    interval between ITS bracketing via sites — no piece carries a
    neighbouring run's stretch of the polyline — and no other-net piece
    on its layer intersects it."""
    d, mesh, wires = _straight_branch_fixture()
    plan = plan_route(d, mesh, wires, 4)

    pts = tuple(tuple(p) for p in wires.branches[0].points)
    pcs = [p for p in plan.pieces if p.branch == 0]
    assert len(pcs) == 3
    vias = sorted(
        (v for v in plan.vias if v.branch == 0),
        key=lambda v: _arc_s(pts, (v.x, v.y)),
    )
    assert len(vias) == 2
    cuts_s = [_arc_s(pts, (v.x, v.y)) for v in vias]
    bounds = [0.0, *cuts_s, float(len(pts) - 1)]
    for pi, piece in enumerate(pcs):
        for xy in piece.points[1:-1]:
            s = _arc_s(pts, xy)
            assert bounds[pi] - 1e-9 <= s <= bounds[pi + 1] + 1e-9, (
                f"piece {pi} carries {xy} outside its bracket "
                f"[{bounds[pi]}, {bounds[pi + 1]}]"
            )
    foreign = [p for p in plan.pieces if p.branch != 0]
    for piece in pcs:
        for other in foreign:
            if other.layer != piece.layer:
                continue
            for a, b in itertools.pairwise(piece.points):
                for c, e in itertools.pairwise(other.points):
                    assert not _segs_cross(a, b, c, e), (
                        f"{piece.layer}: {a}->{b} crosses {c}->{e}"
                    )


def test_net_layer_force_inner_admits_the_inner_layers() -> None:
    """gr520902 (a): a class ``layers`` rule that resolves to no outer
    layer is a force to the INNERS — the old ``_net_layers`` ANDed it
    with pad sides and collapsed it to the uncolourable empty set, so
    the wire saw outer-only candidates the class never allowed. Pad
    sides no longer narrow the set: a free net gets the whole stackup
    (Decision 6 binds only the terminal pieces)."""
    from precis.pcb.topoplace.wires import _net_layers

    d, _, _ = _via_fixture(constrained=(False,) * 4)
    d = dataclasses.replace(d, net_layer_force=np.asarray([-2, 0], np.int8))
    assert _net_layers(d, 0, 4) == frozenset({1, 2})
    assert _net_layers(d, 1, 4) == frozenset({0})
    assert _net_layers(d, 0, 2) == frozenset()  # no inners on 2L — the clash
    d_free = dataclasses.replace(d, net_layer_force=np.asarray([-1, -1], np.int8))
    assert _net_layers(d_free, 0, 4) == frozenset({0, 1, 2, 3})


def test_walked_via_sites_keep_their_own_run_boundary(monkeypatch) -> None:
    """gr520902 review a.2: a via site walked off its intended edge can
    invert the join order — the split vertices and the site xys must be
    sorted TOGETHER or a run gets joined to the wrong via."""
    from precis.pcb.topoplace import realise as realise_mod

    d, mesh, wires = _via_fixture(pin_side=(0, 0, 0, 0), constrained=(False,) * 4)
    # b0 (the yielder) mints cuts [0, 2]; make the first join site far
    # ahead and the second site early — an inverted walk.
    sites = {0: (2, 0.5, 5.0, 1.75, 0.6), 2: (0, 0.5, 0.6, 0.3, 0.6)}
    monkeypatch.setattr(realise_mod, "_site_via", lambda *a, **k: sites.get(a[3]))
    plan = plan_route(d, mesh, wires, 4)

    pcs = [p for p in plan.pieces if p.branch == 0]
    assert len(pcs) == 3
    # each piece ends where its bracketing via sits — the EARLY via
    # joins pieces 0|1, the LATE via pieces 1|2
    assert pcs[0].points[-1] == (0.6, 0.3)
    assert pcs[1].points[0] == (0.6, 0.3)
    assert pcs[1].points[-1] == (5.0, 1.75)
    assert pcs[2].points[0] == (5.0, 1.75)


def test_second_via_site_on_one_edge_is_refused(monkeypatch) -> None:
    """gr520902 review a.2: two cuts may never mint two vias on one
    edge — the second join reports ``no via site`` instead."""
    from precis.pcb.topoplace import realise as realise_mod

    d, mesh, wires = _via_fixture(pin_side=(0, 0, 0, 0), constrained=(False,) * 4)
    monkeypatch.setattr(
        realise_mod,
        "_site_via",
        lambda *a, **k: (2, 0.5, 5.0, 1.75, 0.6),
    )
    plan = plan_route(d, mesh, wires, 4)

    assert len([v for v in plan.vias if v.branch == 0]) == 1
    assert 0 in plan.no_via


def test_run_never_sits_on_a_layer_an_edge_closes() -> None:
    """gr520902 review a.1: a run may only take a layer on which EVERY
    edge it covers is open — the corridor's per-edge open mask. A
    branch routed over an edge a foreign SMD pad closes on F must not
    get a piece on F — that was the bench's coincident-copper DRC."""
    board = _sanity_board()
    design = kernel._design(board, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints, net_classes=design.graph.get("net_classes"))
    mesh = build_mesh(td, td.pose0)
    wires = build_wires(mesh, td, ir.n_layers)
    ctx = wires_mod._make_ctx(mesh, td, ir.n_layers)
    k = int(list(td.net_name).index("N1"))
    open_e = wires_mod._open_layers(
        ctx,
        td,
        wires_mod._cand_mask(wires.net_layers[k], ir.n_layers),
        "N1",
        frozenset(int(p) for p in td.net_pins[k]),
        0.15,
        0.15,
    )
    closed = [e for e in range(len(mesh.edges)) if open_e[e] and not open_e[e] & 1]
    assert closed, "sanity board must carry an F-closed edge for a foreign net"
    e0 = int(closed[0])
    pa, pb = (int(p) for p in td.net_pins[k][:2])
    u, v = (int(x) for x in mesh.edges[e0])
    b = WirePath(
        net=k,
        pin_a=pa,
        pin_b=pb,
        tris=(),
        edges=(e0,),
        t=(0.5,),
        points=(
            (float(mesh.verts[u][0]), float(mesh.verts[u][1])),
            (float(mesh.verts[v][0]), float(mesh.verts[v][1])),
        ),
    )
    order = tuple((0,) if e == e0 else () for e in range(len(mesh.edges)))
    wires2 = dataclasses.replace(wires, branches=(b,), order=order)

    _w2, runs, cuts, _xg = assign_layers(td, mesh, wires2, ir.n_layers)

    for r, c in zip(runs, cuts):
        for p_, (lo, hi) in enumerate(_run_bounds(c, len(b.edges))):
            assert all(open_e[int(b.edges[i])] >> r[p_] & 1 for i in range(lo, hi + 1))


def test_single_edge_fb_branch_takes_no_mid_cut() -> None:
    """gr520902 review a.3: the F↔B mid cut needs ``n_edges >= 2`` — on
    a one-edge branch ``n_edges // 2`` mints a cut at 0 whose second
    run covers edges ``1..0`` — a zero-edge run that still pays a via.
    The branch keeps pin_a's forced layer, uncut."""
    from precis.pcb.topoplace.realise import _initial_segment

    d, _, wires = _via_fixture(pin_side=(0, 1, 0, 0), constrained=(False,) * 4)
    b0 = wires.branches[0]
    b = dataclasses.replace(b0, edges=(0,), t=(0.5,), points=b0.points[:2])
    runs, cuts = _initial_segment(d, wires, b, 4)
    assert runs == [0]
    assert cuts == []


def test_corridor_cut_reproduces_build_wires_overflow() -> None:
    """gr520902 review b: the corridor ledger the crossing-resolution
    re-thread reuses is the SAME bookkeeping ``build_wires`` priced and
    cut under — on a real board, cutting the routed order reproduces
    the recorded overflow record exactly."""
    board = _sanity_board()
    design = kernel._design(board, None)
    ir = kernel._build_ir(design)
    footprints = kernel._footprints(design, ir)
    td = design_from_ir(ir, footprints, net_classes=design.graph.get("net_classes"))
    mesh = build_mesh(td, td.pose0)
    wires = build_wires(mesh, td, ir.n_layers)
    ctx = wires_mod._make_ctx(mesh, td, ir.n_layers)
    rules_used = [(0.15, 0.15) for _ in range(len(td.net_pins))]
    corridor = wires_mod._corridor(ctx, mesh, td, ir.n_layers, rules_used, td.pose0)
    assert corridor.cut(wires.branches, wires.order) == wires.overflow


def test_all_th_k3_resolves_by_a_free_whole_branch_flip() -> None:
    """gr520902 (b): the all-TH K3 fixture — a through-hole pad sits on
    every layer, so the yielder can move its WHOLE branch to B.Cu for
    free. Stage (a)'s span-only greedy could only buy the resolution
    with a two-via mid-piece; the local search must find the 0-via
    whole-branch flip."""
    d = example_a(pin_side=2)
    mesh = build_mesh(d, d.pose0)
    wires = build_wires(mesh, d, 2)
    plan = plan_route(d, mesh, wires, 2)

    assert plan.crossings == ()
    assert plan.vias == ()


def test_via_site_is_the_shared_endpoint_of_both_runs() -> None:
    """gr537831's join contract: a minted via IS the shared endpoint of
    the two runs it brackets — the piece split and the via site are the
    same point, so committed copper can never leave a barrel beside the
    break it was meant to bridge."""
    d, mesh, wires = _via_fixture(
        pin_side=(0, 1, 0, 1), constrained=(True, False, False, False)
    )
    plan = plan_route(d, mesh, _decrowd_b1(wires), 4)

    assert len(plan.vias) == 2
    for v in plan.vias:
        joined = [
            p
            for p in plan.pieces
            if p.branch == v.branch
            and (
                math.dist(p.points[0], (v.x, v.y)) <= 1e-6
                or math.dist(p.points[-1], (v.x, v.y)) <= 1e-6
            )
        ]
        assert len(joined) == 2
        assert {p.layer for p in joined} == {0, 3}


def test_edgeless_flip_branch_reports_no_via() -> None:
    """gr537831 (s5's BOOT): an F↔B hop with NO crossed mesh edge —
    ``edges == ()``, the whole wire inside pad territory — can never
    site a join, so the target block mints no cut. The piece must not
    emit on pin_a's side and read ``realized`` with the far pad an
    island; the branch lands on ``no_via`` like a walk that found
    nothing."""
    n_layers = 4
    mesh = TopoMesh(
        verts=np.asarray([[0.0, 0.0], [1.0, 0.0]]),
        tris=np.zeros((0, 3), np.int32),
        edges=np.asarray([[0, 1]], np.int32),
        tri_edges=np.zeros((0, 3), np.int32),
        edge_tris=np.zeros((1, 2), np.int32),
        constrained=np.zeros(1, dtype=bool),
        pad_vertex={},
    )
    b = WirePath(
        net=0,
        pin_a=0,
        pin_b=1,
        tris=(),
        edges=(),
        t=(),
        points=((0.0, 0.0), (0.04, 0.02)),
    )
    wires = Wires(
        branches=(b,),
        order=((),),
        capacity_mm=np.zeros(1),
        net_layers=(frozenset({0, n_layers - 1}),),
        unreachable=(),
        gap_free_mm=np.full((1, n_layers), -1.0),
        gap_cap=np.full((1, n_layers), -1),
    )
    d = TopoDesign(
        parts=("P1", "P2"),
        pin_xy=np.zeros((2, 2)),
        pin_part=np.asarray([0, 1]),
        pin_side=np.asarray([1, 0], dtype=np.int8),
        pin_label=np.asarray(["1"] * 2, dtype=object),
        pin_net=np.asarray([0, 0], dtype=np.int32),
        net_pins=(np.asarray([0, 1]),),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(2, dtype=bool),
        freedoms=np.full(2, 3, dtype=np.uint8),
        outline=np.asarray([[-1.0, -1.0], [2.0, -1.0], [2.0, 1.0], [-1.0, 1.0]]),
        pose0=np.zeros((2, 4)),
        net_name=("N0",),
        pin_wh=np.zeros((2, 2)),
    )
    plan = plan_route(d, mesh, wires, n_layers)

    # honest geometry stays — one piece on pin_a's (bottom) side — but
    # the join is reported unsited, never silently realized.
    assert plan.no_via == (0,)
    assert [p.layer for p in plan.pieces] == [n_layers - 1]


def _sever_fixture() -> tuple[TopoDesign, SimpleNamespace]:
    """Two pins of net N0 on top-SMD pads: P1's at world (15, 0), P2's
    at world (0, 0). Copper that reaches both is one component; copper
    that misses either — or sits on the wrong layer — is severed."""
    d = TopoDesign(
        parts=("P1", "P2"),
        pin_xy=np.asarray([[5.0, 0.0], [0.0, 0.0]]),
        pin_part=np.asarray([0, 1]),
        pin_side=np.asarray([0, 0], dtype=np.int8),
        pin_label=np.asarray(["1"] * 2, dtype=object),
        pin_net=np.asarray([0, 0], dtype=np.int32),
        net_pins=(np.asarray([0, 1]),),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(2, dtype=bool),
        freedoms=np.full(2, 3, dtype=np.uint8),
        outline=np.asarray([[-5.0, -5.0], [25.0, -5.0], [25.0, 5.0], [-5.0, 5.0]]),
        pose0=np.asarray([[10.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]]),
        net_name=("N0", "N1"),
        pin_wh=np.asarray([[0.8, 1.0], [0.8, 1.0]]),
    )
    ir = SimpleNamespace(
        n_pins=2,
        n_nets=2,
        pin_net=np.asarray([0, 0]),
        pin_instance=np.asarray([0, 1]),
        pin_label=np.asarray(["1"] * 2, dtype=object),
        instance_refdes=np.asarray(list(d.parts), dtype=object),
        net_name=np.asarray(list(d.net_name), dtype=object),
        net_plane_layers=np.zeros(2, dtype=np.uint16),
    )
    return d, ir


def test_severed_net_is_withdrawn_and_reported_unconnected() -> None:
    """gr537831's defence: before a net may report ``realized`` its
    committed copper — pieces, vias and the pads they must reach — must
    be ONE connected piece. A piece on the WRONG layer at the far pad's
    coordinates (the s5 BOOT shape) is severed and withdrawn whole like
    ``guarded``, and ``sketch_rows`` reports the net ``failed``."""
    d, ir = _sever_fixture()
    pose = np.asarray(d.pose0)

    # joined emit: one piece reaching both pads — not severed
    ok = RoutePlan(
        pieces=(
            RoutePiece(net=0, branch=0, layer=0, points=((-1.0, 0.0), (16.0, 0.0))),
        ),
        vias=(),
        layers=(0,),
    )
    assert _severed_nets(ok, d, ir, 4, pose, {}) == {}

    # the BOOT shape: a single piece on the wrong layer at the pad's XY —
    # both F.Cu pads are islands and the piece joins nothing.
    wrong_layer = RoutePlan(
        pieces=(
            RoutePiece(net=0, branch=0, layer=3, points=((14.0, 0.0), (16.0, 0.0))),
        ),
        vias=(),
        layers=(3,),
    )
    assert set(_severed_nets(wrong_layer, d, ir, 4, pose, {})) == {0}

    # two pieces on the right layer that never join — also severed.
    split = RoutePlan(
        pieces=(
            RoutePiece(net=0, branch=0, layer=0, points=((14.0, 0.0), (16.0, 0.0))),
            RoutePiece(net=0, branch=1, layer=0, points=((-1.0, 0.0), (1.0, 0.0))),
        ),
        vias=(),
        layers=(0, 0),
    )
    assert set(_severed_nets(split, d, ir, 4, pose, {})) == {0}

    # the report: severed reads as a failed net, like short-risk.
    rows = sketch_rows(
        dataclasses.replace(wrong_layer, severed=(0,)),
        d,
        ir,
        ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
    )
    assert rows["N0"]["status"] == "failed"
    assert rows["N0"]["fail"]["problems"][-1]["reason"] == "unconnected"


def test_severed_circle_pad_corner_touch_counts_severed() -> None:
    """gr537831 review: the emit check reads a round pad as its
    inscribed disk — a piece that reaches only the pad's bounding-box
    CORNER joins nothing, the same geometry
    ``connectivity._pad_primitives`` flashes. The pads are 1x1 circles
    at world (15,0)/(0,0): the piece ends at the right pad's (15.5,0.5)
    bbox corner but stays outside the r=0.5 disk — severed, where the
    old rect convention read the corner touch as a join."""
    d, ir = _sever_fixture()
    d = dataclasses.replace(
        d,
        pin_wh=np.asarray([[1.0, 1.0], [1.0, 1.0]]),
        pin_shape=np.asarray(["circle", "circle"], dtype=object),
    )
    pose = np.asarray(d.pose0)
    corner_only = RoutePlan(
        pieces=(
            RoutePiece(
                net=0,
                branch=0,
                layer=0,
                points=((-1.0, 0.0), (1.0, 0.0), (15.5, 1.5), (15.5, 0.5)),
            ),
        ),
        vias=(),
        layers=(0,),
    )
    severed = _severed_nets(corner_only, d, ir, 4, pose, {})
    assert set(severed) == {0}
    # the piece joins just the one pad at (0,0) — floating copper, dropped
    assert severed[0][0] == frozenset({0})


def test_severed_stranded_net_keeps_joined_branch() -> None:
    """gr537831 review rule: a copper component joining >=2 of the
    net's pads STAYS — a stranded net keeps the branch it did route.
    Three pads on net N0: one piece joins pads 1+2, a second floats,
    pad 3 is unreachable. The good component's copper is kept, the
    floater is dropped, and the net still reports severed."""
    d = TopoDesign(
        parts=("P1", "P2", "P3"),
        pin_xy=np.asarray([[5.0, 0.0], [0.0, 0.0], [0.0, 0.0]]),
        pin_part=np.asarray([0, 1, 2]),
        pin_side=np.asarray([0, 0, 0], dtype=np.int8),
        pin_label=np.asarray(["1"] * 3, dtype=object),
        pin_net=np.asarray([0, 0, 0], dtype=np.int32),
        net_pins=(np.asarray([0, 1, 2]),),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(3, dtype=bool),
        freedoms=np.full(3, 3, dtype=np.uint8),
        outline=np.asarray([[-5.0, -5.0], [40.0, -5.0], [40.0, 5.0], [-5.0, 5.0]]),
        pose0=np.asarray(
            [[10.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0], [30.0, 0.0, 0.0, 0.0]]
        ),
        net_name=("N0",),
        pin_wh=np.asarray([[0.8, 1.0]] * 3),
    )
    ir = SimpleNamespace(
        n_pins=3,
        n_nets=1,
        pin_net=np.asarray([0, 0, 0]),
        pin_instance=np.asarray([0, 1, 2]),
        pin_label=np.asarray(["1"] * 3, dtype=object),
        instance_refdes=np.asarray(list(d.parts), dtype=object),
        net_name=np.asarray(list(d.net_name), dtype=object),
        net_plane_layers=np.zeros(1, dtype=np.uint16),
    )
    emit = RoutePlan(
        pieces=(
            RoutePiece(net=0, branch=0, layer=0, points=((-1.0, 0.0), (15.0, 0.0))),
            RoutePiece(net=0, branch=1, layer=0, points=((36.0, 3.0), (38.0, 3.0))),
        ),
        vias=(),
        layers=(0, 0),
    )
    severed = _severed_nets(emit, d, ir, 4, np.asarray(d.pose0), {})
    assert set(severed) == {0}
    drop_p, drop_v = severed[0]
    assert drop_p == frozenset({1})  # the floater goes, branch 0 stays
    assert drop_v == frozenset()
    rows = sketch_rows(
        dataclasses.replace(emit, severed=(0,)),
        d,
        ir,
        ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
    )
    assert rows["N0"]["status"] == "failed"
    assert rows["N0"]["fail"]["problems"][-1]["reason"] == "unconnected"


def _unwind_fixture(
    interior: tuple[float, float], extra_pads: list[tuple[float, float, float, float]]
) -> tuple[TopoDesign, TopoMesh, Wires]:
    """Two branches crossing shared mesh edge 0: the peer b0 runs
    straight along y=0, the mover b1 keeps both endpoints below it
    (slot side -1) with one interior vertex. ``wires.order`` puts b0
    first so the unwind pushes b1's wrong-side vertex back below the
    peer — unless the move is refused."""
    n_layers = 4
    pin_xy = [(0.0, 0.0), (10.0, 0.0), (0.0, -1.0), (10.0, -1.0)]
    mesh = TopoMesh(
        verts=np.asarray(pin_xy, dtype=float),
        tris=np.zeros((0, 3), np.int32),
        edges=np.asarray([[0, 1]], np.int32),
        tri_edges=np.zeros((0, 3), np.int32),
        edge_tris=np.zeros((1, 2), np.int32),
        constrained=np.zeros(1, dtype=bool),
        pad_vertex={},
    )
    wires = Wires(
        branches=(
            WirePath(
                net=0,
                pin_a=0,
                pin_b=1,
                tris=(),
                edges=(0,),
                t=(0.0,),
                points=((0.0, 0.0), (10.0, 0.0)),
            ),
            WirePath(
                net=1,
                pin_a=2,
                pin_b=3,
                tris=(),
                edges=(0,),
                t=(0.0,),
                points=((0.0, -1.0), interior, (10.0, -1.0)),
            ),
        ),
        order=((0, 1),),
        capacity_mm=np.zeros(1),
        net_layers=(frozenset({0, n_layers - 1}),) * 2,
        unreachable=(),
        gap_free_mm=np.full((2, n_layers), -1.0),
        gap_cap=np.full((2, n_layers), -1),
    )
    d = TopoDesign(
        parts=("P",),
        pin_xy=np.asarray(pin_xy, dtype=float),
        pin_part=np.zeros(4, dtype=np.int64),
        pin_side=np.zeros(4, dtype=np.int8),
        pin_label=np.asarray(["1"] * 4, dtype=object),
        pin_net=np.asarray([0, 0, 1, 1], dtype=np.int32),
        net_pins=(np.asarray([0, 1]), np.asarray([2, 3])),
        flex=(),
        swap_groups=(),
        fixed=np.zeros(1, dtype=bool),
        freedoms=np.full(1, 3, dtype=np.uint8),
        outline=np.asarray([[-1.0, -2.0], [11.0, -2.0], [11.0, 12.0], [-1.0, 12.0]]),
        pose0=np.zeros((1, 4)),
        net_name=("N0", "N1"),
        pin_wh=np.zeros((4, 2)),
        pad_extra_xy=np.asarray([[x, y] for x, y, _w, _h in extra_pads] or []).reshape(
            -1, 2
        ),
        pad_extra_wh=np.asarray([[w, h] for _x, _y, w, h in extra_pads] or []).reshape(
            -1, 2
        ),
        pad_extra_part=np.zeros(len(extra_pads), dtype=np.int64),
        pad_extra_pin=np.full(len(extra_pads), -1, dtype=np.int64),
        pad_extra_side=np.zeros(len(extra_pads), dtype=np.int8),
    )
    return d, mesh, wires


def test_unwind_far_side_move_is_rejected() -> None:
    """gr537831 review: the unwind's push is bounded — an interior
    vertex whose slot-side target sits more than the cap's few gaps
    away is a weave, not a fan, and keeps its old position instead of
    being dragged across the board."""
    from precis.pcb.topoplace.realise import _spoke_offsets

    d, mesh, wires = _unwind_fixture((5.0, 10.0), [])
    paths = _spoke_offsets(d, mesh, wires, 4)
    assert (5.0, 10.0) in paths[1]


def test_unwind_move_leading_through_foreign_pad_is_rejected() -> None:
    """gr537831 review: the moved vertex's TWO new legs are checked —
    a candidate point can be clean while its leg crosses a foreign
    pad. The unpinned land sits under only the pushed leg's path: the
    old legs pass 0.31 mm clear of it while the new leg crosses it, so
    the point intrusion tie alone (the old check) accepts the move and
    the leg margin check refuses it — the vertex stays on its wrong
    side."""
    from precis.pcb.topoplace.realise import _spoke_offsets

    d, mesh, wires = _unwind_fixture((5.0, 0.2), [(4.35, -0.475, 0.7, 0.25)])
    paths = _spoke_offsets(d, mesh, wires, 4)
    assert (5.0, 0.2) in paths[1]
