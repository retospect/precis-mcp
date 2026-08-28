"""Unit + property tests for precis.pcb.cost — the single most important
module in this slice. No DB.

Covers: the admissibility property (coarse <= fine total, generated over
many random IR states — "nothing else catches the estimator hierarchy
silently rotting"), the undefined-!=-zero rule, money-sums-vs-margin-max
aggregation (the 1x99%-vs-500x5% case explicitly), and the required,
non-empty per-term justification.
"""

from __future__ import annotations

import random

import pytest

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.cost import (
    _BY_NAME,
    _CRITICALITY_WEIGHT,
    TERMS,
    BoundDirection,
    CostConfig,
    Criticality,
    Family,
    TermSpec,
    TermValue,
    _thermal_rise,
    aggregate_margin,
    crossings_term_for_layer,
    evaluate_cost,
    hardened_penalty,
    margin_penalties,
    money_total,
)
from precis.pcb.ir import (
    Level,
    compute_gap_capacity,
    from_graph,
    same_layer_crossing_count,
)
from precis.pcb.rules import ipc2221_capacity_a

_CLASSES = ["signal", "power", "ground", "rf", "clock"]


# ── term registry hygiene ────────────────────────────────────────────
def test_every_registered_term_has_a_justification():
    for spec in TERMS:
        assert spec.justification and spec.justification.strip()


def test_termspec_rejects_empty_justification():
    with pytest.raises(ValueError):
        TermSpec(
            "x", Family.MONEY, Criticality.COSMETIC, "   ", lambda ir, level, cfg: []
        )


def test_registry_covers_both_families():
    families = {spec.family for spec in TERMS}
    assert families == {Family.MONEY, Family.MARGIN}


# ── aggregation: money sums, margin maxes ────────────────────────────
def test_money_total_sums():
    terms = [
        TermValue("board_area", Family.MONEY, "board", 10.0, "j"),
        TermValue("layer_count", Family.MONEY, "board", 5.0, "j"),
        TermValue("via_count", Family.MONEY, "board", 2.5, "j"),
    ]
    assert money_total(terms) == pytest.approx(17.5)


def test_margin_aggregates_by_max_not_diluted_by_many_small_values():
    """The reason the max-not-sum rule exists: one net at 99% of budget
    is a real problem even sitting beside 500 nets at 5% — and adding
    MORE quiet nets must not move the signal at all, which is exactly
    what a sum would get wrong (it grows with the quiet-net count)."""
    critical = TermValue("gap_capacity", Family.MARGIN, "critical_net", 0.99, "j")
    few_quiet = [
        TermValue("gap_capacity", Family.MARGIN, f"n{i}", 0.05, "j") for i in range(5)
    ]
    many_quiet = [
        TermValue("gap_capacity", Family.MARGIN, f"n{i}", 0.05, "j") for i in range(500)
    ]

    weight = _CRITICALITY_WEIGHT[_BY_NAME["gap_capacity"].criticality]
    expected_peak = weight * hardened_penalty(0.99, 0.0)

    risk_few = aggregate_margin(
        margin_penalties([critical, *few_quiet], _BY_NAME, schedule=0.0)
    )
    risk_many = aggregate_margin(
        margin_penalties([critical, *many_quiet], _BY_NAME, schedule=0.0)
    )
    # max correctly ignores how many quiet nets sit beside the peak
    assert risk_few == pytest.approx(expected_peak)
    assert risk_many == pytest.approx(expected_peak)

    # A naive sum — the aggregation this rule exists to avoid — keeps
    # growing linearly with the quiet-net count (5 -> 500 is a 100x
    # jump) instead of tracking the one net that actually matters, which
    # is exactly what `max` (above) correctly refuses to do.
    quiet_sum_few = sum(margin_penalties(few_quiet, _BY_NAME, schedule=0.0))
    quiet_sum_many = sum(margin_penalties(many_quiet, _BY_NAME, schedule=0.0))
    assert quiet_sum_many == pytest.approx(quiet_sum_few * 100)


