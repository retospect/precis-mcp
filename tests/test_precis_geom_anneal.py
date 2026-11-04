"""Generic annealing: convergence, seeded replay and reversible moves."""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from precis_geom.optimize import AnnealResult, anneal, geometric_schedule


@dataclass
class QuadraticMoves:
    x: float = 8.0
    y: float = -6.0
    proposals: list[tuple[float, float]] = field(default_factory=list)

    def snapshot(self) -> tuple[float, float]:
        return self.x, self.y

    def restore(self, state: tuple[float, float]) -> None:
        self.x, self.y = state

    def propose(self, scale: float, rng: random.Random) -> None:
        self.x += rng.uniform(-4.0, 4.0) * scale
        self.y += rng.uniform(-4.0, 4.0) * scale
        self.proposals.append(self.snapshot())

    def energy(self) -> float:
        return (self.x - 1.0) ** 2 + 2.0 * (self.y + 2.0) ** 2


def _quadratic(seed: int) -> tuple[AnnealResult[tuple[float, float]], QuadraticMoves]:
    moves = QuadraticMoves()
    result = anneal(
        moves,
        moves.energy,
        geometric_schedule(4.0, cooling=0.97, steps=300, iters_per_temp=20),
        random.Random(seed),
    )
    return result, moves


def test_quadratic_converges_on_average_over_five_seeds() -> None:
    results = [_quadratic(seed) for seed in range(5)]
    assert sum(result.energy for result, _ in results) / 5 < 0.01
    for result, moves in results:
        assert result.energy < QuadraticMoves().energy()
        assert moves.snapshot() == result.state
        assert moves.energy() == result.energy
        assert result.iterations == 6000


def test_same_seed_replays_proposals_and_result_without_global_randomness() -> None:
    global_state = random.getstate()
    first, first_moves = _quadratic(42)
    second, second_moves = _quadratic(42)
    assert first == second
    assert first_moves.proposals == second_moves.proposals
    assert random.getstate() == global_state
    _, other_moves = _quadratic(43)
    assert first_moves.proposals != other_moves.proposals


@dataclass
class ScriptedMoves:
    proposals: Iterator[float]
    value: float = 2.0
    starts: list[float] = field(default_factory=list)
    scales: list[float] = field(default_factory=list)

    def snapshot(self) -> float:
        return self.value

    def restore(self, state: float) -> None:
        self.value = state

    def propose(self, scale: float, rng: random.Random) -> None:
        self.starts.append(self.value)
        self.scales.append(scale)
        self.value = next(self.proposals)

    def energy(self) -> float:
        return self.value


def test_metropolis_accepts_uphill_then_restores_best() -> None:
    moves = ScriptedMoves(iter([0.0, 1.0, 10.0, -1.0]))
    # Random(1)'s first draw accepts exp(-1); its second rejects exp(-9).
    result = anneal(moves, moves.energy, [1.0] * 4, random.Random(1))
    assert moves.starts == [2.0, 0.0, 1.0, 1.0]
    assert result == AnnealResult(-1.0, -1.0, 4, 3)
    assert moves.value == -1.0

    moves = ScriptedMoves(iter([0.0, 1.0]))
    result = anneal(moves, moves.energy, [1.0, 1.0], random.Random(1))
    assert result.accepted == 2
    assert moves.value == result.state == 0.0


def test_large_finite_energies_preserve_uphill_acceptance_probability() -> None:
    moves = ScriptedMoves(iter([1e308]), value=-1e308)
    result = anneal(moves, moves.energy, [1e308], random.Random(1))
    # exp(-2) exceeds Random(1)'s first draw, despite the raw delta overflowing.
    assert result.accepted == 1
    assert result.energy == moves.value == -1e308


def test_temperature_scaling_and_zero_temperature() -> None:
    moves = ScriptedMoves(iter([1.0, 0.0, 1.0, 0.0]))
    result = anneal(moves, moves.energy, [2.0, 1.0, 0.0, 0.0], random.Random(0))
    assert moves.scales == [1.0, 0.5, 0.0, 0.0]
    assert moves.starts == [2.0, 1.0, 0.0, 0.0]
    assert result.accepted == 3  # downhill and equal accepted, uphill rejected

    moves = ScriptedMoves(iter([1.0]))
    result = anneal(moves, moves.energy, [0.0], random.Random(0))
    assert moves.scales == [0.0]
    assert result.energy == 1.0


def test_empty_schedule_keeps_initial_state_and_rng() -> None:
    moves = ScriptedMoves(iter([]))
    rng = random.Random(0)
    before = rng.getstate()
    assert anneal(moves, moves.energy, [], rng) == AnnealResult(2.0, 2.0, 0, 0)
    assert rng.getstate() == before


def test_infeasible_proposal_is_rejected_and_restored() -> None:
    moves = ScriptedMoves(iter([math.inf, 1.0]))
    result = anneal(moves, moves.energy, [1.0, 1.0], random.Random(0))
    assert moves.starts == [2.0, 2.0]
    assert result == AnnealResult(1.0, 1.0, 2, 1)


@pytest.mark.parametrize("value", [math.nan, -math.inf])
def test_invalid_proposal_energy_restores_state(value: float) -> None:
    moves = ScriptedMoves(iter([value]))
    with pytest.raises(ValueError, match="proposal energy"):
        anneal(moves, moves.energy, [1.0], random.Random(0))
    assert moves.value == 2.0


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_initial_energy_must_be_finite(value: float) -> None:
    moves = ScriptedMoves(iter([]), value=value)
    with pytest.raises(ValueError, match="initial energy"):
        anneal(moves, moves.energy, [], random.Random(0))


@pytest.mark.parametrize("temperature", [-1.0, math.nan, math.inf])
def test_invalid_temperature_refuses_before_proposal(temperature: float) -> None:
    moves = ScriptedMoves(iter([0.0]))
    with pytest.raises(ValueError, match="schedule temperatures"):
        anneal(moves, moves.energy, [temperature], random.Random(0))
    assert not moves.starts


def test_evaluation_failure_restores_previous_state() -> None:
    moves = ScriptedMoves(iter([0.0]))

    def energy() -> float:
        if moves.value == 0.0:
            raise RuntimeError("solver failed")
        return moves.value

    with pytest.raises(RuntimeError, match="solver failed"):
        anneal(moves, energy, [1.0], random.Random(0))
    assert moves.value == 2.0


def test_geometric_cooling_repeats_each_temperature() -> None:
    assert list(geometric_schedule(8.0, cooling=0.5, steps=3, iters_per_temp=2)) == [
        8.0,
        8.0,
        4.0,
        4.0,
        2.0,
        2.0,
    ]
    assert list(geometric_schedule(1.0, steps=0)) == []


@pytest.mark.parametrize("initial", [0.0, -1.0, math.nan, math.inf])
def test_invalid_initial_temperature(initial: float) -> None:
    with pytest.raises(ValueError, match="initial temperature"):
        list(geometric_schedule(initial))


@pytest.mark.parametrize("cooling", [0.0, 1.0, -1.0, math.nan, math.inf])
def test_invalid_cooling(cooling: float) -> None:
    with pytest.raises(ValueError, match="cooling"):
        list(geometric_schedule(1.0, cooling=cooling))


@pytest.mark.parametrize(("steps", "iters_per_temp"), [(-1, 1), (1, 0)])
def test_invalid_schedule_counts(steps: int, iters_per_temp: int) -> None:
    with pytest.raises(ValueError, match="steps"):
        list(geometric_schedule(1.0, steps=steps, iters_per_temp=iters_per_temp))
