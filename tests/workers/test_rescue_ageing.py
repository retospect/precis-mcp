"""Rescue passes keep a bounded wait under a saturated rotation.

Repro for ``docs/backlog/worker-rotation-starves-rescue-passes.md``
(melchior 2026-08-14): ``run_loop`` is strictly serial, so a rescue pass
(``sweeper`` / ``nursery`` / ``quest_loop_reconcile``) waits a full
rotation between runs — the sum of every other pass's duration, with no
bound when long job and background passes always have work.

Deterministic: ``run_loop``'s ``time`` module is swapped for a simulated
clock that only the passes advance, and every non-rescue pass always
reports work (a flood that never drains), so the measured gap is a pure
function of the pass durations below.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import pairwise
from typing import Any

import pytest

from precis.workers import runner
from precis.workers.runner import BatchResult, RefPass, run_loop

_MIN = 60.0
#: ``runner.DEFAULT_RESCUE_BUDGET_S`` — literal so the repro also runs on main.
_DEFAULT_BUDGET_S = 5 * _MIN
#: ``run_loop`` gets no chunk handlers here, so it never touches the store.
_NO_STORE: Any = None


class _SimTime:
    """Stand-in for the ``time`` module inside ``runner``: a clock only
    advanced by the simulated passes (and by idle sleeps, which a
    saturated rotation never takes)."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _pass(name: str, sim: _SimTime, duration_s: float, claimed: int) -> RefPass:
    def fn(batch_size: int) -> BatchResult:
        sim.now += duration_s
        return BatchResult(handler=name, claimed=claimed, ok=claimed, failed=0)

    fn.__name__ = f"_{name}_pass"
    return fn


def _rescue(name: str, sim: _SimTime, runs: list[float]) -> RefPass:
    def fn(batch_size: int) -> BatchResult:
        runs.append(sim.now)
        sim.now += 1.0
        return BatchResult(handler=name, claimed=0, ok=0, failed=0)

    fn.__name__ = f"_{name}_pass"
    return fn


def _max_gap(runs: list[float], horizon_s: float) -> float:
    """Longest stretch with no run, including the lead-in from t=0."""
    edges = [0.0, *runs, horizon_s]
    return max(b - a for a, b in pairwise(edges))


def _simulate(
    monkeypatch: pytest.MonkeyPatch, **loop_kwargs: Any
) -> tuple[dict[str, list[float]], float, float]:
    sim = _SimTime()
    monkeypatch.setattr(runner, "time", sim)
    monkeypatch.delenv("PRECIS_WORKER_RESCUE_BUDGET_S", raising=False)
    horizon_s = 24 * 60 * _MIN
    runs: dict[str, list[float]] = {
        "sweeper": [],
        "nursery": [],
        "quest_loop_reconcile": [],
    }
    # Melchior's 2026-08-14 shape: the job lane opens each rotation with a
    # multi-job claude_inproc batch, then back-to-back background batches.
    # The rescue passes sit where the priority sort puts them.
    long_passes = [
        _pass("job_claude_inproc", sim, 45 * _MIN, 4),
        _pass("llm_summarize", sim, 36 * _MIN, 32),
        _pass("fetch", sim, 30 * _MIN, 32),
        _pass("chase", sim, 30 * _MIN, 32),
        _pass("bib_parse", sim, 30 * _MIN, 32),
    ]
    longest_s = 45 * _MIN
    ref_passes: list[RefPass] = [
        long_passes[0],
        _rescue("sweeper", sim, runs["sweeper"]),
        _rescue("nursery", sim, runs["nursery"]),
        _rescue("quest_loop_reconcile", sim, runs["quest_loop_reconcile"]),
        *long_passes[1:],
    ]
    stop: Callable[[], bool] = lambda: sim.now >= horizon_s
    run_loop(
        [],
        store=_NO_STORE,
        ref_passes=ref_passes,
        should_stop=stop,
        **loop_kwargs,
    )
    return runs, horizon_s, longest_s


def test_rescue_passes_wait_is_bounded_under_flood(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every rescue pass runs within its budget plus one in-flight pass,
    however long the full rotation is (here ~171 min)."""
    runs, horizon_s, longest_s = _simulate(monkeypatch)
    bound_s = _DEFAULT_BUDGET_S + longest_s
    gaps = {name: _max_gap(r, horizon_s) for name, r in runs.items()}
    for name, gap in gaps.items():
        assert gap <= bound_s, (
            f"{name} max wait {gap / _MIN:.0f} min > bound {bound_s / _MIN:.0f} min "
            f"(all gaps, min: { {k: round(v / _MIN) for k, v in gaps.items()} })"
        )


def test_rescue_budget_zero_restores_strict_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``rescue_budget_s=0`` turns the interleave off: rescue passes run
    only in their rotation slot, once per full rotation."""
    runs, horizon_s, _ = _simulate(monkeypatch, rescue_budget_s=0)
    gap = _max_gap(runs["sweeper"], horizon_s)
    assert gap >= 171 * _MIN


def test_rescue_pass_not_rerun_while_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle-fast rotation runs each rescue pass once per cycle, in its
    slot — the interleave only fires once a pass is overdue."""
    sim = _SimTime()
    monkeypatch.setattr(runner, "time", sim)
    runs: list[float] = []
    cycles = 0

    def _tick_pass(batch_size: int) -> BatchResult:
        nonlocal cycles
        cycles += 1
        sim.now += 1.0
        return BatchResult(handler="tick", claimed=1, ok=1, failed=0)

    run_loop(
        [],
        store=_NO_STORE,
        ref_passes=[_tick_pass, _rescue("sweeper", sim, runs)],
        should_stop=lambda: cycles >= 50,
    )
    assert len(runs) == 49  # one per completed cycle, never doubled
