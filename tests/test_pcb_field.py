"""Gradient checks for the unified place-and-route energy (slice 0: T1, T4)."""

from __future__ import annotations

import numpy as np
import pytest

from precis.pcb import field as F
from precis.pcb.ir import pin_point
from precis.pcb.optimize import OptimizeConfig, OptimizeEngine, optimize


def _board(seed: int, n: int = 5, overlap: bool = False) -> F.FieldBoard:
    rng = np.random.default_rng(seed)
    parts = []
    for i in range(n):
        w, h = rng.uniform(1.0, 2.5, 2)
        court = np.array([[-w, -h], [w, -h], [w, h], [-w, h]])
        pads = rng.uniform(-0.8, 0.8, (3, 2))
        parts.append(
            F.Part(pads=pads, court=court, side=int(i == n - 1 and not overlap))
        )
    nets = tuple(
        tuple(
            (int(i), int(rng.integers(3))) for i in rng.choice(n, size=3, replace=False)
        )
        for _ in range(4)
    )
    span = 3.0 if overlap else 6.0
    return F.FieldBoard(
        parts=tuple(parts),
        nets=nets,
        x0=rng.uniform(0, span, n),
        y0=rng.uniform(0, span, n),
        th0=rng.uniform(-3, 3, n),
        vias0=rng.uniform(0, 5, (2, 2)),
    )


def _term(name: str, lay: F.Layout) -> F.Term:
    if name == "T1":
        return F.ConnectivityPull(lay, gamma=0.7)
    return F.CourtyardClearance(lay, beta=6.0)


@pytest.mark.parametrize("name", ["T1", "T4"])
@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_gradient_checks(name: str, seed: int) -> None:
    lay = F.Layout(_board(seed, overlap=(name == "T4" and seed % 2 == 0)))
    term = _term(name, lay)
    z = lay.pack()
    if name == "T4":
        assert term.value_grad(z)[0] > 0.0  # the board actually exercises the term
    res = F.check_gradient(term.value_grad, z, rng=np.random.default_rng(seed), h=1e-5)
    assert res.dot_rel_err < 1e-4, res
    assert all(1.8 < o < 2.3 for o in res.taylor_orders), res
    assert res.fd_max_rel_err < 1e-3, res


@pytest.mark.parametrize("name", ["T1", "T4"])
def test_rotation_gradient_nonzero(name: str) -> None:
    lay = F.Layout(_board(7, overlap=True))
    _, g = _term(name, lay).value_grad(lay.pack())
    start = 2 * int(lay.free_xy.sum())
    rot = g[start : start + int(lay.free_rot.sum())]
    assert np.abs(rot).max() > 1e-6


def test_fixed_variables_have_no_slot() -> None:
    b = _board(3)
    p0 = F.Part(b.parts[0].pads, b.parts[0].court, fixed_xy=True, fixed_rot=True)
    lay = F.Layout(F.FieldBoard((p0, *b.parts[1:]), b.nets, b.x0, b.y0, b.th0, b.vias0))
    assert lay.size == 2 * 4 + 4 + 4
    z = lay.pack()
    x, y, th, vias = lay.unpack(z)
    assert (x[0], y[0], th[0]) == (b.x0[0], b.y0[0], b.th0[0])
    assert vias.shape == (2, 2)
    energy = F.Energy(lay, [(1.0, _term("T1", lay)), (0.5, _term("T4", lay))])
    assert energy.value_grad(z)[1].shape == z.shape


def test_t4_ignores_opposite_sides() -> None:
    sq = np.array([[-1.0, -1], [1, -1], [1, 1], [-1, 1]])
    zeros = np.zeros(2)
    for sides, expect_zero in (((0, 1), True), ((0, 0), False)):
        parts = tuple(F.Part(pads=np.zeros((1, 2)), court=sq, side=s) for s in sides)
        lay = F.Layout(F.FieldBoard(parts, (), np.array([0.0, 0.5]), zeros, zeros))
        v, _ = F.CourtyardClearance(lay).value_grad(lay.pack())
        assert (v == 0.0) == expect_zero


def test_t4_plus_sign_crossing_has_value_and_gradient() -> None:
    """Two bars crossing like a plus: no vertex of either lies inside the
    other, so a vertex-depth rule gives zero penalty and zero gradient."""
    bar = np.array([[-2.0, -0.5], [2.0, -0.5], [2.0, 0.5], [-2.0, 0.5]])
    parts = tuple(F.Part(pads=np.zeros((1, 2)), court=bar) for _ in range(2))
    board = F.FieldBoard(
        parts,
        (),
        np.array([0.0, 0.2]),
        np.array([0.0, 0.1]),
        np.array([0.05, np.pi / 2 + 0.1]),
    )
    lay = F.Layout(board)
    term = F.CourtyardClearance(lay, beta=6.0)
    z = lay.pack()
    v, g = term.value_grad(z)
    assert v > 1.0 and np.linalg.norm(g) > 1e-2
    res = F.check_gradient(term.value_grad, z, rng=np.random.default_rng(5), h=1e-6)
    assert res.dot_rel_err < 1e-4 and res.fd_max_rel_err < 1e-3, res
    assert all(1.8 < o < 2.3 for o in res.taylor_orders), res


@pytest.mark.parametrize("name", ["T5", "T6"])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_t5_t6_gradient_checks(name: str, seed: int) -> None:
    lay = F.Layout(_board(seed, overlap=True))
    if name == "T5":
        term: F.Term = F.BoardEdge(lay, (1.0, 1.0), (2.5, 2.5), beta=6.0)
    else:
        term = F.PadSpring(lay, [(0, 0, 1, 1, 0.3), (2, 2, 3, 0, 0.1)], beta=6.0)
    z = lay.pack()
    assert term.value_grad(z)[0] > 0.0
    res = F.check_gradient(term.value_grad, z, rng=np.random.default_rng(seed), h=1e-5)
    assert res.dot_rel_err < 1e-4, res
    assert res.fd_max_rel_err < 1e-3, res


