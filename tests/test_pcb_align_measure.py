"""The `align` pcb measure — residual maths, the eyes verdict, and the
placement paths (production anneal + snap in ``optimize.py``, quick placer in
``place.py``), plus the store/handler round trip of its ``meta``.

An `align` row: two operands (``instance`` | ``feature_id`` | ``point``, the
proximity shape), ``meta = {"axis": "x"|"y"|"xy", "offset": [dx, dy]}``,
``goal`` = a mm tolerance (default 0.05). Residual
``r = pos_2 - pos_1 - offset`` over the constrained axes; value = |r|; ok iff
value <= goal. Swapping the operands negates the offset.
"""

from __future__ import annotations

import dataclasses
import math
import random

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import DEFAULT_STACKUP, eyes, place, session
from precis.pcb.cost import evaluate_cost
from precis.pcb.ir import Level, from_graph
from precis.pcb.optimize import (
    MOVE_GENERATORS,
    MoveKind,
    OptimizeConfig,
    OptimizeEngine,
    _measure_pair_usd,
    optimize,
    resolve_measures,
)
from tests.test_pcb_optimize import _seeded_ir

_MM = pytest.approx


def _align(
    second: dict,
    *,
    goal=0.05,
    strength="soft",
    weight=None,
    first: dict | str = "A",
    axis: str | None = None,
    offset: tuple[float, float] | None = None,
    measure_id: int | None = None,
):
    meta: dict = {}
    if axis is not None:
        meta["axis"] = axis
    if offset is not None:
        meta["offset"] = list(offset)
    row: dict = {
        "metric": "align",
        "operands": [{"instance": first} if isinstance(first, str) else first, second],
        "goal": goal,
        "strength": strength,
        "reason": "test",
        "meta": meta,
    }
    if weight is not None:
        row["weight"] = weight
    if measure_id is not None:
        row["measure_id"] = measure_id
    return row


def _graph(**xy: tuple[float, float] | None) -> dict:
    return {
        "instances": [
            {
                "refdes": r,
                "x": None if p is None else p[0],
                "y": None if p is None else p[1],
                "roles": [],
            }
            for r, p in xy.items()
        ],
        "nets": [],
        "unconnected": [],
    }


def _inst(refdes: str) -> dict:
    return {"instance": refdes}


# ── residual + verdict (eyes) ───────────────────────────────────────────
def test_residual_axis_xy_x_y_without_offset():
    pa, pb = (1.0, 2.0), (4.0, 6.0)  # r = (3, 4)
    assert eyes.align_residual(pa, pb, (0.0, 0.0), "xy") == _MM(5.0)
    assert eyes.align_residual(pa, pb, (0.0, 0.0), "x") == _MM(3.0)
    assert eyes.align_residual(pa, pb, (0.0, 0.0), "y") == _MM(4.0)


def test_residual_axis_xy_x_y_with_offset():
    pa, pb = (1.0, 2.0), (4.0, 6.0)
    off = (3.0, 1.0)  # r = (0, 3)
    assert eyes.align_residual(pa, pb, off, "xy") == _MM(3.0)
    assert eyes.align_residual(pa, pb, off, "x") == _MM(0.0)
    assert eyes.align_residual(pa, pb, off, "y") == _MM(3.0)


def test_operand_order_gives_the_same_residual_at_zero_offset():
    g = _graph(A=(1.0, 2.0), B=(4.0, 6.0))
    for axis in ("xy", "x", "y"):
        ab = eyes.evaluate_measures(g, [_align(_inst("B"), axis=axis)])[0]
        ba = eyes.evaluate_measures(g, [_align(_inst("A"), first="B", axis=axis)])[0]
        assert ab["value"] == ba["value"] and ab["value"] > 0, axis


def test_swapping_operands_negates_the_offset():
    g = _graph(A=(1.0, 2.0), B=(4.0, 6.0))
    off = (3.0, 1.0)
    ab = eyes.evaluate_measures(g, [_align(_inst("B"), offset=off)])[0]
    # same offset, swapped: a DIFFERENT residual (offset is operand 2 rel. 1) ...
    ba_same = eyes.evaluate_measures(g, [_align(_inst("A"), first="B", offset=off)])[0]
    assert ba_same["value"] != ab["value"]
    # ... and the negated offset restores the identical residual.
    ba = eyes.evaluate_measures(
        g, [_align(_inst("A"), first="B", offset=(-off[0], -off[1]))]
    )[0]
    assert ba["value"] == ab["value"]


