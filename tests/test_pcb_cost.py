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
from precis.pcb.capabilities import capability_for
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
    _via_count,
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
from precis.pcb.objectives import NetAnnotation, SignalLevel
from precis.pcb.optimize import (
    MOVE_GENERATORS,
    MoveKind,
    OptimizeConfig,
    OptimizeEngine,
    seed_placement,
)
from precis.pcb.realize import RealizeConfig, realize
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


# ── courtyard_overlap (gr267456) ────────────────────────────────────────
def _two_instance_graph(xa: float, ya: float, xb: float, yb: float) -> dict:
    return {
        "instances": [
            {"refdes": "U0", "x": xa, "y": ya},
            {"refdes": "U1", "x": xb, "y": yb},
        ],
        "nets": [
            {
                "name": "N0",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "1"},
                    {"refdes": "U1", "pin": "1"},
                ],
            }
        ],
    }


def test_courtyard_overlap_scores_worse_when_instances_coincide():
    """Two instances placed on top of each other score a strictly worse
    cost than the same two separated — the direct, hand-built pin to
    gr267456's "nothing prevents this" defect."""
    ir_coincident = from_graph(_two_instance_graph(0.0, 0.0, 0.0, 0.0))
    ir_separated = from_graph(_two_instance_graph(0.0, 0.0, 50.0, 0.0))
    config = CostConfig()

    coincident = evaluate_cost(ir_coincident, Level.L4, config)
    separated = evaluate_cost(ir_separated, Level.L4, config)

    assert coincident.total > separated.total
    assert coincident.risk > separated.risk

    (t,) = [t for t in coincident.terms if t.name == "courtyard_overlap"]
    assert t.raw == pytest.approx(1.0)  # perfect coincidence: fully at budget
    (t,) = [t for t in separated.terms if t.name == "courtyard_overlap"]
    assert t.raw == pytest.approx(0.0)


def test_courtyard_overlap_pair_term_direction_is_lower():
    spec = _BY_NAME["courtyard_overlap"]
    assert spec.direction is BoundDirection.LOWER


def test_courtyard_overlap_pair_term_admissible_before_l3():
    ir = from_graph(_two_instance_graph(0.0, 0.0, 0.0, 0.0))
    from precis.pcb.cost import courtyard_overlap_pair_term

    t = courtyard_overlap_pair_term(ir, 0, 1, Level.L1, CostConfig())
    assert t.is_bound
    assert t.raw == 0.0  # unconstrained pre-placement, same as coupling_bound_k


# ── board_edge_clearance (gr267456 addendum) ────────────────────────────
def test_board_edge_clearance_scores_worse_when_instance_is_outside_a_small_outline():
    """A deliberately small outline + a part outside it — the direct case
    that proves the term fires (unlike the reference run's 300x300 board,
    where every part is trivially inside)."""
    outline = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    graph = {"instances": [{"refdes": "U0", "x": 5.0, "y": 5.0}], "nets": []}
    config = CostConfig()

    ir_inside = from_graph(graph, outline=outline)
    inside = evaluate_cost(ir_inside, Level.L4, config)

    ir_outside = from_graph(graph, outline=outline)
    ir_outside.inst_x[0] = 500.0
    ir_outside.inst_y[0] = 500.0
    outside = evaluate_cost(ir_outside, Level.L4, config)

    assert outside.total > inside.total
    assert outside.risk > inside.risk

    (t,) = [t for t in inside.terms if t.name == "board_edge_clearance"]
    assert t.raw == pytest.approx(0.0)
    (t,) = [t for t in outside.terms if t.name == "board_edge_clearance"]
    assert t.raw > 1.0  # well past the outline, not just past the budget


def test_board_edge_clearance_absent_without_an_outline():
    graph = {"instances": [{"refdes": "U0", "x": 5.0, "y": 5.0}], "nets": []}
    ir = from_graph(graph)  # no outline authored
    result = evaluate_cost(ir, Level.L4, CostConfig())
    assert not [t for t in result.terms if t.name == "board_edge_clearance"]


