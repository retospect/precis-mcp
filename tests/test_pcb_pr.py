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


# ---- slice 2: free parts, blocks, finish (spec §Slices item 2) ----------


def _free_sources(built: qf.Built) -> qf.Built:
    """Unlock the fanout's source parts so the free-part solve has
    something to move (the QFP stays ``fixed='both'``)."""
    ir = built.ir
    for i in range(ir.n_instances):
        if str(ir.instance_refdes[i]).startswith("S"):
            ir.inst_fixed_xy[i] = False
            ir.inst_fixed_rot[i] = False
    return built


def _small_spec() -> qf.FanoutSpec:
    return qf.FanoutSpec(
        layers=("F.Cu", "B.Cu"), pins_per_side=6, n_nets=12, sources="grid", seed=1
    )


def _small_free() -> tuple[qf.Built, prstate.PrProblem]:
    built = _free_sources(_built(_small_spec()))
    prob = prstate.build_problem(
        built.ir,
        outline=qf.outline(built.spec),
        config=realize.RealizeConfig(
            fab_caps=capability_for("2layer"),
            class_rules=built.graph["net_classes"],
        ),
        footprints=built.footprints,
        free_parts=True,
    )
    return built, prob


def test_dyn_geom_rides_the_pose_and_anchors_wire_endpoints():
    built, prob = _small_free()
    dyn = prob.dyn
    assert dyn.n_parts == built.spec.net_count  # every source, not the QFP
    assert len(dyn.dseg_part) == dyn.n_parts  # one drilled pad each
    assert np.all(dyn.side == 2)  # through-hole: both sides
    # Static obstacles are the QFP's pads only; dynamic pads left them.
    assert len(prob.obs.box_c) == built.spec.n_pads
    # The QFP's courtyard is the one static courtyard a source keeps off.
    assert len(dyn.scourt_c) == 1
    # Each wire's source end is anchored on its part; the QFP end is static.
    for w in prob.wires:
        assert (w.a_part >= 0) != (w.b_part >= 0)
        part = w.a_part if w.a_part >= 0 else w.b_part
        local = w.a_local if w.a_part >= 0 else w.b_local
        cx, cy, th = prob.pose_of(part)
        wx, wy = prstate.local_to_world(local[0], local[1], cx, cy, th)
        want = w.a if w.a_part >= 0 else w.b
        assert abs(wx - want[0]) < 1e-9 and abs(wy - want[1]) < 1e-9
    # Round trip of the transform.
    for pt in [(1.0, 2.0), (-3.5, 0.25)]:
        lx, ly = prstate.world_to_local(pt[0], pt[1], 0.3, -0.7, 1.1)
        assert np.allclose(prstate.local_to_world(lx, ly, 0.3, -0.7, 1.1), pt)


def test_box_box_gap_is_conservative_and_signed():
    torch = pytest.importorskip("torch")
    from shapely import affinity
    from shapely.geometry import box as sbox

    from precis.pcb.pr.relax import _box_box_gap

    rng = np.random.default_rng(11)
    seen_overlap = seen_apart = False
    for _ in range(80):
        ca, cb = rng.uniform(-3, 3, 2), rng.uniform(-3, 3, 2)
        ha, hb = rng.uniform(0.3, 1.5, 2), rng.uniform(0.3, 1.5, 2)
        ta, tb = rng.uniform(0, np.pi, 2)
        g = _box_box_gap(
            torch.tensor(ca)[None],
            torch.tensor(ha)[None],
            torch.tensor([np.cos(ta), np.sin(ta)])[None],
            torch.tensor(cb)[None],
            torch.tensor(hb)[None],
            torch.tensor([np.cos(tb), np.sin(tb)])[None],
        )
        pa = affinity.rotate(
            sbox(-ha[0], -ha[1], ha[0], ha[1]), np.degrees(ta), origin=(0, 0)
        )
        pb = affinity.rotate(
            sbox(-hb[0], -hb[1], hb[0], hb[1]), np.degrees(tb), origin=(0, 0)
        )
        pa = affinity.translate(pa, ca[0], ca[1])
        pb = affinity.translate(pb, cb[0], cb[1])
        exact = pa.distance(pb)
        got = float(g[0])
        if exact > 1e-9:
            seen_apart = True
            # separating-axis: never above the true distance, and positive
            assert 0.0 < got <= exact + 1e-9
        else:
            seen_overlap = True
            assert got <= 1e-9  # overlapping: non-positive, a depth
    assert seen_overlap and seen_apart


