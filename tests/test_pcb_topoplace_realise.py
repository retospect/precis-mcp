"""Slice 5 (phase 4): the topo realiser — per-branch layer assignment
(Decision 6's pad-side forcing + crossing yield), via sites at
``(edge_id, t)``, and copper emitted through the normal
``RealizeResult``/``to_gerber_model`` path. The sanity board's
acceptance is the spec's: every wire gets a layer, every via a site,
and the emitted copper passes DRC with 0 errors."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

import precis_geom.pcb as pcb
from precis.pcb import drc
from precis.pcb import local as kernel
from precis.pcb import realize as pcb_realize
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import PcbIR
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.mesh import TopoMesh, build_mesh
from precis.pcb.topoplace.realise import (
    plan_route,
    realize_result,
    route_design,
    sketch_rows,
    widen_power,
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


def _via_fixture(
    *, pin_side: tuple[int, ...] = (0, 1, 0, 0), constrained: tuple[bool, ...]
) -> tuple[TopoDesign, TopoMesh, Wires]:
    """gr520901 — a two-branch synthetic: b0 (net 0) crosses e0→e2, b1
    (net 1) crosses e1→e3, and the two shared edges carry opposite order
    so the branches cross. ``constrained`` marks which edges are ring
    edges a via may never sit on; e0 plays the pad's own ring edge the
    esp32c3 bench minted end vias onto."""
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
    )
    return d, mesh, wires


def test_end_via_never_sites_on_a_pad_ring_edge() -> None:
    """gr520901's acceptance unit: a yielded F↔B branch whose FIRST
    crossed edge is its own pad's ring edge mints its end via on the
    first keepout-clean edge, not on the ring."""
    d, mesh, wires = _via_fixture(constrained=(True, False, False, False))
    plan = plan_route(d, mesh, wires, 4)

    # b0 (rank-1 F↔B) yields the shared-edge crossing to b1 and flips to
    # B.Cu; its top-SMD pin_a forces an end via at the branch start —
    # where the wire's first crossed edge is the pad's own ring edge.
    assert len(plan.vias) == 1
    (via,) = plan.vias
    assert via.branch == 0
    assert not mesh.constrained[via.edge]
    assert via.edge == 1  # walked to the first clean edge
    assert plan.no_via == ()


def test_via_site_rejects_a_spot_crowding_another_via() -> None:
    """The same siting walk refuses a site within via_dia + clearance of
    an already placed via: b1's mid via skips the edge b0's via took."""
    d, mesh, wires = _via_fixture(
        pin_side=(0, 1, 0, 1), constrained=(True, False, False, False)
    )
    plan = plan_route(d, mesh, wires, 4)

    assert len(plan.vias) == 2
    via0 = next(v for v in plan.vias if v.branch == 0)
    via1 = next(v for v in plan.vias if v.branch == 1)
    assert not mesh.constrained[via0.edge]
    # b0's end via takes e1 (2.0, 1.5); b1's mid via wants e1 too —
    # (2.0, 1.2) at t=0.4 sits 0.3 mm away, inside the 0.75 mm keep-out,
    # so it walks on to e2.
    assert via1.edge == 2


def test_no_clean_edge_reports_no_via_site_instead_of_emitting() -> None:
    """Every edge on the branch is a ring edge: no via is minted and the
    net lands on ``pcb_routes`` as ``failed: no via site`` — a report,
    never keepout-breaking copper."""
    d, mesh, wires = _via_fixture(constrained=(True, True, True, False))
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