def test_board_edge_clearance_registered_term_direction_is_lower():
    spec = _BY_NAME["board_edge_clearance"]
    assert spec.direction is BoundDirection.LOWER


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


# ── reachability property: registry-driven, mirrors the admissibility
# property's shape above — every registered term must be able to VARY,
# not just point the right direction. See cost.py's module docstring:
# a term over a continuous variable that only rewards an exact coincidence
# is measure-zero (the crossings estimator's old Euler-bound backing was
# provably always zero on any real, star-decomposed board — the archetype
# this test exists to catch structurally, not just once by hand). Iterates
# TERMS itself, so a newly registered term is checked for free.
#: Individually-justified exceptions, printed by the test itself — a term
#: that provably cannot vary across ANY generated state or move, because
#: nothing in this test's exploration (board topology, positions, every
#: :class:`~precis.pcb.optimize.MoveKind`, or the ``CostConfig``
#: side-channels a term reads) touches the field it depends on. Empty as
#: of this writing: ``extended_part_fees`` is invariant under every
#: MoveKind (see optimize.py's "evaluated ONCE at construction" note) but
#: DOES vary with board topology, which :func:`_reachability_board`
#: randomizes per trial — state diversity, not a move, is what exercises
#: it, and that's a legitimate way to satisfy this property, not a
#: workaround. ``via_count`` USED to need the same carve-out (it read
#: ``ir.n_vias``, invariant under every MoveKind since nothing ever grew
#: it — see :func:`precis.pcb.rules.implied_via_count`'s docstring for
#: that defect) but no longer does: it is now genuinely
#: ``MoveKind.LAYER_ASSIGN``/``PLANE_PROMOTE``/``PLANE_DEMOTE``-reachable,
#: which is exactly what lets this property test catch it varying below
#: WITHOUT the ``ir.add_via`` seeding this test used to lean on (that
#: seeding is now provably inert for ``via_count`` — see
#: ``test_via_count_nonzero_through_the_real_production_path_not_seeded``).
#: If a future term turns out to need an exception, name it here with its
#: own reason; never widen this to a blanket skip.
_NOT_MOVE_REACHABLE: dict[str, str] = {
    "courtyard_overlap": (
        "Unreachable BY A GENERATED MOVE since 2026-08-28, deliberately: "
        "OptimizeEngine._placement_is_legal now rejects any TRANSLATE/SWAP "
        "proposal that overlaps two parts' keep-outs, so no move this test "
        "can generate produces a nonzero value. The term is NOT dead — it "
        "still scores states this engine did not author (a human-placed or "
        "locked pose loaded from the store, a design hydrated with "
        "overlapping instances), which is precisely the case where a "
        "report matters and no move-time filter can help. What changed is "
        "that the engine can no longer PURCHASE an overlap: a measured run "
        "with this term active and no hard constraint settled with 10 "
        "overlapping pairs, because a penalty is a price."
    ),
    "board_edge_clearance": (
        "Same reason as courtyard_overlap: OptimizeEngine.bounds_for now "
        "shrinks each instance's legal centre range by its OWN keep-out "
        "radius, so no generated move can place a part's copper off the "
        "board. Still scores externally-authored poses."
    ),
}