def _engine(seed: int = 1, n: int = 8) -> OptimizeEngine:
    from tests.test_pcb_optimize import _seeded_ir

    return OptimizeEngine(
        _seeded_ir(n, graph_seed=seed, seed_rng_seed=seed), OptimizeConfig(seed=seed)
    )


def test_board_from_engine_pads_match_pin_point() -> None:
    engine = _engine()
    ir = engine.ir
    ir.inst_rot[0] = 90.0
    built = F.board_from_engine(engine)
    assert built is not None
    board, _ = built
    lay = F.Layout(board)
    x, y, th, _v = lay.unpack(lay.pack())
    for i, part in enumerate(board.parts):
        pins = [p for p in range(len(ir.pin_instance)) if ir.pin_instance[p] == i]
        world = F._world(part, part.pads, x[i], y[i], th[i])
        for k, p in enumerate(pins):
            pt = pin_point(ir, p)
            assert pt is not None
            assert world[k] == pytest.approx(pt, abs=1e-6)


def test_polish_never_leaves_the_board_worse() -> None:
    """Never-worse is field energy + pre-route legality (Reto 2026-10-21):
    an applied pose has lower E and passes the legality gate. The
    annealer's ``total()`` is the reported ``anneal_delta`` comparison,
    not a gate — a rise is not undone."""
    for seed in (1, 2, 3):
        engine = _engine(seed)
        res = F.polish_placement(engine)
        assert res.reason in {
            "applied",
            "no_gain",
            "illegal",
            "wirelength_worse",
            "no_free_vars",
            "bad_gradient",
        }
        if res.applied:
            assert res.e_after < res.e_before
            assert res.anneal_delta is not None


@pytest.mark.parametrize(("n", "seed"), [(6, 1), (8, 7)])
def test_polish_accept_path_lowers_energy_and_stays_legal(n: int, seed: int) -> None:
    """Seeded boards the polish is known to improve: the accept path runs,
    the field energy drops, and the resulting pose passes the legality
    predicate. ``anneal_delta`` is the reported annealer comparison — it
    may be flat or rise and the move still stands (Reto 2026-10-21)."""
    engine = _engine(seed, n)
    before = engine.total()
    res = F.polish_placement(engine)
    assert res.applied, res
    assert res.e_after < res.e_before
    assert res.anneal_delta == pytest.approx(engine.total() - before)
    _assert_applied_pose_legal(engine, res)


def test_field_polish_flag_is_opt_in_and_runs_when_set() -> None:
    from tests.test_pcb_optimize import _seeded_ir

    # Opt-in until the silk gate lands (Reto 2026-10-22).
    assert OptimizeConfig().field_polish is False
    ir = _seeded_ir(6, graph_seed=2, seed_rng_seed=2)
    res = optimize(ir, OptimizeConfig(seed=2, field_polish=True))
    assert any(line.startswith("field_polish ") for line in res.support_groups)


def test_flag_off_never_calls_polish_and_is_deterministic(monkeypatch) -> None:
    from tests.test_pcb_optimize import _seeded_ir

    def boom(*_a, **_k):
        raise AssertionError("polish ran with the flag off")

    monkeypatch.setattr(F, "polish_placement", boom)
    poses = []
    for _ in range(2):
        ir = _seeded_ir(6, graph_seed=2, seed_rng_seed=2)
        res = optimize(ir, OptimizeConfig(seed=2, field_polish=False))
        assert not any(s.startswith("field_polish ") for s in res.support_groups)
        poses.append((ir.inst_x.copy(), ir.inst_y.copy(), ir.inst_rot.copy()))
    for a, b in zip(poses[0], poses[1], strict=True):
        assert np.array_equal(a, b)


def test_polish_is_deterministic() -> None:
    from tests.test_pcb_optimize import _seeded_ir

    poses = []
    for _ in range(2):
        ir = _seeded_ir(6, graph_seed=2, seed_rng_seed=2)
        optimize(ir, OptimizeConfig(seed=2, field_polish=True))
        poses.append((ir.inst_x.copy(), ir.inst_y.copy(), ir.inst_rot.copy()))
    for a, b in zip(poses[0], poses[1], strict=True):
        assert np.array_equal(a, b)


def _assert_applied_pose_legal(engine: OptimizeEngine, res: F.PolishResult) -> None:
    ir = engine.ir
    proposals = [(i, float(ir.inst_x[i]), float(ir.inst_y[i])) for i in res.moved]
    rotations = {i: float(ir.inst_rot[i]) for i in res.moved}
    assert engine._placement_is_legal(proposals, rotations=rotations)


def test_polish_repairs_a_snap_illegal_pose() -> None:
    """n=6 seed=8: every snap-cascade lane fails the gate and the bounded
    repair descent still applies a legal pose. (n=6 seed=2 pinned this
    while the L-BFGS-B y bounds landed on θ; with the bounds on y it ends
    no_gain.)"""
    engine = _engine(8, 6)
    res = F.polish_placement(engine)
    assert res.applied, res
    _assert_applied_pose_legal(engine, res)


def test_polish_stalled_stage_retries_with_theta_pinned() -> None:
    """gr503452: at θ = 0 the courtyard witness is a tied-vertex kink whose
    subgradient stalls L-BFGS-B ABNORMAL after 0–1 steps, freezing x, y
    with it — the sanity board's polish returned no_gain. A stalled stage
    is now retried with θ pinned, so the descent still lands a legal,
    lower-E pose."""
    from precis.pcb import sanity_board as sb
    from precis.workers.job_types.pcb_place import _PLACE_ONLY_SCHEDULE

    built = sb.build(sb.SPECS["free-bottom"])
    config = OptimizeConfig(
        iters=300,
        seed=1,
        schedule=_PLACE_ONLY_SCHEDULE,
        pin_point_cost=True,
        centroid_jump=True,
        field_polish=False,
    )
    optimize(built.ir, config)
    engine = OptimizeEngine(built.ir, config)
    res = F.polish_placement(engine)
    assert res.applied, res
    assert res.e_after < res.e_before
    _assert_applied_pose_legal(engine, res)


