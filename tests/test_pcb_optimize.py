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

Also covers rigid groups/patterns end to end against the REAL
``nano_oc_switch.json`` fixture, built the same way ``pcb_place.py``'s
job does (:func:`precis.pcb.session.build_ir` + its real outline/holes) —
see :func:`test_real_pipeline_shape_nano_fixture_ends_fully_legal_and_
congruent`'s own docstring for the round-3-review defect this exists to
pin down (four real bugs a synthetic-graph unit test could not see).
"""

from __future__ import annotations

import dataclasses
import json
import math
import random
from pathlib import Path

import pytest

from precis.pcb import DEFAULT_STACKUP
from precis.pcb import session as pcb_session
from precis.pcb.cost import COURTYARD_MIN_SEPARATION_MM, CostConfig, evaluate_cost
from precis.pcb.geom import convex_polygons_overlap, point_in_polygon
from precis.pcb.ir import (
    COURTYARD_CLEARANCE_MM,
    Level,
    MountingHole,
    courtyard_bound_radius_mm,
    from_graph,
    instance_courtyard_polygons,
    plane_layers_of,
)
from precis.pcb.landpattern import place_points, rotate_offset
from precis.pcb.optimize import (
    MOVE_GENERATORS,
    Move,
    MoveKind,
    OptimizeConfig,
    OptimizeEngine,
    _hole_keepout_radius_mm,
    _hole_polygon,
    digest_toon,
    optimize,
    recentre_in_outline,
    resolve_measures,
    seed_placement,
)
from precis.pcb.pinswap import PinSwapGroup, propose_reassignment, total_group_crossings

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pcb"

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


def _seeded_ir(
    n: int = 12,
    *,
    graph_seed: int = 0,
    seed_rng_seed: int = 0,
    fixed=None,
    outline: bool = False,
):
    """``outline=True`` authors a generous board profile before seeding.

    Needed by any test that exercises PLANE_PROMOTE: a plane is poured
    INTO the profile, so `_gen_plane_promote` declines outright on a
    board that has none (promoting there produces a net `realize.py` can
    only report as ``failed: unpourable_plane``). Off by default so every
    other test keeps the outline-less seeding it was written against."""
    graph = _board(n, seed=graph_seed, fixed=fixed)
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    if outline:
        ir.outline = [
            (-200.0, -200.0),
            (200.0, -200.0),
            (200.0, 200.0),
            (-200.0, 200.0),
        ]
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
    ir = _seeded_ir(10, graph_seed=39, seed_rng_seed=40, outline=True)
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
    ir = _seeded_ir(10, graph_seed=42, seed_rng_seed=43, outline=True)
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
    ir = _seeded_ir(10, graph_seed=45, seed_rng_seed=46, outline=True)
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
    ir = _seeded_ir(10, graph_seed=48, seed_rng_seed=49, outline=True)
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


def test_recentre_in_outline_centers_the_finished_pack():
    """Regression for the visual-review finding: a board authored
    somewhat larger than the parts' own footprint must not deliver the
    pack hugging the outline's min corner. The centring is a POST-anneal
    rigid translation (:func:`recentre_in_outline`) — the seed itself
    stays deliberately corner-anchored (see its docstring: a centred seed
    flipped two reference-fixture seeds to ``no_path``)."""
    from precis.pcb.ir import (
        COURTYARD_CLEARANCE_MM,
        courtyard_bound_radius_mm,
        instance_courtyard_polygons,
    )
    from precis.pcb.optimize import COURTYARD_MIN_SEPARATION_MM

    graph = _board(10, seed=50)
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    seed_placement(ir, random.Random(51))  # no outline: origin-anchored
    # The SAME part-edge pack bbox recentre_in_outline itself measures.
    radii = courtyard_bound_radius_mm(
        instance_courtyard_polygons(
            ir,
            clearance_mm=COURTYARD_CLEARANCE_MM,
            fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
        )
    )
    n = ir.n_instances
    pack_x0 = min(float(ir.inst_x[i]) - float(radii[i]) for i in range(n))
    pack_x1 = max(float(ir.inst_x[i]) + float(radii[i]) for i in range(n))
    pack_y0 = min(float(ir.inst_y[i]) - float(radii[i]) for i in range(n))
    pack_y1 = max(float(ir.inst_y[i]) + float(radii[i]) for i in range(n))
    w, h = pack_x1 - pack_x0, pack_y1 - pack_y0
    # A COMMENSURATE outline: 1.4x the pack per axis (area ratio 1.96,
    # under _RECENTRE_MAX_AREA_RATIO), deliberately offset so centring
    # must move the pack; the offset stays inside the slack so the clamp
    # cannot zero the shift.
    slack_x, slack_y = 0.2 * w, 0.2 * h
    off_x, off_y = slack_x * 0.5, -slack_y * 0.5
    ir.outline = [
        (pack_x0 - slack_x + off_x, pack_y0 - slack_y + off_y),
        (pack_x1 + slack_x + off_x, pack_y0 - slack_y + off_y),
        (pack_x1 + slack_x + off_x, pack_y1 + slack_y + off_y),
        (pack_x0 - slack_x + off_x, pack_y1 + slack_y + off_y),
    ]
    dx, dy = recentre_in_outline(ir)
    assert (dx, dy) != (0.0, 0.0)
    # The applied shift is exactly the offset (clamp inactive by
    # construction), so the pack's edge-extent centre now coincides with
    # the outline centre.
    assert dx == pytest.approx(off_x, abs=1e-6)
    assert dy == pytest.approx(off_y, abs=1e-6)


def test_recentre_in_outline_skips_a_placeholder_canvas():
    """An outline an order of magnitude larger than the design (the
    esp32c3 fixture ships a 300x300 canvas for a ~35mm pack) is not a
    centring target: the shift would be large enough to perturb routing
    through the absolute-coordinate board-edge interactions, for zero
    layout meaning. The area-ratio gate refuses it."""
    graph = _board(10, seed=50)
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    seed_placement(ir, random.Random(51))
    ir.outline = [
        (-500.0, -500.0),
        (500.0, -500.0),
        (500.0, 500.0),
        (-500.0, 500.0),
    ]
    before = [(float(x), float(y)) for x, y in zip(ir.inst_x, ir.inst_y, strict=True)]
    assert recentre_in_outline(ir) == (0.0, 0.0)
    after = [(float(x), float(y)) for x, y in zip(ir.inst_x, ir.inst_y, strict=True)]
    assert before == after


def test_recentre_in_outline_refuses_when_any_instance_is_locked():
    """A rigid translation that moved a ``fixed_xy`` part would violate
    the lock, and translating everyone else would tear the placement --
    any lock means no shift at all."""
    graph = _board(6, seed=54, fixed={0: "xy"})
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.outline = [
        (-500.0, -500.0),
        (500.0, -500.0),
        (500.0, 500.0),
        (-500.0, 500.0),
    ]
    seed_placement(ir, random.Random(55))
    before = [(float(x), float(y)) for x, y in zip(ir.inst_x, ir.inst_y, strict=True)]
    assert recentre_in_outline(ir) == (0.0, 0.0)
    after = [(float(x), float(y)) for x, y in zip(ir.inst_x, ir.inst_y, strict=True)]
    assert before == after


def test_derive_placement_bounds_stays_seed_capped_inside_a_huge_outline():
    """The domain is the seed extent padded by ``board_side`` even when a
    much larger outline exists — deliberately NOT the full outline bbox.
    Measured 2026-08-31 (esp32c3 reference, 300x300 placeholder canvas):
    a full-outline domain let two seeds sprawl parts until a net came
    back ``no_path``. The outline caps the domain; the seed's own
    centred, padded extent is what keeps the working area compact and
    routable (see :meth:`OptimizeEngine._derive_placement_bounds`)."""
    ir = _seeded_ir(6, graph_seed=52, seed_rng_seed=53)  # tiny blob, no outline yet
    ir.outline = [
        (-300.0, -300.0),
        (300.0, -300.0),
        (300.0, 300.0),
        (-300.0, 300.0),
    ]
    engine = OptimizeEngine(ir, OptimizeConfig(seed=53))
    bx0, by0, bx1, by1 = engine._placement_bounds
    # Strictly inside the inset outline on every side (the cap, not the
    # canvas)...
    assert bx0 > -299.5 and by0 > -299.5 and bx1 < 299.5 and by1 < 299.5
    # ...and exactly the placed extent padded by board_side.
    import numpy as np

    placed = np.isfinite(ir.inst_x) & np.isfinite(ir.inst_y)
    pad = engine.board_side
    assert bx0 == pytest.approx(float(ir.inst_x[placed].min()) - pad)
    assert by0 == pytest.approx(float(ir.inst_y[placed].min()) - pad)
    assert bx1 == pytest.approx(float(ir.inst_x[placed].max()) + pad)
    assert by1 == pytest.approx(float(ir.inst_y[placed].max()) + pad)


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


# ── author-supplied placement measures drive the PRODUCTION anneal ──────
# (previously measured read-only by eyes.py / enforced only by the legacy
# `place.autoplace` quick placer — see this module's `MeasureSpec`/
# `resolve_measures`/`_measure_pair_usd` docstrings for the design).


def _isolated_pair_graph() -> dict:
    """Two instances sharing NO net at all — proximity/separation steering
    has to come entirely from the measure itself, never from an
    incidental wirelength pull (cost.py's module docstring: "no
    wirelength term") or from `alignment`/`courtyard_overlap` (both gated
    to a small nearby-instance neighbourhood, which the seeded starting
    gap below starts well outside of)."""
    return {"instances": [{"refdes": "A"}, {"refdes": "B"}], "nets": []}


def _seeded_pair(sep_mm: float):
    ir = from_graph(_isolated_pair_graph(), stackup=DEFAULT_STACKUP)
    ir.inst_x[0], ir.inst_y[0] = 0.0, 0.0
    ir.inst_x[1], ir.inst_y[1] = sep_mm, 0.0
    return ir


def test_soft_proximity_measure_pulls_two_unconnected_instances_together():
    """The exact "crystal hugs the MCU" idiom (precis-measures-help):
    a `soft` `proximity` measure between A and B, seeded 30mm apart with
    no shared net, ends the anneal within a couple mm of the authored
    8mm goal — while the SAME seed run with no measure at all (nothing
    else on this 2-instance, 0-net board has any positional preference)
    ends measurably farther apart. Values/seed pinned by direct
    measurement (see the git history for this test) — a bare 2-instance
    SA run is inherently noisy, so this asserts a comfortable margin
    rather than exact convergence."""
    goal_mm = 8.0
    measures = resolve_measures(
        [
            {
                "metric": "proximity",
                "operands": [{"instance": "A"}, {"instance": "B"}],
                "goal": goal_mm,
                "strength": "soft",
                "reason": "crystal hugs the MCU",
            }
        ]
    )

    ir_with = _seeded_pair(30.0)
    res_with = optimize(ir_with, OptimizeConfig(seed=1, iters=6000, measures=measures))
    ax, ay, _ = res_with.positions["A"]
    bx, by, _ = res_with.positions["B"]
    dist_with = math.hypot(ax - bx, ay - by)

    ir_without = _seeded_pair(30.0)
    res_without = optimize(ir_without, OptimizeConfig(seed=1, iters=6000))
    ax2, ay2, _ = res_without.positions["A"]
    bx2, by2, _ = res_without.positions["B"]
    dist_without = math.hypot(ax2 - bx2, ay2 - by2)

    assert dist_with <= goal_mm + 2.0  # within ~goal of each other
    assert dist_with < dist_without - 3.0  # measurably closer than unmeasured


def test_hard_separation_measure_pushes_a_pair_apart_past_its_goal():
    """A `hard` `separation` measure between two instances seeded almost
    touching (2mm apart) reliably ends the anneal past the authored 20mm
    goal — the mirror direction of the proximity test above, and `hard`
    (not `soft`) so the push is decisive rather than merely a nudge."""
    goal_mm = 20.0
    measures = resolve_measures(
        [
            {
                "metric": "separation",
                "operands": [{"instance": "A"}, {"instance": "B"}],
                "goal": goal_mm,
                "strength": "hard",
                "reason": "keep the noisy regulator off the sensitive part",
            }
        ]
    )
    ir = _seeded_pair(2.0)
    result = optimize(ir, OptimizeConfig(seed=2, iters=8000, measures=measures))
    ax, ay, _ = result.positions["A"]
    bx, by, _ = result.positions["B"]
    assert math.hypot(ax - bx, ay - by) > goal_mm


def test_no_measures_default_path_is_bit_identical_to_before_this_feature():
    """The measures machinery must be a pure ADDITION: with
    ``config.measures`` at its default ``()``, `_money_measures` never
    leaves 0.0 (the per-move `_refresh_measures` early-returns for every
    instance — `_measures_by_inst` is empty) and `money()`/`total()`
    collapse to EXACTLY the pre-existing formula (the same
    `_money_static_by_name` sum + `_money_board_area`, with no extra
    addend) — an unrelated design's anneal takes no new code path at all,
    not merely "a code path that happens to compute zero". Also pins
    positional determinism (same shape as `test_determinism_same_seed_
    same_result`) so a future change to the measures machinery that
    somehow perturbed the no-measures case would fail here, not silently
    in production."""
    graph = _board(10, seed=50)
    config = OptimizeConfig(seed=51, iters=400)

    ir0 = from_graph(graph, stackup=DEFAULT_STACKUP)
    seed_placement(ir0, random.Random(config.seed))
    engine = OptimizeEngine(ir0, config)
    assert engine._money_measures == 0.0
    assert engine.money() == pytest.approx(
        sum(engine._money_static_by_name.values()) + engine._money_board_area
    )
    engine.anneal(random.Random(config.seed))
    assert engine._money_measures == 0.0
    assert engine.money() == pytest.approx(
        sum(engine._money_static_by_name.values()) + engine._money_board_area
    )

    ir1 = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir2 = from_graph(graph, stackup=DEFAULT_STACKUP)
    r1 = optimize(ir1, config)
    r2 = optimize(ir2, config)
    assert r1.positions == r2.positions


# ── ROTATE must respect polygon legality (it could ignore a circle) ──────
def test_rotate_is_refused_when_it_would_swing_a_part_into_its_neighbour():
    """**A rotation can make a placement illegal, and could not before
    2026-08-30.** While the keep-out was a CIRCLE this move was correctly
    left ungated: a disc's reserved area is rotation-invariant, so
    spinning a part could never bring it into a neighbour. A courtyard
    POLYGON is not — swing an oblong part's long axis toward the part
    beside it and the two overlap.

    Nothing downstream would have caught it: no cost term reads
    ``inst_rot``, so ``delta`` is exactly 0.0 and ``anneal`` accepts every
    generated ROTATE unconditionally. The violation would have surfaced
    only in a later DRC run, as a ``courtyard_overlap`` the annealer had
    no way to reject.

    Built by hand rather than sampled: two long thin parts side by side
    across their SHORT axis, so the placement is legal as seeded and
    illegal at 90 degrees. A random fixture almost never lands there."""
    ir = _seeded_ir(2, graph_seed=70, seed_rng_seed=71)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=72))
    # A 6 x 0.6mm bar for both instances, in the local frame.
    bar = [(-3.0, -0.3), (3.0, -0.3), (3.0, 0.3), (-3.0, 0.3), (-3.0, -0.3)]
    engine._keepout_poly = [list(bar) for _ in range(ir.n_instances)]
    engine._keepout_r = courtyard_bound_radius_mm(engine._keepout_poly)
    engine._world_poly.clear()
    # The domain is derived from the SEED's own extent; these hand-placed
    # coordinates are not in it, and `_placement_is_legal` checks bounds
    # first. Widen it so this test reads the courtyard gate and nothing
    # else.
    engine._placement_bounds = (-100.0, -100.0, 100.0, 100.0)
    # Stacked across the short axis, 1mm apart: clear at 0 degrees (the
    # bars are 0.6 wide), overlapping the moment either turns 90.
    ir.move_instance(0, x=0.0, y=0.0, rot=0.0)
    ir.move_instance(1, x=0.0, y=1.0, rot=0.0)
    assert engine._placement_is_legal(((0, 0.0, 0.0),))
    assert not engine._placement_is_legal(((0, 0.0, 0.0),), rotations={0: 90.0})

    # And the generator itself refuses, rather than handing the annealer a
    # move it will accept unconditionally.
    engine._movable_rot = [0]
    for attempt in range(20):
        assert MOVE_GENERATORS[MoveKind.ROTATE](
            engine, random.Random(attempt), 5.0
        ) is (None)


def test_rotate_still_fires_when_there_is_room_to_turn():
    """The guard must not become "never rotates" — that would silently
    delete a whole move kind from the search while every test still
    passed."""
    ir = _seeded_ir(2, graph_seed=73, seed_rng_seed=74)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=75))
    bar = [(-3.0, -0.3), (3.0, -0.3), (3.0, 0.3), (-3.0, 0.3), (-3.0, -0.3)]
    engine._keepout_poly = [list(bar) for _ in range(ir.n_instances)]
    engine._keepout_r = courtyard_bound_radius_mm(engine._keepout_poly)
    engine._world_poly.clear()
    # The domain is derived from the SEED's own extent; these hand-placed
    # coordinates are not in it, and `_placement_is_legal` checks bounds
    # first. Widen it so this test reads the courtyard gate and nothing
    # else.
    engine._placement_bounds = (-100.0, -100.0, 100.0, 100.0)
    ir.move_instance(0, x=0.0, y=0.0, rot=0.0)
    ir.move_instance(1, x=0.0, y=30.0, rot=0.0)  # far enough that any angle clears
    engine._movable_rot = [0]
    moves = [
        MOVE_GENERATORS[MoveKind.ROTATE](engine, random.Random(a), 5.0)
        for a in range(5)
    ]
    assert any(m is not None for m in moves)


def test_plane_promotion_is_not_offered_on_a_board_with_no_outline():
    """A plane is poured into the board PROFILE, so on a design with no
    outline feature there is nowhere for it to land: ``realize.py`` comes
    back ``failed: unpourable_plane`` for every connection on the promoted
    net, and no amount of routing effort changes that.

    Offering the move anyway let the anneal accept it — PLANE_PROMOTE is
    priced, but a cheap-enough promotion still wins — silently converting
    a perfectly routable 2-pin net into a permanently failed one. Found
    2026-08-30 on a 3-part fixture that had always routed, when a
    placement change shifted the cost landscape enough to make the
    promotion attractive; latent since PLANE_PROMOTE existed."""
    ir = _seeded_ir(6, graph_seed=80, seed_rng_seed=81)
    assert not ir.outline  # the fixture builder authors none
    engine = OptimizeEngine(ir, OptimizeConfig(seed=82))
    assert engine._plane_layers  # the stackup DOES offer pourable layers
    for attempt in range(30):
        assert (
            MOVE_GENERATORS[MoveKind.PLANE_PROMOTE](engine, random.Random(attempt), 5.0)
            is None
        )


# ── rigid groups + mounting-hole keepouts (round-3 review item 4 /
# nano_oc_switch's daughterboard + 4-channel board) ──────────────────────


def _group_graph():
    """A 6-instance chain (``_board``) with the first two turned into an
    AUTHORED rigid group at the nano fixture's own header pitch (0.6",
    15.24mm) — J1's offset is the group's own anchor (0, 0), J2 sits
    15.24mm to its right, mirroring ``tests/fixtures/pcb/
    nano_oc_switch.json``'s J1/J2. J1 is also rotation-locked, so the pair
    can TRANSLATE together but never ROTATE — the one thing that would
    make "the relative offset is unchanged forever" a statistical claim
    rather than a structural one."""
    graph = _board(6, seed=50)
    graph["instances"][0] = {
        **graph["instances"][0],
        "group": "nano_hdr",
        "group_offset": {"x": 0.0, "y": 0.0, "rot": 0.0},
        "fixed": "rot",
    }
    graph["instances"][1] = {
        **graph["instances"][1],
        "group": "nano_hdr",
        "group_offset": {"x": 15.24, "y": 0.0, "rot": 0.0},
    }
    return graph


def _channel_pattern_graph():
    """Four congruent 2-instance "channel" tiles (a connector-like part +
    a passive, netted together so TRANSLATE/ROTATE/SWAP all have a real
    reason to move them) plus one ungrouped instance — the pattern half
    of the nano fixture's four identical driver channels, at unit-test
    scale."""
    instances = []
    nets = []
    for k in range(4):
        instances.append(
            {"refdes": f"J{k}", "pattern": "channel", "pattern_instance": k}
        )
        instances.append(
            {"refdes": f"R{k}", "pattern": "channel", "pattern_instance": k}
        )
        nets.append(
            {
                "name": f"OUT{k}",
                "members": [
                    {"refdes": f"J{k}", "pin": "1"},
                    {"refdes": f"R{k}", "pin": "1"},
                ],
            }
        )
    instances.append({"refdes": "U0"})
    return {"instances": instances, "nets": nets}


#: Same generous board profile ``_seeded_ir(..., outline=True)`` already
#: authors for its own PLANE_PROMOTE tests, reused here for a different
#: reason: without an outline, ``OptimizeEngine``'s no-outline placement
#: domain is capped at ``board_side`` on every axis regardless of where
#: the seed actually landed (``_derive_placement_bounds``'s ``ox1 =
#: self.board_side`` default) — fine for the small, tightly-packed boards
#: every OTHER fixture in this file seeds, but a rigid group/pattern's
#: bigger combined footprint can legitimately spread the pack past that
#: cap, which would then make every TRANSLATE/SWAP candidate illegal by
#: construction (nowhere legal to land) rather than exercising the
#: group-aware code this suite is actually testing.
_WIDE_OUTLINE = [(-200.0, -200.0), (200.0, -200.0), (200.0, 200.0), (-200.0, 200.0)]


def _group_ir(seed_rng_seed: int):
    ir = from_graph(_group_graph(), stackup=DEFAULT_STACKUP)
    ir.outline = _WIDE_OUTLINE
    seed_placement(ir, random.Random(seed_rng_seed))
    return ir


def _pattern_ir(seed_rng_seed: int):
    ir = from_graph(_channel_pattern_graph(), stackup=DEFAULT_STACKUP)
    ir.outline = _WIDE_OUTLINE
    seed_placement(ir, random.Random(seed_rng_seed))
    return ir


def test_seed_honors_authored_group_offset_exactly():
    ir = _group_ir(1)
    gid = int(ir.inst_group[0])
    assert gid >= 0 and gid == int(ir.inst_group[1])
    dx = float(ir.inst_x[1]) - float(ir.inst_x[0])
    dy = float(ir.inst_y[1]) - float(ir.inst_y[0])
    assert math.isclose(dx, 15.24, abs_tol=1e-9)
    assert math.isclose(dy, 0.0, abs_tol=1e-9)


def test_short_anneal_preserves_authored_group_offset():
    """J1/J2's 15.24mm relative offset must survive every ACCEPTED move
    the anneal makes -- a TRANSLATE moves the whole rigid pair by one
    shared delta (offset untouched by construction), and ROTATE never
    reaches this group at all (J1 is rotation-locked, see
    :func:`_group_graph`), so the relative offset is not merely
    approximately preserved on average, it is unchanged after every
    single accepted move."""
    ir = _group_ir(1)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=1, iters=400))
    engine.anneal(random.Random(2))
    dx = float(ir.inst_x[1]) - float(ir.inst_x[0])
    dy = float(ir.inst_y[1]) - float(ir.inst_y[0])
    assert math.isclose(dx, 15.24, abs_tol=1e-6)
    assert math.isclose(dy, 0.0, abs_tol=1e-6)


def test_swap_never_touches_an_authored_group_with_no_pattern():
    """A plain authored group (no ``"pattern"``) may never trade anchors
    with anything -- not another ungrouped instance, not itself, nothing
    -- since it names no congruent counterpart to trade with (the SWAP
    docstring note on :class:`~precis.pcb.optimize.Move`)."""
    ir = _group_ir(3)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=3))
    rng = random.Random(4)
    saw_any_swap = False
    for _ in range(300):
        move = MOVE_GENERATORS[MoveKind.SWAP](engine, rng, 5.0)
        if move is None:
            continue
        saw_any_swap = True
        assert 0 not in move.instances and 1 not in move.instances
    assert saw_any_swap  # the other 4 ungrouped instances swap freely


def _isolated_group_graph():
    """A single 2-member AUTHORED group with nothing else on the board —
    isolates TRANSLATE/ROTATE's rigid-body MECHANICS (the thing this test
    checks) from :meth:`OptimizeEngine._placement_is_legal`'s crowding
    behaviour, which is a separate, already-covered question (a fresh
    shelf pack has every tile touching its neighbours at exactly their
    combined keep-out — see :func:`seed_placement`'s own "legal by
    construction" docstring — so a busier board makes a group's own
    legal-proposal odds a matter of local room, not of whether the rigid-
    body math itself is right)."""
    return {
        "instances": [
            {
                "refdes": "A",
                "group": "g",
                "group_offset": {"x": 0.0, "y": 0.0, "rot": 0.0},
            },
            {
                "refdes": "B",
                "group": "g",
                "group_offset": {"x": 5.0, "y": 0.0, "rot": 0.0},
            },
        ],
        "nets": [],
    }


def test_translate_and_rotate_move_a_pattern_tile_as_one_rigid_body():
    ir = from_graph(_isolated_group_graph(), stackup=DEFAULT_STACKUP)
    seed_placement(ir, random.Random(5))
    engine = OptimizeEngine(ir, OptimizeConfig(seed=5))
    gid = int(ir.inst_group[0])
    members = engine._group_members[gid]
    assert members == (0, 1)

    rng = random.Random(6)
    move = None
    for _ in range(50):
        candidate = MOVE_GENERATORS[MoveKind.TRANSLATE](engine, rng, 5.0)
        if candidate is not None:
            move = candidate
            break
    assert move is not None
    assert move.instances == members
    dxs = {round(n[0] - o[0], 9) for o, n in zip(move.old, move.new)}
    dys = {round(n[1] - o[1], 9) for o, n in zip(move.old, move.new)}
    assert len(dxs) == 1 and len(dys) == 1  # every member shares ONE delta

    rng = random.Random(7)
    rmove = None
    for _ in range(50):
        candidate = MOVE_GENERATORS[MoveKind.ROTATE](engine, rng, 5.0)
        if candidate is not None:
            rmove = candidate
            break
    assert rmove is not None
    assert rmove.instances == members
    for (ox, oy, orot), (nx, ny, nrot) in zip(rmove.old, rmove.new):
        assert (nrot - orot) % 360.0 in (90.0, 270.0)
    # B's offset from A must still be 5mm, just rotated by the SAME step
    # every member's own rotation advanced by -- the point of a rigid
    # body: its own internal geometry never changes shape, only pose.
    (ax, ay, _arot), (bx, by, _brot) = rmove.new
    assert math.isclose(math.hypot(bx - ax, by - ay), 5.0, abs_tol=1e-9)


def test_pattern_tiles_share_internal_layout_after_a_short_anneal():
    """Every "channel" tile's members must sit at IDENTICAL offsets from
    their own tile centroid, up to whatever rigid rotation that tile has
    individually accumulated -- feature 3's whole point ("identical tiles
    by construction"). Checked twice: right after seeding (pins the
    tiling STAMP itself down) and again after a short anneal (pins that
    TRANSLATE/ROTATE/SWAP keep it true, not just the initial stamp)."""
    ir = _pattern_ir(7)

    tiles_by_idx: dict[int, list[int]] = {}
    for i in range(ir.n_instances):
        gid = int(ir.inst_group[i])
        if gid < 0:
            continue
        idx = int(ir.group_pattern_index[gid])
        tiles_by_idx.setdefault(idx, []).append(i)
    tiles = [sorted(tiles_by_idx[k]) for k in sorted(tiles_by_idx)]
    assert len(tiles) == 4

    def _offsets(members: list[int]) -> list[tuple[float, float]]:
        cx = sum(float(ir.inst_x[m]) for m in members) / len(members)
        cy = sum(float(ir.inst_y[m]) for m in members) / len(members)
        return [(float(ir.inst_x[m]) - cx, float(ir.inst_y[m]) - cy) for m in members]

    stamped_offsets = [_offsets(t) for t in tiles]
    stamped_rot0 = [float(ir.inst_rot[t[0]]) for t in tiles]
    # Every tile's members already match the leader's layout, right after
    # the stamp -- and every tile was stamped with the SAME absolute
    # rotation the leader had (never merely the same RELATIVE one), which
    # is what makes `stamped_rot0` a shared baseline below.
    for offs in stamped_offsets[1:]:
        for (ox, oy), (lx, ly) in zip(offs, stamped_offsets[0]):
            assert math.isclose(ox, lx, abs_tol=1e-6)
            assert math.isclose(oy, ly, abs_tol=1e-6)
    assert len(set(stamped_rot0)) == 1

    engine = OptimizeEngine(ir, OptimizeConfig(seed=7, iters=400))
    engine.anneal(random.Random(8))

    for k, members in enumerate(tiles):
        current = _offsets(members)
        # This tile's own accumulated rotation since the stamp -- every
        # member of ONE group always advances by the SAME delta in one
        # move (rigid-body ROTATE), so any member's own rotation drift
        # names the whole tile's.
        delta = (float(ir.inst_rot[members[0]]) - stamped_rot0[k]) % 360.0
        for (cx_, cy_), (sx, sy) in zip(current, stamped_offsets[k]):
            ux, uy = rotate_offset(cx_, cy_, -delta)
            assert math.isclose(ux, sx, abs_tol=1e-6)
            assert math.isclose(uy, sy, abs_tol=1e-6)


def test_swap_between_congruent_pattern_tiles_trades_anchors():
    """A freshly-shelf-packed board has every tile touching its neighbours
    at exactly their combined keep-out (:func:`seed_placement`'s "legal
    by construction... adjacent centres are exactly r_i + r_j apart"), so
    trading two tiles' anchors outright almost always collides with a
    THIRD, untouched tile still sitting where the pack put it — not a
    group-swap defect, the identical reason a fresh seed rarely offers a
    legal plain SWAP either. A short anneal first (which already touches
    this graph's groups via TRANSLATE/ROTATE, per the OTHER tests in this
    module) gives the board the same real breathing room a longer run
    would, then a cross-tile SWAP is easy to find."""
    ir = _pattern_ir(9)
    engine = OptimizeEngine(ir, OptimizeConfig(seed=9, iters=300))
    engine.anneal(random.Random(10))

    rng = random.Random(12)
    found: Move | None = None
    for _ in range(300):
        move = MOVE_GENERATORS[MoveKind.SWAP](engine, rng, 5.0)
        if move is not None and len(move.instances) == 4:
            found = move
            break
    assert found is not None, "expected a cross-tile pattern swap within 300 draws"
    half = len(found.instances) // 2
    # Each member's NEW pose is exactly its swap partner's OLD pose --
    # a straight positional exchange, index-aligned by the same sorted-
    # instance-id correspondence the tiling stamp established.
    assert found.new[:half] == found.old[half:]
    assert found.new[half:] == found.old[:half]


# ── mounting-hole keepouts ────────────────────────────────────────────────


def test_courtyard_overlapping_a_mounting_hole_is_illegal():
    ir = from_graph(
        {"instances": [{"refdes": "U1"}], "nets": []},
        stackup=DEFAULT_STACKUP,
        mounting_holes=(
            MountingHole(x=10.0, y=10.0, drill_mm=5.6, ring_dia_mm=8.0, plated=True),
        ),
    )
    # A modest initial pose (NOT the eventual test point) -- this is what
    # `OptimizeEngine.__init__` derives its no-outline placement domain
    # from (`_derive_placement_bounds`'s seed-extent-plus-``board_side``
    # rule), so it has to be somewhere the domain can actually contain
    # both the on-hole and the far-away point checked below.
    ir.inst_x[0] = 5.0
    ir.inst_y[0] = 5.0
    engine = OptimizeEngine(ir, OptimizeConfig(seed=1))
    assert not engine._placement_is_legal(((0, 10.0, 10.0),))  # dead centre of the hole
    assert engine._placement_is_legal(((0, 18.0, 18.0),))  # well clear of it


def test_hole_keepout_radius_widens_for_an_authored_hardware_head():
    """The round-6 defect: a hole's copper annulus (Ø8mm) is not the
    board's actual keep-out — the physical screw head / solder-nut
    flange (``head_dia_mm``) sitting above the board can be wider, and
    that is what a part must clear. ``head_dia_mm=0.0`` (the default,
    absent-hardware case) must reproduce the pre-existing ring-only
    value exactly -- no regression for holes with no authored head."""
    ring_only = MountingHole(x=0.0, y=0.0, drill_mm=5.6, ring_dia_mm=8.0)
    assert _hole_keepout_radius_mm(ring_only) == 8.0 / 2.0 + COURTYARD_CLEARANCE_MM

    with_head = dataclasses.replace(ring_only, head_dia_mm=9.0)
    assert _hole_keepout_radius_mm(with_head) == 9.0 / 2.0 + COURTYARD_CLEARANCE_MM


def test_courtyard_overlapping_a_mounting_hole_head_envelope_is_illegal():
    """Same shape as ``test_courtyard_overlapping_a_mounting_hole_is_
    illegal`` but the illegal point sits OUTSIDE the Ø8mm copper ring's
    keep-out and INSIDE the Ø20mm screw-head envelope's only -- proving
    the head diameter, not just the ring, drives placement legality (the
    round-6 defect: a part parked legally under the copper-only rule but
    physically under the screw head)."""
    ir = from_graph(
        {"instances": [{"refdes": "U1"}], "nets": []},
        stackup=DEFAULT_STACKUP,
        mounting_holes=(
            MountingHole(
                x=10.0,
                y=10.0,
                drill_mm=5.6,
                ring_dia_mm=8.0,
                head_dia_mm=20.0,
                plated=True,
            ),
        ),
    )
    ir.inst_x[0] = 5.0
    ir.inst_y[0] = 5.0
    engine = OptimizeEngine(ir, OptimizeConfig(seed=1))
    # 8mm off-centre: clear of the ring-only keep-out (4.0 + clearance,
    # comfortably legal there) but well inside the head keep-out
    # (10.0 + clearance) -- illegal only because head_dia_mm now dominates.
    assert not engine._placement_is_legal(((0, 18.0, 10.0),))
    assert engine._placement_is_legal(((0, 18.5, 18.5),))  # well clear of it


def test_short_anneal_on_nano_like_fixture_clears_mounting_hole_overlap():
    """The user-observed defect: Q3 landing on the top-left M4 solder-nut
    hole, R1 almost fully under it, C1 clipping it. A seeded part is
    forced onto a hole (``seed_placement`` doesn't itself avoid holes --
    see that function's own docstring for why: legality + this graded
    pressure are the backstop, not the seed), then a short anneal must
    leave the WHOLE board -- every instance, against every neighbour AND
    every hole -- legal."""
    graph = _board(10, seed=60)
    holes = (
        MountingHole(x=6.0, y=6.0, drill_mm=5.6, ring_dia_mm=8.0, plated=True),
        MountingHole(x=56.0, y=6.0, drill_mm=5.6, ring_dia_mm=8.0, plated=True),
    )
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, mounting_holes=holes)
    ir.outline = _WIDE_OUTLINE
    seed_placement(ir, random.Random(61))
    ir.inst_x[0] = holes[0].x
    ir.inst_y[0] = holes[0].y

    engine = OptimizeEngine(ir, OptimizeConfig(seed=61, iters=1500))
    engine.anneal(random.Random(62))

    proposals = [
        (i, float(ir.inst_x[i]), float(ir.inst_y[i])) for i in range(ir.n_instances)
    ]
    assert engine._placement_is_legal(proposals)


# ── real-pipeline regression: the nano fixture's group/pattern machinery,
# through the SAME session.build_ir() entry pcb_place.py's job uses ──────


def _nano_fixture_graph() -> tuple[
    dict, list[list[float]] | None, tuple[MountingHole, ...]
]:
    """The nano fixture's ``components``/``nets``/``connections``/
    ``features`` create-op shape (as authored — see ``tests/fixtures/pcb/
    nano_oc_switch.json``), converted into the ``{"instances": [...],
    "nets": [...]}`` graph shape :func:`~precis.pcb.session.build_ir`
    consumes -- the SAME shape :meth:`~precis.store.Store.pcb_graph`
    hoists group/pattern fields onto (pinned by ``tests/test_pcb_handler.
    py::test_pcb_graph_round_trips_group_and_pattern_fields``), built
    here without a DB so this stays a fast, no-DB unit test. Every
    instance-level key ``from_graph`` reads (``label``, ``group``,
    ``group_offset``, ``pattern``, ``pattern_instance``) is carried
    through verbatim; ``x``/``y``/``rot``/``fixed``/``part_lcsc`` are
    simply absent (matching an unplaced design), and nets are rebuilt
    from ``connections`` grouped by net name."""
    path = _FIXTURE_DIR / "nano_oc_switch.json"
    with path.open(encoding="utf-8") as fh:
        design = json.load(fh)

    instances = []
    for c in design["components"]:
        inst: dict = {"refdes": c["refdes"], "label": c.get("label")}
        for key in ("group", "group_offset", "pattern", "pattern_instance"):
            if key in c:
                inst[key] = c[key]
        instances.append(inst)

    members_by_net: dict[str, list[dict[str, str]]] = {}
    for conn in design["connections"]:
        members_by_net.setdefault(conn["net"], []).append(
            {"refdes": conn["refdes"], "pin": conn["pin"]}
        )
    nets = [
        {"name": name, "members": members} for name, members in members_by_net.items()
    ]
    graph = {"instances": instances, "nets": nets}

    features = design.get("features") or []
    outline = pcb_session.outline_from_features(features)
    holes = pcb_session.mounting_holes_from_features(features)
    return graph, outline, holes


def test_real_pipeline_shape_nano_fixture_ends_fully_legal_and_congruent():
    """Regression test for a real, round-3-review defect (2026-09):
    wiring the create->graph DB path to persist+hoist ``group``/
    ``group_offset``/``pattern``/``pattern_instance`` (``tests/
    test_pcb_handler.py::test_pcb_graph_round_trips_group_and_pattern_
    fields``) activated this module's group/pattern parsing against a
    REAL design for the first time, and the real ``nano_oc_switch.json``
    render (4 identical "channel" pattern tiles + one authored 2-header
    "super footprint", a real 62x46mm rounded-corner outline, 4 real
    mounting holes) came back with FOUR real bugs none of the earlier,
    synthetic-graph tests in this module could see:

    1. Several pattern tiles seeded (and stayed FROZEN through the whole
       anneal) tens of millimetres off-board — root cause:
       ``_cluster_instances`` has no notion of "pattern" membership, so a
       tile's own netlist-connected members landed in unrelated
       clusters; :func:`~precis.pcb.optimize._merge_pattern_clusters` +
       :func:`~precis.pcb.optimize._compact_pattern_leaders` close it.
    2. J1/J2 rendered end-to-end (one long column) instead of two
       parallel rows — the FIXTURE authored the group_offset along the
       header's own pin-row axis instead of perpendicular to it (fixed
       in the fixture itself, not the engine).
    3. Parts still sitting ON a mounting hole — a downstream symptom of
       (1): a frozen tile can never respond to the graded hole-clearance
       pressure either.
    4. A rigid group whose members have DIFFERENT keep-out radii could
       find literally ZERO legal TRANSLATE/ROTATE proposals forever: the
       shared delta was clamped against only the PICKED member's own
       ``bounds_for``, which could still walk a BIGGER group-mate out of
       ITS OWN (tighter) domain --
       :func:`~precis.pcb.optimize._gen_translate` now clamps to the
       INTERSECTION of every member's domain, sampling UNIFORMLY across
       a corridor too wide for a normal step to explore
       (:func:`~precis.pcb.optimize._sample_delta_1d`) so a single
       reachable-but-blocked corner cannot freeze a tile forever.

    Built the SAME way ``pcb_place.py``'s own job builds it
    (:func:`~precis.pcb.session.build_ir` with the fixture's real
    outline/mounting holes, see :func:`_nano_fixture_graph`), then a
    REAL seed + anneal (not a hand-picked move sequence) — so any of
    the four defects above reappearing fails this test directly.
    """
    graph, outline, holes = _nano_fixture_graph()
    ir = pcb_session.build_ir(graph, outline=outline, mounting_holes=holes)
    assert ir.outline is not None and len(ir.outline) >= 3
    assert len(ir.mounting_holes) == 4
    # 4 "channel" pattern tiles + 1 authored "prog_hdr" group. (Round 7
    # briefly authored a rigid "mcu_xtal" group too — U1 + crystal circuit
    # as one unit — but a ~12x18mm rigid bloc strands outside the outline
    # at every tried seed: containment pressure can't walk a group that
    # big back in. Reverted; the crystal intent rides hard proximity
    # measures instead, and the group-containment defect is filed in the
    # round-7 backlog item.)
    assert ir.n_groups == 5

    # 6000, not the job default 2000: the round-6 redesigned fixture (nano
    # socket replaced by an on-board TQFP-32 MCU + programming headers, 22
    # -> 33 instances) converges slower — at THIS seed U1's large courtyard
    # seeds onto the (6,6) mounting hole's hardware-head keep-out and needs
    # ~5000+ iterations of graded hole pressure to walk off (measured:
    # still parked at 4000, clear at 6000; seed 2 is clear at 2000).
    optimize(ir, OptimizeConfig(iters=6000, seed=1))

    # (a) every instance's courtyard sits FULLY inside the true outline
    # polygon (not just its bounding box -- a rounded-corner outline has
    # slivers a bbox check would miss).
    local_polys = instance_courtyard_polygons(
        ir,
        clearance_mm=COURTYARD_CLEARANCE_MM,
        fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
    )
    world_polys = [
        place_points(
            local_polys[i],
            cx=float(ir.inst_x[i]),
            cy=float(ir.inst_y[i]),
            rot_deg=float(ir.inst_rot[i]),
        )
        for i in range(ir.n_instances)
    ]
    for i, poly in enumerate(world_polys):
        for pt in poly:
            assert point_in_polygon(pt, ir.outline), (
                f"{ir.instance_refdes[i]}'s courtyard reaches {pt}, "
                "outside the board outline"
            )

    # (b) no courtyard overlaps a mounting hole
    for i, poly in enumerate(world_polys):
        for hole in ir.mounting_holes:
            hole_poly = _hole_polygon(hole.x, hole.y, _hole_keepout_radius_mm(hole))
            assert not convex_polygons_overlap(poly, hole_poly), (
                f"{ir.instance_refdes[i]}'s courtyard overlaps the mounting "
                f"hole at ({hole.x}, {hole.y})"
            )

    # (c) J1/J2 (the ICSP + FTDI programming-header pair): still EXACTLY
    # the fixture's own authored group_offset (0, 5.08) once the
    # world-space delta is rotated back by J1's own rotation into J1's
    # frame -- not merely "5.08mm apart at SOME angle" -- and PARALLEL
    # (same rotation), never end-to-end. The offset is deliberately
    # PERPENDICULAR to a header's own pin row (which runs along local X,
    # `landpattern._single_row`) so two rows land side by side rather
    # than nose-to-tail — see the fixture's own ``group_offset`` and
    # this test's module docstring, defect 2.
    j1 = next(i for i in range(ir.n_instances) if str(ir.instance_refdes[i]) == "J1")
    j2 = next(i for i in range(ir.n_instances) if str(ir.instance_refdes[i]) == "J2")
    assert math.isclose(float(ir.inst_rot[j1]), float(ir.inst_rot[j2]), abs_tol=1e-6)
    world_dx = float(ir.inst_x[j2]) - float(ir.inst_x[j1])
    world_dy = float(ir.inst_y[j2]) - float(ir.inst_y[j1])
    local_dx, local_dy = rotate_offset(world_dx, world_dy, -float(ir.inst_rot[j1]))
    assert math.isclose(local_dx, 0.0, abs_tol=1e-6)
    assert math.isclose(local_dy, 5.08, abs_tol=1e-6)

    # (d) every "channel" pattern tile has an IDENTICAL internal layout
    # (member offsets from its own centroid, up to the tile's own
    # rotation) -- the same congruence check
    # `test_pattern_tiles_share_internal_layout_after_a_short_anneal`
    # pins on a synthetic graph, repeated here against the real fixture.
    tiles_by_idx: dict[int, list[int]] = {}
    for i in range(ir.n_instances):
        gid = int(ir.inst_group[i])
        if gid >= 0 and ir.group_pattern[gid] == "channel":
            idx = int(ir.group_pattern_index[gid])
            tiles_by_idx.setdefault(idx, []).append(i)
    tiles = [sorted(tiles_by_idx[k]) for k in sorted(tiles_by_idx)]
    assert len(tiles) == 4
    assert all(len(t) == 4 for t in tiles)

    def _offsets(members: list[int]) -> list[tuple[float, float]]:
        cx = sum(float(ir.inst_x[m]) for m in members) / len(members)
        cy = sum(float(ir.inst_y[m]) for m in members) / len(members)
        return [(float(ir.inst_x[m]) - cx, float(ir.inst_y[m]) - cy) for m in members]

    leader_offsets = _offsets(tiles[0])
    leader_rot0 = float(ir.inst_rot[tiles[0][0]])
    for tile in tiles[1:]:
        offs = _offsets(tile)
        delta = (float(ir.inst_rot[tile[0]]) - leader_rot0) % 360.0
        for (tx, ty), (lx, ly) in zip(offs, leader_offsets):
            ux, uy = rotate_offset(tx, ty, -delta)
            assert math.isclose(ux, lx, abs_tol=1e-6)
            assert math.isclose(uy, ly, abs_tol=1e-6)