def test_free_part_energy_gradient_matches_finite_difference():
    torch = pytest.importorskip("torch")
    from precis.pcb.pr import beliefs
    from precis.pcb.pr import seed as prseed
    from precis.pcb.pr.relax import Relaxer, RelaxParams

    built, prob = _small_free()
    seeded = prseed.seed(prob)
    lay = beliefs.build_layout(prob, [s.polyline for s in seeded.seeds])
    rlx = Relaxer(prob, lay, RelaxParams())
    gen = np.random.default_rng(0)
    assign = torch.tensor(
        beliefs.sample_layers(beliefs.init_q(lay, prob.n_layers), 1, gen),
        dtype=torch.int64,
    )
    pos = torch.tensor(lay.points[None].copy(), dtype=torch.float64)
    # Off the kinks: every box is axis-aligned at the seed, where the
    # separating-axis max ties (a V, central FD reads 0 either side). A
    # small random pose perturbation makes the energy smooth there.
    rng = np.random.default_rng(5)
    gp = rlx.gpose0[None].clone()
    gp[..., :2] += torch.tensor(rng.uniform(-0.3, 0.3, gp[..., :2].shape))
    gp[..., 2] += torch.tensor(rng.uniform(0.05, 0.4, gp[..., 2].shape))
    gp.requires_grad_(True)
    E, _ = rlx.energy(pos, assign, 0.7, gp)
    assert np.isfinite(float(E.detach()[0]))
    g = torch.autograd.grad(E.sum(), gp)[0]
    h = 1e-5
    for i in (0, 3, 7):
        for ax in range(3):
            d = torch.zeros_like(gp)
            d[0, i, ax] = h
            Ep, _ = rlx.energy(pos, assign, 0.7, gp.detach() + d)
            Em, _ = rlx.energy(pos, assign, 0.7, gp.detach() - d)
            fd = float((Ep[0] - Em[0]) / (2 * h))
            assert abs(fd - float(g[0, i, ax])) < 1e-3 * max(1.0, abs(fd)), (
                i,
                ax,
                fd,
                float(g[0, i, ax]),
            )


def test_pair_table_sees_a_crossing_with_far_endpoints():
    """Two long spans that cross mid-way are a belief pair even though no
    endpoint is near the other span (slice 1 tested endpoint proximity
    only, so the sampler put such spans on one layer)."""
    from precis.pcb.pr import beliefs

    built, prob = _small_free()
    w0, w1 = prob.wires[0], prob.wires[1]
    # two 20 mm spans forming an X around the origin, far from each other's ends
    polys = [np.asarray([w.a, w.b]) for w in prob.wires]
    polys[0] = np.array([[-10.0, -10.0], [10.0, 10.0]])
    polys[1] = np.array([[-10.0, 10.0], [10.0, -10.0]])
    lay = beliefs.build_layout(prob, polys, piece_mm=1e9)
    pi, pj = beliefs.pair_table(prob, lay, crowd_mm=0.6)
    p0 = int(np.flatnonzero(lay.piece_wire == 0)[0])
    p1 = int(np.flatnonzero(lay.piece_wire == 1)[0])
    assert (p0, p1) in set(zip(pi.tolist(), pj.tolist(), strict=True))
    assert w0.net_id != w1.net_id


def test_outline_ring_closing_vertex_and_orientation_are_harmless():
    """An authored outline repeats its first vertex last and may run
    clockwise; the margin kernel must read the same interior either way
    (nano's outline did both and every point measured 'at the edge')."""
    torch = pytest.importorskip("torch")
    from precis.pcb.pr import distance as D

    open_ccw = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 6.0], [0.0, 6.0]])
    closed = np.vstack([open_ccw, open_ccw[:1]])
    cw = open_ccw[::-1].copy()
    pts = torch.tensor([[1.0, 1.0], [5.0, 3.0], [11.0, 3.0]], dtype=torch.float64)
    want = torch.tensor([1.0, 3.0, -1.0], dtype=torch.float64)
    assert torch.allclose(D.outline_margin(pts, torch.tensor(open_ccw)), want)
    assert torch.allclose(D.outline_margin(pts, torch.tensor(closed)), want)
    for ring in (closed, cw):
        fixed = prstate._open_ring(ring)
        assert len(fixed) == 4
        assert torch.allclose(D.outline_margin(pts, torch.tensor(fixed)), want)