def test_polish_gate_is_legality_and_field_energy_not_anneal_cost() -> None:
    """gr503458: the accept gate is pre-route DRC legality plus the field's
    own energy; the annealer's ``total()`` is the reported ``anneal_delta``
    comparison, never a reason to undo. This sanity pose raises the
    annealer total — the old gate refused it as cost_worse."""
    from precis.pcb import sanity_board as sb
    from precis.workers.job_types.pcb_place import _PLACE_ONLY_SCHEDULE

    built = sb.build(sb.SPECS["free-bottom"])
    config = OptimizeConfig(
        iters=300,
        seed=1,
        schedule=_PLACE_ONLY_SCHEDULE,
        pin_point_cost=True,
        centroid_jump=True,
        field_polish=False,
    )
    optimize(built.ir, config)
    engine = OptimizeEngine(built.ir, config)
    before = engine.total()
    res = F.polish_placement(engine)
    assert res.applied, res
    assert res.e_after < res.e_before
    assert res.anneal_delta is not None and res.anneal_delta > 0.0
    assert engine.total() > before
    _assert_applied_pose_legal(engine, res)


def test_legality_violations_names_rule_and_part() -> None:
    engine = _engine(1, 8)
    ir = engine.ir
    # Overlap U0 onto U1's pose (same side) and park U2 outside its box.
    x1, y1 = float(ir.inst_x[1]), float(ir.inst_y[1])
    x0, y0, x1b, y1b = engine.bounds_for(2)
    proposals = [(0, x1, y1), (2, x0 - 1.0, (y0 + y1b) / 2.0)]
    violations = F._legality_violations(engine, proposals, {})
    kinds = {v.split(":")[0] for v in violations}
    assert "courtyard" in kinds
    assert "bounds" in kinds
    assert any("U1" in v for v in violations)


# ── gr503640 fix 2: mounting holes as T4 fixed obstacles ────────────────


def test_t4_obstacle_repels_on_both_sides_and_is_fd_checked() -> None:
    """A hole's keep-out polygon enters T4 as a fixed obstacle: a part
    parked on it sees the term's push regardless of side (a hole goes
    through the board — the same-side exemption does not apply)."""
    sq = np.array([[-1.0, -1], [1, -1], [1, 1], [-1, 1]])
    hole = np.array([[9.0, -1.0], [11.0, -1.0], [11.0, 1.0], [9.0, 1.0]])
    parts = tuple(F.Part(pads=np.zeros((1, 2)), court=sq, side=s) for s in (0, 1, 0))
    lay = F.Layout(
        F.FieldBoard(
            parts,
            (),
            np.array([11.5, 8.6, 40.0]),
            np.array([0.2, -0.3, 40.0]),
            # non-zero θ: axis-aligned witnesses tie (a kink — the FD
            # check's contract is away from kinks, same as the θ=0
            # courtyard tie the polish's probe excludes rotation for)
            np.array([0.13, -0.21, 0.5]),
        )
    )
    term = F.CourtyardClearance(lay, beta=6.0, obstacles=[(hole, None)])
    z = lay.pack()
    v, g = term.value_grad(z)
    assert v > 0.0
    # Packed order: x's, then y's, then θ's. Both the top- and the
    # bottom-side part on the hole feel the push; the far part does not.
    assert np.hypot(g[0], g[3]) > 0.0
    assert np.hypot(g[1], g[4]) > 0.0
    res = F.check_gradient(term.value_grad, z, rng=np.random.default_rng(3), h=1e-5)
    assert res.dot_rel_err < 1e-4 and res.fd_max_rel_err < 1e-3, res


def test_t4_obstacle_exempts_the_owning_part() -> None:
    """A footprint-derived hole is no obstacle to the part that cut it
    (``_hole_owned_by``); it still blocks everyone else."""
    sq = np.array([[-1.0, -1], [1, -1], [1, 1], [-1, 1]])
    hole = np.array([[9.0, -1.0], [11.0, -1.0], [11.0, 1.0], [9.0, 1.0]])
    parts = tuple(F.Part(pads=np.zeros((1, 2)), court=sq) for _ in range(2))
    lay = F.Layout(
        F.FieldBoard(
            parts, (), np.array([10.0, 40.0]), np.array([0.0, 40.0]), np.zeros(2)
        )
    )
    z = lay.pack()
    free = F.CourtyardClearance(lay, beta=6.0, obstacles=[(hole, 0)])
    assert free.value_grad(z)[0] == 0.0
    blocked = F.CourtyardClearance(lay, beta=6.0, obstacles=[(hole, 1)])
    assert blocked.value_grad(z)[0] > 0.0


