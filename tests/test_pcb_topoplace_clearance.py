"""G2f: a centreline routes in free space. Every constraint ring in the mesh
stands off its copper by what the wire's copper needs (pad: ``trace/2 +
clearance``, keepout: ``trace/2``, board edge: ``max(clearance, edge
clearance) + trace/2``), so the taut wire pivots on ring vertices at exact
clearance and ``WirePath.points`` is the one geometry. The tests read the taut
points and the emitted pieces, never an edge-level mask."""

from __future__ import annotations

import dataclasses
import time

import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon

import precis_geom.pcb as pcb
from precis.pcb import local as kernel
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.realise import SpreadOverflow, _spread_places, route_design
from precis.pcb.topoplace.wires import build_wires
from tests._timing import budget
from tests.test_pcb_topoplace_route import _channel_wall_design
from tests.workspaces.test_pcb_tier2 import _example

#: The default rule (0.15 mm track, 0.15 mm clearance): a centreline owes a
#: foreign pad ``trace/2 + clearance``.
TRACE, CLEAR = 0.15, 0.15
NEED = TRACE / 2.0 + CLEAR
TOL = 1e-6


def _grown(d, **kw):
    return build_mesh(d, d.pose0, trace_mm=TRACE, clearance_mm=CLEAR, **kw)


def _rules(d, rule=(TRACE, CLEAR, 0.45)):
    return tuple(rule for _ in d.net_pins)


def _pad_board(pads: list[tuple[str, float, float, float, float]]) -> pcb.Board:
    """N1 from P1 (5, 10) to P2 (25, 10), plus unwired pads ``(name, x, y,
    w, h)`` — foreign copper to every net."""
    b = pcb.Board("clr", stackup=4, outline=pcb.rect(30.0, 20.0))
    dot = b.footprint(pcb.Footprint("DOT", [pcb.pad("1", "rect", 0, 0, w=0.9, h=0.9)]))
    q1 = b.part("P1", footprint=dot, pins=["1"]).at(5.0, 10.0)
    q2 = b.part("P2", footprint=dot, pins=["1"]).at(25.0, 10.0)
    for name, x, y, w, h in pads:
        # drilled, so the bar walls every layer and the wire cannot hop under it
        fp = b.footprint(
            pcb.Footprint(f"F{name}", [pcb.pad("1", "rect", 0, 0, w=w, h=h, drill=0.3)])
        )
        b.part(name, footprint=fp, pins=["1"]).at(x, y)
    for p in (q1["1"], q2["1"]):
        p.etype("bidi", fmax_hz=0.0, assume="clearance wire end")
    b.wire(q1["1"], q2["1"], name="N1").current = 0.001
    return b


def _topo(board: pcb.Board):
    design = kernel._design(board, None)
    ir = kernel._build_ir(design)
    fps = kernel._footprints(design, ir)
    td = design_from_ir(
        ir,
        fps,
        net_classes=design.graph.get("net_classes"),
        route_planes=True,
        features=design.features,
    )
    return td, ir


def test_wire_past_a_pad_corner_keeps_clearance_on_emitted_copper() -> None:
    """A tall foreign bar across the lane: the taut wire pivots at the bar's
    grown corner, 0.225 from its copper (it used to pivot ON the corner and
    leave the clearance to a later kink), and so do the emitted pieces."""
    board = _pad_board([("BAR", 15.0, 10.0, 0.6, 4.0)])
    td, ir = _topo(board)
    mesh = _grown(td)
    wires = build_wires(mesh, td, ir.n_layers, _rules(td), pose=td.pose0)
    (branch,) = wires.branches
    bar = Polygon([(14.7, 8.0), (15.3, 8.0), (15.3, 12.0), (14.7, 12.0)])
    taut = LineString([(float(x), float(y)) for x, y in branch.points])
    assert len(branch.points) >= 3, "the wire must bend round the bar"
    assert bar.distance(taut) >= NEED - TOL, bar.distance(taut)
    plan, _res = route_design(td, ir)
    assert plan.pieces and plan.no_via == () and plan.guarded == ()
    for pc in plan.pieces:
        line = LineString([(float(x), float(y)) for x, y in pc.points])
        assert bar.distance(line) >= NEED - TOL, (pc.layer, bar.distance(line))