def _reachability_board(rng: random.Random) -> dict:
    """A random connectivity graph deliberately richer than
    :func:`_random_state`: a mix of every net class (power/ground so
    ``loop_inductance`` has live candidates, rf/clock so ``coupling``
    does), a coin-flip current annotation per net (``thermal_rise``'s two
    branches), and a coin-flip ``extended_part`` per instance — the one
    remaining MONEY term no MoveKind ever touches (:data:`_NOT_MOVE_
    REACHABLE`'s docstring), varied here by state diversity instead.
    ``via_count`` no longer needs a topology carve-out (see that
    docstring) -- it varies through real ``LAYER_ASSIGN``/``PLANE_
    PROMOTE``/``PLANE_DEMOTE`` moves below instead, the same as
    ``layer_count``."""
    n_inst = rng.randint(6, 12)
    instances = [
        {"refdes": f"U{i}", "extended_part": rng.random() < 0.3} for i in range(n_inst)
    ]
    refdes_list = [inst["refdes"] for inst in instances]
    nets = []
    for i in range(n_inst - 1):  # a chain: guarantees every instance is reachable
        net: dict = {
            "name": f"N{i}",
            "net_class": rng.choice(_CLASSES),
            "domain": "electrical",
            "members": [
                {"refdes": f"U{i}", "pin": f"n{i}a"},
                {"refdes": f"U{i + 1}", "pin": f"n{i}b"},
            ],
        }
        if rng.random() < 0.5:
            net["est_current_a"] = rng.uniform(0.2, 4.0)
        nets.append(net)
    for i in range(max(1, n_inst // 3)):  # multi-member nets: real star geometry
        k = rng.randint(2, min(4, n_inst))
        idxs = rng.sample(range(n_inst), k)
        nets.append(
            {
                "name": f"X{i}",
                "net_class": rng.choice(_CLASSES),
                "domain": "electrical",
                "members": [
                    {"refdes": refdes_list[idx], "pin": f"x{i}_{j}"}
                    for j, idx in enumerate(idxs)
                ],
            }
        )
    return {"instances": instances, "nets": nets}


def _random_net_annotations(ir, rng: random.Random) -> dict[int, NetAnnotation]:
    """``coupling``'s own required input — ``aggressor_strength`` reads
    ``edge_rate_v_per_ns``, which the "unknown" default
    (:data:`precis.pcb.objectives._UNKNOWN_DEFAULT`) deliberately leaves
    ``None`` (never assert an aggressor without evidence), so
    ``config.net_annotations`` is genuinely part of the state this term
    needs varied — a fixture-richness gap, not a move-reachability one
    (no MoveKind ever touches this side-channel either); see
    :func:`test_every_registered_term_is_move_reachable`'s docstring."""
    out = {}
    for net_id in range(ir.n_nets):
        edge = rng.uniform(0.1, 2.0) if rng.random() < 0.5 else 0.0
        impedance = rng.choice([1.0, 100.0, 1.0e4, 1.0e6])
        out[net_id] = NetAnnotation(
            impedance_ohm=impedance,
            edge_rate_v_per_ns=edge or None,
            signal_level=SignalLevel.LOGIC,
        )
    return out


def _random_class_rules(rng: random.Random) -> dict[str, dict[str, float]]:
    """``thermal_rise``'s own required input for variation: absent an
    override, ``rules.py`` resolves EVERY current-annotated net's width to
    exactly carry its current (test_thermal_rise_current_derived_width_
    reads_at_budget, verbatim) — a fraction pinned to ~1.0 BY DESIGN, not
    a bug. A random per-trial ``track_width_mm`` override (almost never
    the exact width a given trial's random current would resolve to) is
    what makes the fraction actually move; without it this term is
    tautologically constant across every trial, a fixture-richness gap
    exactly like ``coupling``'s missing annotations, not a move-
    reachability one (no MoveKind touches ``class_rules`` either)."""
    return {
        cls: {"track_width_mm": rng.uniform(0.1, 1.5)} for cls in ("power", "ground")
    }


def test_every_registered_term_is_move_reachable():
    """For every :data:`TERMS` entry: generate many randomized IR states,
    apply every available :class:`~precis.pcb.optimize.MoveKind`, and
    require the term's own aggregate (the SAME sum/max collapse
    :func:`evaluate_cost` itself uses — :func:`_term_scalar`) to take at
    least two distinct values somewhere across that exploration. A term
    that cannot vary under any generated state or move is either dead or
    measure-zero (cost.py's module docstring) — indistinguishable from a
    correctly-working term by any other test, which is exactly the failure
    this property exists to make loud. Registry-driven: a newly registered
    term is checked automatically, no per-term test to remember to write.
    """
    outer_rng = random.Random(0)
    values: dict[str, set[float]] = {spec.name: set() for spec in TERMS}

    def _record(ir, cfg: CostConfig) -> None:
        for spec in TERMS:
            scalar = _term_scalar(spec.estimate(ir, Level.L4, cfg), spec.family)
            values[spec.name].add(round(scalar, 9))

    # A modest outline (module docstring's "board_edge_clearance" needs
    # SOME state where an instance genuinely lands near/outside it) --
    # comparable in scale to `seed_placement`'s own cluster pitch, so
    # across 60 randomized trials + 20 moves each, some placements sit
    # comfortably inside it and some spill outside/near its edge.
    _reachability_outline = [(0.0, 0.0), (30.0, 0.0), (30.0, 30.0), (0.0, 30.0)]

    for trial in range(60):
        state_rng = random.Random(outer_rng.randrange(2**31))
        graph = _reachability_board(state_rng)
        ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=_reachability_outline)
        seed_placement(ir, state_rng)
        # No `ir.add_via` seeding here (2026-08-28): that used to be the
        # only thing making `via_count` vary in this test, which is
        # exactly the hole `test_via_count_nonzero_through_the_real_
        # production_path_not_seeded` closes explicitly -- `via_count` now
        # varies for real, through the `LAYER_ASSIGN`/`PLANE_PROMOTE`/
        # `PLANE_DEMOTE` moves the loop below already applies.

        cost_config = CostConfig(
            assumed_max_gap_mm=500.0,
            coupling_decay_mm=50.0,  # generous: keeps k visible regardless of board scale
            net_annotations=_random_net_annotations(ir, state_rng),
            class_rules=_random_class_rules(state_rng),
        )
        engine = OptimizeEngine(ir, OptimizeConfig(seed=trial, cost=cost_config))
        _record(ir, cost_config)

        move_rng = random.Random(outer_rng.randrange(2**31))
        for _ in range(20):
            kind = move_rng.choice(list(MoveKind))
            move = MOVE_GENERATORS[kind](engine, move_rng, 8.0)
            if move is None:
                continue
            engine.apply_move(move)
            _record(ir, cost_config)

    print(f"move-reachability opt-outs (individually justified): {_NOT_MOVE_REACHABLE}")
    dead = {
        name: vals
        for name, vals in values.items()
        if len(vals) < 2 and name not in _NOT_MOVE_REACHABLE
    }
    assert not dead, (
        "term(s) never varied across randomized states + every available "
        "MoveKind -- measure-zero or dead (cost.py module docstring's "
        f"discrete-vs-continuous rule): {dead}"
    )


def test_move_reachability_opt_outs_still_fire_on_an_externally_authored_pose():
    """The two ``_NOT_MOVE_REACHABLE`` opt-outs claim their terms are
    unreachable *by a generated move* but still score a pose the engine
    did not author. That is a claim, and an unbacked claim is exactly how
    a dead term survives a reachability test — so verify it, by building
    the illegal pose directly instead of asking a move generator for one.

    Without this, adding a name to ``_NOT_MOVE_REACHABLE`` would be
    indistinguishable from deleting the term's only check.
    """
    graph = {
        "instances": [{"refdes": "U0"}, {"refdes": "U1"}],
        "nets": [
            {
                "name": "N",
                "members": [
                    {"refdes": "U0", "pin": "1"},
                    {"refdes": "U1", "pin": "1"},
                ],
            }
        ],
    }
    outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=outline)
    # Coincident, and one of them far outside the board — a pose no
    # TRANSLATE/SWAP this engine generates can reach any more.
    ir.move_instance(0, x=10.0, y=10.0, rot=0.0)
    ir.move_instance(1, x=10.0, y=10.0, rot=0.0)
    cfg = CostConfig()
    by_name = {spec.name: spec for spec in TERMS}

    overlap = _term_scalar(
        by_name["courtyard_overlap"].estimate(ir, Level.L4, cfg),
        by_name["courtyard_overlap"].family,
    )
    assert overlap > 0.0, "courtyard_overlap silent on two coincident parts"

    ir.move_instance(1, x=60.0, y=60.0, rot=0.0)
    edge = _term_scalar(
        by_name["board_edge_clearance"].estimate(ir, Level.L4, cfg),
        by_name["board_edge_clearance"].family,
    )
    assert edge > 0.0, "board_edge_clearance silent on a part 40mm off the board"


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