def test_margin_max_ignores_family_leakage_from_money():
    # money terms must never enter the margin aggregation
    terms = [TermValue("board_area", Family.MONEY, "board", 999.0, "j")]
    penalties = margin_penalties(terms, _BY_NAME, schedule=0.0)
    assert penalties == []


# ── hardened_penalty: convexity IS the schedule ──────────────────────
def test_hardened_penalty_monotonic_in_fraction():
    assert hardened_penalty(0.0, 0.0) == 0.0
    assert (
        hardened_penalty(0.2, 0.0)
        < hardened_penalty(0.5, 0.0)
        < hardened_penalty(0.9, 0.0)
    )
    assert hardened_penalty(0.9, 0.0) < hardened_penalty(1.5, 0.0)


def test_hardened_penalty_sharpens_with_schedule_below_budget():
    # same (sub-budget) fraction, later schedule -> steeper (larger) penalty
    early = hardened_penalty(0.8, schedule=0.0)
    late = hardened_penalty(0.8, schedule=1.0)
    assert late > early


def test_hardened_penalty_nondecreasing_in_schedule_for_fixed_fraction():
    # the actual "hardening" property: the SAME fraction must never look
    # cheaper as the schedule advances, at any fraction.
    for fraction in (0.1, 0.5, 0.8, 0.95, 0.999, 1.0, 1.2, 2.0):
        prev = hardened_penalty(fraction, 0.0)
        for schedule in (0.25, 0.5, 0.75, 1.0):
            cur = hardened_penalty(fraction, schedule)
            assert cur >= prev - 1e-12, (fraction, schedule, prev, cur)
            prev = cur


def test_hardened_penalty_low_fraction_stays_cheap_regardless_of_schedule():
    # comfortably under budget -> near enough to 0 at any schedule; the
    # barrier only tightens near the boundary, not everywhere.
    assert hardened_penalty(0.1, 1.0) < 0.02


def test_hardened_penalty_continuous_at_the_budget_boundary():
    below = hardened_penalty(0.999999, schedule=0.5)
    at = hardened_penalty(1.0, schedule=0.5)
    assert at == pytest.approx(1.0 + 4.0 * 0.5)  # at_budget = 1 + 4*schedule
    assert below == pytest.approx(at, abs=1e-3)


# ── undefined != zero ────────────────────────────────────────────────
def _two_pin_graph(net_class: str = "signal"):
    return {
        "instances": [{"refdes": "U1"}, {"refdes": "U2"}],
        "nets": [
            {
                "name": "N1",
                "net_class": net_class,
                "domain": "electrical",
                "members": [{"refdes": "U1", "pin": "1"}, {"refdes": "U2", "pin": "1"}],
            }
        ],
    }


def test_gap_capacity_undefined_is_a_nonzero_admissible_bound():
    ir = from_graph(_two_pin_graph())  # no L3 positions at all
    result = evaluate_cost(ir, Level.L1)
    gap_terms = [t for t in result.terms if t.name == "gap_capacity"]
    assert gap_terms
    for t in gap_terms:
        assert t.is_bound
        assert t.raw > 0.0  # never zero, or congestion would look free


def test_layer_count_zero_at_l0_is_a_true_bound_not_a_swallowed_signal():
    # contrast with gap_capacity: 0 is fine here because it's a genuine
    # minimum for a *summed* money term, not a max-aggregated margin term.
    ir = from_graph(_two_pin_graph())
    result = evaluate_cost(ir, Level.L0)
    (layer_term,) = [t for t in result.terms if t.name == "layer_count"]
    assert (
        layer_term.raw > 0
    )  # "at least one layer" — bounded below by 1, never literally 0


