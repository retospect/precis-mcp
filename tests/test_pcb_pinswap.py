"""Unit tests for precis.pcb.pinswap's Hungarian and cycle-decomposition
primitives (the crossing-count matcher was removed with MoveKind.PIN_SWAP;
supply/client matching is tested in tests/test_pcb_matching.py). No DB.
"""

from __future__ import annotations

import itertools
import random

from precis.pcb.pinswap import _cycles, _hungarian


# ── the Hungarian algorithm, verified against brute force ────────────────
def _brute_force_assignment(cost: list[list[float]]) -> float:
    n = len(cost)
    return min(
        sum(cost[i][perm[i]] for i in range(n))
        for perm in itertools.permutations(range(n))
    )


def test_hungarian_matches_brute_force_over_random_matrices():
    rng = random.Random(0)
    for _trial in range(300):
        n = rng.randint(1, 6)
        cost = [[float(rng.randint(0, 20)) for _ in range(n)] for _ in range(n)]
        assign = _hungarian(cost)
        assert sorted(assign) == list(range(n))  # a genuine permutation
        total = sum(cost[i][assign[i]] for i in range(n))
        assert total == _brute_force_assignment(cost)


# ── cycle decomposition realizes the target permutation via pivot swaps ──
def _apply_swaps(state: dict[int, str], pairs: list[tuple[int, int]]) -> dict[int, str]:
    state = dict(state)
    for a, b in pairs:
        state[a], state[b] = state[b], state[a]
    return state


def test_cycle_decomposition_realizes_any_permutation():
    rng = random.Random(1)
    for _trial in range(200):
        n = rng.randint(1, 7)
        perm = list(range(n))
        rng.shuffle(perm)
        pins = list(range(100, 100 + n))
        occupants = {pins[i]: f"net{i}" for i in range(n)}
        pairs: list[tuple[int, int]] = []
        for cyc in _cycles(perm):
            if len(cyc) < 2:
                continue
            pivot = pins[cyc[0]]
            pairs.extend((pivot, pins[idx]) for idx in cyc[1:])
        result = _apply_swaps(occupants, pairs)
        expected = {pins[perm[i]]: occupants[pins[i]] for i in range(n)}
        assert result == expected