def test_datum_first_equals_instance_first_with_negated_offset():
    off = (1.0, -2.0)
    a = eyes.parse_align(_align({"point": [5.0, 6.0]}, offset=off))
    b = eyes.parse_align(
        _align(_inst("A"), first={"point": [5.0, 6.0]}, offset=(-off[0], -off[1]))
    )
    assert a is not None and b is not None
    # normalised: ref_a is the instance, the datum is the other end
    assert (b.ref_a, b.ref_b, b.datum) == ("A", None, (5.0, 6.0))
    assert b.offset == (off[0], off[1])  # negation of the negation
    g = _graph(A=(2.0, 3.0))
    va = eyes.evaluate_measures(g, [_align({"point": [5.0, 6.0]}, offset=off)])[0]
    vb = eyes.evaluate_measures(
        g,
        [_align(_inst("A"), first={"point": [5.0, 6.0]}, offset=(-off[0], -off[1]))],
    )[0]
    assert va["value"] == vb["value"]


def test_eyes_pair_value_verdict_and_over():
    g = _graph(A=(0.0, 0.0), B=(3.0, 4.0))
    xy = eyes.evaluate_measures(g, [_align(_inst("B"), goal=5.0)])[0]
    assert xy["value"] == 5.0 and xy["verdict"] == "ok" and xy["over"] == "A, B"
    xy = eyes.evaluate_measures(g, [_align(_inst("B"), goal=4.9)])[0]
    assert xy["verdict"] == "VIOLATED"
    x = eyes.evaluate_measures(g, [_align(_inst("B"), axis="x", goal=2.9)])[0]
    assert x["value"] == 3.0 and x["verdict"] == "VIOLATED"
    y = eyes.evaluate_measures(g, [_align(_inst("B"), axis="y", goal=4.0)])[0]
    assert y["value"] == 4.0 and y["verdict"] == "ok"


def test_eyes_offset_makes_a_violation_ok():
    g = _graph(A=(0.0, 0.0), B=(0.0, 1.5))
    bare = eyes.evaluate_measures(g, [_align(_inst("B"))])[0]
    assert bare["verdict"] == "VIOLATED" and bare["value"] == 1.5
    shifted = _align(_inst("B"), offset=(0.0, 1.5))
    assert eyes.evaluate_measures(g, [shifted])[0]["verdict"] == "ok"
    # axis x ignores the y offset entirely
    assert eyes.evaluate_measures(g, [_align(_inst("B"), axis="x")])[0]["value"] == 0.0


def test_eyes_default_tolerance_is_0_05_mm():
    g = _graph(A=(0.0, 0.0), B=(0.04, 0.0))
    near = eyes.evaluate_measures(g, [_align(_inst("B"), goal=None)])[0]
    assert near["verdict"] == "ok" and near["goal"] == 0.05
    g = _graph(A=(0.0, 0.0), B=(0.06, 0.0))
    far = eyes.evaluate_measures(g, [_align(_inst("B"), goal=None)])[0]
    assert far["verdict"] == "VIOLATED" and far["value"] == 0.06


def test_eyes_point_and_feature_operands():
    g = _graph(A=(10.0, 5.0))
    pt = eyes.evaluate_measures(g, [_align({"point": [10.0, 8.0]})])[0]
    assert pt["value"] == 3.0 and pt["verdict"] == "VIOLATED"
    feats = [{"feature_id": 17, "ftype": "hole", "x": 10.0, "y": 5.0}]
    ft = eyes.evaluate_measures(g, [_align({"feature_id": 17})], feats)[0]
    assert ft["value"] == 0.0 and ft["verdict"] == "ok"
    # the input rows are not mutated by the feature binding
    row = _align({"feature_id": 17})
    eyes.evaluate_measures(g, [row], feats)
    assert row["operands"][1] == {"feature_id": 17}