# ── admissibility property test ──────────────────────────────────────
def _random_state(rng: random.Random):
    """A random, well-formed (non-overlapping) IR — grid-placed, so
    per-instance gaps stay comfortably under the test's generous
    ``assumed_max_gap_mm``, and every instance/net gets distinct pin
    names per membership (never two nets sharing one physical pin)."""
    n_inst = rng.randint(4, 8)
    instances = [{"refdes": f"U{i}"} for i in range(n_inst)]
    refdes_list = [inst["refdes"] for inst in instances]
    n_nets = rng.randint(2, 5)
    nets = []
    for i in range(n_nets):
        k = rng.randint(2, min(4, n_inst))
        members = rng.sample(refdes_list, k)
        nets.append(
            {
                "name": f"N{i}",
                "net_class": rng.choice(_CLASSES),
                "domain": "electrical",
                "members": [{"refdes": r, "pin": f"p{i}"} for r in members],
            }
        )
    ir = from_graph({"instances": instances, "nets": nets}, stackup=DEFAULT_STACKUP)

    cols = max(1, int(n_inst**0.5) + 1)
    for i in range(n_inst):
        row, col = divmod(i, cols)
        ir.inst_x[i] = col * 10.0 + rng.uniform(-1.0, 1.0)
        ir.inst_y[i] = row * 10.0 + rng.uniform(-1.0, 1.0)

    for s in range(ir.n_segments):
        ir.set_layer(s, rng.choice((0, 1)))

    compute_gap_capacity(ir, pitch_mm=0.3)
    return ir


def _term_scalar(terms: list[TermValue], family: Family) -> float:
    """Collapse one term's per-region :class:`TermValue`\\ s to the SAME
    scalar :func:`evaluate_cost` itself uses to combine them into
    ``total`` — money terms SUM, margin terms take the (raw) PEAK — so
    the per-term admissibility check below compares apples to apples with
    how the term actually enters the aggregate cost, not an aggregation
    invented just for this test."""
    if not terms:
        return 0.0
    if family is Family.MONEY:
        return sum(t.raw for t in terms)
    return max(t.raw for t in terms)


@pytest.mark.parametrize("trial", range(200))
def test_admissibility_direction_per_term(trial):
    """The estimator-hierarchy regression check, REVISED 2026-08-28 to
    assert each term's OWN declared :class:`BoundDirection` rather than
    one global ``coarse.total <= fine.total`` inequality. That global
    inequality stopped being sound the moment ``crossings`` became a
    genuine UPPER bound (the geometric sweep-line count backing it can
    only ever overstate, never understate, the eventual realized crossing
    count — see :func:`precis.pcb.ir.same_layer_crossing_count`'s
    docstring) — a coarse UPPER-bound placeholder is *supposed* to sit
    ABOVE the fine value, the opposite of every LOWER-bound term, so
    summing everything into one number and demanding it only shrinks
    would have silently broken the very property this test exists to
    protect. Run over many generated cases, one assertion PER REGISTERED
    TERM — this is what actually catches a term quietly declaring, or
    losing, the wrong direction, not a relaxed global check that happens
    to still pass."""
    rng = random.Random(trial)
    ir = _random_state(rng)
    config = CostConfig(
        assumed_max_gap_mm=500.0
    )  # generous: keeps the coarse gap bound valid regardless of jitter

    for spec in TERMS:
        coarse = _term_scalar(spec.estimate(ir, Level.L1, config), spec.family)
        fine = _term_scalar(spec.estimate(ir, Level.L4, config), spec.family)
        if spec.direction is BoundDirection.LOWER:
            assert coarse <= fine + 1e-9, (
                f"trial {trial}: {spec.name} declares LOWER but coarse={coarse} "
                f"> fine={fine}"
            )
        else:
            assert coarse >= fine - 1e-9, (
                f"trial {trial}: {spec.name} declares UPPER but coarse={coarse} "
                f"< fine={fine}"
            )


def test_admissibility_direction_holds_across_a_hardened_schedule_too():
    """The hardening schedule reshapes :func:`hardened_penalty`, never the
    raw ``estimate()`` values this direction property is actually about
    (no term's ``estimate`` reads ``config.schedule``) — so this pins that
    down explicitly across a spread of schedule values, the same per-term
    check as above rather than the retired global ``total`` comparison."""
    rng = random.Random(12345)
    ir = _random_state(rng)
    for schedule in (0.0, 0.3, 0.7, 1.0):
        config = CostConfig(assumed_max_gap_mm=500.0, schedule=schedule)
        for spec in TERMS:
            coarse = _term_scalar(spec.estimate(ir, Level.L1, config), spec.family)
            fine = _term_scalar(spec.estimate(ir, Level.L4, config), spec.family)
            if spec.direction is BoundDirection.LOWER:
                assert coarse <= fine + 1e-9
            else:
                assert coarse >= fine - 1e-9


