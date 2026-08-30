"""Unit + property tests for precis.pcb.optimize — the joint place+route
optimizer. No DB.

Covers: the locality invariant (each move kind dirties exactly the levels
its own IR mutator dirties, nothing else), delta correctness (the
highest-value test in this slice — an incremental delta must equal a
fresh full :func:`~precis.pcb.cost.evaluate_cost` call, over many random
moves of EVERY kind and both the apply and undo paths), determinism, the
`fixed='xy'|'rot'|'both'` move restrictions (and that locked parts still
contribute cost), SA improving on the constructive seed, the digest's
per-term/per-region breakdown, and (slice 7) layer assignment, side flip,
plane promote/demote, and pin swap — including pin swap's admissible-set/
exclusion contract and its measured crossing reduction.
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.cost import CostConfig, evaluate_cost
from precis.pcb.ir import Level, from_graph, plane_layers_of
from precis.pcb.optimize import (
    MOVE_GENERATORS,
    Move,
    MoveKind,
    OptimizeConfig,
    OptimizeEngine,
    digest_toon,
    optimize,
    seed_placement,
)
from precis.pcb.pinswap import PinSwapGroup, propose_reassignment, total_group_crossings

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


# ── slice 7: layer assignment, side flip, plane promote/demote ──────────
def test_layer_assign_dirties_l1_leaves_l2_l3_clean():
    ir = _seeded_ir(8, graph_seed=30, seed_rng_seed=31)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=31))
    assert not ir.dirty_l1.any()  # the engine's own layer seed cleaned up after itself
    l3_before = ir.dirty_l3.copy()
    rng = random.Random(32)
    move = MOVE_GENERATORS[MoveKind.LAYER_ASSIGN](engine, rng, 5.0)
    assert move is not None
    engine.apply_move(move)

    seg = move.segments[0]
    assert ir.dirty_l1[seg]
    assert not ir.dirty_l2.any()
    assert (ir.dirty_l3 == l3_before).all()  # untouched -- no component moved
    assert int(ir.seg_layer[seg]) == move.new_int[0]

    engine.undo_move(move)
    assert int(ir.seg_layer[seg]) == move.old_int[0]


def test_layer_assign_only_targets_signal_role_layers():
    ir = _seeded_ir(8, graph_seed=33, seed_rng_seed=34)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=34))
    signal_idx = {
        i for i, layer in enumerate(DEFAULT_STACKUP) if layer["role"] == "signal"
    }
    rng = random.Random(35)
    for _ in range(50):
        move = MOVE_GENERATORS[MoveKind.LAYER_ASSIGN](engine, rng, 5.0)
        if move is None:
            continue
        assert move.new_int[0] in signal_idx


def test_side_flip_dirties_l2_leaves_l1_l3_clean():
    ir = _seeded_ir(8, graph_seed=36, seed_rng_seed=37)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=37))
    l3_before = ir.dirty_l3.copy()
    rng = random.Random(38)
    move = MOVE_GENERATORS[MoveKind.SIDE_FLIP](engine, rng, 5.0)
    assert move is not None
    engine.apply_move(move)

    seg = move.segments[0]
    assert ir.dirty_l2[seg]
    assert not ir.dirty_l1.any()
    assert (ir.dirty_l3 == l3_before).all()  # untouched -- no component moved
    assert int(ir.seg_side[seg]) == move.new_int[0]
    assert move.new_int[0] != move.old_int[0]

    engine.undo_move(move)
    assert int(ir.seg_side[seg]) == move.old_int[0]


def test_plane_promote_demote_dirty_only_that_nets_segments_leave_l2_l3_clean():
    ir = _seeded_ir(10, graph_seed=39, seed_rng_seed=40)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=40))
    l3_before = ir.dirty_l3.copy()
    rng = random.Random(41)
    move = MOVE_GENERATORS[MoveKind.PLANE_PROMOTE](engine, rng, 5.0)
    assert move is not None
    net = move.net
    assert net is not None
    seg_ids = [s for s in range(ir.n_segments) if int(ir.seg_net[s]) == net]
    assert seg_ids

    engine.apply_move(move)
    assert plane_layers_of(int(ir.net_plane_layers[net])) == [move.new_int[0]]
    for s in seg_ids:
        assert ir.dirty_l1[s]
    assert not ir.dirty_l2.any()
    assert (ir.dirty_l3 == l3_before).all()  # untouched -- no component moved

    engine.undo_move(move)
    assert int(ir.net_plane_layers[net]) == 0


def test_plane_promote_only_targets_plane_role_layers():
    ir = _seeded_ir(10, graph_seed=42, seed_rng_seed=43)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=43))
    plane_idx = {
        i for i, layer in enumerate(DEFAULT_STACKUP) if layer["role"] == "plane"
    }
    rng = random.Random(44)
    for _ in range(50):
        move = MOVE_GENERATORS[MoveKind.PLANE_PROMOTE](engine, rng, 5.0)
        if move is None:
            continue
        assert move.new_int[0] in plane_idx
        engine.undo_move(
            Move(MoveKind.PLANE_PROMOTE, net=move.net, new_int=move.new_int)
        )


def test_plane_promote_reduces_gap_capacity_penalty_to_zero():
    """The backlog's "plane-served nets excluded from the objective" —
    the nearest analog this slice's registered terms have to it."""
    ir = _seeded_ir(10, graph_seed=45, seed_rng_seed=46)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=46))
    rng = random.Random(47)
    move = None
    for _ in range(50):
        candidate = MOVE_GENERATORS[MoveKind.PLANE_PROMOTE](engine, rng, 5.0)
        if candidate is not None:
            move = candidate
            break
    assert move is not None
    net = move.net
    assert net is not None
    seg_ids = [s for s in range(ir.n_segments) if int(ir.seg_net[s]) == net]

    engine.apply_move(move)
    for s in seg_ids:
        assert engine._margin[("gap_capacity", s)].raw == 0.0