def test_unrouted_wire_is_a_stub_not_a_phantom():
    """A wire the seed could not route must not relax as a straight line
    between its pads: its stub joins no pair and anchors to no pad."""
    torch = pytest.importorskip("torch")
    from precis.pcb.pr import beliefs
    from precis.pcb.pr.relax import Relaxer, RelaxParams

    built, prob = _small_free()
    polys = [np.asarray([w.a, w.b]) for w in prob.wires]
    polys[0] = np.zeros((0, 2))  # wire 0 unrouted
    lay = beliefs.build_layout(prob, polys, piece_mm=1e9)
    assert not lay.wire_active[0] and lay.wire_active[1:].all()
    rlx = Relaxer(prob, lay, RelaxParams())
    pw = lay.piece_wire
    assert not ((pw[rlx.pp_i.numpy()] == 0) | (pw[rlx.pp_j.numpy()] == 0)).any()
    assert not (pw[rlx.pd_i.numpy()] == 0).any()
    pos = torch.tensor(lay.points[None])
    full = rlx.full_points(pos, rlx.gpose0[None])[0].numpy()
    stub = full[lay.wire_of_point == 0]
    assert np.allclose(stub[0], stub[-1])  # both ends stay at pad a


def test_appetite_widens_a_negative_alpha_net_only():
    """Copper appetite (spec §Net attributes): a net with negative α
    widens above its floor during relaxation; a zero-α signal stays at
    its floor; with the term off every width is frozen."""
    pytest.importorskip("torch")
    from precis.pcb.pr import beliefs
    from precis.pcb.pr import seed as prseed
    from precis.pcb.pr.relax import Relaxer, RelaxParams

    built = _built(_small_spec())
    hungry = str(built.ir.net_name[0])
    prob = prstate.build_problem(
        built.ir,
        outline=qf.outline(built.spec),
        config=realize.RealizeConfig(
            fab_caps=capability_for("2layer"),
            class_rules=built.graph["net_classes"],
        ),
        footprints=built.footprints,
        net_overrides={hungry: {"appetite": -1.0}},
    )
    assert prob.net_attrs.appetite[0] == -1.0 and prob.net_attrs.appetite[1] == 0.0
    import torch

    seeded = prseed.seed(prob)
    lay = beliefs.build_layout(prob, [s.polyline for s in seeded.seeds])
    gen = np.random.default_rng(0)
    assign_np = beliefs.sample_layers(beliefs.init_q(lay, prob.n_layers), 1, gen)
    fixed = lay.span_allowed.sum(1) == 1
    assign_np[:, fixed] = np.argmax(lay.span_allowed[fixed], axis=1)[None, :]
    assign = torch.tensor(assign_np)
    on = Relaxer(prob, lay, RelaxParams(appetite_scale=1.0))
    pos = torch.tensor(lay.points[None].copy())
    w0 = [i for i, w in enumerate(prob.wires) if w.net_id == 0]
    w1 = [i for i, w in enumerate(prob.wires) if w.net_id == 1]
    n_w = len(prob.wires)
    # At zero width the barrier is blind to width, so the area term is
    # the only thing an extra width changes: E drops by α·Δw·L for the
    # hungry wire and not at all for the signal.
    zero = torch.zeros((1, n_w), dtype=torch.float64)
    e0, _ = on.energy(pos, assign, 0.0, None, zero)
    for wi, want_drop in ((w0[0], True), (w1[0], False)):
        extra = zero.clone()
        extra[0, wi] = 0.05
        e1, parts = on.energy(pos, assign, 0.0, None, extra)
        pcs = np.flatnonzero(lay.piece_wire == wi)
        length = float(
            np.linalg.norm(
                lay.points[lay.piece_p1[pcs]] - lay.points[lay.piece_p0[pcs]], axis=1
            ).sum()
        )
        drop = float(e0[0] - e1[0])
        if want_drop:
            assert drop == pytest.approx(0.05 * length, rel=1e-6)
        else:
            assert drop == pytest.approx(0.0, abs=1e-9)
    # The relaxer's own step widens the hungry wire and never a signal.
    _, _, parts, _ = on.run(
        assign_np, params=RelaxParams(steps=3, grow_steps=3, appetite_scale=1.0)
    )
    ex = parts["extra"].numpy()
    assert np.all(ex[:, w1] == 0.0)
    assert ex.max() <= on.params.max_widen_mm + 1e-12
    off = Relaxer(prob, lay, RelaxParams(steps=2, grow_steps=2))
    _, _, parts_off, _ = off.run(assign_np)
    assert parts_off["extra"] is None