def test_eyes_snapped_detail_only_on_an_ok_row():
    g = _graph(A=(0.0, 0.0), B=(0.0, 0.0))
    row = _align(_inst("B"))
    row["meta"]["snapped"] = True
    assert eyes.evaluate_measures(g, [row])[0]["detail"] == "snapped"
    g2 = _graph(A=(0.0, 0.0), B=(1.0, 0.0))  # drifted since: not a snap any more
    assert "detail" not in eyes.evaluate_measures(g2, [row])[0]
    # and an un-flagged ok row carries no detail
    assert "detail" not in eyes.evaluate_measures(g, [_align(_inst("B"))])[0]


@pytest.mark.parametrize(
    "row,feats",
    [
        # unknown refdes
        (_align(_inst("NOPE")), None),
        (_align(_inst("B"), first="NOPE"), None),
        # unknown feature id / no features supplied at all
        (_align({"feature_id": 99}), [{"feature_id": 17, "x": 1.0, "y": 1.0}]),
        (_align({"feature_id": 17}), None),
        # role operands are out of scope
        (_align({"role": "noisy"}), None),
        # two datums: nothing to move
        (_align({"point": [1.0, 1.0]}, first={"point": [0.0, 0.0]}), None),
        # wrong arity
        ({**_align(_inst("B")), "operands": [{"instance": "A"}]}, None),
        # malformed pieces
        (_align({"point": [1.0]}), None),
        (_align(_inst("B"), axis="z"), None),
        ({**_align(_inst("B")), "meta": {"offset": [1.0]}}, None),
        ({**_align(_inst("B")), "meta": {"offset": ["a", 0]}}, None),
        (_align(_inst("A")), None),
    ],
)
def test_unresolved_operand_is_pending_never_ok(row, feats):
    g = _graph(A=(0.0, 0.0), B=(0.0, 0.0))
    res = eyes.evaluate_measures(g, [row], feats)[0]
    assert res["verdict"] == "pending" and res["value"] is None


def test_unplaced_refdes_is_pending():
    g = _graph(A=(0.0, 0.0), B=None)
    assert eyes.evaluate_measures(g, [_align(_inst("B"))])[0]["verdict"] == "pending"
    g = _graph(A=None, B=(0.0, 0.0))
    assert eyes.evaluate_measures(g, [_align(_inst("B"))])[0]["verdict"] == "pending"


# ── resolve_measures (optimize) ─────────────────────────────────────────
def test_resolve_measures_pair_and_datum_shapes():
    feats = [{"feature_id": 7, "x": 3.0, "y": 4.0}]
    specs = resolve_measures(
        [
            _align(_inst("B"), axis="y", offset=(0.0, 2.0), goal=None, measure_id=11),
            _align({"point": [1.0, 2.0]}, strength="hard", weight=0.5),
            _align({"feature_id": 7}),
            _align({"feature_id": 8}),  # unknown feature: dropped
            _align({"role": "x"}),  # unsupported: dropped
            _align(_inst("B"), strength="gauge"),  # never steers
        ],
        features=feats,
    )
    assert len(specs) == 3
    pair, point, feat = specs
    assert (pair.refdes_b, pair.datum, pair.axis, pair.offset) == (
        "B",
        None,
        "y",
        (0.0, 2.0),
    )
    assert pair.goal_mm == 0.05 and not pair.hard and pair.measure_id == 11
    assert (point.refdes_b, point.datum, point.hard, point.weight) == (
        "",
        (1.0, 2.0),
        True,
        0.5,
    )
    assert feat.datum == (3.0, 4.0)


# ── optimize: the production anneal ─────────────────────────────────────
def _pair_ir(ax=0.0, ay=0.0, bx=30.0, by=0.0, *, b_fixed=False):
    b = {"refdes": "B", **({"fixed": "xy"} if b_fixed else {})}
    graph = {"instances": [{"refdes": "A"}, b], "nets": []}
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.inst_x[0], ir.inst_y[0] = ax, ay
    ir.inst_x[1], ir.inst_y[1] = bx, by
    return ir