# ── locked_plane_nets: an authored plane assignment is a constraint ──────
def test_gen_plane_demote_excludes_locked_nets():
    """A locked net is never a PLANE_DEMOTE candidate; an unlocked
    promoted net on the SAME board still is -- the "locked" filter must
    not turn into a blanket freeze of every plane assignment."""
    ir = _seeded_ir(10, graph_seed=71, seed_rng_seed=72)
    ir.promote_plane(0, 1)  # In1.Cu
    ir.promote_plane(1, 2)  # In2.Cu
    engine = OptimizeEngine(
        ir, OptimizeConfig(seed=72, locked_plane_nets=frozenset({0}))
    )
    rng = random.Random(73)
    seen_nets = set()
    for _ in range(50):
        move = MOVE_GENERATORS[MoveKind.PLANE_DEMOTE](engine, rng, 5.0)
        assert move is not None  # net 1 is always a candidate
        assert move.net != 0, "a locked net must never be offered to PLANE_DEMOTE"
        seen_nets.add(move.net)
    assert seen_nets == {1}, "the only unlocked promoted net must be the one proposed"


def test_gen_plane_promote_excludes_locked_nets_and_their_layer():
    """A locked net is never itself a promotion candidate, and its OWN
    plane layer is never offered to a different net -- both halves of
    "already spoken for", not just one."""
    ir = _seeded_ir(10, graph_seed=74, seed_rng_seed=75)
    ir.promote_plane(0, 1)  # In1.Cu -- locked
    engine = OptimizeEngine(
        ir, OptimizeConfig(seed=75, locked_plane_nets=frozenset({0}))
    )
    rng = random.Random(76)
    for _ in range(50):
        move = MOVE_GENERATORS[MoveKind.PLANE_PROMOTE](engine, rng, 5.0)
        if move is None:
            continue
        assert move.net != 0, "a locked net must never be re-offered to PLANE_PROMOTE"
        assert move.new_int is not None
        assert move.new_int[0] != 1, (
            "In1.Cu is the locked net's own layer -- it must never be offered "
            "to a different net as if it were free"
        )