# ── crossings: registered margin term, GEOMETRICALLY backed since 2026-08-28
# by ir.same_layer_crossing_count (see that function's docstring, and
# ir.same_layer_crossing_bound's, for the forest proof of why the
# original Euler-bound backing was provably always zero on a real board).
def _crossing_pair_graph():
    """Two 2-member nets whose straight-line airwires visibly cross -- the
    two diagonals of a square (U0-U1 and U2-U3). Neither net shares an
    instance or a pin with the other, so `from_graph`'s star decomposition
    (trivial for a 2-member net) yields exactly one segment per net --
    the SAME fixture shape tests/test_pcb_ir.py uses for
    `same_layer_crossing_count` (mirrored here rather than imported
    across test modules)."""
    return {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 10.0, "y": 10.0},
            {"refdes": "U2", "x": 0.0, "y": 10.0},
            {"refdes": "U3", "x": 10.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "A",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U0", "pin": "1"}, {"refdes": "U1", "pin": "1"}],
            },
            {
                "name": "B",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U2", "pin": "1"}, {"refdes": "U3", "pin": "1"}],
            },
        ],
    }


def test_crossings_term_registered_in_margin_family_with_justification():
    (spec,) = [t for t in TERMS if t.name == "crossings"]
    assert spec.family is Family.MARGIN
    assert spec.justification and spec.justification.strip()
    assert spec.direction is BoundDirection.UPPER


def test_crossings_term_matches_ir_geometric_count_and_is_always_a_bound():
    ir = from_graph(_crossing_pair_graph(), stackup=DEFAULT_STACKUP)
    ir.seg_layer[:] = 0
    config = CostConfig()
    t = crossings_term_for_layer(ir, 0, Level.L4, config)
    assert t.family is Family.MARGIN
    assert t.is_bound
    assert t.raw == pytest.approx(
        same_layer_crossing_count(ir, 0) / config.crossings_tolerance
    )
    assert t.raw > 0.0


def test_crossings_term_worse_with_a_crossing_than_with_it_resolved():
    """A hand-built fixture with two segments crossing on one layer scores
    worse than the same fixture with one moved to another layer -- and the
    term's value decreases when a LAYER_ASSIGN-shaped move (`set_layer`)
    resolves it."""
    ir = from_graph(_crossing_pair_graph(), stackup=DEFAULT_STACKUP)
    ir.seg_layer[:] = 0
    config = CostConfig()
    crossing = crossings_term_for_layer(ir, 0, Level.L4, config)
    assert crossing.raw > 0.0

    ir.set_layer(0, 1)  # move net A's segment off layer 0
    resolved = crossings_term_for_layer(ir, 0, Level.L4, config)
    assert resolved.raw < crossing.raw
    assert resolved.raw == pytest.approx(0.0)