def test_hard_align_to_feature_lands_within_tolerance_after_anneal():
    """A free part, a fixed feature datum, the full production path
    (anneal, then the snap pass): the part ends within a 2 um tolerance,
    far below what the 0.5 mm translate step floor lets the anneal hit, and
    the result says the snap did it."""
    feats = [{"feature_id": 5, "x": 12.0, "y": 7.0}]
    measures = resolve_measures(
        [_align({"feature_id": 5}, goal=0.002, strength="hard", measure_id=9)],
        features=feats,
    )
    ir = _pair_ir()
    res = optimize(ir, OptimizeConfig(seed=3, iters=6000, measures=measures))
    ax, ay, _ = res.positions["A"]
    assert math.hypot(ax - 12.0, ay - 7.0) <= 0.002
    assert res.snapped == (9,)  # the anneal alone left a sub-step residual
    # and the eyes agree, off the same positions
    graph = {
        "instances": [
            {"refdes": r, "x": p[0], "y": p[1], "roles": []}
            for r, p in res.positions.items()
        ]
    }
    ev = eyes.evaluate_measures(
        graph, [_align({"feature_id": 5}, goal=0.002, strength="hard")], feats
    )[0]
    assert ev["verdict"] == "ok" and ev["value"] <= 0.002


def test_hard_align_with_axis_and_offset_after_anneal():
    measures = resolve_measures(
        [
            _align(
                {"point": [20.0, 10.0]},
                axis="x",
                offset=(4.0, 0.0),
                strength="hard",
            )
        ]
    )
    res = optimize(_pair_ir(), OptimizeConfig(seed=4, iters=6000, measures=measures))
    ax, _ay, _ = res.positions["A"]
    assert abs(ax - 16.0) <= 0.05  # pos_2 - pos_1 == 4 -> a.x == 16; y free


def test_soft_align_gets_the_penalty_but_never_the_snap():
    measures = resolve_measures([_align({"point": [12.0, 7.0]}, strength="soft")])
    engine = OptimizeEngine(_pair_ir(12.3, 7.0), OptimizeConfig(measures=measures))
    assert engine.snap_hard_aligns() == ()
    assert float(engine.ir.inst_x[0]) == 12.3  # untouched


def _snap_engine(ax, ay, **kw):
    spec_row = _align({"point": [12.0, 7.0]}, strength="hard", measure_id=4, **kw)
    ir = _pair_ir(ax, ay, 40.0, 40.0, b_fixed=True)
    return OptimizeEngine(ir, OptimizeConfig(measures=resolve_measures([spec_row])))


def test_snap_closes_a_sub_step_residual_exactly():
    engine = _snap_engine(12.3, 7.2)  # residual 0.36 < the 0.5 mm floor
    before = _measure_pair_usd(engine.ir, 0, -1, engine._measure_pairs[0][2])
    assert before > 0.0
    assert engine.snap_hard_aligns() == (4,)
    assert float(engine.ir.inst_x[0]) == _MM(12.0)
    assert float(engine.ir.inst_y[0]) == _MM(7.0)
    assert engine._money_measures == _MM(0.0)  # engine caches followed the move


def test_snap_respects_the_axis_and_leaves_the_free_axis_alone():
    engine = _snap_engine(12.3, 7.2, axis="x")
    assert engine.snap_hard_aligns() == (4,)
    assert float(engine.ir.inst_x[0]) == _MM(12.0)
    assert float(engine.ir.inst_y[0]) == 7.2  # y is free: untouched


def test_snap_leaves_a_residual_at_or_beyond_the_step_floor_to_the_anneal():
    engine = _snap_engine(12.6, 7.0)  # 0.6 mm >= 0.5 mm
    assert engine.snap_hard_aligns() == ()
    assert float(engine.ir.inst_x[0]) == 12.6


def test_snap_skips_an_already_satisfied_align():
    engine = _snap_engine(12.02, 7.0)  # inside the 0.05 mm tolerance
    assert engine.snap_hard_aligns() == ()
    assert float(engine.ir.inst_x[0]) == 12.02


