"""Shared cylinder-wrap mapping — the ``(u, v)`` frame a pattern spec wraps.

A pattern defined on the unrolled cylinder (circumferential ``u ∈ [0, 1)``,
periodic; axial ``v``) is realised two ways in this monorepo, and slice 4 of
``docs/backlog/se-cylinder-wrap-mold.md`` factors the shared core out here so
one spec drives both:

- **macro** — a height relief: :class:`precis.cad.primitives.CylRelief`
  samples ``h(u, v)`` on the tube wall and raises/sinks the surface by it;
- **nano** — honeycomb lattice sites: hexfold's roll-up places atoms whose
  circumferential coordinate (:func:`hexfold.lattice.circumferential`) *is*
  this same ``u`` (:func:`hexfold.lattice.site_uv`).

Both share one object: the periodic circumferential coordinate, the helical
``twist`` that couples axial travel to azimuth (the macro knob; on the nano
side the tube's chiral screw plays the same role), and the closure (seam)
rule that a pattern must be periodic across ``u = 0 ≡ 1``.

This module is the dimension-free core — numpy and the standard library only,
no hexfold or precis internals, so it respects the hexfold fold-in boundary
(``tests/test_hexfold_import_boundary.py``) while the macro side imports *down*
into it. Radius and feature depth never appear: the mapping is scale-free, so
the same code serves µm lattice sites and 50 cm reliefs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

#: A scalar coordinate or an array of them — the mapping is vectorised so the
#: relief's pull grids and a single lattice-site query share one code path.
Coord = Any


def canonical_u(x: Coord) -> Any:
    """Fold a circumferential coordinate into the periodic domain ``[0, 1)``.

    ``u`` wraps: ``u + 1 ≡ u``. Both realisations reduce a raw azimuth
    fraction through this one helper so "which column of the map" and "which
    site around the tube" agree on the fundamental domain.
    """
    return np.mod(x, 1.0)


@dataclass(frozen=True)
class WrapSpec:
    """How a flat ``(u, v)`` pattern wraps onto a cylinder.

    ``repeat`` tiles the pattern ``repeat`` times around the circumference
    (``repeat >= 1``); ``twist`` shears it by that many radians of azimuth
    over the full axial span ``v ∈ [0, 1]`` (``0`` = axis-parallel, ``> 0`` =
    helical). Dimension-free by construction: the tube radius and the feature
    depth live on the consumer (the relief leaf, the lattice), never here, so
    a :class:`WrapSpec` is the part of a pattern spec that is the same at any
    scale.
    """

    repeat: int = 1
    twist: float = 0.0

    def __post_init__(self) -> None:
        if int(self.repeat) < 1:
            raise ValueError(f"wrap repeat must be >= 1, got {self.repeat}")

    def theta(self, u: Coord, v: Coord) -> Any:
        """Tube azimuth (radians) at unrolled map coordinate ``(u, v)``.

        ``θ = (2π·u − twist·v) / repeat`` — the forward map the relief's flank
        raster and pull grids evaluate on (``precis.cad.hmap`` /
        ``precis.cad.mold``).
        """
        u_a = np.asarray(u, dtype=np.float64)
        v_a = np.asarray(v, dtype=np.float64)
        return (2.0 * math.pi * u_a - self.twist * v_a) / self.repeat

    def u_of(self, u_cyl: Coord, v: Coord) -> Any:
        """Map coordinate ``u ∈ [0, 1)`` for tube azimuth-fraction
        ``u_cyl = θ / 2π`` at axial ``v`` — the inverse of :meth:`theta`, used
        by the leaf's point query to look a world point up in the map.
        """
        u_cyl_a = np.asarray(u_cyl, dtype=np.float64)
        v_a = np.asarray(v, dtype=np.float64)
        return canonical_u(self.repeat * u_cyl_a + self.twist * v_a / (2.0 * math.pi))


def seam_error(values: NDArray[np.floating[Any]]) -> float:
    """The wrap discontinuity of a sampled map: ``max |m[0, :] − m[-1, :]|``.

    ``u`` is periodic, so a map's first and last columns are neighbours; a
    nonzero residual is a cliff in the realised pattern. The *tolerance*
    policy (which residual is acceptable) stays with the consumer — this is
    the raw residual both sides measure.
    """
    m = np.asarray(values)
    return float(np.max(np.abs(m[0, :] - m[-1, :])))
