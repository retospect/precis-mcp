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
from precis.pcb.pr import state as prstate
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