def test_snap_pair_moves_only_the_free_operand():
    row = _align(_inst("B"), strength="hard", measure_id=2, offset=(0.0, 6.0))
    ir = _pair_ir(10.2, 5.0, 10.0, 11.0, b_fixed=True)  # want a = b - (0, 6)
    engine = OptimizeEngine(ir, OptimizeConfig(measures=resolve_measures([row])))
    assert engine.snap_hard_aligns() == (2,)
    assert (float(ir.inst_x[0]), float(ir.inst_y[0])) == (_MM(10.0), _MM(5.0))
    assert (float(ir.inst_x[1]), float(ir.inst_y[1])) == (10.0, 11.0)  # B is locked
    # the free operand being the SECOND one moves b by the negated residual
    row2 = _align(
        _inst("B"), first="A", strength="hard", measure_id=3, offset=(6.0, 0.0)
    )
    ir2 = _pair_ir(10.0, 5.0, 16.3, 5.0)
    ir2.inst_fixed_xy[0] = True  # A locked, B free
    engine2 = OptimizeEngine(ir2, OptimizeConfig(measures=resolve_measures([row2])))
    assert engine2.snap_hard_aligns() == (3,)
    assert float(ir2.inst_x[1]) == _MM(16.0)


def test_snap_does_nothing_when_both_operands_are_free():
    row = _align(_inst("B"), strength="hard", measure_id=2, offset=(6.0, 0.0))
    ir = _pair_ir(10.2, 5.0, 16.0, 5.0)
    engine = OptimizeEngine(ir, OptimizeConfig(measures=resolve_measures([row])))
    assert engine.snap_hard_aligns() == ()
    assert float(ir.inst_x[0]) == 10.2


def test_snap_target_inside_another_courtyard_is_refused_and_stays_violated():
    target = (12.0, 7.0)
    ir = _pair_ir(target[0] - 0.3, target[1], 0.0, 0.0, b_fixed=True)
    row = _align({"point": list(target)}, strength="hard", measure_id=4)
    engine = OptimizeEngine(ir, OptimizeConfig(measures=resolve_measures([row])))
    widths = [
        max(px for px, _ in poly) - min(px for px, _ in poly)
        for poly in (engine._world_courtyard(0), engine._world_courtyard(1))
    ]
    # park the fixed B so A's CURRENT pose is legal but the target pose is not
    ir.move_instance(1, x=target[0] + sum(widths) / 2.0 - 0.1, y=target[1])
    engine = OptimizeEngine(ir, OptimizeConfig(measures=resolve_measures([row])))
    assert engine._placement_is_legal([(0, target[0] - 0.3, target[1])])
    assert not engine._placement_is_legal([(0, target[0], target[1])])
    assert engine.snap_hard_aligns() == ()
    assert float(ir.inst_x[0]) == _MM(target[0] - 0.3)  # reverted: never moved
    graph = {
        "instances": [
            {"refdes": "A", "x": float(ir.inst_x[0]), "y": float(ir.inst_y[0])},
            {"refdes": "B", "x": float(ir.inst_x[1]), "y": float(ir.inst_y[1])},
        ]
    }
    ev = eyes.evaluate_measures(graph, [row])[0]
    assert ev["verdict"] == "VIOLATED" and ev["value"] == _MM(0.3)


def test_optimize_reports_the_snapped_measure_ids():
    feats = [{"feature_id": 5, "x": 12.0, "y": 7.0}]
    measures = resolve_measures(
        [_align({"feature_id": 5}, strength="hard", measure_id=9)], features=feats
    )
    res = optimize(_pair_ir(), OptimizeConfig(seed=3, iters=6000, measures=measures))
    assert set(res.snapped) <= {9}


def test_optimize_does_not_recentre_a_design_aligned_to_a_datum():
    """`recentre_in_outline` rigidly shifts the finished pack; a datum is
    fixed to the board, so a steering align to one must veto the shift."""
    ir = _pair_ir()
    ir.outline = [(0.0, 0.0), (60.0, 0.0), (60.0, 40.0), (0.0, 40.0)]
    measures = resolve_measures(
        [_align({"point": [12.0, 7.0]}, strength="hard")],
    )
    res = optimize(ir, OptimizeConfig(seed=3, iters=4000, measures=measures))
    ax, ay, _ = res.positions["A"]
    assert math.hypot(ax - 12.0, ay - 7.0) <= 0.05


def test_hard_align_pair_ends_within_tolerance():
    measures = resolve_measures(
        [_align(_inst("B"), strength="hard", offset=(8.0, 0.0))]
    )
    res = optimize(_pair_ir(), OptimizeConfig(seed=5, iters=8000, measures=measures))
    ax, ay, _ = res.positions["A"]
    bx, by, _ = res.positions["B"]
    # both free: no snap, the anneal alone lands the pair within a step
    assert math.hypot(bx - ax - 8.0, by - ay) <= 0.5