def test_polish_moves_a_part_off_a_mounting_hole() -> None:
    """gr503640 fix 2: with holes in T4 the descent itself walks a part
    off a hole — before, the polish met the overlap only at the legality
    gate, which refused the whole polish or reverted the part. U0 parked
    overlapping the hole's keep-out (the seed can do that: legality is
    the gate's job, not the seed's) must end applied and legal. Off
    centre on purpose: dead-centre on a round keep-out is the
    penetration-axis kink the gradient probe refuses for. U1-U3 sit
    inside the gate's ~[1.2, 18.8] mm box: parked at 26 mm they only
    stayed put while the descent's y bounds were mis-indexed onto θ."""
    from precis.pcb.ir import MountingHole, from_graph
    from tests.fixtures.pcb.stackups import LEGACY_V1_STACKUP
    from tests.test_pcb_optimize import _board

    ir = from_graph(
        _board(4, seed=3),
        stackup=LEGACY_V1_STACKUP,
        mounting_holes=(
            MountingHole(x=10.0, y=10.0, drill_mm=5.6, ring_dia_mm=8.0, plated=True),
        ),
    )
    ir.inst_x[0], ir.inst_y[0] = 10.8, 9.5
    ir.inst_x[1], ir.inst_y[1] = 17.0, 10.0
    ir.inst_x[2], ir.inst_y[2] = 17.0, 17.0
    ir.inst_x[3], ir.inst_y[3] = 10.0, 17.0
    engine = OptimizeEngine(ir, OptimizeConfig(seed=1))
    res = F.polish_placement(engine)
    assert res.applied, res
    assert 0 in res.moved
    _assert_applied_pose_legal(engine, res)
    # …and U0's final courtyard is off the hole, not merely elsewhere.
    from precis.pcb.geom import convex_polygons_overlap

    assert not convex_polygons_overlap(
        engine._world_courtyard(0), engine._hole_at(0)[2]
    )


def test_hole_obstacles_exempt_the_owning_instance() -> None:
    """``_hole_obstacles`` carries the owning instance's index as the
    exemption — a part-owned hole is no obstacle to its owner (the
    gate's ``_hole_owned_by`` verdict) but stays fixed for the rest."""
    from precis.pcb.ir import MountingHole, from_graph
    from tests.fixtures.pcb.stackups import LEGACY_V1_STACKUP
    from tests.test_pcb_optimize import _board

    graph = _board(2, seed=5)
    graph["instances"][0]["refdes"] = "CN1"
    for net in graph["nets"]:
        for m in net["members"]:
            if m["refdes"] == "U0":
                m["refdes"] = "CN1"
    ir = from_graph(
        graph,
        stackup=LEGACY_V1_STACKUP,
        mounting_holes=(MountingHole(x=10.0, y=10.0, drill_mm=6.4, part="CN1"),),
    )
    ir.inst_x[0], ir.inst_y[0] = 10.0, 10.0  # CN1 on its own hole — legal
    ir.inst_x[1], ir.inst_y[1] = 30.0, 30.0
    engine = OptimizeEngine(ir, OptimizeConfig(seed=1))
    obstacles = F._hole_obstacles(engine)
    assert len(obstacles) == 1 and obstacles[0][1] == 0


# ── slice 2: JLCPCB pairwise spacing ─────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b", "mm"),
    [
        ("0402", "0402", 0.15),  # diagonal
        ("1206", "1206", 0.35),  # diagonal
        ("BGA", "BGA", 2.0),
        ("QFP", "QFP", 1.25),
        ("0603", "BGA", 1.0),
        ("BGA", "0603", 1.0),  # symmetric lookup
        ("QFN", "BGA", 1.5),
        ("SOP", "SOT", 0.4),
    ],
)
def test_jlcpcb_spacing_cells(a: str, b: str, mm: float) -> None:
    assert F.pair_spacing(a, b) == mm


def test_jlcpcb_table_is_complete_and_symmetric() -> None:
    cls = F.SPACING_CLASSES
    assert len(F.JLCPCB_SPACING_MM) == len(cls) * (len(cls) + 1) // 2
    for a in cls:
        for b in cls:
            assert F.pair_spacing(a, b) == F.pair_spacing(b, a) is not None
    assert F.pair_spacing("0402", None) is None
    assert F.pair_spacing("0402", "DIP") is None


def _rows(xs: list[float], ys: list[float]) -> np.ndarray:
    return np.array(list(zip(xs, ys, strict=True)))


def _classify(ref: str, pads: np.ndarray, w: float, h: float) -> str | None:
    return F.classify_package(ref, pads, np.full(len(pads), w), np.full(len(pads), h))


def test_classify_package_families() -> None:
    assert _classify("R1", _rows([-0.5, 0.5], [0, 0]), 0.6, 0.5) == "0402"
    assert _classify("C1", _rows([-0.8, 0.8], [0, 0]), 0.9, 0.95) == "0603"
    assert _classify("C2", _rows([-1.5, 1.5], [0, 0]), 1.1, 1.6) == "1206"
    assert _classify("J1", _rows([-0.5, 0.5], [0, 0]), 0.6, 0.5) is None
    sot = _rows([-1.0, -1.0, 1.0], [-0.95, 0.95, 0.0])
    assert _classify("Q1", sot, 1.0, 0.6) == "SOT"
    ys = [-1.905, -0.635, 0.635, 1.905]
    sop = _rows([-2.7] * 4 + [2.7] * 4, ys + ys)
    assert _classify("U1", sop, 1.5, 0.6) == "SOP"
    side = [-0.75, -0.25, 0.25, 0.75]
    qfn = np.array(
        [(-1.5, y) for y in side]
        + [(1.5, y) for y in side]
        + [(x, -1.5) for x in side]
        + [(x, 1.5) for x in side]
    )
    assert _classify("U2", qfn, 0.8, 0.3) == "QFN"
    assert _classify("U3", qfn * 5, 1.6, 0.3) == "QFP"
    grid = np.array([(x, y) for x in range(5) for y in range(5)], dtype=float) * 0.8
    assert _classify("U4", grid, 0.4, 0.4) == "BGA"


