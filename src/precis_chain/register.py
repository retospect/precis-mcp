"""Helical register: where a helix's roll has got to after ``n`` units, and
which units point at a lattice neighbour.

Register is the arithmetic behind every crossover rule in origami-style
design: a helix may only hand a strand to a neighbour at a unit whose
backbone azimuth happens to face that neighbour, and a run of ``n`` units is
only reusable as a lattice repeat when its accumulated roll closes on a whole
number of turns.

**No lattice constants live here.** ``7 bp``, ``21 bp / 2 turns``,
``32 bp / 3 turns`` and the honeycomb/square neighbour directions are the
binding's numbers (``precis_se.chain.nucleic``); the kernel takes a
:class:`Lattice` describing whatever geometry the caller has.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from precis_chain.motif import Motif

#: Default register tolerance, radians. ``21 * 34.286 deg`` misses two whole
#: turns by 1.0e-4 rad, so a tolerance any tighter than that would call the
#: honeycomb repeat incommensurate; anything looser than ~1 deg starts
#: accepting genuinely strained register. 1e-3 rad (0.057 deg) sits between.
REGISTER_TOL = 1e-3

#: Default crossover azimuth window, as a fraction of one unit's twist. A
#: quarter-unit (rather than the half-unit that would tile the circle exactly)
#: is the documented choice for the open numeric in the spec: at a half-unit
#: window the unit *between* two candidate azimuths sits exactly on the
#: boundary and inclusion becomes a floating-point coin flip.
CROSSOVER_WINDOW_FRACTION = 0.25


@dataclass(frozen=True)
class Lattice:
    """The neighbour geometry a helix is registered against.

    Attributes
    ----------
    name:
        Caller's label, e.g. ``"honeycomb"``, ``"square"``. Never interpreted.
    neighbour_azimuths:
        Azimuth of each neighbour, radians, measured in the unit frame the
        same way :func:`precis_chain.fibre.backbone_exit` measures one: 0 is
        the frame's normal, ``+pi/2`` its binormal. Three entries for a
        honeycomb site, four for a square one — the kernel does not care how
        many.
    period_units:
        The lattice's structural repeat in units: the run length after which
        the whole register pattern recurs. Used only by
        :func:`commensurate`.
    """

    name: str
    neighbour_azimuths: tuple[float, ...]
    period_units: int

    def __post_init__(self) -> None:
        if not self.neighbour_azimuths:
            raise ValueError(f"lattice {self.name!r} needs >= 1 neighbour azimuth")
        if self.period_units < 1:
            raise ValueError(
                f"lattice {self.name!r} period_units must be >= 1, "
                f"got {self.period_units}"
            )


def _wrap_pi(angle: float) -> float:
    """Wrap to ``[-pi, pi)`` — the same half-open convention as
    :func:`precis_chain.frames.twist_between`."""
    return float((angle + math.pi) % (2.0 * math.pi) - math.pi)


def phase_after(
    motif: Motif, n: int, *, per_unit_twist: np.ndarray | None = None
) -> float:
    """The helix's roll after ``n`` units, wrapped to ``[-pi, pi)``.

    Read either way, and they are the same number: it is the signed azimuth
    offset of unit ``n``'s backbone from unit 0's, and it is the **register
    error** — how far the run misses closing on a whole number of turns. Zero
    means unit ``n`` is rolled exactly back onto unit 0.

    ``per_unit_twist`` is the insertion/deletion hook: a ``(n,)`` array of
    additive per-unit twist perturbations, radians, for designs that add or
    drop a base to retune register (``+motif.twist`` for an inserted base,
    ``-motif.twist`` for a deleted one). They add to the nominal
    ``n * motif.twist`` before wrapping.
    """
    if n < 0:
        raise ValueError(f"unit count must be >= 0, got {n}")
    if per_unit_twist is not None:
        extra = np.asarray(per_unit_twist, dtype=float)
        if extra.shape != (n,):
            raise ValueError(
                f"per_unit_twist must have shape ({n},), got {extra.shape}"
            )
        return _wrap_pi(float(n) * motif.twist + float(extra.sum()))
    return _wrap_pi(float(n) * motif.twist)


def commensurate(
    motif: Motif, n: int, pitch_units: int, tol: float = REGISTER_TOL
) -> bool:
    """Is a run of ``n`` units a whole number of lattice repeats *and*
    rolled back into register?

    Both halves must hold:

    - ``n`` is an exact integer multiple of ``pitch_units`` (the structural
      repeat — 21 units for a two-turn honeycomb repeat, 32 for a three-turn
      square one), and
    - ``|phase_after(motif, n)| <= tol``: the accumulated roll closes on a
      whole number of turns.

    The two are separate conditions on purpose. A run can close in twist and
    still be unusable because it ends mid-repeat, and a run can be a whole
    number of repeats of a motif whose twist does not actually close (which is
    exactly the strain a ``twist_register`` finding reports).
    """
    if n < 0:
        raise ValueError(f"unit count must be >= 0, got {n}")
    if pitch_units < 1:
        raise ValueError(f"pitch_units must be >= 1, got {pitch_units}")
    if tol < 0.0:
        raise ValueError(f"tol must be >= 0 radians, got {tol}")
    if n % pitch_units != 0:
        return False
    return abs(phase_after(motif, n)) <= tol


def crossover_positions(
    motif: Motif,
    n: int,
    lattice: Lattice,
    *,
    phase0: float = 0.0,
    tol: float | None = None,
) -> list[tuple[int, int]]:
    """Units of a helix whose **own azimuth** faces a lattice neighbour.

    Returns ``(unit_index, neighbour_index)`` pairs for every unit in
    ``range(n)`` whose azimuth ``phase0 + unit * motif.twist`` lands within
    ``tol`` radians of ``lattice.neighbour_azimuths[neighbour_index]``, sorted
    by unit then neighbour.

    The azimuth compared is the unit frame's own — the binding decides what
    that reference means and where any strand sits relative to it
    (:func:`precis_chain.fibre.backbone_exit` takes a strand's azimuth as a
    parameter for the same reason). A caller whose strands are *not* at the
    unit azimuth calls this once per strand with ``phase0`` shifted by that
    strand's offset; a caller whose two strands are not antipodal gets two
    different answers, which is a fact about its chemistry and not about this
    function.

    ``tol`` defaults to :data:`CROSSOVER_WINDOW_FRACTION` times one unit's
    twist — a quarter-unit window, so at most one unit per neighbour per turn
    qualifies and none sits on the boundary (see that constant's note).

    A unit facing two neighbours within the window appears twice; with a
    sensible lattice that cannot happen, and the kernel does not police it.
    """
    if n < 0:
        raise ValueError(f"unit count must be >= 0, got {n}")
    window = CROSSOVER_WINDOW_FRACTION * abs(motif.twist) if tol is None else float(tol)
    if window < 0.0:
        raise ValueError(f"tol must be >= 0 radians, got {tol}")
    out: list[tuple[int, int]] = []
    for unit in range(n):
        azimuth = phase0 + float(unit) * motif.twist
        for k, target in enumerate(lattice.neighbour_azimuths):
            if abs(_wrap_pi(azimuth - target)) <= window:
                out.append((unit, k))
    return out
