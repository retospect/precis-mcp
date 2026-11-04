"""Generic Metropolis minimisation with reversible, caller-defined moves."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol


class RandomSource(Protocol):
    """The acceptance draw supported by Random and numpy Generator."""

    def random(self) -> float:
        """Return a uniform draw in [0, 1)."""
        ...


class Moves[State, Rng: RandomSource](Protocol):
    """Mutable search state; snapshots must survive later proposals/restores.

    ``propose`` mutates the live state using the supplied RNG and temperature
    ratio. It owns proposal size and domain constraints. Metropolis assumes
    symmetric proposals; no Hastings correction is applied.
    """

    def snapshot(self) -> State:
        """Return a detached snapshot of the current state."""
        ...

    def restore(self, state: State) -> None:
        """Restore without mutating or retaining mutable aliases to state."""
        ...

    def propose(self, scale: float, rng: Rng) -> None:
        """Propose one move at scale T/T0 using the supplied RNG."""
        ...


@dataclass(frozen=True)
class AnnealResult[State]:
    """Best snapshot and energy, plus proposal and acceptance counts."""

    state: State
    energy: float
    iterations: int
    accepted: int


def geometric_schedule(
    initial: float,
    *,
    cooling: float = 0.97,
    steps: int = 100,
    iters_per_temp: int = 3,
) -> Iterator[float]:
    """Yield ``initial * cooling**step`` once per proposal, finitely.

    ``steps`` counts temperature levels; each is repeated ``iters_per_temp``
    times. Validation happens when iteration begins.
    """
    if not math.isfinite(initial) or initial <= 0:
        raise ValueError("initial temperature must be finite and positive")
    if not math.isfinite(cooling) or not 0 < cooling < 1:
        raise ValueError("cooling must be finite and between 0 and 1")
    if steps < 0 or iters_per_temp < 1:
        raise ValueError("steps must be nonnegative and iters_per_temp positive")
    for step in range(steps):
        temperature = initial * cooling**step
        for _ in range(iters_per_temp):
            yield temperature


def anneal[State, Rng: RandomSource](
    moves: Moves[State, Rng],
    energy: Callable[[], float],
    schedule: Iterable[float],
    rng: Rng,
) -> AnnealResult[State]:
    """Minimise energy and restore the best state before returning.

    ``energy()`` evaluates the live state without mutating it. Initial energy
    must be finite; a proposal's positive infinity means infeasible and is
    rejected. NaN and negative infinity raise ValueError. Proposal/evaluation
    failures restore the preceding state and propagate to the caller.

    ``schedule`` supplies one finite, nonnegative temperature per proposal.
    Moves receive its ratio to the first temperature (zero if T0 is zero).
    At zero temperature only downhill and equal-energy moves are accepted.
    Empty schedules return the initial state without any random draw.
    Reproducibility requires deterministic callbacks and the same RNG state.
    """
    current_energy = float(energy())
    if not math.isfinite(current_energy):
        raise ValueError("initial energy must be finite")
    best_state = moves.snapshot()
    best_energy = current_energy
    initial_temperature: float | None = None
    iterations = accepted = 0

    for temperature in schedule:
        if not math.isfinite(temperature) or temperature < 0:
            raise ValueError("schedule temperatures must be finite and nonnegative")
        if initial_temperature is None:
            initial_temperature = temperature
        scale = temperature / initial_temperature if initial_temperature else 0.0
        previous = moves.snapshot()
        try:
            moves.propose(scale, rng)
            candidate_energy = float(energy())
            if math.isnan(candidate_energy) or candidate_energy == -math.inf:
                raise ValueError("proposal energy must be finite or positive infinity")
            feasible = math.isfinite(candidate_energy)
            accept = feasible and candidate_energy <= current_energy
            if feasible and not accept and temperature > 0:
                delta = candidate_energy - current_energy
                # Opposite-sign finite energies can overflow on subtraction
                # even when the temperature-scaled difference is small.
                penalty = (
                    delta / temperature
                    if math.isfinite(delta)
                    else candidate_energy / temperature - current_energy / temperature
                )
                accept = rng.random() < math.exp(-penalty)
        except Exception:
            moves.restore(previous)
            raise
        iterations += 1
        if accept:
            accepted += 1
            current_energy = candidate_energy
            if current_energy < best_energy:
                best_state = moves.snapshot()
                best_energy = current_energy
        else:
            moves.restore(previous)

    moves.restore(best_state)
    return AnnealResult(best_state, best_energy, iterations, accepted)
