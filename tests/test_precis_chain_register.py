"""precis_chain.motif + precis_chain.register — unit arithmetic and helical
register.

The numbers in this file (0.334 rise, 34.286 deg twist, 21-unit / 2-turn and
32-unit / 3-turn repeats, three neighbours at 120 deg) are **test fixtures, not
kernel constants**: the kernel deliberately ships no nucleic-acid vocabulary
(``docs/backlog/precis-chain-kernel.md``, "no DNA numbers here"). They are here
because they are the register arithmetic anyone would check against.

Theorems:

- 21 units at 34.286 deg is two whole turns (720 deg) and 32 units at 33.75 deg
  is three (1080 deg) — so both are commensurate at their own pitch and neither
  is at the other's.
- At 10.5 units/turn, a unit advances 34.286 deg, so units 0, 7 and 14 of a
  21-unit run face the three neighbours of a honeycomb site (0 deg, 240 deg,
  120 deg respectively) and no other unit faces any of them.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from precis_chain.motif import (
    Motif,
    length_for_units,
    turns,
    units_for_length,
    units_per_turn,
)
from precis_chain.register import (
    Lattice,
    commensurate,
    crossover_positions,
    phase_after,
)

_HONEYCOMB_MOTIF = Motif(
    name="test-honeycomb-duplex",
    rise=0.334,
    twist=math.radians(360.0 / 10.5),
    radius=1.0,
    min_bend_radius=10.0,
    persistence_length=50.0,
    contour_per_unit=0.63,
)
_SQUARE_MOTIF = Motif(
    name="test-square-duplex",
    rise=0.334,
    twist=math.radians(33.75),
    radius=1.0,
    min_bend_radius=10.0,
    persistence_length=50.0,
    contour_per_unit=0.63,
)
_HONEYCOMB = Lattice(
    name="test-honeycomb",
    neighbour_azimuths=(0.0, 2.0 * math.pi / 3.0, 4.0 * math.pi / 3.0),
    period_units=21,
)


def test_length_and_unit_arithmetic_round_trips() -> None:
    m = _HONEYCOMB_MOTIF
    for n in (0, 1, 7, 21, 1000):
        assert length_for_units(m, n) == pytest.approx(n * 0.334)
        assert units_for_length(m, length_for_units(m, n)) == n
    # floor, never round: a length that does not quite fit a unit loses it.
    assert units_for_length(m, 0.334 * 5 - 1e-6) == 4
    assert units_for_length(m, 0.0) == 0


def test_turns_and_units_per_turn() -> None:
    assert units_per_turn(_HONEYCOMB_MOTIF) == pytest.approx(10.5)
    assert turns(_HONEYCOMB_MOTIF, 21) == pytest.approx(2.0, abs=1e-4)
    assert turns(_SQUARE_MOTIF, 32) == pytest.approx(3.0, abs=1e-4)
    zero_twist = Motif("flat", 1.0, 0.0, 1.0, 1.0, 1.0, 1.0)
    assert turns(zero_twist, 10) == 0.0
    with pytest.raises(ValueError, match="no helical repeat"):
        units_per_turn(zero_twist)


def test_phase_after_is_the_register_error() -> None:
    # 10.5 units/turn: half a turn short after 21/2 units is not an integer
    # count, but 21 units closes to within 1e-4 rad of two whole turns.
    assert abs(phase_after(_HONEYCOMB_MOTIF, 21)) < 1e-3
    assert abs(phase_after(_SQUARE_MOTIF, 32)) < 1e-3
    # One unit off, and the error is a whole unit of twist.
    assert abs(phase_after(_HONEYCOMB_MOTIF, 22)) == pytest.approx(
        _HONEYCOMB_MOTIF.twist, abs=1e-3
    )
    assert phase_after(_HONEYCOMB_MOTIF, 0) == 0.0
    # Signed and wrapped to [-pi, pi): 7 units is 240 deg, i.e. -120 deg.
    assert phase_after(_HONEYCOMB_MOTIF, 7) == pytest.approx(
        -2.0 * math.pi / 3.0, abs=1e-3
    )


def test_commensurate_needs_both_whole_repeats_and_closed_twist() -> None:
    assert commensurate(_HONEYCOMB_MOTIF, 21, 21)
    assert commensurate(_HONEYCOMB_MOTIF, 42, 21)
    assert not commensurate(_HONEYCOMB_MOTIF, 22, 21)  # mid-repeat
    assert not commensurate(_HONEYCOMB_MOTIF, 20, 21)
    assert commensurate(_SQUARE_MOTIF, 32, 32)
    # A pitch the motif's twist does not actually close on: 21 square-lattice
    # units is 708.75 deg, 11.25 deg short of two turns.
    assert not commensurate(_SQUARE_MOTIF, 21, 21)
    assert abs(phase_after(_SQUARE_MOTIF, 21)) == pytest.approx(
        math.radians(11.25), abs=1e-6
    )


def test_crossover_positions_recur_every_seven_units_on_a_honeycomb() -> None:
    hits = crossover_positions(_HONEYCOMB_MOTIF, 21, _HONEYCOMB)
    assert [unit for unit, _ in hits] == [0, 7, 14]
    # Which neighbour each one faces, recomputed from the accumulated twist.
    for unit, neighbour in hits:
        azimuth = unit * _HONEYCOMB_MOTIF.twist
        target = _HONEYCOMB.neighbour_azimuths[neighbour]
        assert abs((azimuth - target + math.pi) % (2.0 * math.pi) - math.pi) < 1e-3
    assert {n for _, n in hits} == {0, 1, 2}


def test_crossover_positions_follow_phase0_and_the_run_length() -> None:
    shifted = crossover_positions(
        _HONEYCOMB_MOTIF, 21, _HONEYCOMB, phase0=_HONEYCOMB_MOTIF.twist
    )
    assert [unit for unit, _ in shifted] == [6, 13, 20]
    assert crossover_positions(_HONEYCOMB_MOTIF, 7, _HONEYCOMB) == [(0, 0)]
    assert crossover_positions(_HONEYCOMB_MOTIF, 0, _HONEYCOMB) == []


def test_crossover_window_excludes_units_between_two_neighbours() -> None:
    # Unit 3 sits 17.14 deg from the 120 deg neighbour — exactly half a unit of
    # twist away, which the default quarter-unit window must exclude.
    default = crossover_positions(_HONEYCOMB_MOTIF, 21, _HONEYCOMB)
    assert 3 not in [u for u, _ in default]
    wide = crossover_positions(
        _HONEYCOMB_MOTIF, 21, _HONEYCOMB, tol=0.6 * _HONEYCOMB_MOTIF.twist
    )
    assert 3 in [u for u, _ in wide]


def test_per_unit_twist_hook_adds_to_the_nominal_roll() -> None:
    assert phase_after(
        _HONEYCOMB_MOTIF, 21, per_unit_twist=np.zeros(21)
    ) == pytest.approx(phase_after(_HONEYCOMB_MOTIF, 21))
    extra = np.zeros(21)
    extra[4] = -_HONEYCOMB_MOTIF.twist  # one deleted base
    assert phase_after(_HONEYCOMB_MOTIF, 21, per_unit_twist=extra) == pytest.approx(
        phase_after(_HONEYCOMB_MOTIF, 20)
    )
    with pytest.raises(ValueError, match="shape"):
        phase_after(_HONEYCOMB_MOTIF, 21, per_unit_twist=np.zeros(20))


def test_motif_and_lattice_validation() -> None:
    with pytest.raises(ValueError, match="Motif.rise"):
        Motif("bad", 0.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    with pytest.raises(ValueError, match="Motif.radius"):
        Motif("bad", 1.0, 1.0, -1.0, 1.0, 1.0, 1.0)
    with pytest.raises(ValueError, match="Motif.twist must be finite"):
        Motif("bad", 1.0, float("nan"), 1.0, 1.0, 1.0, 1.0)
    with pytest.raises(ValueError, match="neighbour azimuth"):
        Lattice("empty", (), 7)
    with pytest.raises(ValueError, match="period_units"):
        Lattice("bad", (0.0,), 0)
    with pytest.raises(ValueError, match="must be >= 0"):
        length_for_units(_HONEYCOMB_MOTIF, -1)
    with pytest.raises(ValueError, match="must be >= 0"):
        units_for_length(_HONEYCOMB_MOTIF, -1.0)
    with pytest.raises(ValueError, match="pitch_units"):
        commensurate(_HONEYCOMB_MOTIF, 21, 0)