# ── delta == full recompute ─────────────────────────────────────────────
def _align_board_measures():
    """pair + datum terms over a connected 8-part board."""
    return resolve_measures(
        [
            _align(_inst("U1"), first="U0", offset=(3.0, 0.0)),
            _align(_inst("U2"), first="U1", axis="x", strength="hard"),
            _align({"point": [5.0, -4.0]}, first="U0", goal=0.1),
            _align({"feature_id": 1}, first="U3", axis="y", strength="hard"),
            _align({"point": [0.0, 0.0]}, first="U2", weight=2.0),
        ],
        features=[{"feature_id": 1, "x": -7.0, "y": 9.0}],
    )


def _expected_measures_usd(ir, specs) -> float:
    idx = {str(ir.instance_refdes[i]): i for i in range(ir.n_instances)}
    return sum(
        _measure_pair_usd(ir, idx[s.refdes_a], idx.get(s.refdes_b, -1), s)
        for s in specs
    )


def test_align_delta_matches_full_recompute_over_random_moves():
    ir = _seeded_ir(8, graph_seed=21, seed_rng_seed=22)
    specs = _align_board_measures()
    assert len(specs) == 5
    cost_config = OptimizeConfig().cost
    engine = OptimizeEngine(ir, OptimizeConfig(seed=22, measures=specs))
    assert engine._money_measures == _MM(_expected_measures_usd(ir, specs))
    assert engine._money_measures > 0.0  # the terms are live, not vacuous

    rng = random.Random(23)
    kinds = list(MoveKind)
    moved = 0
    for trial in range(150):
        move = MOVE_GENERATORS[kinds[rng.randrange(len(kinds))]](engine, rng, 8.0)
        if move is None:
            continue
        engine.apply_move(move)
        moved += 1
        if rng.random() < 0.5:
            engine.undo_move(move)
        want = _expected_measures_usd(ir, specs)
        assert engine._money_measures == _MM(want, rel=1e-9, abs=1e-9), trial
        full = evaluate_cost(
            ir, Level.L4, dataclasses.replace(cost_config, schedule=engine.schedule)
        )
        assert engine.money() == _MM(full.money + want, rel=1e-9, abs=1e-9), trial
    assert moved > 50


def test_align_delta_holds_through_a_real_anneal_and_the_snap():
    ir = _seeded_ir(8, graph_seed=24, seed_rng_seed=25)
    specs = _align_board_measures()
    config = OptimizeConfig(seed=25, iters=150, measures=specs)
    engine = OptimizeEngine(ir, config)
    engine.anneal(random.Random(config.seed))
    engine.snap_hard_aligns()
    assert engine._money_measures == _MM(
        _expected_measures_usd(ir, specs), rel=1e-9, abs=1e-9
    )


def test_datum_end_is_indexed_only_under_its_own_instance():
    specs = resolve_measures([_align({"point": [1.0, 1.0]}, strength="hard")])
    engine = OptimizeEngine(_pair_ir(), OptimizeConfig(measures=specs))
    assert engine._measures_by_inst == {0: [0]}
    assert engine._money_measures == _MM(40.0 * (math.hypot(1.0, 1.0) - 0.05))


def test_gauge_align_never_changes_the_objective():
    gauge = [
        _align({"point": [12.0, 7.0]}, strength="gauge"),
        _align(_inst("B"), strength="gauge"),
    ]
    assert resolve_measures(gauge) == ()
    base = optimize(_pair_ir(), OptimizeConfig(seed=6, iters=500))
    with_gauge = optimize(
        _pair_ir(), OptimizeConfig(seed=6, iters=500, measures=resolve_measures(gauge))
    )
    assert with_gauge.positions == base.positions
    assert with_gauge.cost_after == base.cost_after
    assert with_gauge.snapped == ()
    engine = OptimizeEngine(
        _pair_ir(), OptimizeConfig(measures=resolve_measures(gauge))
    )
    assert engine._money_measures == 0.0


def test_zero_weight_align_is_inert_in_the_engine():
    specs = resolve_measures([_align({"point": [9.0, 9.0]}, strength="hard", weight=0)])
    assert len(specs) == 1
    engine = OptimizeEngine(_pair_ir(9.2, 9.0), OptimizeConfig(measures=specs))
    assert engine._money_measures == 0.0
    assert engine.snap_hard_aligns() == ()  # weight 0 records, never steers