def test_blocks_prolong_rigidly():
    pytest.importorskip("torch")
    from precis.pcb.pr import beliefs
    from precis.pcb.pr import seed as prseed
    from precis.pcb.pr.relax import Relaxer, RelaxParams

    built, prob = _small_free()
    # Two authored blocks over the sources: rows 0-5 and 6-11.
    grp = prob.dyn.group
    grp[:] = np.arange(len(grp)) // 6
    seeded = prseed.seed(prob)
    lay = beliefs.build_layout(prob, [s.polyline for s in seeded.seeds])
    rlx = Relaxer(prob, lay, RelaxParams(), blocks=True)
    assert rlx.n_groups == 2
    gp = rlx.gpose0[None].clone()
    base = rlx.part_poses(gp)[0].numpy()
    assert np.allclose(base, prob.dyn.pose0, atol=1e-9)
    # Rotate block 0 by 30° and shift it: member distances are preserved,
    # block 1 is untouched.
    gp2 = gp.clone()
    gp2[0, 0, 2] += np.radians(30.0)
    gp2[0, 0, 0] += 1.5
    moved = rlx.part_poses(gp2)[0].numpy()
    m0 = np.flatnonzero(grp == 0)
    d_before = np.linalg.norm(base[m0, None, :2] - base[None, m0, :2], axis=-1)
    d_after = np.linalg.norm(moved[m0, None, :2] - moved[None, m0, :2], axis=-1)
    assert np.allclose(d_before, d_after, atol=1e-9)
    assert np.allclose(moved[m0, 2] - base[m0, 2], np.radians(30.0))
    m1 = np.flatnonzero(grp == 1)
    assert np.allclose(moved[m1], base[m1])
    # The fine level ignores the blocks: one group per part.
    assert Relaxer(prob, lay, RelaxParams(), blocks=False).n_groups == len(grp)


def test_carry_q_keeps_settled_layers_across_refinement():
    from precis.pcb.pr import beliefs

    built, prob = _small_free()
    polys = [np.asarray([w.a, w.b]) for w in prob.wires]
    coarse = beliefs.build_layout(prob, polys, piece_mm=4.0)
    q = beliefs.init_q(coarse, prob.n_layers)
    # settle every coarse span of wire 0 on layer 1 where allowed
    w0 = coarse.piece_wire == 0
    q[w0] = np.where(coarse.span_allowed[w0], np.eye(2)[1], 0.0)
    q[w0] /= q[w0].sum(1, keepdims=True)
    fine = beliefs.build_layout(prob, polys, piece_mm=1.0)
    q2 = beliefs.carry_q(coarse, q, fine)
    f0 = fine.piece_wire == 0
    interior = f0 & fine.span_allowed.all(1)
    assert interior.any()
    assert np.allclose(q2[interior][:, 1], 1.0)
    assert np.allclose(q2.sum(1), 1.0)


def test_uncross_by_layer_reduces_same_layer_crossings():
    from precis.pcb.pr import beliefs, uncross

    built, prob = _small_free()
    polys = [np.asarray([w.a, w.b]) for w in prob.wires]
    lay = beliefs.build_layout(prob, polys, piece_mm=1e9)
    pos = lay.points
    pair_i, pair_j = np.triu_indices(lay.n_spans, k=1)
    assign = np.zeros(lay.n_spans, dtype=np.int64)
    assign[~lay.span_allowed[:, 0]] = 1
    geo = uncross.crossing_pairs(lay, pos, pair_i, pair_j)
    if not geo.any():
        pytest.skip("fixture has no projected crossings")
    out, before, after = uncross.uncross_by_layer(
        lay, pos, assign, pair_i, pair_j, n_layers=2
    )
    assert before > 0 and after <= before
    assert np.all(lay.span_allowed[np.arange(lay.n_spans), out])


