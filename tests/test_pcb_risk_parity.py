"""Parity: the annealer's incremental ``risk()`` (per-criticality-weight
running totals of ``hardened_penalty``'s schedule-affine coefficients)
must agree within an absolute tolerance with a from-scratch sum over
every cached entry. Repeated fixed-seed anneals driven by incremental
risk must produce identical moves, final positions, and total cost."""

from __future__ import annotations

import random

import pytest

import precis.pcb.optimize as opt
from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import from_graph
from precis.pcb.optimize import OptimizeConfig, OptimizeEngine, seed_placement
from tests.test_pcb_optimize import _board


def _scanned_risk(self: OptimizeEngine) -> float:
    """The from-scratch sum over every cached entry, recomputed at the
    current schedule — the reference the incremental running totals must
    match."""
    return sum(
        opt._CRITICALITY_WEIGHT[opt._BY_NAME[name].criticality]
        * opt.hardened_penalty(tv.raw, self.schedule)
        for (name, _key), tv in self._margin.items()
    )


def _run(n: int, iters: int, seed: int):
    ir = from_graph(_board(n, seed=seed), stackup=DEFAULT_STACKUP)
    seed_placement(ir, random.Random(seed))
    engine = OptimizeEngine(ir, OptimizeConfig(seed=seed, iters=iters))
    engine.anneal(random.Random(seed))
    pos = [
        (float(ir.inst_x[i]), float(ir.inst_y[i]), float(ir.inst_rot[i]))
        for i in range(ir.n_instances)
    ]
    moves = [(m.kind, m.instances, m.accepted, m.delta) for m in engine.moves]
    return engine.total(), pos, moves


@pytest.mark.parametrize("n,iters,seed", [(10, 150, 1), (24, 200, 7)])
def test_fixed_seed_incremental_anneal_is_deterministic_and_matches_scanned_risk(
    monkeypatch: pytest.MonkeyPatch, n: int, iters: int, seed: int
) -> None:
    real_risk = OptimizeEngine.risk

    def _checked_risk(self: OptimizeEngine) -> float:
        incremental = real_risk(self)
        assert incremental == pytest.approx(_scanned_risk(self), abs=1e-9, rel=0)
        return incremental

    monkeypatch.setattr(OptimizeEngine, "risk", _checked_risk)
    total, pos, moves = _run(n, iters, seed)
    total2, pos2, moves2 = _run(n, iters, seed)
    assert (total, pos, moves) == (total2, pos2, moves2)


def test_risk_matches_reference_at_varied_schedules() -> None:
    ir = from_graph(_board(16, seed=3), stackup=DEFAULT_STACKUP)
    seed_placement(ir, random.Random(3))
    engine = OptimizeEngine(ir, OptimizeConfig(seed=3, iters=50))
    engine.anneal(random.Random(3))
    for sched in (0.0, 0.25, 0.5, 0.9, 1.0):
        engine.schedule = sched
        assert engine.risk() == pytest.approx(_scanned_risk(engine), abs=1e-9, rel=0)
