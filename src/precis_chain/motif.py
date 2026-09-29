"""The repeat-unit description a helical chain is built from — and the
length/unit/turn arithmetic over it.

A :class:`Motif` is seven numbers describing one repeating unit of a helical
polymer: how far it advances along the axis, how far it rolls, how far the
backbone sits off the axis, how tightly the whole thing may bend, how far it
stays straight on its own, and how much single-strand contour one unit costs
when the helix is *not* formed.

**No chemistry lives here.** There is deliberately no ``B_DNA`` constant, no
base-pair vocabulary and no nucleic-acid default anywhere in this package —
the binding (``precis_se.chain.nucleic``) owns every number and cites its
source. The kernel only knows that units
repeat.

All lengths are in the caller's single length unit and all angles are in
radians. Mixing units across a :class:`Motif`'s fields is the one way to get
silently wrong answers out of this package, so construct one per unit system
and stay in it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Fractional slack allowed when deciding how many whole units fit a length —
#: ``units_for_length`` accepts a unit whose rise overshoots by at most this
#: relative amount. Without it, a length computed as ``n * rise`` in floating
#: point yields ``n - 1`` units about half the time.
_FIT_RTOL = 1e-9


@dataclass(frozen=True)
class Motif:
    """One helical repeat unit.

    Attributes
    ----------
    name:
        Caller's label, e.g. ``"B-DNA"``, ``"A-RNA"``, ``"polyproline-II"``.
        Never interpreted here.
    rise:
        Axial advance per unit, length.
    twist:
        Roll per unit about the axis, radians. Positive = right-handed about
        the path tangent (the :mod:`precis_chain.frames` sign convention).
    radius:
        Distance from the axis to the backbone, length. This is the radius a
        backbone exit sits at (:func:`precis_chain.fibre.backbone_exit`) and
        the natural capsule radius for the duplex as a tube.
    min_bend_radius:
        Tightest centre-line radius of curvature the unit tolerates, length.
        A design bending tighter is reported by
        :func:`precis_chain.curvature.min_bend_radius_violations`.
    persistence_length:
        The unit's own persistence length, length — the stiffness scale a
        relax pass converts into a hinge spring constant
        (:func:`precis_chain.relax.hinge_stiffness`).
    contour_per_unit:
        Backbone contour length one unit costs when it is *not* part of the
        helix — the single-strand bond length used for loop feasibility
        (:mod:`precis_chain.loop`). For a duplex motif this is the
        single-strand value, not the rise.
    """

    name: str
    rise: float
    twist: float
    radius: float
    min_bend_radius: float
    persistence_length: float
    contour_per_unit: float

    def __post_init__(self) -> None:
        for field_name in (
            "rise",
            "radius",
            "min_bend_radius",
            "persistence_length",
            "contour_per_unit",
        ):
            value = float(getattr(self, field_name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(
                    f"Motif.{field_name} must be a finite positive length, "
                    f"got {value!r} (motif {self.name!r})"
                )
        if not math.isfinite(float(self.twist)):
            raise ValueError(f"Motif.twist must be finite, got {self.twist!r}")


def length_for_units(motif: Motif, n: int) -> float:
    """Axial length spanned by ``n`` units — ``n * motif.rise``. Negative
    ``n`` is refused: a chain cannot run backwards through its own motif."""
    if n < 0:
        raise ValueError(f"unit count must be >= 0, got {n}")
    return float(n) * motif.rise


def units_for_length(motif: Motif, length: float) -> int:
    """Whole units that fit in ``length`` along the axis — ``floor`` of the
    ratio, with :data:`_FIT_RTOL` slack so ``units_for_length(m,
    length_for_units(m, n)) == n`` round-trips exactly.

    Floor, not round: a caller laying out a helix inside an envelope must not
    be handed a unit count that overruns it. A caller who wants the nearest
    count rounds the ratio itself.
    """
    if length < 0.0:
        raise ValueError(f"length must be >= 0, got {length}")
    return math.floor(length / motif.rise * (1.0 + _FIT_RTOL))


def turns(motif: Motif, n: int) -> float:
    """Whole-and-fractional turns of roll accumulated over ``n`` units —
    ``n * twist / (2 pi)``. Signed with ``twist``."""
    if n < 0:
        raise ValueError(f"unit count must be >= 0, got {n}")
    return float(n) * motif.twist / (2.0 * math.pi)


def units_per_turn(motif: Motif) -> float:
    """``2 pi / |twist|`` — the motif's helical repeat in units (10.5 for
    B-DNA). Raises on a zero-twist motif, which has no turn to speak of."""
    if abs(motif.twist) < 1e-15:
        raise ValueError(
            f"motif {motif.name!r} has zero twist — it has no helical repeat"
        )
    return 2.0 * math.pi / abs(motif.twist)
