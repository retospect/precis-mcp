"""Unit + property tests for precis.pcb.optimize — the joint place+route
optimizer's slice-6 walking skeleton (placement moves only). No DB.

Covers: the locality invariant (a placement move leaves L1/L2 clean),
delta correctness (the highest-value test in this slice — an incremental
delta must equal a fresh full :func:`~precis.pcb.cost.evaluate_cost` call,
over many random moves and both the apply and undo paths), determinism,
the `fixed='xy'|'rot'|'both'` move restrictions (and that locked parts
still contribute cost), SA improving on the constructive seed, and the
digest's per-term/per-region breakdown.
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.cost import CostConfig, evaluate_cost
from precis.pcb.ir import Level, from_graph
from precis.pcb.optimize import (
    MOVE_GENERATORS,
    MoveKind,
    OptimizeConfig,
    OptimizeEngine,
    digest_toon,
    optimize,
    seed_placement,
)

_CLASSES = ["signal", "power", "ground", "rf", "clock"]


def _board(n: int = 12, *, seed: int = 0, fixed: dict[int, str] | None = None) -> dict:
    """A synthetic connectivity graph: a chain (guarantees every instance
    is reachable) plus a handful of 3-member nets (so gap_capacity,
    loop_inductance -- power/ground classes -- and coupling -- rf/clock
    classes -- all have live candidates). Pin labels are unique per
    (net, member) so from_graph never accidentally merges two nets onto
    one physical pin."""
    fixed = fixed or {}
    rng = random.Random(seed)
    instances = [
        {"refdes": f"U{i}", **({"fixed": fixed[i]} if i in fixed else {})}
        for i in range(n)
    ]
    nets = []
    for i in range(n - 1):
        nets.append(
            {
                "name": f"N{i}",
                "net_class": rng.choice(_CLASSES),
                "domain": "electrical",
                "members": [
                    {"refdes": f"U{i}", "pin": f"n{i}a"},
                    {"refdes": f"U{i + 1}", "pin": f"n{i}b"},
                ],
            }
        )
    for i in range(max(1, n // 3)):
        idxs = rng.sample(range(n), min(3, n))
        nets.append(
            {
                "name": f"X{i}",
                "net_class": rng.choice(_CLASSES),
                "domain": "electrical",
                "members": [
                    {"refdes": f"U{idx}", "pin": f"x{i}_{j}"}
                    for j, idx in enumerate(idxs)
                ],
            }
        )
    return {"instances": instances, "nets": nets}


def _seeded_ir(n: int = 12, *, graph_seed: int = 0, seed_rng_seed: int = 0, fixed=None):
    ir = from_graph(_board(n, seed=graph_seed, fixed=fixed), stackup=DEFAULT_STACKUP)
    seed_placement(ir, random.Random(seed_rng_seed))
    return ir


# ── locality: the architecture's core invariant ──────────────────────────
def test_translate_move_leaves_l1_l2_clean():
    ir = _seeded_ir(8, graph_seed=1, seed_rng_seed=2)
    assert not ir.dirty_l1.any() and not ir.dirty_l2.any()  # clean after seeding too
    engine = OptimizeEngine(ir, OptimizeConfig(seed=2))
    rng = random.Random(3)
    move = MOVE_GENERATORS[MoveKind.TRANSLATE](engine, rng, 5.0)
    assert move is not None
    engine.apply_move(move)

    assert not ir.dirty_l1.any()
    assert not ir.dirty_l2.any()
    assert ir.dirty_l3[move.instances[0]]


def test_rotate_and_swap_moves_also_leave_l1_l2_clean():
    ir = _seeded_ir(8, graph_seed=4, seed_rng_seed=5)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=5))
    rng = random.Random(6)
    for kind in (MoveKind.ROTATE, MoveKind.SWAP):
        move = MOVE_GENERATORS[kind](engine, rng, 5.0)
        if move is None:
            continue
        engine.apply_move(move)
        assert not ir.dirty_l1.any()
        assert not ir.dirty_l2.any()


# ── delta correctness: the highest-value test in this slice ─────────────
def test_delta_correctness_matches_full_reevaluation_over_random_moves():
    ir = _seeded_ir(14, graph_seed=7, seed_rng_seed=8)
    cost_config = CostConfig()
    engine = OptimizeEngine(ir, OptimizeConfig(seed=8, cost=cost_config))

    move_rng = random.Random(99)
    kinds = list(MoveKind)
    for trial in range(200):
        kind = kinds[move_rng.randrange(len(kinds))]
        move = MOVE_GENERATORS[kind](engine, move_rng, 8.0)
        if move is None:
            continue
        engine.apply_move(move)
        if move_rng.random() < 0.5:
            # exercise the undo path (the same _apply pipeline, reversed)
            # just as often as the apply-only path.
            engine.undo_move(move)
        engine.schedule = move_rng.choice([0.0, 0.3, 0.7, 1.0])

        full = evaluate_cost(
            ir, Level.L4, dataclasses.replace(cost_config, schedule=engine.schedule)
        )
        assert engine.money() == pytest.approx(full.money, rel=1e-9, abs=1e-9), trial
        assert engine.risk() == pytest.approx(full.risk, rel=1e-9, abs=1e-9), trial
        assert engine.total() == pytest.approx(full.total, rel=1e-9, abs=1e-9), (
            f"trial {trial}: engine={engine.total()} full={full.total}"
        )


def test_delta_correctness_survives_a_full_short_anneal():
    """The same property, exercised through the real SA loop (accept/
    reject via Metropolis, not a coin flip) rather than synthetic
    apply/undo alternation."""
    ir = _seeded_ir(10, graph_seed=10, seed_rng_seed=11)
    cost_config = CostConfig()
    config = OptimizeConfig(seed=11, iters=120, cost=cost_config)
    engine = OptimizeEngine(ir, config)
    engine.anneal(random.Random(config.seed))

    full = evaluate_cost(
        ir, Level.L4, dataclasses.replace(cost_config, schedule=engine.schedule)
    )
    assert engine.total() == pytest.approx(full.total, rel=1e-9, abs=1e-9)


# ── determinism ───────────────────────────────────────────────────────
def test_determinism_same_seed_same_result():
    graph = _board(10, seed=12)
    ir1 = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir2 = from_graph(graph, stackup=DEFAULT_STACKUP)
    config = OptimizeConfig(seed=13, iters=300)

    r1 = optimize(ir1, config)
    r2 = optimize(ir2, config)

    assert r1.cost_after == pytest.approx(r2.cost_after)
    assert r1.positions == r2.positions
    assert [(m.kind, m.instances, m.accepted) for m in r1.moves] == [
        (m.kind, m.instances, m.accepted) for m in r2.moves
    ]


# ── fixed='xy'|'rot'|'both' ───────────────────────────────────────────
def test_fixed_flags_restrict_move_generator_membership():
    fixed = {0: "xy", 1: "both", 2: "rot"}
    ir = _seeded_ir(6, graph_seed=14, seed_rng_seed=14, fixed=fixed)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=14))

    assert 0 not in engine._movable_xy and 0 in engine._movable_rot
    assert 1 not in engine._movable_xy and 1 not in engine._movable_rot
    assert 2 in engine._movable_xy and 2 not in engine._movable_rot


def test_fixed_xy_never_translates_fixed_both_never_moves_over_a_real_anneal():
    fixed = {0: "xy", 1: "both", 2: "rot"}
    ir = _seeded_ir(10, graph_seed=15, seed_rng_seed=15, fixed=fixed)
    before = {
        i: (float(ir.inst_x[i]), float(ir.inst_y[i]), float(ir.inst_rot[i]))
        for i in (0, 1, 2)
    }
    config = OptimizeConfig(seed=15, iters=600)
    engine = OptimizeEngine(ir, config)
    engine.anneal(random.Random(config.seed))
    after = {
        i: (float(ir.inst_x[i]), float(ir.inst_y[i]), float(ir.inst_rot[i]))
        for i in (0, 1, 2)
    }

    assert after[0][:2] == before[0][:2]  # 'xy': position never moves
    assert after[1] == before[1]  # 'both': nothing moves
    assert after[2][2] == before[2][2]  # 'rot': rotation never moves


def test_locked_components_still_contribute_cost():
    fixed = {0: "both"}
    ir = _seeded_ir(8, graph_seed=16, seed_rng_seed=16, fixed=fixed)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=16))

    segs0 = list(ir._segs_of_instance.get(0, []))
    assert segs0, "fixture must connect the locked instance to at least one segment"
    for s in segs0:
        assert ("gap_capacity", s) in engine._margin  # tracked, not dropped


# ── SA improves on the constructive seed ─────────────────────────────
def test_anneal_improves_cost_vs_the_constructive_seed():
    ir = from_graph(_board(24, seed=20), stackup=DEFAULT_STACKUP)
    config = OptimizeConfig(seed=20, iters=4000)
    result = optimize(ir, config)

    assert result.cost_after < result.cost_before


# ── digest: per-term AND per-region, not a scalar ────────────────────
def test_digest_carries_per_term_and_per_region_breakdown():
    ir = from_graph(_board(16, seed=21), stackup=DEFAULT_STACKUP)
    config = OptimizeConfig(seed=21, iters=500)
    result = optimize(ir, config)
    d = result.digest

    names = {t.name for t in d.terms}
    assert {"board_area", "gap_capacity"} <= names
    assert len(d.terms) >= 5  # money + margin terms both represented
    assert d.regions  # at least one spatial region carries a margin entry
    for r in d.regions:
        assert r.nets

    text = digest_toon(result)
    assert "total=" in text
    assert "gap_capacity" in text


def test_optimize_config_rejects_soft_p_norm():
    with pytest.raises(ValueError):
        OptimizeConfig(cost=CostConfig(p_norm=2.0))
