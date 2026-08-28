"""Unit tests for precis.pcb.pinswap — the min-cost bipartite matching
that proposes pin/gate swaps, independent of the OptimizeEngine wiring
(covered in tests/test_pcb_optimize.py). No DB.
"""

from __future__ import annotations

import itertools
import random

from precis.pcb import DEFAULT_STACKUP
from precis.pcb.ir import from_graph
from precis.pcb.pinswap import (
    PinSwapGroup,
    _cycles,
    _hungarian,
    build_cost_matrix,
    propose_reassignment,
    total_group_crossings,
)


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


# ── the fixture: a hand-built two-pin swap that obviously helps ─────────
def _fixture():
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": -5.0, "y": 5.0},
            {"refdes": "U2", "x": 5.0, "y": 5.0},
        ],
        "nets": [
            {
                "name": "A",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "right"},
                    {"refdes": "U1", "pin": "1"},
                ],
            },
            {
                "name": "B",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": "U0", "pin": "left"},
                    {"refdes": "U2", "pin": "1"},
                ],
            },
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    pin_right = next(
        p
        for p in range(ir.n_pins)
        if int(ir.pin_instance[p]) == 0 and str(ir.pin_label[p]) == "right"
    )
    pin_left = next(
        p
        for p in range(ir.n_pins)
        if int(ir.pin_instance[p]) == 0 and str(ir.pin_label[p]) == "left"
    )
    group = PinSwapGroup(
        instance=0,
        pins=(pin_left, pin_right),
        offsets={pin_left: (-1.0, 0.0), pin_right: (1.0, 0.0)},
    )
    return ir, group, pin_left, pin_right


def test_crossing_fixture_crosses_before_and_not_after():
    ir, group, pin_left, pin_right = _fixture()
    assert total_group_crossings(ir, group) == 1
    ir.swap_pins(pin_left, pin_right)
    assert total_group_crossings(ir, group) == 0


def test_propose_reassignment_finds_the_beneficial_swap():
    ir, group, pin_left, pin_right = _fixture()
    pairs = propose_reassignment(ir, group)
    assert pairs == ((pin_left, pin_right),) or pairs == ((pin_right, pin_left),)


def test_propose_reassignment_none_when_no_improvement():
    """Swapping two pins whose airwires are already non-crossing (the
    fixture already swapped) must not propose a no-op."""
    ir, group, pin_left, pin_right = _fixture()
    ir.swap_pins(pin_left, pin_right)  # now optimal already
    assert propose_reassignment(ir, group) is None


def test_propose_reassignment_none_with_fewer_than_two_movable_pins():
    ir, group, _pin_left, pin_right = _fixture()
    single = PinSwapGroup(instance=0, pins=(pin_right,))
    assert propose_reassignment(ir, single) is None


def test_propose_reassignment_none_when_instance_unplaced():
    ir, group, pin_left, pin_right = _fixture()
    ir.inst_x[0] = float("nan")
    assert build_cost_matrix(ir, group) is None
    assert propose_reassignment(ir, group) is None


def test_excluded_pin_never_appears_in_a_proposed_pair():
    ir, group, pin_left, pin_right = _fixture()
    excluded = PinSwapGroup(
        instance=group.instance,
        pins=group.pins,
        offsets=group.offsets,
        excluded=frozenset({pin_right}),
    )
    pairs = propose_reassignment(ir, excluded)
    assert pairs is None  # only one non-excluded pin -- nothing to match


def test_offset_default_is_instance_centroid():
    """A pin absent from `offsets` sits at (0, 0) relative to the
    instance -- degrades to a genuine no-op rather than crashing."""
    ir, _group, pin_left, pin_right = _fixture()
    bare = PinSwapGroup(instance=0, pins=(pin_left, pin_right))  # no offsets at all
    assert (
        total_group_crossings(ir, bare) == 0
    )  # every pin at the same point -> no crossings
    assert propose_reassignment(ir, bare) is None