def test_soft_vs_hard_scale_for_the_same_violation():
    ir = _pair_ir()
    soft = resolve_measures([_align({"point": [3.0, 4.0]}, strength="soft")])[0]
    hard = resolve_measures([_align({"point": [3.0, 4.0]}, strength="hard")])[0]
    assert _measure_pair_usd(ir, 0, -1, soft) == _MM(1.0 * (5.0 - 0.05))
    assert _measure_pair_usd(ir, 0, -1, hard) == _MM(40.0 * (5.0 - 0.05))


# ── place.py: the quick placer's penalty ────────────────────────────────
def _specs(measures, positions, features=None):
    bound = eyes.bind_feature_operands(measures, features)
    return place._measure_specs(bound, positions, {})


def test_place_spec_penalty_pair():
    pos = {"A": (0.0, 0.0), "B": (3.0, 4.0)}
    (spec,) = _specs([_align(_inst("B"), goal=1.0)], pos)
    assert place._spec_penalty(spec, pos) == _MM(5.0 - 1.0)
    (spec,) = _specs([_align(_inst("B"), axis="x", goal=1.0)], pos)
    assert place._spec_penalty(spec, pos) == _MM(3.0 - 1.0)
    (spec,) = _specs([_align(_inst("B"), offset=(3.0, 4.0), goal=1.0)], pos)
    assert place._spec_penalty(spec, pos) == 0.0
    (spec,) = _specs([_align(_inst("B"), goal=1.0, weight=3.0)], pos)
    assert place._spec_penalty(spec, pos) == _MM(3.0 * 4.0)


def test_place_spec_penalty_datum_point_and_feature():
    pos = {"A": (0.0, 0.0)}
    (spec,) = _specs([_align({"point": [3.0, 4.0]}, goal=0.5)], pos)
    assert spec.refs == ("A",)
    assert place._spec_penalty(spec, pos) == _MM(5.0 - 0.5)
    feats = [{"feature_id": 2, "x": 3.0, "y": 4.0}]
    (spec,) = _specs([_align({"feature_id": 2}, axis="y", goal=0.5)], pos, feats)
    assert place._spec_penalty(spec, pos) == _MM(4.0 - 0.5)
    pos["A"] = (3.0, 4.2)
    assert place._spec_penalty(spec, pos) == 0.0  # within tolerance


def test_place_skips_gauge_and_unresolved_align():
    pos = {"A": (0.0, 0.0), "B": (1.0, 1.0)}
    rows = [
        _align(_inst("B"), strength="gauge"),
        _align(_inst("ZZ")),
        _align({"feature_id": 9}),
        _align({"role": "r"}),
    ]
    assert _specs(rows, pos) == []


def test_autoplace_pulls_a_free_part_toward_a_feature_datum():
    instances = [
        {"refdes": "A", "x": 40.0, "y": 40.0, "fixed": None, "roles": []},
        {"refdes": "B", "x": 41.0, "y": 40.0, "fixed": None, "roles": []},
    ]
    feats = [{"feature_id": 3, "x": 10.0, "y": 10.0}]
    res = place.autoplace(
        instances,
        [],
        measures=[_align({"feature_id": 3}, goal=0.05, strength="hard")],
        iters=3000,
        seed=1,
        features=feats,
    )
    ax, ay = res.positions["A"]
    assert res.objective_after < res.objective_before
    assert math.hypot(ax - 10.0, ay - 10.0) < 40.0 * math.sqrt(2) - 10.0


# ── content hash: authored meta counts, the job-written flag does not ───
def test_content_hash_sees_authored_meta_but_not_snapped():
    graph: dict = {"instances": [], "nets": []}

    def h(row):
        return session.content_hash(graph, {}, session_state={"measures": [row]})

    base = _align(_inst("B"))
    shifted = _align(_inst("B"), offset=(0.0, 1.0))
    snapped = _align(_inst("B"))
    snapped["meta"]["snapped"] = True
    assert h(base) != h(shifted)
    assert h(base) == h(snapped)
    # a row with no meta at all hashes exactly as it did before align existed
    legacy = {k: v for k, v in base.items() if k != "meta"}
    assert h(legacy) == h({**base, "meta": {}})