def test_t4_pair_minimum_sets_the_required_clearance() -> None:
    sq = np.array([[-1.0, -1], [1, -1], [1, 1], [-1, 1]])
    zeros = np.zeros(2)
    parts = tuple(F.Part(pads=np.zeros((1, 2)), court=sq) for _ in range(2))
    # 2x2 squares with a 0.3 mm gap: a 0.15 minimum is met, a 1.0 one is not
    lay = F.Layout(F.FieldBoard(parts, (), np.array([0.0, 2.3]), zeros, zeros))
    z = lay.pack()
    loose = F.CourtyardClearance(lay, pair_min={(0, 1): 0.15}).value_grad(z)[0]
    tight = F.CourtyardClearance(lay, pair_min={(0, 1): 1.0}).value_grad(z)[0]
    assert tight > 10 * loose
    assert F.CourtyardClearance(lay, clearance=1.0).value_grad(z)[0] == tight
    # and the per-pair path is FD-checked on a generic board
    lay2 = F.Layout(_board(2, overlap=True))
    mins = {(i, j): 0.2 + 0.1 * (i + j) for i in range(5) for j in range(i + 1, 5)}
    term = F.CourtyardClearance(lay2, beta=6.0, pair_min=mins)
    z2 = lay2.pack()
    res = F.check_gradient(term.value_grad, z2, rng=np.random.default_rng(3), h=1e-5)
    assert res.dot_rel_err < 1e-4 and res.fd_max_rel_err < 1e-3, res


def test_pair_minimums_only_raise_the_fallback() -> None:
    def part(cls: str | None) -> F.Part:
        return F.Part(pads=np.zeros((1, 2)), court=np.zeros((3, 2)), cls=cls)

    parts = (part("SOT"), part("SOT"), part("BGA"), part("BGA"), part(None))
    mins = F.pair_minimums(parts, 1.2)
    assert mins == {(2, 3): 2.0}  # only BGA-BGA beats the fallback
    # SOT-SOT 0.4, SOT-BGA 1.0 and unknown-class pairs keep the term's default
    # courtyards already carry a reserve between two parts: it is subtracted
    inflated = F.pair_minimums(parts, 1.2, reserve=0.8)
    assert inflated == {(2, 3): pytest.approx(1.2)}


def test_board_from_engine_classes_are_known_or_none() -> None:
    built = F.board_from_engine(_engine())
    assert built is not None
    for part in built[0].parts:
        assert part.cls is None or part.cls in F.SPACING_CLASSES


# ── slice 2: wells T9/T10 ────────────────────────────────────────────────


def _descend(term: F.Term, lay: F.Layout, z: np.ndarray) -> np.ndarray:
    from scipy.optimize import minimize

    return minimize(
        term.value_grad, z, jac=True, method="L-BFGS-B", options={"maxiter": 300}
    ).x


@pytest.mark.parametrize("name", ["T9", "T10"])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_t9_t10_gradient_checks(name: str, seed: int) -> None:
    lay = F.Layout(_board(seed))
    term: F.Term = F.GridWell(lay) if name == "T9" else F.RotationWell(lay)
    z = lay.pack()
    assert term.value_grad(z)[0] > 0.0
    res = F.check_gradient(term.value_grad, z, rng=np.random.default_rng(seed), h=1e-5)
    assert res.dot_rel_err < 1e-4, res
    assert all(1.8 < o < 2.3 for o in res.taylor_orders), res
    assert res.fd_max_rel_err < 1e-3, res


def test_t9_coarse_then_fine_lands_on_coarse_multiples() -> None:
    """Harden's coarse-first ramp: the 10 mm well alone pulls every part
    onto the coarse raster; the full pitch set then holds it there."""
    sq = np.array([[-1.0, -1], [1, -1], [1, 1], [-1, 1]])
    parts = tuple(F.Part(pads=np.zeros((1, 2)), court=sq) for _ in range(4))
    lay = F.Layout(
        F.FieldBoard(
            parts,
            (),
            np.array([8.9, 11.2, 16.4, 23.7]),
            np.array([1.3, 7.8, 13.6, 19.9]),
            np.zeros(4),
        )
    )
    z = _descend(F.GridWell(lay, pitches={10.0: 1.0}), lay, lay.pack())
    z = _descend(F.GridWell(lay), lay, z)
    x, y, _th, _v = lay.unpack(z)
    np.testing.assert_allclose(x, np.round(x / 10.0) * 10.0, atol=1e-4)
    np.testing.assert_allclose(y, np.round(y / 10.0) * 10.0, atol=1e-4)


def test_t9_anchor_is_the_part_centre() -> None:
    """Pad 1 sits 2.34 mm off-centre; a bottom-side (mirrored-pad) or
    rotated part still lands its CENTRE on the raster — the pad does not."""
    pads = np.array([[2.34, 0.0]])
    sq = np.array([[-1.0, -1], [1, -1], [1, 1], [-1, 1]])
    for side, th, local in (
        (0, 0.0, pads),
        (1, 0.0, pads * [-1.0, 1.0]),  # board_from_engine's bottom mirror
        (0, np.pi, pads),
    ):
        part = F.Part(pads=local, court=sq, side=side)
        lay = F.Layout(
            F.FieldBoard((part,), (), np.array([8.9]), np.array([1.3]), np.array([th]))
        )
        z = _descend(F.GridWell(lay, pitches={10.0: 1.0}), lay, lay.pack())
        x, y, th_out, _v = lay.unpack(z)
        assert x[0] == pytest.approx(10.0, abs=1e-4)
        assert abs(y[0]) == pytest.approx(0.0, abs=1e-4)
        pad = F._world(part, local, x[0], y[0], th_out[0])[0]
        assert pad[0] % 10.0 != pytest.approx(0.0, abs=1e-3)


def test_t9_via_wells_pull_vias_onto_their_pitch() -> None:
    sq = np.array([[-1.0, -1], [1, -1], [1, 1], [-1, 1]])
    part = F.Part(pads=np.zeros((1, 2)), court=sq, fixed_xy=True, fixed_rot=True)
    lay = F.Layout(
        F.FieldBoard(
            (part,),
            (),
            np.array([3.0]),
            np.array([3.0]),
            np.zeros(1),
            vias0=np.array([[7.7, 2.2]]),
        )
    )
    term = F.GridWell(lay, via_pitches={1.0: 1.0})
    z = _descend(term, lay, lay.pack())
    _x, _y, _th, vias = lay.unpack(z)
    assert vias[0] == pytest.approx([8.0, 2.0], abs=1e-4)


