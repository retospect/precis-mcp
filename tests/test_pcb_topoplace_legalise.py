"""td569542 — the joint legalisation round: moved via centres, per-
crossing wire offsets and unfrozen part translations after the wire
search, before emission. Homotopy is the contract: ``edges``/``tris``/
``layers``/branch identity never change; only geometry does."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from precis.pcb.clearance import FAB_QUANTUM_MM
from precis.pcb.topoplace import legalise_route as lr
from precis.pcb.topoplace import wires as wm
from precis.pcb.topoplace.crossings import edge_xy
from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.mesh import build_mesh
from tests.fixtures.pcb.topo_example_a import example_a

_RULES_T = tuple((0.15, 0.15, 0.45) for _ in range(8))


def _wires_for(d: TopoDesign, n_layers: int = 2):
    pose = np.asarray(d.pose0, dtype=np.float64)
    mesh = build_mesh(d, pose)
    rules_t = _RULES_T[: len(d.net_pins)]
    wires = wm.build_wires(mesh, d, n_layers, rules_t, pose=pose)
    return mesh, wires, rules_t


def test_deterministic_on_example_a() -> None:
    """(e) two runs over the same wires agree bit for bit."""
    d = example_a()
    mesh, wires, rules_t = _wires_for(d)
    pose = np.asarray(d.pose0, dtype=np.float64)
    a = lr.legalise_route(d, mesh, wires, pose, n_layers=2, rules_t=rules_t)
    b = lr.legalise_route(d, mesh, wires, pose, n_layers=2, rules_t=rules_t)
    assert a.vias == b.vias
    assert a.offsets == b.offsets
    assert a.reroute == b.reroute
    np.testing.assert_array_equal(a.pose, b.pose)


def test_fixed_parts_never_move() -> None:
    """(c, half) a fully-fixed design returns the input pose verbatim —
    legalise has no part variables to spend."""
    d0 = example_a()
    d = dataclasses.replace(d0, fixed=np.ones(len(d0.parts), dtype=bool))
    mesh, wires, rules_t = _wires_for(d)
    pose = np.asarray(d.pose0, dtype=np.float64)
    res = lr.legalise_route(d, mesh, wires, pose, n_layers=2, rules_t=rules_t)
    np.testing.assert_array_equal(res.pose, pose)


def test_apply_legalise_preserves_homotopy() -> None:
    """Moved vias/offsets rewrite geometry only: edges, tris, layers,
    nets and branch count are untouched; the via vertex and crossing
    params follow the move."""
    d = example_a()
    mesh, wires, _rules_t = _wires_for(d)
    b = wires.branches[0]
    if not b.edges:
        pytest.skip("example_a net A routed edgeless")
    e = int(b.edges[0])
    p_new = edge_xy(mesh, e, 0.8)
    vias = {(0, 0): p_new} if b.vias else {}
    offsets = {(0, 0): 0.8}
    out = lr.apply_legalise(wires, vias, offsets, mesh)
    bo = out.branches[0]
    assert bo.edges == b.edges and bo.tris == b.tris and bo.layers == b.layers
    assert bo.net == b.net
    if vias:
        v = bo.vias[0]
        assert (v.x, v.y) == pytest.approx(p_new)
        assert bo.points[v.k] == pytest.approx(p_new)
    if offsets:
        assert bo.t[0] == pytest.approx(0.8)
        # the crossing became a polyline vertex on its edge
        assert any(pt == pytest.approx(p_new) for pt in bo.points)


def _hop_design():
    """example_a with net B's first pin flipped to back-SMD — a
    mandatory F->B hop the search plants on net B."""
    d0 = example_a()
    side = np.asarray(d0.pin_side).copy()
    side[int(d0.net_pins[1][0])] = 1
    return dataclasses.replace(d0, pin_side=side)


def test_moved_via_stays_plantable() -> None:
    """(a) start a planted hop illegally off its site: the round either
    re-sites it somewhere ``_ViaPlanter`` accepts or names the net for
    re-route — it never returns unlawful copper."""
    d = _hop_design()
    mesh, wires, rules_t = _wires_for(d)
    pose = np.asarray(d.pose0, dtype=np.float64)
    flat = [(bi, v) for bi, b in enumerate(wires.branches) for v in b.vias]
    if not flat:
        pytest.skip("no planted hop on this pose")
    bi, v = flat[0]
    # drag the via 0.25 mm sideways — inside the hop's tri pair but into
    # whatever keepout it lands in
    bad = dataclasses.replace(v, x=v.x + 0.25, y=v.y)
    branches = list(wires.branches)
    b = branches[bi]
    pts = list(b.points)
    pts[v.k] = (bad.x, bad.y)
    vias = tuple(bad if vv is v else vv for vv in b.vias)
    branches[bi] = dataclasses.replace(b, points=tuple(pts), vias=vias)
    wires = dataclasses.replace(wires, branches=tuple(branches))

    res = lr.legalise_route(d, mesh, wires, pose, n_layers=2, rules_t=rules_t)
    ctx = wm._make_ctx(mesh, d, 2, pose)
    planter = wm._ViaPlanter(ctx, d, pose)
    applied = lr.apply_legalise(wires, res.vias, res.offsets, mesh)
    for (bj, vi), (x, y) in res.vias.items():
        bb = applied.branches[bj]
        vv = next(v for v in bb.vias if int(v.i) == vi)
        ok = planter.plantable_points(
            np.asarray([[x, y]]),
            float(vv.r_site_mm),
            0.15,
            int(bb.net),
            None,
            drill_mm=float(vv.drill_mm or 0.0),
        )[0]
        assert bool(ok), f"via ({bj},{vi}) re-sited unlawfully"
    # homotopy survived whatever moved
    for b0, b1 in zip(wires.branches, applied.branches, strict=True):
        assert b1.edges == b0.edges and b1.tris == b0.tris


def test_route_design_still_realizes_example_a() -> None:
    """End-to-end smoke: the K3 board routes through the new stage and
    the applied result keeps every branch's corridor identical."""
    d = example_a()
    mesh, wires, rules_t = _wires_for(d)
    pose = np.asarray(d.pose0, dtype=np.float64)
    sink: list[np.ndarray] = []
    res = lr.legalise_route(
        d, mesh, wires, pose, n_layers=2, rules_t=rules_t, part_sink=sink
    )
    applied = lr.apply_legalise(wires, res.vias, res.offsets, mesh)
    for b0, b1 in zip(wires.branches, applied.branches, strict=True):
        assert b1.edges == b0.edges
        assert b1.tris == b0.tris
        assert b1.layers == b0.layers