def test_locked_plane_net_survives_a_full_anneal_unlocked_one_still_demotes():
    """The end-to-end contract: run the REAL search, not just the move
    generator, over a graph with a locked authored plane assignment
    alongside an unlocked (optimizer-visible) one.

    Before ``locked_plane_nets`` existed this was measured as a silent
    regression on the ESP32-C3 reference fixture: an authored
    ``{'VCC3V3': 2, 'GND': 1}`` came back ``{}`` after a 3000-iteration
    anneal at seed 1 -- every authored plane demoted, because nothing
    distinguished a human's declaration from the search's own exploration
    (this cost model already disfavours planes: 79 PLANE_PROMOTE proposals
    over 3000 iterations, all rejected on cost -- module docstring's
    PLANE_PROMOTE/DEMOTE note -- so PLANE_DEMOTE on an authored net was
    accepted immediately and permanently). This pins the fix: the locked
    net must survive that same anneal, while an otherwise-identical
    UNLOCKED promoted net remains exactly as demotable as it always was."""
    ir = from_graph(_board(10, seed=77), stackup=DEFAULT_STACKUP)
    ir.promote_plane(0, 1)  # locked, like an authored "GND -> In1.Cu"
    ir.promote_plane(1, 2)  # unlocked, like an optimizer-derived assignment
    optimize(
        ir,
        OptimizeConfig(iters=3000, seed=1, locked_plane_nets=frozenset({0})),
    )
    assert plane_layers_of(int(ir.net_plane_layers[0])) == [1], (
        "the locked/authored net must survive"
    )
    assert int(ir.net_plane_layers[1]) == 0, (
        "the unlocked net must remain exactly as demotable as before -- a "
        "blanket freeze would be as wrong as the original bug"
    )


def test_layer_assign_and_plane_promote_refresh_layer_count():
    ir = _seeded_ir(10, graph_seed=48, seed_rng_seed=49)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=49))
    rng = random.Random(50)
    before = engine._money_static_by_name["layer_count"]
    move = None
    for _ in range(50):
        candidate = MOVE_GENERATORS[MoveKind.PLANE_PROMOTE](engine, rng, 5.0)
        if candidate is not None:
            move = candidate
            break
    assert move is not None
    engine.apply_move(move)
    after = engine._money_static_by_name["layer_count"]
    assert after >= before  # a plane layer entering "used" never lowers the count


# ── via_count term (gr, 2026-08-28): derived from seg_layer, no longer
# structurally zero -- see precis.pcb.rules.implied_via_count's docstring
# for the defect this closes (ir.n_vias had zero production writers).
def test_layer_assign_refreshes_via_count_by_a_local_bounded_delta():
    """The engine's own O(1) delta (:meth:`OptimizeEngine.
    _refresh_via_count_for_segment`), pinned directly: every segment starts
    on PAD_LAYER (the engine's own construction-time seed), so every
    per-segment via count starts at zero; a single LAYER_ASSIGN move must
    change ONLY its own segment's cached count -- never any other
    segment's -- and the aggregate ``via_count`` money delta must be
    exactly that one segment's own count times ``via_usd``, the same
    bounded-delta contract ``crossings``/``gap_capacity`` already obey."""
    ir = _seeded_ir(10, graph_seed=60, seed_rng_seed=61)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=61))
    assert all(n == 0 for n in engine._seg_via_count.values())
    before_usd = engine._money_static_by_name["via_count"]
    assert before_usd == pytest.approx(0.0)

    seg = 0
    other_layer = next(layer for layer in engine._signal_layers if layer != 0)
    move = Move(
        MoveKind.LAYER_ASSIGN, segments=(seg,), old_int=(0,), new_int=(other_layer,)
    )
    engine.apply_move(move)

    changed = {s: n for s, n in engine._seg_via_count.items() if n != 0}
    assert changed == {seg: engine._seg_via_count[seg]}  # ONLY the touched segment
    assert engine._seg_via_count[seg] > 0
    after_usd = engine._money_static_by_name["via_count"]
    assert after_usd - before_usd == pytest.approx(
        engine._seg_via_count[seg] * engine.config.cost.via_usd
    )

    engine.undo_move(move)
    assert engine._seg_via_count[seg] == 0
    assert engine._money_static_by_name["via_count"] == pytest.approx(before_usd)