# ── via_count: derived from segment layer assignments, never `ir.n_vias`
# (gr, 2026-08-28). `ir.n_vias` only grows via `PcbIR.add_via`, which has
# ZERO production callers anywhere in this package -- this term read that
# dead field and was structurally always zero, so the optimizer paid
# nothing for a layer change while `realize.py` independently emitted real
# vias wherever a track's layer differed from `realize.PAD_LAYER`. See
# `precis.pcb.rules.implied_via_count`'s docstring for the shared rule
# that now backs both sides.
def test_via_count_nonzero_through_the_real_production_path_not_seeded():
    """Reachable via ``from_graph -> set_layer -> evaluate_cost`` -- the
    real path a caller actually exercises -- never by seeding
    ``ir.add_via`` into a fixture (the hole the pre-fix reachability
    property test papered over, see ``test_every_registered_term_is_
    move_reachable`` above): ``add_via`` is now provably inert for this
    term, pinned explicitly below."""
    ir = from_graph(_current_net_graph(None, "signal"), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)  # In1.Cu -- not PAD_LAYER (0): a real layer transition
    (t,) = _via_count(ir, Level.L4, CostConfig())
    assert t.raw > 0.0

    ir_seeded = from_graph(_current_net_graph(None, "signal"), stackup=DEFAULT_STACKUP)
    ir_seeded.add_via(layer_span=0b0011)  # seeding an IR via row alone
    (t_seeded,) = _via_count(ir_seeded, Level.L4, CostConfig())
    assert (
        t_seeded.raw == 0.0
    )  # ...buys this term nothing: the segment never left PAD_LAYER