def test_a_gap_under_the_requirement_is_a_named_failure() -> None:
    """The channel wall's 0.6 mm gap leaves 0.15 mm of centreline room
    (``0.6 - 2 x 0.225``): one wire per layer passes. Three nets need three
    slots, so the cut is recorded and named with the two pads bounding it,
    and no wire is squeezed past clearance. Shrinking the gap to 0.4 mm
    overlaps the grown rings: nothing passes and the net is unreachable."""
    d = _channel_wall_design(3)
    mesh = _grown(d)
    w = build_wires(mesh, d, 2, _rules(d), pose=d.pose0)
    assert w.overflow, "three wires through a 2-slot channel must cut"
    cut = max(w.overflow, key=lambda c: c.demand - c.cap)
    assert (cut.demand, cut.cap) == (3.0, 2.0)
    assert len(cut.owners) == 2 and all(o.startswith("H") for o in cut.owners)
    walls = [p.poly for p in mesh.pads if p.pin >= 6]  # the H wall pads
    for b in w.branches:
        line = LineString([(float(x), float(y)) for x, y in b.points])
        assert all(p.distance(line) >= NEED - TOL for p in walls)

    # the same wall with the opening shut to 0.4 mm
    pose = np.array(d.pose0)
    n_nets = 3
    pose[2 * n_nets + 10, 1] = 0.4
    shut = dataclasses.replace(d, pose0=pose)
    m2 = _grown(shut)
    w2 = build_wires(m2, shut, 2, _rules(shut), pose=shut.pose0)
    assert w2.unreachable, "a 0.4 mm gap under a 0.45 requirement is a named failure"
    assert w2.branches == () or all(
        max(p[0] for p in b.points) <= 0.0 or min(p[0] for p in b.points) >= 0.0
        for b in w2.branches
    )


def test_a_ring_on_the_top_layer_does_not_bend_a_bottom_layer_wire() -> None:
    """A top-side SMD pad's ring walls layer 0 only: a wire between two
    bottom-side pads runs straight under it."""
    b = pcb.Board("under", stackup=4, outline=pcb.rect(30.0, 20.0))
    dot = b.footprint(pcb.Footprint("DOT", [pcb.pad("1", "rect", 0, 0, w=0.9, h=0.9)]))
    bar = b.footprint(pcb.Footprint("BAR", [pcb.pad("1", "rect", 0, 0, w=0.3, h=4.0)]))
    q1 = b.part("P1", footprint=dot, pins=["1"]).at(5.0, 10.0, side="bottom")
    q2 = b.part("P2", footprint=dot, pins=["1"]).at(25.0, 10.0, side="bottom")
    b.part("TOP", footprint=bar, pins=["1"]).at(15.0, 10.0)
    for p in (q1["1"], q2["1"]):
        p.etype("bidi", fmax_hz=0.0, assume="under wire end")
    b.wire(q1["1"], q2["1"], name="N1").current = 0.001
    td, ir = _topo(b)
    wires = build_wires(_grown(td), td, ir.n_layers, _rules(td), pose=td.pose0)
    (branch,) = wires.branches
    assert {round(y, 6) for _x, y in branch.points} == {10.0}