def _covered_edge(d: TopoDesign, mesh, r_ref: float) -> int:
    """A mesh edge whose whole length sits inside one pad's grown
    cover on layer 0 — no free interval exists for a cut there."""
    ctx = wm._make_ctx(mesh, d, 2, np.asarray(d.pose0, dtype=np.float64))
    wm._pad_eval(ctx, r_ref)
    for e in range(len(mesh.edges)):
        if min(mesh.edge_tris[e]) < 0:
            continue
        for a, b, q in ctx.cover_cache.get(r_ref, {}).get(e, ()):
            if int(ctx.pad_copper[q]) & 1 and a <= 0.02 and b >= 0.98:
                return e
    raise AssertionError("no fully pad-covered edge on this mesh")


def test_reroute_names_the_contested_nets() -> None:
    """(d) a contested (edge, layer) cut with no free interval names
    both nets — the row is reported, never silently crammed."""
    d = example_a()
    mesh, _wires, rules_t = _wires_for(d)
    pose = np.asarray(d.pose0, dtype=np.float64)
    r_ref = 0.15 / 2.0 + 0.15 + FAB_QUANTUM_MM
    e = _covered_edge(d, mesh, r_ref)
    t0, t1 = (int(t) for t in mesh.edge_tris[e])
    ctx = wm._make_ctx(mesh, d, 2, pose)
    eu, ev = ctx.eu[e], ctx.ev[e]

    def branch(net: int, pin_a: int, pin_b: int, t: float):
        x, y = edge_xy(mesh, e, t)
        return wm.WirePath(
            net=net,
            pin_a=pin_a,
            pin_b=pin_b,
            tris=(t0, t1),
            edges=(e,),
            t=(t,),
            points=(
                (float(eu[0]), float(eu[1])),
                (x, y),
                (float(ev[0]), float(ev[1])),
            ),
            layers=(0, 0, 0),
        )

    pa, pb = (int(p) for p in d.net_pins[0])
    pc, pd_ = (int(p) for p in d.net_pins[1])
    fake = wm.Wires(
        branches=(branch(0, pa, pb, 0.5), branch(1, pc, pd_, 0.51)),
        order=(),
        capacity_mm=np.zeros(0),
        net_layers=(),
        unreachable=(),
        gap_free_mm=np.zeros((0, 0)),
        gap_cap=np.zeros((0, 0), dtype=np.int64),
        order_residual=0,
        order_pos=(),
        overflow=(),
        via_collisions=(),
        negotiated=(),
        shove_grows=0,
        shove_rounds=(),
        branch_lineage=(),
        branch_ids=(),
    )
    res = lr.legalise_route(d, mesh, fake, pose, n_layers=2, rules_t=rules_t)
    assert res.reroute == {0, 1}
