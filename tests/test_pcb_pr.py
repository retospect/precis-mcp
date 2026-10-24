"""Slice-1 place-and-route prototype — unit tests
(``docs/backlog/pcb-place-route-datamodel.md``, §Slices item 1).

Correctness asserts only: state build, the closed-form distances against
the exact shapely oracle, and a finite-difference check of the gradient.
The torch-bound tests skip on the torch-free host venv; the dev
container (all extras baked) runs them.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis.pcb import realize
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import from_graph
from precis.pcb.pr import state as prstate
from precis.pcb.pr import trees as prtrees
from precis.pcb.rules import ipc2221_track_width_mm, via_capacity_a
from tests.fixtures.pcb import qfp_fanout as qf


def _built(spec: qf.FanoutSpec | None = None) -> qf.Built:
    return qf.build(
        spec
        or qf.FanoutSpec(
            layers=("F.Cu", "B.Cu"),
            pins_per_side=10,
            n_nets=40,
            sources="grid",
            seed=1,
        )
    )


def _problem(built: qf.Built) -> prstate.PrProblem:
    return prstate.build_problem(
        built.ir,
        outline=qf.outline(built.spec),
        config=realize.RealizeConfig(
            fab_caps=capability_for("2layer"),
            class_rules=built.graph["net_classes"],
        ),
        footprints=built.footprints,
    )


def test_problem_builds_valid_ir_wires_and_obstacles():
    built = _built()
    prob = _problem(built)
    assert prob.layers == ("F.Cu", "B.Cu")
    assert len(prob.wires) == built.spec.net_count
    seen = {w.seg_id for w in prob.wires}
    assert seen == set(range(built.ir.n_segments))
    for w in prob.wires:
        assert w.a_lmask & 0b11 and w.b_lmask & 0b11
        assert prob.net_name[w.net_id].startswith("N")
    # SMD pads of the bottom-mounted driver pin to B.Cu; drilled source
    # pads span both layers.
    lmasks = {w.a_lmask for w in prob.wires} | {w.b_lmask for w in prob.wires}
    assert lmasks <= {0b11, 0b10, 0b01}
    n_items = len(prob.obs.seg_xy) + len(prob.obs.box_c)
    assert n_items > len(prob.wires)


def test_required_mm_same_net_merges():
    prob = _problem(_built())
    a, b = prob.wires[0].net_id, prob.wires[1].net_id
    assert prob.required_mm(a, a, 0) == 0.0
    assert prob.required_mm(a, b, 0) > 0.0


def test_parts_table_mirrors_ir_poses_and_locks():
    built = _built()
    prob = _problem(built)
    ir = built.ir
    parts = prob.parts
    assert parts.n_live == ir.n_instances == parts.capacity
    assert list(parts.live_ids()) == list(range(ir.n_instances))
    # rot carried as (cos, sin) of the authored degrees; side/locks copied.
    theta = np.radians(np.asarray(ir.inst_rot, dtype=np.float64))
    assert np.allclose(parts.rot[: ir.n_instances, 0], np.cos(theta))
    assert np.allclose(parts.rot[: ir.n_instances, 1], np.sin(theta))
    assert np.array_equal(parts.side[: ir.n_instances], np.asarray(ir.inst_bottom))
    assert np.array_equal(parts.lock_xy[: ir.n_instances], np.asarray(ir.inst_fixed_xy))
    assert np.array_equal(
        parts.lock_rot[: ir.n_instances], np.asarray(ir.inst_fixed_rot)
    )
    assert np.all(parts.cluster[: ir.n_instances] == -1)
    assert [str(r) for r in parts.refdes[: ir.n_instances]] == [
        str(r) for r in ir.instance_refdes
    ]


def test_parts_capacity_mask_and_free_list():
    from precis.pcb.pr import state as prstate

    parts = prstate.build_parts(_built().ir, capacity=None)
    n = parts.n_live
    # No slack: capacity == live, free list empty, alloc refuses.
    assert parts.capacity == n and not parts.free
    with pytest.raises(IndexError):
        parts.alloc(pos=(0.0, 0.0))
    # Free a row -> it leaves the live mask and becomes allocatable again.
    parts.free_row(0)
    assert parts.n_live == n - 1 and not parts.live[0]
    assert np.all(np.isnan(parts.pos[0]))
    i = parts.alloc(pos=(1.5, -2.0), rot_deg=90.0, side=True, refdes="X9")
    assert i == 0 and parts.n_live == n and parts.live[0]
    assert np.allclose(parts.pos[0], (1.5, -2.0))
    assert np.allclose(parts.rot[0], (0.0, 1.0), atol=1e-9)  # cos90, sin90
    assert bool(parts.side[0]) and parts.refdes[0] == "X9"


def test_parts_capacity_slack_preallocates_dead_rows():
    from precis.pcb.pr import state as prstate

    ir = _built().ir
    parts = prstate.build_parts(ir, capacity=ir.n_instances + 3)
    assert parts.capacity == ir.n_instances + 3
    assert parts.n_live == ir.n_instances
    assert len(parts.free) == 3
    # The three dead rows allocate lowest-id first.
    assert parts.alloc(pos=(0.0, 0.0)) == ir.n_instances


def test_net_attrs_role_inference_and_appetite():
    from precis.pcb.pr import state as prstate

    # Ground and power want copper (negative α), plain signals ~0.
    assert prstate.infer_role("GND") == prstate.ROLE_GROUND
    assert prstate.infer_role("AGND") == prstate.ROLE_GROUND
    assert prstate.infer_role("VCC_3V3") == prstate.ROLE_POWER
    assert prstate.infer_role("+5V") == prstate.ROLE_POWER
    assert prstate.infer_role("3V3") == prstate.ROLE_POWER
    assert prstate.infer_role("SPI_CLK") == prstate.ROLE_SIGNAL
    assert prstate.infer_role("N14", hint="sensitive analog input") == (
        prstate.ROLE_SENSITIVE
    )
    assert prstate.default_appetite(prstate.ROLE_GROUND) < 0.0
    assert prstate.default_appetite(prstate.ROLE_SIGNAL) == 0.0
    assert prstate.default_appetite(prstate.ROLE_SENSITIVE) > 0.0
    # A fast signal flips to positive appetite; ground stays area-hungry.
    assert prstate.default_appetite(prstate.ROLE_SIGNAL, f_max=100e6) > 0.0
    assert prstate.default_appetite(prstate.ROLE_SIGNAL, edge_rate=2.0) > 0.0
    assert prstate.default_appetite(prstate.ROLE_GROUND, edge_rate=2.0) < 0.0


def test_net_attrs_built_and_overridable():
    from precis.pcb.pr import state as prstate

    built = _built()
    prob = _problem(built)
    attrs = prob.net_attrs
    assert attrs.n_nets == built.ir.n_nets
    for arr in (attrs.v_min, attrs.v_max, attrs.f_max, attrs.appetite):
        assert arr.shape == (built.ir.n_nets,)
    # Override hook: the netlist step's datasheet figures land per net.
    name0 = prob.net_name[0]
    over = prstate.build_net_attrs(
        built.ir,
        prob.net_name,
        overrides={name0: {"role": "ground", "v_max": 3.3, "appetite": -2.5}},
    )
    assert over.role[0] == prstate.ROLE_GROUND
    assert over.v_max[0] == 3.3 and over.appetite[0] == -2.5


def test_distances_match_shapely():
    torch = pytest.importorskip("torch")
    from shapely.geometry import LineString, Point, Polygon
    from shapely.geometry import box as shapely_box

    from precis.pcb.pr import distance as D

    rng = np.random.default_rng(7)
    n = 200
    a = torch.tensor(rng.uniform(-10, 10, (n, 2)))
    b = torch.tensor(rng.uniform(-10, 10, (n, 2)))
    c = torch.tensor(rng.uniform(-10, 10, (n, 2)))
    d = torch.tensor(rng.uniform(-10, 10, (n, 2)))
    ours = D.seg_seg_dist(a, b, c, d)
    exact = np.array(
        [
            LineString([tuple(p), tuple(q)]).distance(LineString([tuple(r), tuple(s)]))
            for p, q, r, s in zip(
                a.tolist(), b.tolist(), c.tolist(), d.tolist(), strict=True
            )
        ]
    )
    assert np.abs(ours.numpy() - exact).max() < 1e-6

    # Boxes: a mix of outside / grazing / pierced / contained segments.
    segs = torch.tensor(
        [[-5.0, 0.0, 5.0, 0.0], [0.0, 3.0, 1.0, 3.0], [10.0, 0.0, 11.0, 0.0]]
    )
    ctr = torch.tensor([[0.0, 0.0]])
    hw = torch.tensor([[2.0, 1.0]])
    cs = torch.tensor([[1.0, 0.0]])
    got = D.seg_box_dist(segs[:, :2], segs[:, 2:], ctr, hw, cs)
    want = [
        0.0,
        LineString([(0, 3), (1, 3)]).distance(shapely_box(-2, -1, 2, 1)),
        8.0,
    ]
    assert torch.allclose(got, torch.tensor(want), atol=1e-6)

    pts = torch.tensor([[0.0, 0.0], [3.0, 0.0], [0.0, 1.5]])
    got = D.point_box_dist(pts, ctr, hw, cs)
    want = torch.tensor(
        [
            Point(0, 0).distance(shapely_box(-2, -1, 2, 1)),
            1.0,
            0.5,
        ]
    )
    assert torch.allclose(got, want, atol=1e-6)

    poly = torch.tensor([[-2.0, -2.0], [2.0, -2.0], [2.0, 2.0], [-2.0, 2.0]])
    m = D.outline_margin(
        torch.tensor([[0.0, 0.0], [1.5, 0.0], [3.0, 0.0], [-3.0, 0.0]]), poly
    )
    # Interior margin measures to the BOUNDARY, not the filled polygon.
    ring = Polygon(poly.tolist()).boundary
    exact_m = [
        ring.distance(Point(*p)) * s
        for p, s in [((0, 0), 1), ((1.5, 0), 1), ((3, 0), -1), ((-3, 0), -1)]
    ]
    assert torch.allclose(m, torch.tensor(exact_m), atol=1e-6)


def test_gradient_matches_finite_difference():
    """The energy-relevant derivative: d/dx of a segment's gap to a fixed
    obstacle — finite differences over a representative (non-kinked)
    configuration."""
    torch = pytest.importorskip("torch")
    from precis.pcb.pr import distance as D

    torch.manual_seed(3)
    a0 = torch.tensor([[0.0, 0.0]], dtype=torch.float64)
    a1 = torch.tensor([[1.0, 0.4]], dtype=torch.float64, requires_grad=True)
    b0 = torch.tensor([[0.3, 1.0]], dtype=torch.float64)
    b1 = torch.tensor([[1.2, 1.3]], dtype=torch.float64)

    def f(p):
        return D.seg_seg_dist(a0, p, b0, b1).sum()

    g = torch.autograd.grad(f(a1), a1)[0]
    eps = 1e-6
    fd = torch.stack(
        [
            (f(a1 + eps * e) - f(a1 - eps * e)) / (2 * eps)
            for e in torch.eye(2, dtype=torch.float64)
        ]
    )
    assert torch.allclose(g, fd, atol=1e-4), f"grad {g} vs fd {fd}"


# ---- Nets as trees (spec §State "Nets as trees") ----------------------


def _current_graph() -> dict:
    """A trunk-and-two-branch net (gr497904): U1 feeds 2 A, L1/L2 draw 1 A
    each. Members are ordered so the star's hub (HUB, member[0]) is NOT the
    source, so the hub-to-source edge is the trunk carrying both loads."""
    return {
        "instances": [{"refdes": r} for r in ("HUB", "L1", "L2", "U1")],
        "nets": [
            {
                "name": "VOUT",
                "net_class": "power",
                "domain": "electrical",
                "est_current_a": 2.0,
                "members": [
                    {"refdes": "HUB", "pin": "1"},
                    {"refdes": "L1", "pin": "1", "meta": {"est_current_a": 1.0}},
                    {"refdes": "L2", "pin": "1", "meta": {"est_current_a": 1.0}},
                    {
                        "refdes": "U1",
                        "pin": "2",
                        "tags": ["power_out"],
                        "meta": {"est_current_a": 2.0},
                    },
                ],
            }
        ],
    }


def test_solve_tree_currents_star():
    # Source +3 at node 0 feeding two loads; each edge carries its leaf draw.
    edges = np.array([[0, 1], [0, 2]])
    inject = np.array([3.0, -1.0, -2.0])
    f = prtrees.solve_tree_currents(edges, inject, 3)
    assert np.allclose(np.abs(f), [1.0, 2.0])


def test_solve_tree_currents_trunk_sums_downstream():
    # 0 source, 1 Steiner, 2/3 loads: the trunk 0->1 carries both.
    edges = np.array([[0, 1], [1, 2], [1, 3]])
    inject = np.array([3.0, 0.0, -1.0, -2.0])
    f = prtrees.solve_tree_currents(edges, inject, 4)
    by = {tuple(e): abs(c) for e, c in zip(edges.tolist(), f, strict=True)}
    assert by[(0, 1)] == pytest.approx(3.0)
    assert by[(1, 2)] == pytest.approx(1.0)
    assert by[(1, 3)] == pytest.approx(2.0)


def test_solve_tree_currents_rejects_unbalanced_and_non_tree():
    with pytest.raises(ValueError, match="balance"):
        prtrees.solve_tree_currents(np.array([[0, 1]]), np.array([1.0, 0.0]), 2)
    with pytest.raises(ValueError, match="spanning tree"):
        prtrees.solve_tree_currents(np.array([[0, 1], [1, 2]]), np.array([0.0, 0.0]), 2)


def test_solve_loop_currents_divides_by_conductance():
    # A triangle, 1 A in at node 0, out at node 2. The direct edge 0->2 and
    # the detour 0->1->2 split by conductance; equal g gives 2:1 (the detour
    # is two edges in series, half the conductance of the one-edge path).
    edges = np.array([[0, 1], [1, 2], [0, 2]])
    inject = np.array([1.0, 0.0, -1.0])
    f = prtrees.solve_loop_currents(edges, inject, np.ones(3), 3)
    assert f[2] == pytest.approx(2.0 / 3.0)  # direct path
    assert f[0] == pytest.approx(1.0 / 3.0) and f[1] == pytest.approx(1.0 / 3.0)
    # On a tree it agrees with the KCL solve.
    tedges = np.array([[0, 1], [0, 2]])
    tinj = np.array([3.0, -1.0, -2.0])
    assert np.allclose(
        prtrees.solve_loop_currents(tedges, tinj, np.ones(2), 3),
        prtrees.solve_tree_currents(tedges, tinj, 3),
    )


def test_edge_min_width_floors_and_sizes_by_current():
    floor = 0.2
    w = prtrees.edge_min_width_mm(np.array([0.0, 5.0]), floor, layer_is_outer=True)
    assert w[0] == pytest.approx(floor)  # zero current takes the floor
    assert w[1] == pytest.approx(ipc2221_track_width_mm(5.0, layer_is_outer=True))
    assert w[1] > floor
    # A tiny current whose IPC width is below the floor still gets the floor.
    assert prtrees.edge_min_width_mm(np.array([1e-3]), floor)[0] == pytest.approx(floor)


def test_parallel_via_count_ceils_and_guards():
    cap = via_capacity_a(0.3)
    counts = prtrees.parallel_via_count(np.array([0.0, cap, cap * 2.5]), 0.3)
    assert counts.tolist() == [1, 1, 3]
    # No modelled via geometry: one via, never a divide-by-zero.
    assert prtrees.parallel_via_count(np.array([5.0]), 0.0).tolist() == [1]


def test_build_net_trees_matches_ir_seg_currents():
    ir = from_graph(_current_graph())
    net_id = next(i for i in range(ir.n_nets) if str(ir.net_name[i]) == "VOUT")
    tree = prtrees.build_net_trees(ir)[net_id]
    assert tree.is_tree and tree.n_edges == 3 and tree.n_nodes == 4
    # edge_current_a is read off the IR's resolved per-segment current.
    for e in range(tree.n_edges):
        assert tree.edge_current_a[e] == pytest.approx(
            abs(float(ir.seg_current_a[int(tree.edge_seg[e])]))
        )
    # The hub-balanced injections, run back through the KCL solve, reproduce
    # those same magnitudes — the new mechanism agrees with the IR.
    f = prtrees.solve_tree_currents(tree.edges, tree.node_inject_a, tree.n_nodes)
    assert np.allclose(sorted(np.abs(f)), sorted(tree.edge_current_a))
    # Trunk (to the power_out source U1) carries the sum of both loads.
    src = next(
        i
        for i in range(tree.n_nodes)
        if ir.pin_etype[int(tree.node_pin[i])] == "power_out"
    )
    trunk = next(e for e in range(tree.n_edges) if src in tree.edges[e].tolist())
    assert tree.edge_current_a[trunk] == pytest.approx(2.0)


def test_build_net_trees_on_fanout_problem():
    prob = _problem(_built())
    assert prob.net_trees  # the fanout's multi-pin nets each get a tree
    for net_id, tree in prob.net_trees.items():
        assert tree.net_id == net_id
        assert tree.is_tree and tree.n_edges >= 1
        floor = prob.net_rules[prob.net_name[net_id]].track_width_mm
        # Width is never below the class floor; a zero-current edge sits on it.
        assert np.all(tree.edge_width_mm >= floor - 1e-9)
        zero = tree.edge_current_a == 0.0
        assert np.allclose(tree.edge_width_mm[zero], floor)