def test_a_pin_inside_a_foreign_grown_polygon_stays_reachable() -> None:
    """A 0.2 mm pad whose centre lies 0.15 mm from a foreign pad is inside
    that pad's grown ring. Its anchor moves onto its own copper outside the
    ring, so the wire has a vertex to start from and the net is routed."""
    b = pcb.Board("tight", stackup=4, outline=pcb.rect(30.0, 20.0))
    thin = b.footprint(
        pcb.Footprint("THIN", [pcb.pad("1", "rect", 0, 0, w=0.2, h=0.2)])
    )
    big = b.footprint(pcb.Footprint("BIG", [pcb.pad("1", "rect", 0, 0, w=1.0, h=1.0)]))
    a = b.part("A", footprint=thin, pins=["1"]).at(10.0, 10.0)
    c = b.part("C", footprint=thin, pins=["1"]).at(20.0, 10.0)
    b.part("F", footprint=big, pins=["1"]).at(10.65, 10.0)  # edge 0.05 from A's
    for p in (a["1"], c["1"]):
        p.etype("bidi", fmax_hz=0.0, assume="tight wire end")
    b.wire(a["1"], c["1"], name="N1").current = 0.001
    td, ir = _topo(b)
    mesh = _grown(td)
    pin = next(
        p for p in range(len(td.pin_part)) if td.parts[int(td.pin_part[p])] == "A"
    )
    foreign = next(p for p in mesh.pads if td.parts[p.part] == "F")
    own = next(p for p in mesh.pads if p.pin == pin)
    v = Point(*mesh.verts[int(mesh.pin_vertex[pin])])
    assert foreign.poly.buffer(NEED).distance(v) > 0.0, (
        "anchor is inside the foreign ring"
    )
    assert own.poly.buffer(1e-6).contains(v), "anchor left its own copper"
    wires = build_wires(mesh, td, ir.n_layers, _rules(td), pose=td.pose0)
    assert not wires.unreachable and len(wires.branches) == 1


def test_the_outline_keeps_clearance_and_pads_near_the_edge_survive() -> None:
    """The board edge is a second ring inside the real outline. The 0.3 mm
    residual between the wall's end pad and the outline (gr551114) is under
    ``0.225 + 0.225``: the third net no longer slips through it, the cut is
    recorded, and no wire sits within clearance of a wall pad. A pad 0.1 mm
    from the outline is still meshed: no MeshConflictError, its vertex in a
    triangle."""
    d = _channel_wall_design(3)
    d = dataclasses.replace(
        d,
        outline=np.asarray([[-10, -10.0], [10, -10.0], [10, 10.0], [-10, 10.0]], float),
    )
    mesh = _grown(d)
    w = build_wires(mesh, d, 2, _rules(d), pose=d.pose0)
    assert w.overflow
    walls = [p.poly for p in mesh.pads if p.pin >= 6]
    for b in w.branches:
        line = LineString([(float(x), float(y)) for x, y in b.points])
        assert all(p.distance(line) >= NEED - TOL for p in walls), b.points
        assert min(y for _x, y in b.points) >= -10.0 + NEED - TOL
        assert max(y for _x, y in b.points) <= 10.0 - NEED + TOL
    # the real outline ring is still in the mesh (it drops the triangles
    # outside), beside the inset one
    out_edges = {tuple(sorted(e)) for e in mesh.edges[mesh.edge_owner[:, 0] == -1]}
    assert len(out_edges) > 8
    # pads at the outline: the top wall pad's edge is 0.3 mm from it, the
    # bottom one 0.1 mm
    assert np.isin(mesh.pin_vertex, mesh.tris.ravel()).all()


def test_a_coarser_net_keeps_the_edge_level_closure() -> None:
    """The mesh stands its rings off for the FINEST rule (0.15/0.15). A net
    with a coarser rule (0.3/0.3, needing 0.9 of a gap) still meets
    ``_pad_eval``'s edge closure at its own radius: through a 0.8 mm gap the
    fine net passes and the coarse one is unreachable, however far the ring
    leaves the centreline room."""
    d = _channel_wall_design(2)
    pose = np.array(d.pose0)
    pose[2 * 2 + 10, 1] = 0.8  # gap 0.8 between the pads at -0.6 and 0.8
    d = dataclasses.replace(d, pose0=pose)
    mesh = _grown(d)
    rules = list(_rules(d))
    rules[1] = (0.3, 0.3, 0.6)
    w = build_wires(mesh, d, 2, tuple(rules), pose=d.pose0)
    fine = [b for b in w.branches if b.net == 0]
    assert fine and min(x for b in fine for x, _y in b.points) < 0 < max(
        x for b in fine for x, _y in b.points
    )
    assert [u.net for u in w.unreachable] == [1]