def test_plane_promote_zeroes_via_count_for_a_transitioned_segment():
    """A plane-promoted net's segments dog-bone instead of transitioning
    layers (:func:`precis.pcb.rules.implied_via_count`'s dog-bone branch)
    -- exercised here through a segment that already had real implied vias
    from an earlier LAYER_ASSIGN, mirroring ``gap_capacity``'s own
    plane-promote-clears-the-penalty test above."""
    ir = _seeded_ir(10, graph_seed=63, seed_rng_seed=64)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=64))
    seg = 0
    net = int(ir.seg_net[seg])
    other_layer = next(layer for layer in engine._signal_layers if layer != 0)
    engine.apply_move(
        Move(
            MoveKind.LAYER_ASSIGN, segments=(seg,), old_int=(0,), new_int=(other_layer,)
        )
    )
    assert engine._seg_via_count[seg] > 0

    assert engine._plane_layers
    plane_layer = engine._plane_layers[0]
    engine.apply_move(Move(MoveKind.PLANE_PROMOTE, net=net, new_int=(plane_layer,)))
    assert engine._seg_via_count[seg] == 0


# ── crossings term (gr, 2026-08-28): GEOMETRIC backing, LAYER_ASSIGN's
# real cost effect ────────────────────────────────────────────────────
def _crossing_ir_with_positions():
    """A GEOMETRIC fixture — replaces the old K5 pin-reuse construction,
    which faked non-planarity for the now-retired Euler-bound backing
    (see :func:`precis.pcb.ir.same_layer_crossing_bound`'s docstring for
    the forest proof of why that trick was ever necessary, and why it
    can't back a real cost signal). Net A (U0-U1) and net B (U2-U3) are
    ORDINARY 2-pin nets forming an X — the crossing comes from real 2D
    geometry, not from an artificial multi-edge-per-vertex graph. N2
    (hub U4, three spokes) adds a genuine star alongside them, the same
    shape ``from_graph`` produces for any real multi-member net, so this
    fixture is simultaneously realistic AND guaranteed non-zero — exactly
    the combination the pre-fix bug (`same_layer_crossing_bound` was
    always zero on real, star-decomposed nets) could never produce."""
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 40.0, "y": 40.0},
            {"refdes": "U2", "x": 0.0, "y": 40.0},
            {"refdes": "U3", "x": 40.0, "y": 0.0},
            {"refdes": "U4", "x": 20.0, "y": 60.0},
            {"refdes": "U5", "x": 10.0, "y": 80.0},
            {"refdes": "U6", "x": 30.0, "y": 80.0},
            {"refdes": "U7", "x": 20.0, "y": 100.0},
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
            {
                "name": "N2",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U4", "pin": "1"},
                    {"refdes": "U5", "pin": "1"},
                    {"refdes": "U6", "pin": "1"},
                    {"refdes": "U7", "pin": "1"},
                ],
            },
        ],
    }
    return from_graph(graph, stackup=DEFAULT_STACKUP)


def test_layer_assign_is_no_longer_cost_neutral():
    """The whole point of registering ``crossings``: unlike before, a
    LAYER_ASSIGN move that RESOLVES a real geometric crossing produces a
    real, non-zero, cost-REDUCING ``total()`` delta. Targets net A's
    segment (id 0) explicitly — the one actually party to the A x B
    crossing — rather than a random segment, since (unlike the retired
    Euler-bound backing) moving an UNRELATED segment (e.g. one of N2's
    non-crossing star spokes) is legitimately still cost-neutral for
    ``crossings`` under the geometric backing."""
    ir = _crossing_ir_with_positions()
    engine = OptimizeEngine(ir, OptimizeConfig(seed=1))
    crossings_before = engine._margin[("crossings", 0)].raw
    assert crossings_before > 0.0  # A x B geometric crossing, both start on layer 0
    before = engine.total()

    other_layer = next(layer for layer in engine._signal_layers if layer != 0)
    move = Move(
        MoveKind.LAYER_ASSIGN, segments=(0,), old_int=(0,), new_int=(other_layer,)
    )
    engine.apply_move(move)
    after = engine.total()
    assert after != before
    assert after < before  # moving net A's segment off layer 0 resolves the crossing
    assert engine._margin[("crossings", 0)].raw < crossings_before
    assert engine._margin[("crossings", 0)].raw == pytest.approx(0.0)


