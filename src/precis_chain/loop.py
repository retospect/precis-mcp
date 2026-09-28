"""Flexible loops between two fixed backbone exits: can they reach, how
strained are they, and what shape do they take.

**The (n+1)-bond contour convention — the one arithmetic fact this module
exists to pin down.** A loop of ``n`` units bridges two *already-placed*
backbone exits, so it contributes ``n + 1`` backbone bonds: one from the
upstream exit into the first loop unit, ``n - 1`` between loop units, and one
from the last loop unit into the downstream exit. Its contour is therefore

``contour(n, c) = (n + 1) * c``

and **not** ``n * c``. The consequence the design layer cares about: a zero-unit
loop is a real connection with one bond of reach, so a crossover between two
helices is feasible exactly when their exits are within one bond of each other.
Counting ``n * c`` would make a 0-unit crossover impossible and every loop one
bond short.

``c`` is the per-unit backbone contour length
(:attr:`precis_chain.motif.Motif.contour_per_unit`), in the caller's length
unit. Energies are in units of ``kT`` — the ideal-chain free energy is
dimensionless once the extension is measured in contour units, so no
temperature enters anywhere.
"""

from __future__ import annotations

import math

import numpy as np

from precis_chain.path import Path, hermite


def contour(n: int, c: float) -> float:
    """``(n + 1) * c`` — the backbone contour an ``n``-unit loop spans. See the
    module docstring for why the ``+1`` is there."""
    if n < 0:
        raise ValueError(f"loop unit count must be >= 0, got {n}")
    if c <= 0.0:
        raise ValueError(f"per-unit contour c must be positive, got {c}")
    return float(n + 1) * c


def loop_feasible(
    p: np.ndarray, q: np.ndarray, n: int, c: float, tol: float = 0.0
) -> bool:
    """Can an ``n``-unit loop reach from exit ``p`` to exit ``q``?

    ``|p - q| <= (n + 1) * c + tol``. The bound is the loop's fully-extended
    contour, so feasibility here means *geometrically possible*, not
    *unstrained* — a loop at exactly its contour has essentially zero
    conformational entropy (see :func:`loop_slack_energy`).

    ``tol`` is a slack in the caller's length unit, for the measurement error
    in wherever the exits came from. The comparison is inclusive, so a loop
    stretched to exactly its contour is feasible.
    """
    dist = float(
        np.linalg.norm(np.asarray(q, dtype=float) - np.asarray(p, dtype=float))
    )
    return dist <= contour(n, c) + tol


def min_units(p: np.ndarray, q: np.ndarray, c: float) -> int:
    """The fewest units a loop from ``p`` to ``q`` needs — the smallest ``n``
    with ``(n + 1) * c >= |p - q|``, floored at 0.

    Inverse of :func:`loop_feasible` at ``tol = 0``, and the number a layout
    pass wants when it is choosing loop lengths rather than checking them.
    """
    if c <= 0.0:
        raise ValueError(f"per-unit contour c must be positive, got {c}")
    dist = float(
        np.linalg.norm(np.asarray(q, dtype=float) - np.asarray(p, dtype=float))
    )
    return max(0, math.ceil(dist / c - 1.0 - 1e-12))


def loop_slack_energy(p: np.ndarray, q: np.ndarray, n: int, c: float) -> float:
    """The ideal-chain stretching free energy of an ``n``-unit loop spanning
    ``p`` to ``q``, in ``kT``.

    ``3 |p - q|^2 / (2 (n + 1) c^2)`` — the Gaussian-chain result for a
    freely-jointed chain of ``n + 1`` bonds of length ``c`` held at end-to-end
    extension ``|p - q|``. Strictly decreasing in ``n`` at fixed extension
    (more units, more slack, less strain), which is what makes it usable as a
    "this loop is taut" signal ranked across a design.

    Deliberately *not* capped at the contour: the Gaussian model has no finite
    extensibility, so an infeasible loop reports a large finite energy rather
    than infinity. Feasibility is :func:`loop_feasible`'s job, and a caller
    should ask that question first — this number is only meaningful inside the
    reachable regime, and a Gaussian chain in any case underestimates strain
    as the extension approaches the contour.
    """
    dist = float(
        np.linalg.norm(np.asarray(q, dtype=float) - np.asarray(p, dtype=float))
    )
    bonds = contour(n, c) / c
    return 3.0 * dist * dist / (2.0 * bonds * c * c)


def loop_curve(
    p: np.ndarray,
    tp: np.ndarray,
    q: np.ndarray,
    tq: np.ndarray,
    n: int,
    c: float,
    *,
    samples: int = 32,
) -> Path:
    """A smooth loop path leaving ``p`` along ``tp`` and arriving at ``q``
    along ``tq``.

    A cubic Hermite span whose velocity magnitudes are both set to the loop's
    contour ``(n + 1) * c``, so a loop with plenty of slack bows out and a taut
    one runs nearly straight. ``tp``/``tq`` need not be unit vectors — only
    their directions are used, the magnitudes come from the contour.

    The result's arc length is *close to but not equal to* the contour: making
    it exact needs a solve, and the loop's real conformation is an ensemble
    rather than a curve. This is a seed for a relax pass and a shape for a
    renderer, and :attr:`~precis_chain.path.Path.length` reports what it
    actually came out as. ``c`` is a parameter here (the spec's signature omits
    it) because ``n`` alone carries no length scale and the kernel has no
    default bond length to supply.
    """
    scale = contour(n, c)
    dir_p = np.asarray(tp, dtype=float).reshape(3)
    dir_q = np.asarray(tq, dtype=float).reshape(3)
    norm_p = float(np.linalg.norm(dir_p))
    norm_q = float(np.linalg.norm(dir_q))
    if norm_p < 1e-12 or norm_q < 1e-12:
        raise ValueError("loop_curve needs non-zero exit tangents tp and tq")
    return hermite(
        np.asarray(p, dtype=float).reshape(3),
        scale * dir_p / norm_p,
        np.asarray(q, dtype=float).reshape(3),
        scale * dir_q / norm_q,
        samples=samples,
    )
