"""precis_surface — lattice-agnostic smooth-surface kernel
(docs/backlog/precis-surface-kernel.md).

Surfaces as a first-class geometric object, independent of any one
lattice or material: patches, rims as Dirichlet curves, seams as Plateau
film clusters, curvature bound in caller units, mesh realisation.
`hexfold.smooth` is the carbon binding on top of this (hex-lattice
singularity charges, the discretiser that emits a ``.hx`` section, the
``smooth:`` grammar) -- `precis_surface -> hexfold`, hexfold never
imports precis. Useful for any surface-shaped thing (membranes, the
30 nm oval box tiling), not only carbon.

House rules, same as :mod:`precis.structsolve` and :mod:`precis.cad`:
**pure functions over passed-in arrays, no store access** -- handlers own
IO and units. Every function is unit-agnostic (metres, bond-lengths, the
numbers never know); numpy only, scipy is not a core dependency.

Slice 1 ("the dual route", docs/backlog/precis-surface-kernel.md) pipeline:
level set -> periodic marching cubes -> [dual route mesh ops, later
slices] -> dualise -> hexfold net -> stick relaxation. This package holds
the geometric half of that pipeline; the discretiser and net live in
``hexfold.smooth``.

Modules:

- :mod:`precis_surface.level_set` -- closed-form TPMS nodal
  approximations (Schwarz P, Schwarz D, gyroid) as ``(N, 3)`` point
  array -> ``(N,)`` value functions, parameterised by cell edge ``a``.
- :mod:`precis_surface.periodic_mesh` -- marching cubes over one
  periodic cell, producing the welded-quotient-ready mesh: the contract
  ``Mesh = (verts, tris)`` tuple plus ``wrap`` (identified vertex-index
  pairs across opposite cell faces) and the cell's lattice matrix, and
  the helpers (:func:`~precis_surface.periodic_mesh.welded_euler`,
  :func:`~precis_surface.periodic_mesh.is_edge_manifold_closed`) that
  read the welded quotient's topology.
- :mod:`precis_surface.curvature` -- discrete differential-geometry
  operators (angle-defect and cotangent-Laplacian Gaussian/mean
  curvature) on a bare ``Mesh``, independent of how it was built.
"""

from __future__ import annotations