def test_side_flip_remains_cost_neutral_even_with_crossings_registered():
    """**Reported, not silently worked around**: unlike LAYER_ASSIGN,
    SIDE_FLIP genuinely cannot become cost-sensitive at this engine's
    fidelity. ``crossings`` is backed by
    :func:`precis.pcb.ir.same_layer_crossing_count`, which reads segment
    endpoints at INSTANCE-centroid granularity — a side flip never
    perturbs an instance's own (x, y), so a straight-line count is
    structurally blind to it (the module docstring's SIDE_FLIP note has
    the full reasoning). This test pins the resulting (still correct)
    behaviour on the SAME geometrically-crossing fixture."""
    ir = _crossing_ir_with_positions()
    engine = OptimizeEngine(ir, OptimizeConfig(seed=5))
    assert engine._margin[("crossings", 0)].raw > 0.0  # a real crossing is present
    before = engine.total()
    rng = random.Random(6)
    move = MOVE_GENERATORS[MoveKind.SIDE_FLIP](engine, rng, 5.0)
    assert move is not None
    engine.apply_move(move)
    assert engine.total() == pytest.approx(before)


def test_delta_correctness_for_crossings_over_random_moves():
    """Delta correctness — incremental delta equals full re-evaluation —
    specifically exercised on a fixture where ``crossings`` is actually
    non-zero at some point during the run (``_seeded_ir``'s netlist, used
    by the module's general delta-correctness test below, is not
    GUARANTEED to ever produce a real geometric crossing under
    ``seed_placement``'s connectivity-clustering seed — clustering
    deliberately keeps connected instances close together, which tends to
    AVOID crossings — so that test alone would never reliably catch a
    ``crossings``-specific incremental bug). Covers LAYER_ASSIGN and
    TRANSLATE explicitly — the two move kinds that can actually change
    ``crossings``' value (LAYER_ASSIGN by moving a segment between
    layers, TRANSLATE by moving a segment's own geometry) — alongside
    SIDE_FLIP/ROTATE (provably cost-neutral for this term, exercised for
    contrast). This is the test that caught silent-fatal bugs in slices
    3, 6 and 7."""
    ir = _crossing_ir_with_positions()
    cost_config = CostConfig()
    engine = OptimizeEngine(ir, OptimizeConfig(seed=3, cost=cost_config))
    move_rng = random.Random(4)
    kinds = [
        MoveKind.LAYER_ASSIGN,
        MoveKind.SIDE_FLIP,
        MoveKind.TRANSLATE,
        MoveKind.ROTATE,
    ]
    saw_nonzero_crossings = False
    for trial in range(200):
        kind = kinds[move_rng.randrange(len(kinds))]
        move = MOVE_GENERATORS[kind](engine, move_rng, 8.0)
        if move is None:
            continue
        engine.apply_move(move)
        if move_rng.random() < 0.5:
            engine.undo_move(move)
        if any(
            name == "crossings" and tv.raw > 0.0
            for (name, _key), tv in engine._margin.items()
        ):
            saw_nonzero_crossings = True

        full = evaluate_cost(ir, Level.L4, cost_config)
        assert engine.money() == pytest.approx(full.money, rel=1e-9, abs=1e-9), trial
        assert engine.risk() == pytest.approx(full.risk, rel=1e-9, abs=1e-9), trial
        assert engine.total() == pytest.approx(full.total, rel=1e-9, abs=1e-9), (
            f"trial {trial}: engine={engine.total()} full={full.total}"
        )
    assert (
        saw_nonzero_crossings
    )  # the interesting (non-zero) case was actually exercised


