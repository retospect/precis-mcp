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


def test_field_polish_flag_is_off_by_default_and_runs_when_on() -> None:
    from tests.test_pcb_optimize import _seeded_ir

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
        res = optimize(ir, OptimizeConfig(seed=2))
        assert not any(s.startswith("field_polish ") for s in res.support_groups)
        poses.append((ir.inst_x.copy(), ir.inst_y.copy(), ir.inst_rot.copy()))
    for a, b in zip(poses[0], poses[1], strict=True):
        assert np.array_equal(a, b)
