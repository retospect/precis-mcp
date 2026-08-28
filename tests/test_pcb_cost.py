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
    CostConfig,
    Criticality,
    Family,
    TermSpec,
    TermValue,
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
    same_layer_crossing_bound,
)

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


@pytest.mark.parametrize("trial", range(200))
def test_admissibility_coarse_never_exceeds_fine(trial):
    """The estimator-hierarchy regression check: generate a random state,
    evaluate the SAME object at a coarse (L1) and a fine (L4) level, and
    assert the coarse total never overstates the fine one. Run over many
    generated cases — this is the only thing that catches an estimator
    quietly losing its admissibility."""
    rng = random.Random(trial)
    ir = _random_state(rng)
    config = CostConfig(
        assumed_max_gap_mm=500.0
    )  # generous: keeps the coarse gap bound valid regardless of jitter

    coarse = evaluate_cost(ir, Level.L1, config)
    fine = evaluate_cost(ir, Level.L4, config)

    assert coarse.total <= fine.total + 1e-9, (
        f"trial {trial}: coarse={coarse.total} > fine={fine.total}; "
        f"coarse terms={coarse.terms}; fine terms={fine.terms}"
    )


def test_admissibility_holds_across_a_hardened_schedule_too():
    rng = random.Random(12345)
    ir = _random_state(rng)
    for schedule in (0.0, 0.3, 0.7, 1.0):
        config = CostConfig(assumed_max_gap_mm=500.0, schedule=schedule)
        coarse = evaluate_cost(ir, Level.L1, config)
        fine = evaluate_cost(ir, Level.L4, config)
        assert coarse.total <= fine.total + 1e-9


# ── crossings: registered margin term backed by ir.same_layer_crossing_bound
def _complete_graph(n: int, layer: int = 0):
    """K_n, one net per pair, deliberately reusing pin "1" for every
    membership of a given instance -- the SAME construction
    tests/test_pcb_ir.py's own K5 fixture uses (mirrored here rather than
    imported across test modules) so every instance is exactly one vertex
    in the layer graph. This is NOT how a real netlist looks (a real
    physical pin belongs to exactly one net) -- see
    test_crossings_term_is_zero_on_a_realistic_star_decomposed_netlist
    below for why that distinction matters."""
    instances = [{"refdes": f"U{i}"} for i in range(n)]
    nets = [
        {
            "name": f"E{i}_{j}",
            "net_class": "signal",
            "domain": "electrical",
            "members": [
                {"refdes": f"U{i}", "pin": "1"},
                {"refdes": f"U{j}", "pin": "1"},
            ],
        }
        for i in range(n)
        for j in range(i + 1, n)
    ]
    ir = from_graph({"instances": instances, "nets": nets}, stackup=DEFAULT_STACKUP)
    ir.seg_layer[:] = layer
    return ir


def test_crossings_term_registered_in_margin_family_with_justification():
    (spec,) = [t for t in TERMS if t.name == "crossings"]
    assert spec.family is Family.MARGIN
    assert spec.justification and spec.justification.strip()


def test_crossings_term_matches_ir_bound_and_is_always_a_bound():
    ir = _complete_graph(5)  # K5: 5 vertices, 10 edges -> Euler bound = 1
    config = CostConfig()
    t = crossings_term_for_layer(ir, 0, Level.L4, config)
    assert t.family is Family.MARGIN
    assert t.is_bound
    assert t.raw == pytest.approx(
        same_layer_crossing_bound(ir, 0, refine=False) / config.crossings_tolerance
    )
    assert t.raw > 0.0


def test_crossings_term_worse_with_a_crossing_than_with_it_resolved():
    """A hand-built fixture with two segments crossing on one layer scores
    worse than the same fixture with one moved to another layer -- and the
    term's value decreases when a LAYER_ASSIGN-shaped move (`set_layer`)
    resolves it."""
    ir = _complete_graph(5)
    config = CostConfig()
    crossing = crossings_term_for_layer(ir, 0, Level.L4, config)
    assert crossing.raw > 0.0

    ir.set_layer(0, 1)  # move one K5 edge off layer 0
    resolved = crossings_term_for_layer(ir, 0, Level.L4, config)
    assert resolved.raw < crossing.raw
    assert resolved.raw == pytest.approx(0.0)


def test_crossings_term_is_level_invariant_admissibility_holds_trivially():
    """`crossings` is deliberately level-invariant (module docstring: the
    finer per-connected-component variant needs dynamic connectivity
    tracking this slice's O(1)-per-move locality budget can't afford), so
    coarse (L1) and fine (L4) values are exactly equal here -- admissible
    (coarse <= fine) by equality, extending
    test_admissibility_coarse_never_exceeds_fine's own property to a
    fixture where the term is actually non-zero rather than trivially so."""
    ir = _complete_graph(5)
    config = CostConfig()
    coarse = crossings_term_for_layer(ir, 0, Level.L1, config)
    fine = crossings_term_for_layer(ir, 0, Level.L4, config)
    assert coarse.raw == pytest.approx(fine.raw)
    assert coarse.raw > 0.0


def test_crossings_term_is_zero_on_a_realistic_star_decomposed_netlist():
    """**Found on contact (2026-08-28), reported rather than silently
    worked around.** `same_layer_crossing_bound` is the coarse, whole-
    layer Euler edge-count bound (E - (3V-6)). `from_graph`'s star
    decomposition, combined with the "one physical pin belongs to exactly
    one net" invariant every real netlist has, means a layer's segment
    graph is ALWAYS a vertex-disjoint forest of per-net stars (no two
    different nets' segments ever share a pin/vertex) -- and a forest
    always satisfies E <= V-1 <= 3V-6 for V>=3, so this bound is
    PROVABLY zero for any board `from_graph` produces the normal way,
    not merely "usually small". `_complete_graph` above only gets a
    non-zero bound by deliberately reusing one pin id per instance across
    many "nets" (an artificial construction, not a real netlist shape).
    Consequence: on a real board, `crossings` is currently a real,
    correct, admissible term that will not yet move LAYER_ASSIGN's cost
    -- a genuine gap between this term's admissibility (satisfied) and
    its usefulness on this system's actual segment topology (not yet),
    worth a design follow-up (a denser segment decomposition, or an
    estimator that isn't pure per-layer V/E Euler counting) rather than
    silently pretending the mechanism helps today."""
    n = 14
    instances = [{"refdes": f"U{i}"} for i in range(n)]
    nets = []
    for i in range(n - 1):
        nets.append(
            {
                "name": f"N{i}",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": f"U{i}", "pin": f"n{i}a"},
                    {"refdes": f"U{i + 1}", "pin": f"n{i}b"},
                ],
            }
        )
    for i in range(max(1, n // 3)):
        nets.append(
            {
                "name": f"X{i}",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": f"U{idx}", "pin": f"x{i}_{j}"}
                    for j, idx in enumerate((0, i + 1, (i + 2) % n))
                ],
            }
        )
    ir = from_graph({"instances": instances, "nets": nets}, stackup=DEFAULT_STACKUP)
    ir.seg_layer[:] = 0
    assert ir.n_segments > n  # plenty of segments crammed onto one layer
    assert same_layer_crossing_bound(ir, 0, refine=False) == 0
    assert crossings_term_for_layer(ir, 0, Level.L4, CostConfig()).raw == 0.0