# ── courtyard_overlap (gr267456) ────────────────────────────────────────
def _courtyard_ir_with_positions():
    """Two overlapping pairs (U0/U1 coincide-ish, U2/U3 coincide-ish) plus
    the connectivity to give them all a segment — guarantees a real,
    nonzero courtyard_overlap value exists from construction, the same
    "exercise the interesting case" discipline the crossings test above
    follows."""
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 0.4, "y": 0.0},
            {"refdes": "U2", "x": 20.0, "y": 20.0},
            {"refdes": "U3", "x": 20.4, "y": 20.0},
        ],
        "nets": [
            {
                "name": "N0",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "1"},
                    {"refdes": "U2", "pin": "1"},
                ],
            },
            {
                "name": "N1",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "U3", "pin": "1"},
                ],
            },
        ],
    }
    return from_graph(graph, stackup=DEFAULT_STACKUP)


def test_delta_correctness_for_courtyard_overlap_over_random_moves():
    ir = _courtyard_ir_with_positions()
    cost_config = CostConfig()
    engine = OptimizeEngine(ir, OptimizeConfig(seed=5, cost=cost_config))
    move_rng = random.Random(6)
    kinds = [MoveKind.TRANSLATE, MoveKind.ROTATE, MoveKind.SWAP]
    saw_nonzero = False
    for trial in range(200):
        kind = kinds[move_rng.randrange(len(kinds))]
        move = MOVE_GENERATORS[kind](engine, move_rng, 8.0)
        if move is None:
            continue
        engine.apply_move(move)
        if move_rng.random() < 0.5:
            engine.undo_move(move)
        if any(
            name == "courtyard_overlap" and tv.raw > 0.0
            for (name, _key), tv in engine._margin.items()
        ):
            saw_nonzero = True

        full = evaluate_cost(ir, Level.L4, cost_config)
        assert engine.money() == pytest.approx(full.money, rel=1e-9, abs=1e-9), trial
        assert engine.risk() == pytest.approx(full.risk, rel=1e-9, abs=1e-9), trial
        assert engine.total() == pytest.approx(full.total, rel=1e-9, abs=1e-9), (
            f"trial {trial}: engine={engine.total()} full={full.total}"
        )
    assert saw_nonzero  # the interesting (overlapping) case was actually exercised


def test_courtyard_overlap_translate_of_unrelated_instance_leaves_other_pairs_unchanged():
    ir = _courtyard_ir_with_positions()
    engine = OptimizeEngine(ir, OptimizeConfig(seed=7))
    key01 = (0, 1)
    before = engine._margin[("courtyard_overlap", key01)].raw
    assert before > 0.0  # U0/U1 overlap from construction

    # Move U2 (part of the OTHER overlapping pair, U2/U3) a few mm --
    # still nowhere near U0/U1.
    old = (float(ir.inst_x[2]), float(ir.inst_y[2]), float(ir.inst_rot[2]))
    new = (old[0] + 3.0, old[1] - 2.0, old[2])
    move = Move(MoveKind.TRANSLATE, (2,), (old,), (new,))
    engine.apply_move(move)

    after = engine._margin[("courtyard_overlap", key01)].raw
    assert after == before


