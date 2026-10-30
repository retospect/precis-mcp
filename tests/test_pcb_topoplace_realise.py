"""Slice 5 (phase 4): the topo realiser — per-branch layer assignment
(Decision 6's pad-side forcing + crossing yield), via sites at
``(edge_id, t)``, and copper emitted through the normal
``RealizeResult``/``to_gerber_model`` path. The sanity board's
acceptance is the spec's: every wire gets a layer, every via a site,
and the emitted copper passes DRC with 0 errors."""

from __future__ import annotations

import dataclasses
import math
from types import SimpleNamespace

import numpy as np
import pytest
from shapely.geometry import Point, Polygon

import precis_geom.pcb as pcb
from precis.pcb import drc
from precis.pcb import local as kernel
from precis.pcb import realize as pcb_realize
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import PcbIR, from_graph
from precis.pcb.session import clearance_net_rules, route_net_rules
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace.design import TopoDesign, TopoZone
from precis.pcb.topoplace.mesh import TopoMesh, build_mesh
from precis.pcb.topoplace.realise import (
    RoutePiece,
    RoutePlan,
    _guarded_nets,
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
        net_layers=(outers, outers),
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
    plan = plan_route(d, mesh, wires, 4)

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
    plan = plan_route(d, mesh, wires, 4)

    assert len(plan.vias) == 2
    via0 = next(v for v in plan.vias if v.branch == 0)
    via1 = next(v for v in plan.vias if v.branch == 1)
    assert (via0.x, via0.y) == pytest.approx((2.0, 1.5))  # b0 keeps its anchor
    assert math.dist((via0.x, via0.y), (via1.x, via1.y)) >= 0.75 - 1e-9


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


def _stall_fixture() -> tuple[TopoDesign, Wires]:
    """gr524239 — the yield-stall synthetic: branch A (TH, rank 0) sits
    on F.Cu and inverts with B (also F.Cu), C and D (both B.Cu) on
    disjoint edge pairs. Only (A,B) is live initially; the rank-preferred
    yield (flip A) moves A onto B.Cu where its inverted orders against
    C and D turn TWO dormant pairs live — the pre-pass-based yielder
    broke right there and returned the stall. Flipping B instead
    resolves the live pair and touches nothing else."""
    n_layers = 4
    outers = frozenset({0, n_layers - 1})
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
    return d, wires


def test_assign_layers_pass_based_yield_unstalls_the_rank_trap() -> None:
    """The pre-gr524239 greedy yielded the rank-preferred branch of the
    minimum pair and stopped when that flip failed to improve: here the
    rank move (flip TH branch A onto B.Cu) turns two dormant crossings
    live, so the old loop stalled at 1 live pair. The pass-based yielder
    trials BOTH branches' flips and takes the one that empties the
    live set — B to B.Cu leaves every other pair layer-split."""
    d, wires = _stall_fixture()
    layers, crossings = assign_layers(d, wires, 4)
    assert crossings == ()
    assert layers[0] == 0  # A stays on F.Cu — the rank-preferred move was refused
    assert layers[1] == 3  # B yields to B.Cu instead


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
    trades it for a new crossing with C or D), so the layer pass leaves
    it live. The span flip changes A's layer only on the shared,
    order-inverting run: the pair resolves, nothing else moves, and A
    emerges via-bracketed with three runs F.Cu -> B.Cu -> F.Cu."""
    d, mesh, wires = _span_fixture()
    layers, crossings = assign_layers(d, wires, 4)
    assert crossings == ((0, 1),)  # the whole-branch stall, pinned
    plan = plan_route(d, mesh, wires, 4)
    assert plan.crossings == ()
    assert plan.no_via == ()
    vias_a = [v for v in plan.vias if v.branch == 0]
    assert len(vias_a) == 2
    runs_a = sorted(
        (p for p in plan.pieces if p.branch == 0), key=lambda p: p.points[0][0]
    )
    assert [p.layer for p in runs_a] == [0, 3, 0]
