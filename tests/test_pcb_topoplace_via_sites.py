"""G2b/G2g (topo-route-geometry-truth): a join mints the via it was
planted with — verbatim, never re-sited — and a corridor-less synthetic
mesh sites its joins on the wire's own crossing of the cut edge (the
naive mint). Two hops of one branch still space by real clearance; a
refused join names its cut and keeps the split."""

from __future__ import annotations

import dataclasses
import math

import numpy as np

from precis.pcb.rules import NetRules
from precis.pcb.topoplace import realise as realise_mod
from precis.pcb.topoplace import wires as wires_mod
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.proxy import all_pin_world
from precis.pcb.topoplace.realise import plan_route
from precis.pcb.topoplace.wires import PlantedVia, WirePath
from tests.fixtures.pcb.topo_example_a import example_a
from tests.test_pcb_topoplace_realise import _via_fixture


def _two_cut_plan(d, mesh, monkeypatch, wires, rules=None):
    """The via fixture's b0 with cuts on edges 0 and 1 — the sites are
    the wire's own crossings of those edges (the naive mint)."""
    monkeypatch.setattr(
        realise_mod,
        "assign_layers",
        lambda *a, **k: (wires, [[0, 3, 0], [0]], [[0, 1], []], ()),
    )
    return plan_route(d, mesh, wires, 4, net_rules=rules)


def _b0_edits(wires, **kw):
    """``wires`` with branch 0 replaced per ``kw`` (edges/t/etc.)."""
    return dataclasses.replace(
        wires,
        branches=(dataclasses.replace(wires.branches[0], **kw), wires.branches[1]),
    )


def test_two_hops_on_one_branch_mint_at_their_edge_crossings(monkeypatch) -> None:
    """Cuts at edge indices 0 and 1 mint at the wire's own crossing of
    each edge — (0.6, 0.3) on the pad ring edge, (2.0, 1.5) on the
    channel edge. No siting walk: the point IS the crossing."""
    d, mesh, wires = _via_fixture(pin_side=(0, 0, 0, 0), constrained=(False,) * 4)
    plan = _two_cut_plan(d, mesh, monkeypatch, wires)
    assert [(v.x, v.y) for v in plan.vias if v.branch == 0] == [
        (0.6, 0.3),
        (2.0, 1.5),
    ]
    assert 0 not in plan.no_via
    assert plan.no_via_reason == {}


def test_coincident_naive_sites_refuse_the_second_with_a_reason(monkeypatch) -> None:
    """Two cuts whose edge crossings coincide mint one barrel: the
    second is refused on the branch's own spacing and the reason names
    the earlier via."""
    d, mesh, wires = _via_fixture(pin_side=(0, 0, 0, 0), constrained=(False,) * 4)
    wires = _b0_edits(wires, edges=(1, 1), t=(0.5, 0.5))
    plan = _two_cut_plan(d, mesh, monkeypatch, wires)
    assert len([v for v in plan.vias if v.branch == 0]) == 1
    assert 0 in plan.no_via
    assert "earlier via" in plan.no_via_reason[0]


def test_a_wide_barrel_refuses_the_second_close_join(monkeypatch) -> None:
    """The naive sites are 1.72 mm apart — clear for the house barrel,
    refused when the rule's via is wide enough that the two barrels
    plus clearance overlap."""
    d, mesh, wires = _via_fixture(pin_side=(0, 0, 0, 0), constrained=(False,) * 4)
    wide = {
        "N0": NetRules(track_width_mm=0.15, clearance_mm=0.15, via_dia_mm=4.0),
        "N1": NetRules(track_width_mm=0.15, clearance_mm=0.15, via_dia_mm=4.0),
    }
    plan = _two_cut_plan(d, mesh, monkeypatch, wires, rules=wide)
    assert len([v for v in plan.vias if v.branch == 0]) == 1
    assert 0 in plan.no_via