# ── board_edge_clearance (gr267456 addendum) ────────────────────────────
def test_delta_correctness_for_board_edge_clearance_over_random_moves():
    ir = _seeded_ir(10, graph_seed=30, seed_rng_seed=31)
    ir.outline = [(0.0, 0.0), (15.0, 0.0), (15.0, 15.0), (0.0, 15.0)]
    cost_config = CostConfig()
    engine = OptimizeEngine(ir, OptimizeConfig(seed=31, cost=cost_config))
    # Start ONE instance genuinely off the board. No generated move can
    # produce this state any more (`bounds_for` forbids it), so without
    # authoring it directly the delta-correctness loop below would only
    # ever compare zero against zero and `saw_nonzero` could never be
    # satisfied -- the term would be untested while looking tested.
    ir.move_instance(0, x=40.0, y=40.0)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=31, cost=cost_config))
    move_rng = random.Random(32)
    saw_nonzero = any(
        name == "board_edge_clearance" and tv.raw > 0.0
        for (name, _key), tv in engine._margin.items()
    )
    assert saw_nonzero, "authored off-board pose did not make the term fire"
    for trial in range(150):
        move = MOVE_GENERATORS[MoveKind.TRANSLATE](engine, move_rng, 8.0)
        if move is None:
            continue
        engine.apply_move(move)
        if move_rng.random() < 0.5:
            engine.undo_move(move)
        if any(
            name == "board_edge_clearance" and tv.raw > 0.0
            for (name, _key), tv in engine._margin.items()
        ):
            saw_nonzero = True

        full = evaluate_cost(ir, Level.L4, cost_config)
        assert engine.money() == pytest.approx(full.money, rel=1e-9, abs=1e-9), trial
        assert engine.risk() == pytest.approx(full.risk, rel=1e-9, abs=1e-9), trial
        assert engine.total() == pytest.approx(full.total, rel=1e-9, abs=1e-9), (
            f"trial {trial}: engine={engine.total()} full={full.total}"
        )
    assert saw_nonzero


def test_translate_move_stays_within_a_real_outline_once_one_exists():
    ir = _seeded_ir(8, graph_seed=40, seed_rng_seed=41)
    ir.outline = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
    engine = OptimizeEngine(ir, OptimizeConfig(seed=41))
    # The domain is the outline inset by the copper-to-edge margin -- a
    # part's CENTRE on the outline means its copper is off the board.
    assert engine._placement_bounds == (0.5, 0.5, 19.5, 19.5)

    move_rng = random.Random(42)
    for _ in range(300):
        move = MOVE_GENERATORS[MoveKind.TRANSLATE](engine, move_rng, 8.0)
        if move is None:
            continue
        engine.apply_move(move)

    # Every part's own PADS stay on the board, not merely its centre --
    # `bounds_for` shrinks the domain per instance by that instance's own
    # land-pattern extent, which is the whole point (a module whose pads
    # reach 8.9mm from its centre is not contained by a centre-only
    # bound). This is the assertion the centre-only version could not
    # make, and the one the board_edge_clearance DRC rule actually cares
    # about.
    for i in range(ir.n_instances):
        x, y = float(ir.inst_x[i]), float(ir.inst_y[i])
        assert -1e-9 <= x <= 20.0 + 1e-9
        assert -1e-9 <= y <= 20.0 + 1e-9
        bx0, by0, bx1, by1 = engine.bounds_for(i)
        assert bx0 - 1e-9 <= x <= bx1 + 1e-9
        assert by0 - 1e-9 <= y <= by1 + 1e-9


def test_translate_move_falls_back_to_synthetic_square_without_an_outline():
    ir = _seeded_ir(6, graph_seed=43, seed_rng_seed=44)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=44))
    assert engine._placement_bounds == (0.0, 0.0, engine.board_side, engine.board_side)


# ── slice 7: pin swap ─────────────────────────────────────────────────
def _pin_swap_ir_and_group():
    """Two nets whose airwires obviously cross under the CURRENT pin
    assignment and obviously don't after swapping which of U0's two
    admissible pins each occupies — the hand-built fixture the task asks
    for. U0 sits at the origin with two candidate pins offset left/right;
    U1 (net A's far end) sits up-left, U2 (net B's far end) sits up-right.
    Net A wired to the RIGHT pin and net B to the LEFT pin cross; swapped,
    they don't."""
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": -5.0, "y": 5.0},
            {"refdes": "U2", "x": 5.0, "y": 5.0},
        ],
        "nets": [
            {
                "name": "A",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "right"},
                    {"refdes": "U1", "pin": "1"},
                ],
            },
            {
                "name": "B",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "left"},
                    {"refdes": "U2", "pin": "1"},
                ],
            },
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    u0 = 0
    pin_right = next(
        p
        for p in range(ir.n_pins)
        if int(ir.pin_instance[p]) == u0 and str(ir.pin_label[p]) == "right"
    )
    pin_left = next(
        p
        for p in range(ir.n_pins)
        if int(ir.pin_instance[p]) == u0 and str(ir.pin_label[p]) == "left"
    )
    group = PinSwapGroup(
        instance=u0,
        pins=(pin_left, pin_right),
        offsets={pin_left: (-1.0, 0.0), pin_right: (1.0, 0.0)},
    )
    return ir, group, pin_left, pin_right