def test_crossings_term_upper_bound_shrinks_toward_the_geometric_count_by_level():
    """`crossings` is an UPPER bound (BoundDirection.UPPER), so "coarser"
    means "more pessimistic" here, the mirror image of every LOWER-bound
    term: the pre-L1 placeholder (every segment on the board could land on
    this layer) is loosest, the pre-L3 placeholder (real per-layer segment
    count, no positions yet -- every same-layer PAIR might cross) is
    tighter, and the L3+ geometric sweep-line count is tightest/exact.
    Each tier must be >= the next -- the per-term half of what
    test_admissibility_direction_per_term checks generically, pinned down
    concretely and STRICTLY (not merely non-decreasing) on a fixture with
    a genuine crossing (A x B), a non-crossing same-layer segment (C, so
    the L1 pair-count bound overstates the true L4 count), and a segment
    on a DIFFERENT layer (D, so the L0 whole-board bound overstates the
    true per-layer L1 count)."""
    graph = _crossing_pair_graph()
    graph["instances"] += [
        {"refdes": "U4", "x": 100.0, "y": 100.0},
        {"refdes": "U5", "x": 110.0, "y": 100.0},
        {"refdes": "U6", "x": 200.0, "y": 200.0},
        {"refdes": "U7", "x": 210.0, "y": 200.0},
    ]
    graph["nets"] += [
        {
            "name": "C",
            "net_class": "signal",
            "domain": "electrical",
            "members": [{"refdes": "U4", "pin": "1"}, {"refdes": "U5", "pin": "1"}],
        },
        {
            "name": "D",
            "net_class": "signal",
            "domain": "electrical",
            "members": [{"refdes": "U6", "pin": "1"}, {"refdes": "U7", "pin": "1"}],
        },
    ]
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.seg_layer[:] = 0
    ir.set_layer(3, 1)  # net D's segment (id 3) moves to layer 1
    config = CostConfig()

    l0 = crossings_term_for_layer(ir, 0, Level.L0, config)
    l1 = crossings_term_for_layer(ir, 0, Level.L1, config)
    l4 = crossings_term_for_layer(ir, 0, Level.L4, config)
    assert (
        l0.raw > l1.raw > l4.raw
    )  # 4 segs -> C(4,2)=6; 3 on layer0 -> C(3,2)=3; 1 true crossing
    assert l4.raw == pytest.approx(1.0 / config.crossings_tolerance)
    assert all(t.is_bound for t in (l0, l1, l4))  # never claims to BE ground truth


def test_crossings_term_is_nonzero_on_a_realistic_star_decomposed_netlist_with_crossings():
    """**The opposite of the pre-fix regression test this replaces.**
    Before 2026-08-28, `same_layer_crossing_bound` was PROVABLY zero on
    any star-decomposed board (a forest, per that function's docstring),
    so a star's spoke could visibly cross another net's segment and the
    old term would still read exactly 0 -- the defect this fix exists
    for. This fixture is exactly that: net A is a real star (hub U0, two
    spokes), net B is an ordinary 2-pin net whose segment geometrically
    crosses one of A's spokes. The GEOMETRIC backing must catch it."""
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},  # star hub
            {"refdes": "U1", "x": 10.0, "y": 10.0},  # spoke 1 (crosses net B)
            {
                "refdes": "U2",
                "x": 10.0,
                "y": -10.0,
            },  # spoke 2 (parallel to B, no cross)
            {"refdes": "U3", "x": 0.0, "y": 10.0},  # net B
            {"refdes": "U4", "x": 10.0, "y": 0.0},  # net B
        ],
        "nets": [
            {
                "name": "A",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "1"},
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "U2", "pin": "1"},
                ],
            },
            {
                "name": "B",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U3", "pin": "1"}, {"refdes": "U4", "pin": "1"}],
            },
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.seg_layer[:] = 0
    assert ir.n_segments == 3  # net A's star (2 spokes) + net B's single segment
    assert same_layer_crossing_count(ir, 0) == 1  # A's U0-U1 spoke x B's U3-U4
    t = crossings_term_for_layer(ir, 0, Level.L4, CostConfig())
    assert t.raw > 0.0


# ── thermal_rise: scored against the ACTUAL resolved width (gr-shaped:
# the exact bug this closes -- an optimizer reasoning about current while
# the geometry it scores ignores it) ─────────────────────────────────────
def _current_net_graph(current_a: float | None, net_class: str = "power"):
    net: dict = {
        "name": "N1",
        "net_class": net_class,
        "domain": "electrical",
        "members": [{"refdes": "U1", "pin": "1"}, {"refdes": "U2", "pin": "1"}],
    }
    if current_a is not None:
        net["est_current_a"] = current_a
    return {"instances": [{"refdes": "U1"}, {"refdes": "U2"}], "nets": [net]}