def test_spread_leaves_apart_wires_where_they_cross_an_edge() -> None:
    """Twelve wires cross one long sliver edge; one pair sits 0.14 mm apart.
    Only that pair moves, as a block about its mean: the wires 2.54 mm apart
    keep their crossing (the old spread packed all twelve into one 3.6 mm
    stack, which left three straight wires bent into a deadlock and failed
    five sanity nets)."""
    nat = [0.0, 2.54, 5.08, 10.0, 10.14, 20.0, 22.54]
    slot = [0.3] * (len(nat) - 1)
    places = _spread_places(nat, slot, 0.0, 47.8)
    assert isinstance(places, list)
    assert places[:3] == pytest.approx(nat[:3])
    assert places[5:] == pytest.approx(nat[5:])
    assert places[4] - places[3] == pytest.approx(0.3)
    assert (places[3] + places[4]) / 2 == pytest.approx((nat[3] + nat[4]) / 2)
    # the free stretch bounds it, and clipping never pushes a block into its
    # neighbour (the clamp used to be per block)
    for nat_c, slot_c, lo, hi in (
        ([0.05, 0.1, 0.55], [0.3, 0.3], 0.0, 10.0),
        ([9.45, 9.9, 9.95], [0.3, 0.3], 0.0, 10.0),
        ([0.0, 0.1, 5.0], [0.3, 0.3], 0.0, 10.0),
    ):
        got = _spread_places(nat_c, slot_c, lo, hi)
        assert isinstance(got, list)
        assert min(got) >= lo - 1e-9 and max(got) <= hi + 1e-9
        for k, sl in enumerate(slot_c):
            assert got[k + 1] - got[k] >= sl - 1e-9, (nat_c, got)
    # the run needs 0.6 mm and the stretch is 0.5: named, not spilled past hi
    assert isinstance(
        _spread_places([0.1, 0.2, 0.3], [0.3, 0.3], 0.0, 0.5), SpreadOverflow
    )


def test_a_spread_run_that_does_not_fit_is_named_not_clamped() -> None:
    """A 0.3 mm slot over a 0.2 mm stretch: the overflow carries the mm it
    needed and had (``_spoke_offsets`` logs it by edge and leaves the
    crossings put) instead of packing the wires past the stretch."""
    got = _spread_places([0.0, 0.1], [0.3], 0.0, 0.2)
    assert isinstance(got, SpreadOverflow)
    assert (got.need, got.room) == (pytest.approx(0.3), pytest.approx(0.2))
    # exactly fitting is not an overflow
    assert _spread_places([0.0, 0.1], [0.3], 0.0, 0.3) == pytest.approx([0.0, 0.3])