def test_t10_preference_order_zero_then_180_then_90() -> None:
    """θ is a real variable: descent lands in the nearest well basin, with
    0° and 180° preferred over 90° — a start between 90 and 180 settles on
    180. Only 0/180 are exact minima of the summed wells (the coarse terms
    tilt every finer well toward 0), so the exact snap then puts θ on the
    15° grid."""
    sq = np.array([[-1.0, -1], [1, -1], [1, 1], [-1, 1]])
    parts = tuple(F.Part(pads=np.zeros((1, 2)), court=sq) for _ in range(5))
    lay = F.Layout(
        F.FieldBoard(
            parts,
            (),
            np.linspace(0.0, 40.0, 5),
            np.linspace(0.0, 40.0, 5),
            np.radians([5.0, 95.0, 170.0, 135.0, -175.0]),
        )
    )
    z = _descend(F.RotationWell(lay), lay, lay.pack())
    _x, _y, th, _v = lay.unpack(z)
    deg = np.degrees(th)
    snapped = np.round(deg / F.ROT_SNAP_DEG) * F.ROT_SNAP_DEG % 360.0
    np.testing.assert_allclose(snapped, [0.0, 90.0, 180.0, 180.0, 180.0], atol=1e-9)

    def well(d: float) -> float:
        return sum(
            w * (1.0 - np.cos(k * np.radians(d))) for k, w in F.ROT_WELL_WEIGHTS.items()
        )

    assert well(0.0) < well(180.0) < well(90.0)


def test_polish_snaps_onto_raster_and_rotation_wells() -> None:
    """The applied lane's own contract: which snap lane wins is legality-
    dependent (a raster snap that collides U3<>U4 falls to the θ-only
    lane, and the choice is platform-sensitive), so this pins what each
    lane must deliver — not one lane. ``raster+15deg``: centres on each
    part's own raster or clamped onto its bounds edge (the gate's box
    wins over the raster point), θ on the 15° grid; ``15deg``/``90deg``:
    θ snapped, x/y at the descent; ``seed-theta``: x/y at the descent,
    θ at the seed (the continuous descended θ is never written,
    gr503694)."""
    engine = _engine(2, 8)
    seed_rot = engine.ir.inst_rot.copy()
    res = F.polish_placement(engine)
    assert res.applied, res
    _assert_applied_pose_legal(engine, res)
    ir = engine.ir
    assert res.moved
    lane = res.detail.split(";", 1)[0]
    assert lane in {"raster+15deg", "15deg", "90deg", "seed-theta"}
    if lane == "raster+15deg":
        for i in res.moved:
            pitch = engine._preferred_snap_grid_mm(i)
            x0, y0, x1, y1 = (float(v) for v in engine.bounds_for(i))
            for v, lo, hi in (
                (float(ir.inst_x[i]), x0, x1),
                (float(ir.inst_y[i]), y0, y1),
            ):
                assert v == pytest.approx(round(v / pitch) * pitch, abs=1e-6) or v in (
                    lo,
                    hi,
                )
    if lane == "seed-theta":
        built = F.board_from_engine(engine)
        assert built is not None
        free_rot = F.Layout(built[0]).free_rot
        for i in res.moved:
            if free_rot[i]:
                assert float(ir.inst_rot[i]) == pytest.approx(
                    float(seed_rot[i]) % 360.0, abs=1e-6
                )
    else:
        snap_deg = F.ROT_SNAP_DEG if lane.endswith("15deg") else 90.0
        for i in res.moved:
            rot = float(ir.inst_rot[i])
            assert rot == pytest.approx(round(rot / snap_deg) * snap_deg, abs=1e-6)


# ── D14: supply-tree term ────────────────────────────────────────────────


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_supply_tree_gradient_checks(seed: int) -> None:
    rng = np.random.default_rng(seed)
    b = _board(seed)
    tree_nets = (tuple((i, int(rng.integers(3))) for i in (0, 2, 4)),)
    board = F.FieldBoard(b.parts, b.nets, b.x0, b.y0, b.th0, b.vias0, tree_nets, (1.0,))
    lay = F.Layout(board)
    term = F.SupplyTree(lay)
    z = lay.pack()
    assert term.value_grad(z)[0] > 0.0
    res = F.check_gradient(term.value_grad, z, rng=np.random.default_rng(seed), h=1e-5)
    assert res.dot_rel_err < 1e-4, res
    assert all(1.8 < o < 2.3 for o in res.taylor_orders), res
    assert res.fd_max_rel_err < 1e-3, res


# ── bin-density spread term (gr503556) ────────────────────────────────────


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_bin_density_gradient_checks(seed: int) -> None:
    """The Gaussian-kernel bin density is smooth everywhere, so the check
    applies at any point — including a clustered one where the term is
    actually loaded (a symmetric or empty start would leave it idle)."""
    lay = F.Layout(_board(seed, overlap=True))
    term = F.BinDensity(lay, (0.0, 0.0), (8.0, 8.0), bins=6)
    z = lay.pack()
    res = F.check_gradient(term.value_grad, z, rng=np.random.default_rng(seed), h=1e-5)
    assert res.dot_rel_err < 1e-4, res
    assert all(1.8 < o < 2.3 for o in res.taylor_orders), res
    assert res.fd_max_rel_err < 1e-3, res


