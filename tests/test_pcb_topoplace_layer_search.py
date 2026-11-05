"""G2c/G2g — the layer state in the route search (topo-route-geometry-truth).

``wires._net_tree`` searches (edge, triangle, layer): crossing an edge
keeps the layer and needs it open, a layer change at an edge needs a
planted via point there and costs ``VIA_COST_MM``. The committed
layering is read back off ``b.layers`` by ``realise._layers_segment``
— a cut is a hop edge carrying a planted via. The boards here are a
pair of pads 20 mm apart with a foreign 0.4 mm pad row between them
(0.1 mm gaps: no wire and no via passes on the top layer) long enough
that going round it costs more than the two vias that cross under it.
"""

from __future__ import annotations

import numpy as np
from shapely.geometry import LineString, Point, box

from precis.pcb.rules import NetRules
from precis.pcb.topoplace import wires as wires_mod
from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.realise import _layers_segment, plan_route
from precis.pcb.topoplace.wires import VIA_COST_MM, build_wires, layer_dp
from tests.test_pcb_topoplace_walls import _board

_TRACE, _CLEAR = 0.15, 0.15
_ROW = box(19.81, 9.81, 20.19, 30.19)  # the row's copper, a hair inside


def _row_board(*, end_side: int = 0, start_side: int = 0):
    """Net 0: two pads on y = 20, 20 mm apart; net 1: a column of 41
    top-side 0.4 mm pads at x = 20, y = 10..30, pitch 0.5 mm."""
    pins = [
        {"x": 10.0, "y": 20.0, "net": 0, "side": start_side},
        {"x": 30.0, "y": 20.0, "net": 0, "side": end_side},
        *({"x": 20.0, "y": 10.0 + 0.5 * i, "net": 1} for i in range(41)),
    ]
    return _board(pins, n_nets=2)


def _route(d, via_dia: float):
    mesh = build_mesh(d, d.pose0)
    rule = (_TRACE, _CLEAR, via_dia)
    w = build_wires(mesh, d, 4, (rule, rule), pose=d.pose0)
    ctx = wires_mod._make_ctx(mesh, d, 4)
    masks = wires_mod._net_masks(ctx, d, 0, 4, _TRACE, _CLEAR, via_dia)
    (b,) = [x for x in w.branches if x.net == 0]
    return mesh, w, b, masks


def _cuts_are_planted(b, cuts) -> bool:
    """Every cut is a hop edge carrying a planted via (G2g: the via the
    search sited IS the via the emit keeps — nothing slides)."""
    planted = {int(v.i) for v in b.vias}
    return all(c in planted for c in cuts)


def test_no_via_fits_so_the_wire_goes_round_the_row_on_one_layer() -> None:
    d = _row_board()
    # a via wider than the board fits nowhere: the layer change is off
    _mesh, w, b, masks = _route(d, via_dia=50.0)

    assert not masks.fit.any()
    assert not LineString(b.points).intersects(_ROW)
    runs, cuts = _layers_segment(d, w, b, 4)
    assert (runs, cuts) == ([0], [])


def test_a_cheaper_pair_of_vias_crosses_under_the_row() -> None:
    d = _row_board()
    _mesh, w, b, masks = _route(d, via_dia=0.6)

    # the detour round the row's end is 9 mm longer: two vias at 3 mm win
    assert 2 * VIA_COST_MM < 9.0
    assert LineString(b.points).intersects(_ROW)
    assert b.length_mm < 21.0
    runs, cuts = _layers_segment(d, w, b, 4)
    # both ends on their pads' face, the middle run on another layer —
    # the planted search picks the layer now (G2g)
    assert runs[0] == 0 and runs[-1] == 0 and len(runs) == 3 and runs[1] != 0
    assert len(cuts) == 2
    # every cut is a planted via the search already sited
    assert _cuts_are_planted(b, cuts)


def test_a_drilled_end_needs_no_via() -> None:
    """Bottom SMD to drilled: the drilled pad is on every layer, so with
    vias fitting nowhere the net still routes layered, uncut."""
    d = _row_board(start_side=1, end_side=2)
    _mesh, w, b, masks = _route(d, via_dia=50.0)

    runs, cuts = _layers_segment(d, w, b, 4)
    assert (runs, cuts) == ([3], [])