def test_via_count_zero_when_the_segment_stays_on_pad_layer():
    ir = from_graph(_current_net_graph(None, "signal"), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 0)  # PAD_LAYER -- nothing to transition
    (t,) = _via_count(ir, Level.L4, CostConfig())
    assert t.raw == 0.0


def test_via_count_zero_below_l1():
    ir = from_graph(_current_net_graph(None, "signal"), stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)
    (t,) = _via_count(ir, Level.L0, CostConfig())
    assert t.raw == 0.0
    assert t.is_bound


def test_via_count_matches_realized_vias_exactly():
    """The anti-drift pin, and the whole point of this fix: for the SAME
    IR, the USD ``cost.py`` charges for vias must correspond EXACTLY to
    the number of vias ``realize.realize()`` actually emits -- this is the
    test that would have caught the original defect, where ``via_count``
    silently charged 0 while ``realize.py`` emitted real geometry onto the
    gerbers."""
    ir = from_graph(_current_net_graph(5.0, "power"), stackup=DEFAULT_STACKUP)
    ir.move_instance(0, x=0.0, y=0.0, rot=0.0)
    ir.move_instance(1, x=10.0, y=0.0, rot=0.0)
    ir.set_layer(0, 1)  # a real layer transition, with a current annotation
    fab_caps = capability_for("4layer")
    cost_config = CostConfig(fab_caps=fab_caps, via_usd=0.02)
    realize_config = RealizeConfig(fab_caps=fab_caps, router="tangent")

    (t,) = _via_count(ir, Level.L4, cost_config)
    result = realize(ir, config=realize_config)

    assert result.vias  # sanity: the production path really emits vias here
    assert t.raw == pytest.approx(len(result.vias) * cost_config.via_usd)


def test_via_count_registered_term_direction_is_lower():
    (spec,) = [t for t in TERMS if t.name == "via_count"]
    assert spec.direction is BoundDirection.LOWER