def test_bin_density_spreads_a_clustered_board() -> None:
    """A corner-packed board descends to a lower density energy with the
    parts visibly spread. T4 rides along in the mix: coincident or
    near-coincident parts share one density gradient and can never split
    on it alone — the courtyard term is what breaks the degeneracy in the
    real energy, so the test mirrors that."""
    sq = np.array([[-1.0, -1], [1, -1], [1, 1], [-1, 1]])
    n = 6
    parts = tuple(F.Part(pads=np.zeros((1, 2)), court=sq) for _ in range(n))
    packed = F.FieldBoard(
        parts,
        (),
        x0=np.array([5.0, 6.0, 5.0, 7.0, 6.0, 5.0]),
        y0=np.array([5.0, 5.0, 6.0, 5.0, 7.0, 6.0]),
        th0=np.zeros(n),
    )
    lay = F.Layout(packed)
    term = F.BinDensity(lay, (0.5, 0.5), (58.5, 58.5), bins=8)
    z = lay.pack()
    v0, g0 = term.value_grad(z)
    assert v0 > 0.0 and np.linalg.norm(g0) > 0.0
    both = F.Energy(
        lay,
        [
            (1.0, term),
            (4.0, F.CourtyardClearance(lay, beta=6.0)),
            (1.0, F.BoardEdge(lay, (0.5, 0.5), (58.5, 58.5), beta=6.0)),
        ],
    )
    z1 = _descend(both, lay, z)
    v1 = term.value_grad(z1)[0]
    assert v1 < v0 * 0.5
    x, y, _t, _v = lay.unpack(z1)
    assert np.ptp(x) > 10.0 and np.ptp(y) > 10.0


def test_bin_density_uniform_fill_scores_low() -> None:
    """A spread board prices far below a clustered one — E≈∫ρ² is
    minimized by uniform density."""
    sq = np.array([[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]])
    parts = tuple(F.Part(pads=np.zeros((1, 2)), court=sq) for _ in range(4))
    spread = F.FieldBoard(
        parts,
        (),
        x0=np.array([10.0, 30.0, 10.0, 30.0]),
        y0=np.array([10.0, 10.0, 30.0, 30.0]),
        th0=np.zeros(4),
    )
    lay = F.Layout(spread)
    term = F.BinDensity(lay, (0.5, 0.5), (39.5, 39.5), bins=4)
    v_spread = term.value_grad(lay.pack())[0]
    packed = F.FieldBoard(
        parts,
        (),
        # coincident — σ ≈ one cell, so the kernel smears even a point
        # cluster over ~3 bins and the contrast is ~2.4x, not 4x
        x0=np.full(4, 19.5),
        y0=np.full(4, 19.5),
        th0=np.zeros(4),
    )
    lay2 = F.Layout(packed)
    term2 = F.BinDensity(lay2, (0.5, 0.5), (39.5, 39.5), bins=4)
    v_packed = term2.value_grad(lay2.pack())[0]
    assert v_packed > 2.0 * v_spread


# ── gr503694: written rotations are grid values, never the descended θ ──


def _assert_written_rots_valid(
    engine: OptimizeEngine, res: F.PolishResult, seed_rot: np.ndarray
) -> None:
    """Every rotation the polish WROTE is in [0, 360) and a multiple of
    15° — or the part's own seed θ restored by the fallback lane."""
    ir = engine.ir
    for i in res.moved:
        rot = float(ir.inst_rot[i])
        assert 0.0 <= rot < 360.0, f"part {i}: rot {rot} out of range"
        off = min(rot % 15.0, 15.0 - rot % 15.0)
        seeded = abs(rot - float(seed_rot[i]) % 360.0) < 1e-6
        assert off < 1e-6 or seeded, f"part {i}: rot {rot} off the 15° grid"


@pytest.mark.parametrize(("seed", "n"), [(1, 8), (2, 6), (7, 8), (2, 8)])
def test_polish_writes_only_grid_or_seed_rotations(seed: int, n: int) -> None:
    engine = _engine(seed, n)
    seed_rot = engine.ir.inst_rot.copy()
    res = F.polish_placement(engine)
    _assert_written_rots_valid(engine, res, seed_rot)
    # …and nothing outside [0, 360) exists afterwards, written or not.
    assert ((engine.ir.inst_rot >= 0.0) & (engine.ir.inst_rot < 360.0)).all()


def test_descended_wrapped_theta_is_never_written(monkeypatch) -> None:
    """gr503694's 494.87° case: the descent's continuous θ can wind far
    past 2π (U1 494.87°, R1 536.2°, C11 −88.56° on the esp32c3 dogfood).
    Winding is representation-invisible to the energy (every term reads
    θ through cos/sin) — this monkeypatch adds the same windings to every
    unpacked θ, so the descent is identical but the descended θ is
    wrapped. Before the fix the repair fallback wrote those values raw;
    now every written rotation is snapped or seed-restored and
    normalised to [0, 360)."""
    engine = _engine(1, 8)
    seed_rot = engine.ir.inst_rot.copy()
    winds = np.array([-494.87, -88.56, 324.05, 536.22, 404.87, -210.5, 77.7, -360.9])
    real_unpack = F.Layout.unpack

    def wound(self: F.Layout, z: np.ndarray):
        x, y, th, v = real_unpack(self, z)
        th = th.copy()
        th[self.free_rot] += np.radians(winds[: int(self.free_rot.sum())])
        return x, y, th, v

    monkeypatch.setattr(F.Layout, "unpack", wound)
    res = F.polish_placement(engine)
    _assert_written_rots_valid(engine, res, seed_rot)
    assert ((engine.ir.inst_rot >= 0.0) & (engine.ir.inst_rot < 360.0)).all()