def test_thermal_rise_no_current_annotation_keeps_todays_class_fraction_behaviour():
    """ "Keep today's behaviour" (task, verbatim): a net with no current
    annotation must not invent one -- it falls back to the original
    class-fraction placeholder, unchanged."""
    ir = from_graph(_current_net_graph(None, "power"), stackup=DEFAULT_STACKUP)
    config = CostConfig()
    (t,) = _thermal_rise(ir, Level.L4, config)
    assert t.raw == pytest.approx(config.thermal_budget_fraction["power"])
    assert t.is_bound


def test_thermal_rise_current_derived_width_reads_at_budget():
    """No override: the resolver sizes the width to EXACTLY carry the
    net's current at the configured temperature rise, so scoring that
    same width against that same current reads ~1.0 (at budget), not a
    manufactured violation."""
    ir = from_graph(_current_net_graph(5.0, "power"), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)  # F.Cu -- outer
    config = CostConfig()
    (t,) = _thermal_rise(ir, Level.L4, config)
    assert t.raw == pytest.approx(1.0, rel=1e-6)
    assert not t.is_bound  # a real derivation, not a placeholder, once layer is known


def test_thermal_rise_flags_a_too_narrow_class_override_as_a_violation():
    """The exact bug this term exists to catch: an authored class rule
    that under-sizes the copper for the net's actual current must show up
    as OVER budget, not silently pass."""
    ir = from_graph(_current_net_graph(5.0, "power"), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)
    config = CostConfig(class_rules={"power": {"track_width_mm": 0.3}})
    (t,) = _thermal_rise(ir, Level.L4, config)
    assert t.raw > 1.0


def test_thermal_rise_inner_layer_reads_worse_than_outer_for_a_fixed_width():
    """The outer-vs-inner split matters here too: with a FIXED (class-
    override) width independent of layer, the same current reads a
    higher (worse) fraction on an inner layer than on an outer one --
    inner copper dissipates heat less readily for the same cross-section."""
    override = {"power": {"track_width_mm": 1.0}}

    ir_outer = from_graph(_current_net_graph(5.0, "power"), stackup=DEFAULT_STACKUP)
    ir_outer.set_layer(0, 0)  # F.Cu
    (t_outer,) = _thermal_rise(ir_outer, Level.L4, CostConfig(class_rules=override))

    ir_inner = from_graph(_current_net_graph(5.0, "power"), stackup=DEFAULT_STACKUP)
    ir_inner.set_layer(0, 1)  # In1.Cu
    (t_inner,) = _thermal_rise(ir_inner, Level.L4, CostConfig(class_rules=override))

    assert t_inner.raw > t_outer.raw
    expected_outer = 5.0 / ipc2221_capacity_a(1.0, layer_is_outer=True)
    expected_inner = 5.0 / ipc2221_capacity_a(1.0, layer_is_outer=False)
    assert t_outer.raw == pytest.approx(expected_outer, rel=1e-6)
    assert t_inner.raw == pytest.approx(expected_inner, rel=1e-6)


def test_thermal_rise_admissible_before_layer_assignment_is_known():
    """LOWER-bound admissibility, concretely: before L1 (no layer decided
    yet), the optimistic outer assumption must never overstate the risk
    once the net's real layer -- here deliberately an INNER one -- is
    known. Coarse (L0) <= fine (L1+), matching this term's declared
    BoundDirection.LOWER."""
    override = {"power": {"track_width_mm": 1.0}}
    ir = from_graph(_current_net_graph(5.0, "power"), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)  # the net's real assignment is an INNER layer
    config = CostConfig(class_rules=override)

    (coarse,) = _thermal_rise(ir, Level.L0, config)
    (fine,) = _thermal_rise(ir, Level.L4, config)
    assert coarse.is_bound
    assert not fine.is_bound
    assert coarse.raw <= fine.raw + 1e-9


def test_thermal_rise_registered_term_direction_is_lower():
    (spec,) = [t for t in TERMS if t.name == "thermal_rise"]
    assert spec.direction is BoundDirection.LOWER