def test_cleanup_and_snapshot_round_trip():
    from precis.pcb.pr import finish

    built, prob = _small_free()
    poly = np.array([[0.0, 0.0], [1.0, 0.001], [2.0, 0.0], [3.0, 1.0], [4.0, 1.0]])
    lay = np.array([0, 0, 1, 1])
    cp, cl = finish.cleanup_wires({0: poly}, {0: lay}, tol_mm=0.01)
    # the near-collinear point goes; the layer-change vertex stays
    assert len(cp[0]) == 4 and cl[0].tolist() == [0, 1, 1]
    state = finish.snapshot(built.ir, prob, {0: poly}, {0: lay})
    assert state["wires"][0]["seg_id"] == prob.wires[0].seg_id
    ir2 = _free_sources(_built(built.spec)).ir
    polys, lays = finish.restore(ir2, state)
    assert np.allclose(polys[prob.wires[0].seg_id], poly)
    assert lays[prob.wires[0].seg_id].tolist() == lay.tolist()
    for i in range(ir2.n_instances):
        assert float(ir2.inst_x[i]) == pytest.approx(float(built.ir.inst_x[i]))


@pytest.mark.slow
def test_v1_free_parts_small_runs_moves_parts_and_resumes():
    pytest.importorskip("torch")
    from precis.pcb.pr import v1
    from precis.pcb.pr.v1 import Stage, V1Params

    built, prob = _small_free()
    cfg = realize.RealizeConfig(
        fab_caps=capability_for("2layer"), class_rules=built.graph["net_classes"]
    )
    prm = V1Params(K=4, stages=(Stage(4.0, 1, 30, 15), Stage(2.0, 1, 30, 15)))
    run = v1.run(
        built.ir,
        outline=qf.outline(built.spec),
        config=cfg,
        footprints=built.footprints,
        params=prm,
    )
    assert run.rounds_run == 2 and run.levels == ["part@4.0mm", "part@2.0mm"]
    assert all(np.isfinite(p).all() for p in run.polylines.values())
    assert np.isfinite(run.problem.dyn.pose0).all()
    assert run.part_travel_mm > 0.0  # the sources moved
    # the input IR is untouched; the run's copy carries the moved poses
    assert np.allclose(built.ir.inst_x, prob.parts.pos[: built.ir.n_instances, 0])
    moved = sum(
        1
        for i in range(run.ir.n_instances)
        if abs(float(run.ir.inst_x[i]) - float(built.ir.inst_x[i])) > 1e-6
    )
    assert moved > 0
    # every written rotation is a grid value (D5/Q7: the finest tier),
    # never the descended continuous θ — the rotation-grid gate's read
    from precis.pcb.field import ROT_SNAP_DEG

    for i in range(run.ir.n_instances):
        rot = float(run.ir.inst_rot[i]) % 360.0
        assert min(rot % ROT_SNAP_DEG, ROT_SNAP_DEG - rot % ROT_SNAP_DEG) < 1e-6
    # determinism within tolerance
    again = v1.run(
        built.ir,
        outline=qf.outline(built.spec),
        config=cfg,
        footprints=built.footprints,
        params=prm,
    )
    assert again.via_count == run.via_count and again.unrouted == run.unrouted
    assert abs(again.wire_mm - run.wire_mm) < 1e-6 * max(1.0, run.wire_mm)
    # resume: a run from the saved state starts where this one ended
    res = v1.run(
        built.ir,
        outline=qf.outline(built.spec),
        config=cfg,
        footprints=built.footprints,
        params=V1Params(K=4, stages=(Stage(2.0, 1, 10, 5),), finish=False),
        resume=run.state,
    )
    assert res.rounds_run == 1
    # the resumed run STARTS exactly at the saved run's final poses
    dyn = run.problem.dyn
    for row, inst in enumerate(dyn.part_inst):
        x, y, th = res.start_pose[row]
        assert x == pytest.approx(float(run.ir.inst_x[inst]), abs=1e-9)
        assert y == pytest.approx(float(run.ir.inst_y[inst]), abs=1e-9)
        assert np.degrees(th) % 360.0 == pytest.approx(
            float(run.ir.inst_rot[inst]) % 360.0, abs=1e-6
        )