def test_gate_refusal_falls_back_to_seed_theta(monkeypatch) -> None:
    """gr503694 fix 1: when every snapped pose fails the gate, the
    fallback restores that part's SEED θ — never the descended continuous
    θ. Simulated here by a legality gate that refuses every rotated
    proposal, so only the seed-theta lane can ever apply."""
    engine = _engine(1, 8)
    ir = engine.ir
    seed_rot = ir.inst_rot.copy()
    real_legal = engine._placement_is_legal

    def seed_rot_only(proposals, rotations=None, in_outline=False):
        for i, rot in (rotations or {}).items():
            d = abs((rot - float(seed_rot[i])) % 360.0)
            if min(d, 360.0 - d) > 1e-3:
                return False
        return real_legal(proposals, rotations=rotations, in_outline=in_outline)

    monkeypatch.setattr(engine, "_placement_is_legal", seed_rot_only)
    res = F.polish_placement(engine)
    for i in res.moved:
        assert float(ir.inst_rot[i]) == pytest.approx(
            float(seed_rot[i]) % 360.0, abs=1e-6
        )
    if res.applied:
        assert res.detail.startswith("seed-theta")


def test_wirelength_worse_basin_is_refused(monkeypatch) -> None:
    """The sanity-board platform regression: a descent that lands in a
    basin where density/courtyard gains hide a wirelength regression
    (legal and lower-E, but the ratsnest is longer) must not commit —
    POLISH_WIRELENGTH_WORSEN is the gate. n=6 seed=2's part 5 dragged to
    (10.8, 6.0) — a point on its own 0.1 mm raster, so every cascade lane
    evaluates the same pose — is legal, E 75.79 -> 73.57, but T1+tree
    42.79 -> 45.00 (+5 %): refused, and the board is left exactly as it
    was."""
    engine = _engine(2, 6)
    ir = engine.ir
    built = F.board_from_engine(engine)
    assert built is not None
    lay = F.Layout(built[0])
    x, y, th, _v = lay.unpack(lay.pack())
    x[5], y[5] = 10.8, 6.0
    z_bad = lay.pack_from(x, y, th)

    class _Res:
        status = 0

        def __init__(self, xx: np.ndarray):
            self.x = xx

    monkeypatch.setattr(F, "minimize", lambda fg, z, **kw: _Res(z_bad))
    poses0 = (ir.inst_x.copy(), ir.inst_y.copy(), ir.inst_rot.copy())
    res = F.polish_placement(engine)
    assert not res.applied, res
    assert res.reason == "wirelength_worse", res
    assert np.array_equal(ir.inst_x, poses0[0])
    assert np.array_equal(ir.inst_y, poses0[1])
    assert np.array_equal(ir.inst_rot, poses0[2])


def test_supply_tree_p2u_keeps_u1_near_the_hub() -> None:
    """gr503459: T1 drops every rail/plane net, so without the D14 tree
    U1's pins see no pull at all and p2u's polish left it parked past the
    hub. With the tree, VIN/VOUT/GND pull it onto the J_IN → HUB corridor
    it sits on topologically."""
    from precis.pcb import sanity_power as sp

    built = sp.build(sp.PENDING["p2u"])
    engine = OptimizeEngine(built.ir, OptimizeConfig(seed=1))
    ir = engine.ir
    refdes = [str(r) for r in ir.instance_refdes]
    u1, hub, j_in = (refdes.index(r) for r in ("U1", "HUB", "J_IN"))
    # Pin U1's start: where the constructive seed happens to drop it is
    # not the question — the spread seed (gr503556) legitimately parks it
    # in a far corner. The tree must pull it onto the corridor from a
    # mid-field start between J_IN and HUB.
    ir.move_instance(u1, x=45.0, y=80.0)
    res = F.polish_placement(engine)
    assert res.applied, res
    ux, uy = float(ir.inst_x[u1]), float(ir.inst_y[u1])
    hx, hy = float(ir.inst_x[hub]), float(ir.inst_y[hub])
    assert float(ir.inst_x[j_in]) < ux < hx
    assert abs(uy - hy) < 5.0


# ── tilt resolution: an off-90° θ must earn its keep at the written pose ──


def _tilt_state(rot_deg: list[float], seed_deg: list[float]):
    rot = np.array(rot_deg, dtype=float)
    ths = -np.radians(rot)
    th0 = -np.radians(np.array(seed_deg, dtype=float))
    return ths, rot, th0


def test_a_part_left_at_15_deg_with_no_gain_snaps_back_to_the_90_grid():
    """A polish pose with one part at 15° and nothing earned by the tilt
    (a flat energy) writes 0°, not 15°."""
    ths, rot, th0 = _tilt_state([15.0, 90.0], [0.0, 90.0])
    out_ths, out_rot = F.resolve_tilts(
        lambda _t: 1.0, ths, rot, free_rot=np.array([True, True]), th0=th0
    )
    assert out_rot.tolist() == [0.0, 90.0]
    assert out_ths[0] == pytest.approx(0.0)


def test_a_tilt_short_of_the_min_gain_snaps_and_one_past_it_stays():
    """The bar is ROT_TILT_MIN_GAIN, measured at the pose being written:
    a 15° part whose tilt saves 0.9x the bar goes to 0°, 1.1x stays."""

    def priced(saving: float):
        def energy_of(t: np.ndarray) -> float:
            return 100.0 - (saving if abs(t[0] + np.radians(15.0)) < 1e-9 else 0.0)

        return energy_of

    for frac, expect in ((0.9, 0.0), (1.1, 15.0)):
        ths, rot, th0 = _tilt_state([15.0], [0.0])
        _ths, out_rot = F.resolve_tilts(
            priced(frac * F.ROT_TILT_MIN_GAIN),
            ths,
            rot,
            free_rot=np.array([True]),
            th0=th0,
        )
        assert out_rot.tolist() == [expect], frac


def test_a_part_at_its_seed_theta_or_locked_is_never_resnapped():
    """15° at the part's own seed θ is intent, not a polish tilt; a
    rotation-locked part is not the polish's to move."""
    ths, rot, th0 = _tilt_state([15.0, 30.0], [15.0, 0.0])
    _ths, out_rot = F.resolve_tilts(
        lambda _t: 1.0, ths, rot, free_rot=np.array([True, False]), th0=th0
    )
    assert out_rot.tolist() == [15.0, 30.0]