def test_failed_site_keeps_the_split_and_names_the_cut(monkeypatch) -> None:
    """A refused join still splits the pieces at its intended crossing
    — the failed net's honest geometry — and the reason keys on the
    cut's edge index."""
    d, mesh, wires = _via_fixture(pin_side=(0, 0, 0, 0), constrained=(False,) * 4)
    wires = _b0_edits(wires, edges=(1, 1), t=(0.5, 0.5))
    plan = _two_cut_plan(d, mesh, monkeypatch, wires)
    # the coincident marks collapse to one split — the wire still cuts
    # at its intended crossing, the refused run vanishes empty
    assert len([p for p in plan.pieces if p.branch == 0]) == 2
    assert plan.no_via == (0,)
    assert plan.no_via_reason[0].startswith("cut@1:")


def test_a_planted_join_mints_its_via_verbatim(monkeypatch) -> None:
    """A hop the search (or the resolver) planted is the point kept —
    the emit never re-sites it, so a planted site reads out x/y/dia/
    drill exactly as planted."""
    _d, mesh, wires = _via_fixture(pin_side=(0, 0, 0, 0), constrained=(False,) * 4)
    b0 = dataclasses.replace(
        wires.branches[0],
        layers=(0, 0, 3, 3),
        vias=(
            PlantedVia(i=1, k=2, x=2.5, y=1.4, dia_mm=0.6, drill_mm=0.3, r_site_mm=0.3),
        ),
    )
    wires = dataclasses.replace(wires, branches=(b0, wires.branches[1]))
    monkeypatch.setattr(
        realise_mod,
        "assign_layers",
        lambda *a, **k: (wires, [[0, 3, 0], [0]], [[1], []], ()),
    )
    plan = plan_route(_d, mesh, wires, 4)
    (v,) = [v for v in plan.vias if v.branch == 0]
    assert (v.x, v.y, v.dia_mm, v.drill_mm) == (2.5, 1.4, 0.6, 0.3)
    assert 0 not in plan.no_via


def _replant_scaffold(rule: tuple[float, float, float]):
    """example_a with net B's first pin flipped to back-SMD: a branch
    between B's pins owes a mandatory 1->0 hop — and on a one-edge
    corridor that hop has exactly one edge it can ever sit on."""
    d = example_a()
    side = np.asarray(d.pin_side).copy()
    side[int(d.net_pins[1][0])] = 1
    d = dataclasses.replace(d, pin_side=side)
    n_layers = 2
    mesh = build_mesh(d, d.pose0)
    ctx = wires_mod._make_ctx(mesh, d, n_layers, d.pose0)
    rules_used = ((0.15, 0.15),) * len(d.net_pins)
    corridor = wires_mod._corridor(ctx, mesh, d, n_layers, rules_used, d.pose0)
    occupancy = wires_mod._Occupancy(
        corridor,
        mesh,
        n_layers,
        d.pose0,
        net_clearance=tuple(c for _t, c in rules_used),
    )
    k = 1
    masks = wires_mod._net_masks(ctx, d, k, n_layers, *rule)
    pa, pb = (int(p) for p in d.net_pins[k])
    return d, mesh, ctx, occupancy, k, masks, pa, pb, n_layers