def test_content_hash_orders_rows_tied_up_to_their_dict_operands():
    """Four standoff aligns differ only in operands (dicts) and reason:
    the hash must not compare dicts, and row order must not matter."""
    graph: dict = {"instances": [], "nets": []}
    rows = [
        _align({"feature_id": 46 + i}, first=f"CN{i}", strength="hard")
        for i in (1, 2, 5, 6)
    ]
    rows.append({"metric": "proximity", "operands": [_inst("A"), _inst("B")]})
    rows.append({**rows[-1], "goal": 2.0})  # None vs float goal on a tie

    def h(ms):
        return session.content_hash(graph, {}, session_state={"measures": ms})

    assert h(rows) == h(list(reversed(rows)))


# ── store + handler: meta round trip, snapped stamp, view detail ────────
@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _align_design(pcb, slug):
    pcb.put(
        id=slug,
        args={
            "components": [
                {
                    "refdes": "D3",
                    "label": "led",
                    "pins": [{"name": "1"}],
                },
                {
                    "refdes": "J1",
                    "label": "conn",
                    "pins": [{"name": "1"}],
                },
            ],
            "measures": [
                {
                    "metric": "align",
                    "operands": [{"instance": "D3"}, {"instance": "J1"}],
                    "meta": {"axis": "x", "offset": [0.0, 1.5]},
                    "goal": 0.05,
                    "strength": "hard",
                    "reason": "LED on the connector's centre line",
                },
            ],
        },
    )
    ref = pcb.store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    # The LED sits on its mounting hole and the connector overlaps it — the
    # point of the fixture, and exactly what `put` refuses to create, so the
    # poses and the hole are written straight to the store.
    pcb.store.pcb_set_pose(ref.id, {"D3": (10.0, 5.0, 0.0), "J1": (10.0, 6.5, 0.0)})
    pcb.store.pcb_apply(
        slug=slug,
        title=slug,
        components=[],
        nets=[],
        connections=[],
        features=[
            {"ftype": "mounting_hole", "x": 10.0, "y": 5.0, "geom": {"diameter": 3.2}}
        ],
    )
    return ref.id


def test_measure_meta_round_trips_and_view_evaluates_it(pcb, store):
    ref_id = _align_design(pcb, "align-a")
    (row,) = store.pcb_measures_list(ref_id)
    assert row["meta"] == {"axis": "x", "offset": [0.0, 1.5]}
    assert isinstance(row["measure_id"], int)
    body = pcb.get(id="align-a", view="measures").body
    assert "align" in body and "ok" in body and "VIOLATED" not in body
    assert "snapped" not in body


def test_mark_snapped_stamps_clears_and_surfaces_as_detail(pcb, store):
    ref_id = _align_design(pcb, "align-b")
    (row,) = store.pcb_measures_list(ref_id)
    assert store.pcb_measures_mark_snapped(ref_id, [row["measure_id"]]) == 1
    (row,) = store.pcb_measures_list(ref_id)
    assert row["meta"] == {"axis": "x", "offset": [0.0, 1.5], "snapped": True}
    body = pcb.get(id="align-b", view="measures").body
    assert "detail" in body and "snapped" in body
    # the next run snapped nothing: the flag is cleared, authored meta kept
    assert store.pcb_measures_mark_snapped(ref_id, []) == 0
    (row,) = store.pcb_measures_list(ref_id)
    assert row["meta"] == {"axis": "x", "offset": [0.0, 1.5]}
    assert "snapped" not in pcb.get(id="align-b", view="measures").body


def test_view_resolves_a_feature_id_operand_from_the_store(pcb, store):
    ref_id = _align_design(pcb, "align-c")
    (feat,) = store.pcb_features_list(ref_id)
    pcb.put(
        id="align-c",
        args={
            "measures": [
                {
                    "metric": "align",
                    "operands": [
                        {"instance": "D3"},
                        {"feature_id": feat["feature_id"]},
                    ],
                    "goal": 0.05,
                    "strength": "gauge",
                    "reason": "LED over its mounting hole",
                }
            ]
        },
    )
    body = pcb.get(id="align-c", view="measures").body
    assert "pending" not in body and "gauge" in body
