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
    for seed in (1, 2, 3):
        engine = _engine(seed)
        before = engine.total()
        res = F.polish_placement(engine)
        assert res.reason in {
            "applied",
            "no_gain",
            "illegal",
            "cost_worse",
            "no_free_vars",
            "bad_gradient",
        }
        assert engine.total() <= before + 1e-9
        if res.applied:
            assert res.e_after < res.e_before


@pytest.mark.parametrize(("n", "seed"), [(6, 1), (8, 7)])
def test_polish_accept_path_lowers_cost_and_stays_legal(n: int, seed: int) -> None:
    """Seeded boards the polish is known to improve: the accept path runs,
    the engine's own cost drops, and the resulting pose passes the legality
    predicate."""
    engine = _engine(seed, n)
    before = engine.total()
    res = F.polish_placement(engine)
    assert res.applied, res
    assert engine.total() < before
    ir = engine.ir
    proposals = [(i, float(ir.inst_x[i]), float(ir.inst_y[i])) for i in res.moved]
    rotations = {i: float(ir.inst_rot[i]) for i in res.moved}
    assert engine._placement_is_legal(proposals, rotations=rotations)


def test_field_polish_flag_is_on_by_default_and_runs() -> None:
    from tests.test_pcb_optimize import _seeded_ir

    assert OptimizeConfig().field_polish is True
    ir = _seeded_ir(6, graph_seed=2, seed_rng_seed=2)
    res = optimize(ir, OptimizeConfig(seed=2))
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
        optimize(ir, OptimizeConfig(seed=2))
        poses.append((ir.inst_x.copy(), ir.inst_y.copy(), ir.inst_rot.copy()))
    for a, b in zip(poses[0], poses[1], strict=True):
        assert np.array_equal(a, b)


def test_polish_repairs_a_snap_illegal_pose() -> None:
    """n=6 seed=2 descended into a pose the 90° snap made illegal (U0 out of
    its bounds box, U1<>U4 courtyard overlap) — the repair descent must
    satisfy the gate rather than refuse."""
    engine = _engine(2, 6)
    res = F.polish_placement(engine)
    assert res.applied, res
    ir = engine.ir
    proposals = [(i, float(ir.inst_x[i]), float(ir.inst_y[i])) for i in res.moved]
    rotations = {i: float(ir.inst_rot[i]) for i in res.moved}
    assert engine._placement_is_legal(proposals, rotations=rotations)


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