def test_sanity_board_routes_all_twenty_nets_with_clean_drc() -> None:
    """Four headers, 20 nets, the TB nets crossing the LR nets through
    shared sliver edges: stubs and runs on layer 3 must not run over a
    neighbour. 18/20 realised before the spread fix (G2f: two nets withdrawn
    as short-risk), and every one now."""
    from precis.pcb import drc as pcb_drc
    from precis.pcb import realize as pcb_realize
    from precis.pcb import session as pcb_session

    board = _example("sanity")
    design = kernel._design(board, None)
    ir, fps, _m = kernel._prepare(design)
    t0 = time.perf_counter()
    stage = kernel._route_stage(design, ir, fps)
    elapsed = time.perf_counter() - t0
    # measured: ~18s in a plain local run, 31-37s on the macOS nightly
    # runner (gr647532 — a 30s cap tripped on runner speed, not on a route
    # blowup). 60s still trips if the stage goes quadratic.
    assert elapsed < budget(60.0), f"route stage took {elapsed:.1f}s"
    done = [n for n, r in stage.rows.items() if r["status"] == "realized"]
    assert len(done) == 20, sorted(set(stage.rows) - set(done))
    model = pcb_session._copper_model(
        ir,
        pcb_realize.result_copper_rows(
            stage.result, ir, design.layer_names, derived=True
        ),
        footprints=fps,
    )
    errors = [
        f
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


def test_a_pad_near_the_edge_opens_the_band_to_its_own_net_only() -> None:
    """A top-side pad 0.1 mm from the outline sits inside the edge band (the
    inset board ring is 0.225 in). The ring stretch within the pad's grown
    ring opens for the pad's own net on the pad's layer so the pad stays
    reachable. It must not open for another net, nor on the other face: a
    bottom-layer wire entering the band would run inside edge clearance."""
    from precis.pcb.topoplace import wires as wires_mod

    b = pcb.Board("edge", stackup=4, outline=pcb.rect(30.0, 20.0))
    dot = b.footprint(pcb.Footprint("DOT", [pcb.pad("1", "rect", 0, 0, w=0.9, h=0.9)]))
    e1 = b.part("E1", footprint=dot, pins=["1"]).at(0.55, 10.0)  # 0.1 from x=0
    e2 = b.part("E2", footprint=dot, pins=["1"]).at(25.0, 10.0)
    c1 = b.part("C1", footprint=dot, pins=["1"]).at(10.0, 5.0, side="bottom")
    c2 = b.part("C2", footprint=dot, pins=["1"]).at(20.0, 5.0, side="bottom")
    for p in (e1["1"], e2["1"], c1["1"], c2["1"]):
        p.etype("bidi", fmax_hz=0.0, assume="edge wire end")
    b.wire(e1["1"], e2["1"], name="N1").current = 0.001
    b.wire(c1["1"], c2["1"], name="N2").current = 0.001
    td, ir = _topo(b)
    mesh = _grown(td)
    ctx = wires_mod._make_ctx(mesh, td, ir.n_layers)
    assert ctx.board_reach, "the pad's ring must reach the inset board ring"
    top, bottom = 1 << 0, 1 << (ir.n_layers - 1)
    nets = {n: k for k, n in enumerate(td.net_name)}
    open_n1 = wires_mod._net_masks(ctx, td, nets["N1"], ir.n_layers, TRACE, CLEAR, 0.45)
    open_n2 = wires_mod._net_masks(ctx, td, nets["N2"], ir.n_layers, TRACE, CLEAR, 0.45)
    reach = sorted(ctx.board_reach)
    assert all(open_n1.open[e] & top for e in reach), "own net must reach its pad"
    assert not any(open_n1.open[e] & bottom for e in reach)
    assert not any(open_n2.open[e] for e in reach), "another net must stay out"
    wires = build_wires(mesh, td, ir.n_layers, _rules(td), pose=td.pose0)
    assert not wires.unreachable and len(wires.branches) == 2


def test_flat_sliver_loop_that_cannot_progress_raises_instead_of_cycling(
    monkeypatch,
) -> None:
    """The flat-sliver pass splits each chord over a collinear vertex. A
    triple that ``split_edge`` cannot remove (here: every triangle reads as
    a collinear triple and the split leaves the soup alone) used to requeue
    forever; a triple that comes back more often than the mesh has vertices
    now raises ``MeshConflictError``. (Real boards bring a triple back up to
    four times while a sliver walks a collinear row, and still finish.)"""
    from precis.pcb.topoplace import mesh as mesh_mod

    td, _ir = _topo(_pad_board([]))
    calls = {"n": 0}

    def stuck(self, e, w):  # a split that removes nothing
        calls["n"] += 1
        assert calls["n"] < 5000, "flat-sliver loop did not terminate"

    monkeypatch.setattr(mesh_mod._Soup, "split_at_vertex", stuck)
    monkeypatch.setattr(mesh_mod, "_on_segment", lambda verts, a, w, b: True)
    with pytest.raises(mesh_mod.MeshConflictError, match="survived"):
        build_mesh(td, td.pose0)
    assert calls["n"] >= 1
