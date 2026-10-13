"""The synthetic QFP fanout family (``tests/fixtures/pcb/qfp_fanout.py``):
shape invariants, the warm start's effect, and the routed counts the
production maze reaches on its members. The numbers are measurements
recorded in ``docs/backlog/pcb-global-plan-router.md`` (Benchmarks); the
assertions here are floors and invariants, not pins."""

from __future__ import annotations

import pytest

from precis.pcb.ir import pin_point
from tests.fixtures.pcb import qfp_fanout as qf

SMALL = qf.FanoutSpec(
    pins_per_side=4, pitch_mm=1.0, pad_h_mm=1.5, source_radius_mm=8.0, outline_mm=24.0
)
EWOD_LIKE = qf.FanoutSpec(pins_per_side=20, n_nets=55, sources="grid")
PLAZA = qf.FanoutSpec(pins_per_side=20, n_nets=55, sources="plaza")


def test_family_shape_is_what_the_spec_says():
    built = qf.build(SMALL)
    ir = built.ir
    assert SMALL.n_pads == 16
    assert ir.n_segments == 16  # one airwire per net
    assert [len(g.pins) for g in built.groups] == [16]  # every pad swappable
    assert ir.n_layers == 4
    assert built.config.class_rules is not None
    assert built.config.class_rules[qf.NET_CLASS]["layers"] == ["In2.Cu", "B.Cu"]
    assert not built.warnings


def test_pads_sit_on_the_ring_with_the_long_axis_radial():
    fp = qf.qfp_footprint(EWOD_LIKE)
    assert len(fp["pads"]) == 80
    half = EWOD_LIKE.ring_half_mm
    for pad in fp["pads"]:
        on_x_edge = abs(abs(pad["x"]) - half) < 1e-6
        on_y_edge = abs(abs(pad["y"]) - half) < 1e-6
        assert on_x_edge or on_y_edge
        # Long axis points out of the package.
        assert (pad["h"] > pad["w"]) == on_y_edge


def test_sources_are_deterministic_per_seed_and_inside_the_outline():
    a = qf.source_points(EWOD_LIKE)
    b = qf.source_points(EWOD_LIKE)
    c = qf.source_points(
        qf.FanoutSpec(pins_per_side=20, n_nets=55, sources="grid", seed=2)
    )
    assert a == b
    assert a != c
    assert len(a) == 55
    limit = EWOD_LIKE.outline_mm / 2.0
    assert all(abs(x) < limit and abs(y) < limit for x, y in a)


def test_distance_warm_start_shortens_the_airwires():
    def airwire_total(built: qf.Built) -> float:
        ir = built.ir
        total = 0.0
        for s in range(ir.n_segments):
            a = pin_point(ir, int(ir.seg_pin_a[s]))
            b = pin_point(ir, int(ir.seg_pin_b[s]))
            assert a is not None and b is not None
            total += abs(a[0] - b[0]) + abs(a[1] - b[1])
        return total

    before = qf.build(EWOD_LIKE)
    after = qf.build(EWOD_LIKE)
    assert qf.warm_start(after, "distance") == "distance"
    assert airwire_total(after) < 0.6 * airwire_total(before)


def test_small_member_routes_completely():
    """16 pads at 1.0 mm pitch with sources on a ring: the family's sanity
    member. If this stops routing 16/16 the router regressed, not the
    fixture.

    Regression for the first thing the family found (2026-10-13): the
    sources are through-hole pads on top-mounted parts and the class is
    locked to In2.Cu+B.Cu, and ``realize`` routed 0/16 because a pin's
    drilled PRIMARY pad was never offered as a terminal on the other
    signal layers (``_sibling_pad_terminals_by_pin`` covered non-primary
    pads only); with F.Cu allowed the same board routed 16/16."""
    r = qf.route(SMALL)
    assert r.complete, r
    assert r.seconds < 60


@pytest.mark.slow
def test_ewod_like_member_scoreboard():
    """The EWOD-shaped member (80 pads, 55 nets, 0.8 mm pitch, grid
    sources, In2+B.Cu) with and without the warm start. Floors only;
    the measured numbers live in the plan doc's Benchmarks section."""
    cold = qf.route(EWOD_LIKE, warm="none")
    warm = qf.route(EWOD_LIKE, warm="distance")
    plaza_cold = qf.route(PLAZA, warm="none")
    plaza_warm = qf.route(PLAZA, warm="distance")
    print(qf.scoreboard([cold, warm, plaza_cold, plaza_warm]))
    assert cold.total == warm.total == plaza_cold.total == plaza_warm.total == 55
    assert warm.routed >= cold.routed
    assert plaza_warm.routed >= plaza_cold.routed
    # 2026-10-13: grid 46 -> 55/55 (4 vias), plaza 41 -> 53/55 (35 vias):
    # the plaza packing is the dogfood's wall. When the plaza member
    # reaches 55/55 the plan-router slices did their job — move this
    # floor up with the measurement in the plan doc.
    assert warm.complete
    assert plaza_warm.routed >= 50