def test_pin_swap_reduces_crossings_on_hand_built_fixture():
    ir, group, _pin_left, _pin_right = _pin_swap_ir_and_group()
    before = total_group_crossings(ir, group)
    assert (
        before == 1
    )  # net A (right pin -> upper-left) crosses net B (left pin -> upper-right)

    pairs = propose_reassignment(ir, group)
    assert pairs  # a beneficial swap must be found on this fixture
    for a, b in pairs:
        ir.swap_pins(a, b)

    after = total_group_crossings(ir, group)
    assert after == 0
    assert after < before


def test_pin_swap_dirties_l2_l4_l5_leaves_l1_l3_clean():
    ir, group, pin_left, pin_right = _pin_swap_ir_and_group()
    config = OptimizeConfig(seed=1, pin_swap_groups=(group,))
    engine = OptimizeEngine(ir, config)
    rng = random.Random(2)
    move = MOVE_GENERATORS[MoveKind.PIN_SWAP](engine, rng, 5.0)
    assert move is not None
    assert set(move.pin_pairs) == {(pin_left, pin_right)} or set(move.pin_pairs) == {
        (pin_right, pin_left)
    }

    before_net_left = int(ir.pin_net[pin_left])
    before_net_right = int(ir.pin_net[pin_right])
    engine.apply_move(move)
    assert int(ir.pin_net[pin_left]) == before_net_right
    assert int(ir.pin_net[pin_right]) == before_net_left
    assert not ir.dirty_l1.any()
    assert not ir.dirty_l3.any()
    assert ir.dirty_l2.any()
    assert ir.dirty_l5.any()

    engine.undo_move(move)
    assert int(ir.pin_net[pin_left]) == before_net_left
    assert int(ir.pin_net[pin_right]) == before_net_right


def test_pin_swap_respects_exclusions():
    ir, group, pin_left, pin_right = _pin_swap_ir_and_group()
    excluded_group = dataclasses.replace(group, excluded=frozenset({pin_right}))
    pairs = propose_reassignment(ir, excluded_group)
    assert pairs is None  # only one non-excluded pin left -- nothing to match


def test_pin_swap_never_proposed_with_empty_config():
    ir, _group, _pl, _pr = _pin_swap_ir_and_group()
    engine = OptimizeEngine(ir, OptimizeConfig(seed=3))  # default pin_swap_groups=()
    rng = random.Random(4)
    for _ in range(20):
        assert MOVE_GENERATORS[MoveKind.PIN_SWAP](engine, rng, 5.0) is None


def test_pin_swap_delta_correctness_over_random_moves():
    ir, group, _pl, _pr = _pin_swap_ir_and_group()
    cost_config = CostConfig()
    config = OptimizeConfig(seed=5, cost=cost_config, pin_swap_groups=(group,))
    engine = OptimizeEngine(ir, config)
    rng = random.Random(6)
    for trial in range(20):
        move = MOVE_GENERATORS[MoveKind.PIN_SWAP](engine, rng, 5.0)
        if move is None:
            continue
        engine.apply_move(move)
        if rng.random() < 0.5:
            engine.undo_move(move)
        full = evaluate_cost(ir, Level.L4, cost_config)
        assert engine.total() == pytest.approx(full.total, rel=1e-9, abs=1e-9), trial


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