def test_the_search_will_not_change_layer_where_no_via_fits() -> None:
    """Top SMD to bottom SMD needs one layer change. With a via fitting
    nowhere there is none to take and no any-layer fallback (G2g): the
    pin roots its own component and ``Unreachable(layered=True)`` names
    it — an any-layer path exists, the layered search has no plantable
    hop. With a fitting via the same pair routes layered."""
    d = _row_board(start_side=0, end_side=1)

    mesh = build_mesh(d, d.pose0)
    w = build_wires(mesh, d, 4, ((_TRACE, _CLEAR, 50.0),) * 2, pose=d.pose0)
    ctx = wires_mod._make_ctx(mesh, d, 4)
    masks = wires_mod._net_masks(ctx, d, 0, 4, _TRACE, _CLEAR, 50.0)
    assert not hasattr(w, "layer_fallback")
    assert not masks.fit.any()
    (u,) = [x for x in w.unreachable if x.net == 0]
    assert u.layered

    _mesh, w, b, masks = _route(d, via_dia=0.6)
    assert not w.unreachable
    runs, cuts = _layers_segment(d, w, b, 4)
    # ends on the pads' faces with planted hop(s) between — the search
    # picks run layers and via count now (G2g)
    assert runs[0] == 0 and runs[-1] == 3 and len(runs) == len(cuts) + 1
    assert _cuts_are_planted(b, cuts)


def test_via_fit_is_false_in_the_row_and_true_in_the_open() -> None:
    d = _row_board()
    mesh = build_mesh(d, d.pose0)
    ctx = wires_mod._make_ctx(mesh, d, 4)
    fit = wires_mod._net_masks(ctx, d, 0, 4, _TRACE, _CLEAR, 0.6).fit
    mid = 0.5 * (mesh.verts[mesh.edges[:, 0]] + mesh.verts[mesh.edges[:, 1]])
    in_row = np.array([_ROW.contains(Point(m)) for m in mid])
    assert in_row.any() and not fit[in_row].any()
    assert fit[~in_row & (mid[:, 0] < 15.0)].any()
    assert not fit[ctx.board].any()


def test_layer_dp_gives_the_fewest_vias_on_a_fixed_corridor() -> None:
    both, top, bot = 0b1001, 0b0001, 0b1000
    # layer 3 is the only one open on e3: top, top, top, bottom, top
    open_e = [both, top, both, bot, both]
    fit = [True] * 5
    runs, cuts, dead = layer_dp(open_e, fit, 0b0001, 0b0001, 4)
    assert (runs, cuts, dead) == ([0, 3, 0], [2, 4], None)
    # a free corridor stays on the start layer: no via
    assert layer_dp([both] * 4, [True] * 4, 0b0001, 0b0001, 4)[:2] == ([0], [])
    # inner layers lose a tie to the outers
    assert layer_dp([0b0111] * 3, [True] * 3, 0b0111, 0b0111, 4)[:2] == ([0], [])
    # an inner-only stretch is allowed when nothing else is open
    inner = 0b0110
    runs, cuts, dead = layer_dp([0b0111, inner, 0b0111], [True] * 3, 0b0001, 0b0001, 4)
    assert dead is None and len(cuts) == 2 and runs[1] in (1, 2)


def test_layer_dp_names_a_corridor_with_no_legal_assignment() -> None:
    both, top, bot = 0b1001, 0b0001, 0b1000
    open_e = [both, top, both, bot, both]
    # the one edge that could carry the top -> bottom change has no via
    fit = [True, True, False, True, True]
    runs, cuts, dead = layer_dp(open_e, fit, 0b0001, 0b0001, 4)
    assert (runs, cuts) == ([], [])
    assert dead == (
        0,
        3,
    )  # last edge a change was possible on, first edge no layer reaches
    # the ends bind too: an end pad on a layer the last edge closes
    runs, cuts, dead = layer_dp([top, top], [False, False], 0b0001, 0b1000, 4)
    assert dead == (0, 1)


def test_layers_segment_reads_the_searchs_planted_layering() -> None:
    """The assign pass's initial segment IS the search's committed
    layering: faces on the pads' top, the middle run on another layer,
    each cut a planted via."""
    d = _row_board()
    _mesh, w, b, _masks = _route(d, via_dia=0.6)
    runs, cuts = _layers_segment(d, w, b, 4)
    assert len(runs) == 3 and runs[0] == 0 and runs[2] == 0 and runs[1] != 0
    assert len(cuts) == 2
    assert _cuts_are_planted(b, cuts)


def test_plan_route_strands_the_pin_no_planted_hop_reaches() -> None:
    d = _row_board(start_side=0, end_side=1)
    mesh = build_mesh(d, d.pose0)
    rule = NetRules(track_width_mm=_TRACE, clearance_mm=_CLEAR, via_dia_mm=50.0)
    w = build_wires(mesh, d, 4, ((_TRACE, _CLEAR, 50.0),) * 2, pose=d.pose0)
    plan = plan_route(d, mesh, w, 4, net_rules={"N0": rule, "N1": rule})
    assert 0 in plan.unreachable
    (u,) = [x for x in plan.stranded if x.net == 0]
    assert u.layered
