"""Parity: the annealer's ``risk()`` aggregation (per-name peak raw, one
penalty eval per term name) must equal the original per-entry max, and a
fixed-seed full anneal must be identical with either implementation —
same accepted-move sequence, same final placement, same total."""

from __future__ import annotations

import random

import pytest

import precis.pcb.optimize as opt
from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import from_graph
from precis.pcb.optimize import OptimizeConfig, OptimizeEngine, seed_placement
from tests.test_pcb_optimize import _board


def _old_risk(self: OptimizeEngine) -> float:
    """The pre-optimisation ``risk()``, copied verbatim."""
    if not self._margin:
        return 0.0
    return max(
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
def test_fixed_seed_anneal_identical_to_reference_risk(
    monkeypatch: pytest.MonkeyPatch, n: int, iters: int, seed: int
) -> None:
    new = _run(n, iters, seed)
    monkeypatch.setattr(OptimizeEngine, "risk", _old_risk)
    old = _run(n, iters, seed)
    assert new == old  # exact: total, every position, every move + delta


def test_risk_matches_reference_at_varied_schedules() -> None:
    ir = from_graph(_board(16, seed=3), stackup=DEFAULT_STACKUP)
    seed_placement(ir, random.Random(3))
    engine = OptimizeEngine(ir, OptimizeConfig(seed=3, iters=50))
    engine.anneal(random.Random(3))
    for sched in (0.0, 0.25, 0.5, 0.9, 1.0):
        engine.schedule = sched
        assert engine.risk() == _old_risk(engine)
