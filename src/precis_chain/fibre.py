"""Placing repeat units along a path: one origin and one frame per unit, and
the backbone exit points that hang off them.

This is where a :class:`~precis_chain.motif.Motif` meets a
:class:`~precis_chain.path.Path`. :func:`unit_frames` walks the path at the
motif's rise, builds a rotation-minimizing frame at each unit and rolls it by
the accumulated twist; :func:`backbone_exit` then takes a strand's constant
azimuth in that unit frame and returns where its backbone actually sits.

Two azimuths describe a duplex's two strands; a third (a major-groove strand,
a triplex) is the binding's business, as is what any of them mean chemically.
The kernel only rotates vectors.

**Unit indexing.** Unit ``k`` sits at arc length ``k * motif.rise`` along the
path, so ``n_units`` units need ``(n_units - 1) * motif.rise`` of path — the
last unit's *origin*, not its far end. A helix physically occupies
``n_units * rise``; sizing the path for that is the caller's call and the
kernel does not insist on it, because a design routinely runs a helix right to
the end of its authored centre line.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from precis_chain.frames import apply_twist, rmf_double_reflection
from precis_chain.motif import Motif
from precis_chain.path import Path, sample_at

#: Relative slack when checking a path is long enough for ``n_units`` — the
#: same reason :mod:`precis_chain.motif` needs one: a path built as
#: ``n * rise`` misses it in the last bit about half the time.
_FIT_RTOL = 1e-9


@dataclass(frozen=True)
class UnitFrames:
    """Where every repeat unit of a helix sits, and how it is rolled.

    Attributes
    ----------
    origins:
        ``(n_units, 3)`` axis position of each unit.
    frames:
        ``(n_units, 3, 3)`` frame per unit, columns ``(t, n, b)`` — tangent
        along the helix axis, normal at the unit's own accumulated azimuth.
    s:
        ``(n_units,)`` arc length of each unit along the path
        (``k * motif.rise``).
    """

    origins: np.ndarray
    frames: np.ndarray
    s: np.ndarray

    def __len__(self) -> int:
        return int(self.origins.shape[0])


def unit_frames(
    path: Path,
    motif: Motif,
    n_units: int,
    phase0: float = 0.0,
    *,
    r0: np.ndarray | None = None,
) -> UnitFrames:
    """Place ``n_units`` units of ``motif`` along ``path``.

    ``phase0`` is the azimuth of unit 0's normal, radians — the design's
    register offset. ``r0`` seeds the rotation-minimizing frame (default: the
    world axis least aligned with the first tangent, so the result is
    deterministic but the absolute azimuth is arbitrary; pass an explicit
    ``r0`` when the design's azimuths must mean something in world terms, e.g.
    "toward the lattice neighbour at 12 o'clock").

    The frame field is built from the **unit-spaced samples themselves**, not
    from the path's own (possibly finer) sampling. At a rise three orders of
    magnitude below the bend radius the double-reflection RMF's second-order
    error is ~1e-3 rad, which is well under any register tolerance; a caller
    wanting more should hand in a finer motif, not a finer path.
    """
    if n_units < 0:
        raise ValueError(f"n_units must be >= 0, got {n_units}")
    if n_units == 0:
        return UnitFrames(np.zeros((0, 3)), np.zeros((0, 3, 3)), np.zeros(0))
    needed = float(n_units - 1) * motif.rise
    if path.length * (1.0 + _FIT_RTOL) < needed:
        raise ValueError(
            f"path is {path.length:g} long but {n_units} units of motif "
            f"{motif.name!r} need {needed:g} (rise {motif.rise:g}) to place "
            "every unit origin — shorten the run or lengthen the path"
        )
    s = np.arange(n_units, dtype=float) * motif.rise
    origins, tangents = sample_at(path, s)
    if r0 is None:
        t0 = tangents[0]
        least = int(np.argmin(np.abs(t0)))
        seed = np.zeros(3)
        seed[least] = 1.0
    else:
        seed = np.asarray(r0, dtype=float).reshape(3)
    frames = rmf_double_reflection(origins, tangents, seed)
    angles = phase0 + np.arange(n_units, dtype=float) * motif.twist
    return UnitFrames(origins, apply_twist(frames, 1.0, angles), s)


def backbone_exit(
    frames: np.ndarray,
    motif: Motif,
    azimuth: float,
    origins: np.ndarray | None = None,
) -> np.ndarray:
    """Where a strand at constant ``azimuth`` sits, relative to the unit frames.

    ``frames`` is ``(3, 3)`` or ``(N, 3, 3)``; ``azimuth`` is radians in the
    unit frame — 0 along the frame's normal, ``+pi/2`` along its binormal, the
    same convention :class:`precis_chain.register.Lattice` uses. The offset is
    ``motif.radius`` from the axis.

    With ``origins`` (``(3,)`` or ``(N, 3)``, matching ``frames``) the result
    is the backbone point in world coordinates; without it, the offset vector
    from the axis, which is what a caller composing its own transform wants.
    Shape follows ``frames``: ``(3,)`` in, ``(3,)`` out.

    Note this is the *exit* the loop machinery pins to: a duplex's two strands
    are two azimuths, and the gap between two helices' exits — not between
    their axes — is what a loop's contour has to span.
    """
    f = np.asarray(frames, dtype=float)
    single = f.ndim == 2
    if single:
        f = f[None, :, :]
    if f.ndim != 3 or f.shape[1:] != (3, 3):
        raise ValueError(f"frames must be (3, 3) or (N, 3, 3), got {np.shape(frames)}")
    offset = motif.radius * (
        np.cos(azimuth) * f[:, :, 1] + np.sin(azimuth) * f[:, :, 2]
    )
    if origins is not None:
        org = np.asarray(origins, dtype=float).reshape(-1, 3)
        if org.shape[0] != f.shape[0]:
            raise ValueError(
                f"origins has {org.shape[0]} rows but frames has {f.shape[0]}"
            )
        offset = org + offset
    return offset[0] if single else offset
