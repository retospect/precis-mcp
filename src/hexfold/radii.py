"""Carbon radius tables for smooth targets (docs/backlog/precis-surface-kernel.md
"Slice -- smooth drum").

Every radius a graphene shell can take is quantised, so a smooth target is
assembled from table values rather than solved for:

- a tube neck is :func:`hexfold.lattice.tube_radius` of an ``(n, m)``;
- a convex fillet is the radius of an icosahedral fullerene ``C_N``,
  ``N = 20 (h^2 + hk + k^2)``, starting at C60 -- the smallest shell
  whose pyramidalisation sp2 carbon tolerates (C60's POAV1 angle is the
  default bound);
- the radius follows from area, ``4 pi r^2 = N * A_atom`` with
  ``A_atom = (3 sqrt 3 / 4) sigma^2`` (2.62 A^2), which gives C60 3.54 A.

Strain only *chooses* among entries (widest neck, largest fillet that
fits); it never produces a continuous radius that would need snapping.

Pyramidalisation is estimated from mean curvature: three bonds of length
``sigma`` on a surface with principal curvatures ``k1, k2`` tilt out of the
tangent plane by ``theta_p = asin(sigma (k1 + k2) / 4)``. On a sphere this is
``asin(sigma / 2r)``: 11.5 deg for C60 against the POAV1 11.6 deg, 3.0 deg for a
(10,10) tube. Units: Angstrom throughout, like :mod:`hexfold.lattice`.
"""

from __future__ import annotations

import math

from hexfold.lattice import SIGMA_DEFAULT

#: POAV1 pyramidalisation angle of C60 (Haddon), the default bound.
THETA_P_C60_DEG = 11.64


def atom_area(sigma: float = SIGMA_DEFAULT) -> float:
    """Graphene area per atom, ``(3 sqrt 3 / 4) sigma^2``."""
    return 0.75 * math.sqrt(3.0) * sigma * sigma


def fullerene_radius(n_atoms: int, sigma: float = SIGMA_DEFAULT) -> float:
    """Radius of the sphere carrying ``n_atoms`` at graphene density."""
    return math.sqrt(n_atoms * atom_area(sigma) / (4.0 * math.pi))


def icosahedral_sizes(max_atoms: int = 6000) -> list[int]:
    """Icosahedral fullerene atom counts ``20 (h^2 + hk + k^2)`` from C60 up."""
    sizes = {
        20 * (h * h + h * k + k * k) for h in range(1, 40) for k in range(0, h + 1)
    }
    return sorted(n for n in sizes if 60 <= n <= max_atoms)


def fillet_radii(max_atoms: int = 6000, sigma: float = SIGMA_DEFAULT) -> list[float]:
    """Candidate convex-fillet radii, ascending, C60 first."""
    return [fullerene_radius(n, sigma) for n in icosahedral_sizes(max_atoms)]


def theta_p_deg(mean_curvature_sum: float, sigma: float = SIGMA_DEFAULT) -> float:
    """Pyramidalisation estimate from ``k1 + k2`` (1/A)."""
    x = min(1.0, abs(mean_curvature_sum) * sigma / 4.0)
    return math.degrees(math.asin(x))


def curvature_sum_bound(
    theta_p_max_deg: float = THETA_P_C60_DEG, sigma: float = SIGMA_DEFAULT
) -> float:
    """Largest ``k1 + k2`` (1/A) whose :func:`theta_p_deg` stays in bound."""
    return 4.0 * math.sin(math.radians(theta_p_max_deg)) / sigma
