"""Slice 5 (phase 4): the topo realiser — per-branch layer assignment
(Decision 6's pad-side forcing + crossing yield), via sites at
``(edge_id, t)``, and copper emitted through the normal
``RealizeResult``/``to_gerber_model`` path. The sanity board's
acceptance is the spec's: every wire gets a layer, every via a site,
and the emitted copper passes DRC with 0 errors."""

from __future__ import annotations

import precis_geom.pcb as pcb
from precis.pcb import drc
from precis.pcb import local as kernel
from precis.pcb import realize as pcb_realize
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import PcbIR
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.realise import (
    plan_route,
    realize_result,
    route_design,
    widen_power,
)
from precis.pcb.topoplace.wires import build_wires


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


def _drc_errors(result: pcb_realize.RealizeResult, ir: PcbIR, layers: list[str]) -> int:
    model = pcb_realize.to_gerber_model(
        result, ir, layers=layers, outline=[list(p) for p in (ir.outline or [])]
    )
    return sum(
        1
        for f in drc.run_geometric_drc(
            model, capability=capability_for("4layer"), outline=model["outline"]
        )
        if f.severity == "error"
    )


def test_widen_power_widens_gnd_where_clearance_allows() -> None:
    """The post-route stage's live case: the gnd rail leaves its rule
    width for the widest ladder step the board's own DRC still scores
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
        res, {gnd_ir}, drc_errors=lambda r: _drc_errors(r, ir, layers)
    )
    assert widths[gnd_ir] > 0.15  # left the default rule width
    assert {t.width_mm for t in res2.tracks if t.net_id == gnd_ir} == {widths[gnd_ir]}
    assert _drc_errors(res2, ir, layers) == 0
    sig = [t for t in res2.tracks if t.net_id != gnd_ir]
    assert sig and all(t.width_mm == 0.15 for t in sig)


def test_widen_power_keeps_the_rule_width_when_the_oracle_refuses() -> None:
    """The contract without a board: accepted widths never push the
    error count over the baseline, and a net the oracle always refuses
    keeps its rule width verbatim."""
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

    def errs(r: pcb_realize.RealizeResult) -> int:
        # net 1 clears only at <=0.5; net 2 clears nothing above its rule
        return sum(
            1
            for t in r.tracks
            if (t.net_id == 1 and t.width_mm > 0.5)
            or (t.net_id == 2 and t.width_mm > 0.15)
        )

    res2, widths = widen_power(res, {0, 1, 2}, drc_errors=errs)
    assert widths == {0: 1.0, 1: 0.5, 2: 0.15}
    assert {t.net_id: t.width_mm for t in res2.tracks} == widths