def test_a_colliding_hop_resites_along_its_own_edge() -> None:
    """topo-fanclear round 2 (x2 near-zero VBAT_UV): a same-net hop
    minted at a sibling barrel's site is re-sited a step ALONG the edge
    — the collision poisons the collider's disc, not the edge. The
    barred-edge replant the fix replaces marked the branch's only
    plantable edge dead and dropped the mandatory hop's via."""
    rule = (0.001, 0.0, 0.001)
    r_site, clearance = rule[2] / 2.0, rule[1]
    d, mesh, ctx, occupancy, k, masks, pa, pb, n_layers = _replant_scaffold(rule)
    e = next(
        e
        for e in range(len(mesh.edges))
        if not ctx.board[e]
        and int(masks.open[e]) & 0b11 == 0b11
        and min(mesh.edge_tris[e]) >= 0
        and occupancy.planter.point(e, r_site, clearance, k, None) is not None
    )
    x, y, t_v = occupancy.planter.point(e, r_site, clearance, k, None)
    pw = all_pin_world(d, d.pose0)
    t0, t1 = (int(t) for t in mesh.edge_tris[e])
    branch = WirePath(
        net=k,
        pin_a=pa,
        pin_b=pb,
        tris=(t0, t1),
        edges=(e,),
        t=(t_v,),
        points=(tuple(pw[pa]), (x, y), tuple(pw[pb])),
        layers=(1, 0),
        vias=(
            PlantedVia(
                i=0, k=1, x=x, y=y, dia_mm=rule[2], drill_mm=0.0, r_site_mm=r_site
            ),
        ),
    )
    got = wires_mod._replant_branch(
        ctx,
        mesh,
        d,
        n_layers,
        k,
        rule,
        occupancy,
        masks,
        masks.open,
        frozenset(int(v) for v in mesh.pin_vertex),
        wires_mod._cand_mask(wires_mod._net_layers(d, k, n_layers), n_layers),
        branch,
        (e,),
        [],
        [(x, y, r_site)],
        False,  # the base replant path — pass 0 has no hole-to-hole wall
    )
    assert got is not None
    rebuilt, _edges = got
    (v,) = rebuilt.vias
    assert int(rebuilt.edges[v.i]) == e
    assert math.dist((v.x, v.y), (x, y)) >= v.r_site_mm + r_site + clearance


def test_a_colliding_hop_resites_at_real_reach() -> None:
    """The same resite at the real rule's reach (0.45 barrel + 0.15
    clearance): the standing sibling's disc covers 0.6 mm of the edge;
    the hop moves to the first candidate past it."""
    rule = (0.15, 0.15, 0.45)
    r_site, clearance = rule[2] / 2.0, rule[1]
    d, mesh, ctx, occupancy, k, masks, pa, pb, n_layers = _replant_scaffold(rule)
    pw = all_pin_world(d, d.pose0)
    for e in range(len(mesh.edges)):
        if (
            ctx.board[e]
            or int(masks.open[e]) & 0b11 != 0b11
            or min(mesh.edge_tris[e]) < 0
        ):
            continue
        pt = occupancy.planter.point(e, r_site, clearance, k, None)
        if pt is None:
            continue
        dyn = occupancy.dyn_index(extra=[(pt[0], pt[1], r_site)])
        if occupancy.planter.point(e, r_site, clearance, k, dyn) is None:
            continue
        x, y, t_v = pt
        break
    else:
        raise AssertionError("no edge holds a resitable collider")
    t0, t1 = (int(t) for t in mesh.edge_tris[e])
    branch = WirePath(
        net=k,
        pin_a=pa,
        pin_b=pb,
        tris=(t0, t1),
        edges=(e,),
        t=(t_v,),
        points=(tuple(pw[pa]), (x, y), tuple(pw[pb])),
        layers=(1, 0),
        vias=(
            PlantedVia(
                i=0, k=1, x=x, y=y, dia_mm=rule[2], drill_mm=0.0, r_site_mm=r_site
            ),
        ),
    )
    got = wires_mod._replant_branch(
        ctx,
        mesh,
        d,
        n_layers,
        k,
        rule,
        occupancy,
        masks,
        masks.open,
        frozenset(int(v) for v in mesh.pin_vertex),
        wires_mod._cand_mask(wires_mod._net_layers(d, k, n_layers), n_layers),
        branch,
        (e,),
        [],
        [(x, y, r_site)],
        False,  # the base replant path — pass 0 has no hole-to-hole wall
    )
    assert got is not None
    rebuilt, _edges = got
    (v,) = rebuilt.vias
    assert int(rebuilt.edges[v.i]) == e
    assert math.dist((v.x, v.y), (x, y)) >= v.r_site_mm + r_site + clearance
